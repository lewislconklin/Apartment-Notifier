from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlparse


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GeoPolygon:
    name: str
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class FilterConfig:
    neighborhoods: tuple[str, ...] = ()
    min_price: int | None = None
    max_price: int | None = None
    min_bedrooms: float | None = None
    max_bedrooms: float | None = None
    min_bathrooms: float | None = None
    available_on_or_before: date | None = None
    required_keywords: tuple[str, ...] = ()
    excluded_keywords: tuple[str, ...] = ()
    required_amenities: tuple[str, ...] = ()
    excluded_areas: tuple[GeoPolygon, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchConfig:
    name: str
    url: str
    filters: FilterConfig = field(default_factory=FilterConfig)


@dataclass(frozen=True, slots=True)
class StreetEasyConfig:
    allow_live_requests: bool = False
    permission_reference: str = ""
    allowed_url_prefix: str = "https://streeteasy.com/for-rent/nyc"
    minimum_request_interval_seconds: int = 40
    max_pages: int = 25
    rate_limit_state_file: Path = Path("data/streeteasy-rate-limit")
    user_agent: str = "ApartmentNotifier/0.2"
    timeout_seconds: int = 20


@dataclass(frozen=True, slots=True)
class EmailConfig:
    enabled: bool = False
    host: str = ""
    port: int = 587
    starttls: bool = True
    use_ssl: bool = False
    username_env: str = "SMTP_USERNAME"
    password_env: str = "SMTP_PASSWORD"
    password_keychain_service: str = ""
    password_keychain_account: str = ""
    from_address: str = ""
    to_addresses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SmsConfig:
    enabled: bool = False
    account_sid_env: str = "TWILIO_ACCOUNT_SID"
    auth_token_env: str = "TWILIO_AUTH_TOKEN"
    from_number: str = ""
    to_numbers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IMessageConfig:
    enabled: bool = False
    recipients: tuple[str, ...] = ()
    chat_id: str = ""
    recipient_env: str = "IMESSAGE_RECIPIENT"


@dataclass(frozen=True, slots=True)
class AppConfig:
    database: Path
    poll_interval_seconds: int
    full_scan_interval_seconds: int
    notify_existing_on_first_run: bool
    notify_price_changes: bool
    searches: tuple[SearchConfig, ...]
    streeteasy: StreetEasyConfig
    email: EmailConfig
    sms: SmsConfig
    imessage: IMessageConfig


def _tuple_of_strings(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field_name} must be an array of strings")
    return tuple(item.strip() for item in value if item.strip())


def _number(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field_name} must be a number")
    return float(value)


def _table(value: object, field_name: str) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field_name} must be a table")
    return value


