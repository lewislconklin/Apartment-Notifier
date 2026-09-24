from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
import json


@dataclass(frozen=True, slots=True)
class Listing:
    listing_id: str
    url: str
    address: str
    price: int | None = None
    bedrooms: float | None = None
    bathrooms: float | None = None
    available_date: date | None = None
    neighborhood: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    description: str = ""
    amenities: tuple[str, ...] = field(default_factory=tuple)
    source: str = "streeteasy"

    @property
    def fingerprint(self) -> str:
        payload = {
            "address": self.address,
            "amenities": sorted(self.amenities),
            "bathrooms": self.bathrooms,
            "bedrooms": self.bedrooms,
            "available_date": (
                self.available_date.isoformat() if self.available_date else None
            ),
            "description": self.description,
            "neighborhood": self.neighborhood,
            "price": self.price,
            "url": self.url,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ListingEvent:
    search_name: str
    event_type: str
    listing: Listing

    @property
    def event_key(self) -> str:
        return f"{self.event_type}:{self.listing.fingerprint}"
