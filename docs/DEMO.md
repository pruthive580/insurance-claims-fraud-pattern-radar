# Demonstration Guide — Fraud Pattern Radar (#12)

A 5–7 minute demo script: what to say, what to click, and the exact numbers to
expect. The dataset is engineered so all 16 rules and a fraud ring are visible.

## 0. Before you start (30s)
```bash
cd fraud-radar/backend && ./run.sh
```
Open **http://localhost:8000**. Confirm the header shows `8 claims · 1 ring(s)`.
Optional: export a fresh dataset first with `python -m fraud_radar.data.samples`.

> Tip: even offline it works — narratives fall back to deterministic summaries.
> Add `ANTHROPIC_API_KEY` beforehand if you want live Claude notes.

## 1. The one-liner (30s)
> "Fraud Pattern Radar scores every insurance claim against 16 fraud rules,
> ranks an investigator queue, explains **why** each claim is suspicious, and
> uncovers **organised fraud rings** by linking claims that share infrastructure.
> The scoring is fully deterministic and explainable — the AI only writes the
> investigator note."

## 2. The investigator queue → SIU prioritization (1 min)
Point at the **left panel**. It's sorted by risk, highest first:

| Claim | Band | Score | Routing |
|---|---|---|---|
| C-101 | HIGH | 82 | SIU queue |
| C-103 | HIGH | 75 | SIU queue |
| C-102 | HIGH | 69 | SIU queue |
| C-202 | MEDIUM | 34 | Adjuster review |
| C-201 | LOW | 24 | Auto-adjudicate |
| C-203 | LOW | 24 | Auto-adjudicate |
| C-301 | LOW | 0 | Auto-adjudicate |
| C-302 | LOW | 0 | Auto-adjudicate |

> "The top three are auto-routed to the SIU queue; the clean claims at the bottom
> straight-through auto-adjudicate. This is MVP #4 and Use Case #4 — SIU
> prioritization — out of the box." *(Covers score bands → action from the sheet.)*

## 3. Explainability → why flagged (1.5 min)
Click **C-101**. Walk the **right panel**:
- Header: **82 · HIGH · ➜ Route to SIU queue · RING-1**.
- Narrative (one line, plain English).
- The rule list — read 2–3 aloud:
  - `R1 +15` filed 8 days after policy inception (just-in-time cover)
  - `R12 +15` shares a repairer with other unrelated claims (ring)
  - `R13 +10` that repairer has prior SIU referrals
  - `R8 +12` amount $24,000 sits just under the $25,000 auto-approval threshold

> "Every point traces to a named rule with a claim-specific reason — nothing is a
> black box. The points **add up exactly** to the score. That's Use Case #5 and
> MVP #2." *(You can note R7/R6/R11/R3 also fired.)*

## 4. Anomaly detection (45s)
Still on C-101 / hop to C-102:
- **Amount** anomalies — R7 (above 90th percentile), R8 (threshold gaming).
- **Timing** anomalies — R1 (inception), R2 (reinstatement, see C-103), R4 (late
  report, C-102), R6 (2:30 a.m. weekend loss, C-101).
- **Frequency** — R5 on claimant **Dan** (C-201/202/203: three claims in 12 months).

> "Use Case #2 — anomalies across amount, timing, and frequency — each surfaces
> as its own explainable rule."

## 5. The centrepiece — fraud-ring detection (1.5 min)
Point at the **centre graph**. Three red nodes (C-101/102/103) are wired together.
Click an edge / hover to show labels.

> "These are three **different claimants** — Alice, Bruno, Carla — with separate
> policies. On the surface, unrelated. But they share the same flagged **repairer**,
> a **phone number**, a **bank account**, and an **attorney**. The network analysis
> connects them into **RING-1**. That's Use Case #6 and #3 — fraud-ring detection
> and entity linking — the thing rules-in-isolation can never catch."

Contrast: the LOW/clean nodes sit **unconnected** on the edge of the canvas.

## 6. Live scoring at intake → FNOL (1 min) — do this in the UI
Click **“＋ Score claim at intake (FNOL)”** (top bar). In the dialog, hit the
**🔴 Ring member (walk-in)** preset, then **Score claim ▶**.

