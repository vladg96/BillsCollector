"""Revolut Business, read-only: monthly statements per currency + card payments.

Access uses Revolut's Business API with the READ scope only, so this
connection cannot move money. Setup (the UI walks you through it):

1. The app makes a private key + certificate. The key stays in your Keychain.
2. In Revolut Business: Settings -> APIs -> Business API -> add the certificate
   and a redirect URL. Revolut gives you a Client ID.
3. Approve read-only access once; the app keeps a refresh token in the Keychain.

Every request authenticates with a short-lived JWT signed by your key.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .. import vault
from ..models import Invoice, Period
from .base import USER_AGENT, Source

log = logging.getLogger(__name__)

API = "https://b2b.revolut.com/api/1.0"
CONSENT_URL = "https://business.revolut.com/app-confirm"
DEFAULT_REDIRECT = "https://example.com/billscollector"


class RevolutAuthError(Exception):
    """Access expired or was revoked: reconnect in the app."""


# ---- keys and the signed client assertion -----------------------------------

def generate_keypair(common_name: str = "BillsCollector") -> tuple[str, str]:
    """Return (private key PEM, self-signed certificate PEM) for Revolut's API settings."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=5 * 365))
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def client_assertion(private_key_pem: str, client_id: str, issuer: str, ttl: int = 300) -> str:
    """RS256 JWT: iss = redirect URL's domain, sub = client id, aud = https://revolut.com."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"iss": issuer, "sub": client_id, "aud": "https://revolut.com",
                               "exp": int(time.time()) + ttl}).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


def issuer_for(redirect_uri: str) -> str:
    return urlparse(redirect_uri).hostname or redirect_uri


def consent_url(client_id: str, redirect_uri: str) -> str:
    return CONSENT_URL + "?" + urlencode({"client_id": client_id, "redirect_uri": redirect_uri,
                                          "response_type": "code", "scope": "READ"})


def code_from_redirect(pasted: str) -> str:
    """Accept the full URL Revolut redirected to, or just the code."""
    pasted = pasted.strip()
    if "code=" in pasted:
        code = parse_qs(urlparse(pasted).query).get("code", [""])[0]
        if code:
            return code
    if pasted and " " not in pasted and "/" not in pasted:
        return pasted
    raise ValueError("couldn't find ?code=... in what you pasted")


# ---- API client -------------------------------------------------------------

class RevolutClient:
    def __init__(self, sid: str, client_id: str, redirect_uri: str,
                 session: requests.Session | None = None):
        self.sid = sid
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._access: tuple[str, float] | None = None

    # secrets live in the vault under names derived from the connection id
    def secret(self, what: str) -> str:
        return vault.secret_name(self.sid, what)

    def _assertion(self) -> str:
        key = vault.get(self.secret("private_key"))
        if not key:
            raise RevolutAuthError("private key missing - set up the Revolut connection again")
        return client_assertion(key, self.client_id, issuer_for(self.redirect_uri))

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        resp = self.session.post(f"{API}/auth/token", data={
            **data, "client_id": self.client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": self._assertion()}, timeout=30)
        if resp.status_code in (400, 401):
            raise RevolutAuthError(f"Revolut refused access ({resp.status_code}): {resp.text[:200]}"
                                   " - reconnect Revolut in the app")
        resp.raise_for_status()
        body = resp.json()
        self._access = (body["access_token"], time.time() + int(body.get("expires_in", 2399)) - 60)
        if body.get("refresh_token"):
            vault.set(self.secret("refresh_token"), body["refresh_token"])
        return body

    def exchange_code(self, code: str) -> None:
        self._token_request({"grant_type": "authorization_code", "code": code})

    def access_token(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        refresh = vault.get(self.secret("refresh_token"))
        if not refresh:
            raise RevolutAuthError("not connected yet - finish the Revolut setup in the app")
        self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        return self._access[0]

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self.session.get(f"{API}{path}", params=params, timeout=60,
                                headers={"Authorization": f"Bearer {self.access_token()}"})
        if resp.status_code == 401:
            raise RevolutAuthError("Revolut access expired - reconnect Revolut in the app")
        resp.raise_for_status()
        return resp.json()

    def accounts(self) -> list[dict[str, Any]]:
        return self.get("/accounts")

    def transactions(self, start: date, end: date, account: str | None = None) -> list[dict[str, Any]]:
        """All transactions created in [start, end). Pages backwards using `to`."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        to = f"{end.isoformat()}T00:00:00Z"
        while True:
            params = {"from": f"{start.isoformat()}T00:00:00Z", "to": to, "count": 1000}
            if account:
                params["account"] = account
            page = self.get("/transactions", params)
            fresh = [t for t in page if t["id"] not in seen]
            out += fresh
            seen.update(t["id"] for t in fresh)
            if not fresh:  # don't trust the page size: stop only when nothing new comes back
                return out
            to = min(t["created_at"] for t in page)


