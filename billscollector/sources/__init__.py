from __future__ import annotations

from typing import Any

from .applemail import AppleMailSource
from .apis import HttpSource, PluginSource, StripeSource
from .base import Source
from .imap import ImapSource
from .revolut import RevolutSource

REGISTRY: dict[str, type[Source]] = {
    cls.type: cls for cls in (ImapSource, AppleMailSource, StripeSource, HttpSource, PluginSource, RevolutSource)
}


def build(cfg: dict[str, Any]) -> Source:
    kind = cfg.get("type")
    if kind not in REGISTRY:
        raise ValueError(f"unknown source type {kind!r} (known: {', '.join(REGISTRY)})")
    return REGISTRY[kind](cfg)
