# Architecture — Fraud Pattern Radar (Use Case #12)

## 1. Purpose
Explainable, deterministic **claim-fraud scoring** with **fraud-ring detection**.
Every incoming claim is scored against 16 published rules (R1–R16), routed by a
risk band, and explained down to the individual rule — with an optional
LLM-written investigator narrative on top.

Design principle: **deterministic core, AI garnish.** The score, the bands, and
the ring graph are 100% reproducible from rules; the language model only *phrases*
the "why", and the app runs fully without it (offline demo mode).

## 2. High-level diagram
```
                         ┌──────────────────────────────────────────┐
                         │              Dashboard (SPA)             │
                         │  queue · fraud-ring graph · why-flagged  │
                         └───────────────┬──────────────────────────┘
                                         │ REST/JSON
                         ┌───────────────▼──────────────────────────┐
                         │            FastAPI  (api.py)             │
                         │  /claims  /claims/{id}  /graph  /score   │
                         └───────────────┬──────────────────────────┘
                                         │
                 ┌───────────────────────▼───────────────────────┐
                 │             scoring.py (orchestrator)          │
                 │   percentiles → signals → rules → band/route   │
                 │                    → narrative                 │
                 └───┬───────────────┬───────────────┬───────────┘
                     │               │               │
          ┌──────────▼───┐   ┌───────▼────────┐   ┌──▼───────────┐
          │  rules.py    │   │   graph.py     │   │   llm.py     │
          │  R1–R16      │   │ rings + cross- │   │ Anthropic /  │
          │  each → pts  │   │ claim signals  │   │ offline fbk  │
          │  + reason    │   │ (R5,R12,R15)   │   └──────────────┘
          └──────┬───────┘   └───────┬────────┘
                 │                   │
                 └─────────┬─────────┘
                           │
                   ┌───────▼────────┐        ┌──────────────────────┐
                   │  models.py     │◄───────│ data/claims.json     │
                   │  pydantic      │  load  │ data/config.json     │
                   │  Claim/Party…  │        │ (the dataset)        │
                   └────────────────┘        └──────────────────────┘
```

## 3. Component responsibilities

| Module | Responsibility |
|---|---|
| `models.py` | Pydantic schema for `Claim`, `Party`, `Invoice`, `LineItem`, `Photo`, `DocField`, `RuleContext`, `FiredRule`, `ClaimScore`. Field names map 1:1 to rule triggers. |
| `rules.py` | The 16-rule engine. Each rule `rN(claim, ctx, sig) -> FiredRule | None`, returning fixed points + a **claim-specific** reason. `evaluate()` runs all 16. |
| `graph.py` | Cross-claim analytics: claimant frequency (R5), shared-identifier links (R12), perceptual-hash duplicates (R15), and **connected-component ring detection**. Produces the graph payload for the UI. |
| `scoring.py` | Orchestrator. Derives p90 percentiles, calls `graph.build`, runs `evaluate` per claim, sums points, assigns **band → routing action**, attaches a narrative. |
| `llm.py` | Thin Anthropic Messages wrapper. Returns `None` on missing key/SDK/network so callers fall back to deterministic text. Never hard-fails. |
| `data/samples.py` | Seed generator + dataset load/export. `build_book()` reads `claims.json`; `export_dataset()` regenerates it. |
| `api.py` | FastAPI app. Scores the book once at startup, serves the dashboard + JSON endpoints. |
| `web/index.html` | Single-page dashboard (vanilla JS + vis-network) — queue, ring graph, explainability panel. |

## 4. Data model (core)
```
Claim
 ├─ party: Party           claimant_id, name, phone, bank_account, ip_address,
 │                         attorney, policyholder_address   ← ring identifiers
 ├─ policy: Policy         inception_date, coverage_increase_date,
 │                         reinstatement_date, lapsed
 ├─ loss_datetime, reported_date, report_delay_reason
 ├─ amount, claim_type, segment, risk_address
 ├─ demands_fast_cash, refused_inspection, police_report_present
 ├─ repairer
 ├─ invoices: [Invoice(number, vendor, line_items:[LineItem])]
 ├─ photos:   [Photo(phash, exif_datetime, exif_city)]
 └─ documents:[DocField(field, source, value)]              ← cross-doc checks
```

## 5. Scoring pipeline (per run)
1. **Context** — compute 90th-percentile amount per `claim_type` from the book;
   load thresholds / flagged repairers / police-report-required from `config.json`.
2. **Cross-claim signals** — `graph.build(book)` returns:
   - `claimant_claims_12mo` (R5), `shared_identifiers` (R12), `duplicate_photo` (R15)
   - the ring graph (nodes, edges, connected components).
3. **Rule evaluation** — `evaluate(claim, ctx, sig)` runs R1–R16; each fired rule
   contributes fixed points and a reason.
4. **Band + route** — sum points → band → routing action.
5. **Narrative** — LLM note if a key is present, else a deterministic summary of the
   top-3 drivers.

## 6. Rules → score bands → routing (per #12/#48 sheet)
Rule points (R1..R16): 15, 10, 8, 8, 12, 5, 10, 12, 8, 10, 7, 15, 10, 10, 15, 8.

| Band | Score | Routing action |
|---|---|---|
| **Low** | 0–29 | Straight-through / auto-adjudicate |
| **Medium** | 30–59 | Adjuster review with reason codes |
| **High** | ≥60 | Route to SIU queue (top of investigator list) |

