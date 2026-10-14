"""One month's folder: collected + hand-added files, what's missing, what was sent."""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .bundle import RunReport
from .models import Period, slug


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


_STOP = {"srl", "ltd", "inc", "llc", "gmbh", "limited", "romania", "com", "www", "the", "and",
         "services", "service", "payment", "payments", "online", "store", "shop", "company"}


def _words(s: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", s.lower()) if len(w) >= 3 and w not in _STOP}


def similar(merchant: str, name: str) -> bool:
    """'GOOGLE *GSUITE_OH' ~ 'Google Workspace', 'Digi Romania SA' ~ 'Digi'."""
    a, b = _words(merchant), _words(name)
    return bool(a & b) or (bool(a) and bool(b) and
                           any(x in y or y in x for x in a for y in b if min(len(x), len(y)) >= 4))


@dataclass
class Entry:
    name: str
    path: Path
    vendor: str
    date: str | None
    source: str
    kind: str  # "auto" (collected) or "manual" (added by hand)
    item: str | None = None


class Month:
    def __init__(self, root: Path, period: Period):
        self.period = period
        self.folder = Path(root) / str(period)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._manifest = self.folder / "manifest.json"
        try:
            self.data: dict[str, Any] = json.loads(self._manifest.read_text())
        except (FileNotFoundError, ValueError):
            self.data = {}
        self.data.setdefault("files", {})
        self.data.setdefault("run", None)
        self.data.setdefault("sent", [])

    def save(self) -> None:
        tmp = self._manifest.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, default=str))
        tmp.replace(self._manifest)

    # ---- files -------------------------------------------------------------
    def entries(self) -> list[Entry]:
        out = []
        for name, meta in self.data["files"].items():
            path = self.folder / name
            if path.exists():
                out.append(Entry(name, path, meta.get("vendor", ""), meta.get("date"),
                                 meta.get("source", ""), meta.get("kind", "auto"), meta.get("item")))
        return sorted(out, key=lambda e: (e.vendor.lower(), e.name))

    def record_run(self, report: RunReport) -> None:
        for s in report.saved:
            self.data["files"][s.path.name] = {
                "vendor": s.invoice.vendor, "source": s.invoice.source, "kind": "auto",
                "date": s.invoice.date.isoformat() if s.invoice.date else None,
            }
        self.data["run"] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "required": sorted(set(report.required), key=str.lower),
            "errors": report.errors,
            "warnings": report.warnings,
            "found": len(report.saved),
            "payments": report.payments,
            "duplicates": report.duplicates,
        }
        self.save()

    def add_file(self, filename: str, content: bytes, vendor: str,
                 item: dict[str, Any] | None = None) -> str:
        ext = Path(filename).suffix.lower() or ".pdf"
        if item:  # one file per checklist item: replace the previous upload
            for name, meta in list(self.data["files"].items()):
                if meta.get("item") == item["id"]:
                    self.remove(name)
            name = f"{self.period}_{slug(vendor)}_{slug(item['label'])}{ext}"
        else:
            name = f"{self.period}_{slug(vendor)}_{slug(Path(filename).stem)}{ext}"
        stem, n = Path(name).stem, 2
        while (self.folder / name).exists():
            name = f"{stem}-{n}{ext}"
            n += 1
        (self.folder / name).write_bytes(content)
        self.data["files"][name] = {"vendor": vendor, "source": "added by hand", "kind": "manual",
                                    "date": None, "item": item["id"] if item else None}
        self.save()
        return name

    def remove(self, name: str) -> None:
        if name in self.data["files"]:
            del self.data["files"][name]
            (self.folder / name).unlink(missing_ok=True)
            self.save()

    # ---- status ------------------------------------------------------------
    def missing_vendors(self) -> list[str]:
        run = self.data["run"] or {}
        have = {e.vendor.lower() for e in self.entries()}
        return [v for v in run.get("required", []) if v.lower() not in have]

    def missing_items(self, manual: list[dict[str, Any]]) -> list[dict[str, Any]]:
        done = {e.item for e in self.entries() if e.item}
        return [i for i in manual if i.get("id") not in done]

    def unmatched_payments(self, vendors: list[dict[str, Any]],
                           ignore: list[str]) -> list[dict[str, Any]]:
        """Card payments with no file from a vendor of a similar name."""
        names = [e.vendor for e in self.entries()]
        aliases = {v["vendor"]: [v["vendor"]] + _as_list(v.get("merchant")) for v in vendors}
        out = []
        for p in (self.data["run"] or {}).get("payments", []):
            merchant = p.get("merchant", "")
            if any(similar(merchant, i) for i in ignore):
                continue
            if any(similar(merchant, alias) for n in names for alias in aliases.get(n, [n])):
                continue
            out.append(p)
        return out

    def complete(self, manual: list[dict[str, Any]]) -> bool:
        run = self.data["run"]
        return bool(run and self.entries() and not run["errors"]
                    and not self.missing_vendors() and not self.missing_items(manual))

    def summary(self, manual: list[dict[str, Any]], owner_view: bool = True) -> str:
        entries = self.entries()
        lines = [f"Invoices for {self.period.label}: {len(entries)} file(s).", ""]
        for e in entries:
            lines.append(f"  {(e.date or ''):<10}  {e.vendor:<24} {e.name}")
        if owner_view:
            run = self.data["run"] or {}
            sections = [
                ("MISSING (expected every month, nothing found):", self.missing_vendors()),
                ("STILL TO ADD BY HAND:",
                 [f"{i['vendor']}: {i['label']}" for i in self.missing_items(manual)]),
                ("SOURCES THAT FAILED:", [f"{k}: {v}" for k, v in run.get("errors", {}).items()]),
                ("NEEDS A HAND:", run.get("warnings", [])),
            ]
            for title, items in sections:
                if items:
                    lines += ["", title] + [f"  - {x}" for x in items]
        return "\n".join(lines) + "\n"

    def record_sent(self, method: str, to: str) -> None:
        self.data["sent"].append({"at": datetime.now().isoformat(timespec="seconds"),
                                  "method": method, "to": to})
        self.save()

    def write_zip(self, manual: list[dict[str, Any]], target: Path | None = None) -> Path:
        target = target or self.folder.parent / f"invoices-{self.period}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for e in self.entries():
                zf.write(e.path, e.name)
            zf.writestr("SUMMARY.txt", self.summary(manual))
        return target
