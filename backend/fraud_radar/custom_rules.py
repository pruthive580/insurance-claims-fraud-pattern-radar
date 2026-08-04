"""Human-in-the-loop custom rules (offline rule R&D).

The AI proposes candidate rules the organisation may be missing, but ONLY in a
constrained, safe DSL — never executable code. A human reviews and adopts them;
adopted rules are then evaluated *deterministically* by the same engine, so they
are exactly as auditable as R1–R16.

A rule:
  {id, name, category, points, rationale, enabled,
   conditions: [{field, op, value}]}   # ALL conditions must hold (AND)
"""

from __future__ import annotations

import json
from pathlib import Path

from . import llm
from .models import Claim, FiredRule

_FILE = Path(__file__).parent / "data" / "custom_rules.json"


# --- the safe vocabulary -----------------------------------------------------
def _report_delay_days(c: Claim) -> int:
    return (c.reported_date - c.loss_datetime.date()).days


def _days_since_inception(c: Claim) -> int:
    return (c.reported_date - c.policy.inception_date).days


# field name -> accessor(claim, cross_claim_ctx) -> value
FIELDS = {
    "amount": lambda c, x: c.amount,
    "claim_type": lambda c, x: c.claim_type,
    "repairer": lambda c, x: (c.repairer or ""),
    "police_report_present": lambda c, x: bool(c.police_report_present),
    "demands_fast_cash": lambda c, x: bool(c.demands_fast_cash),
    "refused_inspection": lambda c, x: bool(c.refused_inspection),
    "report_delay_days": lambda c, x: _report_delay_days(c),
    "days_since_inception": lambda c, x: _days_since_inception(c),
    "loss_hour": lambda c, x: c.loss_datetime.hour,
    "loss_is_weekend": lambda c, x: c.loss_datetime.weekday() >= 5,
    "has_attorney": lambda c, x: bool(c.party.attorney),
    "has_ip": lambda c, x: bool(c.party.ip_address),
    "ip_shared": lambda c, x: x["ip_shared"].get(c.id, False),
    "bank_shared": lambda c, x: x["bank_shared"].get(c.id, False),
    "claimant_claim_count": lambda c, x: x["claim_count"].get(c.party.claimant_id, 0),
}

OPS = {
    "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
    "is_true": lambda a, b: bool(a) is True,
    "is_false": lambda a, b: bool(a) is False,
}

VOCAB_DOC = (
    "FIELDS: amount(num), claim_type(str), repairer(str), police_report_present(bool), "
    "demands_fast_cash(bool), refused_inspection(bool), report_delay_days(num), "
    "days_since_inception(num), loss_hour(0-23), loss_is_weekend(bool), has_attorney(bool), "
    "has_ip(bool), ip_shared(bool: same IP across different claimants), "
    "bank_shared(bool: same bank across different claimants), claimant_claim_count(num). "
    "OPS: gt, ge, lt, le, eq, ne (all need a value); is_true, is_false (bool fields, no value)."
)


# --- cross-claim context -----------------------------------------------------
def book_ctx(book: list[Claim]) -> dict:
    ipmap: dict[str, set] = {}
    bankmap: dict[str, set] = {}
    count: dict[str, int] = {}
    for c in book:
        count[c.party.claimant_id] = count.get(c.party.claimant_id, 0) + 1
        if c.party.ip_address:
            ipmap.setdefault(c.party.ip_address.strip().lower(), set()).add(c.party.claimant_id)
        if c.party.bank_account:
            bankmap.setdefault(c.party.bank_account.strip().lower(), set()).add(c.party.claimant_id)
    ip_shared, bank_shared = {}, {}
    for c in book:
        v = (c.party.ip_address or "").strip().lower()
        ip_shared[c.id] = bool(v and len(ipmap.get(v, set())) >= 2)
        b = (c.party.bank_account or "").strip().lower()
        bank_shared[c.id] = bool(b and len(bankmap.get(b, set())) >= 2)
    return {"ip_shared": ip_shared, "bank_shared": bank_shared, "claim_count": count}