> The sheet writes High as ">60" and Medium as "30–59", leaving 60 undefined; we
> escalate the boundary value 60 into **High** so no score falls through the gap.

## 7. Fraud-ring detection
- Build an index of `(identifier_type, value) → [claims]` over phone / bank /
  address / IP / repairer / attorney.
- A value is a **link** only if it spans **≥2 distinct claimants** (same-claimant
  reuse is *not* a ring — that's R5's job).
- Claims become nodes; shared values become edges (de-duplicated per pair+type).
- **Rings = connected components of size ≥2**, found via DFS. Each gets a `RING-n`
  id shown on the node and in the claim detail.

## 8. API surface
| Method | Path | Returns |
|---|---|---|
| GET | `/` | Dashboard SPA |
| GET | `/api/health` | LLM/offline status, claim & ring counts |
| GET | `/api/claims` | Queue: id, type, claimant, amount, score, band, action, rings |
| GET | `/api/claims/{id}` | Full score (fired rules + reasons + narrative) + raw claim (404 if unknown) |
| GET | `/api/graph` | `{nodes, edges, rings}`, nodes band-coloured |
| POST | `/api/score` | Score an ad-hoc claim against the live book context (422 on invalid payload) |

## 9. Tech stack
- **Python 3.12**, FastAPI + Uvicorn, Pydantic v2.
- **Anthropic SDK** (`claude-haiku-4-5`) for narratives — optional.
- Front-end: static HTML + vanilla JS + **vis-network** (CDN) for the graph.
- No database — the dataset is JSON on disk; the book is scored in memory at startup.
- Available libs (already in venv) for future depth: **pillow** (real EXIF/pHash),
  **pypdf** (real PDF metadata for R14).

## 10. Design decisions & trade-offs
- **Deterministic first** so results are explainable, testable, and reproducible;
  the LLM is never in the scoring path.
- **Exact-match entity linking** for the MVP (fast, transparent). Fuzzy/normalized
  matching is the obvious next step for messy real data.
- **In-memory scoring at startup** keeps the demo simple; for production this moves
  behind a queue with per-claim scoring and persistence.
- **Percentiles computed from the loaded book** (`inclusive` method) — with small
  samples this is approximate; production would use a stable historical baseline.

## 11. Extending
- **New rule** → add `rN` to `rules.py`, append to `ALL_RULES`, add any needed
  field to `models.py`. Points/reason live with the rule.
- **New identifier for rings** → add to `_ID_FIELDS` in `graph.py`.
- **Real dataset** → convert source rows into the `claims.json` shape (see
  `data/samples.export_dataset`), no code change needed to score.
- **Tune routing** → edit the thresholds in `scoring._band` / `ROUTING`.

## 12. Test architecture
- `tests/test_scope.py` — 24 checks mapping the 6 Use Cases + 4 MVP items +
  band/routing conformance (engine + live API).
- `tests/test_extensive.py` — 79 checks: every rule positive/negative/boundary,
  cross-claim internals, ring detection, API robustness (404/422), false-positive
  controls. **103 checks total, all passing.**

---

## Latest additions (AI edge layers, Rule Lab, UX)

The deterministic core (rules → score → band → rings) is unchanged. Everything below sits at the **edges**.

### Provider-aware AI (`llm.py`)
Auto-selects a backend: **local Ollama → Anthropic → offline** (deterministic template).
Config: `FRAUD_LLM=auto|ollama|anthropic|offline`, `FRAUD_OLLAMA_MODEL` (default `qwen3:8b`), `OLLAMA_HOST`.
`run.sh` auto-starts Ollama and pins the model. `think:false` is sent so reasoning models (qwen3) don't blank the output.

### Lazy narratives
Bulk scoring is deterministic/instant. The AI "why flagged" note is generated on demand via
`GET /api/claims/{id}/narrative` (cached), so adding a claim never blocks on the model.

### AI edge layers (`ai.py`)
- `POST /api/extract` — **input edge**: unstructured text/PDF → structured `Claim` (human reviews before scoring).
- `GET /api/claims/{id}/referral` — **output edge**: draft SIU referral memo.
- `GET /api/rings/{ring_id}/summary` — **output edge**: plain-English ring summary.

### Rule Lab — offline rule R&D, human-in-the-loop (`custom_rules.py`)
AI proposes new rules in a **safe DSL** (whitelisted fields + ops, never code). A human adopts them;
adopted rules run **deterministically** alongside R1–R16 and persist to `data/custom_rules.json`.
Endpoints: `POST /api/rules/discover`, `GET/POST /api/rules/custom`, `DELETE /api/rules/custom/{id}`.

### Fuzzy / normalized identifier matching (`graph.py`)
Identifiers are normalized before linking so `555-0100` == `0555 0100`, `Shady & Co` == `shady and co`,
`12 Elm St` == `12 Elm Street`.

### Runtime persistence
Intake claims persist to `data/runtime_claims.json` and survive restart; `POST /api/reset` clears them.

### Entity-graph ring visualization + theming (dashboard)
`/api/graph` now returns `hubs` (shared-identifier nodes). The dashboard renders a bipartite entity graph —
claims + shared-detail boxes (📞/🔧/⚖️) — so a box tied to several claimants *is* the ring.
A light/dark theme toggle is persisted in `localStorage`.
