from __future__ import annotations

from .config import FilterConfig
from .models import Listing


def matches(listing: Listing, filters: FilterConfig) -> bool:
    """Return True only when every configured constraint is satisfied.

    If a constrained numeric or neighborhood field is absent from the source,
    the listing does not match. This prevents an incomplete card from producing
    a false-positive notification.
    """

    if filters.min_price is not None and (
        listing.price is None or listing.price < filters.min_price
    ):
        return False
    if filters.max_price is not None and (
        listing.price is None or listing.price > filters.max_price
    ):
        return False
    if filters.min_bedrooms is not None and (
        listing.bedrooms is None or listing.bedrooms < filters.min_bedrooms
    ):
        return False
    if filters.max_bedrooms is not None and (
        listing.bedrooms is None or listing.bedrooms > filters.max_bedrooms
    ):
        return False
    if filters.min_bathrooms is not None and (
        listing.bathrooms is None or listing.bathrooms < filters.min_bathrooms
    ):
        return False
    if (
        filters.available_on_or_before is not None
        and listing.available_date is not None
        and listing.available_date > filters.available_on_or_before
    ):
        return False

    if filters.neighborhoods:
        if listing.neighborhood is None:
            return False
        allowed = {_normalize(value) for value in filters.neighborhoods}
        if _normalize(listing.neighborhood) not in allowed:
            return False

    if filters.excluded_areas:
        if listing.latitude is None or listing.longitude is None:
            return False
        if any(
            _point_in_polygon(listing.latitude, listing.longitude, area.points)
            for area in filters.excluded_areas
        ):
            return False

    searchable_text = " ".join(
        [
            listing.address,
            listing.neighborhood or "",
            listing.description,
            *listing.amenities,
        ]
    ).casefold()
    if any(keyword.casefold() not in searchable_text for keyword in filters.required_keywords):
        return False
    if any(keyword.casefold() in searchable_text for keyword in filters.excluded_keywords):
        return False

    listing_amenities = {_normalize(value) for value in listing.amenities}
    if any(_normalize(value) not in listing_amenities for value in filters.required_amenities):
        return False
    return True


def _normalize(value: str) -> str:
    return " ".join(value.casefold().replace("-", " ").split())


def _point_in_polygon(
    latitude: float,
    longitude: float,
    points: tuple[tuple[float, float], ...],
) -> bool:
    """Return True for points inside or on the boundary of a polygon."""
    inside = False
    previous_latitude, previous_longitude = points[-1]
    for current_latitude, current_longitude in points:
        if _point_on_segment(
            latitude,
            longitude,
            previous_latitude,
            previous_longitude,
            current_latitude,
            current_longitude,
        ):
            return True
        crosses_latitude = (current_latitude > latitude) != (
            previous_latitude > latitude
        )
        if crosses_latitude:
            crossing_longitude = current_longitude + (
                (latitude - current_latitude)
                * (previous_longitude - current_longitude)
                / (previous_latitude - current_latitude)
            )
            if longitude < crossing_longitude:
                inside = not inside
        previous_latitude, previous_longitude = (
            current_latitude,
            current_longitude,
        )
    return inside


def _point_on_segment(
    latitude: float,
    longitude: float,
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> bool:
    tolerance = 1e-10
    cross_product = (latitude - latitude_a) * (longitude_b - longitude_a) - (
        longitude - longitude_a
    ) * (latitude_b - latitude_a)
    if abs(cross_product) > tolerance:
        return False
    return (
        min(latitude_a, latitude_b) - tolerance
        <= latitude
        <= max(latitude_a, latitude_b) + tolerance
        and min(longitude_a, longitude_b) - tolerance
        <= longitude
        <= max(longitude_a, longitude_b) + tolerance
    )