# --- evaluation --------------------------------------------------------------
def _cond_holds(c: Claim, cctx: dict, cond: dict):
    field, op, val = cond.get("field"), cond.get("op"), cond.get("value")
    if field not in FIELDS or op not in OPS:
        return False, ""
    a = FIELDS[field](c, cctx)
    try:
        if op in ("is_true", "is_false"):
            ok = OPS[op](a, None)
            return ok, f"{field} {op} (={a})"
        if isinstance(a, bool):
            ok = OPS[op](a, str(val).lower() in ("true", "1"))
        elif isinstance(a, (int, float)):
            ok = OPS[op](a, float(val))
        else:
            ok = OPS[op](str(a).lower(), str(val).lower())
    except Exception:
        return False, ""
    return ok, f"{field} {op} {val} (={a})"


def evaluate(claim: Claim, rule: dict, cctx: dict) -> FiredRule | None:
    if not rule.get("enabled", True):
        return None
    conds = rule.get("conditions") or []
    if not conds:
        return None
    parts = []
    for cond in conds:
        ok, desc = _cond_holds(claim, cctx, cond)
        if not ok:
            return None
        parts.append(desc)
    return FiredRule(rule_id=rule["id"], category=rule.get("category", "Custom (adopted)"),
                     points=int(rule.get("points", 10)),
                     reason=f"[custom] {rule.get('name', 'rule')}: " + "; ".join(parts))


def valid_conditions(conds) -> list:
    return [c for c in (conds or [])
            if isinstance(c, dict) and c.get("field") in FIELDS and c.get("op") in OPS]


# --- redundancy filter: don't re-suggest rules the engine already has --------
_COARSE = {"gt": "high", "ge": "high", "lt": "low", "le": "low",
           "eq": "eq", "ne": "eq", "is_true": "true", "is_false": "false"}


def _sig(conds) -> frozenset:
    """Reduce a rule to (field, direction) pairs so equivalent rules match."""
    return frozenset((c["field"], _COARSE.get(c["op"], "?"))
                     for c in conds if c.get("field"))


# Signals the built-in R1–R16 already cover. A proposal whose conditions are a
# subset of any of these is just re-expressing an existing rule -> drop it.
_BUILTIN_SIGS = [
    frozenset({("days_since_inception", "low")}),        # R1 just-in-time cover
    frozenset({("report_delay_days", "high")}),          # R4 late report
    frozenset({("claimant_claim_count", "high")}),       # R5 serial claimant
    frozenset({("loss_is_weekend", "true")}),            # R6 weak-witness timing
    frozenset({("loss_hour", "low")}),                   # R6 late-night
    frozenset({("police_report_present", "false")}),     # R11 no police report
    frozenset({("demands_fast_cash", "true")}),          # R10 (part)
    frozenset({("refused_inspection", "true")}),         # R10 (part)
    frozenset({("demands_fast_cash", "true"), ("refused_inspection", "true")}),  # R10
    frozenset({("ip_shared", "true")}),                  # R12 (graph indexes IP)
    frozenset({("bank_shared", "true")}),                # R12 (graph indexes bank)
]


def _is_redundant(sig: frozenset, existing_sigs: list) -> bool:
    if not sig:
        return True
    for b in _BUILTIN_SIGS:
        if sig <= b:                 # proposal fully covered by a built-in rule
            return True
    for e in existing_sigs:
        if sig <= e or e <= sig:     # duplicate / weaker-or-equal of an adopted rule
            return True
    return False


# --- persistence -------------------------------------------------------------
def load() -> list[dict]:
    if _FILE.exists():
        try:
            return json.loads(_FILE.read_text())
        except Exception:
            return []
    return []


def save(rules: list[dict]):
    _FILE.parent.mkdir(exist_ok=True)
    _FILE.write_text(json.dumps(rules, indent=2))


