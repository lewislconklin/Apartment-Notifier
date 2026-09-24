from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import getpass
import json
import os
from pathlib import Path
import tempfile
from collections.abc import Callable
from urllib.parse import urlparse

from .config import ConfigError
from .keychain import KeychainError, store_password


KEYCHAIN_SERVICE = "Apartment Notifier Gmail"


@dataclass(slots=True)
class WizardSearch:
    name: str
    url: str
    neighborhoods: tuple[str, ...]
    min_price: int | None
    max_price: int | None
    min_bedrooms: float | None
    max_bedrooms: float | None
    min_bathrooms: float | None
    available_on_or_before: str
    required_keywords: tuple[str, ...]
    excluded_keywords: tuple[str, ...]
    required_amenities: tuple[str, ...]


def configure(
    path: str | Path,
    *,
    force: bool = False,
    input_fn: Callable[[str], str] = input,
    password_fn: Callable[[str], str] = getpass.getpass,
    output_fn: Callable[[str], None] = print,
) -> Path:
    config_path = _config_path(path)
    if config_path.exists() and not force:
        raise ConfigError(
            f"Config already exists: {config_path}. Use configure --force only "
            "if you intend to replace it; your database will not be changed."
        )

    output_fn("Apartment Notifier guided configuration")
    output_fn("Create each StreetEasy search in your browser, then paste its full URL.")
    searches = _collect_searches(input_fn, output_fn)

    imessage_enabled = _yes_no(input_fn, "Enable iMessage notifications?", True)
    imessage_recipients: tuple[str, ...] = ()
    imessage_chat_id = ""
    if imessage_enabled:
        imessage_chat_id = input_fn(
            "Existing Messages chat ID (leave blank for separate messages): "
        ).strip()
        if not imessage_chat_id:
            imessage_recipients = _required_csv(
                input_fn,
                "Phone numbers or Apple IDs, comma-separated: ",
            )

    email_enabled = _yes_no(input_fn, "Enable Gmail email notifications?", False)
    email_address = ""
    email_recipients: tuple[str, ...] = ()
    keychain_service = ""
    keychain_account = ""
    if email_enabled:
        email_address = _required(input_fn, "Gmail address: ")
        email_recipients = _required_csv(
            input_fn, "Email recipients, comma-separated: "
        )
        app_password = password_fn(
            "Gmail App Password (leave blank to supply SMTP_PASSWORD later): "
        ).strip()
        if app_password:
            try:
                store_password(KEYCHAIN_SERVICE, email_address, app_password)
            except KeychainError as exc:
                raise ConfigError(str(exc)) from exc
            keychain_service = KEYCHAIN_SERVICE
            keychain_account = email_address
            output_fn("Saved the Gmail App Password in macOS Keychain.")

    allow_live = _yes_no(
        input_fn,
        "Have you personally received written permission to automate StreetEasy requests?",
        False,
    )
    permission_reference = ""
    contact_email = "you@example.com"
    if allow_live:
        permission_reference = _required(
            input_fn, "Permission ticket, agreement, or email reference: "
        )
        contact_email = _required(input_fn, "Contact email for the User-Agent: ")

    contents = _render_config(
        searches,
        imessage_enabled=imessage_enabled,
        imessage_recipients=imessage_recipients,
        imessage_chat_id=imessage_chat_id,
        email_enabled=email_enabled,
        email_address=email_address,
        email_recipients=email_recipients,
        keychain_service=keychain_service,
        keychain_account=keychain_account,
        allow_live=allow_live,
        permission_reference=permission_reference,
        contact_email=contact_email,
    )
    _atomic_write(config_path, contents)
    output_fn(f"Wrote {config_path}")
    if not allow_live:
        output_fn("Live StreetEasy requests remain disabled.")
    output_fn("Next: ./apartment-notifier validate")
    return config_path


def _collect_searches(
    input_fn: Callable[[str], str], output_fn: Callable[[str], None]
) -> list[WizardSearch]:
    searches: list[WizardSearch] = []
    names: set[str] = set()
    while True:
        output_fn(f"\nSearch {len(searches) + 1}")
        name = _required(input_fn, "Short unique name: ")
        if name in names:
            output_fn("That search name is already in use.")
            continue
        url = _street_easy_url(input_fn)
        search = WizardSearch(
            name=name,
            url=url,
            neighborhoods=_csv(input_fn("Local neighborhoods (comma-separated, optional): ")),
            min_price=_optional_int(input_fn, "Minimum monthly rent (optional): "),
            max_price=_optional_int(input_fn, "Maximum monthly rent (optional): "),
            min_bedrooms=_optional_float(input_fn, "Minimum bedrooms (optional): "),
            max_bedrooms=_optional_float(input_fn, "Maximum bedrooms (optional): "),
            min_bathrooms=_optional_float(input_fn, "Minimum bathrooms (optional): "),
            available_on_or_before=_optional_date(
                input_fn, "Available on or before YYYY-MM-DD (optional): "
            ),
            required_keywords=_csv(input_fn("Required keywords (comma-separated, optional): ")),
            excluded_keywords=_csv(input_fn("Excluded keywords (comma-separated, optional): ")),
            required_amenities=_csv(input_fn("Required amenities (comma-separated, optional): ")),
        )
        if (
            search.min_price is not None
            and search.max_price is not None
            and search.min_price > search.max_price
        ):
            raise ConfigError("Minimum rent cannot exceed maximum rent")
        if (
            search.min_bedrooms is not None
            and search.max_bedrooms is not None
            and search.min_bedrooms > search.max_bedrooms
        ):
            raise ConfigError("Minimum bedrooms cannot exceed maximum bedrooms")
        searches.append(search)
        names.add(name)
        if not _yes_no(input_fn, "Add another search?", False):
            return searches


