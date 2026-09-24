from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import date
from email.message import Message
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from apartment_notifier.config import SearchConfig, StreetEasyConfig
from apartment_notifier.streeteasy import (
    PermissionRequiredError,
    SourceError,
    StreetEasySource,
    _RequestRateLimiter,
    parse_search_html,
    parse_search_page,
)


FIXTURE = Path(__file__).parent / "fixtures" / "search_results.html"


def page_html(listing_id: str, *, total_pages: int = 1) -> str:
    last_page_link = (
        f'<a href="/for-rent/nyc/example?sort_by=listed_desc&amp;page={total_pages}">Last</a>'
        if total_pages > 1
        else ""
    )
    return f"""
    <script type="application/ld+json">
    {{
      "@graph": [{{
        "@type": "Apartment",
        "url": "/building/example/{listing_id}",
        "name": "1 Example Street #{listing_id}",
        "address": {{"streetAddress": "1 Example Street", "addressLocality": "Soho"}},
        "numberOfBedrooms": 2,
        "numberOfBathroomsTotal": 1,
        "additionalProperty": [{{"name": "Monthly Rent", "value": "$7,500"}}]
      }}]
    }}
    </script>
    <script>window.data = "{{\\\"totalCount\\\":25,\\\"totalPages\\\":{total_pages}}}";</script>
    {last_page_link}
    """


