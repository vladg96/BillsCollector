"""The local app: a small web UI on http://127.0.0.1 for setup and the monthly review."""

from __future__ import annotations

import secrets as stdsecrets
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from flask import (Flask, abort, flash, redirect, render_template, request, send_file,
                   send_from_directory, url_for)

from .. import config, deliver, paths, runner, vault
from ..models import Period, slug
from ..sources import revolut

# Connection kinds offered in the UI. Mailboxes use IMAP with an app password.
PRESETS: dict[str, dict[str, Any]] = {
    "applemail": {"title": "Apple Mail on this Mac", "type": "applemail",
                  "blurb": "Reads every account already set up in Mail.app. No password needed."},
    "gmail": {"title": "Gmail", "type": "imap", "host": "imap.gmail.com",
              "folders": ["[Gmail]/All Mail"],
              "app_password_url": "https://myaccount.google.com/apppasswords",
              "blurb": "Needs 2-Step Verification on, then create an app password."},
    "yahoo": {"title": "Yahoo Mail", "type": "imap", "host": "imap.mail.yahoo.com",
              "folders": ["INBOX"],
              "app_password_url": "https://login.yahoo.com/myaccount/security/app-password",
              "blurb": "Account security -> Generate app password."},
    "outlook": {"title": "Outlook / Microsoft 365", "type": "imap",
                "host": "outlook.office365.com", "folders": ["INBOX"],
                "app_password_url": "https://account.live.com/proofs/AppPassword",
                "blurb": "Personal Outlook.com accounts need an app password."},
    "icloud": {"title": "iCloud Mail", "type": "imap", "host": "imap.mail.me.com",
               "folders": ["INBOX"], "app_password_url": "https://account.apple.com",
               "blurb": "Sign-In and Security -> App-Specific Passwords."},
    "imap": {"title": "Other mailbox (IMAP)", "type": "imap", "host": "", "folders": ["INBOX"],
             "blurb": "Any provider that supports IMAP."},
}

REVOLUT_STATEMENTS_URL = "https://business.revolut.com/"


