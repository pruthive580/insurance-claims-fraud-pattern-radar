"""The R1–R16 deterministic fraud rule engine.

Each rule is a small pure function: given a claim, the book-of-business context,
and a bundle of precomputed cross-claim signals, it returns a FiredRule (with
points + a claim-specific human-readable reason) or None.

Points and trigger logic follow the provided rule card verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .models import Claim, FiredRule, RuleContext


@dataclass
class CrossSignals:
    """Cross-claim facts the engine computes once over the whole book and hands
    to the rules that can't decide from a single claim in isolation."""

    # claimant_id -> number of claims by that claimant in the trailing 12 months
    claimant_claims_12mo: dict[str, int] = field(default_factory=dict)
    # claim_id -> list of "shared identifier" descriptions (R12)
    shared_identifiers: dict[str, list[str]] = field(default_factory=dict)
    # claim_id -> description of the prior claim whose photo it duplicates (R15)
    duplicate_photo: dict[str, str] = field(default_factory=dict)


# --- small helpers -----------------------------------------------------------

def _city(addr: str | None) -> str:
    """Best-effort city token from a free-text address (last comma segment)."""
    if not addr:
        return ""
    return addr.split(",")[-1].strip().lower()


def _loss_date(claim: Claim) -> date:
    return claim.loss_datetime.date()


def _is_round(amount: float, step: int = 100) -> bool:
    return amount > 0 and abs(amount) % step == 0


def _invoice_seq(number: str) -> int | None:
    m = re.search(r"(\d+)", number or "")
    return int(m.group(1)) if m else None


# --- individual rules --------------------------------------------------------
# Every rule: (claim, ctx, sig) -> FiredRule | None

