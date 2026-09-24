import base64
import os
import unittest
from datetime import date
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

from apartment_notifier.config import EmailConfig, IMessageConfig, SmsConfig
from apartment_notifier.models import Listing, ListingEvent
from apartment_notifier.notifications import (
    EmailNotifier,
    IMessageNotifier,
    TwilioSmsNotifier,
    format_text,
)


def event() -> ListingEvent:
    return ListingEvent(
        search_name="Greenpoint 2BR",
        event_type="new",
        listing=Listing(
            listing_id="123",
            url="https://streeteasy.com/building/example/2a",
            address="123 Example Street #2A",
            price=4500,
            bedrooms=2,
            bathrooms=1.5,
            available_date=date(2026, 10, 31),
            neighborhood="Greenpoint",
        ),
    )


class NotificationTests(unittest.TestCase):
    def test_text_contains_core_listing_details(self) -> None:
        message = format_text(event())
        self.assertIn("$4,500/mo", message)
        self.assertIn("2 / 1.5", message)
        self.assertIn("Available: 10/31/2026", message)
        self.assertIn("https://streeteasy.com/", message)

    @patch.dict(os.environ, {"TEST_SMTP_USER": "user", "TEST_SMTP_PASS": "secret"})
    @patch("apartment_notifier.notifications.smtplib.SMTP")
    def test_smtp_delivery(self, smtp: MagicMock) -> None:
        client = smtp.return_value.__enter__.return_value
        notifier = EmailNotifier(
            EmailConfig(
                enabled=True,
                host="smtp.example.com",
                username_env="TEST_SMTP_USER",
                password_env="TEST_SMTP_PASS",
                from_address="alerts@example.com",
                to_addresses=("recipient@example.com",),
            )
        )

        notifier.send(event())

        client.starttls.assert_called_once()
        client.login.assert_called_once_with("user", "secret")
        sent = client.send_message.call_args.args[0]
        self.assertEqual("recipient@example.com", sent["To"])
        self.assertIn("123 Example Street", sent["Subject"])

    @patch.dict(os.environ, {"TEST_TWILIO_SID": "AC123", "TEST_TWILIO_TOKEN": "token"})
    @patch("apartment_notifier.notifications.urlopen")
    def test_twilio_delivery(self, urlopen: MagicMock) -> None:
        response = urlopen.return_value.__enter__.return_value
        response.status = 201
        notifier = TwilioSmsNotifier(
            SmsConfig(
                enabled=True,
                account_sid_env="TEST_TWILIO_SID",
                auth_token_env="TEST_TWILIO_TOKEN",
                from_number="+12125550100",
                to_numbers=("+12125550101",),
            )
        )

        notifier.send(event())

        request = urlopen.call_args.args[0]
        payload = parse_qs(request.data.decode())
        self.assertEqual(["+12125550101"], payload["To"])
        self.assertIn("123 Example Street", payload["Body"][0])
        expected = base64.b64encode(b"AC123:token").decode()
        self.assertEqual(f"Basic {expected}", request.headers["Authorization"])

    @patch.dict(os.environ, {"TEST_IMESSAGE_RECIPIENT": "+12125550101"})
    @patch("apartment_notifier.notifications.platform.system", return_value="Darwin")
    @patch("apartment_notifier.notifications.subprocess.run")
    def test_imessage_delivery(
        self, run: MagicMock, _system: MagicMock
    ) -> None:
        notifier = IMessageNotifier(
            IMessageConfig(
                enabled=True,
                recipient_env="TEST_IMESSAGE_RECIPIENT",
            )
        )

        notifier.send(event())

        command = run.call_args.args[0]
        self.assertEqual("osascript", command[0])
        self.assertEqual("+12125550101", command[-2])
        self.assertIn("123 Example Street", command[-1])
        self.assertTrue(run.call_args.kwargs["check"])

    @patch("apartment_notifier.notifications.platform.system", return_value="Darwin")
    @patch("apartment_notifier.notifications.subprocess.run")
    def test_multiple_imessage_recipients_are_sent_individually(
        self, run: MagicMock, _system: MagicMock
    ) -> None:
        notifier = IMessageNotifier(
            IMessageConfig(
                enabled=True,
                recipients=("+12125550101", "+12125550102"),
            )
        )

        notifier.send(event())

        self.assertEqual(2, run.call_count)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual("+12125550101", commands[0][-2])
        self.assertEqual("+12125550102", commands[1][-2])
        self.assertNotIn("every chat whose id", commands[0][2])

    @patch("apartment_notifier.notifications.platform.system", return_value="Darwin")
    @patch("apartment_notifier.notifications.subprocess.run")
    def test_imessage_group_delivery(
        self, run: MagicMock, _system: MagicMock
    ) -> None:
        notifier = IMessageNotifier(
            IMessageConfig(
                enabled=True,
                recipients=("+12125550101", "+12125550102"),
                chat_id="any;+;apartment-alerts",
            )
        )

        notifier.send(event())

        command = run.call_args.args[0]
        self.assertEqual("osascript", command[0])
        self.assertIn("every chat whose id", command[2])
        self.assertEqual("any;+;apartment-alerts", command[-2])
        self.assertIn("123 Example Street", command[-1])
        self.assertTrue(run.call_args.kwargs["check"])

    @patch.dict(os.environ, {}, clear=True)
    @patch("apartment_notifier.notifications.read_password", return_value="app-password")
    @patch("apartment_notifier.notifications.smtplib.SMTP")
    def test_email_can_read_password_from_macos_keychain(
        self, smtp: MagicMock, read_password: MagicMock
    ) -> None:
        client = smtp.return_value.__enter__.return_value
        notifier = EmailNotifier(
            EmailConfig(
                enabled=True,
                host="smtp.gmail.com",
                password_keychain_service="Apartment Notifier Gmail",
                password_keychain_account="sender@example.com",
                from_address="sender@example.com",
                to_addresses=("recipient@example.com",),
            )
        )

        notifier.send(event())

        read_password.assert_called_once_with(
            "Apartment Notifier Gmail", "sender@example.com"
        )
        client.login.assert_called_once_with("sender@example.com", "app-password")


if __name__ == "__main__":
    unittest.main()
