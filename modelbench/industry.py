"""Industry scenario benchmark: Claude (generator) writes a use case + rubric, candidates answer, a judge scores.

Quality = rubric-weighted judge score (0-100). Cost = tokens x configured price. Speed = wall-clock latency.
"""
import json, random, re, statistics
from concurrent.futures import ThreadPoolExecutor
import modelbench as mb

INDUSTRIES = {
    "Banking & Fintech": "payments reconciliation, ledgers, PSD2/SCA, fraud rules, rounding and currency handling",
    "Healthcare": "HL7/FHIR messages, patient identifiers, PII redaction, GDPR and audit trails",
    "Insurance": "claims processing, policy rules engines, actuarial tables, regulatory reporting",
    "E-commerce & Retail": "pricing and promotions engines, inventory sync, checkout, returns",
    "Telecom": "CDR/billing pipelines, network inventory, provisioning APIs, SLA reporting",
    "Manufacturing & IoT": "sensor telemetry, predictive maintenance, PLC/OPC-UA gateways, batch traceability",
    "Public Sector": "citizen services, legacy COBOL/mainframe migration, accessibility, data residency",
    "Cloud & Platform Engineering": "Kubernetes manifests, Terraform, CI/CD, cost optimisation, incident runbooks",
}

GEN_SYSTEM = "You design realistic, discriminating evaluations for AI coding assistants. Reply with a single JSON object and nothing else."
GEN_PROMPT = """Design ONE evaluation task for an AI coding assistant in the industry: {industry} (typical work: {focus}).
{extra}
The task must be something a staff engineer at a real company in this industry would hand to a coding agent: concrete, self-contained, with
hidden pitfalls (edge cases, compliance or domain rules) that separate strong models from weak ones. It must be answerable in one reply (<= ~60 lines of code plus brief explanation).

Return JSON: {{"title": str, "prompt": str (the full task text given to the assistant, including all needed context),
"rubric": [4 to 5 items {{"name": str, "weight": number, "description": str (what a 5/5 vs 1/5 answer looks like, concretely)}}]}}
Rubric must include: correctness of the core logic, handling of the domain-specific pitfall(s), and code quality/maintainability. Weights sum to 1."""

JUDGE_SYSTEM = "You are a strict, impartial senior reviewer. You never reward length or confident tone, only correctness and fit to the rubric. Reply with a single JSON object and nothing else."
JUDGE_PROMPT = """TASK GIVEN TO THE ASSISTANT:
{task}

RUBRIC (score each criterion 1-5; 5 = fully meets, 1 = fails):
{rubric}

ASSISTANT RESPONSE:
{response}

Return JSON: {{"scores": {{<criterion name>: integer 1-5, ...}}, "reasons": {{<criterion name>: one short sentence naming the concrete flaw or strength}}}}"""

def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m: raise ValueError("no JSON in model output")
    return json.loads(m.group(0))

# ---------------------------------------------------------------- simulated backends (clearly labelled in the UI)
def _sim_skill(m, industry):
    return m["sim"]["skill"].get(industry, m["sim"]["skill"]["default"])

def sim_scenario(industry, focus):
    return dict(
        title=f"{industry}: edge-case-heavy rules module (simulated scenario)",
        prompt=f"You maintain a platform in {industry} ({focus}). Write a Python module that applies a set of domain rules to incoming records, "
               "rejects malformed ones with an auditable reason, and never silently drops data. List the edge cases you handled.",
        rubric=[dict(name="Core logic correctness", weight=.35, description="Rules applied correctly on normal input"),
                dict(name="Domain pitfalls", weight=.30, description="Handles the regulatory/rounding/identifier edge cases"),
                dict(name="Auditability", weight=.20, description="Every rejection carries a traceable reason"),
                dict(name="Code quality", weight=.15, description="Readable, typed, testable")])

def sim_answer(m, scenario, trial):
    rng = random.Random(f"{m['name']}|{scenario['title']}|{trial}")
    tout = int(380 * m["sim"].get("verbosity", 1) * rng.uniform(.8, 1.3)) + m["sim"].get("reasoning_tokens", 0)
    return dict(text=f"[simulated response from {m['name']}]", finish="stop", tin=len(scenario["prompt"]) // 4 + 40, tout=tout,
                latency=m["sim"]["ttft"] + tout / m["sim"]["tps"] * rng.uniform(.85, 1.25))

def sim_judge(m, scenario, trial, industry):
    rng = random.Random(f"judge|{m['name']}|{trial}|{industry}")
    base = _sim_skill(m, industry)
    scores, reasons = {}, {}
    for c in scenario["rubric"]:
        v = max(1, min(5, round(1 + 4 * base + rng.uniform(-1.1, .9))))
        scores[c["name"]] = v
        reasons[c["name"]] = "(simulated judge)"
    return dict(scores=scores, reasons=reasons)

