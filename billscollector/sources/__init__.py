from __future__ import annotations

from typing import Any

from .apis import HttpSource, PluginSource, StripeSource
from .base import Source
from .imap import ImapSource

REGISTRY: dict[str, type[Source]] = {
    cls.type: cls for cls in (ImapSource, StripeSource, HttpSource, PluginSource)
}


def build(cfg: dict[str, Any]) -> Source:
    kind = cfg.get("type")
    if kind not in REGISTRY:
        raise ValueError(f"unknown source type {kind!r} (known: {', '.join(REGISTRY)})")
    return REGISTRY[kind](cfg)
