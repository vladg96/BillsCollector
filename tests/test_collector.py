from __future__ import annotations

import zipfile
from datetime import date
from email.message import EmailMessage
from email.utils import make_msgid
from email.policy import default as default_policy

import pytest

from billscollector import __main__ as cli
from billscollector import bundle, config, deliver
from billscollector.models import Invoice, Period
from billscollector.sources import imap as imap_mod
from billscollector.sources.imap import ImapSource, Rule, extract_links

PDF = b"%PDF-1.4\n%fake invoice\n"


def make_mail(sender, subject, when="Tue, 09 Sep 2026 10:00:00 +0000",
              attachment=("invoice.pdf", PDF), html=None) -> EmailMessage:
    msg = EmailMessage(policy=default_policy)
    msg["From"], msg["Subject"], msg["Date"] = sender, subject, when
    msg["Message-ID"] = make_msgid()
    msg.set_content("Your invoice is ready.")
    if html:
        msg.add_alternative(html, subtype="html")
    if attachment:
        msg.add_attachment(attachment[1], maintype="application", subtype="pdf",
                           filename=attachment[0])
    return msg


class FakeImap:
    """Just enough of imaplib.IMAP4 for ImapSource. SEARCH matches on FROM only."""

    mailbox: dict[bytes, EmailMessage] = {}

    def __init__(self, host, port):
        self.searches = []

    def login(self, user, password):
        return "OK", [b""]

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            crit = list(args[1:])
            sender = crit[crit.index("FROM") + 1].strip('"') if "FROM" in crit else ""
            hits = [u for u, m in self.mailbox.items() if sender in str(m["From"])]
            return "OK", [b" ".join(hits)]
        uid = args[0]
        raw = self.mailbox[uid].as_bytes()
        if "HEADER" in args[1]:
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
        return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]

    def logout(self):
        return "BYE", [b""]


def test_period():
    p = Period.parse("2026-09")
    assert (p.start, p.end, p.label) == (date(2026, 9, 1), date(2026, 10, 1), "September 2026")
    assert str(Period.previous(date(2026, 1, 14))) == "2025-12"
    with pytest.raises(ValueError):
        Period.parse("2026-13")


def test_resolve_env(monkeypatch):
    monkeypatch.setenv("TOKEN", "s3cret")
    assert config.resolve({"a": ["x ${TOKEN}"]}) == {"a": ["x s3cret"]}
    with pytest.raises(config.ConfigError, match="NOPE"):
        config.resolve("${NOPE}")


def test_rule_matching_and_criteria():
    rule = Rule({"vendor": "Acme", "from": ["@acme.com", "billing@acme.io"],
                 "subject": "invoice", "extend_days": 3})
    assert rule.matches(make_mail("Acme <no-reply@ACME.com>", "Your Invoice #12"))
    assert not rule.matches(make_mail("Acme <no-reply@acme.com>", "Newsletter"))
    assert not rule.matches(make_mail("Other <x@other.com>", "invoice"))
    crit = rule.search_criteria(Period(2026, 9))
    assert len(crit) == 2
    assert crit[0][:4] == ["SINCE", "01-Sep-2026", "BEFORE", "04-Oct-2026"]
    assert crit[1][-1] == '"billing@acme.io"'


def test_extract_links_from_html_and_text():
    msg = make_mail("a@b.c", "x", attachment=None,
                    html='<a href="https://acme.com/inv?id=1&amp;t=2">Download</a>')
    links = extract_links(msg)
    assert "https://acme.com/inv?id=1&t=2" in links


def test_imap_source_end_to_end(monkeypatch):
    FakeImap.mailbox = {
        b"1": make_mail("Acme <billing@acme.com>", "Invoice September"),
        b"2": make_mail("Acme <billing@acme.com>", "Promo", attachment=("promo.pdf", PDF + b"2")),
        b"3": make_mail("Cloud <noreply@cloud.io>", "Receipt", attachment=None,
                        html='<a href="https://cloud.io/r/abc.pdf">get</a>'
                             '<a href="https://cloud.io/help">help</a>'),
        b"4": make_mail("Portal <noreply@portal.io>", "Invoice", attachment=None,
                        html='<a href="https://portal.io/invoices/9">view</a>'),
    }

    def fake_download(url, session=None, headers=None, timeout=60):
        return ("abc.pdf", PDF + b"cloud") if url.endswith(".pdf") else None

    monkeypatch.setattr(imap_mod, "download_pdf", fake_download)
    monkeypatch.setattr(ImapSource, "imap_factory", FakeImap)
    src = ImapSource({"name": "mail", "host": "h", "username": "u", "password": "p", "rules": [
        {"vendor": "Acme", "from": "@acme.com", "subject": "invoice"},
        {"vendor": "Cloud", "from": "@cloud.io", "attachments": False, "links": "cloud.io/r/"},
        {"vendor": "Portal", "from": "@portal.io", "attachments": False, "links": "invoices"},
    ]})
    got = src.collect(Period(2026, 9))
    assert sorted((i.vendor, i.filename) for i in got) == [("Acme", "invoice.pdf"),
                                                          ("Cloud", "abc.pdf")]
    assert got[0].date == date(2026, 9, 9)
    assert len(src.warnings) == 1 and "portal.io/invoices/9" in src.warnings[0]


