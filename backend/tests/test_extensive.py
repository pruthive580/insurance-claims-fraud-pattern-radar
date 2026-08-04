"""Extensive test suite for use case #12 — Fraud Pattern Radar.

Goes well beyond happy-path scope coverage:
  * Every rule R1–R16: positive fire, negative (must-not-fire), and boundary.
  * Cross-claim internals: claimant frequency window, shared-identifier logic,
    perceptual-hash duplicate detection, connected-component ring detection.
  * Banding boundaries (29/30/59/60), routing map, score additivity.
  * API robustness: 404 unknown claim, 422 invalid payload, endpoint shapes.
  * Full UC1–6 + MVP1–4 mapping, engine + live API.
  * False-positive / robustness: minimal claim, missing optional fields.

Run:  .venv/bin/python -m tests.test_extensive
Exits non-zero on any failure.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import date, datetime

from fraud_radar import rules as R
from fraud_radar.data import samples
from fraud_radar.graph import (_claimant_frequency, _duplicate_photos,
                               _hamming_hex, build as build_graph)
from fraud_radar.models import (Claim, DocField, Invoice, LineItem, Party,
                                Photo, Policy, RuleContext)
from fraud_radar.rules import CrossSignals, evaluate
from fraud_radar.scoring import ROUTING, _band, base_context, score_book

API = "http://127.0.0.1:8000"

# Points each rule is worth, per the sheet — asserted so weights can't drift.
POINTS = {"R1": 15, "R2": 10, "R3": 8, "R4": 8, "R5": 12, "R6": 5, "R7": 10,
          "R8": 12, "R9": 8, "R10": 10, "R11": 7, "R12": 15, "R13": 10,
          "R14": 10, "R15": 15, "R16": 8}

# ---- harness ----------------------------------------------------------------
_PASS, _FAIL = [], []


def check(gid: str, desc: str, cond: bool, detail: str = ""):
    (_PASS if cond else _FAIL).append(gid)
    print(f"[{'PASS' if cond else 'FAIL'}] {gid:10} {desc}" + (f"  -> {detail}" if detail else ""))


def http_raw(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def http(path, method="GET", body=None):
    _, b = http_raw(path, method, body)
    return b


# ---- fixtures ---------------------------------------------------------------
# A deliberately clean claim; individual tests override one thing at a time.
_UID = [0]


def mk(**over) -> Claim:
    _UID[0] += 1
    base = dict(
        id=f"T-{_UID[0]}", claim_type="windshield", segment="motor",
        party=Party(claimant_id=f"CLX-{_UID[0]}", name="Clean Claimant",
                    policyholder_address="1 A St, Karachi"),
        policy=Policy(number=f"PX-{_UID[0]}", inception_date=date(2020, 1, 1)),
        loss_datetime=datetime(2026, 6, 3, 12, 0),   # Wed noon — no timing flag
        reported_date=date(2026, 6, 3),              # 0-day delay
        amount=100.0,
        risk_address="1 A St, Karachi",              # same city as policyholder
        police_report_present=True,
    )
    # allow nested override of party fields via party=... ; else patch scalars
    base.update(over)
    return Claim(**base)


def CTX() -> RuleContext:
    return RuleContext(
        percentile90={"windshield": 1000.0, "auto_theft": 20000.0},
        auto_approval_threshold={"windshield": 5000.0, "auto_theft": 25000.0},
        flagged_repairers={"BadShop": "prior SIU flags; high loss ratio"},
        police_report_required={"auto_theft", "third_party_injury"},
    )


NOSIG = CrossSignals()


def fired_map(claim, ctx=None, sig=None):
    ctx = ctx or CTX()
    fired = evaluate(claim, ctx, sig or NOSIG)
    return {f.rule_id: f for f in fired}


# =============================================================================
# 0. Baseline — a clean claim must fire NOTHING
# =============================================================================
def t_baseline():
    fm = fired_map(mk())
    check("BASE", "Clean claim fires zero rules", fm == {}, f"fired={list(fm)}")


# =============================================================================
# 1. Per-rule: positive + points, negative, boundary
# =============================================================================
def t_r1():
    # positive: reported 30d after inception (inclusive edge)
    fm = fired_map(mk(policy=Policy(number="P", inception_date=date(2026, 6, 1)),
                      reported_date=date(2026, 7, 1)))
    check("R1+", "R1 fires within 30d of inception (edge=30)",
          "R1" in fm and fm["R1"].points == POINTS["R1"])
    # boundary negative: 31 days
    fm = fired_map(mk(policy=Policy(number="P", inception_date=date(2026, 6, 1)),
                      reported_date=date(2026, 7, 2)))
    check("R1-", "R1 silent at 31d after inception", "R1" not in fm)
    # coverage-increase clause: 15d edge fires, 16d does not
    fm = fired_map(mk(policy=Policy(number="P", inception_date=date(2020, 1, 1),
                                    coverage_increase_date=date(2026, 6, 1)),
                      reported_date=date(2026, 6, 16)))
    check("R1ci+", "R1 fires within 15d of coverage increase (edge=15)", "R1" in fm)
    fm = fired_map(mk(policy=Policy(number="P", inception_date=date(2020, 1, 1),
                                    coverage_increase_date=date(2026, 6, 1)),
                      reported_date=date(2026, 6, 17)))
    check("R1ci-", "R1 silent at 16d after coverage increase", "R1" not in fm)


def t_r2():
    fm = fired_map(mk(policy=Policy(number="P", inception_date=date(2020, 1, 1),
                                    lapsed=True, reinstatement_date=date(2026, 6, 1)),
                      loss_datetime=datetime(2026, 7, 1, 12, 0),
                      reported_date=date(2026, 7, 1)))
    check("R2+", "R2 fires when reinstated <=30d before loss (edge=30)",
          "R2" in fm and fm["R2"].points == POINTS["R2"])
    fm = fired_map(mk(policy=Policy(number="P", inception_date=date(2020, 1, 1),
                                    reinstatement_date=date(2026, 6, 1)),
                      loss_datetime=datetime(2026, 7, 2, 12, 0),
                      reported_date=date(2026, 7, 2)))
    check("R2-", "R2 silent at 31d after reinstatement", "R2" not in fm)


def t_r3():
    fm = fired_map(mk(party=Party(claimant_id="C", name="n",
                                  policyholder_address="9 X St, Lahore"),
                      risk_address="4 Y Rd, Karachi"))
    check("R3+", "R3 fires on city mismatch (policyholder vs risk)",
          "R3" in fm and fm["R3"].points == POINTS["R3"])
    fm = fired_map(mk(party=Party(claimant_id="C", name="n",
                                  policyholder_address="9 X St, Karachi"),
                      risk_address="4 Y Rd, Karachi"))
    check("R3-", "R3 silent when cities match", "R3" not in fm)


def t_r4():
    fm = fired_map(mk(loss_datetime=datetime(2026, 6, 1, 12, 0),
                      reported_date=date(2026, 6, 9)))            # 8d late, no reason
    check("R4+", "R4 fires when reported >7d late w/o reason",
          "R4" in fm and fm["R4"].points == POINTS["R4"])
    fm = fired_map(mk(loss_datetime=datetime(2026, 6, 1, 12, 0),
                      reported_date=date(2026, 6, 8)))            # exactly 7d
    check("R4-edge", "R4 silent at exactly 7d", "R4" not in fm)
    fm = fired_map(mk(loss_datetime=datetime(2026, 6, 1, 12, 0),
                      reported_date=date(2026, 6, 20),
                      report_delay_reason="hospitalised; documented"))
    check("R4-reason", "R4 silent when a plausible reason is given", "R4" not in fm)


def t_r5():
    fm = fired_map(mk(party=Party(claimant_id="CL-Z", name="Serial")),
                   sig=CrossSignals(claimant_claims_12mo={"CL-Z": 3}))
    check("R5+", "R5 fires at >=3 claims/12mo",
          "R5" in fm and fm["R5"].points == POINTS["R5"])
    fm = fired_map(mk(party=Party(claimant_id="CL-Z", name="Serial")),
                   sig=CrossSignals(claimant_claims_12mo={"CL-Z": 2}))
    check("R5-", "R5 silent at 2 claims/12mo", "R5" not in fm)


def t_r6():
    fm = fired_map(mk(loss_datetime=datetime(2026, 6, 6, 12, 0)))   # Saturday
    check("R6wknd", "R6 fires on weekend loss", "R6" in fm and fm["R6"].points == 5)
    fm = fired_map(mk(loss_datetime=datetime(2026, 6, 3, 2, 30)))   # Wed 02:30
    check("R6night", "R6 fires on late-night loss", "R6" in fm)
    fm = fired_map(mk(loss_datetime=datetime(2026, 1, 1, 12, 0)))   # holiday
    check("R6hol", "R6 fires on holiday loss", "R6" in fm)
    fm = fired_map(mk(loss_datetime=datetime(2026, 6, 3, 12, 0)))   # Wed noon
    check("R6-", "R6 silent on a normal weekday daytime loss", "R6" not in fm)


def t_r7():
    fm = fired_map(mk(claim_type="windshield", amount=1001))        # p90=1000
    check("R7+", "R7 fires above 90th percentile",
          "R7" in fm and fm["R7"].points == POINTS["R7"])
    fm = fired_map(mk(claim_type="windshield", amount=1000))        # at p90
    check("R7-", "R7 silent exactly at p90 (strictly greater)", "R7" not in fm)


def t_r8():
    fm = fired_map(mk(amount=4999))          # just under 5000
    check("R8+", "R8 fires just under threshold",
          "R8" in fm and fm["R8"].points == POINTS["R8"])
    fm = fired_map(mk(amount=4750))          # exactly 5% below
    check("R8edge", "R8 fires at exactly 5% below threshold", "R8" in fm)
    fm = fired_map(mk(amount=4749))          # >5% below
    check("R8-below", "R8 silent when >5% below threshold", "R8" not in fm)
    fm = fired_map(mk(amount=5000))          # at threshold
    check("R8-at", "R8 silent exactly at threshold", "R8" not in fm)


def t_r9():
    fm = fired_map(mk(invoices=[Invoice(number="INV-500", vendor="V", line_items=[
        LineItem(description="a", amount=1000), LineItem(description="b", amount=2000)])]))
    check("R9round", "R9 fires on all-round line items",
          "R9" in fm and fm["R9"].points == POINTS["R9"])
    fm = fired_map(mk(invoices=[
        Invoice(number="INV-1001", vendor="V", line_items=[LineItem(description="a", amount=1234.5)]),
        Invoice(number="INV-1002", vendor="V", line_items=[LineItem(description="b", amount=987.6)])]))
    check("R9seq", "R9 fires on consecutive invoice numbers (same vendor)", "R9" in fm)
    fm = fired_map(mk(invoices=[
        Invoice(number="INV-1001", vendor="V", line_items=[LineItem(description="a", amount=1234.5)]),
        Invoice(number="INV-1009", vendor="V", line_items=[LineItem(description="b", amount=987.6)])]))
    check("R9-", "R9 silent: non-round, non-consecutive invoices", "R9" not in fm)


def t_r10():
    fm = fired_map(mk(demands_fast_cash=True, refused_inspection=True))
    check("R10+", "R10 fires on fast-cash AND refused inspection",
          "R10" in fm and fm["R10"].points == POINTS["R10"])
    fm = fired_map(mk(demands_fast_cash=True, refused_inspection=False))
    check("R10-", "R10 silent when only one condition holds", "R10" not in fm)


def t_r11():
    fm = fired_map(mk(claim_type="auto_theft", police_report_present=False))
    check("R11+", "R11 fires: theft with no police report",
          "R11" in fm and fm["R11"].points == POINTS["R11"])
    fm = fired_map(mk(claim_type="auto_theft", police_report_present=True))
    check("R11-present", "R11 silent when police report present", "R11" not in fm)
    fm = fired_map(mk(claim_type="windshield", police_report_present=False))
    check("R11-nreq", "R11 silent for classes that don't require a report", "R11" not in fm)


def t_r12():
    c = mk()
    fm = fired_map(c, sig=CrossSignals(shared_identifiers={c.id: ["phone '555' (also on T-9)"]}))
    check("R12+", "R12 fires on shared identifiers across claims",
          "R12" in fm and fm["R12"].points == POINTS["R12"])
    check("R12-", "R12 silent with no shared identifiers", "R12" not in fired_map(mk()))


def t_r13():
    fm = fired_map(mk(repairer="BadShop"))
    check("R13+", "R13 fires on flagged repairer",
          "R13" in fm and fm["R13"].points == POINTS["R13"])
    check("R13-good", "R13 silent on non-flagged repairer", "R13" not in fired_map(mk(repairer="GoodShop")))
    check("R13-none", "R13 silent with no repairer", "R13" not in fired_map(mk()))


def t_r14():
    fm = fired_map(mk(documents=[
        DocField(field="loss_date", source="claim_form", value="2026-05-10"),
        DocField(field="loss_date", source="pdf_metadata", value="2026-05-18")]))
    check("R14+", "R14 fires when a field differs across documents",
          "R14" in fm and fm["R14"].points == POINTS["R14"])
    fm = fired_map(mk(documents=[
        DocField(field="loss_date", source="claim_form", value="2026-05-10"),
        DocField(field="loss_date", source="pdf_metadata", value="2026-05-10")]))
    check("R14-", "R14 silent when documents agree", "R14" not in fm)


def t_r15():
    c = mk()
    fm = fired_map(c, sig=CrossSignals(duplicate_photo={c.id: "claim C-101"}))
    check("R15+", "R15 fires on duplicate photo (pHash match)",
          "R15" in fm and fm["R15"].points == POINTS["R15"])
    check("R15-", "R15 silent with unique photos", "R15" not in fired_map(mk()))


def t_r16():
    fm = fired_map(mk(risk_address="4 Y Rd, Karachi",
                      photos=[Photo(id="p", exif_city="Dubai",
                                    exif_datetime=datetime(2026, 6, 3, 12, 0))]))
    check("R16city", "R16 fires when EXIF city != loss city",
          "R16" in fm and fm["R16"].points == POINTS["R16"])
    fm = fired_map(mk(risk_address="4 Y Rd, Karachi",
                      loss_datetime=datetime(2026, 6, 3, 12, 0),
                      photos=[Photo(id="p", exif_city="Karachi",
                                    exif_datetime=datetime(2026, 6, 20, 12, 0))]))  # >3d
    check("R16date", "R16 fires when EXIF date far from loss date", "R16" in fm)
    fm = fired_map(mk(risk_address="4 Y Rd, Karachi",
                      loss_datetime=datetime(2026, 6, 3, 12, 0),
                      photos=[Photo(id="p", exif_city="Karachi",
                                    exif_datetime=datetime(2026, 6, 3, 13, 0))]))
    check("R16-", "R16 silent when EXIF city+date consistent", "R16" not in fm)


def t_points_complete():
    """Every rule's emitted points match the sheet, gathered from all positive tests."""
    # Fire one claim that trips a bunch and verify each point value.
    ctx = CTX()
    sig = CrossSignals(claimant_claims_12mo={"CL-1": 4},
                       shared_identifiers={"MEGA": ["x"]},
                       duplicate_photo={"MEGA": "claim Y"})
    c = Claim(id="MEGA", claim_type="auto_theft", segment="motor",
              party=Party(claimant_id="CL-1", name="n", policyholder_address="a, Lahore"),
              policy=Policy(number="P", inception_date=date(2026, 6, 1),
                            reinstatement_date=date(2026, 6, 10)),
              loss_datetime=datetime(2026, 6, 20, 2, 0), reported_date=date(2026, 7, 1),
              amount=24000, risk_address="b, Karachi", repairer="BadShop",
              demands_fast_cash=True, refused_inspection=True, police_report_present=False,
              invoices=[Invoice(number="INV-1", vendor="V", line_items=[
                  LineItem(description="a", amount=1000), LineItem(description="b", amount=2000)])],
              photos=[Photo(id="p", exif_city="Dubai", exif_datetime=datetime(2026, 6, 20, 2, 0))],
              documents=[DocField(field="amount", source="form", value="24000"),
                         DocField(field="amount", source="pdf", value="26000")])
    fm = {f.rule_id: f.points for f in evaluate(c, ctx, sig)}
    ok = all(fm.get(r) == p for r, p in POINTS.items() if r in fm)
    check("POINTS", "Emitted points match sheet for every fired rule", ok,
          f"fired={sorted(fm, key=lambda x:int(x[1:]))}")