def _render_config(
    searches: list[WizardSearch],
    *,
    imessage_enabled: bool,
    imessage_recipients: tuple[str, ...],
    imessage_chat_id: str,
    email_enabled: bool,
    email_address: str,
    email_recipients: tuple[str, ...],
    keychain_service: str,
    keychain_account: str,
    allow_live: bool,
    permission_reference: str,
    contact_email: str,
) -> str:
    lines = [
        "# Generated by apartment-notifier configure.",
        "# Advanced options are documented in config.example.toml and README.md.",
        "",
        "[app]",
        'database = "data/notifier.sqlite3"',
        "poll_interval_seconds = 40",
        "full_scan_interval_seconds = 3600",
        "notify_existing_on_first_run = false",
        "notify_price_changes = true",
        "",
        "[streeteasy]",
        f"allow_live_requests = {_toml_bool(allow_live)}",
        f"permission_reference = {_toml_string(permission_reference)}",
        'allowed_url_prefix = "https://streeteasy.com/for-rent/nyc"',
        "minimum_request_interval_seconds = 40",
        "max_pages = 25",
        'rate_limit_state_file = "data/streeteasy-rate-limit"',
        f"user_agent = {_toml_string(f'ApartmentNotifier/0.2 (+mailto:{contact_email})')}",
        "timeout_seconds = 20",
    ]
    for search in searches:
        lines.extend(
            [
                "",
                "[[searches]]",
                f"name = {_toml_string(search.name)}",
                f"url = {_toml_string(search.url)}",
                "",
                "[searches.filters]",
                f"neighborhoods = {_toml_array(search.neighborhoods)}",
            ]
        )
        for key, value in (
            ("min_price", search.min_price),
            ("max_price", search.max_price),
            ("min_bedrooms", search.min_bedrooms),
            ("max_bedrooms", search.max_bedrooms),
            ("min_bathrooms", search.min_bathrooms),
        ):
            if value is not None:
                lines.append(f"{key} = {_toml_number(value)}")
        if search.available_on_or_before:
            lines.append(
                f"available_on_or_before = {search.available_on_or_before}"
            )
        lines.extend(
            [
                f"required_keywords = {_toml_array(search.required_keywords)}",
                f"excluded_keywords = {_toml_array(search.excluded_keywords)}",
                f"required_amenities = {_toml_array(search.required_amenities)}",
            ]
        )

    lines.extend(
        [
            "",
            "[notifications.email]",
            f"enabled = {_toml_bool(email_enabled)}",
            'host = "smtp.gmail.com"',
            "port = 587",
            "starttls = true",
            "use_ssl = false",
            'username_env = "SMTP_USERNAME"',
            'password_env = "SMTP_PASSWORD"',
            f"password_keychain_service = {_toml_string(keychain_service)}",
            f"password_keychain_account = {_toml_string(keychain_account)}",
            f"from_address = {_toml_string(email_address)}",
            f"to_addresses = {_toml_array(email_recipients)}",
            "",
            "[notifications.sms]",
            "enabled = false",
            'account_sid_env = "TWILIO_ACCOUNT_SID"',
            'auth_token_env = "TWILIO_AUTH_TOKEN"',
            'from_number = ""',
            "to_numbers = []",
            "",
            "[notifications.imessage]",
            f"enabled = {_toml_bool(imessage_enabled)}",
            f"recipients = {_toml_array(imessage_recipients)}",
            f"chat_id = {_toml_string(imessage_chat_id)}",
            'recipient_env = "IMESSAGE_RECIPIENT"',
            "",
        ]
    )
    return "\n".join(lines)


def _config_path(path: str | Path) -> Path:
    raw = os.path.expandvars(os.path.expanduser(str(path)))
    if "$" in raw:
        raise ConfigError(f"Config path contains an unset environment variable: {raw}")
    return Path(raw).resolve()


def _atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            handle.write(contents)
            temporary_path = Path(handle.name)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _required(input_fn: Callable[[str], str], prompt: str) -> str:
    while True:
        value = input_fn(prompt).strip()
        if value:
            return value


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _required_csv(input_fn: Callable[[str], str], prompt: str) -> tuple[str, ...]:
    while True:
        values = _csv(input_fn(prompt))
        if values:
            return values


def _yes_no(input_fn: Callable[[str], str], prompt: str, default: bool) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        value = input_fn(prompt + suffix).strip().casefold()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False


def _street_easy_url(input_fn: Callable[[str], str]) -> str:
    while True:
        value = _required(input_fn, "Full StreetEasy rental-search URL: ")
        parsed = urlparse(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"streeteasy.com", "www.streeteasy.com"}
            and parsed.path.startswith("/for-rent/")
        ):
            return value


def _optional_int(input_fn: Callable[[str], str], prompt: str) -> int | None:
    while True:
        raw = input_fn(prompt).strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        if value >= 0:
            return value


def _optional_float(input_fn: Callable[[str], str], prompt: str) -> float | None:
    while True:
        raw = input_fn(prompt).strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            continue
        if value >= 0:
            return value


def _optional_date(input_fn: Callable[[str], str], prompt: str) -> str:
    while True:
        raw = input_fn(prompt).strip()
        if not raw:
            return ""
        try:
            date.fromisoformat(raw)
        except ValueError:
            continue
        return raw


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_number(value: int | float) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)
