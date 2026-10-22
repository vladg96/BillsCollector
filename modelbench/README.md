# modelbench

Compare coding models on **cost per solved task** and get a plain-English explanation of *why* one fits a workload better than another. Stdlib-only Python 3.

```
python3 modelbench.py run --models models.sim.json --simulate --trials 5   # demo, SIMULATED data
cp models.example.json models.json   # fill in endpoint, model id, $/1M-token prices
export GATEWAY_API_KEY=...
python3 modelbench.py run --trials 5                                       # real run, any OpenAI-compatible endpoint
python3 modelbench.py report results.json                                  # re-render
```

Outputs `results.json` (raw rows) and `report.html` (verdict, scoreboard, per-category heatmap, routing policy).

## What it explains
- **$/solved task** = total spend / passes (charges cheap models for failures and reasoning models for hidden thinking tokens)
- **Break-even pass rate**: the accuracy a cheaper model needs to beat the best one on cost-per-solve
- **Failure modes** per model (truncated, wrong answer, syntax error, wrong interface, no code, timeout)
- **Per-category strengths** with Wilson 95% intervals, flagged only when intervals don't overlap
- **Routing policy**: cheapest model meeting a pass-rate threshold per category, and the blended saving

## Custom tasks
Edit `sample_tasks.py` (or write JSON with `id, category, prompt, tests`), then `python3 sample_tasks.py`. The model's code block is concatenated with `tests` and run in a subprocess. Swap in the customer's own repos/tests for a real POC.

## Limits
Single-attempt, unit-test pass/fail; no multi-step agent loops or code-quality scoring. Few trials = wide intervals. Latency includes network noise.

## Industry mode (web UI)
```
python3 server.py --simulate            # demo, fake models + fake judge (labelled SIMULATED)
python3 server.py --models models.json  # live: needs "generator" and "judge" entries, see models.example.json
```
Open http://127.0.0.1:8765, pick an industry, **Generate test case** (the generator model writes a realistic task plus a weighted rubric; you can edit it), then **Run benchmark**.
Each candidate answers; a judge model scores every rubric criterion 1-5 (candidate names hidden). You get quality /100, cost per run, median latency,
a Pareto flag (dominated = another model is at least as good on all three), a plain-English verdict, and per-criterion scores with the judge's reasons.

Caveats: LLM judges favour their own style (use a judge that is not a candidate; the UI warns if it is), and one scenario x few trials is a signal, not a verdict.

## Deploy on Railway (phone access)
1. Railway -> New Project -> Deploy from GitHub repo -> `vladg96/BillsCollector`, branch `modelbench`. In Settings set **Root Directory** to `modelbench` (it builds the Dockerfile).
2. Variables:
   - `APP_PASSWORD` = something only you know (browser asks for it; username can be anything). **Required for live mode.**
   - Demo: `SIMULATE=1`.
   - Live: `MODELS_JSON` = contents of your models.json (with generator + judge), plus the API key env var it references (e.g. `GATEWAY_API_KEY`).
3. Settings -> Networking -> Generate Domain, open it on your phone.
Railway sets `PORT`; the server then binds 0.0.0.0 and refuses to serve live mode without `APP_PASSWORD`.

## Going live in 3 steps (OpenRouter = one key, many vendors)
1. Get a key at openrouter.ai, `export OPENROUTER_API_KEY=...`
2. `cp models.openrouter.example.json models.json`, replace the placeholder ids with real ones from openrouter.ai/models. Prices are fetched automatically; override with `price_in`/`price_out` ($ per 1M tokens) if you want.
3. `python3 server.py --models models.json`  (a spend of roughly a few cents per run with 3-4 models x 2 trials)
Any other OpenAI-compatible gateway works too (set `base_url`, `api_key_env`, and explicit prices).