def _csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def create_app(config_path: Path, invoices_root: Path | None = None, port: int = 8765) -> Flask:
    app = Flask(__name__)
    app.secret_key = stdsecrets.token_hex(32)
    csrf_token = stdsecrets.token_urlsafe(32)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    root = Path(invoices_root) if invoices_root else paths.invoices_dir()
    test_results: dict[str, tuple[bool, str]] = {}

    def load() -> dict[str, Any]:
        return config.load(config_path)

    def store(cfg: dict[str, Any]) -> None:
        config.save(cfg, config_path)

    def month_of(ym: str):
        try:
            period = Period.parse(ym)
        except ValueError:
            abort(404)
        from ..month import Month
        return period, Month(root, period)

    @app.before_request
    def guard() -> None:
        # Only answer to our own address (stops DNS-rebinding), and only accept
        # forms we rendered (stops other websites posting to localhost).
        if not app.testing and request.host not in allowed_hosts:
            abort(403)
        if request.method == "POST" and request.form.get("csrf") != csrf_token:
            abort(403)

    @app.context_processor
    def globals_() -> dict[str, Any]:
        return {"csrf": csrf_token, "is_mac": sys.platform == "darwin"}

    # ---- month review -------------------------------------------------------
    @app.get("/")
    def home():
        cfg = load()
        if not cfg["sources"]:
            return redirect(url_for("connections"))
        return redirect(url_for("month", ym=str(Period.previous())))

    @app.get("/month/<ym>")
    def month(ym: str):
        period, m = month_of(ym)
        cfg = load()
        prev_p = Period.previous(period.start)
        next_p = Period(period.year + period.month // 12, period.month % 12 + 1)
        d = cfg.get("delivery") or {}
        return render_template(
            "month.html", period=period, m=m, entries=m.entries(), run=m.data["run"],
            missing_vendors=m.missing_vendors(), missing_items=m.missing_items(cfg["manual"]),
            manual=cfg["manual"], complete=m.complete(cfg["manual"]), delivery=d,
            prev=str(prev_p), next=str(next_p), vendors=[v["vendor"] for v in cfg["vendors"]],
            has_sources=bool(cfg["sources"]),
            payments=m.unmatched_payments(cfg["vendors"], cfg.get("ignore_merchants") or []),
        )

    @app.post("/month/<ym>/collect")
    def collect(ym: str):
        period, _ = month_of(ym)
        m = runner.run_month(load(), period, root)
        run = m.data["run"]
        flash(f"Collected {run['found']} file(s) for {period.label}."
              + (f" {len(run['errors'])} connection(s) failed." if run["errors"] else ""),
              "ok" if not run["errors"] else "warn")
        return redirect(url_for("month", ym=ym))

    @app.post("/month/<ym>/upload")
    def upload(ym: str):
        _, m = month_of(ym)
        cfg = load()
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a file first.", "warn")
            return redirect(url_for("month", ym=ym))
        item = next((i for i in cfg["manual"] if i["id"] == request.form.get("item")), None)
        vendor = item["vendor"] if item else (request.form.get("vendor") or "").strip() or "Other"
        name = m.add_file(f.filename, f.read(), vendor, item)
        flash(f"Added {name}.", "ok")
        return redirect(url_for("month", ym=ym))

    @app.post("/month/<ym>/ignore-merchant")
    def ignore_merchant(ym: str):
        cfg = load()
        merchant = request.form.get("merchant", "").strip()
        if merchant:
            cfg["ignore_merchants"] = sorted(set(cfg.get("ignore_merchants") or []) | {merchant})
            store(cfg)
            flash(f"Won't ask about {merchant} again (undo in Settings → config file).", "ok")
        return redirect(url_for("month", ym=ym))

    @app.post("/month/<ym>/remove")
    def remove(ym: str):
        _, m = month_of(ym)
        m.remove(request.form["name"])
        return redirect(url_for("month", ym=ym))

    @app.get("/month/<ym>/file/<path:name>")
    def file(ym: str, name: str):
        _, m = month_of(ym)
        if name not in m.data["files"]:
            abort(404)
        return send_from_directory(m.folder, name)

    @app.get("/month/<ym>/zip")
    def download_zip(ym: str):
        _, m = month_of(ym)
        path = m.write_zip(load()["manual"])
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.post("/month/<ym>/open")
    def open_folder(ym: str):
        _, m = month_of(ym)
        subprocess.run(["open", str(m.folder)], check=False)
        return redirect(url_for("month", ym=ym))

    @app.post("/month/<ym>/send")
    def send(ym: str):
        period, m = month_of(ym)
        cfg = load()
        d = cfg.get("delivery") or {}
        if not d.get("to"):
            flash("Set your accountant's email in Settings first.", "warn")
            return redirect(url_for("settings"))
        files = [e.path for e in m.entries()]
        text = m.summary(cfg["manual"], owner_view=False)
        try:
            if d.get("method", "applemail") == "applemail":
                n = deliver.apple_mail_drafts(period, files, config.resolve(d, "delivery"), text)
                m.record_sent("applemail draft", str(d["to"]))
                flash(f"Opened {n} draft(s) in Apple Mail - check and hit Send.", "ok")
            else:
                s = deliver.smtp_settings(cfg)
                deliver.send_smtp(deliver.compose(period, files, s, True, text), s)
                m.record_sent("smtp", str(s["to"]))
                flash(f"Sent {len(files)} file(s) to {s['to']}.", "ok")
        except Exception as exc:  # noqa: BLE001 - show it in the UI
            flash(f"Sending failed: {exc}", "error")
        return redirect(url_for("month", ym=ym))

    # ---- connections --------------------------------------------------------
    @app.get("/connections")
    def connections():
        cfg = load()
        return render_template("connections.html", sources=cfg["sources"], presets=PRESETS,
                               results=test_results, vault_where=vault.where())

    @app.route("/connections/new/<kind>", methods=["GET", "POST"])
    @app.route("/connections/<sid>/edit", methods=["GET", "POST"], endpoint="edit_connection")
    def connection_form(kind: str | None = None, sid: str | None = None):
        cfg = load()
        existing = next((s for s in cfg["sources"] if s.get("id") == sid), None) if sid else None
        if sid and existing is None:
            abort(404)
        kind = existing.get("preset", existing["type"]) if existing else kind
        if kind not in PRESETS:
            abort(404)
        preset = PRESETS[kind]
        if request.method == "GET":
            return render_template("connection_form.html", kind=kind, preset=preset,
                                   src=existing or {"folders": preset.get("folders", [])})

        f = request.form
        src = dict(existing or {"type": preset["type"], "preset": kind})
        src["name"] = f.get("name", "").strip() or preset["title"]
        if preset["type"] == "imap":
            src["username"] = f["username"].strip()
            src["host"] = (f.get("host") or preset["host"]).strip()
            src["folders"] = _csv(f.get("folders")) or ["INBOX"]
            if not src["username"] or not src["host"]:
                flash("Email address and server are required.", "warn")
                return render_template("connection_form.html", kind=kind, preset=preset, src=src)
        else:
            src["path"] = f.get("path", "").strip() or None
            if not src["path"]:
                src.pop("path")
        src.setdefault("id", slug(f"{kind}-{src.get('username', '')}").lower().strip("-"))
        if not existing and any(s.get("id") == src["id"] for s in cfg["sources"]):
            src["id"] += f"-{stdsecrets.token_hex(2)}"
        if preset["type"] == "imap":
            secret = vault.secret_name(src["id"], "password")
            if f.get("password"):
                vault.set(secret, f["password"].replace(" ", ""))
            elif not vault.get(secret):
                flash("Paste the app password.", "warn")
                return render_template("connection_form.html", kind=kind, preset=preset, src=src)
            src["password"] = "${" + secret + "}"
        if existing:
            cfg["sources"] = [src if s.get("id") == sid else s for s in cfg["sources"]]
        else:
            cfg["sources"].append(src)
        store(cfg)
        test_results[src["id"]] = _test(cfg, src)
        flash(f"Saved {src['name']}. Test: {test_results[src['id']][1]}",
              "ok" if test_results[src["id"]][0] else "error")
        return redirect(url_for("connections"))

    @app.post("/connections/<sid>/test")
    def test_connection(sid: str):
        cfg = load()
        src = next((s for s in cfg["sources"] if s.get("id") == sid), None)
        if src is None:
            abort(404)
        test_results[sid] = _test(cfg, src)
        return redirect(url_for("connections"))

    @app.post("/connections/<sid>/toggle")
    def toggle_connection(sid: str):
        cfg = load()
        for s in cfg["sources"]:
            if s.get("id") == sid:
                s["enabled"] = not s.get("enabled", True)
        store(cfg)
        return redirect(url_for("connections"))

    @app.post("/connections/<sid>/delete")
    def delete_connection(sid: str):
        cfg = load()
        cfg["sources"] = [s for s in cfg["sources"] if s.get("id") != sid]
        store(cfg)
        for what in ("password", "private_key", "refresh_token"):
            vault.delete(vault.secret_name(sid, what))
        test_results.pop(sid, None)
        flash("Connection removed.", "ok")
        return redirect(url_for("connections"))

    # ---- Revolut Business (read-only API) ------------------------------------
    def revolut_src(cfg: dict[str, Any], sid: str) -> dict[str, Any]:
        src = next((s for s in cfg["sources"] if s.get("id") == sid and s["type"] == "revolut"), None)
        if src is None:
            abort(404)
        return src

    def cert_path(sid: str) -> Path:
        return paths.data_dir() / f"{sid}-publiccert.cer"

    @app.post("/connections/revolut/start")
    def revolut_start():
        cfg = load()
        existing = [s for s in cfg["sources"] if s.get("type") == "revolut"]
        sid = "revolut" if not existing else f"revolut-{len(existing) + 1}"
        key_pem, cert_pem = revolut.generate_keypair()
        vault.set(vault.secret_name(sid, "private_key"), key_pem)
        cert_path(sid).write_text(cert_pem)
        cfg["sources"].append({"id": sid, "type": "revolut", "name": "Revolut Business",
                               "redirect_uri": revolut.DEFAULT_REDIRECT,
                               "formats": ["pdf", "csv"]})
        store(cfg)
        return redirect(url_for("revolut_setup", sid=sid))

    @app.get("/connections/revolut/<sid>")
    def revolut_setup(sid: str):
        cfg = load()
        src = revolut_src(cfg, sid)
        cert = cert_path(sid).read_text() if cert_path(sid).exists() else ""
        connected = bool(vault.get(vault.secret_name(sid, "refresh_token")))
        consent = (revolut.consent_url(src["client_id"], src["redirect_uri"])
                   if src.get("client_id") else None)
        return render_template("revolut.html", src=src, cert=cert, connected=connected,
                               consent=consent, result=test_results.get(sid))

    @app.post("/connections/revolut/<sid>/client")
    def revolut_client(sid: str):
        cfg = load()
        src = revolut_src(cfg, sid)
        src["client_id"] = request.form.get("client_id", "").strip()
        src["redirect_uri"] = request.form.get("redirect_uri", "").strip() or revolut.DEFAULT_REDIRECT
        store(cfg)
        return redirect(url_for("revolut_setup", sid=sid) + "#authorize")

    @app.post("/connections/revolut/<sid>/code")
    def revolut_code(sid: str):
        cfg = load()
        src = revolut_src(cfg, sid)
        try:
            code = revolut.code_from_redirect(request.form.get("redirect", ""))
            revolut.RevolutClient(sid, src["client_id"], src["redirect_uri"]).exchange_code(code)
            test_results[sid] = _test(cfg, src)
            flash("Revolut connected (read-only). " + test_results[sid][1],
                  "ok" if test_results[sid][0] else "warn")
        except Exception as exc:  # noqa: BLE001
            flash(f"Couldn't connect: {exc}", "error")
        return redirect(url_for("revolut_setup", sid=sid))

    @app.post("/connections/revolut/<sid>/options")
    def revolut_options(sid: str):
        cfg = load()
        src = revolut_src(cfg, sid)
        src["formats"] = [f for f in ("pdf", "csv") if request.form.get(f"fmt_{f}")] or ["pdf"]
        currencies = _csv(request.form.get("currencies", "").upper())
        if currencies:
            src["currencies"] = currencies
        else:
            src.pop("currencies", None)
        company = request.form.get("company", "").strip()
        if company:
            src["company"] = company
        else:
            src.pop("company", None)
        store(cfg)
        flash("Saved.", "ok")
        return redirect(url_for("revolut_setup", sid=sid))

    # ---- vendors (what to look for in mail) ---------------------------------
    @app.route("/vendors", methods=["GET", "POST"])
    def vendors():
        cfg = load()
        edit = request.args.get("edit", type=int)
        if request.method == "POST":
            f = request.form
            v: dict[str, Any] = {"vendor": f["vendor"].strip()}
            if _csv(f.get("from")):
                senders = _csv(f["from"])
                v["from"] = senders[0] if len(senders) == 1 else senders
            for key in ("subject", "links", "merchant"):
                if f.get(key, "").strip():
                    v[key] = f[key].strip()
            if not f.get("attachments"):
                v["attachments"] = False
            if not f.get("required"):
                v["required"] = False
            if not v["vendor"] or not (v.get("from") or v.get("subject")):
                flash("A vendor needs a name and a sender (or a subject to match).", "warn")
                return redirect(url_for("vendors"))
            idx = f.get("index", type=int)
            if idx is not None and 0 <= idx < len(cfg["vendors"]):
                cfg["vendors"][idx] = v
            else:
                cfg["vendors"].append(v)
            store(cfg)
            flash(f"Saved {v['vendor']}.", "ok")
            return redirect(url_for("vendors"))
        current = cfg["vendors"][edit] if edit is not None and 0 <= edit < len(cfg["vendors"]) else None
        return render_template("vendors.html", vendors=cfg["vendors"], edit=edit, current=current)

    @app.post("/vendors/<int:idx>/delete")
    def delete_vendor(idx: int):
        cfg = load()
        if 0 <= idx < len(cfg["vendors"]):
            cfg["vendors"].pop(idx)
            store(cfg)
        return redirect(url_for("vendors"))

    # ---- manual checklist ---------------------------------------------------
    @app.route("/checklist", methods=["GET", "POST"])
    def checklist():
        cfg = load()
        if request.method == "POST":
            f = request.form
            if f.get("preset") == "revolut":
                formats = [x for x in ("PDF", "CSV") if f.get(f"fmt_{x}")] or ["PDF"]
                for cur in _csv(f.get("currencies", "").upper()):
                    for fmt in formats:
                        item = {"id": slug(f"revolut-{cur}-{fmt}").lower(), "vendor": "Revolut",
                                "label": f"{cur} statement ({fmt})", "url": REVOLUT_STATEMENTS_URL}
                        if not any(i["id"] == item["id"] for i in cfg["manual"]):
                            cfg["manual"].append(item)
            else:
                vendor, label = f.get("vendor", "").strip(), f.get("label", "").strip()
                if not vendor or not label:
                    flash("Fill in vendor and what to download.", "warn")
                    return redirect(url_for("checklist"))
                item = {"id": slug(f"{vendor}-{label}").lower(), "vendor": vendor, "label": label}
                if f.get("url", "").strip():
                    item["url"] = f["url"].strip()
                cfg["manual"] = [i for i in cfg["manual"] if i["id"] != item["id"]] + [item]
            store(cfg)
            return redirect(url_for("checklist"))
        return render_template("checklist.html", items=cfg["manual"])

    @app.post("/checklist/<item_id>/delete")
    def delete_item(item_id: str):
        cfg = load()
        cfg["manual"] = [i for i in cfg["manual"] if i["id"] != item_id]
        store(cfg)
        return redirect(url_for("checklist"))

    # ---- settings -----------------------------------------------------------
    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        cfg = load()
        if request.method == "POST":
            f = request.form
            if "raw" in f:
                import yaml
                try:
                    data = yaml.safe_load(f["raw"]) or {}
                    if not isinstance(data, dict):
                        raise ValueError("top level must be a mapping")
                    config_path.write_text(f["raw"], encoding="utf-8")
                    flash("Config saved.", "ok")
                except Exception as exc:  # noqa: BLE001
                    flash(f"Not saved, the YAML is invalid: {exc}", "error")
                return redirect(url_for("settings"))
            d = cfg.get("delivery") or {}
            d.update({"to": f.get("to", "").strip(), "cc": f.get("cc", "").strip(),
                      "name": f.get("name", "").strip(), "intro": f.get("intro", "").strip(),
                      "method": f.get("method", "applemail")})
            if f.get("smtp_source"):
                d["smtp_source"] = f["smtp_source"]
            d = {k: v for k, v in d.items() if v not in ("", None)}
            cfg["delivery"] = d
            cfg["schedule"] = {"day": max(1, min(28, f.get("day", 5, type=int)))}
            store(cfg)
            flash("Settings saved.", "ok")
            return redirect(url_for("settings"))
        from .. import __main__ as cli
        return render_template(
            "settings.html", d=cfg.get("delivery") or {}, schedule=cfg.get("schedule") or {"day": 5},
            mailboxes=[s for s in cfg["sources"] if s.get("type") == "imap"],
            raw=config_path.read_text(encoding="utf-8") if config_path.exists() else "",
            config_path=config_path, scheduled=cli.launch_agent_path().exists(),
            data_dir=paths.data_dir(), vault_where=vault.where(),
            python=Path(sys.executable).resolve(),
        )

    @app.post("/settings/schedule")
    def schedule():
        from .. import __main__ as cli
        try:
            if request.form.get("action") == "remove":
                cli.remove_schedule()
                flash("Monthly run turned off.", "ok")
            else:
                day = (load().get("schedule") or {}).get("day", 5)
                cli.install_schedule(day, config_path)
                flash(f"Monthly run turned on: day {day} at 09:07.", "ok")
        except Exception as exc:  # noqa: BLE001
            flash(f"Could not change the schedule: {exc}", "error")
        return redirect(url_for("settings"))

    return app


def _test(cfg: dict[str, Any], raw: dict[str, Any]) -> tuple[bool, str]:
    try:
        src = runner.build_source(cfg, raw)
        return True, src.check() if hasattr(src, "check") else "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def serve(config_path: Path, port: int = 8765, open_browser: bool = True) -> None:
    app = create_app(config_path, port=port)
    url = f"http://127.0.0.1:{port}/"
    print(f"BillsCollector is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
