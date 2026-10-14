"""Read invoices straight from Apple Mail's local storage (~/Library/Mail).

Covers every account configured in Mail.app with no extra sign-in. macOS
protects this folder, so the app running the collector (Terminal, iTerm, or
Python) needs Full Disk Access: System Settings -> Privacy & Security ->
Full Disk Access.

Mail stores each message as an .emlx file: a byte count line, the raw RFC 822
message, then an XML plist. Messages whose attachments were downloaded
separately are `.partial.emlx`, with the files in a sibling Attachments/ folder.
"""

from __future__ import annotations

import email
import logging
import os
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path
from typing import Any, Iterator

import requests

from ..models import Invoice, Period
from .base import Source
from .imap import Rule, invoices_from_message, message_date

log = logging.getLogger(__name__)

FULL_DISK_ACCESS_HELP = (
    "macOS blocked access to {root}. Open System Settings -> Privacy & Security -> "
    "Full Disk Access and turn it on for the app that runs BillsCollector "
    "(Terminal/iTerm, or the Python it uses), then try again."
)


def parse_emlx(data: bytes) -> EmailMessage:
    first, _, rest = data.partition(b"\n")
    try:
        length = int(first.strip())
        raw = rest[:length]
    except ValueError:  # not an emlx header, treat as a plain message
        raw = data
    return email.message_from_bytes(raw, policy=default_policy)


def partial_attachments(emlx: Path) -> Iterator[Path]:
    """Files Mail stored separately for a .partial.emlx message."""
    number = emlx.name.split(".", 1)[0]
    folder = emlx.parent.parent / "Attachments" / number
    if folder.is_dir():
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                yield path


class AppleMailSource(Source):
    type = "applemail"

    def __init__(self, cfg: dict[str, Any]):
        super().__init__(cfg)
        self.root = Path(cfg.get("path") or "~/Library/Mail").expanduser()
        self.rules = [Rule(r) for r in cfg.get("rules", [])]
        self.warnings: list[str] = []

    def required_vendors(self) -> list[str]:
        return [r.vendor for r in self.rules if r.required]

    def check(self) -> str:
        """Used by the UI's Test button."""
        count = sum(1 for _ in self._emlx_files(None))
        return f"found {count} messages in {self.root}"

    def _emlx_files(self, modified_since: float | None) -> Iterator[Path]:
        try:
            os.listdir(self.root)
        except PermissionError as exc:
            raise PermissionError(FULL_DISK_ACCESS_HELP.format(root=self.root)) from exc
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"{self.root} not found - is Apple Mail set up?") from exc
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                if not name.endswith(".emlx"):
                    continue
                path = Path(dirpath) / name
                if modified_since is not None:
                    try:
                        if path.stat().st_mtime < modified_since:
                            continue
                    except OSError:
                        continue
                yield path

    def collect(self, period: Period) -> list[Invoice]:
        # A message received in the period was written to disk no earlier than its
        # start (Mail rewrites flags later, so there's no useful upper bound).
        since = datetime.combine(period.start - timedelta(days=2), datetime.min.time()).timestamp()
        latest_extend = max((r.extend_days for r in self.rules), default=0)
        session = requests.Session()
        seen_ids: set[str] = set()  # the same message can sit in several mailboxes
        invoices: list[Invoice] = []
        for path in self._emlx_files(since):
            try:
                msg = parse_emlx(path.read_bytes())
            except Exception as exc:  # noqa: BLE001 - one bad file shouldn't stop the run
                log.debug("skip %s: %s", path, exc)
                continue
            when = message_date(msg)
            if when is None or not period.start <= when < period.end + timedelta(days=latest_extend):
                continue
            msg_id = str(msg.get("Message-ID", "")) or str(path)
            if msg_id in seen_ids:
                continue
            for rule in self.rules:
                if not period.start <= when < period.end + timedelta(days=rule.extend_days):
                    continue
                if not rule.matches(msg):
                    continue
                seen_ids.add(msg_id)
                found = invoices_from_message(msg, rule, f"{self.name}", session, self.warnings)
                if path.name.endswith(".partial.emlx") and rule.attachments:
                    for att in partial_attachments(path):
                        if rule.wanted_attachment(att.name):
                            found.append(Invoice(rule.vendor, att.name, att.read_bytes(),
                                                 self.name, when, note=str(msg.get("Subject", ""))))
                invoices += found
                break
        return invoices
