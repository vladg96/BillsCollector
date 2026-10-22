#!/usr/bin/env python3
"""modelbench: compare coding models on cost per *solved* task, and explain why.

  modelbench.py run    --models models.json --tasks tasks/sample.json --trials 5
  modelbench.py run    --models models.sim.json --simulate        # no API keys, SIMULATED data
  modelbench.py report results.json                               # re-render report

Works against any OpenAI-compatible /chat/completions endpoint. Stdlib only.
"""
import argparse, json, math, os, random, re, statistics, subprocess, sys, tempfile, time
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from html import escape

SYSTEM = "You are a careful senior engineer. Reply with the requested code in a single ```python block and nothing else."

# ---------------------------------------------------------------- model calls
def call_openai_compat(m, prompt, timeout=120, system=SYSTEM):
    url = m["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": m["model"], "temperature": m.get("temperature", 0.2),
            "max_tokens": m.get("max_tokens", 2048),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    headers = {"Content-Type": "application/json"}
    key = os.environ.get(m.get("api_key_env", ""), "")
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    ch = d["choices"][0]
    u = d.get("usage", {})
    return dict(text=ch["message"].get("content") or "", finish=ch.get("finish_reason"),
                tin=u.get("prompt_tokens", 0), tout=u.get("completion_tokens", 0),
                latency=time.time() - t0)

def call_simulated(m, task, trial):
    """Deterministic fake model. Skill/verbosity/latency come from models.sim.json. NOT real data."""
    rng = random.Random(f"{m['name']}|{task['id']}|{trial}")
    p = m["sim"]["skill"].get(task["category"], m["sim"]["skill"]["default"])
    r = rng.random()
    if r < p:
        text, finish = "```python\n" + task["reference"] + "\n```", "stop"
    elif r < p + (1 - p) * m["sim"].get("truncate_share", 0):
        text, finish = "```python\n" + task["reference"][: len(task["reference"]) // 2], "length"
    elif r < p + (1 - p) * 0.75:
        text, finish = "```python\n" + task["broken"] + "\n```", "stop"
    else:
        text, finish = "I'd suggest restructuring this; here is the idea in prose.", "stop"
    tin = (len(task["prompt"]) + len(SYSTEM)) // 4
    tout = int(len(text) / 4 * m["sim"].get("verbosity", 1) * rng.uniform(0.8, 1.3)) + m["sim"].get("reasoning_tokens", 0)
    lat = m["sim"]["ttft"] + tout / m["sim"]["tps"] * rng.uniform(0.85, 1.25)
    return dict(text=text, finish=finish, tin=tin, tout=tout, latency=lat)

def fill_prices(cfg):
    """Fill missing price_in/price_out ($ per 1M tokens) from OpenRouter's public model list, so cost numbers are real."""
    entries = [m for m in cfg["models"] + [cfg.get("generator"), cfg.get("judge")] if m and "price_in" not in m]
    if not entries: return
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=30) as r:
        prices = {d["id"]: d["pricing"] for d in json.load(r)["data"]}
    for m in entries:
        p = prices.get(m["model"])
        if not p: raise SystemExit(f"No price for model id '{m['model']}': check the id at openrouter.ai/models or set price_in/price_out")
        m["price_in"], m["price_out"] = float(p["prompt"]) * 1e6, float(p["completion"]) * 1e6

# ---------------------------------------------------------------- grading
def extract_code(text):
    blocks = re.findall(r"```(?:python|py)?\n(.*?)```", text, re.S)
    if blocks:
        return max(blocks, key=len)
    m = re.search(r"```(?:python|py)?\n(.*)$", text, re.S)  # unterminated (truncated)
    return m.group(1) if m else None

def grade(task, resp):
    """Returns (passed, failure_class)."""
    code = extract_code(resp["text"])
    if resp["finish"] == "length":
        return False, "truncated"
    if code is None:
        return False, "no_code"
    src = code + "\n\n" + task["tests"] + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(src); path = f.name
    try:
        r = subprocess.run([sys.executable, "-I", path], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    finally:
        os.unlink(path)
    if r.returncode == 0:
        return True, None
    err = r.stderr
    if "SyntaxError" in err or "IndentationError" in err: return False, "syntax_error"
    if "AssertionError" in err: return False, "wrong_answer"
    if "NameError" in err or "TypeError" in err or "AttributeError" in err: return False, "wrong_interface"
    return False, "runtime_error"

def cost(m, r):
    return (r["tin"] * m["price_in"] + r["tout"] * m["price_out"]) / 1e6

# ---------------------------------------------------------------- run
def run(models, tasks, trials, simulate, workers):
    jobs = [(m, t, i) for m in models for t in tasks for i in range(trials)]
    def one(job):
        m, t, i = job
        try:
            r = call_simulated(m, t, i) if simulate else call_openai_compat(m, t["prompt"])
            ok, why = grade(t, r)
            return dict(model=m["name"], task=t["id"], category=t["category"], trial=i, passed=ok, failure=why,
                        tin=r["tin"], tout=r["tout"], latency=r["latency"], cost=cost(m, r))
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            return dict(model=m["name"], task=t["id"], category=t["category"], trial=i, passed=False,
                        failure="api_error", tin=0, tout=0, latency=0.0, cost=0.0, error=str(e)[:200])
    with ThreadPoolExecutor(workers) as ex:
        rows = list(ex.map(one, jobs))
    return dict(simulated=simulate, trials=trials, models=[{k: m[k] for k in ("name", "price_in", "price_out")} for m in models],
                tasks=[{"id": t["id"], "category": t["category"]} for t in tasks], rows=rows, created=time.strftime("%Y-%m-%d %H:%M"))

# ---------------------------------------------------------------- analysis
def wilson(k, n, z=1.96):
    if n == 0: return (0.0, 1.0)
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0, c - h), min(1, c + h))

