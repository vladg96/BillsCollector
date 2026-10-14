"""Log into a billing portal with a headless browser and download the month's invoice.

Works for the common "log in -> billing page -> list of invoices with download
links" pattern, driven entirely by config. For anything more unusual, copy this
file and adjust `collect`.

    - type: plugin
      name: electricity-portal
      module: collectors.portal
      vendor: Iberdrola
      login_url: https://example.com/login
      username: ${IBERDROLA_USER}
      password: ${IBERDROLA_PASSWORD}
      username_selector: "input[name=email]"
      password_selector: "input[type=password]"
      submit_selector: "button[type=submit]"
      invoices_url: https://example.com/account/invoices
      # A row/link on the invoices page that mentions the month. Placeholders:
      # {year} {month} {mm} {month_name} {mon}  -> 2026 9 09 September Sep
      row_text: "{mon} {year}"
      download_selector: "a:has-text('PDF')"

Caveat: portals that demand a 2FA code on every login can't run unattended.
List those in a mail rule instead (forward the invoice to yourself), or keep
them as `required: false` and grab them by hand when the report asks.
"""

from __future__ import annotations

import calendar
from typing import Any

from billscollector.models import Invoice, Period


def _placeholders(period: Period) -> dict[str, Any]:
    return {"year": period.year, "month": period.month, "mm": f"{period.month:02d}",
            "month_name": calendar.month_name[period.month],
            "mon": calendar.month_abbr[period.month]}


def collect(period: Period, cfg: dict[str, Any]) -> list[Invoice]:
    from playwright.sync_api import sync_playwright  # optional dependency

    row_text = cfg["row_text"].format(**_placeholders(period))
    invoices: list[Invoice] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(accept_downloads=True).new_page()
        page.set_default_timeout(int(cfg.get("timeout_ms", 30000)))

        page.goto(cfg["login_url"])
        page.fill(cfg["username_selector"], cfg["username"])
        page.fill(cfg["password_selector"], cfg["password"])
        page.click(cfg["submit_selector"])
        page.wait_for_load_state("networkidle")

        page.goto(cfg["invoices_url"])
        page.wait_for_load_state("networkidle")
        rows = page.locator(cfg.get("row_selector", "tr, li, div[role=row]")).filter(has_text=row_text)
        for i in range(rows.count()):
            link = rows.nth(i).locator(cfg["download_selector"]).first
            if not link.count():
                continue
            with page.expect_download() as info:
                link.click()
            download = info.value
            invoices.append(Invoice(cfg["vendor"], download.suggested_filename,
                                    open(download.path(), "rb").read(),
                                    cfg.get("name", "portal"), period.start))
        browser.close()
    return invoices