def _boolean(value: object, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be true or false")
    return value


def _integer(value: object, field_name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field_name} must be a whole number")
    return value


def load_config(path: str | Path) -> AppConfig:
    config_path = _configured_path(Path.cwd(), str(path), "config path")
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise ConfigError("The configuration root must be a TOML table")

    app_raw = _table(raw.get("app"), "app")
    source_raw = _table(raw.get("streeteasy"), "streeteasy")
    notifications_raw = _table(raw.get("notifications"), "notifications")
    email_raw = _table(notifications_raw.get("email"), "notifications.email")
    sms_raw = _table(notifications_raw.get("sms"), "notifications.sms")
    imessage_raw = _table(
        notifications_raw.get("imessage"), "notifications.imessage"
    )

    database_raw = str(app_raw.get("database", "data/notifier.sqlite3"))
    database = _configured_path(config_path.parent, database_raw, "app.database")

    searches: list[SearchConfig] = []
    names: set[str] = set()
    searches_raw = raw.get("searches", [])
    if not isinstance(searches_raw, list):
        raise ConfigError("searches must be an array of tables")
    for index, raw_search in enumerate(searches_raw, start=1):
        search_raw = _table(raw_search, f"searches[{index}]")
        name = str(search_raw.get("name", "")).strip()
        url = str(search_raw.get("url", "")).strip()
        if not name:
            raise ConfigError(f"searches[{index}].name is required")
        if name in names:
            raise ConfigError(f"Duplicate search name: {name}")
        names.add(name)
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "streeteasy.com",
            "www.streeteasy.com",
        }:
            raise ConfigError(f"{name}: URL must be an https://streeteasy.com URL")

        filters_raw = _table(search_raw.get("filters"), f"{name}.filters")
        filters = FilterConfig(
            neighborhoods=_tuple_of_strings(filters_raw.get("neighborhoods"), "neighborhoods"),
            min_price=_optional_int(filters_raw.get("min_price"), "min_price"),
            max_price=_optional_int(filters_raw.get("max_price"), "max_price"),
            min_bedrooms=_number(filters_raw.get("min_bedrooms"), "min_bedrooms"),
            max_bedrooms=_number(filters_raw.get("max_bedrooms"), "max_bedrooms"),
            min_bathrooms=_number(filters_raw.get("min_bathrooms"), "min_bathrooms"),
            available_on_or_before=_calendar_date(
                filters_raw.get("available_on_or_before"),
                "available_on_or_before",
            ),
            required_keywords=_tuple_of_strings(
                filters_raw.get("required_keywords"), "required_keywords"
            ),
            excluded_keywords=_tuple_of_strings(
                filters_raw.get("excluded_keywords"), "excluded_keywords"
            ),
            required_amenities=_tuple_of_strings(
                filters_raw.get("required_amenities"), "required_amenities"
            ),
            excluded_areas=_geo_polygons(
                filters_raw.get("excluded_areas"), "excluded_areas"
            ),
        )
        _validate_filter(filters, name)
        searches.append(SearchConfig(name=name, url=url, filters=filters))

    if not searches:
        raise ConfigError("At least one [[searches]] entry is required")

    poll_interval = _integer(
        app_raw.get("poll_interval_seconds"), "poll_interval_seconds", 40
    )
    if poll_interval < 30:
        raise ConfigError("poll_interval_seconds must be at least 30")
    full_scan_interval = _integer(
        app_raw.get("full_scan_interval_seconds"),
        "full_scan_interval_seconds",
        3600,
    )
    if full_scan_interval < poll_interval:
        raise ConfigError(
            "full_scan_interval_seconds cannot be less than poll_interval_seconds"
        )

    streeteasy = StreetEasyConfig(
        allow_live_requests=_boolean(
            source_raw.get("allow_live_requests"),
            "streeteasy.allow_live_requests",
            False,
        ),
        permission_reference=str(source_raw.get("permission_reference", "")).strip(),
        allowed_url_prefix=str(
            source_raw.get(
                "allowed_url_prefix", "https://streeteasy.com/for-rent/nyc"
            )
        ).strip(),
        minimum_request_interval_seconds=_integer(
            source_raw.get("minimum_request_interval_seconds"),
            "streeteasy.minimum_request_interval_seconds",
            40,
        ),
        max_pages=_integer(
            source_raw.get("max_pages"), "streeteasy.max_pages", 25
        ),
        rate_limit_state_file=_configured_path(
            config_path.parent,
            str(
                source_raw.get(
                    "rate_limit_state_file", "data/streeteasy-rate-limit"
                )
            ),
            "streeteasy.rate_limit_state_file",
        ),
        user_agent=str(source_raw.get("user_agent", "ApartmentNotifier/0.2")).strip(),
        timeout_seconds=_integer(
            source_raw.get("timeout_seconds"), "streeteasy.timeout_seconds", 20
        ),
    )
    if streeteasy.allow_live_requests and not streeteasy.permission_reference:
        raise ConfigError(
            "permission_reference is required when allow_live_requests is true"
        )
    if streeteasy.allow_live_requests and (
        not streeteasy.user_agent
        or "you@example.com" in streeteasy.user_agent.casefold()
        or "mailto:" not in streeteasy.user_agent.casefold()
    ):
        raise ConfigError(
            "A contactable streeteasy.user_agent containing mailto: is required "
            "when allow_live_requests is true"
        )
    if streeteasy.timeout_seconds < 1:
        raise ConfigError("timeout_seconds must be positive")
    if streeteasy.minimum_request_interval_seconds < 30:
        raise ConfigError(
            "minimum_request_interval_seconds cannot be less than the authorized 30 seconds"
        )
    if not 1 <= streeteasy.max_pages <= 100:
        raise ConfigError("max_pages must be between 1 and 100")
    if streeteasy.allow_live_requests:
        _validate_authorized_search_urls(searches, streeteasy.allowed_url_prefix)

    email = EmailConfig(
        enabled=_boolean(
            email_raw.get("enabled"), "notifications.email.enabled", False
        ),
        host=str(email_raw.get("host", "")).strip(),
        port=_integer(email_raw.get("port"), "notifications.email.port", 587),
        starttls=_boolean(
            email_raw.get("starttls"), "notifications.email.starttls", True
        ),
        use_ssl=_boolean(
            email_raw.get("use_ssl"), "notifications.email.use_ssl", False
        ),
        username_env=str(email_raw.get("username_env", "SMTP_USERNAME")).strip(),
        password_env=str(email_raw.get("password_env", "SMTP_PASSWORD")).strip(),
        password_keychain_service=str(
            email_raw.get("password_keychain_service", "")
        ).strip(),
        password_keychain_account=str(
            email_raw.get("password_keychain_account", "")
        ).strip(),
        from_address=str(email_raw.get("from_address", "")).strip(),
        to_addresses=_tuple_of_strings(
            email_raw.get("to_addresses"), "email.to_addresses"
        ),
    )
    if email.enabled and (
        not email.host or not email.from_address or not email.to_addresses
    ):
        raise ConfigError("Enabled email requires host, from_address, and to_addresses")
    if not 1 <= email.port <= 65535:
        raise ConfigError("notifications.email.port must be between 1 and 65535")
    if email.starttls and email.use_ssl:
        raise ConfigError("Email cannot enable both starttls and use_ssl")
    if bool(email.password_keychain_service) != bool(email.password_keychain_account):
        raise ConfigError(
            "Email Keychain configuration requires both "
            "password_keychain_service and password_keychain_account"
        )

    sms = SmsConfig(
        enabled=_boolean(
            sms_raw.get("enabled"), "notifications.sms.enabled", False
        ),
        account_sid_env=str(
            sms_raw.get("account_sid_env", "TWILIO_ACCOUNT_SID")
        ).strip(),
        auth_token_env=str(
            sms_raw.get("auth_token_env", "TWILIO_AUTH_TOKEN")
        ).strip(),
        from_number=str(sms_raw.get("from_number", "")).strip(),
        to_numbers=_tuple_of_strings(sms_raw.get("to_numbers"), "sms.to_numbers"),
    )
    if sms.enabled and (not sms.from_number or not sms.to_numbers):
        raise ConfigError("Enabled SMS requires from_number and to_numbers")

    imessage = IMessageConfig(
        enabled=_boolean(
            imessage_raw.get("enabled"), "notifications.imessage.enabled", False
        ),
        recipients=_tuple_of_strings(
            imessage_raw.get("recipients"), "imessage.recipients"
        ),
        chat_id=str(imessage_raw.get("chat_id", "")).strip(),
        recipient_env=str(
            imessage_raw.get("recipient_env", "IMESSAGE_RECIPIENT")
        ).strip(),
    )
    if imessage.enabled and not imessage.recipients and not imessage.recipient_env:
        raise ConfigError("Enabled iMessage requires recipients or recipient_env")

    return AppConfig(
        database=database,
        poll_interval_seconds=poll_interval,
        full_scan_interval_seconds=full_scan_interval,
        notify_existing_on_first_run=_boolean(
            app_raw.get("notify_existing_on_first_run"),
            "app.notify_existing_on_first_run",
            False,
        ),
        notify_price_changes=_boolean(
            app_raw.get("notify_price_changes"), "app.notify_price_changes", True
        ),
        searches=tuple(searches),
        streeteasy=streeteasy,
        email=email,
        sms=sms,
        imessage=imessage,
    )