def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0

def analyze(res, threshold=0.8):
    rows, models = res["rows"], [m["name"] for m in res["models"]]
    cats = sorted({r["category"] for r in rows})
    S = {}
    for m in models:
        rs = [r for r in rows if r["model"] == m]
        k, n = sum(r["passed"] for r in rs), len(rs)
        total = sum(r["cost"] for r in rs)
        fails = {}
        for r in rs:
            if r["failure"]: fails[r["failure"]] = fails.get(r["failure"], 0) + 1
        by_cat = {}
        for c in cats:
            cr = [r for r in rs if r["category"] == c]
            ck, cn = sum(r["passed"] for r in cr), len(cr)
            by_cat[c] = dict(k=ck, n=cn, rate=ck / cn if cn else 0, ci=wilson(ck, cn),
                             cost=sum(r["cost"] for r in cr), cps=(sum(r["cost"] for r in cr) / ck) if ck else None)
        S[m] = dict(k=k, n=n, rate=k / n if n else 0, ci=wilson(k, n), cost_run=total / n if n else 0,
                    cps=total / k if k else None, p50=statistics.median([r["latency"] for r in rs]) if rs else 0,
                    p95=pct([r["latency"] for r in rs], .95), avg_out=statistics.mean([r["tout"] for r in rs]) if rs else 0,
                    fails=fails, by_cat=by_cat)
    best = max(models, key=lambda m: (S[m]["rate"], -S[m]["cost_run"]))
    ins = []
    b = S[best]
    ins.append(f"**{best}** has the highest pass rate ({b['rate']:.0%}, 95% CI {b['ci'][0]:.0%}–{b['ci'][1]:.0%}) and is the quality baseline.")
    cheapest = min((m for m in models if S[m]["cps"]), key=lambda m: S[m]["cps"])
    if cheapest != best:
        ins.append(f"Cheapest **per solved task** is **{cheapest}** at ${S[cheapest]['cps']:.4f} vs ${b['cps']:.4f} for {best} "
                   f"({b['cps'] / S[cheapest]['cps']:.1f}× cheaper, at a {S[cheapest]['rate']:.0%} pass rate) — sticker price alone would say "
                   f"{b['cost_run'] / max(S[cheapest]['cost_run'], 1e-12):.1f}×.")
    for m in models:
        if m == best: continue
        s = S[m]; why = []
        be = s["cost_run"] / b["cps"] if b["cps"] else None
        if be is not None and be >= 1:
            why.append(f"costs more per attempt (${s['cost_run']:.4f}) than {best} costs per solved task (${b['cps']:.4f}), so it cannot win on cost even at 100% pass rate")
        elif be is not None:
            verdict = "clears" if s["rate"] > be else "does not clear"
            why.append(f"needs a pass rate above {be:.0%} to beat {best} on cost-per-solve; it scores {s['rate']:.0%} and {verdict} that bar")
        weak = [c for c in cats if b["by_cat"][c]["ci"][0] > s["by_cat"][c]["ci"][1]]
        if weak: why.append("significantly weaker on " + ", ".join(weak))
        if s["fails"]:
            top = max(s["fails"], key=s["fails"].get)
            why.append(f"dominant failure mode: {top.replace('_', ' ')} ({s['fails'][top]}/{s['n'] - s['k']} failures)")
        if s["avg_out"] > 2.5 * b["avg_out"]: why.append(f"emits {s['avg_out'] / max(b['avg_out'], 1):.1f}× more output tokens (reasoning/verbosity overhead)")
        if s["p50"] > 2 * b["p50"]: why.append(f"median latency {s['p50']:.1f}s vs {b['p50']:.1f}s")
        ins.append(f"**{m}**: " + "; ".join(why) + ".")
    # routing policy: cheapest cost-per-solve per category among models meeting threshold
    route, blended, blended_base = {}, 0.0, 0.0
    for c in cats:
        ok = [m for m in models if S[m]["by_cat"][c]["rate"] >= threshold and S[m]["by_cat"][c]["cps"]]
        pick = min(ok, key=lambda m: S[m]["by_cat"][c]["cps"]) if ok else max(models, key=lambda m: S[m]["by_cat"][c]["rate"])
        route[c] = dict(model=pick, rate=S[pick]["by_cat"][c]["rate"], cps=S[pick]["by_cat"][c]["cps"], met=bool(ok))
        blended += S[pick]["by_cat"][c]["cost"] / max(S[pick]["by_cat"][c]["n"], 1)
        blended_base += S[best]["by_cat"][c]["cost"] / max(S[best]["by_cat"][c]["n"], 1)
    save = 1 - blended / blended_base if blended_base else 0
    return dict(S=S, cats=cats, models=models, best=best, insights=ins, route=route, route_saving=save, threshold=threshold)

