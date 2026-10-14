"""Run every enabled source for a month and file the results."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import bundle, config, sources
from .models import Invoice, Period
from .month import Month

log = logging.getLogger(__name__)

MAIL_TYPES = ("imap", "applemail")


def source_label(raw: dict[str, Any], index: int) -> str:
    return raw.get("name") or raw.get("id") or f"{raw.get('type')}#{index}"


def build_source(cfg: dict[str, Any], raw: dict[str, Any], index: int = 0) -> sources.Source:
    """Resolve secrets and give mailboxes the shared vendor rules."""
    raw = dict(raw)
    if raw.get("type") in MAIL_TYPES and not raw.get("rules"):
        raw["rules"] = cfg.get("vendors", [])
    raw.setdefault("name", source_label(raw, index))
    return sources.build(config.resolve(raw, f"source {raw['name']!r}"))


def collect(cfg: dict[str, Any], period: Period, report: bundle.RunReport) -> list[Invoice]:
    invoices: list[Invoice] = []
    for i, raw in enumerate(cfg["sources"]):
        if raw.get("enabled", True) is False:
            continue
        name = source_label(raw, i)
        try:
            source = build_source(cfg, raw, i)
            report.required += source.required_vendors()
            got = source.collect(period)
            report.warnings += getattr(source, "warnings", [])
            log.info("%s: %d file(s)", name, len(got))
            invoices += got
        except Exception as exc:  # one broken source must not sink the whole run
            log.exception("%s failed", name)
            report.errors[name] = f"{type(exc).__name__}: {exc}"
    report.required = sorted(set(report.required), key=str.lower)
    found = {inv.vendor.lower() for inv in invoices}
    report.missing = [v for v in report.required if v.lower() not in found]
    return invoices


def run_month(cfg: dict[str, Any], period: Period, root: Path) -> Month:
    report = bundle.RunReport(period)
    invoices = collect(cfg, period, report)
    bundle.save(invoices, period, Path(root), report)
    month = Month(root, period)
    month.record_run(report)
    return month