# =============================================================================
# 2. Cross-claim internals
# =============================================================================
def t_hamming():
    ok = (_hamming_hex("ff", "ff") == 0 and _hamming_hex("ff", "fe") == 1
          and _hamming_hex("f0", "0f") == 8
          and _hamming_hex("0000000000000000", "ffffffffffffffff") == 64
          and _hamming_hex("ff", "fff") == 999)   # length mismatch guard
    check("HAM", "Perceptual-hash Hamming distance is correct", ok)


def t_freq_window():
    book = [
        mk(party=Party(claimant_id="CL-A", name="a"), reported_date=date(2026, 7, 1)),
        mk(party=Party(claimant_id="CL-A", name="a"), reported_date=date(2026, 6, 1)),
        mk(party=Party(claimant_id="CL-A", name="a"), reported_date=date(2026, 5, 1)),
        mk(party=Party(claimant_id="CL-A", name="a"), reported_date=date(2024, 1, 1)),  # outside 12mo
        mk(party=Party(claimant_id="CL-B", name="b"), reported_date=date(2026, 8, 1)),  # sets 'today'
    ]
    freq = _claimant_frequency(book)
    check("FREQ", "12-month window counts only in-window claims (old excluded)",
          freq.get("CL-A") == 3, f"CL-A={freq.get('CL-A')}")