# ---------------------------------------------------------------- output
def md(s): return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(s, quote=False))
def money(x): return "–" if x is None else f"${x:.5f}"

def heat(rate):
    return f"background:hsl({int(rate * 120)},55%,85%)"

def render_html(res, A):
    S, M, C = A["S"], A["models"], A["cats"]
    sim = '<div class="sim">SIMULATED DATA — produced by a deterministic fake backend to demo the tool. Not a measurement of any real model.</div>' if res["simulated"] else ""
    h = [f"""<!doctype html><meta charset=utf-8><title>modelbench report</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e3e3e3;--card:#fafafa}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16181b;--fg:#e8e8e8;--mut:#9a9a9a;--line:#2e3136;--card:#1d2024}}}}
body{{font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:1000px;margin:0 auto;padding:24px 16px}}
h1{{margin:0 0 4px}}h2{{margin-top:36px;border-bottom:1px solid var(--line);padding-bottom:6px}}.mut{{color:var(--mut)}}
table{{border-collapse:collapse;width:100%;font-size:14px;display:block;overflow-x:auto}}th,td{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}}.sim{{background:#fff3cd;color:#664d03;padding:8px 12px;border-radius:6px;margin:12px 0;font-weight:600}}
.cell{{color:#111}}ul{{padding-left:20px}}li{{margin:6px 0}}.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px}}</style>
<h1>Which model, and why</h1><div class="mut">{res['created']} · {len(res['tasks'])} tasks × {res['trials']} trials × {len(M)} models · cost = tokens × configured price</div>{sim}
<h2>Verdict</h2><div class="card"><ul>{''.join('<li>'+md(i)+'</li>' for i in A['insights'])}</ul></div>
<h2>Scoreboard</h2><table><tr><th>model</th><th>pass rate (95% CI)</th><th>$/run</th><th>$/solved task</th><th>p50 latency</th><th>p95</th><th>avg out tokens</th><th>failure modes</th></tr>"""]
    for m in M:
        s = S[m]
        fm = ", ".join(f"{k.replace('_',' ')} {v}" for k, v in sorted(s["fails"].items(), key=lambda x: -x[1])) or "–"
        h.append(f"<tr><td><b>{escape(m)}</b></td><td>{s['rate']:.0%} ({s['ci'][0]:.0%}–{s['ci'][1]:.0%})</td><td>{money(s['cost_run'])}</td><td>{money(s['cps'])}</td>"
                 f"<td>{s['p50']:.1f}s</td><td>{s['p95']:.1f}s</td><td>{s['avg_out']:.0f}</td><td style='text-align:left;white-space:normal'>{fm}</td></tr>")
    h.append("</table><h2>Where each model is strong or weak</h2><p class=mut>Pass rate by task category. Few trials per cell means wide intervals — read colour as a hint, not a verdict.</p><table><tr><th>category</th>"
             + "".join(f"<th>{escape(m)}</th>" for m in M) + "</tr>")
    for c in C:
        h.append(f"<tr><td>{escape(c)}</td>" + "".join(
            f"<td class=cell style='{heat(S[m]['by_cat'][c]['rate'])}'>{S[m]['by_cat'][c]['rate']:.0%} <span style='opacity:.6'>({S[m]['by_cat'][c]['k']}/{S[m]['by_cat'][c]['n']})</span></td>" for m in M) + "</tr>")
    h.append(f"</table><h2>Suggested routing policy</h2><p class=mut>Per category, the cheapest model per solved task that reaches ≥{A['threshold']:.0%} pass rate; if none does, the most accurate one (flagged).</p><table><tr><th>category</th><th>route to</th><th>pass rate</th><th>$/solved</th></tr>")
    for c, r in A["route"].items():
        h.append(f"<tr><td>{escape(c)}</td><td>{escape(r['model'])}{'' if r['met'] else ' ⚠ below threshold'}</td><td>{r['rate']:.0%}</td><td>{money(r['cps'])}</td></tr>")
    h.append(f"</table><p>Blended cost per run vs always using <b>{escape(A['best'])}</b>: <b>{A['route_saving']:.0%} lower</b> (assuming an even task mix — weight by your real traffic).</p>")
    h.append("""<h2>How to read this (and where it can mislead)</h2><div class="card"><ul>
<li><b>Cost per solved task</b> = total spend ÷ passes. It charges a cheap model for its failures, and charges a reasoning model for its hidden thinking tokens.</li>
<li><b>Intervals are Wilson 95%.</b> With 8 tasks × few trials they are wide; overlapping intervals mean “can't tell yet”, not “equal”.</li>
<li><b>Pass = hidden unit tests</b>, single attempt. It ignores code quality, review effort, and multi-step agent behaviour; rerun on the customer's own repos and success criteria before drawing conclusions.</li>
<li><b>Latency</b> includes network and queueing at the time of the run.</li></ul></div>""")
    return "".join(h)

