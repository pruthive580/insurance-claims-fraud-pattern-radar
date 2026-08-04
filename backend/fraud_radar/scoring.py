"""Orchestration: turn a book of claims into scored, explained, ring-aware
results.

Pipeline per run:
  1. Derive book-level context (90th-percentile amounts per claim type).
  2. Build cross-claim signals + the ring graph.
  3. Evaluate R1–R16 for every claim, sum points, assign a risk band.
  4. Attach a "why flagged" narrative (LLM if a key is present; otherwise a
     deterministic summary of the fired rules).
"""

from __future__ import annotations

from statistics import quantiles

from . import llm
from .graph import build as build_graph
from .models import Claim, ClaimScore, RuleContext
from .rules import evaluate

# Score -> band -> routing action, verbatim from the #12/#48 sheet:
#   Low     0 - 29   Straight-through / auto-adjudicate
#   Medium  30 - 59  Adjuster review with reason codes
#   High    > 60     Route to SIU queue (top of the investigator list)
# The sheet leaves 60 undefined (Medium tops at 59, High is ">60"); we escalate
# the boundary value 60 into High so no score falls through the gap.
ROUTING = {
    "low": "Straight-through / auto-adjudicate",
    "medium": "Adjuster review with reason codes",
    "high": "Route to SIU queue (top of investigator list)",
}


def _band(score: int) -> str:
    if score >= 60:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


def _percentile90(book: list[Claim]) -> dict[str, float]:
    by_type: dict[str, list[float]] = {}
    for c in book:
        by_type.setdefault(c.claim_type, []).append(c.amount)
    out: dict[str, float] = {}
    for ctype, amounts in by_type.items():
        if len(amounts) >= 2:
            # 10 cut points -> the 9th boundary is the 90th percentile.
            # 'inclusive' keeps the estimate within the data range (with tiny
            # samples 'exclusive' extrapolates past the max and R7 never fires).
            out[ctype] = quantiles(amounts, n=10, method="inclusive")[-1]
        else:
            out[ctype] = amounts[0]
    return out


def _deterministic_narrative(claim: Claim, fired, band: str) -> str:
    if not fired:
        return "No fraud rules fired; claim looks clean on the current signals."
    top = sorted(fired, key=lambda f: f.points, reverse=True)[:3]
    lead = f"Risk band {band.upper()} — {len(fired)} rule(s) fired. Key drivers: "
    return lead + " ".join(f"[{f.rule_id}] {f.reason}" for f in top)


def _llm_narrative(claim: Claim, fired, band: str, ring_ids) -> str | None:
    if not fired or not llm.available():
        return None
    bullet = "\n".join(f"- {f.rule_id} (+{f.points}): {f.reason}" for f in fired)
    ring = f"\nRing membership: {', '.join(ring_ids)}." if ring_ids else ""
    prompt = (
        f"Claim {claim.id} ({claim.claim_type}, ${claim.amount:,.0f}) triggered "
        f"these fraud rules:\n{bullet}{ring}\n\n"
        "Write a 2-3 sentence SIU investigator note explaining, in plain English, "
        "why this claim is suspicious and what to verify next. Be specific and "
        "reference the concrete signals. No preamble."
    )
    return llm.complete(prompt, max_tokens=220, temperature=0.3)


def llm_narrative(claim: Claim, fired, band: str, ring_ids) -> str | None:
    """Public wrapper so the API can generate an LLM note for ONE claim on demand
    (keeps bulk scoring fast; the model is only called when a claim is opened)."""
    return _llm_narrative(claim, fired, band, ring_ids)


def base_context(book: list[Claim],
                 auto_approval_threshold: dict[str, float] | None = None,
                 flagged_repairers: dict[str, str] | None = None,
                 police_report_required: set[str] | None = None) -> RuleContext:
    return RuleContext(
        percentile90=_percentile90(book),
        auto_approval_threshold=auto_approval_threshold or {},
        flagged_repairers=flagged_repairers or {},
        police_report_required=police_report_required or set(),
    )


def score_book(book: list[Claim], ctx: RuleContext | None = None,
               use_llm: bool = True,
               custom_rules: list[dict] | None = None) -> tuple[list[ClaimScore], dict]:
    if ctx is None:
        ctx = base_context(book)
    else:
        # Always (re)derive percentiles from the actual book.
        ctx = ctx.model_copy(update={"percentile90": _percentile90(book)})

    signals, graph = build_graph(book)
    ring_of = graph["ring_of"]

    # Human-adopted custom rules (offline rule R&D) — evaluated deterministically
    # alongside R1–R16 so their points count and appear in "why flagged".
    ccx = None
    if custom_rules:
        from .custom_rules import book_ctx as _cbook, evaluate as _ceval
        ccx = _cbook(book)

    results: list[ClaimScore] = []
    for claim in book:
        fired = evaluate(claim, ctx, signals)
        if custom_rules:
            for cr in custom_rules:
                fr = _ceval(claim, cr, ccx)
                if fr is not None:
                    fired.append(fr)
        score = sum(f.points for f in fired)
        band = _band(score)
        ring_ids = [ring_of[claim.id]] if claim.id in ring_of else []

        narrative = None
        if use_llm:
            narrative = _llm_narrative(claim, fired, band, ring_ids)
        if narrative is None:
            narrative = _deterministic_narrative(claim, fired, band)

        results.append(ClaimScore(
            claim_id=claim.id, score=score, band=band, action=ROUTING[band],
            fired=fired, ring_ids=ring_ids, narrative=narrative,
        ))

    results.sort(key=lambda r: r.score, reverse=True)
    return results, graph