def t_dup_photos():
    book = [
        mk(reported_date=date(2026, 1, 1), photos=[Photo(id="e", phash="ffe1c0803f8f1f0f")]),
        mk(reported_date=date(2026, 2, 1), photos=[Photo(id="l", phash="ffe1c0803f8f1f0f")]),  # dup
        mk(reported_date=date(2026, 3, 1), photos=[Photo(id="u", phash="0000000000000000")]),  # unique
    ]
    dup = _duplicate_photos(book)
    later_id = book[1].id
    check("DUP+", "Duplicate photo detected on the later claim", later_id in dup)
    check("DUP-", "Non-matching photo not flagged as duplicate", book[2].id not in dup)


def t_shared_and_rings():
    book = [
        mk(party=Party(claimant_id="CL-1", name="a", phone="555-1")),
        mk(party=Party(claimant_id="CL-2", name="b", phone="555-1")),  # shares phone w/ CL-1
        mk(party=Party(claimant_id="CL-3", name="c", phone="555-9")),  # lonely
        mk(party=Party(claimant_id="CL-9", name="d", phone="555-7")),
        mk(party=Party(claimant_id="CL-9", name="d", phone="555-7")),  # SAME claimant, not a ring
    ]
    sig, g = build_graph(book)
    c1, c2, c3, c4, c5 = [c.id for c in book]
    check("SHARE+", "Shared identifier across distinct claimants is flagged",
          c1 in sig.shared_identifiers and c2 in sig.shared_identifiers)
    check("SHARE-lonely", "Unique identifier is not flagged", c3 not in sig.shared_identifiers)
    check("SHARE-self", "Same-claimant shared value is NOT treated as a ring",
          c4 not in sig.shared_identifiers and c5 not in sig.shared_identifiers)
    check("RING", "Ring = connected component of >=2 distinct-claimant claims",
          any(set(r) == {c1, c2} for r in g["rings"]), f"rings={g['rings']}")
    # edge dedup: exactly one phone edge between c1 and c2
    phone_edges = [e for e in g["edges"] if {e["source"], e["target"]} == {c1, c2}]
    check("EDGE-dedup", "Parallel edges on same pair/type are de-duplicated",
          len(phone_edges) == 1, f"n={len(phone_edges)}")