def print_summary(A):
    S = A["S"]
    print(f"\n{'model':<16}{'pass':>7}{'$/run':>10}{'$/solved':>11}{'p50':>7}{'out tok':>9}")
    for m in A["models"]:
        s = S[m]; print(f"{m:<16}{s['rate']:>7.0%}{s['cost_run']:>10.5f}{(s['cps'] or 0):>11.5f}{s['p50']:>6.1f}s{s['avg_out']:>9.0f}")
    print()
    for i in A["insights"]: print(" •", i.replace("**", ""))
    print(f"\n routing policy saves {A['route_saving']:.0%} vs always-{A['best']}")

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--models", default="models.json"); r.add_argument("--tasks", default="tasks/sample.json")
    r.add_argument("--trials", type=int, default=5); r.add_argument("--simulate", action="store_true"); r.add_argument("--workers", type=int, default=8)
    r.add_argument("--out", default="results.json"); r.add_argument("--html", default="report.html"); r.add_argument("--threshold", type=float, default=0.8)
    p = sub.add_parser("report"); p.add_argument("results"); p.add_argument("--html", default="report.html"); p.add_argument("--threshold", type=float, default=0.8)
    a = ap.parse_args()
    if a.cmd == "run":
        models = json.load(open(a.models))["models"]; tasks = json.load(open(a.tasks))
        res = run(models, tasks, a.trials, a.simulate, a.workers)
        json.dump(res, open(a.out, "w"), indent=1)
    else:
        res = json.load(open(a.results))
    A = analyze(res, a.threshold)
    open(a.html, "w").write(render_html(res, A))
    print_summary(A); print(f"\nreport: {a.html}")

if __name__ == "__main__":
    main()
