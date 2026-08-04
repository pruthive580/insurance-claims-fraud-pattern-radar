"""Pydantic data models for claims and their supporting evidence.

Field names map directly onto the R1–R16 rule triggers so the engine reads
cleanly. Everything the rules need lives on the Claim (or is derived across the
whole book of claims by the engine, e.g. percentiles and shared identifiers).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class Policy(BaseModel):
    number: str
    inception_date: date
    # Most recent coverage increase, if any (R1 second clause).
    coverage_increase_date: Optional[date] = None
    # Reinstatement after a lapse, if any (R2).
    reinstatement_date: Optional[date] = None
    lapsed: bool = False


class Party(BaseModel):
    """The people/handles attached to a claim. Shared values across unrelated
    claims are the raw material for ring detection (R12)."""

    claimant_id: str
    name: str
    phone: Optional[str] = None
    bank_account: Optional[str] = None
    ip_address: Optional[str] = None
    attorney: Optional[str] = None
    # Where the claimant lives (R3 compares this to the risk/garaging address).
    policyholder_address: Optional[str] = None


class LineItem(BaseModel):
    description: str
    amount: float


class Invoice(BaseModel):
    number: str          # invoice number (R9 looks for sequential runs per vendor)
    vendor: str
    issued_date: Optional[date] = None
    line_items: list[LineItem] = Field(default_factory=list)

    @property
    def total(self) -> float:
        return round(sum(li.amount for li in self.line_items), 2)


class Photo(BaseModel):
    id: str
    # Perceptual hash (hex string). Near-equal hashes => re-used image (R15).
    phash: Optional[str] = None
    exif_datetime: Optional[datetime] = None
    exif_city: Optional[str] = None   # city derived from EXIF GPS (R16)


class DocField(BaseModel):
    """A single fact as it appears on a specific document, for cross-doc
    consistency checks (R14). e.g. field='loss_date' may appear differently on
    the claim form vs an invoice vs PDF metadata."""

    field: str
    source: str          # e.g. "claim_form", "invoice", "pdf_metadata"
    value: str


class Claim(BaseModel):
    id: str
    claim_type: str                  # e.g. "auto_theft", "water_damage", "windshield"
    segment: str = "default"         # peer group for percentile comparison (R7)
    party: Party
    policy: Policy

    loss_datetime: datetime          # when the loss occurred
    reported_date: date              # when it was first reported (R4)
    report_delay_reason: Optional[str] = None

    amount: float                    # total claimed amount

    # Risk/garaging address of the insured item (R3).
    risk_address: Optional[str] = None

    # Behavioural flags.
    demands_fast_cash: bool = False
    refused_inspection: bool = False
    police_report_present: bool = False

    # The vendor/repairer/provider handling the repair (R12 shared-node, R13).
    repairer: Optional[str] = None

    invoices: list[Invoice] = Field(default_factory=list)
    photos: list[Photo] = Field(default_factory=list)
    documents: list[DocField] = Field(default_factory=list)


class RuleContext(BaseModel):
    """Book-of-business context the engine computes once, then hands to rules
    that need cross-claim knowledge."""

    # 90th-percentile amount per (claim_type) for R7.
    percentile90: dict[str, float] = Field(default_factory=dict)
    # Auto-approval / SIU-referral thresholds per claim_type for R8.
    auto_approval_threshold: dict[str, float] = Field(default_factory=dict)
    # Repairers with prior SIU flags or high loss ratios for R13.
    flagged_repairers: dict[str, str] = Field(default_factory=dict)  # name -> reason
    # Claim types that require a police report for R11.
    police_report_required: set[str] = Field(default_factory=set)


class FiredRule(BaseModel):
    rule_id: str
    category: str
    points: int
    reason: str          # human-readable "why this fired for THIS claim"


class ClaimScore(BaseModel):
    claim_id: str
    score: int
    band: str            # "low" | "medium" | "high"  (per #12/#48 sheet)
    action: str          # routing action driven by the band
    fired: list[FiredRule]
    ring_ids: list[str] = Field(default_factory=list)   # ring(s) this claim belongs to
    narrative: Optional[str] = None                     # LLM (or deterministic) summary
