#!/usr/bin/env python3
"""Local web UI: pick an industry -> generate scenario -> run models -> quality / cost / speed.

  python3 server.py --simulate                 # demo with fake models (labelled SIMULATED)
  python3 server.py --models models.json       # real run; needs "generator" and "judge" entries
"""
import argparse, base64, hmac, json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import industry, modelbench

HERE = os.path.dirname(os.path.abspath(__file__))

def make_handler(cfg, simulate):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def _authed(self):
            pw = os.environ.get("APP_PASSWORD")
            if not pw: return True
            h = self.headers.get("Authorization", "")
            try: given = base64.b64decode(h.split(" ", 1)[1]).decode().split(":", 1)[1] if h.startswith("Basic ") else ""
            except Exception: given = ""
            if hmac.compare_digest(given, pw): return True
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="modelbench"'); self.send_header("Content-Length", "0"); self.end_headers()
            return False
        def do_GET(self):
            if not self._authed(): return
            if self.path == "/api/config":
                return self._send(200, dict(simulated=simulate, industries=industry.INDUSTRIES, models=[m["name"] for m in cfg["models"]]))
            if self.path in ("/", "/index.html"):
                return self._send(200, open(os.path.join(HERE, "ui.html"), "rb").read(), "text/html; charset=utf-8")
            self._send(404, {"error": "not found"})
        def do_POST(self):
            if not self._authed(): return
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
    ap.add_argument("--models"); ap.add_argument("--simulate", action="store_true"); ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    a = ap.parse_args()
    a.simulate = a.simulate or os.environ.get("SIMULATE") == "1"
    # Deploy-friendly: models config can come from the MODELS_JSON env var (keeps keys/prices out of git).
    cfg = json.loads(os.environ["MODELS_JSON"]) if os.environ.get("MODELS_JSON") and not a.simulate else \
          json.load(open(a.models or ("models.sim.json" if a.simulate else "models.json")))
    if not a.simulate and not ("generator" in cfg and "judge" in cfg):
        raise SystemExit('models file needs "generator" and "judge" entries (see models.example.json)')
    if not a.simulate: modelbench.fill_prices(cfg)
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    if host == "0.0.0.0" and not a.simulate and not os.environ.get("APP_PASSWORD"):
        raise SystemExit("Refusing to serve live (paid) mode publicly without APP_PASSWORD set")
    print(f"http://{host}:{a.port}  ({'SIMULATED' if a.simulate else 'live'})")
    ThreadingHTTPServer((host, a.port), make_handler(cfg, a.simulate)).serve_forever()
