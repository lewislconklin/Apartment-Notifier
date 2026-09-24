from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from html import escape
import base64
import os
import platform
import smtplib
import ssl
import subprocess
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import EmailConfig, IMessageConfig, SmsConfig
from .keychain import KeychainError, read_password
from .models import ListingEvent


class NotificationError(RuntimeError):
    pass


class Notifier(Protocol):
    channel: str

    def send(self, event: ListingEvent) -> None: ...


@dataclass(slots=True)
class ConsoleNotifier:
    channel: str = "console"

    def send(self, event: ListingEvent) -> None:
        print(format_text(event))


@dataclass(slots=True)
class EmailNotifier:
    config: EmailConfig
    channel: str = "email"

    def send(self, event: ListingEvent) -> None:
        username, password = _email_credentials(self.config)
        listing = event.listing

        message = EmailMessage()
        message["Subject"] = _subject(event)
        message["From"] = self.config.from_address
        message["To"] = ", ".join(self.config.to_addresses)
        message.set_content(format_text(event))
        message.add_alternative(
            """
            <html><body>
              <h2>{heading}</h2>
              <p><strong>{address}</strong></p>
              <ul>
                <li>Rent: {price}</li>
                <li>Bedrooms: {bedrooms}</li>
                <li>Bathrooms: {bathrooms}</li>
                <li>Available: {available}</li>
                <li>Neighborhood: {neighborhood}</li>
              </ul>
              <p><a href="{url}">Open listing on StreetEasy</a></p>
            </body></html>
            """.format(
                heading=escape(_subject(event)),
                address=escape(listing.address),
                price=escape(_price(listing.price)),
                bedrooms=escape(_number(listing.bedrooms)),
                bathrooms=escape(_number(listing.bathrooms)),
                available=escape(_available_date(listing.available_date)),
                neighborhood=escape(listing.neighborhood or "Unknown"),
                url=escape(listing.url, quote=True),
            ),
            subtype="html",
        )

        try:
            if self.config.use_ssl:
                with smtplib.SMTP_SSL(
                    self.config.host,
                    self.config.port,
                    context=ssl.create_default_context(),
                    timeout=30,
                ) as server:
                    server.login(username, password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(self.config.host, self.config.port, timeout=30) as server:
                    if self.config.starttls:
                        server.starttls(context=ssl.create_default_context())
                    server.login(username, password)
                    server.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotificationError(f"Email delivery failed: {exc}") from exc


@dataclass(slots=True)
class TwilioSmsNotifier:
    config: SmsConfig
    channel: str = "sms"

    def send(self, event: ListingEvent) -> None:
        account_sid = _required_env(self.config.account_sid_env)
        auth_token = _required_env(self.config.auth_token_env)
        endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
        body = _sms_text(event)

        for to_number in self.config.to_numbers:
            payload = urlencode(
                {"From": self.config.from_number, "To": to_number, "Body": body}
            ).encode()
            request = Request(
                endpoint,
                data=payload,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "ApartmentNotifier/0.2",
                },
            )
            try:
                with urlopen(request, timeout=30) as response:
                    if response.status >= 300:
                        raise NotificationError(f"Twilio returned HTTP {response.status}")
            except OSError as exc:
                raise NotificationError(f"SMS delivery failed for {to_number}: {exc}") from exc


