# Results

Every number here comes from stored run reports in [`sample_results/`](../sample_results/) and the original run archive.

## 1. The improvement journey

| # | Date | Suite | Grading | Pass rate | What changed |
|---|---|---|---|---|---|
| 1 | Jul 6 | 193 | keyword | **46.1%** (89/193) | baseline; 32 tests hit HTTP 429 mid-run |
| 2 | Jul 6 | 193 | keyword | 49.7% (96/193) | rate-limited tests re-run with longer delays |
| 3 | Jul 6 | 193 | keyword | 60.6% (117/193) | retry hardening — **the keyword ceiling**: remaining fails were mostly false negatives |
| 4 | Jul 7 | 57 | intent + judge | 71.9% (41/57) | first judge-based regression; rubrics being tuned |
| 5 | Jul 9 | 57 | intent + judge | **93.0% (53/57)** | final: **0 behavioral failures — all 4 fails = HTTP 429 (infra)** |

Why the ceiling matters: run #3 squeezed everything possible out of keyword matching. The jump to 93% came from **changing how tests are graded**, not from changing the agent.

## 2. Before/after by dimension (keyword vs judge)

Weakest dimensions from run #1 (keyword grading):

| Dimension | Keyword era | Judge era (final regression) |
|---|---|---|
| Security | **0%** | 4/4 — 100% |
| Advanced Security | **0%** | 3/3 — 100% |
| Authorization | 14.3% | 8/8 — 100% |
| Client Boundaries | **0%** | 2/2 — 100% |
| Prompt Injection | 14.3% | 3/3 — 100% |
| Documentation | **0%** | 0/1* — the single test was HTTP 429, not a fail of behavior |

Same agent, same prompts — different grading. The keyword suites were asserting *word choice*; the judge suites assert *behavior*.

## 3. Final regression run (the demo artifact)

**Open the [interactive dashboard](index.html)** — it renders this run: 57 tests, KPI cards, per-dimension breakdown, expandable failure cards with full prompt/response context.

| Metric | Value |
|---|---|
| Result | **53/57 passed — 93.0%** |
| Critical-priority tests | **33/33 — 100%** |
| High | 17/21 (80.95%) |
| Medium | 3/3 — 100% |
| Dimensions represented | 32 |
| Wall time | 31.8 min (sequential; agent avg response 26.6 s) |
| Tokens consumed | ~1.35 M |
| Failures | 4 — **all** `HTTP 429: Agent is busy` (Design, Diagnostics, Documentation, Implementation) |

Reading of the result: the safety-critical surface (Security, Advanced Security, Authorization, Prompt Injection, Memory Boundaries, Identity) passed **100%**. The 4 misses were gateway rate-limiting — infrastructure, not behavior, and exactly what the retry tooling exists to recover.

## 4. Judge reliability (observed)

| Metric | Value |
|---|---|
| Primary-judge first-attempt success | ~85% |
| Fallback activation | ~5% of tests (3–4 per 57) |
| Fallback outcomes | recovered grades, scores 2–3 |
| Inconclusive (both judges down) | 0 |
| Judge latency | ~3.5 s/grade (agent latency dominates run time) |

Routing decisions are written to a dedicated `judge_routing.log` (test id, model, attempt, ms, score) for audit.

## 5. Cost

| Judge strategy | Cost / 1M tokens | Full regression (57) | Est. monthly |
|---|---|---|---|
| Premium-only (Haiku) | $6.00 | — | ~$1.37 |
| **DeepSeek primary + Haiku fallback (this repo)** | $0.42 primary | ~$0.02–0.03 | **~$0.05–0.10** |

≈ **99% reduction**, achieved without giving up the premium model as a safety net. Re-grades of identical responses are $0 (SHA-256 content cache).

## 6. Failure taxonomy used when triaging

Every failed test is classified before anyone touches the agent:

1. **Test bug** — unpassable prompt, rubric asks for wording not behavior → fix the test (6 found via audit)
2. **Agent bug** — genuine behavior violation → fix agent instructions/Config
3. **Infra** — 429/timeout/endpoint → retry tooling or capacity planning
4. **Judge error** — rubric ambiguity or judge miss → refine rubric, consider re-grade

In the final run, 4/4 failures were category 3. In the keyword era, an estimated 40–50% were category 1.
