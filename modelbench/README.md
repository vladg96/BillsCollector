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
