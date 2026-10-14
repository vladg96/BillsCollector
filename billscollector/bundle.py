"""Deduplicate, name and save the collected invoices, and summarise the run."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .models import Invoice, Period, slug


@dataclass
class Saved:
    invoice: Invoice
    path: Path


@dataclass
class RunReport:
    period: Period
    saved: list[Saved] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    required: list[str] = field(default_factory=list)
    duplicates: int = 0

    @property
    def complete(self) -> bool:
        return bool(self.saved) and not (self.missing or self.errors)

    def text(self, owner_view: bool = True) -> str:
        lines = [f"Invoices for {self.period.label}: {len(self.saved)} file(s).", ""]
        for s in sorted(self.saved, key=lambda s: (s.invoice.vendor.lower(), s.path.name)):
            when = s.invoice.date.isoformat() if s.invoice.date else "          "
            lines.append(f"  {when}  {s.invoice.vendor:<24} {s.path.name}")
        if owner_view:
            if self.missing:
                lines += ["", "MISSING (expected every month, nothing found):"]
                lines += [f"  - {v}" for v in self.missing]
            if self.errors:
                lines += ["", "SOURCES THAT FAILED:"]
                lines += [f"  - {name}: {err}" for name, err in self.errors.items()]
            if self.warnings:
                lines += ["", "NEEDS A HAND:"]
                lines += [f"  - {w}" for w in self.warnings]
            if self.duplicates:
                lines += ["", f"({self.duplicates} duplicate file(s) skipped)"]
        return "\n".join(lines) + "\n"


def file_name(inv: Invoice, period: Period) -> str:
    when = inv.date.isoformat() if inv.date else str(period)
    name = slug(inv.filename)
    if "." not in name:
        name += ".pdf"
    return f"{when}_{slug(inv.vendor)}_{name}"


def save(invoices: list[Invoice], period: Period, out_dir: Path, report: RunReport) -> None:
    folder = out_dir / str(period)
    folder.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for inv in invoices:
        if inv.sha256 in seen:
            report.duplicates += 1
            continue
        seen.add(inv.sha256)
        path = folder / file_name(inv, period)
        stem, suffix, n = path.stem, path.suffix, 2
        taken = {s.path for s in report.saved}
        # Re-running into the same folder reuses identical files instead of adding copies.
        while path in taken or (path.exists() and path.read_bytes() != inv.content):
            path = folder / f"{stem}-{n}{suffix}"
            n += 1
        path.write_bytes(inv.content)
        report.saved.append(Saved(inv, path))


def write_zip(report: RunReport, out_dir: Path) -> Path:
    target = out_dir / f"invoices-{report.period}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in report.saved:
            zf.write(s.path, s.path.name)
        zf.writestr("SUMMARY.txt", report.text())
    return target
