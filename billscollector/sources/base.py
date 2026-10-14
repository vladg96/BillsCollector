from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from ..models import Invoice, Period

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) BillsCollector/0.1"


class Source:
    """A place invoices come from. Subclasses implement `collect`."""

    type = "base"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.name = cfg.get("name") or self.type

    def collect(self, period: Period) -> list[Invoice]:
        raise NotImplementedError

    def required_vendors(self) -> list[str]:
        """Vendors that must produce at least one invoice every month."""
        vendor = self.cfg.get("vendor")
        return [vendor] if vendor and self.cfg.get("required", True) else []


def looks_like_pdf(content: bytes) -> bool:
    return content[:1024].lstrip().startswith(b"%PDF")


def filename_from_response(resp: requests.Response, fallback: str = "invoice.pdf") -> str:
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or re.search(
        r'filename="?([^";]+)"?', cd, re.I
    )
    if m:
        return unquote(m.group(1).strip().strip('"'))
    tail = unquote(urlparse(resp.url).path.rsplit("/", 1)[-1])
    if tail and "." in tail:
        return tail
    return fallback


def download_pdf(
    url: str,
    session: requests.Session | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[str, bytes] | None:
    """Fetch `url`; return (filename, bytes) if the response is a PDF, else None."""
    session = session or requests.Session()
    resp = session.get(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}, timeout=timeout,
        allow_redirects=True,
    )
    resp.raise_for_status()
    if not looks_like_pdf(resp.content):
        return None
    name = filename_from_response(resp)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name, resp.content