@dataclass(slots=True)
class IMessageNotifier:
    config: IMessageConfig
    channel: str = "imessage"

    def send(self, event: ListingEvent) -> None:
        if platform.system() != "Darwin":
            raise NotificationError("iMessage delivery requires macOS")
        recipients = _imessage_recipients(self.config)
        message = _sms_text(event)
        if self.config.chat_id:
            script = """
            on run argv
                set targetChatId to item 1 of argv
                set messageText to item 2 of argv
                tell application "Messages"
                    set matchingChats to every chat whose id is targetChatId
                    if (count of matchingChats) is 0 then
                        error "No Messages chat with id " & targetChatId
                    end if
                    set targetChat to item 1 of matchingChats
                    send messageText to targetChat
                end tell
            end run
            """
            deliveries = [[self.config.chat_id, message]]
        else:
            script = """
            on run argv
                set recipientId to item 1 of argv
                set messageText to item 2 of argv
                tell application "Messages"
                    set targetService to first service whose service type = iMessage
                    set targetBuddy to buddy recipientId of targetService
                    send messageText to targetBuddy
                end tell
            end run
            """
            deliveries = [[recipient, message] for recipient in recipients]
        for arguments in deliveries:
            try:
                subprocess.run(
                    ["osascript", "-e", script, *arguments],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except FileNotFoundError as exc:
                raise NotificationError("The macOS osascript command was not found") from exc
            except subprocess.TimeoutExpired as exc:
                raise NotificationError("Messages did not respond within 30 seconds") from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "unknown Messages error").strip()
                raise NotificationError(f"iMessage delivery failed: {detail}") from exc


def build_notifiers(
    email: EmailConfig, sms: SmsConfig, imessage: IMessageConfig
) -> list[Notifier]:
    notifiers: list[Notifier] = []
    if email.enabled:
        notifiers.append(EmailNotifier(email))
    if sms.enabled:
        notifiers.append(TwilioSmsNotifier(sms))
    if imessage.enabled:
        notifiers.append(IMessageNotifier(imessage))
    return notifiers


def format_text(event: ListingEvent) -> str:
    listing = event.listing
    label = "New listing" if event.event_type == "new" else "Price change"
    return "\n".join(
        [
            f"{label} — {event.search_name}",
            listing.address,
            f"Rent: {_price(listing.price)}",
            f"Beds/baths: {_number(listing.bedrooms)} / {_number(listing.bathrooms)}",
            f"Available: {_available_date(listing.available_date)}",
            f"Neighborhood: {listing.neighborhood or 'Unknown'}",
            listing.url,
        ]
    )


def _subject(event: ListingEvent) -> str:
    label = "New rental" if event.event_type == "new" else "Rental price change"
    return f"{label}: {event.listing.address} — {_price(event.listing.price)}"


def _sms_text(event: ListingEvent) -> str:
    listing = event.listing
    label = "NEW" if event.event_type == "new" else "PRICE"
    return (
        f"{label} [{event.search_name}] {listing.address} "
        f"{_price(listing.price)}, {_number(listing.bedrooms)}bd/"
        f"{_number(listing.bathrooms)}ba, available "
        f"{_available_date(listing.available_date)} {listing.url}"
    )


def _price(value: int | None) -> str:
    return f"${value:,}/mo" if value is not None else "Unknown"


def _number(value: float | None) -> str:
    if value is None:
        return "?"
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def _available_date(value: date | None) -> str:
    return value.strftime("%m/%d/%Y") if value is not None else "Unknown"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise NotificationError(f"Required environment variable is not set: {name}")
    return value


def _email_credentials(config: EmailConfig) -> tuple[str, str]:
    username = os.environ.get(config.username_env, "") if config.username_env else ""
    password = os.environ.get(config.password_env, "") if config.password_env else ""
    if username and password:
        return username, password

    if config.password_keychain_service and config.password_keychain_account:
        try:
            keychain_password = read_password(
                config.password_keychain_service,
                config.password_keychain_account,
            )
        except KeychainError as exc:
            if password:
                return username or config.password_keychain_account, password
            raise NotificationError(str(exc)) from exc
        return username or config.password_keychain_account, password or keychain_password

    missing = config.username_env if not username else config.password_env
    raise NotificationError(f"Required environment variable is not set: {missing}")


def _imessage_recipients(config: IMessageConfig) -> tuple[str, ...]:
    if config.recipients:
        return config.recipients
    raw = _required_env(config.recipient_env)
    recipients = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not recipients:
        raise NotificationError("No iMessage recipients are configured")
    return recipients
