# 🛰️ Fraud Pattern Radar

Explainable insurance **claim-fraud scoring** with **fraud-ring detection** —
hackathon use case #12.

A deterministic engine scores every claim against 16 published fraud rules
(**R1–R16**), links nominally unrelated claims into **rings** by shared
identifiers (phone / bank / address / IP / repairer / attorney), and explains
*why* each claim was flagged — with an optional Claude-generated SIU narrative.

## Why it demos well
- **Explainable, not a black box** — every point of the risk score traces to a
  named rule with a claim-specific reason.
- **Fraud-ring graph** — the visual centrepiece: unrelated claims light up as a
  connected ring around a flagged repairer.
- **Runs offline** — no API key needed; narratives fall back to deterministic
  summaries. Add `ANTHROPIC_API_KEY` to switch on live Claude narratives.

## Documentation
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, data model, scoring pipeline, ring detection
- [docs/HOW_TO_USE.md](docs/HOW_TO_USE.md) — run it, use the dashboard/API, edit the dataset, tests
- [docs/DEMO.md](docs/DEMO.md) — 5–7 min demo script with talk track and expected numbers

## Run
```bash
cd backend
./run.sh                 # -> http://localhost:8000
# optional live narratives:
ANTHROPIC_API_KEY=sk-... ./run.sh
```

## Architecture
```
backend/fraud_radar/
  models.py        Pydantic claim / party / invoice / photo models
  rules.py         The 16-rule engine (R1–R16), each rule -> points + reason
  graph.py         Ring detection + cross-claim signals (R5, R12, R15)
  scoring.py       Orchestrates rules + rings + LLM narrative into a banded score
  llm.py           Anthropic wrapper with graceful offline fallback
  data/samples.py  Synthetic book: a fraud ring, a serial claimant, clean controls
  api.py           FastAPI: /api/claims, /api/claims/{id}, /api/graph, /api/score
  web/index.html   Single-page dashboard (queue + ring graph + explainability)
```

## The rules (points)
| | | | |
|---|---|---|---|
| R1 just-in-time cover (15) | R2 reinstated pre-loss (10) | R3 address mismatch (8) | R4 late report (8) |
| R5 serial claimant (12) | R6 weak-witness timing (5) | R7 above-p90 amount (10) | R8 threshold gaming (12) |
| R9 fabricated invoices (8) | R10 fast-cash + no inspect (10) | R11 missing police report (7) | R12 shared-identifier ring (15) |
| R13 flagged repairer (10) | R14 doc inconsistency (10) | R15 duplicate photo (15) | R16 EXIF mismatch (8) |

Score bands: `low` <15 · `elevated` 15–29 · `high` 30–49 · `refer_siu` ≥50.
