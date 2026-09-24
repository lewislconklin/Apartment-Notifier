import unittest
from datetime import date

from apartment_notifier.config import FilterConfig, GeoPolygon
from apartment_notifier.matcher import matches
from apartment_notifier.models import Listing


class MatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.listing = Listing(
            listing_id="1",
            url="https://streeteasy.com/building/example/1",
            address="123 Example Street",
            price=4500,
            bedrooms=2,
            bathrooms=1.5,
            available_date=date(2026, 9, 20),
            neighborhood="Greenpoint",
            latitude=40.7301,
            longitude=-73.9542,
            description="Bright apartment with a private terrace",
            amenities=("In-unit laundry", "Dishwasher"),
        )

    def test_matches_all_constraints(self) -> None:
        filters = FilterConfig(
            neighborhoods=("greenpoint",),
            min_price=4000,
            max_price=5000,
            min_bedrooms=1,
            max_bedrooms=2,
            min_bathrooms=1,
            required_keywords=("terrace",),
            excluded_keywords=("short term",),
            required_amenities=("in unit laundry",),
        )
        self.assertTrue(matches(self.listing, filters))

    def test_rejects_missing_constrained_data(self) -> None:
        incomplete = Listing(
            listing_id="2",
            url="https://streeteasy.com/building/example/2",
            address="Unknown",
        )
        self.assertFalse(matches(incomplete, FilterConfig(max_price=5000)))

    def test_allows_listing_available_on_cutoff_date(self) -> None:
        filters = FilterConfig(available_on_or_before=date(2026, 9, 20))
        self.assertTrue(matches(self.listing, filters))

    def test_rejects_listing_available_after_cutoff_date(self) -> None:
        filters = FilterConfig(available_on_or_before=date(2026, 9, 19))
        self.assertFalse(matches(self.listing, filters))

    def test_allows_unknown_available_date(self) -> None:
        listing = Listing(
            listing_id="2",
            url="https://streeteasy.com/building/example/2",
            address="Unknown availability",
        )
        filters = FilterConfig(available_on_or_before=date(2026, 9, 20))
        self.assertTrue(matches(listing, filters))

    def test_rejects_excluded_keyword(self) -> None:
        self.assertFalse(
            matches(self.listing, FilterConfig(excluded_keywords=("terrace",)))
        )

    def test_rejects_listing_inside_excluded_area(self) -> None:
        area = GeoPolygon(
            name="test exclusion",
            points=(
                (40.72, -73.96),
                (40.74, -73.96),
                (40.74, -73.94),
                (40.72, -73.94),
            ),
        )
        self.assertFalse(matches(self.listing, FilterConfig(excluded_areas=(area,))))

    def test_allows_listing_outside_excluded_area(self) -> None:
        area = GeoPolygon(
            name="test exclusion",
            points=(
                (40.72, -74.00),
                (40.74, -74.00),
                (40.74, -73.99),
                (40.72, -73.99),
            ),
        )
        self.assertTrue(matches(self.listing, FilterConfig(excluded_areas=(area,))))

    def test_rejects_missing_coordinates_when_area_filter_is_configured(self) -> None:
        incomplete = Listing(
            listing_id="2",
            url="https://streeteasy.com/building/example/2",
            address="Unknown",
        )
        area = GeoPolygon(
            name="test exclusion",
            points=((40.72, -73.96), (40.74, -73.96), (40.73, -73.94)),
        )
        self.assertFalse(matches(incomplete, FilterConfig(excluded_areas=(area,))))


if __name__ == "__main__":
    unittest.main()