What the audience sees, live:
- The claim scores **74 · HIGH → SIU queue**, explained in the right panel.
- It **appears at the top of the queue**, tagged `LIVE`.
- A new **dashed node animates into the graph and links to RING-1** — because it
  reuses the ring's phone and repairer (R12 fires on the fly).

Try the other presets: **🟠 Threshold gamer** → 35 · MEDIUM (adjuster review);
**🟢 Clean claim** → 0 · LOW (auto-adjudicate). You can also hand-edit the JSON.

> "A claim we've never seen, scored at FNOL right in the console. Because it reuses
> the ring's phone and repairer, it's **instantly pulled into RING-1** — the ring
> detection is dynamic, not precomputed. That's Use Case #1 — scoring at intake."

*(Prefer a terminal? The same `POST /api/score` body returns score 74 / HIGH / RING-1.)*

## 7. Trust it → the tests (30s)
```bash
.venv/bin/python -m tests.test_scope        # 24/24
.venv/bin/python -m tests.test_extensive    # 79/79
```
> "103 automated checks, all green: every rule's positive, negative, and boundary
> behaviour; ring logic; API robustness; and a negative control proving clean
> claims stay clean — it discriminates, it doesn't just flag everything."

## 8. Close (15s)
> "Deterministic, explainable, dataset-driven, and ring-aware — mapping cleanly to
> all six use cases and all four MVP items. Next steps: fuzzy entity linking for
> messy real data, real EXIF/pHash on uploaded photos, and wiring the official
> #12/#48 dataset in — the engine is already dataset-agnostic."

---

## Demo cheat-sheet (numbers to expect)
| Thing | Value |
|---|---|
| Claims / rings | 8 / 1 |
| Ring members | C-101, C-102, C-103 (3 distinct claimants) |
| Ring links | repairer, phone, bank account, attorney |
| Top score | C-101 = 82 (HIGH → SIU) |
| Serial claimant | Dan → C-201/202/203 (R5) |
| Clean controls | C-301, C-302 = 0 (LOW) |
| Live FNOL score | ≈ 74, HIGH, RING-1 |
| Tests | 24 scope + 79 extensive = 103, all pass |

## Rule → use-case map (for Q&A)
| Use Case | Backed by |
|---|---|
| 1 · Scoring at FNOL/intake | `POST /api/score` (all rules) |
| 2 · Anomaly (amount/timing/frequency) | R7,R8 / R1,R2,R4,R6 / R5 |
| 3 · Entity linking (claimants/providers/addresses) | R5, R12, R13 + graph |
| 4 · SIU prioritization | ranked queue + bands → routing |
| 5 · Explanation layer | per-rule reasons + narrative + additive score |
| 6 · Fraud-ring detection | R12 + connected-component analysis |

## Failure recovery during a live demo
| If… | Do |
|---|---|
| Graph doesn't render | Network/CDN blocked — narrate from the queue + detail panel instead; the API data is all there. |
| Server not responding | `pkill -f "uvicorn fraud_radar"; ./run.sh` and reload. |
| Narratives look generic | You're in offline mode (expected). Set `ANTHROPIC_API_KEY` and restart for live Claude notes. |
| Someone doubts a score | Open that claim — the rule list sums exactly to the score; run `test_extensive` live. |

---

## Latest additions — extra demo beats

1. **AI reads a messy claim.** ＋ Score claim at intake → AI intake box → paste:
   *"Auto theft claim for $24,000 filed 5 days after buying the policy, QuickFix Auto Body, phone 555-0100,
   no police report, demanded fast cash, refused inspection, loss Saturday 2am, policyholder in Lahore but
   garaged in Karachi."* → Extract with AI → Score. Lands ~82 / HIGH. Message: "real claims aren't clean JSON."
2. **The ring, made obvious.** Point at the entity graph: three claims all wire into the 🔧 QuickFix and
   ⚖️ Shady & Co boxes → that convergence *is* RING-1. Click a claim → **📝 Draft SIU referral** and
   **🕸️ Summarize RING-1**.
3. **Rule Lab (the "AI grows the engine" beat).** 🧪 Rule Lab → Discover → adopt "shared IP / represented repeat
   claimant" → watch scores update live and the new rule appear as CR1. Message: "AI proposes, a human ratifies,
   the math decides."
4. **Theme toggle** for the room's lighting: header 🌙 / ☀️.

Reset before a clean run: **⟲ Reset**.