def _optional_int(value: object, field_name: str) -> int | None:
    number = _number(value, field_name)
    if number is None:
        return None
    if not number.is_integer():
        raise ConfigError(f"{field_name} must be a whole number")
    return int(number)


def _calendar_date(value: object, field_name: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        raise ConfigError(f"{field_name} must be a date in YYYY-MM-DD format")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ConfigError(
                f"{field_name} must be a date in YYYY-MM-DD format"
            ) from exc
    raise ConfigError(f"{field_name} must be a date in YYYY-MM-DD format")


def _geo_polygons(value: object, field_name: str) -> tuple[GeoPolygon, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} must be an array of tables")

    polygons: list[GeoPolygon] = []
    for index, raw_polygon in enumerate(value, start=1):
        label = f"{field_name}[{index}]"
        if not isinstance(raw_polygon, Mapping):
            raise ConfigError(f"{label} must be a table")
        name = str(raw_polygon.get("name", "")).strip()
        if not name:
            raise ConfigError(f"{label}.name is required")
        raw_points = raw_polygon.get("points")
        if not isinstance(raw_points, list) or len(raw_points) < 3:
            raise ConfigError(f"{label}.points must contain at least 3 coordinates")

        points: list[tuple[float, float]] = []
        for point_index, raw_point in enumerate(raw_points, start=1):
            point_label = f"{label}.points[{point_index}]"
            if not isinstance(raw_point, list) or len(raw_point) != 2:
                raise ConfigError(
                    f"{point_label} must be [latitude, longitude]"
                )
            latitude = _number(raw_point[0], f"{point_label} latitude")
            longitude = _number(raw_point[1], f"{point_label} longitude")
            assert latitude is not None and longitude is not None
            if not -90 <= latitude <= 90:
                raise ConfigError(f"{point_label} latitude must be between -90 and 90")
            if not -180 <= longitude <= 180:
                raise ConfigError(
                    f"{point_label} longitude must be between -180 and 180"
                )
            points.append((latitude, longitude))

        if len(set(points)) < 3 or _polygon_area(points) < 1e-12:
            raise ConfigError(f"{label}.points must form a non-empty polygon")
        polygons.append(GeoPolygon(name=name, points=tuple(points)))
    return tuple(polygons)


def _polygon_area(points: list[tuple[float, float]]) -> float:
    area = 0.0
    for (latitude_a, longitude_a), (latitude_b, longitude_b) in zip(
        points, points[1:] + points[:1]
    ):
        area += longitude_a * latitude_b - longitude_b * latitude_a
    return abs(area) / 2


_ENVIRONMENT_VARIABLE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\})")


