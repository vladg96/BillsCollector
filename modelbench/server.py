#!/usr/bin/env python3
"""Local web UI: pick an industry -> generate scenario -> run models -> quality / cost / speed.

  python3 server.py --simulate                 # demo with fake models (labelled SIMULATED)
  python3 server.py --models models.json       # real run; needs "generator" and "judge" entries
"""
import argparse, json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import industry

HERE = os.path.dirname(os.path.abspath(__file__))

def make_handler(cfg, simulate):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path == "/api/config":
                return self._send(200, dict(simulated=simulate, industries=industry.INDUSTRIES, models=[m["name"] for m in cfg["models"]]))
            if self.path in ("/", "/index.html"):
                return self._send(200, open(os.path.join(HERE, "ui.html"), "rb").read(), "text/html; charset=utf-8")
            self._send(404, {"error": "not found"})
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path == "/api/generate":
                    return self._send(200, industry.generate_scenario(cfg, body["industry"], body.get("focus", ""), simulate))
                if self.path == "/api/run":
                    return self._send(200, industry.evaluate(cfg, body["scenario"], int(body.get("trials", 2)), simulate))
                self._send(404, {"error": "not found"})
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
        def log_message(self, *a): pass
    return H

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--models"); ap.add_argument("--simulate", action="store_true"); ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    cfg = json.load(open(a.models or ("models.sim.json" if a.simulate else "models.json")))
    if not a.simulate and not ("generator" in cfg and "judge" in cfg):
        raise SystemExit('models file needs "generator" and "judge" entries (see models.example.json)')
    print(f"http://127.0.0.1:{a.port}  ({'SIMULATED' if a.simulate else 'live'})")
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(cfg, a.simulate)).serve_forever()
