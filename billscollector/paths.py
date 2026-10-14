from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    """Where the app keeps its config, secrets fallback and collected invoices."""
    if os.environ.get("BILLS_HOME"):
        path = Path(os.environ["BILLS_HOME"]).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "BillsCollector"
    else:
        path = Path.home() / ".billscollector"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config() -> Path:
    if os.environ.get("BILLS_CONFIG"):
        return Path(os.environ["BILLS_CONFIG"]).expanduser()
    local = Path("config.yaml")
    return local if local.exists() else data_dir() / "config.yaml"


def invoices_dir() -> Path:
    path = data_dir() / "invoices"
    path.mkdir(parents=True, exist_ok=True)
    return path
