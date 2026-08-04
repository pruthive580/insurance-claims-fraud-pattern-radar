"""Optional AI 'edge' layers around the deterministic core.

The score/band/rules/rings are NEVER produced here — those stay deterministic.
These functions only:
  * input edge  — turn unstructured text into the structured fields the engine needs
  * output edge — turn structured results into human-readable prose

Everything degrades gracefully when no LLM is available.
"""

from __future__ import annotations

import json
from datetime import date

from . import llm
from .models import Claim

# --- input edge: unstructured text -> structured Claim -----------------------
_EXTRACT_SYS = (
    "You extract a structured insurance claim from free text for a fraud engine. "
    "Output ONLY JSON with these keys: id, claim_type, party{claimant_id,name,phone,"
    "bank_account,attorney,policyholder_address}, policy{number,inception_date,"
    "coverage_increase_date,reinstatement_date}, loss_datetime, reported_date, amount, "
    "risk_address, demands_fast_cash, refused_inspection, police_report_present, "
    "repairer, invoices[], photos[], documents[]. "
    "Dates are ISO (YYYY-MM-DD; loss_datetime YYYY-MM-DDTHH:MM:SS). Resolve relative "
    "dates against the given today. claim_type is snake_case (e.g. auto_theft, "
    "water_damage, windshield, burglary, third_party_injury). If a detail isn't stated, "
    "use null (false for the booleans, [] for the lists). "
    "policyholder_address and risk_address are plain strings like '12 Elm St, Lahore', "
    "NOT objects. Do not invent identifiers."
)


def _addr(v):
    """Addresses sometimes come back as an object — flatten to a string."""
    if isinstance(v, dict):
        return ", ".join(str(x) for x in v.values() if x) or None
    return v


def _num(v):
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        import re
        cleaned = re.sub(r"[^0-9.]", "", v)
        return float(cleaned) if cleaned else 0
    return 0


def _coerce(d: dict, today: date) -> dict:
    """Fill required fields the model may have left null, and normalise loose types
    so extraction never hard-fails Pydantic validation."""
    d["id"] = d.get("id") or "AI-CLAIM"
    d["claim_type"] = d.get("claim_type") or "unknown"
    party = d.get("party") or {}
    party["claimant_id"] = party.get("claimant_id") or "AI-CL"
    party["name"] = party.get("name") or "Unknown"
    party["policyholder_address"] = _addr(party.get("policyholder_address"))
    d["party"] = party
    policy = d.get("policy") or {}
    policy["number"] = policy.get("number") or "AI-P"
    policy["inception_date"] = policy.get("inception_date") or today.isoformat()
    d["policy"] = policy
    d["loss_datetime"] = d.get("loss_datetime") or f"{today.isoformat()}T12:00:00"
    d["reported_date"] = d.get("reported_date") or today.isoformat()
    d["risk_address"] = _addr(d.get("risk_address"))
    d["amount"] = _num(d.get("amount"))
    for b in ("demands_fast_cash", "refused_inspection", "police_report_present"):
        d[b] = bool(d.get(b))
    for lst in ("invoices", "photos", "documents"):
        if not isinstance(d.get(lst), list):
            d[lst] = []
    return d


def extract_claim(text: str, today: date | None = None) -> tuple[dict | None, str | None]:
    """Free text -> (structured claim dict ready to score, error). Returns
    (None, error) if the LLM is off or the text can't be structured."""
    if not llm.available():
        return None, "AI mode is off — enable Ollama/Claude to auto-extract, or enter JSON manually."
    today = today or date.today()
    data = llm.complete_json(
        f"Today is {today.isoformat()}.\nFree-text claim report:\n\"\"\"\n{text}\n\"\"\"",
        system=_EXTRACT_SYS, max_tokens=900)
    if not isinstance(data, dict):
        return None, "Could not extract a structured claim from that text — try adding detail."
    try:
        claim = Claim(**_coerce(data, today))
    except Exception as e:
        return None, f"Extracted fields were invalid: {e}"
    return json.loads(claim.model_dump_json()), None


# --- output edge: ring summary ----------------------------------------------
def _deterministic_ring_summary(ring_id: str, members: list[dict]) -> str:
    ids = ", ".join(m["claim_id"] for m in members)
    total = sum(m.get("amount", 0) for m in members)
    return (f"{ring_id}: {len(members)} claims ({ids}) from different claimants, linked "
            f"by shared identifiers. Combined exposure ${total:,.0f}. All routed for review.")


def ring_summary(ring_id: str, members: list[dict]) -> str:
    """members: [{claim_id, claimant, score, band, amount, shared:[...]}]"""
    if not llm.available():
        return _deterministic_ring_summary(ring_id, members)
    lines = "\n".join(
        f"- {m['claim_id']} ({m['claimant']}, ${m.get('amount',0):,.0f}, "
        f"score {m['score']}/{m['band']}): {'; '.join(m.get('shared', []) or [])}"
        for m in members)
    prompt = (f"Fraud ring {ring_id} groups these separate insurance claims that share "
              f"infrastructure across different claimants:\n{lines}\n\n"
              "Write a 3-4 sentence SIU summary: what ties the ring together, the combined "
              "exposure, and the recommended action. Be specific; no preamble.")
    return llm.complete(prompt, max_tokens=280, temperature=0.3) \
        or _deterministic_ring_summary(ring_id, members)


# --- output edge: draft SIU referral ----------------------------------------
def _deterministic_referral(claim: dict, score: dict) -> str:
    flags = "\n".join(f"  - {f['rule_id']} (+{f['points']}): {f['reason']}"
                      for f in score.get("fired", []))
    ring = f" Ring: {', '.join(score.get('ring_ids', []))}." if score.get("ring_ids") else ""
    return (f"SIU REFERRAL — Claim {claim['id']}\n"
            f"Risk score {score['score']} ({score['band'].upper()}) → {score.get('action','')}.{ring}\n"
            f"Triggered indicators:\n{flags}\n"
            f"Recommendation: investigate the flagged indicators above and verify supporting "
            f"documents before settlement.")


def draft_referral(claim: dict, score: dict) -> str:
    if not llm.available():
        return _deterministic_referral(claim, score)
    flags = "\n".join(f"- {f['rule_id']} (+{f['points']}): {f['reason']}"
                      for f in score.get("fired", []))
    ring = f"\nRing membership: {', '.join(score.get('ring_ids', []))}." if score.get("ring_ids") else ""
    prompt = (f"Draft a concise SIU (Special Investigations Unit) referral memo for claim "
              f"{claim['id']} ({claim.get('claim_type')}, ${claim.get('amount',0):,.0f}). "
              f"Risk score {score['score']} ({score['band']}).{ring}\n"
              f"Triggered fraud indicators:\n{flags}\n\n"
              "Include: a one-line summary, the key concerns in plain English, and 3 specific "
              "next steps to verify. Keep it under 150 words. No preamble.")
    return llm.complete(prompt, max_tokens=320, temperature=0.3) \
        or _deterministic_referral(claim, score)
