from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, parse_qs, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .config import SearchConfig, StreetEasyConfig
from .models import Listing


class SourceError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PermissionRequiredError(SourceError):
    pass


@dataclass(frozen=True, slots=True)
class SearchPage:
    listings: tuple[Listing, ...]
    current_page: int
    total_pages: int
    total_results: int | None


@dataclass(slots=True)
class StreetEasySource:
    config: StreetEasyConfig
    _rate_limiter: _RequestRateLimiter = field(init=False, repr=False)
    _consecutive_403s: int = field(init=False, default=0, repr=False)

    def __post_init__(self) -> None:
        self._rate_limiter = _RequestRateLimiter(
            self.config.rate_limit_state_file,
            self.config.minimum_request_interval_seconds,
        )

    def fetch(
        self, search: SearchConfig, *, full_scan: bool = True
    ) -> list[Listing]:
        if not self.config.allow_live_requests or not self.config.permission_reference:
            raise PermissionRequiredError(
                "Live StreetEasy requests are locked. Set allow_live_requests=true "
                "and add the written authorization identifier to permission_reference."
            )

        first_url = _page_url(search.url, 1)
        first_page = parse_search_page(self._fetch_html(first_url), first_url)
        total_pages = first_page.total_pages
        if full_scan and total_pages > self.config.max_pages:
            raise SourceError(
                f"StreetEasy reported {total_pages} pages for {search.name!r}, "
                f"which exceeds the configured max_pages={self.config.max_pages}"
            )

        result_count = (
            f"{first_page.total_results} result(s)"
            if first_page.total_results is not None
            else "results"
        )
        if not full_scan:
            print(
                f"StreetEasy: checked newest page for {search.name!r} "
                f"({result_count} total).",
                file=sys.stderr,
                flush=True,
            )
            return list(first_page.listings)

        if total_pages > 1:
            print(
                f"StreetEasy: {result_count} across {total_pages} pages for "
                f"{search.name!r}; fetching every page with request starts at least "
                f"{self.config.minimum_request_interval_seconds}s apart.",
                file=sys.stderr,
                flush=True,
            )

        listings: dict[str, Listing] = {}
        _merge_listings(listings, first_page.listings)
        for page_number in range(2, total_pages + 1):
            print(
                f"StreetEasy: fetching page {page_number}/{total_pages} for "
                f"{search.name!r}...",
                file=sys.stderr,
                flush=True,
            )
            page_url = _page_url(search.url, page_number)
            page = parse_search_page(self._fetch_html(page_url), page_url)
            _merge_listings(listings, page.listings)
        return list(listings.values())

    def _fetch_html(self, url: str) -> str:
        page_number = _current_page(url)
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": self.config.user_agent,
            },
        )
        self._rate_limiter.wait()
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                content_type = response.headers.get_content_charset() or "utf-8"
                html = response.read().decode(content_type, errors="replace")
        except HTTPError as exc:
            diagnostic_headers = _safe_response_headers(exc.headers)
            if exc.code == 403:
                self._consecutive_403s += 1
                exponential_delay = min(
                    120 * (2 ** min(self._consecutive_403s - 1, 4)),
                    1800,
                )
                server_delay = _retry_after_seconds(exc.headers.get("Retry-After"))
                backoff_seconds = max(exponential_delay, server_delay or 0)
                raise SourceError(
                    f"StreetEasy returned HTTP 403 for page {page_number}; "
                    f"consecutive 403s={self._consecutive_403s}; "
                    f"diagnostic response headers: {diagnostic_headers}; "
                    f"backing off for {backoff_seconds}s",
                    retry_after_seconds=backoff_seconds,
                ) from exc

            self._consecutive_403s = 0
            raise SourceError(
                f"StreetEasy returned HTTP {exc.code} for page {page_number}; "
                f"diagnostic response headers: {diagnostic_headers}"
            ) from exc
        except URLError as exc:
            self._consecutive_403s = 0
            raise SourceError(
                f"Could not reach StreetEasy for page {page_number}: {exc.reason}"
            ) from exc

        self._consecutive_403s = 0

        lowered = html.casefold()
        if "captcha" in lowered or "verify you are human" in lowered:
            raise SourceError(
                f"StreetEasy returned an anti-automation challenge for page "
                f"{page_number}; stopping"
            )
        return html


_DIAGNOSTIC_RESPONSE_HEADERS = (
    "Date",
    "Retry-After",
    "Server",
    "Via",
    "X-Request-ID",
    "X-Zillow-Request-ID",
    "X-Cache",
    "CF-Ray",
    "X-Amz-Cf-Id",
)


