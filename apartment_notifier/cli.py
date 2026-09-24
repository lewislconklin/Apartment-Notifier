from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
import time

from .config import AppConfig, ConfigError, load_config
from .launchd import LaunchdError
from . import launchd
from .models import Listing, ListingEvent
from .notifications import NotificationError, build_notifiers
from .service import MonitorService, RunSummary
from .state import StateStore
from .streeteasy import FixtureStreetEasySource, SourceError, StreetEasySource
from .wizard import configure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apartment-notifier",
        description="Watch configured rental searches and notify on new listings.",
    )
    parser.add_argument("--config", default="config.toml", help="Path to TOML config")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure_parser = subparsers.add_parser(
        "configure", help="Create a personal configuration interactively"
    )
    configure_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing config file (never changes the database)",
    )

    subparsers.add_parser("validate", help="Validate configuration without fetching")
    test_notifications = subparsers.add_parser(
        "test-notifications", help="Send a sample alert without fetching listings"
    )
    test_notifications.add_argument(
        "--channel",
        choices=("all", "email", "sms", "imessage"),
        default="all",
        help="Test all enabled channels or one specific enabled channel",
    )

    check = subparsers.add_parser("check", help="Run one check")
    check.add_argument(
        "--fixture",
        action="append",
        default=[],
        metavar="SEARCH=HTML_FILE",
        help="Use saved HTML for a named search; repeat for multiple searches",
    )

    subparsers.add_parser(
        "baseline",
        help="Record all current matching listings without sending notifications",
    )
    check.add_argument(
        "--dry-run",
        action="store_true",
        help="Print notifications and use temporary in-memory state",
    )

    run = subparsers.add_parser("run", help="Run continuously at the configured interval")
    run.add_argument("--dry-run", action="store_true", help="Print instead of delivering")

    subparsers.add_parser(
        "install-launch-agent",
        help="Install and start the per-user macOS launch agent",
    )
    subparsers.add_parser(
        "launch-agent-status", help="Show the macOS launch agent status"
    )
    subparsers.add_parser("start-launch-agent", help="Start the installed launch agent")
    subparsers.add_parser("stop-launch-agent", help="Stop the installed launch agent")
    subparsers.add_parser(
        "uninstall-launch-agent", help="Stop and remove the macOS launch agent"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "configure":
            configure(args.config, force=args.force)
            return 0
        if args.command == "uninstall-launch-agent":
            path = launchd.uninstall()
            print(f"Stopped and removed launch agent: {path}")
            return 0
        if args.command == "launch-agent-status":
            print(launchd.status(), end="")
            return 0
        if args.command == "start-launch-agent":
            path = launchd.start()
            print(f"Started launch agent: {path}")
            return 0
        if args.command == "stop-launch-agent":
            path = launchd.stop()
            print(f"Stopped launch agent: {path}")
            return 0

        config = load_config(args.config)
        if args.command == "validate":
            print(
                f"Configuration is valid: {len(config.searches)} search(es), "
                f"database {config.database}"
            )
            if not config.streeteasy.allow_live_requests:
                print("Live StreetEasy requests are locked pending written permission.")
            return 0

        if args.command == "install-launch-agent":
            if not build_notifiers(config.email, config.sms, config.imessage):
                raise ConfigError(
                    "At least one notification channel must be enabled before "
                    "installing the launch agent"
                )
            path = launchd.install(Path(args.config))
            print(f"Installed and started launch agent: {path}")
            return 0

        dry_run = bool(getattr(args, "dry_run", False))
        notifiers = build_notifiers(config.email, config.sms, config.imessage)
        if args.command == "test-notifications":
            selected = [
                notifier
                for notifier in notifiers
                if args.channel == "all" or notifier.channel == args.channel
            ]
            if not selected:
                raise ConfigError(
                    f"No enabled notification channel matched: {args.channel}"
                )
            sample = ListingEvent(
                search_name="Notification test",
                event_type="new",
                listing=Listing(
                    listing_id="notification-test",
                    url="https://streeteasy.com/",
                    address="123 Example Street #2A",
                    price=7500,
                    bedrooms=2,
                    bathrooms=1.5,
                    neighborhood="Example Neighborhood",
                ),
            )
            failures: list[str] = []
            for notifier in selected:
                try:
                    notifier.send(sample)
                except NotificationError as exc:
                    failures.append(f"{notifier.channel}: {exc}")
                else:
                    print(f"Sent sample notification through {notifier.channel}.")
            for failure in failures:
                print(f"Error: {failure}", file=sys.stderr)
            return 1 if failures else 0

        if args.command != "baseline" and not dry_run and not notifiers:
            raise ConfigError(
                "No notification channels are enabled; enable email/SMS or use --dry-run"
            )

        if args.command == "check" and args.fixture:
            source = _fixture_source(config, args.fixture)
        else:
            source = StreetEasySource(config.streeteasy)

        state_path: Path | str = ":memory:" if dry_run else config.database
        with StateStore(state_path) as state:
            service = MonitorService(config, source, state, notifiers, dry_run=dry_run)
            if args.command in {"check", "baseline"}:
                summary = service.check_once(baseline_only=args.command == "baseline")
                _print_summary(summary)
                return 1 if summary.notification_errors else 0

            print(
                f"Monitoring {len(config.searches)} search(es): newest page every "
                f"{config.poll_interval_seconds}s, full scan every "
                f"{config.full_scan_interval_seconds}s. Press Ctrl-C to stop."
            )
            next_full_scan_at = 0.0
            while True:
                scan_started_at = time.monotonic()
                full_scan = scan_started_at >= next_full_scan_at
                sleep_seconds = config.poll_interval_seconds
                try:
                    summary = service.check_once(full_scan=full_scan)
                    _print_summary(summary)
                    if full_scan:
                        next_full_scan_at = (
                            scan_started_at + config.full_scan_interval_seconds
                        )
                except SourceError as exc:
                    print(
                        f"Source error [{_local_timestamp()}]: {exc}",
                        file=sys.stderr,
                    )
                    sleep_seconds = max(
                        sleep_seconds,
                        exc.retry_after_seconds or 0,
                    )
                time.sleep(sleep_seconds)
    except SourceError as exc:
        print(f"Error [{_local_timestamp()}]: {exc}", file=sys.stderr)
        return 2
    except (ConfigError, LaunchdError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped.")
        return 0


def _fixture_source(config: AppConfig, specs: list[str]) -> FixtureStreetEasySource:
    fixtures: dict[str, str] = {}
    for spec in specs:
        if "=" in spec:
            name, raw_path = spec.split("=", 1)
        elif len(config.searches) == 1:
            name, raw_path = config.searches[0].name, spec
        else:
            raise ConfigError("Use SEARCH=HTML_FILE when multiple searches are configured")
        name = name.strip()
        if name not in {search.name for search in config.searches}:
            raise ConfigError(f"Fixture refers to unknown search: {name}")
        path = Path(raw_path).expanduser().resolve()
        try:
            fixtures[name] = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(f"Fixture not found: {path}") from exc

    missing = [search.name for search in config.searches if search.name not in fixtures]
    if missing:
        raise ConfigError(f"Missing fixtures for searches: {', '.join(missing)}")
    return FixtureStreetEasySource(fixtures)


def _print_summary(summary: RunSummary) -> None:
    label = "Full scan" if summary.full_scan else "Newest-page check"
    print(
        f"{label} complete: "
        f"{summary.searches_checked} search(es), "
        f"{summary.listings_seen} seen, "
        f"{summary.listings_matched} matched, "
        f"{summary.baselined} baselined, "
        f"{summary.events} event(s), "
        f"{summary.notifications_sent} notification(s) sent."
    )
    for error in summary.notification_errors:
        print(f"Notification error: {error}", file=sys.stderr)


def _local_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
