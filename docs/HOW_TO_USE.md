# How to Use — Fraud Pattern Radar

## 1. Prerequisites
- macOS/Linux, Python 3.12 (already provisioned in `backend/.venv`).
- Internet access **only** for the dashboard's graph library (vis-network CDN).
  The API itself runs fully offline.
- Optional: an `ANTHROPIC_API_KEY` to enable live Claude narratives.

## 2. Start the app
```bash
cd fraud-radar/backend
./run.sh                       # -> http://localhost:8000
```
Or explicitly with the venv:
```bash
.venv/bin/python -m uvicorn fraud_radar.api:app --host 0.0.0.0 --port 8000
```
Enable live AI narratives:
```bash
ANTHROPIC_API_KEY=sk-... ./run.sh
```
Open **http://localhost:8000** in a browser.

### Health check
```bash
curl -s http://localhost:8000/api/health
# {"status":"🟡 Offline demo mode (deterministic)","ai_mode":false,"claims":8,"rings":1}
```
`🟢 AI mode` means a live model is wired in; `🟡 Offline demo mode` means
deterministic narratives (everything else is identical).

## 3. Using the dashboard
Three panels:
1. **Claim queue (left)** — every claim, ranked by risk. Each row shows the band
   badge (LOW/MEDIUM/HIGH), score, ring tag, claimant, type, amount. Click a row
   to inspect.
2. **Fraud-ring network (centre)** — nodes are claims (colour = band, size = score,
   thick border = ring member). Red edges are shared identifiers (phone / bank /
   address / repairer / attorney). Click any node to inspect that claim.
3. **Why-flagged (right)** — the selected claim's score, band, **routing action**,
   the AI/deterministic narrative, and every fired rule with its points and a
   plain-English reason.

## 4. Using the API

### List the investigator queue (ranked)
```bash
curl -s http://localhost:8000/api/claims | python3 -m json.tool
```
Each item: `claim_id, claim_type, claimant, amount, score, band, action, ring_ids, rule_count`.

### Inspect one claim (full explanation)
```bash
curl -s http://localhost:8000/api/claims/C-101 | python3 -m json.tool
```
Returns `{ "score": {…fired rules, reasons, narrative…}, "claim": {…raw…} }`.

### Get the ring graph
```bash
curl -s http://localhost:8000/api/graph | python3 -m json.tool
# { "nodes":[…band-coloured…], "edges":[…shared identifiers…], "rings":[[...]] }
```

### Score a brand-new claim at intake (FNOL)
```bash
curl -s -X POST http://localhost:8000/api/score \
  -H 'content-type: application/json' \
  -d '{
    "id":"NEW-1","claim_type":"auto_theft",
    "party":{"claimant_id":"CL-99","name":"New Claimant","phone":"555-0100"},
    "policy":{"number":"P-99","inception_date":"2026-07-25"},
    "loss_datetime":"2026-07-26T02:00:00","reported_date":"2026-07-28",
    "amount":24500,"risk_address":"Somewhere, Karachi",
    "repairer":"QuickFix Auto Body","police_report_present":false
  }' | python3 -m json.tool
```
The new claim is scored **against the existing book**, so shared phone/repairer
pull it into the live ring (R12) and percentile context (R7) still applies.
Invalid payloads return HTTP **422** with validation detail.

## 5. Reading a result
```
score  82   band HIGH   → Route to SIU queue        RING-1
R12 +15  Shares identifiers with other unrelated claims: repairer 'quickfix…'
R13 +10  Repairer 'QuickFix Auto Body' is flagged: 3 prior SIU referrals…
R1  +15  Claim filed 8 day(s) after policy inception (2026-07-20)
…
```
- **score** = sum of fired-rule points (always adds up — fully explainable).
- **band → action** = the routing decision (see table below).
- Each **Rn +pts** line is an auditable reason an investigator can act on.

| Band | Score | Action |
|---|---|---|
| LOW | 0–29 | Straight-through / auto-adjudicate |
| MEDIUM | 30–59 | Adjuster review with reason codes |
| HIGH | ≥60 | Route to SIU queue (top of the list) |

## 6. Working with the dataset
The claims live in **`backend/fraud_radar/data/claims.json`** and thresholds in
**`config.json`** — the app loads these at startup.

- **Edit claims:** change `claims.json` (or `config.json`) and restart the server.
- **Regenerate from the seed:**
  ```bash
  cd backend && .venv/bin/python -m fraud_radar.data.samples
  ```
- **Import a real dataset:** transform your source rows into the `claims.json`
  shape (same fields as `models.Claim`), drop it in, restart. No code changes
  needed to score.

`config.json` keys:
- `auto_approval_threshold` — per-`claim_type` threshold used by R8.
- `flagged_repairers` — `name -> reason` used by R13.
- `police_report_required` — claim types that must have a police report (R11).

## 7. Running the tests
```bash
cd backend
.venv/bin/python -m tests.test_scope        # 24 scope + band/routing checks
.venv/bin/python -m tests.test_extensive    # 79 rule/internal/API/robustness checks
```
Both exit `0` when everything passes (103 checks total). `test_scope` needs the
server running; `test_extensive` runs the engine directly and hits the API for the
robustness section.

## 8. Troubleshooting
| Symptom | Fix |
|---|---|
| Graph area blank | vis-network CDN blocked — allow network, or self-host the JS. |
| `🟡 Offline demo mode` | Expected without a key; set `ANTHROPIC_API_KEY` for live narratives. |
| `test_scope` connection errors | Start the server first (`./run.sh`). |
| Port 8000 busy | `pkill -f "uvicorn fraud_radar"` then restart, or pass `--port 8080`. |
| Dataset edits not showing | Restart the server (the book is scored once at startup). |

---

## Latest additions — how to use

### AI mode with local Ollama
```
ollama pull qwen3:8b            # one time
./run.sh                        # auto-starts Ollama, uses qwen3:8b
FRAUD_OLLAMA_MODEL=llama3.2:latest ./run.sh   # faster, lower fidelity
FRAUD_LLM=offline ./run.sh      # force deterministic
```
The status bar shows the active backend (e.g. "AI mode (Ollama · qwen3:8b)").

### AI intake (unstructured → scored)
＋ Score claim at intake → **✨ AI intake** box → describe a claim in plain English → **Extract with AI** →
review the JSON it fills in → **Score ▶**. Endpoint: `POST /api/extract {"text": "..."}`.

### Draft SIU referral / ring summary
On a claim's detail panel: **📝 Draft SIU referral** and (if in a ring) **🕸️ Summarize RING-1**.
Endpoints: `GET /api/claims/{id}/referral`, `GET /api/rings/{id}/summary`.

### Rule Lab (discover & adopt new rules)
Header → **🧪 Rule Lab** → **Discover missing rules with AI** → review proposals (each shows which claims it
would flag) → **➕ Adopt**. Adopted rules re-score the book live and appear as `CR1, CR2…` in 📖 Rules.
Remove any with the Remove button. Endpoints under `/api/rules/discover` and `/api/rules/custom`.

### Reading the ring graph
Circles = claims (colour = risk, size = score). Boxes = shared details (📞 phone, 🔧 repairer, ⚖️ attorney,
🏦 bank, 🏠 address, 🌐 IP). A **red box** wired to several different claimants is a fraud ring. Click a box to
highlight its claims; click a claim to open it.

### Theme
Header → 🌙 / ☀️ toggles light/dark (remembered across visits).