# =============================================================================
# 3. Banding, routing, additivity
# =============================================================================
def t_bands():
    cases = {0: "low", 29: "low", 30: "medium", 59: "medium", 60: "high", 61: "high", 200: "high"}
    ok = all(_band(s) == b for s, b in cases.items())
    check("BAND", "Bands: 0-29 low / 30-59 medium / >=60 high", ok,
          str({s: _band(s) for s in cases}))
    check("ROUTE", "Routing text matches sheet",
          "auto-adjudicate" in ROUTING["low"] and "review" in ROUTING["medium"].lower()
          and "SIU" in ROUTING["high"])


def t_additivity():
    book = samples.build_book()
    results, _ = score_book(book, samples.build_context(), use_llm=False)
    ok = all(sum(f.points for f in r.fired) == r.score for r in results)
    check("ADD", "Every claim: sum(points) == score (fully explainable)", ok)
    ok2 = all(r.action == ROUTING[r.band] for r in results)
    check("ADD-route", "Every claim's action matches its band", ok2)


def t_full_coverage():
    book = samples.build_book()
    results, graph = score_book(book, samples.build_context(), use_llm=False)
    fired = {f.rule_id for r in results for f in r.fired}
    missing = {f"R{i}" for i in range(1, 17)} - fired
    check("COVER", "All 16 rules fire somewhere in the demo book",
          not missing, f"missing={sorted(missing) or 'none'}")
    check("COVER-ring", "Demo book yields at least one ring", len(graph["rings"]) >= 1)


