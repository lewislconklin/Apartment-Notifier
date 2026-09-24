import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from apartment_notifier.cli import _local_timestamp, main


class CliTests(unittest.TestCase):
    def test_source_error_timestamp_is_local_iso_8601(self) -> None:
        timestamp = _local_timestamp()

        self.assertRegex(
            timestamp,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
        )

    @patch("apartment_notifier.cli.StreetEasySource")
    @patch("apartment_notifier.cli.build_notifiers")
    def test_notification_test_never_constructs_listing_source(
        self, build_notifiers: MagicMock, source: MagicMock
    ) -> None:
        notifier = MagicMock()
        notifier.channel = "email"
        build_notifiers.return_value = [notifier]
        config = Path(__file__).parents[1] / "config.example.toml"

        result = main(
            ["--config", str(config), "test-notifications", "--channel", "email"]
        )

        self.assertEqual(0, result)
        notifier.send.assert_called_once()
        source.assert_not_called()


if __name__ == "__main__":
    unittest.main()
