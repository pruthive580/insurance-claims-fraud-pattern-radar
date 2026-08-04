"""FastAPI backend for Fraud Pattern Radar.

Endpoints:
  GET  /                  -> the single-page dashboard
  GET  /api/health        -> LLM/offline status + counts
  GET  /api/claims        -> scored claim summaries (sorted by risk)
  GET  /api/claims/{id}   -> full detail: fired rules, narrative, raw claim
  GET  /api/graph         -> fraud-ring network (nodes + edges + rings), band-coloured
  POST /api/score         -> preview-score an ad-hoc claim (does NOT persist)
  POST /api/claims        -> add a claim to the book, re-score, and persist it
  POST /api/reset         -> reset the book back to the seed dataset

The book is held in memory and re-scored on every mutation, so a claim added at
intake survives page refreshes and even re-links existing claims into its ring.
Restart the server (or POST /api/reset) for a clean slate.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import llm
from .data.samples import build_book, build_context
from .models import Claim
from .scoring import score_book

app = FastAPI(title="Fraud Pattern Radar", version="1.1.0")

_WEB = Path(__file__).parent / "web"
# Claims added at intake are persisted here so they SURVIVE a server restart.
# Kept separate from the seed (claims.json) so the seed stays pristine; "Reset"
# deletes this overlay to return to the clean seed.
_RUNTIME = Path(__file__).parent / "data" / "runtime_claims.json"


class BookState:
    """Book of claims + derived views. Seed comes from claims.json; runtime
    additions are persisted to runtime_claims.json and reloaded on startup, so
    intake claims survive a restart. reset() clears the overlay back to the seed."""

    def __init__(self):
        self._load()

    def _read_runtime(self) -> list[Claim]:
        if not _RUNTIME.exists():
            return []
        try:
            return [Claim(**c) for c in json.loads(_RUNTIME.read_text())]
        except Exception:
            return []   # corrupt/incompatible overlay -> ignore, fall back to seed

    def _write_runtime(self):
        runtime = [c for c in self.book if c.id not in self.seed_ids]
        _RUNTIME.parent.mkdir(exist_ok=True)
        _RUNTIME.write_text(json.dumps(
            [json.loads(c.model_dump_json()) for c in runtime], indent=2))

    def _load(self):
        from . import custom_rules
        self.seed = build_book()
        self.seed_ids = {c.id for c in self.seed}
        runtime = self._read_runtime()
        runtime_ids = {c.id for c in runtime}
        self.book: list[Claim] = [c for c in self.seed
                                  if c.id not in runtime_ids] + runtime
        self.ctx = build_context(self.book)
        self.custom_rules = custom_rules.load()   # human-adopted rules
        self._rescore()

    def reload_custom(self):
        from . import custom_rules
        self.custom_rules = custom_rules.load()
        self._rescore()

    def reset(self):
        """Drop runtime additions and return to the pristine seed."""
        _RUNTIME.unlink(missing_ok=True)
        self._load()

    def add(self, claim: Claim):
        # Replace any existing claim with the same id (idempotent re-submits).
        self.book = [c for c in self.book if c.id != claim.id] + [claim]
        self._write_runtime()   # persist so it survives a restart
        self._rescore()

    def _rescore(self):
        # Bulk-score deterministically (fast) — the AI note is generated lazily
        # per claim when it's opened, so adding a claim never blocks on the LLM.
        self.results, self.graph = score_book(
            self.book, self.ctx, use_llm=False,
            custom_rules=getattr(self, "custom_rules", None))
        self.score_by_id = {r.claim_id: r for r in self.results}
        self.claim_by_id = {c.id: c for c in self.book}
        self.band_by_id = {r.claim_id: r.band for r in self.results}
        self.narr_cache: dict[str, str] = {}   # claim_id -> AI narrative (lazy)


STATE = BookState()


# Serve the HTML with no-cache so edits always show on refresh (no stale pages).
_NOCACHE = {"Cache-Control": "no-store, max-age=0"}


@app.get("/")
def index():
    return FileResponse(_WEB / "index.html", headers=_NOCACHE)


@app.get("/arch")
def arch():
    """The architecture deep-dive deck — how it works under the hood."""
    return FileResponse(_WEB / "architecture.html", headers=_NOCACHE)


@app.get("/api/health")
def health():
    return {"status": llm.status_label(), "ai_mode": llm.available(),
            "claims": len(STATE.book), "rings": len(STATE.graph["rings"])}


def _summary(r):
    claim = STATE.claim_by_id[r.claim_id]
    return {
        "claim_id": r.claim_id,
        "claim_type": claim.claim_type,
        "claimant": claim.party.name,
        "amount": claim.amount,
        "score": r.score,
        "band": r.band,
        "action": r.action,
        "ring_ids": r.ring_ids,
        "rule_count": len(r.fired),
    }


@app.get("/api/claims")
def claims():
    return [_summary(r) for r in STATE.results]


@app.get("/api/claims/{claim_id}")
def claim_detail(claim_id: str):
    r = STATE.score_by_id.get(claim_id)
    claim = STATE.claim_by_id.get(claim_id)
    if r is None or claim is None:
        raise HTTPException(status_code=404, detail="claim not found")
    # Loads instantly (deterministic note). The AI note streams in separately via
    # /api/claims/{id}/narrative so the detail view never blocks on the model.
    return {"score": r.model_dump(), "claim": claim.model_dump(mode="json")}


@app.get("/api/claims/{claim_id}/narrative")
def claim_narrative(claim_id: str):
    """AI-written investigator note (Ollama/Claude), generated on demand and
    cached per claim. The slow model call lives here, off the detail path."""
    r = STATE.score_by_id.get(claim_id)
    claim = STATE.claim_by_id.get(claim_id)
    if r is None or claim is None:
        raise HTTPException(status_code=404, detail="claim not found")
    if not llm.available():
        return {"narrative": r.narrative, "ai": False}
    if claim_id not in STATE.narr_cache:
        from .scoring import llm_narrative
        STATE.narr_cache[claim_id] = (
            llm_narrative(claim, r.fired, r.band, r.ring_ids) or r.narrative)
    return {"narrative": STATE.narr_cache[claim_id], "ai": True}


@app.get("/api/rules")
def rules():
    """The R1–R16 catalog joined with LIVE firing data from the current book:
    for each rule, how many claims it fired on, which claims, and a sample reason."""
    from .rules import RULE_CATALOG

    hits: dict[str, list[dict]] = {}
    for r in STATE.results:
        for f in r.fired:
            hits.setdefault(f.rule_id, []).append(
                {"claim_id": r.claim_id, "band": r.band, "reason": f.reason})

    out = []
    for rule in RULE_CATALOG:
        fired = hits.get(rule["rule_id"], [])
        out.append({
            **rule,
            "fired_count": len(fired),
            "claims": [f["claim_id"] for f in fired],
            "example_reason": fired[0]["reason"] if fired else None,
        })
    # Adopted custom rules appear right alongside the built-ins.
    for cr in STATE.custom_rules:
        fired = hits.get(cr["id"], [])
        out.append({
            "rule_id": cr["id"], "category": cr.get("category", "Custom (adopted)"),
            "points": cr["points"], "name": cr["name"],
            "description": cr.get("rationale", ""), "custom": True,
            "fired_count": len(fired),
            "claims": [f["claim_id"] for f in fired],
            "example_reason": fired[0]["reason"] if fired else None,
        })
    return {"total_claims": len(STATE.book), "rules": out}


@app.get("/api/graph")
def graph():
    nodes = [{**n, "band": STATE.band_by_id.get(n["id"], "low"),
              "score": STATE.score_by_id[n["id"]].score if n["id"] in STATE.score_by_id else 0}
             for n in STATE.graph["nodes"]]
    return {"nodes": nodes, "edges": STATE.graph["edges"], "rings": STATE.graph["rings"],
            "hubs": STATE.graph.get("hubs", [])}


@app.post("/api/score")
def score_preview(claim: Claim):
    """Preview-score a claim against the current book WITHOUT persisting it.
    Handy for the CLI demo and tests; the dashboard uses POST /api/claims."""
    book = STATE.book + [c for c in [claim] if c.id not in STATE.claim_by_id]
    if claim.id in STATE.claim_by_id:
        book = [c for c in STATE.book if c.id != claim.id] + [claim]
    results, _ = score_book(book, STATE.ctx, use_llm=False,
                            custom_rules=STATE.custom_rules)  # fast; note is lazy
    for r in results:
        if r.claim_id == claim.id:
            return r.model_dump()
    raise HTTPException(status_code=400, detail="scoring failed")


@app.post("/api/claims")
def add_claim(claim: Claim):
    """Add (or replace) a claim in the book, re-score everything, and return the
    new claim's result. It now persists across refreshes and can pull existing
    claims into its ring."""
    STATE.add(claim)
    r = STATE.score_by_id.get(claim.id)
    if r is None:
        raise HTTPException(status_code=400, detail="scoring failed")
    return r.model_dump()


@app.post("/api/reset")
def reset():
    """Reset the book to the seed dataset (drops any claims added at runtime)."""
    STATE.reset()
    return {"ok": True, "claims": len(STATE.book), "rings": len(STATE.graph["rings"])}


# --- AI edge layers (input: extract · output: referral / ring summary) -------
@app.post("/api/extract")
def extract(payload: dict):
    """INPUT EDGE — turn a free-text (or OCR'd) claim into structured fields the
    deterministic engine can score. Returns the claim for human review before it
    is submitted; the score is still produced by the rules, not the LLM."""
    text = (payload or {}).get("text", "")
    if not text.strip():
        raise HTTPException(status_code=422, detail="empty text")
    from .ai import extract_claim
    claim, err = extract_claim(text)
    return {"ok": err is None, "claim": claim, "error": err}


@app.get("/api/claims/{claim_id}/referral")
def referral(claim_id: str):
    """OUTPUT EDGE — draft an SIU referral memo for a claim (AI or template)."""
    r = STATE.score_by_id.get(claim_id)
    claim = STATE.claim_by_id.get(claim_id)
    if r is None or claim is None:
        raise HTTPException(status_code=404, detail="claim not found")
    from .ai import draft_referral
    return {"referral": draft_referral(claim.model_dump(mode="json"), r.model_dump()),
            "ai": llm.available()}


@app.post("/api/rules/discover")
def rules_discover():
    """OFFLINE RULE R&D — AI proposes new rules the org may be missing, each with
    a dry-run of which current claims it would flag. Nothing is adopted here."""
    from . import custom_rules
    from .rules import RULE_CATALOG
    names = [r["name"] for r in RULE_CATALOG] + [c["name"] for c in STATE.custom_rules]
    return {"ai": llm.available(),
            "proposals": custom_rules.propose(STATE.book, names)}


@app.get("/api/rules/custom")
def rules_custom_list():
    return {"rules": STATE.custom_rules}


@app.post("/api/rules/custom")
def rules_custom_add(rule: dict):
    """Human-in-the-loop ADOPT: validate a proposed rule and add it to the engine."""
    from . import custom_rules
    conds = custom_rules.valid_conditions(rule.get("conditions"))
    if not conds:
        raise HTTPException(status_code=422, detail="no valid conditions")
    existing = custom_rules.load()
    new = {
        "id": f"CR{len(existing) + 1}",
        "name": str(rule.get("name", "Custom rule"))[:80],
        "category": rule.get("category", "Custom (adopted)"),
        "points": max(1, min(25, int(rule.get("points", 10)))),
        "rationale": str(rule.get("rationale", ""))[:300],
        "conditions": conds, "enabled": True,
    }
    existing.append(new)
    custom_rules.save(existing)
    STATE.reload_custom()
    return {"ok": True, "adopted": new, "count": len(existing)}


@app.delete("/api/rules/custom/{rule_id}")
def rules_custom_delete(rule_id: str):
    from . import custom_rules
    existing = [r for r in custom_rules.load() if r["id"] != rule_id]
    custom_rules.save(existing)
    STATE.reload_custom()
    return {"ok": True, "count": len(existing)}


@app.get("/api/rings/{ring_id}/summary")
def ring_summary_ep(ring_id: str):
    """OUTPUT EDGE — plain-English summary of a fraud ring (AI or template)."""
    members = []
    for cid, rid in STATE.graph["ring_of"].items():
        if rid != ring_id:
            continue
        r = STATE.score_by_id.get(cid)
        claim = STATE.claim_by_id.get(cid)
        if r and claim:
            r12 = next((f.reason for f in r.fired if f.rule_id == "R12"), None)
            members.append({"claim_id": cid, "claimant": claim.party.name,
                            "score": r.score, "band": r.band, "amount": claim.amount,
                            "shared": [r12] if r12 else []})
    if not members:
        raise HTTPException(status_code=404, detail="ring not found")
    members.sort(key=lambda m: m["score"], reverse=True)
    from .ai import ring_summary
    return {"ring_id": ring_id, "members": [m["claim_id"] for m in members],
            "summary": ring_summary(ring_id, members), "ai": llm.available()}
