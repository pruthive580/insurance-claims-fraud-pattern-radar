"""A hand-built synthetic book of claims for the demo.

Engineered so the engine surfaces recognisable stories:
  * RING-1  — claims C-101/C-102/C-103, three *different* claimants bound by a
    flagged repairer ("QuickFix Auto Body") plus a shared phone, bank and
    attorney. Every ring rule (R12/R13) and a spread of others fire.
  * Serial claimant — "Dan" files three near-threshold windshield claims inside
    12 months (R5 + R8 threshold gaming).
  * Clean controls — low/zero-score claims so the banding is believable.

Every rule R1–R16 fires on at least one claim here.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from ..models import Claim, DocField, Invoice, LineItem, Party, Photo, Policy, RuleContext

_DATA_DIR = Path(__file__).parent
CLAIMS_PATH = _DATA_DIR / "claims.json"     # the persisted dataset the app reads
CONFIG_PATH = _DATA_DIR / "config.json"     # thresholds / flagged repairers / etc.

# Shared by scoring.base_context — the parts of context that aren't derived from
# the book itself (percentiles are computed live).
CONFIG = dict(
    auto_approval_threshold={"windshield": 5000.0, "water_damage": 5000.0,
                             "auto_theft": 25000.0},
    flagged_repairers={
        "QuickFix Auto Body": "3 prior SIU referrals; loss ratio 2.1x peer shops",
    },
    police_report_required={"auto_theft", "burglary", "third_party_injury"},
)

_DUP_HASH = "ffe1c0803f8f1f0f"  # identical phash on C-101 & C-103 (re-used photo)


def _seed_book() -> list[Claim]:
    """The canonical hand-built book. Used to (re)generate the dataset file."""
    return [
        # ---- RING-1 ---------------------------------------------------------
        Claim(
            id="C-101", claim_type="auto_theft", segment="motor",
            party=Party(claimant_id="CL-1", name="Alice Nardo", phone="555-0100",
                        attorney="Shady & Co", policyholder_address="12 Elm St, Lahore"),
            policy=Policy(number="P-1", inception_date=date(2026, 7, 20)),
            loss_datetime=datetime(2026, 7, 25, 2, 30),   # Sat, late-night -> R6
            reported_date=date(2026, 7, 28),              # 8d after inception -> R1
            amount=24000,                                 # just under 25k -> R8
            risk_address="Plot 9, Karachi",               # city != policyholder -> R3
            repairer="QuickFix Auto Body",                # R12 + R13
            police_report_present=False,                  # theft w/o report -> R11
            photos=[Photo(id="P-101a", phash=_DUP_HASH,
                          exif_datetime=datetime(2026, 7, 25, 3, 0), exif_city="Karachi")],
        ),
        Claim(
            id="C-102", claim_type="water_damage", segment="property",
            party=Party(claimant_id="CL-2", name="Bruno Katz", phone="555-0100",
                        bank_account="ACC-999", attorney="Shady & Co",
                        policyholder_address="88 Oak Ave, Karachi"),
            policy=Policy(number="P-2", inception_date=date(2025, 1, 5)),
            loss_datetime=datetime(2026, 6, 1, 14, 0),
            reported_date=date(2026, 6, 20),              # 19d late, no reason -> R4
            amount=60000,                                 # above p90 -> R7
            risk_address="88 Oak Ave, Karachi",
            repairer="QuickFix Auto Body",                # R12 + R13
            police_report_present=True,
            invoices=[
                Invoice(number="INV-1001", vendor="AquaDry Restoration", issued_date=date(2026, 6, 10),
                        line_items=[LineItem(description="dry-out", amount=30000)]),
                Invoice(number="INV-1002", vendor="AquaDry Restoration", issued_date=date(2026, 6, 10),
                        line_items=[LineItem(description="repairs", amount=30000)]),  # seq -> R9
            ],
            photos=[Photo(id="P-102a", exif_datetime=datetime(2026, 6, 1, 15, 0),
                          exif_city="Dubai")],             # EXIF city != loss city -> R16
            documents=[
                DocField(field="loss_date", source="claim_form", value="2026-06-01"),
                DocField(field="loss_date", source="invoice", value="2026-06-10"),
                DocField(field="loss_date", source="pdf_metadata", value="2026-06-18"),  # R14
            ],
        ),
        Claim(
            id="C-103", claim_type="auto_theft", segment="motor",
            party=Party(claimant_id="CL-3", name="Carla Reyes", bank_account="ACC-999",
                        attorney="Shady & Co", policyholder_address="5 Pine Rd, Karachi"),
            policy=Policy(number="P-3", inception_date=date(2024, 3, 1),
                          lapsed=True, reinstatement_date=date(2026, 7, 1)),
            loss_datetime=datetime(2026, 7, 10, 21, 0),   # 9d after reinstatement -> R2
            reported_date=date(2026, 7, 30),
            amount=22000,
            risk_address="5 Pine Rd, Karachi",
            repairer="QuickFix Auto Body",                # R12 + R13
            demands_fast_cash=True, refused_inspection=True,   # R10
            police_report_present=False,                  # theft w/o report -> R11
            photos=[Photo(id="P-103a", phash=_DUP_HASH,   # duplicate of C-101 -> R15
                          exif_datetime=datetime(2026, 7, 10, 21, 30), exif_city="Karachi")],
        ),

        # ---- Serial claimant "Dan": R5 + R8 threshold gaming ----------------
        Claim(
            id="C-201", claim_type="windshield", segment="motor",
            party=Party(claimant_id="CL-4", name="Dan Wu", phone="555-0201",
                        policyholder_address="7 Birch Ln, Karachi"),
            policy=Policy(number="P-4", inception_date=date(2025, 9, 1)),
            loss_datetime=datetime(2026, 2, 3, 10, 0),
            reported_date=date(2026, 2, 4), amount=4875,  # just under 5k -> R8
            risk_address="7 Birch Ln, Karachi", police_report_present=True,
        ),
        Claim(
            id="C-202", claim_type="windshield", segment="motor",
            party=Party(claimant_id="CL-4", name="Dan Wu", phone="555-0201",
                        policyholder_address="7 Birch Ln, Karachi"),
            policy=Policy(number="P-4", inception_date=date(2025, 9, 1)),
            loss_datetime=datetime(2026, 5, 12, 10, 0),
            reported_date=date(2026, 5, 13), amount=4900,  # -> R8
            risk_address="7 Birch Ln, Karachi", police_report_present=True,
        ),
        Claim(
            id="C-203", claim_type="windshield", segment="motor",
            party=Party(claimant_id="CL-4", name="Dan Wu", phone="555-0201",
                        policyholder_address="7 Birch Ln, Karachi"),
            policy=Policy(number="P-4", inception_date=date(2025, 9, 1)),
            loss_datetime=datetime(2026, 7, 20, 10, 0),
            reported_date=date(2026, 7, 21), amount=4850,  # -> R8
            risk_address="7 Birch Ln, Karachi", police_report_present=True,
        ),

        # ---- Clean controls -------------------------------------------------
        Claim(
            id="C-301", claim_type="windshield", segment="motor",
            party=Party(claimant_id="CL-5", name="Eve Moss", phone="555-0301",
                        policyholder_address="1 Cedar Ct, Karachi"),
            policy=Policy(number="P-5", inception_date=date(2024, 4, 1)),
            loss_datetime=datetime(2026, 3, 4, 11, 0),
            reported_date=date(2026, 3, 4), amount=320,
            risk_address="1 Cedar Ct, Karachi", police_report_present=True,
        ),
        Claim(
            id="C-302", claim_type="water_damage", segment="property",
            party=Party(claimant_id="CL-6", name="Frank Ott", phone="555-0302",
                        policyholder_address="4 Maple Dr, Karachi"),
            policy=Policy(number="P-6", inception_date=date(2023, 8, 1)),
            loss_datetime=datetime(2026, 4, 9, 9, 0),
            reported_date=date(2026, 4, 11), amount=3000,
            report_delay_reason="claimant hospitalised; documented",
            risk_address="4 Maple Dr, Karachi", police_report_present=True,
        ),
    ]


# --- dataset persistence -----------------------------------------------------

def export_dataset() -> tuple[Path, Path]:
    """Write the seed book + config out to JSON so the app is dataset-driven.
    Regenerate any time with:  python -m fraud_radar.data.samples
    """
    claims = [c.model_dump(mode="json") for c in _seed_book()]
    CLAIMS_PATH.write_text(json.dumps(claims, indent=2))
    config = {
        "auto_approval_threshold": CONFIG["auto_approval_threshold"],
        "flagged_repairers": CONFIG["flagged_repairers"],
        # JSON has no sets; persist as a sorted list, re-hydrated on load.
        "police_report_required": sorted(CONFIG["police_report_required"]),
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    return CLAIMS_PATH, CONFIG_PATH


def build_book() -> list[Claim]:
    """Load the persisted dataset if present; otherwise fall back to the seed."""
    if CLAIMS_PATH.exists():
        raw = json.loads(CLAIMS_PATH.read_text())
        return [Claim.model_validate(d) for d in raw]
    return _seed_book()


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {
        "auto_approval_threshold": CONFIG["auto_approval_threshold"],
        "flagged_repairers": CONFIG["flagged_repairers"],
        "police_report_required": sorted(CONFIG["police_report_required"]),
    }


def build_context(book=None) -> RuleContext:
    """Base context from the config file (percentiles are filled live by scoring)."""
    cfg = load_config()
    return RuleContext(
        auto_approval_threshold=cfg["auto_approval_threshold"],
        flagged_repairers=cfg["flagged_repairers"],
        police_report_required=set(cfg["police_report_required"]),
    )


if __name__ == "__main__":
    c, cfg = export_dataset()
    print(f"Wrote dataset -> {c}\nWrote config  -> {cfg}")
