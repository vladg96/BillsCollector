"""Sample task suite. `python3 sample_tasks.py` regenerates tasks/sample.json.

Each task: prompt sent to the model, `tests` appended to the model's code and run
in a subprocess. `reference`/`broken` are only used by the simulated backend.
"""
import json

T = []

def task(id, category, prompt, tests, reference, broken):
    T.append(dict(id=id, category=category, prompt=prompt.strip(), tests=tests.strip(),
                  reference=reference.strip(), broken=broken.strip()))

task("moving_avg_offbyone", "bugfix",
"""Fix the bug. Return the full corrected function in a python code block.

def moving_average(xs, k):
    # average of each window of k consecutive items
    return [sum(xs[i:i+k]) / k for i in range(len(xs) - k)]
""",
"""assert moving_average([1,2,3,4], 2) == [1.5, 2.5, 3.5]
assert moving_average([5], 1) == [5.0]
assert moving_average([1,2], 3) == []""",
"def moving_average(xs, k):\n    return [sum(xs[i:i+k]) / k for i in range(len(xs) - k + 1)]",
"def moving_average(xs, k):\n    return [sum(xs[i:i+k]) / k for i in range(len(xs) - k)]")

task("dedupe_order", "bugfix",
"""Fix the bug (must preserve first-seen order, must not mutate input, must work for unhashable items like lists).
Return the full function in a python code block.

def dedupe(items):
    return list(set(items))
""",
"""assert dedupe([3,1,3,2,1]) == [3,1,2]
x=[[1],[2],[1]]; assert dedupe(x) == [[1],[2]] and len(x)==3""",
"def dedupe(items):\n    out = []\n    for i in items:\n        if i not in out:\n            out.append(i)\n    return out",
"def dedupe(items):\n    seen = set()\n    return [i for i in items if not (i in seen or seen.add(i))]")

task("merge_intervals", "algorithm",
"Write `merge_intervals(intervals)` taking a list of [start, end] pairs (unsorted, may touch or overlap; touching intervals merge) and returning merged sorted list of [start, end] lists. Python code block only.",
"""assert merge_intervals([[8,10],[1,3],[2,6]]) == [[1,6],[8,10]]
assert merge_intervals([[1,2],[2,3]]) == [[1,3]]
assert merge_intervals([]) == []
assert merge_intervals([[1,10],[2,3]]) == [[1,10]]""",
"def merge_intervals(intervals):\n    out = []\n    for s, e in sorted(intervals):\n        if out and s <= out[-1][1]:\n            out[-1][1] = max(out[-1][1], e)\n        else:\n            out.append([s, e])\n    return out",
"def merge_intervals(intervals):\n    out = []\n    for s, e in sorted(intervals):\n        if out and s < out[-1][1]:\n            out[-1][1] = e\n        else:\n            out.append([s, e])\n    return out")

task("lru_cache", "algorithm",
"Implement class `LRU` with `__init__(capacity)`, `get(key)` (returns None if missing, marks as recently used) and `put(key, value)` (evicts least recently used when over capacity). O(1) operations. Python code block only.",
"""c = LRU(2); c.put(1,1); c.put(2,2); assert c.get(1) == 1
c.put(3,3); assert c.get(2) is None
c.put(4,4); assert c.get(1) is None and c.get(3) == 3 and c.get(4) == 4
c.put(3,30); assert c.get(3) == 30""",
"from collections import OrderedDict\nclass LRU:\n    def __init__(self, capacity):\n        self.c = capacity; self.d = OrderedDict()\n    def get(self, k):\n        if k not in self.d: return None\n        self.d.move_to_end(k); return self.d[k]\n    def put(self, k, v):\n        self.d[k] = v; self.d.move_to_end(k)\n        if len(self.d) > self.c: self.d.popitem(last=False)",
"from collections import OrderedDict\nclass LRU:\n    def __init__(self, capacity):\n        self.c = capacity; self.d = OrderedDict()\n    def get(self, k):\n        return self.d.get(k)\n    def put(self, k, v):\n        self.d[k] = v\n        if len(self.d) > self.c: self.d.popitem(last=False)")

task("parse_kv", "refactor",
"""Rewrite as a pure function `parse_kv(s)` that behaves identically on valid input but ALSO strips whitespace around keys/values and ignores empty segments (e.g. 'a=1;;b = 2;'). Values stay strings. Python code block only.

def parse_kv(s):
    d = {}
    for part in s.split(';'):
        k, v = part.split('=')
        d[k] = v
    return d
""",
"""assert parse_kv("a=1;b=2") == {"a":"1","b":"2"}
assert parse_kv("a=1;;b = 2;") == {"a":"1","b":"2"}
assert parse_kv("") == {}""",
"def parse_kv(s):\n    d = {}\n    for part in s.split(';'):\n        if not part.strip(): continue\n        k, v = part.split('=', 1)\n        d[k.strip()] = v.strip()\n    return d",
"def parse_kv(s):\n    d = {}\n    for part in s.split(';'):\n        k, v = part.split('=')\n        d[k.strip()] = v.strip()\n    return d")