# =============================================================================
# 4. API robustness
# =============================================================================
def t_api():
    code, h = http_raw("/api/health")
    check("API-health", "GET /api/health -> 200 with status/claims",
          code == 200 and "status" in h and h["claims"] > 0)
    code, cl = http_raw("/api/claims")
    check("API-claims", "GET /api/claims -> 200, list with band+action+score",
          code == 200 and all({"band", "action", "score"} <= set(c) for c in cl))
    code, _ = http_raw("/api/claims/DOES-NOT-EXIST")
    check("API-404", "GET unknown claim -> 404", code == 404, f"code={code}")
    code, d = http_raw("/api/claims/C-101")
    add_ok = sum(f["points"] for f in d["score"]["fired"]) == d["score"]["score"]
    check("API-detail", "GET claim detail -> fired rules + additive score", code == 200 and add_ok)
    code, g = http_raw("/api/graph")
    check("API-graph", "GET /api/graph -> nodes+edges+rings",
          code == 200 and {"nodes", "edges", "rings"} <= set(g))
    # valid ad-hoc score
    code, s = http_raw("/api/score", "POST", {
        "id": "API-1", "claim_type": "auto_theft",
        "party": {"claimant_id": "CLQ", "name": "n"},
        "policy": {"number": "PQ", "inception_date": "2026-07-25"},
        "loss_datetime": "2026-07-26T02:00:00", "reported_date": "2026-07-28",
        "amount": 24500, "risk_address": "x, Karachi", "police_report_present": False})
    check("API-score+", "POST /api/score (valid) -> 200 scored", code == 200 and s["score"] > 0,
          f"score={s and s.get('score')}")
    # invalid payload -> 422
    code, _ = http_raw("/api/score", "POST", {"id": "bad"})
    check("API-422", "POST /api/score (invalid) -> 422 validation error", code == 422, f"code={code}")


