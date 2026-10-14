from __future__ import annotations

import base64
import csv
import io
import json
import re

import pytest
import yaml

from billscollector import vault
from billscollector.bundle import RunReport, save
from billscollector.models import Period
from billscollector.month import Month, similar
from billscollector.sources import revolut
from billscollector.sources.revolut import RevolutSource


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLS_HOME", str(tmp_path / "home"))


@pytest.fixture(scope="module")
def keys():
    return revolut.generate_keypair()


ACCOUNTS = [
    {"id": "acc-eur", "name": "Main", "currency": "EUR", "state": "active", "balance": 900},
    {"id": "acc-usd", "name": "USD", "currency": "USD", "state": "active", "balance": 50},
    {"id": "acc-old", "name": "Old", "currency": "GBP", "state": "inactive", "balance": 0},
]


def tx(id, created, completed, legs, type="card_payment", state="completed", merchant=None):
    t = {"id": id, "type": type, "state": state, "created_at": created,
         "completed_at": completed, "legs": legs, "reference": ""}
    if merchant:
        t["merchant"] = {"name": merchant}
    return t


TXS = [
    tx("t1", "2026-09-03T10:00:00Z", "2026-09-03T10:00:01Z",
       [{"account_id": "acc-eur", "amount": -19.99, "currency": "EUR", "balance": 980.01,
         "description": "Notion Labs"}], merchant="NOTION LABS"),
    tx("t2", "2026-09-10T08:00:00Z", "2026-09-10T08:00:00Z",
       [{"account_id": "acc-eur", "amount": -80.0, "currency": "EUR", "balance": 900.01,
         "description": "Digi România", "fee": 0}], merchant="Digi Romania SA"),
    tx("t3", "2026-09-15T08:00:00Z", "2026-09-15T08:00:00Z",
       [{"account_id": "acc-usd", "amount": 1500.0, "currency": "USD", "balance": 1550.0,
         "description": "Client Inc", "counterparty": {"name": "Client Inc"}}],
       type="transfer"),
    # created in August, completed in September -> belongs to September
    tx("t4", "2026-08-30T22:00:00Z", "2026-09-01T09:00:00Z",
       [{"account_id": "acc-eur", "amount": -4.5, "currency": "EUR", "balance": 1000.0,
         "description": "Coffee"}], merchant="Coffee Shop"),
    tx("t5", "2026-09-20T08:00:00Z", None,
       [{"account_id": "acc-eur", "amount": -10, "currency": "EUR"}], state="declined"),
    tx("t6", "2026-10-01T08:00:00Z", "2026-10-01T08:00:00Z",
       [{"account_id": "acc-eur", "amount": -1, "currency": "EUR"}]),
]


class FakeResp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeRevolut:
    """Stands in for requests.Session against b2b.revolut.com."""

    def __init__(self, page_size=1000):
        self.headers = {}
        self.calls = []
        self.page_size = page_size
        self.refresh_ok = True

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        assert url.endswith("/auth/token")
        assert data["client_assertion_type"].endswith("jwt-bearer")
        if data["grant_type"] == "refresh_token" and not self.refresh_ok:
            return FakeResp({"error": "invalid_grant"}, 400)
        return FakeResp({"access_token": "at-" + data["grant_type"], "expires_in": 2399,
                         "refresh_token": "rt-1"})

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("GET", url, params))
        assert headers["Authorization"].startswith("Bearer at-")
        if url.endswith("/accounts"):
            return FakeResp(ACCOUNTS)
        start, end = params["from"], params["to"]
        hits = sorted((t for t in TXS if start <= t["created_at"] < end),
                      key=lambda t: t["created_at"], reverse=True)
        return FakeResp(hits[: min(params["count"], self.page_size)])