task("fix_client_config", "config-debug",
"""A customer's OpenAI-compatible client can't connect. Their config:

cfg = {"base_url": "https://gateway.acme.eu/v1/", "api_key": "sk-123", "headers": {"authorization": "sk-123"}, "model": "gpt-4o"}

Gateway docs: base_url must NOT end with '/' ; auth is header `Authorization: Bearer <key>` (exact casing); the api_key field must be removed since the header is used; model must be 'acme-coder-large'.
Write `fixed_config()` returning the corrected dict. Python code block only.""",
"""c = fixed_config()
assert c["base_url"] == "https://gateway.acme.eu/v1"
assert c["headers"] == {"Authorization": "Bearer sk-123"}
assert "api_key" not in c and c["model"] == "acme-coder-large\"""".replace('\\"', '"'),
"def fixed_config():\n    return {'base_url': 'https://gateway.acme.eu/v1', 'headers': {'Authorization': 'Bearer sk-123'}, 'model': 'acme-coder-large'}",
"def fixed_config():\n    return {'base_url': 'https://gateway.acme.eu/v1', 'api_key': 'sk-123', 'headers': {'Authorization': 'sk-123'}, 'model': 'acme-coder-large'}")

task("truncate_context", "context-mgmt",
"""Write `truncate_to_budget(messages, budget, count)`. `messages` is a list of {"role","content"}; `count(msg)` returns its token cost. Keep ALL system messages, then keep as many of the NEWEST non-system messages as fit in the remaining budget (contiguous from the end, never skipping a message to fit an older one), preserving original order. Python code block only.""",
"""cnt = lambda m: len(m["content"])
ms = [{"role":"system","content":"sss"},{"role":"user","content":"aaaa"},{"role":"assistant","content":"bb"},{"role":"user","content":"c"}]
assert truncate_to_budget(ms, 6, cnt) == [ms[0], ms[2], ms[3]]
assert truncate_to_budget(ms, 100, cnt) == ms
assert truncate_to_budget(ms, 3, cnt) == [ms[0]]
m2 = [{"role":"system","content":"s"},{"role":"user","content":"a"},{"role":"user","content":"bbbbb"}]
assert truncate_to_budget(m2, 3, cnt) == [m2[0]]""",
"def truncate_to_budget(messages, budget, count):\n    sys_ = [m for m in messages if m['role'] == 'system']\n    left = budget - sum(count(m) for m in sys_)\n    keep = set()\n    for i in range(len(messages) - 1, -1, -1):\n        m = messages[i]\n        if m['role'] == 'system': continue\n        if count(m) > left: break\n        left -= count(m); keep.add(i)\n    return [m for i, m in enumerate(messages) if m['role'] == 'system' or i in keep]",
"def truncate_to_budget(messages, budget, count):\n    sys_ = [m for m in messages if m['role'] == 'system']\n    left = budget - sum(count(m) for m in sys_)\n    keep = set()\n    for i in range(len(messages) - 1, -1, -1):\n        m = messages[i]\n        if m['role'] == 'system': continue\n        if count(m) <= left:\n            left -= count(m); keep.add(i)\n    return [m for i, m in enumerate(messages) if m['role'] == 'system' or i in keep]")

task("cost_with_cache", "token-economics",
"""Write `cost_usd(usage, price)`. usage: {"input": int, "cached_input": int, "output": int} where `cached_input` is a SUBSET of `input` (already counted in it). price: per-1M-token dict {"input": x, "cached_input": y, "output": z}. Cached tokens are billed at the cached rate, the remaining input at the normal rate. Return a float. Python code block only.""",
"""p = {"input": 2.0, "cached_input": 0.5, "output": 10.0}
assert abs(cost_usd({"input": 1_000_000, "cached_input": 0, "output": 0}, p) - 2.0) < 1e-9
assert abs(cost_usd({"input": 1_000_000, "cached_input": 1_000_000, "output": 0}, p) - 0.5) < 1e-9
assert abs(cost_usd({"input": 1_000_000, "cached_input": 400_000, "output": 100_000}, p) - (0.6*2.0 + 0.4*0.5 + 0.1*10.0)) < 1e-9""",
"def cost_usd(usage, price):\n    fresh = usage['input'] - usage['cached_input']\n    return (fresh * price['input'] + usage['cached_input'] * price['cached_input'] + usage['output'] * price['output']) / 1e6",
"def cost_usd(usage, price):\n    return (usage['input'] * price['input'] + usage['cached_input'] * price['cached_input'] + usage['output'] * price['output']) / 1e6")

if __name__ == "__main__":
    json.dump(T, open("tasks/sample.json", "w"), indent=2)
    print(f"wrote {len(T)} tasks")