# =============================================================================
# 5. Robustness / false positives
# =============================================================================
def t_robust():
    # Minimal claim (no invoices/photos/docs/optional identifiers) must not crash / must be clean.
    minimal = Claim(id="MIN", claim_type="windshield",
                    party=Party(claimant_id="CLM", name="m"),
                    policy=Policy(number="PM", inception_date=date(2020, 1, 1)),
                    loss_datetime=datetime(2026, 6, 3, 12, 0), reported_date=date(2026, 6, 3),
                    amount=100)
    results, _ = score_book([minimal], base_context([minimal]), use_llm=False)
    check("MIN", "Minimal claim scores without error and stays low",
          results[0].score == 0 and results[0].band == "low")
    # Clean controls in the demo book remain low.
    book = samples.build_book()
    res, _ = score_book(book, samples.build_context(), use_llm=False)
    by = {r.claim_id: r for r in res}
    check("FP", "Clean controls are not false-positives (C-301/C-302 = low/0)",
          by["C-301"].score == 0 and by["C-302"].score == 0)


# =============================================================================
# 6. Use-case & MVP mapping (engine + API)
# =============================================================================
def t_scope():
    book = samples.build_book()
    res, graph = score_book(book, samples.build_context(), use_llm=False)
    by = {r.claim_id: r for r in res}
    fired = {f.rule_id for r in res for f in r.fired}
    sig, _ = build_graph(book)

    check("UC1", "FNOL/intake scoring (POST /api/score returns banded score)",
          (http("/api/score", "POST", {
              "id": "UC1x", "claim_type": "windshield",
              "party": {"claimant_id": "c", "name": "n"},
              "policy": {"number": "p", "inception_date": "2026-07-20"},
              "loss_datetime": "2026-07-25T12:00:00", "reported_date": "2026-07-25",
              "amount": 4999}) or {}).get("band") in {"low", "medium", "high"})
    check("UC2", "Anomaly detection: amount(R7,R8) + timing(R1,R2,R4,R6) + frequency(R5)",
          {"R7", "R8", "R1", "R2", "R4", "R6", "R5"} <= fired)
    check("UC3", "Entity linking: repeat claimants + providers + addresses",
          any(v >= 3 for v in sig.claimant_claims_12mo.values()) and len(graph["edges"]) > 0)
    check("UC4", "SIU prioritization: sorted queue + high-band SIU routing",
          [r.score for r in res] == sorted((r.score for r in res), reverse=True)
          and any(r.band == "high" for r in res))
    check("UC5", "Explanation layer: reasons on every rule + narrative + additive",
          all(f.reason for r in res for f in r.fired)
          and all(r.narrative for r in res)
          and sum(f.points for f in by["C-101"].fired) == by["C-101"].score)
    check("UC6", "Fraud-ring detection via network analysis (>=3 linked, distinct claimants)",
          any(len(r) >= 3 for r in graph["rings"]))

    check("MVP1", "Risk score for incoming claims", all(isinstance(r.score, int) for r in res))
    check("MVP2", "Explainable why-flagged summary", by["C-101"].fired and by["C-101"].narrative)
    check("MVP3", "Duplicate/similar detection (R15)", "R15" in {f.rule_id for f in by["C-103"].fired})
    check("MVP4", "Investigator queue ranked by fraud probability",
          res[0].score == max(r.score for r in res) and res[0].band == "high")


