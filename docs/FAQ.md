# FAQ

**Why grade with an LLM instead of assertions?**
Because agents answer in natural language, not fixed output. Keyword assertions scored ~46–60% in practice — with an estimated 40–50% of those failures being *false negatives* (right behavior, different words). An intent + rubric judge grades what the response *means*, which survives paraphrasing and model updates.

**How do you keep the judge itself from being wrong?**
Guardrails: per-test rubrics (not one generic prompt), strict JSON contract with retry on parse failure, scores clamped to 0–3, `passed = score >= 2` enforced in code, explicit `INCONCLUSIVE` when no judge responds, full routing log for audit. What's still missing — and called out in [EVAL_METHODOLOGY.md](EVAL_METHODOLOGY.md) — is a measured human/LLM agreement study (Cohen's κ). Until that exists, treat scores as consistent-but-not-yet-validated.

**Why two judge models?**
Cost/quality tradeoff. The primary (DeepSeek Flash, $0.42/1M) grades ~85% of tests on the first attempt for cents per run. The premium fallback (Claude Haiku, $6/1M) catches primary outages and throttle storms — but only *per test*: after a fallback, the next test tries the primary again, so one hiccup doesn't demote the whole run.

**What happens if both judges fail?**
The test is marked `INCONCLUSIVE` with `score: 0` and an explicit reason. It can never silently appear as a pass.

**Why is execution sequential?**
The agent gateway allows concurrency 1 — parallel requests would trip rate limits (HTTP 429) and produce noise instead of results. The harness respects that with an inter-test delay plus a circuit breaker that backs off when the gateway starts refusing.

**How do you handle rate limits?**
Three layers: in-run transient retries with exponential backoff, a circuit breaker to stop cascading 429s, and `runners/rerun_failed_tests.py` which re-runs failures with escalating waits (30/60/90/120 s) and merges results — so infra noise never erases real grades.

**How do I test a different agent?**
Add an entry to `config/agents.yaml` (name, endpoint, model) and a test CSV, then `python run_tests.py --agent <key> --suite regression`. The harness doesn't care which agent it talks to.

**Why keep `keyword_grader.py` if everything uses the judge now?**
History with teeth: it documents the Phase-1 approach that produced the 46% baseline and the false-negative analysis that justified the redesign. It's wired as a no-op so old report schemas still load.

**Is it CI-ready?**
Partially — exit codes are CI-friendly (`0` only if pass rate ≥ 95%), `--dry-run` and `--limit` support pipeline smoke checks, reports are timestamped JSON + HTML. Pipeline wiring (nightly job + published HTML artifact) is on the roadmap, not built yet.

**Where do secrets live?**
Only in `.env` (git-ignored, excluded from this repo; `.env.example` shows the required keys). Settings validates keys at boot and redacts them from logs.

**What do the reports contain?**
JSON: everything machine-readable (per-test scores, reasons, tokens, latencies, full agent responses). HTML: the same data as a human dashboard with KPI cards, dimension breakdowns, and expandable failure cards — see the [sample dashboard](index.html).
