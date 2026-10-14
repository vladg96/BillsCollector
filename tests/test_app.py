from __future__ import annotations

import io
import os
import re
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from billscollector import config, vault
from billscollector.models import Period
from billscollector.month import Month
from billscollector.sources.applemail import AppleMailSource, parse_emlx
from billscollector.web import create_app

from test_collector import PDF, make_mail


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLS_HOME", str(tmp_path / "home"))


def write_emlx(folder: Path, number: int, msg, mtime: datetime, partial_files=()) -> Path:
    messages = folder / "Data" / "Messages"
    messages.mkdir(parents=True, exist_ok=True)
    raw = msg.as_bytes()
    name = f"{number}.partial.emlx" if partial_files else f"{number}.emlx"
    path = messages / name
    path.write_bytes(b"%d\n" % len(raw) + raw + b"<?xml version=\"1.0\"?><plist/>")
    ts = mtime.timestamp()
    os.utime(path, (ts, ts))
    for fname, content in partial_files:
        att = folder / "Data" / "Attachments" / str(number) / "2"
        att.mkdir(parents=True, exist_ok=True)
        (att / fname).write_bytes(content)
    return path


def test_parse_emlx_strips_length_and_plist(tmp_path):
    msg = make_mail("a@acme.com", "Invoice")
    path = write_emlx(tmp_path, 1, msg, datetime(2026, 9, 9))
    parsed = parse_emlx(path.read_bytes())
    assert parsed["Subject"] == "Invoice"
    assert [a.get_filename() for a in parsed.iter_attachments()] == ["invoice.pdf"]


def test_apple_mail_source(tmp_path):
    mbox = tmp_path / "Mail" / "V10" / "acct" / "INBOX.mbox"
    rules = [{"vendor": "Acme", "from": "@acme.com"}, {"vendor": "Digi", "from": "@digi.ro"}]
    sept = datetime(2026, 9, 9)
    acme = make_mail("Acme <bill@acme.com>", "Invoice")
    write_emlx(mbox, 1, acme, sept)
    # same message also in another mailbox -> taken once
    write_emlx(tmp_path / "Mail" / "V10" / "acct" / "Archive.mbox", 1, acme, sept)
    # partial message: attachment stored next to it
    stub = make_mail("Digi <facturi@digi.ro>", "Factura", attachment=None)
    write_emlx(mbox, 2, stub, sept, partial_files=[("factura.pdf", PDF + b"digi")])
    # wrong month by Date header
    write_emlx(mbox, 3, make_mail("bill@acme.com", "Invoice",
                                  when="Tue, 05 Aug 2026 10:00:00 +0000",
                                  attachment=("aug.pdf", PDF + b"aug")), sept)
    # old file, skipped by mtime
    write_emlx(mbox, 4, make_mail("bill@acme.com", "Invoice",
                                  attachment=("old.pdf", PDF + b"old")), datetime(2025, 1, 1))

    src = AppleMailSource({"name": "mail", "path": str(tmp_path / "Mail"), "rules": rules})
    got = src.collect(Period(2026, 9))
    assert sorted((i.vendor, i.filename) for i in got) == [("Acme", "invoice.pdf"),
                                                          ("Digi", "factura.pdf")]
    assert "found 5 messages" in src.check()


def test_apple_mail_missing_folder(tmp_path):
    src = AppleMailSource({"path": str(tmp_path / "nope"), "rules": []})
    with pytest.raises(FileNotFoundError):
        src.check()


def test_vault_file_fallback_and_resolve(monkeypatch):
    vault.set("MY_SECRET", "abc")
    assert vault.get("MY_SECRET") == "abc"
    assert config.resolve("pw=${MY_SECRET}") == "pw=abc"
    secrets_file = Path(os.environ["BILLS_HOME"]) / "secrets.json"
    assert oct(secrets_file.stat().st_mode & 0o777) == "0o600"
    monkeypatch.setenv("MY_SECRET", "from-env")  # env wins (GitHub Actions)
    assert vault.get("MY_SECRET") == "from-env"
    vault.delete("MY_SECRET")
    monkeypatch.delenv("MY_SECRET")
    assert vault.get("MY_SECRET") is None


def test_month_manual_items(tmp_path):
    m = Month(tmp_path, Period(2026, 9))
    manual = [{"id": "revolut-eur-pdf", "vendor": "Revolut", "label": "EUR statement (PDF)"}]
    assert m.missing_items(manual) == manual
    name = m.add_file("statement.pdf", PDF, "Revolut", manual[0])
    assert name == "2026-09_Revolut_EUR-statement-PDF.pdf"
    m.add_file("statement-v2.pdf", PDF + b"2", "Revolut", manual[0])  # replaces
    assert [e.name for e in m.entries()] == [name]
    assert (m.folder / name).read_bytes() == PDF + b"2"
    assert m.missing_items(manual) == []
    assert not m.complete(manual)  # never collected
    assert "Revolut" in Month(tmp_path, Period(2026, 9)).summary(manual)  # persisted


