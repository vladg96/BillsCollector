"""Email the bundle: to the accountant when complete, to you when something is missing."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from .bundle import RunReport, Saved

log = logging.getLogger(__name__)


def chunk(saved: list[Saved], max_bytes: int) -> list[list[Saved]]:
    """Split attachments into groups that each fit in one email."""
    groups: list[list[Saved]] = [[]]
    size = 0
    for s in sorted(saved, key=lambda s: s.path.name):
        n = s.path.stat().st_size
        if groups[-1] and size + n > max_bytes:
            groups.append([])
            size = 0
        groups[-1].append(s)
        size += n
    return groups


def build_messages(report: RunReport, cfg: dict[str, Any], to_accountant: bool) -> list[EmailMessage]:
    sender = cfg["from"]
    owner = cfg.get("owner") or sender
    name = cfg.get("name", "")
    if to_accountant:
        recipients, cc = _list(cfg["to"]), _list(cfg.get("cc", owner))
        subject = f"Invoices {report.period.label}" + (f" - {name}" if name else "")
        intro = cfg.get("intro", "Hi,\n\nPlease find attached this month's invoices.\n")
        body = intro + "\n" + report.text(owner_view=False)
    else:
        recipients, cc = [owner], []
        subject = f"[Action needed] Invoices {report.period.label} not sent to accountant"
        body = ("Some invoices are missing or a source failed, so nothing was sent to "
                "your accountant.\nEverything found so far is attached. Fix what is listed "
                "below and re-run the workflow (or forward this mail yourself).\n\n"
                + report.text(owner_view=True))

    max_bytes = int(float(cfg.get("max_attachment_mb", 18)) * 1024 * 1024)
    groups = chunk(report.saved, max_bytes) if report.saved else [[]]
    messages = []
    for i, group in enumerate(groups, 1):
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject + (f" ({i}/{len(groups)})" if len(groups) > 1 else "")
        msg.set_content(body if i == 1 else f"Part {i} of {len(groups)}.\n")
        for s in group:
            msg.add_attachment(s.path.read_bytes(), maintype="application",
                               subtype="pdf" if s.path.suffix.lower() == ".pdf" else "octet-stream",
                               filename=s.path.name)
        messages.append(msg)
    return messages


def send(messages: list[EmailMessage], cfg: dict[str, Any]) -> None:
    host, port = cfg["smtp_host"], int(cfg.get("smtp_port", 465))
    smtp = smtplib.SMTP_SSL(host, port) if port == 465 else smtplib.SMTP(host, port)
    with smtp:
        if port != 465:
            smtp.starttls()
        smtp.login(cfg["smtp_username"], cfg["smtp_password"])
        for msg in messages:
            smtp.send_message(msg)
            log.info("sent %r to %s %s", msg["Subject"], msg["To"], msg.get("Cc", ""))


def _list(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else list(value or [])