def _safe_response_headers(headers: Mapping[str, str] | None) -> str:
    """Return a small allowlist of useful headers, excluding cookies and secrets."""
    if headers is None:
        return "none present"

    details: list[str] = []
    for name in _DIAGNOSTIC_RESPONSE_HEADERS:
        value = headers.get(name)
        if not value:
            continue
        sanitized = " ".join(str(value).split())[:200]
        details.append(f"{name}={sanitized!r}")
    return ", ".join(details) if details else "none present"


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    raw = value.strip()
    if raw.isdigit():
        return max(0, int(raw))
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    remaining = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return max(0, math.ceil(remaining))


@dataclass(slots=True)
class _RequestRateLimiter:
    """Coordinate request starts across processes using an advisory file lock."""

    path: Path
    minimum_interval_seconds: int

    def wait(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw = handle.read().strip()
                try:
                    last_started_at = float(raw) if raw else 0.0
                except ValueError:
                    last_started_at = 0.0

                now = time.time()
                delay = last_started_at + self.minimum_interval_seconds - now
                if delay > 0:
                    time.sleep(min(delay, float(self.minimum_interval_seconds)))

                started_at = time.time()
                handle.seek(0)
                handle.truncate()
                handle.write(f"{started_at:.6f}\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FixtureStreetEasySource:
    def __init__(self, html_by_search: Mapping[str, str]):
        self._html_by_search = html_by_search

    def fetch(
        self, search: SearchConfig, *, full_scan: bool = True
    ) -> list[Listing]:
        try:
            html = self._html_by_search[search.name]
        except KeyError as exc:
            raise SourceError(f"No fixture supplied for search: {search.name}") from exc
        return parse_search_html(html, search.url)


class _StructuredDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_blocks: list[str] = []
        self.script_blocks: list[str] = []
        self.cards: list[dict[str, str]] = []
        self.links: list[str] = []
        self._in_script = False
        self._script_is_json = False
        self._script_parts: list[str] = []
        self._active_card: dict[str, str] | None = None
        self._active_card_tag = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        if tag == "script":
            self._in_script = True
            self._script_is_json = (
                "json" in attributes.get("type", "").casefold()
                or attributes.get("id") == "__NEXT_DATA__"
            )
            self._script_parts = []
            return

        listing_id = attributes.get("data-listing-id", "")
        if listing_id:
            card = {
                "listing_id": listing_id,
                "url": attributes.get("data-url", ""),
                "address": attributes.get("data-address", ""),
                "price": attributes.get("data-price", ""),
                "bedrooms": attributes.get("data-bedrooms", ""),
                "bathrooms": attributes.get("data-bathrooms", ""),
                "available_at": attributes.get("data-available-at", ""),
                "neighborhood": attributes.get("data-neighborhood", ""),
                "description": attributes.get("data-description", ""),
                "amenities": attributes.get("data-amenities", ""),
            }
            self.cards.append(card)
            self._active_card = card
            self._active_card_tag = tag
        elif tag == "a" and self._active_card is not None and not self._active_card.get("url"):
            self._active_card["url"] = attributes.get("href", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script:
            self._in_script = False
            block = "".join(self._script_parts)
            self.script_blocks.append(block)
            if self._script_is_json:
                self.json_blocks.append(block)
            self._script_is_json = False
            self._script_parts = []
        if tag == self._active_card_tag:
            self._active_card = None
            self._active_card_tag = ""

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_parts.append(data)


def parse_search_html(html: str, base_url: str) -> list[Listing]:
    return list(parse_search_page(html, base_url).listings)


def parse_search_page(html: str, base_url: str) -> SearchPage:
    parser = _StructuredDataParser()
    parser.feed(html)

    candidates: list[Mapping[str, Any]] = list(parser.cards)
    for payload in _flight_records(parser.script_blocks):
        candidates.extend(_candidate_mappings(payload))
    for block in parser.json_blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        candidates.extend(_candidate_mappings(payload))

    listings: dict[str, Listing] = {}
    for candidate in candidates:
        listing = _mapping_to_listing(candidate, base_url)
        if listing is None:
            continue
        existing = listings.get(listing.listing_id)
        if existing is None or _completeness(listing) > _completeness(existing):
            listings[listing.listing_id] = listing
    current_page = _current_page(base_url)
    total_pages = max(
        current_page,
        _largest_linked_page(parser.links, base_url),
        _embedded_integer(html, "totalPages") or 1,
    )
    total_results = _embedded_integer(html, "totalCount", "totalResults")
    return SearchPage(
        listings=tuple(listings.values()),
        current_page=current_page,
        total_pages=total_pages,
        total_results=total_results,
    )


def _merge_listings(
    destination: dict[str, Listing], candidates: Iterable[Listing]
) -> None:
    for listing in candidates:
        existing = destination.get(listing.listing_id)
        if existing is None or _completeness(listing) > _completeness(existing):
            destination[listing.listing_id] = listing


def _current_page(url: str) -> int:
    values = parse_qs(urlparse(url).query).get("page", [])
    if values and values[0].isdigit():
        return max(1, int(values[0]))
    return 1


def _largest_linked_page(links: Iterable[str], base_url: str) -> int:
    base = urlparse(base_url)
    largest = _current_page(base_url)
    for link in links:
        candidate = urlparse(urljoin(base_url, link))
        if (
            candidate.scheme != base.scheme
            or candidate.hostname != base.hostname
            or candidate.port != base.port
            or candidate.path != base.path
        ):
            continue
        for value in parse_qs(candidate.query).get("page", []):
            if value.isdigit():
                largest = max(largest, int(value))
    return largest


def _embedded_integer(html: str, *field_names: str) -> int | None:
    for field_name in field_names:
        patterns = (
            rf'\\?"{re.escape(field_name)}\\?"\s*:\s*(\d+)',
            rf'&quot;{re.escape(field_name)}&quot;\s*:\s*(\d+)',
        )
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return int(match.group(1))
    return None


def _page_url(url: str, page_number: int) -> str:
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "page"
    ]
    if page_number > 1:
        query.append(("page", str(page_number)))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _candidate_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if _looks_like_listing(value):
            yield value
        for child in value.values():
            yield from _candidate_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _candidate_mappings(child)


def _flight_records(script_blocks: Iterable[str]) -> Iterable[Any]:
    chunks: list[str] = []
    for block in script_blocks:
        match = re.fullmatch(
            r"\s*self\.__next_f\.push\((.*)\)\s*", block, re.DOTALL
        )
        if not match:
            continue
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, list)
            and len(value) > 1
            and isinstance(value[1], str)
        ):
            chunks.append(value[1])

    for line in "".join(chunks).splitlines():
        if ":" not in line:
            continue
        _, payload = line.split(":", 1)
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _looks_like_listing(value: Mapping[str, Any]) -> bool:
    keys = {_key(key) for key in value}
    has_url = bool(keys & {"url", "urlpath", "canonicalurl", "permalink", "href"})
    has_identity = bool(
        keys & {"listingid", "id", "address", "streetaddress", "street"}
    )
    has_listing_data = bool(
        keys
        & {
            "price",
            "rentalprice",
            "bedrooms",
            "bedroomcount",
            "beds",
            "bathrooms",
            "fullbathroomcount",
            "baths",
        }
    )
    type_value = str(value.get("@type", "")).casefold()
    typed_listing = any(word in type_value for word in ("residence", "apartment", "product"))
    return has_url and has_identity and (has_listing_data or typed_listing)