def _configured_path(base_directory: Path, raw_path: str, field_name: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw_path))
    unresolved = _ENVIRONMENT_VARIABLE.search(expanded)
    if unresolved:
        raise ConfigError(
            f"{field_name} references an unset environment variable: "
            f"{unresolved.group(0)}"
        )
    path = Path(expanded)
    if not path.is_absolute():
        path = base_directory / path
    return path.resolve()


def _validate_authorized_search_urls(
    searches: list[SearchConfig], allowed_url_prefix: str
) -> None:
    allowed = urlparse(allowed_url_prefix)
    if (
        allowed.scheme != "https"
        or not allowed.hostname
        or allowed.query
        or allowed.fragment
    ):
        raise ConfigError(
            "allowed_url_prefix must be an HTTPS URL without a query or fragment"
        )
    allowed_path = allowed.path.rstrip("/")
    for search in searches:
        candidate = urlparse(search.url)
        in_scope = (
            candidate.scheme == allowed.scheme
            and candidate.hostname == allowed.hostname
            and candidate.port == allowed.port
            and (
                candidate.path.rstrip("/") == allowed_path
                or candidate.path.startswith(f"{allowed_path}/")
            )
        )
        if not in_scope:
            raise ConfigError(
                f"{search.name}: URL is outside the authorized scope "
                f"{allowed_url_prefix}"
            )


def _validate_filter(filters: FilterConfig, search_name: str) -> None:
    for field_name in (
        "min_price",
        "max_price",
        "min_bedrooms",
        "max_bedrooms",
        "min_bathrooms",
    ):
        value = getattr(filters, field_name)
        if value is not None and value < 0:
            raise ConfigError(f"{search_name}: {field_name} cannot be negative")
    if (
        filters.min_price is not None
        and filters.max_price is not None
        and filters.min_price > filters.max_price
    ):
        raise ConfigError(f"{search_name}: min_price cannot exceed max_price")
    if (
        filters.min_bedrooms is not None
        and filters.max_bedrooms is not None
        and filters.min_bedrooms > filters.max_bedrooms
    ):
        raise ConfigError(f"{search_name}: min_bedrooms cannot exceed max_bedrooms")
