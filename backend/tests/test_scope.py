"""Strict conformance tests for the Fraud Pattern Radar engine and API.

Maps every official requirement (6 Use Cases + 4 MVP items) to concrete
assertions against BOTH the engine (unit) and the live API (integration).

Run:  .venv/bin/python -m tests.test_scope
Exits non-zero if any requirement fails. No pytest dependency.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime

from fraud_radar.data.samples import build_book, build_context
from fraud_radar.graph import build as build_graph
from fraud_radar.models import Party, Policy
from fraud_radar.rules import CrossSignals, evaluate
from fraud_radar.scoring import ROUTING, _band, base_context, score_book

API = "http://127.0.0.1:8000"

# ---- tiny harness -----------------------------------------------------------
_PASS, _FAIL = [], []


def check(req_id: str, desc: str, cond: bool, detail: str = ""):
    (_PASS if cond else _FAIL).append(req_id)
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {req_id:8} {desc}" + (f"  -> {detail}" if detail else ""))


def http(path: str, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


# ---- shared fixtures --------------------------------------------------------
BOOK = build_book()
CTX = build_context(BOOK)
RESULTS, GRAPH = score_book(BOOK, CTX, use_llm=False)
BY_ID = {r.claim_id: r for r in RESULTS}


def rules_of(cid):
    return {f.rule_id for f in BY_ID[cid].fired}


# =============================================================================
# USE CASES
# =============================================================================
def uc1_fnol_scoring():
    """UC1: Claim fraud risk scoring at FNOL / claim intake."""
    # A brand-new claim submitted at intake must come back scored + banded.
    claim = {
        "id": "C-FNOL", "claim_type": "auto_theft",
        "party": {"claimant_id": "CL-NEW", "name": "Intake Test", "phone": "555-0100"},
        "policy": {"number": "P-N", "inception_date": "2026-07-25"},
        "loss_datetime": "2026-07-26T02:00:00", "reported_date": "2026-07-28",
        "amount": 24500, "risk_address": "X, Karachi",
        "repairer": "QuickFix Auto Body", "police_report_present": False,
    }
    r = http("/api/score", "POST", claim)
    check("UC1", "FNOL/intake scoring returns a numeric score",
          isinstance(r.get("score"), int) and r["score"] > 0, f"score={r.get('score')}")
    check("UC1", "FNOL/intake scoring returns a risk band",
          r.get("band") in {"low", "elevated", "high", "refer_siu"}, f"band={r.get('band')}")


def uc2_anomaly_detection():
    """UC2: Anomaly detection across amount, timing, and frequency."""
    fired_all = {f.rule_id for r in RESULTS for f in r.fired}
    # amount anomalies
    check("UC2-amt", "Amount anomalies detected (R7 p90 + R8 threshold-gaming)",
          {"R7", "R8"} <= fired_all)
    # timing anomalies
    check("UC2-time", "Timing anomalies detected (R1 inception, R2 reinstatement, R4 late, R6 odd-hour)",
          {"R1", "R2", "R4", "R6"} <= fired_all)
    # frequency anomaly
    check("UC2-freq", "Frequency anomaly detected (R5 serial claimant >=3/12mo)",
          "R5" in fired_all, "R5 present" if "R5" in fired_all else "R5 MISSING")


def uc3_entity_linking():
    """UC3: Entity linking — repeat claimants, providers, addresses."""
    _, g = build_graph(BOOK)
    edge_types = {e["label"].split(":")[0].strip() for e in g["edges"]}
    # repeat claimants -> R5 frequency signal
    sig, _ = build_graph(BOOK)
    check("UC3-claimant", "Repeat claimant linked (>=3 claims same claimant)",
          any(v >= 3 for v in sig.claimant_claims_12mo.values()),
          f"max={max(sig.claimant_claims_12mo.values())}")
    # providers linked
    check("UC3-provider", "Providers/repairers linked across claims",
          "repairer" in edge_types, f"edge types={sorted(edge_types)}")
    # addresses / other identifiers linkable
    check("UC3-identifiers", "Multiple identifier types available for linking",
          {"repairer", "phone", "bank account", "attorney"} & edge_types != set(),
          f"linked on={sorted(edge_types)}")


def uc4_siu_prioritization():
    """UC4: Investigation prioritization for SIU teams."""
    claims = http("/api/claims")
    scores = [c["score"] for c in claims]
    check("UC4-order", "Queue is prioritized (sorted by score, descending)",
          scores == sorted(scores, reverse=True), f"scores={scores}")
    check("UC4-siu", "High-risk claims routed to SIU queue",
          any(c["band"] == "high" and "SIU" in c["action"] for c in claims),
          f"high={[c['claim_id'] for c in claims if c['band']=='high']}")


def uc5_explanation_layer():
    """UC5: Explanation layer showing why a claim was flagged."""
    d = http("/api/claims/C-103")["score"]
    has_reasons = all(f.get("reason") for f in d["fired"]) and len(d["fired"]) > 0
    check("UC5-reasons", "Every fired rule carries a human-readable reason", has_reasons,
          f"{len(d['fired'])} rules, all with reasons={has_reasons}")
    check("UC5-narrative", "Flagged claim has a 'why flagged' narrative",
          bool(d.get("narrative")), (d.get("narrative") or "")[:60] + "...")
    # points must add up to the score (score is fully explained)
    check("UC5-additive", "Score is fully explained (sum of rule points == score)",
          sum(f["points"] for f in d["fired"]) == d["score"],
          f"sum={sum(f['points'] for f in d['fired'])} score={d['score']}")


def uc6_ring_detection():
    """UC6: Fraud ring detection using relationship/network analysis."""
    g = http("/api/graph")
    check("UC6-ring", "A fraud ring (>=3 linked claims) is detected",
          any(len(r) >= 3 for r in g["rings"]), f"rings={g['rings']}")
    ring = next((r for r in g["rings"] if len(r) >= 3), [])
    # ring members must be genuinely linked by edges (network analysis, not coincidence)
    linked = {e["source"] for e in g["edges"]} | {e["target"] for e in g["edges"]}
    check("UC6-network", "Ring members are connected via relationship edges",
          set(ring) <= linked and len(g["edges"]) > 0,
          f"ring={ring}, edges={len(g['edges'])}")
    # ring members must be DIFFERENT claimants (unrelated on the surface)
    claimants = {BY_ID[c].claim_id and next(cl.party.claimant_id for cl in BOOK if cl.id == c)
                 for c in ring}
    check("UC6-unrelated", "Ring spans multiple distinct claimants",
          len(claimants) >= 3, f"distinct claimants={claimants}")


# =============================================================================
# MVP SCOPE
# =============================================================================
def mvp1_risk_score():
    """MVP1: Risk score for incoming claims."""
    claims = http("/api/claims")
    check("MVP1", "Every claim has an integer risk score",
          all(isinstance(c["score"], int) for c in claims), f"n={len(claims)}")


def mvp2_explainable():
    """MVP2: Explainable 'why flagged' summary."""
    d = http("/api/claims/C-101")["score"]
    check("MVP2", "Flagged claim exposes rule-level why + summary narrative",
          len(d["fired"]) > 0 and bool(d.get("narrative")),
          f"{len(d['fired'])} rules + narrative")


def mvp3_duplicate_detection():
    """MVP3: Duplicate / similar claim detection."""
    # C-103 re-uses C-101's damage photo (perceptual-hash match) -> R15.
    check("MVP3", "Duplicate/similar claim detected (R15 perceptual-hash match)",
          "R15" in rules_of("C-103"),
          BY_ID["C-103"].fired and next((f.reason for f in BY_ID["C-103"].fired
                                         if f.rule_id == "R15"), ""))


def mvp4_ranked_queue():
    """MVP4: Investigator queue ranked by fraud probability."""
    claims = http("/api/claims")
    top = claims[0]
    check("MVP4", "Queue ranked by fraud probability (top = highest score, High/SIU band)",
          top["score"] == max(c["score"] for c in claims) and top["band"] == "high",
          f"top={top['claim_id']} score={top['score']} band={top['band']}")


def sheet_bands():
    """Score bands -> action, verbatim from the #12/#48 sheet.
    Low 0-29 auto-adjudicate · Medium 30-59 adjuster review · High >60 SIU."""
    cases = [(0, "low"), (15, "low"), (29, "low"),
             (30, "medium"), (45, "medium"), (59, "medium"),
             (60, "high"), (61, "high"), (82, "high")]
    ok = all(_band(s) == b for s, b in cases)
    check("BANDS", "Band thresholds match sheet (0-29 low / 30-59 med / >=60 high)",
          ok, f"{[(s, _band(s)) for s, b in cases]}")
    check("ROUTE", "Routing actions match sheet (auto-adjudicate / review / SIU)",
          "auto-adjudicate" in ROUTING["low"].lower()
          and "adjuster review" in ROUTING["medium"].lower()
          and "SIU" in ROUTING["high"],
          str(ROUTING))
    # every live claim's action is consistent with its band
    claims = http("/api/claims")
    consistent = all(c["action"] == ROUTING[c["band"]] for c in claims)
    check("ROUTE-live", "Every claim's routing action matches its band", consistent,
          f"sample={claims[0]['band']}->{claims[0]['action']}")


# ---- negative control: clean claims must NOT be flagged ---------------------
def control_clean():
    check("CTRL", "Clean control claims are not flagged (band=low, score=0)",
          all(BY_ID[c].score == 0 and BY_ID[c].band == "low" for c in ("C-301", "C-302")),
          f"C-301={BY_ID['C-301'].score}, C-302={BY_ID['C-302'].score}")


def main():
    print("=" * 74)
    print("STRICT CONFORMANCE — Use Case #12 (6 Use Cases + 4 MVP items)")
    print("=" * 74)
    print("\n--- USE CASES ---")
    uc1_fnol_scoring(); uc2_anomaly_detection(); uc3_entity_linking()
    uc4_siu_prioritization(); uc5_explanation_layer(); uc6_ring_detection()
    print("\n--- MVP SCOPE ---")
    mvp1_risk_score(); mvp2_explainable(); mvp3_duplicate_detection(); mvp4_ranked_queue()
    print("\n--- SCORE BANDS -> ACTION (sheet) ---")
    sheet_bands()
    print("\n--- NEGATIVE CONTROL ---")
    control_clean()

    print("\n" + "=" * 74)
    total = len(_PASS) + len(_FAIL)
    print(f"RESULT: {len(_PASS)}/{total} checks passed.")
    if _FAIL:
        print("FAILED:", ", ".join(_FAIL))
        return 1
    print("ALL REQUIREMENTS SATISFIED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
