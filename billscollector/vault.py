"""Passwords and tokens: macOS Keychain when available, else a private file.

Config files only ever contain ${NAME} placeholders; the values live here (or in
environment variables, which win - that's how GitHub Actions secrets work).
"""

from __future__ import annotations

import json
import os
import re

from . import paths

SERVICE = "BillsCollector"


def _keyring():
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring) or backend.priority < 1:
            return None
        return keyring
    except Exception:  # noqa: BLE001 - no usable backend
        return None


def _file():
    return paths.data_dir() / "secrets.json"


def _read_file() -> dict[str, str]:
    try:
        return json.loads(_file().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _write_file(data: dict[str, str]) -> None:
    f = _file()
    f.touch(mode=0o600, exist_ok=True)
    os.chmod(f, 0o600)
    f.write_text(json.dumps(data, indent=1))


def secret_name(*parts: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", "_".join(parts).upper()).strip("_")


def get(name: str) -> str | None:
    if name in os.environ:
        return os.environ[name]
    kr = _keyring()
    if kr:
        return kr.get_password(SERVICE, name)
    return _read_file().get(name)


def set(name: str, value: str) -> None:  # noqa: A001 - mirrors keyring API
    kr = _keyring()
    if kr:
        kr.set_password(SERVICE, name, value)
    else:
        data = _read_file()
        data[name] = value
        _write_file(data)


def delete(name: str) -> None:
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:  # noqa: BLE001 - already gone
            pass
    else:
        data = _read_file()
        if data.pop(name, None) is not None:
            _write_file(data)


def where() -> str:
    return "macOS Keychain" if _keyring() else str(_file())