def test_client_assertion_is_valid_rs256(keys):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key_pem, cert_pem = keys
    jwt = revolut.client_assertion(key_pem, "client-123", "example.com")
    header, payload, sig = jwt.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    claims = json.loads(base64.urlsafe_b64decode(pad(payload)))
    assert json.loads(base64.urlsafe_b64decode(pad(header))) == {"alg": "RS256", "typ": "JWT"}
    assert claims["iss"] == "example.com" and claims["sub"] == "client-123"
    assert claims["aud"] == "https://revolut.com" and isinstance(claims["exp"], int)
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    cert.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{header}.{payload}".encode(),
                             padding.PKCS1v15(), hashes.SHA256())  # raises if invalid


def test_consent_and_code_parsing():
    url = revolut.consent_url("cid", "https://example.com/billscollector")
    assert url.startswith("https://business.revolut.com/app-confirm?")
    assert "scope=READ" in url and "response_type=code" in url
    assert revolut.code_from_redirect(
        "https://example.com/billscollector?code=oa_prod_abc123") == "oa_prod_abc123"
    assert revolut.code_from_redirect("  oa_prod_abc123 ") == "oa_prod_abc123"
    with pytest.raises(ValueError):
        revolut.code_from_redirect("https://example.com/billscollector")
    assert revolut.issuer_for("https://example.com/billscollector") == "example.com"


def test_rows_by_completion_date():
    rows = revolut.statement_rows(TXS, "acc-eur", Period(2026, 9))
    assert [r.id for r in rows] == ["t4", "t1", "t2"]
    assert rows[-1].balance == 900.01
    data = list(csv.reader(io.StringIO(revolut.to_csv(rows, "EUR").decode("utf-8-sig"))))
    assert data[0][5] == "Amount (EUR)" and data[3][2] == "Digi România" and data[3][5] == "-80.00"


def test_pdf_handles_diacritics():
    rows = revolut.statement_rows(TXS, "acc-eur", Period(2026, 9))
    pdf = revolut.to_pdf(rows, ACCOUNTS[0], Period(2026, 9), "Ștefan SRL")
    assert pdf.startswith(b"%PDF") and len(pdf) > 1000
    empty = revolut.to_pdf([], ACCOUNTS[1], Period(2026, 9))
    assert empty.startswith(b"%PDF")


def connect(keys, session):
    vault.set(vault.secret_name("revolut", "private_key"), keys[0])
    client = revolut.RevolutClient("revolut", "cid", revolut.DEFAULT_REDIRECT, session)
    client.exchange_code("oa_prod_x")
    return client


def test_token_exchange_refresh_and_pagination(keys):
    session = FakeRevolut(page_size=2)
    client = connect(keys, session)
    assert vault.get(vault.secret_name("revolut", "refresh_token")) == "rt-1"
    client._access = None  # force a refresh
    txs = client.transactions(Period(2026, 8).start, Period(2026, 10).start)
    assert sorted(t["id"] for t in txs) == ["t1", "t2", "t3", "t4", "t5"]
    grants = [c[2]["grant_type"] for c in session.calls if c[0] == "POST"]
    assert grants == ["authorization_code", "refresh_token"]
    assert sum(c[0] == "GET" for c in session.calls) >= 3  # paged

    session.refresh_ok = False
    client._access = None
    with pytest.raises(revolut.RevolutAuthError, match="reconnect"):
        client.accounts()


