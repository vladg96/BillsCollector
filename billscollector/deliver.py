"""Get the month's files to the accountant: an Apple Mail draft you send, or SMTP."""

from __future__ import annotations

import logging
import smtplib
import subprocess
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from . import config
from .models import Period

log = logging.getLogger(__name__)

# IMAP host -> (SMTP host, port), so a mailbox connection can also send.
SMTP_FOR_IMAP = {
    "imap.gmail.com": ("smtp.gmail.com", 465),
    "imap.mail.yahoo.com": ("smtp.mail.yahoo.com", 465),
    "outlook.office365.com": ("smtp.office365.com", 587),
    "imap.mail.me.com": ("smtp.mail.me.com", 587),
}

DEFAULT_INTRO = "Hi,\n\nPlease find attached the invoices for {month}.\n\nThanks!\n"


def chunk(paths: list[Path], max_bytes: int) -> list[list[Path]]:
    """Split attachments into groups that each fit in one email."""
    groups: list[list[Path]] = [[]]
    size = 0
    for path in sorted(paths, key=lambda p: p.name):
        n = path.stat().st_size
        if groups[-1] and size + n > max_bytes:
            groups.append([])
            size = 0
        groups[-1].append(path)
        size += n
    return groups


def _list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value)


def subject(period: Period, d: dict[str, Any], part: int = 1, parts: int = 1) -> str:
    name = d.get("name", "")
    text = f"Invoices {period.label}" + (f" - {name}" if name else "")
    return text + (f" ({part}/{parts})" if parts > 1 else "")


def body(period: Period, d: dict[str, Any], file_list: str) -> str:
    intro = (d.get("intro") or DEFAULT_INTRO).replace("{month}", period.label)
    return intro.rstrip() + "\n\n" + file_list


def max_bytes(d: dict[str, Any]) -> int:
    return int(float(d.get("max_attachment_mb", 18)) * 1024 * 1024)


def compose(period: Period, paths: list[Path], d: dict[str, Any], to_accountant: bool,
            text: str) -> list[EmailMessage]:
    sender = d.get("from") or d.get("owner")
    owner = d.get("owner") or sender
    if to_accountant:
        recipients, cc = _list(d["to"]), _list(d.get("cc", owner))
        main_body = body(period, d, text)
        subj = lambda i, n: subject(period, d, i, n)  # noqa: E731
    else:
        recipients, cc = [owner], []
        main_body = ("Some invoices are missing or a source failed, so nothing was sent to "
                     "your accountant. Everything found so far is attached.\n\n" + text)
        subj = lambda i, n: (f"[Action needed] Invoices {period.label}"  # noqa: E731
                             + (f" ({i}/{n})" if n > 1 else ""))
    groups = chunk(paths, max_bytes(d)) if paths else [[]]
    messages = []
    for i, group in enumerate(groups, 1):
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subj(i, len(groups))
        msg.set_content(main_body if i == 1 else f"Part {i} of {len(groups)}.\n")
        for path in group:
            msg.add_attachment(path.read_bytes(), maintype="application",
                               subtype="pdf" if path.suffix.lower() == ".pdf" else "octet-stream",
                               filename=path.name)
        messages.append(msg)
    return messages


def smtp_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """SMTP details from the delivery section, or borrowed from a mailbox connection."""
    d = config.resolve(cfg.get("delivery") or {}, "delivery")
    if d.get("smtp_source"):
        raw = next((s for s in cfg["sources"] if s.get("id") == d["smtp_source"]), None)
        if raw is None:
            raise config.ConfigError(f"delivery: no connection with id {d['smtp_source']!r}")
        src = config.resolve(raw, f"source {d['smtp_source']!r}")
        host, port = SMTP_FOR_IMAP.get(src.get("host", ""), (None, None))
        if not host:
            raise config.ConfigError(f"don't know the SMTP server for {src.get('host')}")
        d = {"smtp_host": host, "smtp_port": port, "smtp_username": src["username"],
             "smtp_password": src["password"], **{k: v for k, v in d.items() if v}}
        d.setdefault("from", src["username"])
    return d


def send_smtp(messages: list[EmailMessage], d: dict[str, Any]) -> None:
    host, port = d["smtp_host"], int(d.get("smtp_port", 465))
    smtp = smtplib.SMTP_SSL(host, port) if port == 465 else smtplib.SMTP(host, port)
    with smtp:
        if port != 465:
            smtp.starttls()
        smtp.login(d["smtp_username"], d["smtp_password"])
        for msg in messages:
            smtp.send_message(msg)
            log.info("sent %r to %s %s", msg["Subject"], msg["To"], msg.get("Cc", ""))


# Arguments are passed via argv, so nothing needs escaping inside the script.
_APPLESCRIPT = r'''
on splitText(t, d)
    set AppleScript's text item delimiters to d
    set parts to text items of t
    set AppleScript's text item delimiters to ""
    return parts
end splitText

on run argv
    set theSubject to item 1 of argv
    set theBody to item 2 of argv
    set toList to my splitText(item 3 of argv, ",")
    set ccList to my splitText(item 4 of argv, ",")
    tell application "Mail"
        set m to make new outgoing message with properties {subject:theSubject, content:theBody & return & return, visible:true}
        tell m
            repeat with a in toList
                if length of a > 0 then make new to recipient at end of to recipients with properties {address:a}
            end repeat
            repeat with a in ccList
                if length of a > 0 then make new cc recipient at end of cc recipients with properties {address:a}
            end repeat
            repeat with i from 5 to count of argv
                make new attachment with properties {file name:(POSIX file (item i of argv)) as alias} at after the last paragraph
                delay 0.2
            end repeat
        end tell
        activate
    end tell
end run
'''


def apple_mail_drafts(period: Period, paths: list[Path], d: dict[str, Any], text: str) -> int:
    """Open ready-to-send drafts in Apple Mail. Returns how many were created."""
    groups = chunk(paths, max_bytes(d)) if paths else [[]]
    for i, group in enumerate(groups, 1):
        content = body(period, d, text) if i == 1 else f"Part {i} of {len(groups)}."
        args = [subject(period, d, i, len(groups)), content,
                ",".join(_list(d["to"])), ",".join(_list(d.get("cc")))]
        args += [str(p.resolve()) for p in group]
        subprocess.run(["osascript", "-", *args], input=_APPLESCRIPT, text=True,
                       check=True, capture_output=True)
    return len(groups)