def test_save_dedupes_and_names(tmp_path):
    p = Period(2026, 9)
    report = bundle.RunReport(p)
    invs = [Invoice("Acme Inc", "inv 1.pdf", PDF, "s", date(2026, 9, 3)),
            Invoice("Acme Inc", "copy.pdf", PDF, "s", date(2026, 9, 3)),       # same bytes
            Invoice("Acme Inc", "inv 1.pdf", PDF + b"x", "s", date(2026, 9, 3))]  # same name
    bundle.save(invs, p, tmp_path, report)
    names = sorted(s.path.name for s in report.saved)
    assert names == ["2026-09-03_Acme-Inc_inv-1-2.pdf", "2026-09-03_Acme-Inc_inv-1.pdf"]
    assert report.duplicates == 1

    again = bundle.RunReport(p)  # re-running doesn't pile up copies
    bundle.save(invs, p, tmp_path, again)
    assert sorted(s.path.name for s in again.saved) == names

    with zipfile.ZipFile(bundle.write_zip(report, tmp_path)) as zf:
        assert set(zf.namelist()) == set(names) | {"SUMMARY.txt"}


def test_delivery_splits_and_routes(tmp_path):
    p = Period(2026, 9)
    files = []
    for i in range(3):
        f = tmp_path / f"{i}.pdf"
        f.write_bytes(PDF + bytes(600_000) + bytes([i]))
        files.append(f)
    cfg = {"from": "me@x.com", "to": "acc@y.com", "max_attachment_mb": 1, "name": "Me"}

    msgs = deliver.compose(p, files, cfg, True, "file list")
    assert len(msgs) == 3
    assert msgs[0]["To"] == "acc@y.com" and msgs[0]["Cc"] == "me@x.com"
    assert msgs[0]["Subject"] == "Invoices September 2026 - Me (1/3)"
    assert "invoices for September 2026" in msgs[0].get_body().get_content()

    held = deliver.compose(p, files, cfg, False, "MISSING: Vodafone")
    assert held[0]["To"] == "me@x.com" and "Vodafone" in held[0].get_body().get_content()


def test_smtp_settings_borrow_mailbox(monkeypatch):
    monkeypatch.setenv("GM_PASSWORD", "app-pw")
    cfg = {"sources": [{"id": "gm", "type": "imap", "host": "imap.gmail.com",
                        "username": "me@gmail.com", "password": "${GM_PASSWORD}"}],
           "delivery": {"to": "acc@y.com", "smtp_source": "gm", "method": "smtp"}}
    s = deliver.smtp_settings(cfg)
    assert (s["smtp_host"], s["smtp_port"], s["smtp_password"], s["from"]) == \
        ("smtp.gmail.com", 465, "app-pw", "me@gmail.com")


def test_cli_run_reports_missing_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(ImapSource, "imap_factory", FakeImap)
    FakeImap.mailbox = {b"1": make_mail("billing@acme.com", "Invoice")}
    monkeypatch.setenv("PW", "x")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sources:\n"
        "  - {type: imap, name: mail, host: h, username: u, password: '${PW}', rules: [\n"
        "      {vendor: Acme, from: '@acme.com'}, {vendor: Telco, from: '@telco.com'}]}\n"
        "  - {type: http, name: broken, vendor: X, url: '${UNSET_VAR}'}\n"
        "  - {type: http, name: off, enabled: false, vendor: Y, url: '${UNSET_VAR}'}\n"
    )
    code = cli.main(["--config", str(cfg), "run", "--month", "2026-09",
                     "--out", str(tmp_path / "out")])
    assert (tmp_path / "out" / "invoices-2026-09.zip").exists()
    summary = (tmp_path / "out" / "summary-2026-09.txt").read_text()
    assert code == 1
    assert "Acme" in summary and "- Telco" in summary
    assert "broken: ConfigError" in summary and "off" not in summary