def _mapping_to_listing(value: Mapping[str, Any], base_url: str) -> Listing | None:
    normalized = {_key(key): item for key, item in value.items()}
    raw_url = _first(
        normalized, "url", "urlpath", "canonicalurl", "permalink", "href"
    )
    if isinstance(raw_url, Mapping):
        raw_url = _first({_key(k): v for k, v in raw_url.items()}, "url", "href")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    url = urljoin(base_url, raw_url.strip())
    parsed = urlparse(url)
    if parsed.hostname not in {"streeteasy.com", "www.streeteasy.com"}:
        return None

    raw_id = _first(normalized, "listingid")
    listing_id = str(raw_id).strip() if raw_id is not None else ""
    if not listing_id or listing_id.startswith("http"):
        listing_id = sha256(url.encode()).hexdigest()[:20]

    raw_address = _first(normalized, "address", "streetaddress", "street")
    listing_name = _text(_first(normalized, "name", "headline"))
    address = _listing_address(raw_address, listing_name)
    unit = _text(_first(normalized, "displayunit", "unit"))
    if address and unit and unit.casefold() not in address.casefold():
        address = f"{address} {unit}"
    if not address:
        address = _address_from_url(url)
    if not address:
        return None

    price = _money(_first(normalized, "neteffectiveprice"))
    if price in (None, 0):
        price = _money(
            _first(
                normalized,
                "totalmonthlyprice",
                "price",
                "rentalprice",
                "monthlyprice",
                "offers",
            )
        )
    if price is None:
        price = _money(
            _named_property_value(normalized.get("additionalproperty"), "monthly rent")
        )
    bedrooms = _decimal(
        _first(normalized, "bedrooms", "bedroomcount", "beds", "numberofbedrooms")
    )
    bathrooms = _decimal(
        _first(
            normalized,
            "bathrooms",
            "baths",
            "numberofbathroomstotal",
            "numberofbathrooms",
            "numberoffullbathrooms",
            "fullbathroomcount",
            "fullbathrooms",
        )
    )
    half_bathrooms = _decimal(_first(normalized, "halfbathroomcount"))
    if bathrooms is not None and half_bathrooms is not None:
        bathrooms += half_bathrooms * 0.5
    available_date = _calendar_date(
        _first(
            normalized,
            "availableat",
            "availabledate",
            "availabilitydate",
            "moveindate",
            "moveindt",
        )
    )
    neighborhood = _text(_first(normalized, "neighborhood", "area", "areaname"))
    if not neighborhood and isinstance(raw_address, Mapping):
        address_fields = {_key(key): item for key, item in raw_address.items()}
        neighborhood = _text(
            _first(address_fields, "addresslocality", "neighborhood", "city")
        )
    description = _text(_first(normalized, "description", "summary"))
    amenities = _amenities(_first(normalized, "amenities", "amenityfeature", "features"))
    latitude, longitude = _coordinates(normalized)

    return Listing(
        listing_id=listing_id,
        url=url,
        address=address,
        price=price,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        available_date=available_date,
        neighborhood=neighborhood or None,
        latitude=latitude,
        longitude=longitude,
        description=description,
        amenities=amenities,
    )