# ---- web UI ------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    app = create_app(cfg_path, invoices_root=tmp_path / "inv")
    app.testing = True
    c = app.test_client()
    c.cfg_path = cfg_path
    return c


def csrf(client, url="/vendors") -> str:
    html = client.get(url).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_post_without_csrf_is_rejected(client):
    assert client.post("/vendors", data={"vendor": "X", "from": "@x"}).status_code == 403


def test_host_check_blocks_foreign_hosts(tmp_path):
    app = create_app(tmp_path / "c.yaml", invoices_root=tmp_path / "inv", port=8765)
    c = app.test_client()
    assert c.get("/connections", base_url="http://evil.example").status_code == 403
    assert c.get("/connections", base_url="http://127.0.0.1:8765").status_code == 200


def test_setup_flow(client, monkeypatch):
    token = csrf(client)
    # home redirects to connections while there are none
    assert client.get("/").headers["Location"].endswith("/connections")

    # add a Gmail connection: password goes to the vault, not the file
    from billscollector.sources.imap import ImapSource
    from test_collector import FakeImap
    monkeypatch.setattr(ImapSource, "imap_factory", FakeImap)
    FakeImap.mailbox = {b"1": make_mail("Digi <facturi@digi.ro>", "Factura")}
    r = client.post("/connections/new/gmail", data={
        "csrf": token, "name": "Personal Gmail", "username": "rvgprod@gmail.com",
        "password": "abcd efgh ijkl mnop", "folders": "[Gmail]/All Mail"})
    assert r.status_code == 302
    cfg = yaml.safe_load(client.cfg_path.read_text())
    src = cfg["sources"][0]
    assert src["password"] == "${GMAIL_RVGPROD_GMAIL_COM_PASSWORD}"
    assert "abcd" not in client.cfg_path.read_text()
    assert vault.get("GMAIL_RVGPROD_GMAIL_COM_PASSWORD") == "abcdefghijklmnop"
    assert "connected" in client.get("/connections").get_data(as_text=True)

    # vendors + checklist
    client.post("/vendors", data={"csrf": token, "vendor": "Digi", "from": "@digi.ro",
                                  "attachments": "on", "required": "on"})
    client.post("/vendors", data={"csrf": token, "vendor": "Enel", "from": "@enel.ro",
                                  "attachments": "on", "required": "on"})
    client.post("/checklist", data={"csrf": token, "preset": "revolut",
                                    "currencies": "eur, usd", "fmt_PDF": "on"})
    cfg = yaml.safe_load(client.cfg_path.read_text())
    assert [v["vendor"] for v in cfg["vendors"]] == ["Digi", "Enel"]
    assert [i["label"] for i in cfg["manual"]] == ["EUR statement (PDF)", "USD statement (PDF)"]

    # collect September
    client.post("/month/2026-09/collect", data={"csrf": token})
    page = client.get("/month/2026-09").get_data(as_text=True)
    assert "2026-09-09_Digi_invoice.pdf" in page
    assert "Enel" in page and "USD statement (PDF)" in page
    assert "thing(s) need you" in page

    # upload the checklist items and the missing vendor
    for item in ("revolut-eur-pdf", "revolut-usd-pdf"):
        client.post("/month/2026-09/upload", data={
            "csrf": token, "item": item, "file": (io.BytesIO(PDF + item.encode()), "x.pdf")},
            content_type="multipart/form-data")
    client.post("/month/2026-09/upload", data={
        "csrf": token, "vendor": "Enel", "file": (io.BytesIO(PDF + b"enel"), "enel sept.pdf")},
        content_type="multipart/form-data")
    page = client.get("/month/2026-09").get_data(as_text=True)
    assert "Complete" in page
    assert client.get("/month/2026-09/file/2026-09_Enel_enel-sept.pdf").status_code == 200
    assert client.get("/month/2026-09/file/../config.yaml").status_code == 404
    assert client.get("/month/2026-09/zip").status_code == 200

    # settings
    client.post("/settings", data={"csrf": token, "to": "office@highconta.ro",
                                   "method": "applemail", "day": "5"})
    cfg = yaml.safe_load(client.cfg_path.read_text())
    assert cfg["delivery"] == {"to": "office@highconta.ro", "method": "applemail"}

    # sending via Apple Mail runs osascript with the files as arguments
    calls = []
    monkeypatch.setattr("subprocess.run", lambda args, **kw: calls.append(args))
    client.post("/month/2026-09/send", data={"csrf": token})
    assert calls and calls[0][:2] == ["osascript", "-"]
    assert calls[0][4] == "office@highconta.ro"
    assert sum(a.endswith(".pdf") for a in calls[0]) == 4
    assert "sent" in client.get("/month/2026-09").get_data(as_text=True)


def test_raw_config_rejects_invalid_yaml(client):
    token = csrf(client)
    client.post("/settings", data={"csrf": token, "raw": "sources: [1, 2"})
    assert not client.cfg_path.exists()
    client.post("/settings", data={"csrf": token, "raw": "vendors: []\n"})
    assert client.cfg_path.read_text() == "vendors: []\n"
