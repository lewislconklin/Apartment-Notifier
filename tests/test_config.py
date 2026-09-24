from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import date
import os
from unittest.mock import patch

from apartment_notifier.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_example_config_is_valid_and_live_access_is_locked(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.example.toml")
        self.assertFalse(config.streeteasy.allow_live_requests)
        self.assertEqual(1, len(config.searches))

    def test_live_access_requires_permission_reference(self) -> None:
        body = """
        [streeteasy]
        allow_live_requests = true

        [[searches]]
        name = "test"
        url = "https://streeteasy.com/for-rent/nyc"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "permission_reference"):
                load_config(path)

    def test_live_access_rejects_url_outside_authorized_scope(self) -> None:
        body = """
        [streeteasy]
        allow_live_requests = true
        permission_reference = "ticket-123"
        allowed_url_prefix = "https://streeteasy.com/for-rent/nyc/"
        user_agent = "ApartmentNotifier/0.2 (+mailto:operator@example.com)"

        [[searches]]
        name = "sales"
        url = "https://streeteasy.com/for-sale/nyc"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "outside the authorized scope"):
                load_config(path)

    def test_rate_limit_cannot_be_configured_below_permission(self) -> None:
        body = """
        [streeteasy]
        minimum_request_interval_seconds = 29

        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc/example"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "authorized 30 seconds"):
                load_config(path)

    def test_authorized_thirty_second_rate_limit_is_valid(self) -> None:
        body = """
        [streeteasy]
        minimum_request_interval_seconds = 30

        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc/example"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            config = load_config(path)
            self.assertEqual(30, config.streeteasy.minimum_request_interval_seconds)

    def test_thirty_second_poll_and_hourly_full_scan_are_valid(self) -> None:
        body = """
        [app]
        poll_interval_seconds = 30
        full_scan_interval_seconds = 3600

        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc/example"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            config = load_config(path)

        self.assertEqual(30, config.poll_interval_seconds)
        self.assertEqual(3600, config.full_scan_interval_seconds)

    def test_full_scan_interval_cannot_be_shorter_than_poll(self) -> None:
        body = """
        [app]
        poll_interval_seconds = 60
        full_scan_interval_seconds = 30

        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc/example"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "full_scan_interval_seconds"):
                load_config(path)

    def test_max_pages_has_a_bounded_range(self) -> None:
        body = """
        [streeteasy]
        max_pages = 0

        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc/example"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "max_pages"):
                load_config(path)

    def test_multiple_imessage_recipients_can_be_sent_individually(self) -> None:
        body = """
        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc/example"

        [notifications.imessage]
        enabled = true
        recipients = ["+12125550101", "+12125550102"]
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            config = load_config(path)

        self.assertEqual(
            ("+12125550101", "+12125550102"),
            config.imessage.recipients,
        )
        self.assertEqual("", config.imessage.chat_id)

    def test_parses_available_on_or_before_date(self) -> None:
        body = """
        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc/example"

        [searches.filters]
        available_on_or_before = 2026-10-15
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            config = load_config(path)

        self.assertEqual(
            date(2026, 10, 15),
            config.searches[0].filters.available_on_or_before,
        )

    def test_rejects_invalid_available_on_or_before_date(self) -> None:
        body = """
        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc/example"

        [searches.filters]
        available_on_or_before = "October 15"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "YYYY-MM-DD"):
                load_config(path)

    def test_parses_excluded_area_polygon(self) -> None:
        body = """
        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc/example"

        [[searches.filters.excluded_areas]]
        name = "east of the avenue"
        points = [
          [40.72, -73.98],
          [40.74, -73.96],
          [40.72, -73.95],
        ]
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            config = load_config(path)

        area = config.searches[0].filters.excluded_areas[0]
        self.assertEqual("east of the avenue", area.name)
        self.assertEqual((40.72, -73.98), area.points[0])

    def test_rejects_invalid_excluded_area_polygon(self) -> None:
        body = """
        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc/example"

        [[searches.filters.excluded_areas]]
        name = "invalid"
        points = [[40.72, -73.98], [40.74, -73.96]]
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "at least 3 coordinates"):
                load_config(path)

    def test_expands_environment_variables_in_portable_paths(self) -> None:
        body = """
        [app]
        database = "$APARTMENT_NOTIFIER_TEST_HOME/state/notifier.sqlite3"

        [streeteasy]
        rate_limit_state_file = "$APARTMENT_NOTIFIER_TEST_HOME/state/rate-limit"

        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with patch.dict(
                os.environ, {"APARTMENT_NOTIFIER_TEST_HOME": directory}
            ):
                config = load_config(path)

        self.assertEqual(
            (Path(directory) / "state" / "notifier.sqlite3").resolve(),
            config.database,
        )
        self.assertEqual(
            (Path(directory) / "state" / "rate-limit").resolve(),
            config.streeteasy.rate_limit_state_file,
        )

    def test_rejects_unset_environment_variable_in_path(self) -> None:
        body = """
        [app]
        database = "$APARTMENT_NOTIFIER_MISSING_HOME/notifier.sqlite3"

        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("APARTMENT_NOTIFIER_MISSING_HOME", None)
                with self.assertRaisesRegex(ConfigError, "unset environment variable"):
                    load_config(path)

    def test_rejects_string_that_looks_like_boolean(self) -> None:
        body = """
        [streeteasy]
        allow_live_requests = "false"

        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "must be true or false"):
                load_config(path)

    def test_live_access_requires_contactable_user_agent(self) -> None:
        body = """
        [streeteasy]
        allow_live_requests = true
        permission_reference = "ticket-123"

        [[searches]]
        name = "rentals"
        url = "https://streeteasy.com/for-rent/nyc"
        """
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "contactable.*user_agent"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