# ---------------------------------------------------------------- real backends
def generate_scenario(cfg, industry, custom_focus="", simulate=False):
    focus = custom_focus or INDUSTRIES.get(industry, "")
    if simulate:
        return dict(industry=industry, **sim_scenario(industry, focus))
    extra = f"Extra steer from the evaluator: {custom_focus}" if custom_focus and industry in INDUSTRIES else ""
    gen = {**cfg["generator"], "temperature": 0.7, "max_tokens": 3000}
    r = mb.call_openai_compat(gen, GEN_PROMPT.format(industry=industry, focus=focus, extra=extra), system=GEN_SYSTEM)
    d = parse_json(r["text"])
    tot = sum(c["weight"] for c in d["rubric"]) or 1
    for c in d["rubric"]: c["weight"] /= tot
    return dict(industry=industry, title=d["title"], prompt=d["prompt"], rubric=d["rubric"])

def judge_response(cfg, scenario, response_text):
    rub = "\n".join(f"- {c['name']} (weight {c['weight']:.2f}): {c['description']}" for c in scenario["rubric"])
    j = {**cfg["judge"], "temperature": 0, "max_tokens": 1200}
    r = mb.call_openai_compat(j, JUDGE_PROMPT.format(task=scenario["prompt"], rubric=rub, response=response_text), system=JUDGE_SYSTEM)
    return parse_json(r["text"])

# ---------------------------------------------------------------- run + analysis
def pareto(rows):
    for a in rows:
        a["dominated"] = any(b is not a and b["quality"] >= a["quality"] and b["cost"] <= a["cost"] and b["latency"] <= a["latency"]
                             and (b["quality"] > a["quality"] or b["cost"] < a["cost"] or b["latency"] < a["latency"]) for b in rows)

def verdict(rows, simulate):
    ok = [r for r in rows if r["quality"] is not None]
    if not ok: return ["No model produced a scoreable answer."]
    best = max(ok, key=lambda r: r["quality"])
    out = [f"**{best['name']}** scores highest on quality ({best['quality']:.0f}/100)."]
    near = [r for r in ok if r["quality"] >= best["quality"] - 5 and r["cost"] < best["cost"]]
    if near:
        v = min(near, key=lambda r: r["cost"])
        out.append(f"**{v['name']}** is within {best['quality'] - v['quality']:.0f} points of {best['name']} at {best['cost'] / max(v['cost'], 1e-12):.1f}× lower cost per run — the best value if that gap is acceptable.")
    fast = min(ok, key=lambda r: r["latency"])
    if fast["name"] != best["name"]:
        out.append(f"Fastest is **{fast['name']}** ({fast['latency']:.1f}s vs {best['latency']:.1f}s) at quality {fast['quality']:.0f} — worth it for interactive/IDE use, less so for batch agents.")
    for r in ok:
        if r["dominated"]: out.append(f"**{r['name']}** is dominated: another model is at least as good on quality, cost and speed.")
    out.append("One scenario × few trials is a signal, not a verdict: re-run on the prospect's own tasks and read the judge's reasons before quoting numbers."
               + (" (SIMULATED data.)" if simulate else ""))
    return out

def evaluate(cfg, scenario, trials=2, simulate=False, workers=8):
    models = cfg["models"]
    judge_is_candidate = (not simulate) and any(m.get("model") == cfg["judge"].get("model") for m in models)
    def one(job):
        m, t = job
        try:
            r = sim_answer(m, scenario, t) if simulate else mb.call_openai_compat(m, scenario["prompt"], system="You are an expert engineer. Answer the task directly and completely.")
            if r["finish"] == "length": r["text"] += "\n[TRUNCATED]"
            j = sim_judge(m, scenario, t, scenario["industry"]) if simulate else judge_response(cfg, scenario, r["text"])
            sc = {c["name"]: max(1, min(5, int(j["scores"].get(c["name"], 1)))) for c in scenario["rubric"]}
            q = sum(sc[c["name"]] * c["weight"] for c in scenario["rubric"]) / 5 * 100
            return dict(m=m, ok=True, q=q, scores=sc, reasons=j.get("reasons", {}), cost=mb.cost(m, r), latency=r["latency"], tout=r["tout"], text=r["text"])
        except Exception as e:
            return dict(m=m, ok=False, err=str(e)[:200])
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(one, [(m, t) for m in models for t in range(trials)]))
    rows = []
    for m in models:
        rs = [r for r in res if r["m"] is m]; good = [r for r in rs if r["ok"]]
        if not good:
            rows.append(dict(name=m["name"], quality=None, cost=0, latency=0, error=rs[0].get("err"))); continue
        crit = {c["name"]: statistics.mean(r["scores"][c["name"]] for r in good) for c in scenario["rubric"]}
        rows.append(dict(name=m["name"], quality=statistics.mean(r["q"] for r in good),
                         q_spread=(max(r["q"] for r in good) - min(r["q"] for r in good)), n=len(good), failed=len(rs) - len(good),
                         cost=statistics.mean(r["cost"] for r in good), latency=statistics.median(r["latency"] for r in good),
                         tout=statistics.mean(r["tout"] for r in good), criteria=crit, reasons=good[0]["reasons"], sample=good[0]["text"][:2500],
                         cost_per_quality_pt=(statistics.mean(r["cost"] for r in good) / max(statistics.mean(r["q"] for r in good), 1e-9))))
    scored = [r for r in rows if r["quality"] is not None]
    pareto(scored)
    return dict(scenario=scenario, rows=rows, verdict=verdict(scored, simulate), simulated=simulate, trials=trials,
                warnings=(["The judge model is also a candidate — judges tend to favour their own style. Use a different judge."] if judge_is_candidate else []))