# --- AI proposer (with deterministic fallback) -------------------------------
def _fallback_proposals() -> list[dict]:
    return [
        {"name": "Represented + refused inspection", "category": "Behavioural", "points": 12,
         "rationale": "An attorney attached to a claim that also refused inspection is a stronger signal "
                      "than either alone.",
         "conditions": [{"field": "has_attorney", "op": "is_true"},
                        {"field": "refused_inspection", "op": "is_true"}]},
        {"name": "High-value property loss", "category": "Amount & anomaly", "points": 10,
         "rationale": "Very large water-damage claims deserve automatic scrutiny beyond the percentile rule.",
         "conditions": [{"field": "amount", "op": "gt", "value": 50000},
                        {"field": "claim_type", "op": "eq", "value": "water_damage"}]},
        {"name": "Represented repeat claimant", "category": "Behavioural", "points": 12,
         "rationale": "An attorney attached to a claimant with multiple claims correlates with organised activity.",
         "conditions": [{"field": "has_attorney", "op": "is_true"},
                        {"field": "claimant_claim_count", "op": "ge", "value": 2}]},
        {"name": "Late-night high-value loss", "category": "Timing & frequency", "points": 8,
         "rationale": "Weak-witness timing is more suspicious on high-value claims.",
         "conditions": [{"field": "loss_hour", "op": "lt", "value": 6},
                        {"field": "amount", "op": "gt", "value": 20000}]},
    ]


def _llm_propose(existing_names: list[str]) -> list[dict] | None:
    if not llm.available():
        return None
    sys = ("You are a fraud strategy analyst proposing NEW detection rules an insurer may be missing. "
           "Return ONLY JSON: a list of up to 4 rules, each {name, category, points (5-20), rationale, "
           "conditions:[{field, op, value}]}. Conditions are ANDed. Use ONLY this vocabulary:\n"
           + VOCAB_DOC +
           "\nSTRICT: boolean fields MUST use is_true/is_false with NO value. "
           "Numeric fields (amount, report_delay_days, days_since_inception, loss_hour, "
           "claimant_claim_count) use gt/ge/lt/le/eq with a number. "
           "String fields (claim_type, repairer) use eq/ne with a realistic value. "
           "\nThe engine ALREADY has rules for: filing soon after inception, late reporting, weekend/late-night "
           "timing, serial/repeat claimants, missing police report, fast-cash + refused-inspection, amount vs "
           "percentile/threshold, and shared phone/bank/IP/attorney/address/repairer. DO NOT propose any of these. "
           "Each rule MUST combine 2+ conditions into a genuinely NEW pattern (e.g. an attorney on a high-value "
           "claim, or a large loss of a specific claim_type).")
    prompt = ("These rules already exist — do NOT repeat them: " + ", ".join(existing_names) + ".\n"
              "Propose only new, non-overlapping multi-condition rules using the vocabulary above.")
    data = llm.complete_json(prompt, system=sys, max_tokens=700)
    return data if isinstance(data, list) else None


def propose(book: list[Claim], existing_names: list[str]) -> list[dict]:
    """Return AI-proposed (or fallback) candidate rules, each with a dry-run of
    which current claims it *would* flag — nothing is adopted here."""
    cctx = book_ctx(book)
    existing_sigs = [_sig(r.get("conditions", [])) for r in load()]  # already-adopted rules

    def _build(cands):
        seen, out = set(), []
        for c in cands:
            if not isinstance(c, dict):
                continue
            conds = valid_conditions(c.get("conditions"))
            if not conds:
                continue
            sig = _sig(conds)
            if sig in seen or _is_redundant(sig, existing_sigs):
                continue           # duplicate, or just re-expresses a rule the engine already has
            seen.add(sig)
            rule = {"id": f"CR-{len(out) + 1}", "name": str(c.get("name", "Untitled rule"))[:80],
                    "category": c.get("category", "Custom (adopted)"),
                    "points": max(1, min(25, int(c.get("points", 10)))),
                    "rationale": str(c.get("rationale", ""))[:300],
                    "conditions": conds, "enabled": True}
            rule["would_flag"] = [cl.id for cl in book if evaluate(cl, rule, cctx)]
            out.append(rule)
        return out

    out = _build(_llm_propose(existing_names) or [])
    if not out:                    # AI returned only duplicates (or is off) -> novel fallback ideas
        out = _build(_fallback_proposals())
    return out
