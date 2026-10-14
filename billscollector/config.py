from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    pass


def load(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    data.setdefault("sources", [])
    return data


def resolve(value: Any, where: str = "config") -> Any:
    """Replace ${VAR} with environment values, recursively. Missing vars are an error."""
    if isinstance(value, str):
        missing = [n for n in _ENV.findall(value) if n not in os.environ]
        if missing:
            raise ConfigError(f"{where}: environment variable(s) not set: {', '.join(missing)}")
        return _ENV.sub(lambda m: os.environ[m.group(1)], value)
    if isinstance(value, list):
        return [resolve(v, where) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, where) for k, v in value.items()}
    return value
