"""Collect invoices from any mailbox reachable over IMAP (Gmail, Yahoo, Outlook, ...).

Each rule describes one vendor: who the mail comes from, optionally a subject
pattern, and whether to keep the PDF attachments and/or follow a download link.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import date, timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Iterable

import requests

from ..models import Invoice, Period
from .base import Source, download_pdf

log = logging.getLogger(__name__)

_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
_TEXT_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href.strip())


def extract_links(msg: EmailMessage) -> list[str]:
    links: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain") or part.get_filename():
            continue
        try:
            text = part.get_content()
        except (LookupError, UnicodeDecodeError):
            text = part.get_payload(decode=True).decode("utf-8", "replace")
        if ctype == "text/html":
            parser = _LinkParser()
            parser.feed(text)
            links += parser.links
        else:
            links += _TEXT_URL.findall(text)
    return list(dict.fromkeys(links))  # dedupe, keep order


def message_date(msg: EmailMessage) -> date | None:
    try:
        return parsedate_to_datetime(msg["Date"]).date()
    except (TypeError, ValueError):
        return None


def as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class Rule:
    def __init__(self, cfg: dict[str, Any]):
        if not cfg.get("vendor"):
            raise ValueError("every imap rule needs a 'vendor'")
        self.vendor: str = cfg["vendor"]
        self.senders: list[str] = as_list(cfg.get("from"))
        self.subject = re.compile(cfg["subject"], re.I) if cfg.get("subject") else None
        if not self.senders and not self.subject:
            raise ValueError(f"rule {self.vendor!r}: set 'from' and/or 'subject'")
        self.attachments: bool = cfg.get("attachments", True)
        self.extensions = tuple(e.lower() for e in as_list(cfg.get("extensions") or ".pdf"))
        self.filename = re.compile(cfg["filename"], re.I) if cfg.get("filename") else None
        self.links = re.compile(cfg["links"], re.I) if cfg.get("links") else None
        self.required: bool = cfg.get("required", True)
        self.extend_days: int = int(cfg.get("extend_days", 0))

    def search_criteria(self, period: Period) -> list[list[str]]:
        """One IMAP SEARCH per sender (results are unioned); subject is checked locally."""
        window = ["SINCE", imap_date(period.start),
                  "BEFORE", imap_date(period.end + timedelta(days=self.extend_days))]
        if not self.senders:
            return [window]
        return [window + ["FROM", quote(s)] for s in self.senders]

    def matches(self, msg: EmailMessage) -> bool:
        if self.subject and not self.subject.search(str(msg.get("Subject", ""))):
            return False
        if self.senders:
            sender = str(msg.get("From", "")).lower()
            return any(s.lower() in sender for s in self.senders)
        return True

    def wanted_attachment(self, filename: str) -> bool:
        name = filename.lower()
        if not name.endswith(self.extensions):
            return False
        return not self.filename or bool(self.filename.search(filename))


def invoices_from_message(
    msg: EmailMessage,
    rule: Rule,
    source: str,
    session: requests.Session | None = None,
    warnings: list[str] | None = None,
) -> list[Invoice]:
    found: list[Invoice] = []
    when = message_date(msg)
    subject = str(msg.get("Subject", ""))

    if rule.attachments:
        for part in msg.iter_attachments():
            name = part.get_filename() or ""
            if name and rule.wanted_attachment(name):
                found.append(Invoice(rule.vendor, name, part.get_payload(decode=True),
                                     source, when, note=subject))

    if rule.links:
        for url in extract_links(msg):
            if not rule.links.search(url):
                continue
            try:
                got = download_pdf(url, session)
            except requests.RequestException as exc:
                got = None
                log.warning("download failed for %s: %s", url, exc)
            if got:
                found.append(Invoice(rule.vendor, got[0], got[1], source, when, note=subject))
            elif warnings is not None:
                warnings.append(
                    f"{rule.vendor}: link in '{subject}' ({when}) did not return a PDF "
                    f"(probably needs a login) - download it by hand: {url}"
                )
    return found


class ImapSource(Source):
    type = "imap"
    imap_factory = imaplib.IMAP4_SSL

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        for key in ("host", "username", "password"):
            if not cfg.get(key):
                raise ValueError(f"imap source {self.name!r}: '{key}' is required")
        self.folders: list[str] = as_list(cfg.get("folders") or "INBOX")
        self.rules = [Rule(r) for r in cfg.get("rules", [])]
        self.warnings: list[str] = []

    def required_vendors(self) -> list[str]:
        return [r.vendor for r in self.rules if r.required]

    def collect(self, period: Period) -> list[Invoice]:
        conn = self.imap_factory(self.cfg["host"], int(self.cfg.get("port", 993)))
        session = requests.Session()
        invoices: list[Invoice] = []
        try:
            conn.login(self.cfg["username"], self.cfg["password"])
            for folder in self.folders:
                invoices += self._collect_folder(conn, folder, period, session)
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001 - best effort
                pass
        return invoices

    def _collect_folder(self, conn, folder: str, period: Period,
                        session: requests.Session) -> list[Invoice]:
        status, _ = conn.select(quote(folder), readonly=True)
        if status != "OK":
            raise RuntimeError(f"cannot open folder {folder!r}")
        seen: set[bytes] = set()
        invoices: list[Invoice] = []
        for rule in self.rules:
            for uid in self._search(conn, rule, period):
                if uid in seen:
                    continue
                # Check headers first so broad rules don't download every message body.
                head = self._fetch(conn, uid, "BODY.PEEK[HEADER]")
                if head is None or not rule.matches(head):
                    continue
                msg = self._fetch(conn, uid, "BODY.PEEK[]")
                if msg is None:
                    continue
                seen.add(uid)
                invoices += invoices_from_message(
                    msg, rule, f"{self.name}/{folder}", session, self.warnings
                )
        return invoices

    @staticmethod
    def _search(conn, rule: Rule, period: Period) -> Iterable[bytes]:
        uids: dict[bytes, None] = {}
        for criteria in rule.search_criteria(period):
            status, data = conn.uid("SEARCH", None, *criteria)
            if status != "OK":
                raise RuntimeError(f"IMAP search failed: {criteria}")
            for uid in (data[0] or b"").split():
                uids[uid] = None
        return uids.keys()

    @staticmethod
    def _fetch(conn, uid: bytes, what: str) -> EmailMessage | None:
        status, data = conn.uid("FETCH", uid, f"({what})")  # PEEK: don't mark as read
        if status != "OK":
            return None
        for item in data:
            if isinstance(item, tuple):
                return email.message_from_bytes(item[1], policy=default_policy)
        return None