# ---- statements -------------------------------------------------------------

@dataclass
class Row:
    when: str
    type: str
    description: str
    reference: str
    amount: float
    fee: float
    balance: float | None
    counterparty: str
    id: str


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def statement_rows(transactions: list[dict[str, Any]], account_id: str, period: Period) -> list[Row]:
    """Completed movements on one account, by completion date within the period."""
    rows = []
    for t in transactions:
        if t.get("state") != "completed":
            continue
        done = _parse_ts(t.get("completed_at") or t.get("created_at"))
        if not done or not period.start <= done.date() < period.end:
            continue
        merchant = (t.get("merchant") or {}).get("name", "")
        for leg in t.get("legs", []):
            if leg.get("account_id") != account_id:
                continue
            cp = leg.get("counterparty") or {}
            rows.append(Row(
                when=done.strftime("%Y-%m-%d %H:%M"),
                type=t.get("type", ""),
                description=leg.get("description") or merchant or t.get("reference", ""),
                reference=t.get("reference", "") or "",
                amount=float(leg.get("amount", 0)),
                fee=float(leg.get("fee", 0) or 0),
                balance=float(leg["balance"]) if leg.get("balance") is not None else None,
                counterparty=cp.get("name") or merchant or "",
                id=t["id"],
            ))
    return sorted(rows, key=lambda r: (r.when, r.id))


def to_csv(rows: list[Row], currency: str) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date completed (UTC)", "Type", "Description", "Counterparty", "Reference",
                f"Amount ({currency})", f"Fee ({currency})", f"Balance ({currency})",
                "Transaction ID"])
    for r in rows:
        w.writerow([r.when, r.type, r.description, r.counterparty, r.reference,
                    f"{r.amount:.2f}", f"{r.fee:.2f}",
                    "" if r.balance is None else f"{r.balance:.2f}", r.id])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens accents correctly