class StreetEasyParserTests(unittest.TestCase):
    @patch("apartment_notifier.streeteasy.urlopen")
    def test_live_request_lock_prevents_network_access(self, urlopen) -> None:
        with TemporaryDirectory() as directory:
            source = StreetEasySource(
                StreetEasyConfig(
                    rate_limit_state_file=Path(directory) / "limit",
                )
            )
            search = SearchConfig(
                "test", "https://streeteasy.com/for-rent/nyc"
            )
            with self.assertRaises(PermissionRequiredError):
                source.fetch(search)

        urlopen.assert_not_called()

    def test_parses_structured_listing_data(self) -> None:
        listings = parse_search_html(
            FIXTURE.read_text(encoding="utf-8"),
            "https://streeteasy.com/for-rent/nyc",
        )

        self.assertEqual(2, len(listings))
        first = {listing.listing_id: listing for listing in listings}["se-1001"]
        self.assertEqual("123 Example Street #4A, Greenpoint", first.address)
        self.assertEqual(4200, first.price)
        self.assertEqual(2, first.bedrooms)
        self.assertEqual(1.5, first.bathrooms)
        self.assertEqual("Greenpoint", first.neighborhood)
        self.assertEqual(40.7301, first.latitude)
        self.assertEqual(-73.9542, first.longitude)
        self.assertIn("In-unit laundry", first.amenities)
        self.assertTrue(first.url.startswith("https://streeteasy.com/building/"))

    def test_parses_data_attribute_fallback(self) -> None:
        html = """
        <article data-listing-id="se-2001" data-price="3995"
          data-address="789 Test Road #5" data-bedrooms="1"
          data-bathrooms="1" data-neighborhood="Astoria"
          data-amenities="Laundry,Elevator">
          <img src="example.jpg">
          <a href="/building/789-test-road-queens/5">View</a>
        </article>
        """
        listings = parse_search_html(html, "https://streeteasy.com/for-rent/nyc")
        self.assertEqual(1, len(listings))
        self.assertEqual("se-2001", listings[0].listing_id)
        self.assertEqual(("Laundry", "Elevator"), listings[0].amenities)

    def test_parses_nextjs_flight_listing_data(self) -> None:
        record = {
            "id": "5157187",
            "areaName": "Williamsburg",
            "availableAt": "2026-10-31",
            "bedroomCount": 2,
            "fullBathroomCount": 1,
            "halfBathroomCount": 1,
            "netEffectivePrice": 7188,
            "price": 7600,
            "street": "209 North 11th Street",
            "displayUnit": "#7F",
            "latitude": 40.7178,
            "longitude": -73.9547,
            "urlPath": "/building/209-north-11-street-brooklyn/7f",
        }
        flight_chunk = f"61:{json.dumps(record)}\n"
        html = (
            "<script>self.__next_f.push("
            f"{json.dumps([1, flight_chunk])}"
            ")</script>"
        )

        listings = parse_search_html(
            html, "https://streeteasy.com/for-rent/nyc?sort_by=listed_desc"
        )

        self.assertEqual(1, len(listings))
        self.assertEqual("209 North 11th Street #7F", listings[0].address)
        self.assertEqual(7188, listings[0].price)
        self.assertEqual(2, listings[0].bedrooms)
        self.assertEqual(1.5, listings[0].bathrooms)
        self.assertEqual("Williamsburg", listings[0].neighborhood)
        self.assertEqual(date(2026, 10, 31), listings[0].available_date)
        self.assertEqual(40.7178, listings[0].latitude)
        self.assertEqual(-73.9547, listings[0].longitude)

    def test_cross_process_rate_limiter_waits_for_remaining_interval(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "limit"
            path.write_text("100.0\n", encoding="utf-8")
            limiter = _RequestRateLimiter(path, 60)
            with (
                patch("apartment_notifier.streeteasy.time.time", return_value=130.0),
                patch("apartment_notifier.streeteasy.time.sleep") as sleep,
            ):
                limiter.wait()
            sleep.assert_called_once_with(30.0)

    def test_parses_pagination_metadata(self) -> None:
        page = parse_search_page(
            page_html("one", total_pages=3),
            "https://streeteasy.com/for-rent/nyc/example?sort_by=listed_desc",
        )

        self.assertEqual(1, page.current_page)
        self.assertEqual(3, page.total_pages)
        self.assertEqual(25, page.total_results)
        self.assertEqual(1, len(page.listings))

    def test_source_fetches_and_deduplicates_every_page(self) -> None:
        with TemporaryDirectory() as directory:
            source = StreetEasySource(
                StreetEasyConfig(
                    allow_live_requests=True,
                    permission_reference="ticket-123",
                    max_pages=3,
                    rate_limit_state_file=Path(directory) / "limit",
                )
            )
            search = SearchConfig(
                "test",
                "https://streeteasy.com/for-rent/nyc/example?sort_by=listed_desc",
            )
            responses = [
                page_html("one", total_pages=3),
                page_html("two", total_pages=3),
                page_html("three", total_pages=3),
            ]

            with patch.object(
                StreetEasySource, "_fetch_html", side_effect=responses
            ) as fetch_html:
                listings = source.fetch(search)

            self.assertEqual(3, len(listings))
            self.assertEqual(
                [
                    "https://streeteasy.com/for-rent/nyc/example?sort_by=listed_desc",
                    "https://streeteasy.com/for-rent/nyc/example?sort_by=listed_desc&page=2",
                    "https://streeteasy.com/for-rent/nyc/example?sort_by=listed_desc&page=3",
                ],
                [call.args[0] for call in fetch_html.call_args_list],
            )

    def test_source_fetches_only_first_page_for_newest_check(self) -> None:
        with TemporaryDirectory() as directory:
            source = StreetEasySource(
                StreetEasyConfig(
                    allow_live_requests=True,
                    permission_reference="ticket-123",
                    max_pages=3,
                    rate_limit_state_file=Path(directory) / "limit",
                )
            )
            search = SearchConfig(
                "test",
                "https://streeteasy.com/for-rent/nyc/example?sort_by=listed_desc",
            )
            with patch.object(
                StreetEasySource,
                "_fetch_html",
                return_value=page_html("newest", total_pages=3),
            ) as fetch_html:
                listings = source.fetch(search, full_scan=False)

            self.assertEqual(1, len(listings))
            self.assertEqual("newest", listings[0].url.rsplit("/", 1)[-1])
            fetch_html.assert_called_once_with(
                "https://streeteasy.com/for-rent/nyc/example?sort_by=listed_desc"
            )

    def test_source_stops_when_page_count_exceeds_safety_cap(self) -> None:
        with TemporaryDirectory() as directory:
            source = StreetEasySource(
                StreetEasyConfig(
                    allow_live_requests=True,
                    permission_reference="ticket-123",
                    max_pages=2,
                    rate_limit_state_file=Path(directory) / "limit",
                )
            )
            search = SearchConfig(
                "test", "https://streeteasy.com/for-rent/nyc/example"
            )
            with (
                patch.object(
                    StreetEasySource,
                    "_fetch_html",
                    return_value=page_html("one", total_pages=3),
                ),
                self.assertRaisesRegex(SourceError, "exceeds.*max_pages=2"),
            ):
                source.fetch(search)

    def test_403_errors_back_off_and_log_only_safe_diagnostics(self) -> None:
        with TemporaryDirectory() as directory:
            source = StreetEasySource(
                StreetEasyConfig(
                    allow_live_requests=True,
                    permission_reference="ticket-123",
                    rate_limit_state_file=Path(directory) / "limit",
                )
            )
            url = "https://streeteasy.com/for-rent/nyc/example?page=2"
            headers = Message()
            headers["Date"] = "Mon, 14 Sep 2026 20:00:00 GMT"
            headers["Retry-After"] = "180"
            headers["Server"] = "example-edge"
            headers["X-Request-ID"] = "request-123"
            headers["Set-Cookie"] = "secret-cookie=do-not-log"

            first_error = HTTPError(
                url, 403, "Forbidden", headers, BytesIO(b"forbidden")
            )
            second_headers = Message()
            second_headers["Server"] = "example-edge"
            second_error = HTTPError(
                url, 403, "Forbidden", second_headers, BytesIO(b"forbidden")
            )

            with (
                patch.object(_RequestRateLimiter, "wait"),
                patch(
                    "apartment_notifier.streeteasy.urlopen",
                    side_effect=[first_error, second_error],
                ),
            ):
                with self.assertRaises(SourceError) as first_context:
                    source._fetch_html(url)
                with self.assertRaises(SourceError) as second_context:
                    source._fetch_html(url)

            first = first_context.exception
            self.assertEqual(180, first.retry_after_seconds)
            self.assertIn("page 2", str(first))
            self.assertIn("consecutive 403s=1", str(first))
            self.assertIn("Date='Mon, 14 Sep 2026 20:00:00 GMT'", str(first))
            self.assertIn("Server='example-edge'", str(first))
            self.assertIn("X-Request-ID='request-123'", str(first))
            self.assertNotIn("Set-Cookie", str(first))
            self.assertNotIn("secret-cookie", str(first))

            second = second_context.exception
            self.assertEqual(240, second.retry_after_seconds)
            self.assertIn("consecutive 403s=2", str(second))


if __name__ == "__main__":
    unittest.main()
