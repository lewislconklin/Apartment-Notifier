from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from apartment_notifier.config import (
    AppConfig,
    EmailConfig,
    FilterConfig,
    IMessageConfig,
    SearchConfig,
    SmsConfig,
    StreetEasyConfig,
)
from apartment_notifier.models import Listing, ListingEvent
from apartment_notifier.service import MonitorService
from apartment_notifier.state import StateStore


class SequenceSource:
    def __init__(self, sequences: list[list[Listing]]):
        self.sequences = sequences
        self.index = 0
        self.full_scans: list[bool] = []

    def fetch(
        self, search: SearchConfig, *, full_scan: bool = True
    ) -> list[Listing]:
        self.full_scans.append(full_scan)
        result = self.sequences[self.index]
        self.index += 1
        return result


class RecordingNotifier:
    channel = "recording"

    def __init__(self) -> None:
        self.events: list[ListingEvent] = []

    def send(self, event: ListingEvent) -> None:
        self.events.append(event)


class FailOnceNotifier(RecordingNotifier):
    channel = "fail-once"

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def send(self, event: ListingEvent) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("temporary failure")
        super().send(event)


def listing(listing_id: str, price: int) -> Listing:
    return Listing(
        listing_id=listing_id,
        url=f"https://streeteasy.com/building/example/{listing_id}",
        address=f"{listing_id} Example Street",
        price=price,
        bedrooms=2,
        bathrooms=1,
        neighborhood="Greenpoint",
    )


class MonitorServiceTests(unittest.TestCase):
    def test_baselines_then_notifies_new_and_price_change(self) -> None:
        original = listing("100", 4000)
        added = listing("200", 4500)
        changed = listing("100", 3900)
        source = SequenceSource(
            [[original], [original, added], [changed, added]]
        )
        notifier = RecordingNotifier()

        with TemporaryDirectory() as directory:
            config = AppConfig(
                database=Path(directory) / "state.sqlite3",
                poll_interval_seconds=300,
                full_scan_interval_seconds=3600,
                notify_existing_on_first_run=False,
                notify_price_changes=True,
                searches=(
                    SearchConfig(
                        "test",
                        "https://streeteasy.com/for-rent/nyc",
                        FilterConfig(max_price=5000),
                    ),
                ),
                streeteasy=StreetEasyConfig(),
                email=EmailConfig(),
                sms=SmsConfig(),
                imessage=IMessageConfig(),
            )
            with StateStore(config.database) as state:
                service = MonitorService(config, source, state, [notifier])

                first = service.check_once()
                second = service.check_once()
                third = service.check_once()

                self.assertEqual(1, first.baselined)
                self.assertEqual(0, first.events)
                self.assertEqual(1, second.events)
                self.assertEqual(1, third.events)
                self.assertEqual(["new", "price_change"], [e.event_type for e in notifier.events])
                self.assertEqual(2, state.counts()["notifications_sent"])

    def test_retries_failed_channel_on_next_observation(self) -> None:
        existing = listing("100", 4000)
        added = listing("200", 4500)
        source = SequenceSource([[existing], [existing, added], [existing, added]])
        notifier = FailOnceNotifier()

        with TemporaryDirectory() as directory:
            config = AppConfig(
                database=Path(directory) / "state.sqlite3",
                poll_interval_seconds=300,
                full_scan_interval_seconds=3600,
                notify_existing_on_first_run=False,
                notify_price_changes=True,
                searches=(
                    SearchConfig("test", "https://streeteasy.com/for-rent/nyc"),
                ),
                streeteasy=StreetEasyConfig(),
                email=EmailConfig(),
                sms=SmsConfig(),
                imessage=IMessageConfig(),
            )
            with StateStore(config.database) as state:
                service = MonitorService(config, source, state, [notifier])
                service.check_once()
                failed = service.check_once()
                retried = service.check_once()

                self.assertEqual(1, len(failed.notification_errors))
                self.assertEqual(1, retried.notifications_sent)
                self.assertEqual(2, notifier.attempts)
                self.assertEqual(["new"], [event.event_type for event in notifier.events])

    def test_explicit_baseline_suppresses_alerts_after_an_earlier_run(self) -> None:
        original = listing("100", 4000)
        added_during_rebaseline = listing("200", 4500)
        genuinely_new = listing("300", 4700)
        source = SequenceSource(
            [
                [original],
                [original, added_during_rebaseline],
                [original, added_during_rebaseline, genuinely_new],
            ]
        )
        notifier = RecordingNotifier()

        with TemporaryDirectory() as directory:
            config = AppConfig(
                database=Path(directory) / "state.sqlite3",
                poll_interval_seconds=300,
                full_scan_interval_seconds=3600,
                notify_existing_on_first_run=False,
                notify_price_changes=True,
                searches=(
                    SearchConfig("test", "https://streeteasy.com/for-rent/nyc"),
                ),
                streeteasy=StreetEasyConfig(),
                email=EmailConfig(),
                sms=SmsConfig(),
                imessage=IMessageConfig(),
            )
            with StateStore(config.database) as state:
                service = MonitorService(config, source, state, [notifier])
                service.check_once()
                baseline = service.check_once(baseline_only=True)
                checked = service.check_once()

                self.assertEqual(1, baseline.baselined)
                self.assertEqual(0, baseline.events)
                self.assertEqual(1, checked.events)
                self.assertEqual(["300"], [event.listing.listing_id for event in notifier.events])

    def test_newest_page_check_detects_listing_after_full_baseline(self) -> None:
        original = listing("100", 4000)
        added = listing("200", 4500)
        source = SequenceSource([[original], [added]])
        notifier = RecordingNotifier()

        with TemporaryDirectory() as directory:
            config = AppConfig(
                database=Path(directory) / "state.sqlite3",
                poll_interval_seconds=30,
                full_scan_interval_seconds=3600,
                notify_existing_on_first_run=False,
                notify_price_changes=False,
                searches=(
                    SearchConfig("test", "https://streeteasy.com/for-rent/nyc"),
                ),
                streeteasy=StreetEasyConfig(),
                email=EmailConfig(),
                sms=SmsConfig(),
                imessage=IMessageConfig(),
            )
            with StateStore(config.database) as state:
                service = MonitorService(config, source, state, [notifier])
                baseline = service.check_once(full_scan=True)
                latest = service.check_once(full_scan=False)

                self.assertTrue(baseline.full_scan)
                self.assertFalse(latest.full_scan)
                self.assertEqual([True, False], source.full_scans)
                self.assertEqual(
                    ["200"], [event.listing.listing_id for event in notifier.events]
                )


if __name__ == "__main__":
    unittest.main()