_FONTS = [
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", None),
    ("/Library/Fonts/Arial Unicode.ttf", None),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


def to_pdf(rows: list[Row], account: dict[str, Any], period: Period, company: str = "") -> bytes:
    from fpdf import FPDF

    currency = account.get("currency", "")
    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(True, margin=12)
    family, ascii_only = "Helvetica", True
    for regular, bold in _FONTS:
        if Path(regular).exists():
            pdf.add_font("Body", "", regular)
            pdf.add_font("Body", "B", bold if bold and Path(bold).exists() else regular)
            family, ascii_only = "Body", False
            break

    def txt(s: str) -> str:
        if not ascii_only:
            return s
        import unicodedata
        return unicodedata.normalize("NFKD", s).encode("latin-1", "ignore").decode("latin-1")

    pdf.add_page()
    pdf.set_font(family, "B", 16)
    pdf.cell(0, 9, txt(f"{currency} account statement - {period.label}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 9)
    money_in = sum(r.amount for r in rows if r.amount > 0)
    money_out = sum(-r.amount for r in rows if r.amount < 0)
    fees = sum(r.fee for r in rows)
    closing = next((r.balance for r in reversed(rows) if r.balance is not None), None)
    lines = [
        f"{company + ' - ' if company else ''}Revolut Business - account: {account.get('name') or currency}",
        f"Period: {period.start:%d %b %Y} - {(period.end - timedelta(days=1)):%d %b %Y}"
        f"   Transactions: {len(rows)}",
        f"Money in: {money_in:,.2f} {currency}   Money out: {money_out:,.2f} {currency}"
        f"   Fees: {fees:,.2f} {currency}"
        + (f"   Balance after last transaction: {closing:,.2f} {currency}" if closing is not None else ""),
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by BillsCollector from the Revolut Business API "
        "(read-only). Not an official Revolut document.",
    ]
    for line in lines:
        pdf.cell(0, 5, txt(line), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    if not rows:
        pdf.set_font(family, "", 11)
        pdf.cell(0, 8, "No transactions in this period.")
        return bytes(pdf.output())

    pdf.set_font(family, "", 8)
    widths = (28, 72, 60, 30, 30, 22, 31)
    with pdf.table(col_widths=widths, text_align=("LEFT", "LEFT", "LEFT", "LEFT", "RIGHT",
                                                  "RIGHT", "RIGHT"),
                   line_height=4.5, first_row_as_headings=True) as table:
        table.row([txt(h) for h in ("Date", "Description", "Counterparty", "Type",
                                    f"Amount {currency}", "Fee", f"Balance {currency}")])
        for r in rows:
            table.row([r.when, txt(r.description[:80]), txt(r.counterparty[:60]),
                       r.type.replace("_", " "), f"{r.amount:,.2f}",
                       f"{r.fee:,.2f}" if r.fee else "",
                       "" if r.balance is None else f"{r.balance:,.2f}"])
    return bytes(pdf.output())


def card_payments(transactions: list[dict[str, Any]], period: Period) -> list[dict[str, Any]]:
    """Outgoing card payments in the period: what should each have an invoice for."""
    out = []
    for t in transactions:
        if t.get("type") != "card_payment" or t.get("state") != "completed":
            continue
        done = _parse_ts(t.get("completed_at") or t.get("created_at"))
        if not done or not period.start <= done.date() < period.end:
            continue
        leg = (t.get("legs") or [{}])[0]
        if float(leg.get("amount", 0)) >= 0:
            continue
        out.append({"date": done.date().isoformat(),
                    "merchant": (t.get("merchant") or {}).get("name") or leg.get("description", ""),
                    "amount": -float(leg["amount"]), "currency": leg.get("currency", ""),
                    "id": t["id"]})
    return sorted(out, key=lambda p: (p["date"], p["merchant"]))


class RevolutSource(Source):
    type = "revolut"

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        for key in ("id", "client_id"):
            if not cfg.get(key):
                raise ValueError(f"revolut connection: '{key}' missing - finish setup in the app")
        self.client = RevolutClient(cfg["id"], cfg["client_id"],
                                    cfg.get("redirect_uri", DEFAULT_REDIRECT))
        self.formats = [f.lower() for f in cfg.get("formats", ["pdf", "csv"])]
        self.currencies = {c.upper() for c in cfg.get("currencies", [])}
        self.payments: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def required_vendors(self) -> list[str]:
        return []  # statements are generated, so they can't go missing

    def check(self) -> str:
        accounts = [a for a in self.client.accounts() if a.get("state") == "active"]
        return "read-only access OK: " + ", ".join(
            f"{a.get('name') or a['currency']} ({a['currency']})" for a in accounts)

    def collect(self, period: Period) -> list[Invoice]:
        accounts = [a for a in self.client.accounts() if a.get("state") == "active"
                    and (not self.currencies or a["currency"] in self.currencies)]
        # Created up to a week earlier can complete inside the month.
        txs = self.client.transactions(period.start - timedelta(days=7), period.end)
        self.payments = card_payments(txs, period)
        per_currency: dict[str, int] = {}
        for a in accounts:
            per_currency[a["currency"]] = per_currency.get(a["currency"], 0) + 1
        out: list[Invoice] = []
        for a in accounts:
            rows = statement_rows(txs, a["id"], period)
            label = a["currency"]
            if per_currency[a["currency"]] > 1:  # several pockets in one currency
                label += f"-{a.get('name') or a['id'][:6]}"
            if "pdf" in self.formats:
                out.append(Invoice("Revolut", f"{label}-statement.pdf",
                                   to_pdf(rows, a, period, self.cfg.get("company", "")),
                                   self.name, None, replace=True))
            if "csv" in self.formats:
                out.append(Invoice("Revolut", f"{label}-statement.csv", to_csv(rows, a["currency"]),
                                   self.name, None, replace=True))
        return out
