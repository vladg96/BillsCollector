"""Command line entry point.

    python -m billscollector ui                      # open the app in your browser
    python -m billscollector run [--month 2026-09]   # collect (and send, per settings)
    python -m billscollector check                   # validate connections config
    python -m billscollector schedule install|remove # monthly run on this Mac
"""

from __future__ import annotations

import argparse
import logging
import plistlib
import subprocess
import sys
from pathlib import Path

from . import config, deliver, paths, runner
from .models import Period

log = logging.getLogger("billscollector")
LAUNCH_LABEL = "io.billscollector.monthly"


def notify(title: str, message: str) -> None:
    print(f"{title}: {message}")
    if sys.platform == "darwin":
        script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
        subprocess.run(["osascript", "-e", script, title, message], check=False)


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    period = Period.parse(args.month) if args.month else Period.previous()
    root = Path(args.out) if args.out else paths.invoices_dir()
    log.info("collecting invoices for %s", period)

    month = runner.run_month(cfg, period, root)
    manual = cfg["manual"]
    complete = month.complete(manual)
    summary = month.summary(manual)
    (root / f"summary-{period}.txt").write_text(summary, encoding="utf-8")
    zip_path = month.write_zip(manual)
    print(summary)
    print(f"Bundle: {zip_path}")

    d = cfg.get("delivery") or {}
    if args.no_send or not d.get("to"):
        print("Not sending (--no-send or no accountant address set).")
        return 0 if complete else 1

    files = [e.path for e in month.entries()]
    accountant_text = month.summary(manual, owner_view=False)
    if d.get("method", "smtp") == "applemail":
        if args.scheduled:
            what = "ready to send" if complete else "needs your attention"
            notify("BillsCollector", f"{period.label}: {len(files)} invoice(s), {what}. "
                                     "Open the app to review and send.")
        else:
            n = deliver.apple_mail_drafts(period, files, config.resolve(d, "delivery"),
                                          accountant_text)
            month.record_sent("applemail draft", str(d["to"]))
            print(f"Opened {n} draft(s) in Apple Mail.")
        return 0 if complete else 1

    if args.scheduled and not d.get("auto_send", False):
        notify("BillsCollector", f"{period.label}: {len(files)} invoice(s) collected. "
                                 "Open the app to review and send.")
        return 0 if complete else 1
    s = deliver.smtp_settings(cfg)
    to_accountant = complete or args.force_send or d.get("on_incomplete") == "send"
    text = accountant_text if to_accountant else summary
    deliver.send_smtp(deliver.compose(period, files, s, to_accountant, text), s)
    if to_accountant:
        month.record_sent("smtp", str(s["to"]))
    print("Sent to accountant." if to_accountant else "Incomplete: sent the report to you only.")
    return 0 if complete else 1


def cmd_check(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    ok = True
    for i, raw in enumerate(cfg["sources"]):
        name = runner.source_label(raw, i)
        if raw.get("enabled", True) is False:
            print(f"-  {name}: disabled")
            continue
        try:
            src = runner.build_source(cfg, raw, i)
            print(f"ok {name} ({src.type}); expects: {', '.join(src.required_vendors()) or '-'}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"!! {name}: {exc}")
    d = cfg.get("delivery")
    if d:
        try:
            if d.get("method", "smtp") == "smtp":
                s = deliver.smtp_settings(cfg)
                missing = [k for k in ("smtp_host", "smtp_username", "smtp_password", "to")
                           if not s.get(k)]
                if missing:
                    raise config.ConfigError(f"delivery: missing {', '.join(missing)}")
            print(f"ok delivery ({d.get('method', 'smtp')}) -> {config.resolve(d.get('to'))}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"!! {exc}")
    return 0 if ok else 1


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def install_schedule(day: int, config_path: Path) -> Path:
    logs = paths.data_dir() / "logs"
    logs.mkdir(exist_ok=True)
    plist = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [sys.executable, "-m", "billscollector", "--config",
                             str(Path(config_path).resolve()), "run", "--scheduled"],
        "WorkingDirectory": str(Path(__file__).resolve().parent.parent),
        # launchd runs a missed calendar job when the Mac wakes up.
        "StartCalendarInterval": {"Day": int(day), "Hour": 9, "Minute": 7},
        "StandardOutPath": str(logs / "monthly.log"),
        "StandardErrorPath": str(logs / "monthly.log"),
    }
    target = launch_agent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(target)], check=False, capture_output=True)
    target.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "load", str(target)], check=True)
    return target


def remove_schedule() -> None:
    target = launch_agent_path()
    if target.exists():
        subprocess.run(["launchctl", "unload", str(target)], check=False, capture_output=True)
        target.unlink()


def cmd_schedule(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        print("Scheduling uses launchd and only works on macOS.")
        return 1
    if args.action == "remove":
        remove_schedule()
        print("Monthly run removed.")
        return 0
    day = (config.load(args.config).get("schedule") or {}).get("day", 5)
    print(f"Installed {install_schedule(day, args.config)}: runs on day {day} at 09:07.")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .web import serve

    serve(Path(args.config), port=args.port, open_browser=not args.no_browser)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="billscollector")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="collect a month and send it")
    run.add_argument("--month", help="YYYY-MM (default: last month)")
    run.add_argument("--out", help="folder for the monthly folders (default: app data)")
    run.add_argument("--no-send", action="store_true", help="collect only, don't email")
    run.add_argument("--force-send", action="store_true",
                     help="email the accountant even if something is missing")
    run.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    run.set_defaults(func=cmd_run)

    sub.add_parser("check", help="validate config and secrets").set_defaults(func=cmd_check)

    sched = sub.add_parser("schedule", help="run every month on this Mac")
    sched.add_argument("action", choices=["install", "remove"])
    sched.set_defaults(func=cmd_schedule)

    ui = sub.add_parser("ui", help="open the app")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    ui.set_defaults(func=cmd_ui)

    args = parser.parse_args(argv)
    args.config = args.config or paths.default_config()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    sys.path.insert(0, str(Path.cwd()))  # so plugin modules in ./collectors import
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