def main():
    print("=" * 78)
    print("EXTENSIVE TESTS — Fraud Pattern Radar (#12): rules, internals, API, scope")
    print("=" * 78)
    groups = [
        ("Baseline", [t_baseline]),
        ("Rules R1-R16 (positive / negative / boundary)",
         [t_r1, t_r2, t_r3, t_r4, t_r5, t_r6, t_r7, t_r8, t_r9, t_r10,
          t_r11, t_r12, t_r13, t_r14, t_r15, t_r16, t_points_complete]),
        ("Cross-claim internals", [t_hamming, t_freq_window, t_dup_photos, t_shared_and_rings]),
        ("Banding / routing / additivity", [t_bands, t_additivity, t_full_coverage]),
        ("API robustness", [t_api]),
        ("Robustness / false positives", [t_robust]),
        ("Use-case & MVP mapping", [t_scope]),
    ]
    for title, fns in groups:
        print(f"\n--- {title} ---")
        for fn in fns:
            fn()

    print("\n" + "=" * 78)
    total = len(_PASS) + len(_FAIL)
    print(f"RESULT: {len(_PASS)}/{total} checks passed.")
    if _FAIL:
        print("FAILED:", ", ".join(_FAIL))
        return 1
    print("ALL EXTENSIVE CHECKS PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
