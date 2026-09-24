from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .models import Listing, ListingEvent


@dataclass(frozen=True, slots=True)
class Observation:
    is_new: bool
    changed: bool
    previous_price: int | None


class StateStore:
    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def has_completed_run(self, search_name: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM search_runs WHERE search_name = ? LIMIT 1", (search_name,)
        ).fetchone()
        return row is not None

    def complete_run(self, search_name: str, listing_count: int) -> None:
        now = _now()
        self._connection.execute(
            """
            INSERT INTO search_runs(search_name, first_completed_at, last_completed_at, listing_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(search_name) DO UPDATE SET
                last_completed_at = excluded.last_completed_at,
                listing_count = excluded.listing_count
            """,
            (search_name, now, now, listing_count),
        )
        self._connection.commit()

    def observe(self, search_name: str, listing: Listing) -> Observation:
        row = self._connection.execute(
            """
            SELECT fingerprint, price
            FROM listings
            WHERE search_name = ? AND listing_id = ?
            """,
            (search_name, listing.listing_id),
        ).fetchone()
        now = _now()
        is_new = row is None
        changed = row is not None and row["fingerprint"] != listing.fingerprint
        previous_price = row["price"] if row is not None else None
        self._connection.execute(
            """
            INSERT INTO listings(
                search_name, listing_id, url, address, price, fingerprint,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(search_name, listing_id) DO UPDATE SET
                url = excluded.url,
                address = excluded.address,
                price = excluded.price,
                fingerprint = excluded.fingerprint,
                last_seen_at = excluded.last_seen_at
            """,
            (
                search_name,
                listing.listing_id,
                listing.url,
                listing.address,
                listing.price,
                listing.fingerprint,
                now,
                now,
            ),
        )
        self._connection.commit()
        return Observation(is_new=is_new, changed=changed, previous_price=previous_price)

    def notification_succeeded(self, event: ListingEvent, channel: str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1 FROM notification_log
            WHERE search_name = ? AND listing_id = ? AND event_key = ?
              AND channel = ? AND status = 'sent'
            LIMIT 1
            """,
            (event.search_name, event.listing.listing_id, event.event_key, channel),
        ).fetchone()
        return row is not None

    def pending_event_type(self, search_name: str, listing: Listing) -> str | None:
        """Return a failed event type for this exact listing version, if any."""
        row = self._connection.execute(
            """
            SELECT event_key FROM notification_log
            WHERE search_name = ? AND listing_id = ?
              AND event_key LIKE ? AND status IN ('pending', 'failed')
            ORDER BY attempted_at ASC
            LIMIT 1
            """,
            (search_name, listing.listing_id, f"%:{listing.fingerprint}"),
        ).fetchone()
        if row is None:
            return None
        return str(row["event_key"]).split(":", 1)[0]

    def record_pending_notifications(
        self, event: ListingEvent, channels: list[str]
    ) -> None:
        now = _now()
        self._connection.executemany(
            """
            INSERT INTO notification_log(
                search_name, listing_id, event_key, channel, status, error, attempted_at
            ) VALUES (?, ?, ?, ?, 'pending', '', ?)
            ON CONFLICT(search_name, listing_id, event_key, channel) DO NOTHING
            """,
            [
                (
                    event.search_name,
                    event.listing.listing_id,
                    event.event_key,
                    channel,
                    now,
                )
                for channel in channels
            ],
        )
        self._connection.commit()

    def record_notification(
        self,
        event: ListingEvent,
        channel: str,
        *,
        success: bool,
        error: str = "",
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO notification_log(
                search_name, listing_id, event_key, channel, status, error, attempted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(search_name, listing_id, event_key, channel) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                attempted_at = excluded.attempted_at
            """,
            (
                event.search_name,
                event.listing.listing_id,
                event.event_key,
                channel,
                "sent" if success else "failed",
                error,
                _now(),
            ),
        )
        self._connection.commit()

    def counts(self) -> dict[str, int]:
        listings = self._connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        sent = self._connection.execute(
            "SELECT COUNT(*) FROM notification_log WHERE status = 'sent'"
        ).fetchone()[0]
        failed = self._connection.execute(
            "SELECT COUNT(*) FROM notification_log WHERE status = 'failed'"
        ).fetchone()[0]
        return {"listings": listings, "notifications_sent": sent, "notifications_failed": failed}

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS listings (
                search_name TEXT NOT NULL,
                listing_id TEXT NOT NULL,
                url TEXT NOT NULL,
                address TEXT NOT NULL,
                price INTEGER,
                fingerprint TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(search_name, listing_id)
            );

            CREATE TABLE IF NOT EXISTS search_runs (
                search_name TEXT PRIMARY KEY,
                first_completed_at TEXT NOT NULL,
                last_completed_at TEXT NOT NULL,
                listing_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notification_log (
                search_name TEXT NOT NULL,
                listing_id TEXT NOT NULL,
                event_key TEXT NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'failed')),
                error TEXT NOT NULL DEFAULT '',
                attempted_at TEXT NOT NULL,
                PRIMARY KEY(search_name, listing_id, event_key, channel)
            );
            """
        )
        self._connection.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
