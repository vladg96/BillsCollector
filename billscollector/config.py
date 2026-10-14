from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from . import vault

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    pass


def load(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    data["sources"] = data.get("sources") or []
    data["vendors"] = data.get("vendors") or []
    data["manual"] = data.get("manual") or []
    return data


def save(data: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)


def resolve(value: Any, where: str = "config") -> Any:
    """Replace ${NAME} with the env var or stored secret, recursively. Missing ones are an error."""
    if isinstance(value, str):
        names = _ENV.findall(value)
        found = {n: vault.get(n) for n in names}
        missing = [n for n, v in found.items() if v is None]
        if missing:
            raise ConfigError(f"{where}: password/secret not saved yet ({', '.join(missing)}). "
                              "Edit the connection and paste it again.")
        return _ENV.sub(lambda m: found[m.group(1)], value)
    if isinstance(value, list):
        return [resolve(v, where) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, where) for k, v in value.items()}
    return value