def test_source_collects_statements_and_payments(keys, monkeypatch, tmp_path):
    session = FakeRevolut()
    connect(keys, session)
    monkeypatch.setattr(revolut.requests, "Session", lambda: session)
    src = RevolutSource({"id": "revolut", "type": "revolut", "client_id": "cid",
                         "name": "Revolut Business"})
    period = Period(2026, 9)
    got = src.collect(period)
    assert sorted(i.filename for i in got) == ["EUR-statement.csv", "EUR-statement.pdf",
                                              "USD-statement.csv", "USD-statement.pdf"]
    assert [p["merchant"] for p in src.payments] == ["Coffee Shop", "NOTION LABS",
                                                     "Digi Romania SA"]
    assert "Main (EUR)" in src.check()

    # re-collecting overwrites the generated statements instead of adding copies
    report = RunReport(period)
    save(got, period, tmp_path, report)
    report2 = RunReport(period)
    save(src.collect(period), period, tmp_path, report2)
    assert sorted(p.name for p in (tmp_path / "2026-09").iterdir()) == sorted(
        s.path.name for s in report2.saved)

    # payments are matched against the month's files
    report2.payments = src.payments
    m = Month(tmp_path, period)
    m.record_run(report2)
    m.add_file("digi.pdf", b"%PDF-1.4 digi", "Digi")
    left = m.unmatched_payments([{"vendor": "Notion", "merchant": "NOTION LABS"}],
                                ignore=["Coffee Shop"])
    assert [p["merchant"] for p in left] == ["NOTION LABS"]  # no Notion file yet
    m.add_file("n.pdf", b"%PDF-1.4 n", "Notion")
    assert m.unmatched_payments([{"vendor": "Notion"}], ignore=["Coffee Shop"]) == []


def test_similar():
    assert similar("GOOGLE *GSUITE_OH", "Google Workspace")
    assert similar("Digi Romania SA", "Digi")
    assert not similar("Enel Energie SRL", "Digi Romania")
    assert not similar("Some Services SRL", "Other Services SRL")


def test_web_wizard(tmp_path, keys, monkeypatch):
    from billscollector.web import create_app

    app = create_app(tmp_path / "config.yaml", invoices_root=tmp_path / "inv")
    app.testing = True
    c = app.test_client()
    token = re.search(r'name="csrf" value="([^"]+)"',
                      c.get("/vendors").get_data(as_text=True)).group(1)

    r = c.post("/connections/revolut/start", data={"csrf": token})
    assert r.headers["Location"].endswith("/connections/revolut/revolut")
    page = c.get("/connections/revolut/revolut").get_data(as_text=True)
    assert "BEGIN CERTIFICATE" in page and "setup not finished" in page
    assert vault.get("REVOLUT_PRIVATE_KEY", ).startswith("-----BEGIN PRIVATE KEY")

    c.post("/connections/revolut/revolut/client",
           data={"csrf": token, "client_id": "cid", "redirect_uri": revolut.DEFAULT_REDIRECT})
    page = c.get("/connections/revolut/revolut").get_data(as_text=True)
    assert "app-confirm?client_id=cid" in page

    session = FakeRevolut()
    monkeypatch.setattr(revolut.requests, "Session", lambda: session)
    c.post("/connections/revolut/revolut/code",
           data={"csrf": token, "redirect": revolut.DEFAULT_REDIRECT + "?code=oa_prod_1"})
    page = c.get("/connections/revolut/revolut").get_data(as_text=True)
    assert ">connected<" in page and "Main (EUR)" in page

    c.post("/connections/revolut/revolut/options",
           data={"csrf": token, "fmt_pdf": "on", "currencies": "eur"})
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["sources"][0]["formats"] == ["pdf"] and cfg["sources"][0]["currencies"] == ["EUR"]
    assert "PRIVATE KEY" not in (tmp_path / "config.yaml").read_text()

    c.post("/month/2026-09/collect", data={"csrf": token})
    page = c.get("/month/2026-09").get_data(as_text=True)
    assert "2026-09_Revolut_EUR-statement.pdf" in page and "USD-statement" not in page
    assert "Card payments without an invoice (3)" in page
    c.post("/month/2026-09/ignore-merchant", data={"csrf": token, "merchant": "Coffee Shop"})
    assert "Card payments without an invoice (2)" in c.get("/month/2026-09").get_data(as_text=True)

    c.post("/connections/revolut/delete", data={"csrf": token})
    assert vault.get("REVOLUT_PRIVATE_KEY") is None and vault.get("REVOLUT_REFRESH_TOKEN") is None
