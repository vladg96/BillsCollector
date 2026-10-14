"""Command line entry point.

    python -m billscollector run [--month 2026-09] [--config config.yaml] [--out out]
                                 [--no-send] [--force-send]
    python -m billscollector check [--config config.yaml]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import bundle, config, deliver, sources
from .models import Invoice, Period

log = logging.getLogger("billscollector")


def collect(cfg: dict, period: Period, report: bundle.RunReport) -> list[Invoice]:
    invoices: list[Invoice] = []
    required: list[str] = []
    for i, raw in enumerate(cfg["sources"]):
        if raw.get("enabled", True) is False:
            continue
        name = raw.get("name") or f"{raw.get('type')}#{i}"
        try:
            source = sources.build(config.resolve(raw, f"source {name!r}"))
            required += source.required_vendors()
            got = source.collect(period)
            report.warnings += getattr(source, "warnings", [])
            log.info("%s: %d file(s)", name, len(got))
            invoices += got
        except Exception as exc:  # one broken source must not sink the whole run
            log.exception("%s failed", name)
            report.errors[name] = f"{type(exc).__name__}: {exc}"
    found = {inv.vendor.lower() for inv in invoices}
    report.missing = sorted({v for v in required if v.lower() not in found}, key=str.lower)
    return invoices


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    period = Period.parse(args.month) if args.month else Period.previous()
    out = Path(args.out)
    report = bundle.RunReport(period)
    log.info("collecting invoices for %s", period)

    bundle.save(collect(cfg, period, report), period, out, report)
    zip_path = bundle.write_zip(report, out)
    summary = report.text()
    (out / f"summary-{period}.txt").write_text(summary, encoding="utf-8")
    print(summary)
    print(f"Bundle: {zip_path}")

    delivery = cfg.get("delivery")
    if args.no_send or not delivery:
        print("Not sending email (--no-send or no 'delivery' section).")
        return 0 if report.complete else 1
    delivery = config.resolve(delivery, "delivery")
    to_accountant = report.complete or args.force_send or delivery.get("on_incomplete") == "send"
    deliver.send(deliver.build_messages(report, delivery, to_accountant), delivery)
    print("Sent to accountant." if to_accountant else "Incomplete: sent the report to you only.")
    return 0 if report.complete else 1


def cmd_check(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    ok = True
    for i, raw in enumerate(cfg["sources"]):
        name = raw.get("name") or f"{raw.get('type')}#{i}"
        if raw.get("enabled", True) is False:
            print(f"-  {name}: disabled")
            continue
        try:
            src = sources.build(config.resolve(raw, f"source {name!r}"))
            print(f"ok {name} ({src.type}); expects: {', '.join(src.required_vendors()) or '-'}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"!! {name}: {exc}")
    if cfg.get("delivery"):
        try:
            d = config.resolve(cfg["delivery"], "delivery")
            missing = [k for k in ("smtp_host", "smtp_username", "smtp_password", "from", "to")
                       if not d.get(k)]
            if missing:
                raise config.ConfigError(f"delivery: missing {', '.join(missing)}")
            print(f"ok delivery -> {d['to']}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"!! {exc}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="billscollector")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="collect a month and send it")
    run.add_argument("--month", help="YYYY-MM (default: last month)")
    run.add_argument("--out", default="out")
    run.add_argument("--no-send", action="store_true", help="collect only, don't email")
    run.add_argument("--force-send", action="store_true",
                     help="email the accountant even if something is missing")
    run.set_defaults(func=cmd_run)
    sub.add_parser("check", help="validate config and secrets").set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    sys.path.insert(0, str(Path.cwd()))  # so plugin modules in ./collectors import
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