def r1(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    days_in = (claim.reported_date - claim.policy.inception_date).days
    if 0 <= days_in <= 30:
        return FiredRule(rule_id="R1", category="Identity & policy", points=15,
                         reason=f"Claim filed {days_in} day(s) after policy inception "
                                f"({claim.policy.inception_date}).")
    ci = claim.policy.coverage_increase_date
    if ci is not None:
        d = (claim.reported_date - ci).days
        if 0 <= d <= 15:
            return FiredRule(rule_id="R1", category="Identity & policy", points=15,
                             reason=f"Claim filed {d} day(s) after a coverage increase ({ci}).")
    return None


def r2(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    ri = claim.policy.reinstatement_date
    if ri is not None:
        d = (_loss_date(claim) - ri).days
        if 0 <= d <= 30:
            return FiredRule(rule_id="R2", category="Identity & policy", points=10,
                             reason=f"Policy reinstated {d} day(s) before the loss "
                                    f"(reinstated {ri}, loss {_loss_date(claim)}).")
    return None


def r3(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    pa, ra = claim.party.policyholder_address, claim.risk_address
    if pa and ra and _city(pa) != _city(ra):
        return FiredRule(rule_id="R3", category="Identity & policy", points=8,
                         reason=f"Policyholder city ({_city(pa) or '?'}) differs from the "
                                f"risk/garaging city ({_city(ra) or '?'}).")
    return None


def r4(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    delay = (claim.reported_date - _loss_date(claim)).days
    if delay > 7 and not claim.report_delay_reason:
        return FiredRule(rule_id="R4", category="Timing & frequency", points=8,
                         reason=f"Loss reported {delay} days late with no stated reason "
                                f"(loss {_loss_date(claim)}, reported {claim.reported_date}).")
    return None


def r5(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    n = sig.claimant_claims_12mo.get(claim.party.claimant_id, 0)
    if n >= 3:
        return FiredRule(rule_id="R5", category="Timing & frequency", points=12,
                         reason=f"Claimant has {n} claims across policies in the last 12 months.")
    return None


_HOLIDAYS = {(1, 1), (12, 25), (12, 26), (7, 4), (12, 31)}


def r6(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    dt = claim.loss_datetime
    weekend = dt.weekday() >= 5
    late_night = dt.hour < 6 or dt.hour >= 23
    holiday = (dt.month, dt.day) in _HOLIDAYS
    if weekend or late_night or holiday:
        tags = [t for t, on in (("weekend", weekend), ("late-night", late_night),
                                ("holiday", holiday)) if on]
        return FiredRule(rule_id="R6", category="Timing & frequency", points=5,
                         reason=f"Loss timing is weak-witness ({', '.join(tags)} at "
                                f"{dt.strftime('%a %H:%M')}).")
    return None


def r7(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    p90 = ctx.percentile90.get(claim.claim_type)
    if p90 and claim.amount > p90:
        return FiredRule(rule_id="R7", category="Amount & anomaly", points=10,
                         reason=f"Amount ${claim.amount:,.0f} exceeds the 90th-percentile "
                                f"${p90:,.0f} for {claim.claim_type}.")
    return None


def r8(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    thr = ctx.auto_approval_threshold.get(claim.claim_type)
    if thr and thr * 0.95 <= claim.amount < thr:
        return FiredRule(rule_id="R8", category="Amount & anomaly", points=12,
                         reason=f"Amount ${claim.amount:,.0f} sits just under the "
                                f"${thr:,.0f} auto-approval/SIU threshold (threshold gaming).")
    return None


def r9(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    # (a) rounded line items across an invoice.
    for inv in claim.invoices:
        amts = [li.amount for li in inv.line_items]
        if len(amts) >= 2 and all(_is_round(a, 100) for a in amts):
            return FiredRule(rule_id="R9", category="Behavioural", points=8,
                             reason=f"Invoice {inv.number} ({inv.vendor}) has all-round line "
                                    f"items {', '.join(f'${a:,.0f}' for a in amts)}.")
    # (b) sequential invoice numbers from the same vendor.
    by_vendor: dict[str, list[int]] = {}
    for inv in claim.invoices:
        seq = _invoice_seq(inv.number)
        if seq is not None:
            by_vendor.setdefault(inv.vendor, []).append(seq)
    for vendor, seqs in by_vendor.items():
        seqs.sort()
        if len(seqs) >= 2 and any(b - a == 1 for a, b in zip(seqs, seqs[1:])):
            return FiredRule(rule_id="R9", category="Behavioural", points=8,
                             reason=f"Consecutive invoice numbers from {vendor} "
                                    f"({', '.join(map(str, seqs))}).")
    return None


def r10(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    if claim.demands_fast_cash and claim.refused_inspection:
        return FiredRule(rule_id="R10", category="Behavioural", points=10,
                         reason="Claimant pushed for a fast cash settlement and refused inspection.")
    return None


def r11(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    if claim.claim_type in ctx.police_report_required and not claim.police_report_present:
        return FiredRule(rule_id="R11", category="Behavioural", points=7,
                         reason=f"No police report supplied though one is expected for "
                                f"{claim.claim_type}.")
    return None


def r12(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    hits = sig.shared_identifiers.get(claim.id)
    if hits:
        return FiredRule(rule_id="R12", category="Network / ring", points=15,
                         reason="Shares identifiers with other unrelated claims: "
                                + "; ".join(hits) + ".")
    return None


def r13(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    if claim.repairer and claim.repairer in ctx.flagged_repairers:
        return FiredRule(rule_id="R13", category="Network / ring", points=10,
                         reason=f"Repairer '{claim.repairer}' is flagged: "
                                f"{ctx.flagged_repairers[claim.repairer]}.")
    return None


def r14(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    by_field: dict[str, dict[str, str]] = {}
    for d in claim.documents:
        by_field.setdefault(d.field, {})[d.source] = d.value
    for field_name, sources in by_field.items():
        distinct = set(sources.values())
        if len(distinct) > 1:
            detail = ", ".join(f"{s}={v}" for s, v in sources.items())
            return FiredRule(rule_id="R14", category="Document / text", points=10,
                             reason=f"'{field_name}' differs across documents ({detail}).")
    return None


def r15(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    dup = sig.duplicate_photo.get(claim.id)
    if dup:
        return FiredRule(rule_id="R15", category="Document / text", points=15,
                         reason=f"Photo perceptual-hash matches {dup} (re-used damage image).")
    return None


def r16(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> FiredRule | None:
    loss_city = _city(claim.risk_address)
    loss_d = _loss_date(claim)
    for ph in claim.photos:
        if ph.exif_city and loss_city and ph.exif_city.strip().lower() != loss_city:
            return FiredRule(rule_id="R16", category="Document / text", points=8,
                             reason=f"Photo {ph.id} EXIF GPS is in {ph.exif_city}, not the "
                                    f"loss city {loss_city}.")
        if ph.exif_datetime and abs((ph.exif_datetime.date() - loss_d).days) > 3:
            return FiredRule(rule_id="R16", category="Document / text", points=8,
                             reason=f"Photo {ph.id} EXIF date {ph.exif_datetime.date()} is far "
                                    f"from the loss date {loss_d}.")
    return None


ALL_RULES = [r1, r2, r3, r4, r5, r6, r7, r8, r9, r10, r11, r12, r13, r14, r15, r16]

# Static catalog for the dashboard "rules" showcase. Points/categories mirror the
# FiredRule each rule emits above (kept in sync intentionally).
RULE_CATALOG = [
    {"rule_id": "R1",  "category": "Identity & policy",   "points": 15, "name": "Just-in-time cover",
     "description": "Claim filed within 30 days of policy inception (or 15 days of a coverage increase)."},
    {"rule_id": "R2",  "category": "Identity & policy",   "points": 10, "name": "Reinstated pre-loss",
     "description": "Policy reinstated within 30 days before the loss occurred."},
    {"rule_id": "R3",  "category": "Identity & policy",   "points": 8,  "name": "Address mismatch",
     "description": "Policyholder city differs from the risk / garaging city."},
    {"rule_id": "R4",  "category": "Timing & frequency",  "points": 8,  "name": "Late report",
     "description": "Loss reported more than 7 days late with no stated reason."},
    {"rule_id": "R5",  "category": "Timing & frequency",  "points": 12, "name": "Serial claimant",
     "description": "Claimant has 3+ claims across policies in the last 12 months."},
    {"rule_id": "R6",  "category": "Timing & frequency",  "points": 5,  "name": "Weak-witness timing",
     "description": "Loss timed for weak witnesses: weekend, late-night, or holiday."},
    {"rule_id": "R7",  "category": "Amount & anomaly",    "points": 10, "name": "Above 90th percentile",
     "description": "Claimed amount exceeds the 90th-percentile for that claim type."},
    {"rule_id": "R8",  "category": "Amount & anomaly",    "points": 12, "name": "Threshold gaming",
     "description": "Amount sits within 5% just under the auto-approval / SIU threshold."},
    {"rule_id": "R9",  "category": "Behavioural",         "points": 8,  "name": "Fabricated invoices",
     "description": "All-round line items, or sequential invoice numbers from one vendor."},
    {"rule_id": "R10", "category": "Behavioural",         "points": 10, "name": "Fast cash, no inspection",
     "description": "Claimant pushed for fast cash settlement and refused inspection."},
    {"rule_id": "R11", "category": "Behavioural",         "points": 7,  "name": "No police report",
     "description": "No police report where one is expected for the claim type."},
    {"rule_id": "R12", "category": "Network / ring",      "points": 15, "name": "Shared-identifier ring",
     "description": "Shares phone / bank / address / repairer / attorney with unrelated claims."},
    {"rule_id": "R13", "category": "Network / ring",      "points": 10, "name": "Flagged repairer",
     "description": "Repairer has prior SIU flags or an abnormal loss ratio."},
    {"rule_id": "R14", "category": "Document / text",     "points": 10, "name": "Doc inconsistency",
     "description": "A field's value differs across the claim's documents."},
    {"rule_id": "R15", "category": "Document / text",     "points": 15, "name": "Duplicate photo",
     "description": "Photo perceptual-hash matches an earlier claim's damage image."},
    {"rule_id": "R16", "category": "Document / text",     "points": 8,  "name": "EXIF mismatch",
     "description": "Photo GPS city or capture date is far from the loss."},
]


def evaluate(claim: Claim, ctx: RuleContext, sig: CrossSignals) -> list[FiredRule]:
    """Run every rule against a claim; return the ones that fired."""
    fired: list[FiredRule] = []
    for rule in ALL_RULES:
        result = rule(claim, ctx, sig)
        if result is not None:
            fired.append(result)
    return fired