def _key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        candidate = value.get(key)
        if candidate not in (None, "", [], {}):
            return candidate
    return None


def _address(value: Any) -> str:
    if isinstance(value, Mapping):
        normalized = {_key(key): item for key, item in value.items()}
        street = _text(_first(normalized, "streetaddress", "address", "name"))
        unit = _text(_first(normalized, "unit", "unitnumber", "apartment"))
        locality = _text(_first(normalized, "addresslocality", "city"))
        return ", ".join(part for part in (" ".join(p for p in (street, unit) if p), locality) if part)
    return _text(value)


def _listing_address(value: Any, listing_name: str) -> str:
    """Prefer the listing name because StreetEasy includes the unit there."""
    if not isinstance(value, Mapping):
        return _address(value) or listing_name
    normalized = {_key(key): item for key, item in value.items()}
    locality = _text(_first(normalized, "addresslocality", "city"))
    if listing_name:
        if locality and locality.casefold() not in listing_name.casefold():
            return f"{listing_name}, {locality}"
        return listing_name
    return _address(value)


def _address_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if "building" in parts:
        index = parts.index("building")
        if len(parts) > index + 1:
            return parts[index + 1].replace("-", " ").title()
    return ""


def _money(value: Any) -> int | None:
    if isinstance(value, Mapping):
        normalized = {_key(key): item for key, item in value.items()}
        value = _first(normalized, "price", "lowprice", "highprice")
    number = _decimal(value)
    return round(number) if number is not None else None


def _coordinates(value: Mapping[str, Any]) -> tuple[float | None, float | None]:
    latitude = _decimal(_first(value, "latitude", "lat"))
    longitude = _decimal(_first(value, "longitude", "lon", "lng"))
    raw_geo = _first(value, "geo", "geocoordinates", "location")
    if isinstance(raw_geo, Mapping):
        normalized_geo = {_key(key): item for key, item in raw_geo.items()}
        if latitude is None:
            latitude = _decimal(_first(normalized_geo, "latitude", "lat"))
        if longitude is None:
            longitude = _decimal(
                _first(normalized_geo, "longitude", "lon", "lng")
            )
    if latitude is not None and not -90 <= latitude <= 90:
        latitude = None
    if longitude is not None and not -180 <= longitude <= 180:
        longitude = None
    if latitude is None or longitude is None:
        return None, None
    return latitude, longitude


def _named_property_value(value: Any, property_name: str) -> Any:
    if not isinstance(value, list):
        return None
    expected = property_name.casefold()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        normalized = {_key(key): child for key, child in item.items()}
        name = _text(normalized.get("name")).casefold()
        if name == expected:
            return normalized.get("value")
    return None


def _decimal(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


def _calendar_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:$|[T ])", text)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return " ".join(str(value).split())
    return ""


def _amenities(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                result.append(item.strip())
            elif isinstance(item, Mapping):
                normalized = {_key(key): child for key, child in item.items()}
                text = _text(_first(normalized, "name", "value"))
                if text:
                    result.append(text)
        return tuple(item for item in result if item)
    return ()


def _completeness(listing: Listing) -> int:
    return sum(
        value not in (None, "", ())
        for value in (
            listing.address,
            listing.price,
            listing.bedrooms,
            listing.bathrooms,
            listing.available_date,
            listing.neighborhood,
            listing.latitude,
            listing.longitude,
            listing.description,
            listing.amenities,
        )
    )
