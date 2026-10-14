from __future__ import annotations

import calendar
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class Period:
    """A calendar month. `end` is exclusive."""

    year: int
    month: int

    @classmethod
    def parse(cls, value: str) -> "Period":
        m = re.fullmatch(r"(\d{4})-(\d{1,2})", value.strip())
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise ValueError(f"Expected YYYY-MM, got {value!r}")
        return cls(int(m.group(1)), int(m.group(2)))

    @classmethod
    def previous(cls, today: date | None = None) -> "Period":
        first = (today or date.today()).replace(day=1)
        last_month = first - timedelta(days=1)
        return cls(last_month.year, last_month.month)

    @property
    def start(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def end(self) -> date:
        days = calendar.monthrange(self.year, self.month)[1]
        return self.start + timedelta(days=days)

    @property
    def label(self) -> str:
        return f"{calendar.month_name[self.month]} {self.year}"

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass
class Invoice:
    vendor: str
    filename: str
    content: bytes
    source: str
    date: date | None = None
    note: str = ""
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.sha256 = hashlib.sha256(self.content).hexdigest()


def slug(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.ASCII).strip("-.")
    return value or "file"
