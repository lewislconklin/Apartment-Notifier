from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from apartment_notifier.config import ConfigError, load_config
from apartment_notifier.wizard import configure


class WizardTests(unittest.TestCase):
    def test_guided_configuration_is_safe_and_valid(self) -> None:
        answers = iter(
            [
                "downtown",
                "https://streeteasy.com/for-rent/nyc?sort_by=listed_desc",
                "Soho, Tribeca",
                "3000",
                "5000",
                "1",
                "2",
                "1",
                "2026-11-01",
                "laundry",
                "short term",
                "Dishwasher",
                "n",
                "y",
                "",
                "+12125550101, person@example.com",
                "n",
                "n",
            ]
        )
        output: list[str] = []
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            configure(
                path,
                input_fn=lambda _prompt: next(answers),
                password_fn=lambda _prompt: self.fail("Password prompt was unexpected"),
                output_fn=output.append,
            )
            config = load_config(path)

        self.assertFalse(config.streeteasy.allow_live_requests)
        self.assertEqual("downtown", config.searches[0].name)
        self.assertEqual(("Soho", "Tribeca"), config.searches[0].filters.neighborhoods)
        self.assertEqual(
            ("+12125550101", "person@example.com"),
            config.imessage.recipients,
        )
        self.assertFalse(config.email.enabled)
        self.assertTrue(any("Live StreetEasy requests remain disabled" in line for line in output))

    def test_does_not_overwrite_existing_config_without_force(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("original", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "already exists"):
                configure(path, input_fn=lambda _prompt: self.fail("Unexpected prompt"))
            self.assertEqual("original", path.read_text(encoding="utf-8"))

    @patch("apartment_notifier.wizard.store_password")
    def test_gmail_password_is_stored_in_keychain_not_toml(
        self, store_password
    ) -> None:
        answers = iter(
            [
                "test",
                "https://streeteasy.com/for-rent/nyc",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "n",
                "n",
                "y",
                "sender@example.com",
                "recipient@example.com",
                "n",
            ]
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            configure(
                path,
                input_fn=lambda _prompt: next(answers),
                password_fn=lambda _prompt: "super-secret-app-password",
                output_fn=lambda _message: None,
            )
            contents = path.read_text(encoding="utf-8")
            config = load_config(path)

        store_password.assert_called_once_with(
            "Apartment Notifier Gmail",
            "sender@example.com",
            "super-secret-app-password",
        )
        self.assertNotIn("super-secret-app-password", contents)
        self.assertEqual(
            "Apartment Notifier Gmail", config.email.password_keychain_service
        )
        self.assertEqual("sender@example.com", config.email.password_keychain_account)


if __name__ == "__main__":
    unittest.main()
