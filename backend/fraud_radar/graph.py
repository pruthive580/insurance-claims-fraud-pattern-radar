"""Cross-claim analytics: fraud-ring detection and the signals that feed the
rules which can't decide from one claim alone (R5, R12, R15).

A "ring" is a connected component of claims linked by a shared identifier
(phone / bank / IP / address / repairer / attorney) belonging to *different*
claimants — organised fraud reuses infrastructure across nominally unrelated
claims.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .models import Claim
from .rules import CrossSignals

# Identifier types we link on, with display labels.
_ID_FIELDS = [
    ("phone", "phone", lambda c: c.party.phone),
    ("bank", "bank account", lambda c: c.party.bank_account),
    ("ip", "IP", lambda c: c.party.ip_address),
    ("attorney", "attorney", lambda c: c.party.attorney),
    ("address", "address", lambda c: c.party.policyholder_address),
    ("repairer", "repairer", lambda c: c.repairer),
]

# Common street-type abbreviations, normalised so "12 Elm St" == "12 Elm Street".
_STREET_ABBR = {"st": "street", "rd": "road", "ave": "avenue", "av": "avenue",
                "ln": "lane", "dr": "drive", "ct": "court", "blvd": "boulevard",
                "apt": "apartment", "hwy": "highway"}


def _normalize(id_type: str, value: str) -> str:
    """Collapse cosmetic differences so the same real-world identifier matches
    regardless of formatting:
        '555-0100' == '0555 0100'   (phone: digits only, drop leading zeros)
        'ACC-999'  == 'acc 999'     (bank: alphanumerics only)
        'Shady & Co' == 'shady and co'
        '12 Elm St' == '12 Elm Street'
    """
    v = str(value).strip().lower()
    if id_type == "phone":
        digits = re.sub(r"\D", "", v)
        return digits.lstrip("0") or digits
    if id_type == "bank":
        return re.sub(r"[^a-z0-9]", "", v)
    if id_type in ("attorney", "repairer"):
        return re.sub(r"[^a-z0-9]", "", v.replace("&", "and"))
    if id_type == "address":
        v = re.sub(r"[^\w\s]", " ", v)
        return " ".join(_STREET_ABBR.get(t, t) for t in v.split())
    return v  # ip / fallback


def _hamming_hex(a: str, b: str) -> int:
    """Bit-difference between two equal-length hex perceptual hashes."""
    if not a or not b or len(a) != len(b):
        return 999
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 999


def _claimant_frequency(book: list[Claim]) -> dict[str, int]:
    """Claims per claimant within the trailing 12 months of the latest report."""
    if not book:
        return {}
    today = max(c.reported_date for c in book)
    cutoff = today - timedelta(days=365)
    counts: dict[str, int] = {}
    for c in book:
        if c.reported_date >= cutoff:
            counts[c.party.claimant_id] = counts.get(c.party.claimant_id, 0) + 1
    return counts


def _duplicate_photos(book: list[Claim], threshold: int = 10) -> dict[str, str]:
    """Map a claim to an earlier claim whose photo it perceptually duplicates."""
    dup: dict[str, str] = {}
    ordered = sorted(book, key=lambda c: c.reported_date)
    seen: list[tuple[str, str]] = []  # (phash, claim_id)
    for c in ordered:
        for ph in c.photos:
            if not ph.phash:
                continue
            for prev_hash, prev_claim in seen:
                if prev_claim != c.id and _hamming_hex(ph.phash, prev_hash) <= threshold:
                    dup[c.id] = f"claim {prev_claim}"
                    break
            seen.append((ph.phash, c.id))
    return dup


def build(book: list[Claim]) -> tuple[CrossSignals, dict]:
    """Return (cross-claim signals, ring graph payload)."""
    # --- shared identifiers across different claimants -----------------------
    # value_index: (id_type, value) -> list of (claim_id, claimant_id)
    value_index: dict[tuple[str, str], list[tuple[str, str]]] = {}
    display_of: dict[tuple[str, str], str] = {}   # normalised key -> readable value
    for c in book:
        for key, _label, getter in _ID_FIELDS:
            raw = getter(c)
            if not raw:
                continue
            norm = _normalize(key, raw)
            if not norm:
                continue
            value_index.setdefault((key, norm), []).append((c.id, c.party.claimant_id))
            display_of.setdefault((key, norm), str(raw).strip())

    edges: list[dict] = []
    edge_seen: set[tuple[str, str, str]] = set()
    shared: dict[str, list[str]] = {}
    hubs: list[dict] = []   # shared-identifier nodes (the "what connects them")

    for (key, norm), members in value_index.items():
        claimants = {cl for _, cl in members}
        # Only a signal if the same value spans >= 2 *distinct* claimants.
        if len(members) < 2 or len(claimants) < 2:
            continue
        label = next(lbl for k, lbl, _ in _ID_FIELDS if k == key)
        disp = display_of[(key, norm)]
        claim_ids = [cid for cid, _ in members]
        hubs.append({"id": f"HUB::{key}::{norm}", "type": key, "label": label,
                     "value": disp, "claims": claim_ids})
        for cid in claim_ids:
            others = [o for o in claim_ids if o != cid]
            shared.setdefault(cid, []).append(
                f"{label} '{disp}' (also on {', '.join(others)})")
        # Pairwise edges (undirected, deduped).
        for i in range(len(claim_ids)):
            for j in range(i + 1, len(claim_ids)):
                a, b = sorted((claim_ids[i], claim_ids[j]))
                ek = (a, b, key)
                if ek not in edge_seen:
                    edge_seen.add(ek)
                    edges.append({"source": a, "target": b,
                                  "label": f"{label}: {disp}"})

    # --- connected components (rings) ----------------------------------------
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e["source"], set()).add(e["target"])
        adj.setdefault(e["target"], set()).add(e["source"])

    rings: list[list[str]] = []
    visited: set[str] = set()
    for node in adj:
        if node in visited:
            continue
        stack, comp = [node], []
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            comp.append(n)
            stack.extend(adj[n] - visited)
        if len(comp) >= 2:
            rings.append(sorted(comp))

    ring_of: dict[str, str] = {}
    for idx, comp in enumerate(rings, start=1):
        for cid in comp:
            ring_of[cid] = f"RING-{idx}"

    nodes = [{"id": c.id, "claimant": c.party.name,
              "ring": ring_of.get(c.id), "amount": c.amount}
             for c in book]

    signals = CrossSignals(
        claimant_claims_12mo=_claimant_frequency(book),
        shared_identifiers=shared,
        duplicate_photo=_duplicate_photos(book),
    )
    graph = {"nodes": nodes, "edges": edges, "rings": rings, "ring_of": ring_of,
             "hubs": hubs}
    return signals, graph
