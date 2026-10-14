"""API-based sources: Stripe, a generic HTTP download, and custom Python plugins."""

from __future__ import annotations

import importlib
from datetime import datetime, time, timezone
from typing import Any

import requests

from ..models import Invoice, Period
from .base import USER_AGENT, Source, download_pdf


def _epoch(d) -> int:
    return int(datetime.combine(d, time.min, tzinfo=timezone.utc).timestamp())


class StripeSource(Source):
    """Invoices *you issued* through Stripe in the period (your accountant wants those too).

    cfg: api_key, vendor (label, default "Stripe"), statuses (default [paid, open])
    """

    type = "stripe"
    api = "https://api.stripe.com/v1/invoices"

    def required_vendors(self) -> list[str]:
        # Some months you may issue nothing; only enforce when asked to.
        return [self.vendor] if self.cfg.get("required", False) else []

    @property
    def vendor(self) -> str:
        return self.cfg.get("vendor", "Stripe")

    def collect(self, period: Period) -> list[Invoice]:
        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {self.cfg['api_key']}",
                                "User-Agent": USER_AGENT})
        statuses = set(self.cfg.get("statuses", ["paid", "open"]))
        params: dict[str, Any] = {
            "created[gte]": _epoch(period.start),
            "created[lt]": _epoch(period.end),
            "limit": 100,
        }
        invoices: list[Invoice] = []
        while True:
            resp = session.get(self.api, params=params, timeout=60)
            resp.raise_for_status()
            page = resp.json()
            for inv in page["data"]:
                if inv.get("status") not in statuses or not inv.get("invoice_pdf"):
                    continue
                got = download_pdf(inv["invoice_pdf"])
                if got:
                    number = inv.get("number") or inv["id"]
                    when = datetime.fromtimestamp(inv["created"], timezone.utc).date()
                    invoices.append(Invoice(self.vendor, f"{number}.pdf", got[1], self.name,
                                            when, note=inv.get("customer_name") or ""))
            if not page.get("has_more"):
                return invoices
            params["starting_after"] = page["data"][-1]["id"]


class HttpSource(Source):
    """Download a PDF from a URL that depends on the month.

    cfg: vendor, url, headers (optional). url/headers may use {year}, {month},
    {start}, {end} (ISO dates), e.g. https://api.example.com/invoices/{year}-{month:02d}.pdf
    """

    type = "http"

    def collect(self, period: Period) -> list[Invoice]:
        fields = {"year": period.year, "month": period.month,
                  "start": period.start.isoformat(), "end": period.end.isoformat()}
        url = self.cfg["url"].format(**fields)
        headers = {k: str(v).format(**fields) for k, v in (self.cfg.get("headers") or {}).items()}
        got = download_pdf(url, headers=headers)
        if not got:
            raise RuntimeError(f"{url} did not return a PDF")
        return [Invoice(self.cfg["vendor"], got[0], got[1], self.name, period.start)]


class PluginSource(Source):
    """Run your own collector: `module.function(period, cfg) -> list[Invoice]`.

    Use this for APIs with their own auth flow or for portals that need a
    browser login (see collectors/example_portal.py).
    cfg: module, function (default "collect"), vendors (required each month)
    """

    type = "plugin"

    def required_vendors(self) -> list[str]:
        if not self.cfg.get("required", True):
            return []
        return list(self.cfg.get("vendors") or ([self.cfg["vendor"]] if "vendor" in self.cfg else []))

    def collect(self, period: Period) -> list[Invoice]:
        module = importlib.import_module(self.cfg["module"])
        func = getattr(module, self.cfg.get("function", "collect"))
        return list(func(period, self.cfg))
