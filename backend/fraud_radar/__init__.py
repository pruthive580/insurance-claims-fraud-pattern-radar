"""Fraud Pattern Radar — explainable claim-fraud scoring with ring detection.

Deterministic rule engine (R1–R16) first, with an
optional LLM layer for natural-language "why flagged" narratives. The app never
hard-fails: if no ANTHROPIC_API_KEY is present it runs in offline demo mode.
"""

__version__ = "1.0.0"
