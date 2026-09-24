from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .config import AppConfig, SearchConfig
from .matcher import matches
from .models import Listing, ListingEvent
from .notifications import Notifier
from .state import StateStore


class ListingSource(Protocol):
    def fetch(
        self, search: SearchConfig, *, full_scan: bool = True
    ) -> list[Listing]: ...


@dataclass(slots=True)
class RunSummary:
    full_scan: bool = True
    searches_checked: int = 0
    listings_seen: int = 0
    listings_matched: int = 0
    baselined: int = 0
    events: int = 0
    notifications_sent: int = 0
    notification_errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MonitorService:
    config: AppConfig
    source: ListingSource
    state: StateStore
    notifiers: list[Notifier]
    dry_run: bool = False

    def check_once(
        self, *, baseline_only: bool = False, full_scan: bool = True
    ) -> RunSummary:
        summary = RunSummary(full_scan=full_scan)
        for search in self.config.searches:
            first_run = not self.state.has_completed_run(search.name)
            listings = self.source.fetch(search, full_scan=full_scan)
            matching = [listing for listing in listings if matches(listing, search.filters)]

            summary.searches_checked += 1
            summary.listings_seen += len(listings)
            summary.listings_matched += len(matching)

            for listing in matching:
                observation = self.state.observe(search.name, listing)
                if baseline_only:
                    if observation.is_new:
                        summary.baselined += 1
                    continue
                event_type: str | None = None
                if observation.is_new:
                    if first_run and not self.config.notify_existing_on_first_run:
                        summary.baselined += 1
                    else:
                        event_type = "new"
                elif (
                    self.config.notify_price_changes
                    and observation.changed
                    and observation.previous_price != listing.price
                ):
                    event_type = "price_change"
                else:
                    event_type = self.state.pending_event_type(search.name, listing)

                if event_type is not None:
                    event = ListingEvent(search.name, event_type, listing)
                    summary.events += 1
                    self._dispatch(event, summary)

            if full_scan:
                self.state.complete_run(search.name, len(matching))
        return summary

    def _dispatch(self, event: ListingEvent, summary: RunSummary) -> None:
        targets: list[Notifier] = self.notifiers
        if self.dry_run:
            from .notifications import ConsoleNotifier

            targets = [ConsoleNotifier(channel="dry-run")]

        self.state.record_pending_notifications(
            event, [notifier.channel for notifier in targets]
        )
        for notifier in targets:
            if self.state.notification_succeeded(event, notifier.channel):
                continue
            try:
                notifier.send(event)
            except Exception as exc:
                message = (
                    f"{event.search_name}/{event.listing.listing_id}/"
                    f"{notifier.channel}: {exc}"
                )
                summary.notification_errors.append(message)
                self.state.record_notification(
                    event, notifier.channel, success=False, error=str(exc)
                )
            else:
                summary.notifications_sent += 1
                self.state.record_notification(event, notifier.channel, success=True)
