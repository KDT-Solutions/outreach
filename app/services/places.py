"""Google Places API (New): text search for businesses, details for website/phone.

Cost model (per SKU, free monthly quota first):
- Text Search Pro: id, displayName, formattedAddress, types  -> 5'000 free/month
- Place Details Enterprise: websiteUri, nationalPhoneNumber -> 1'000 free/month
We only call details for places not seen before, so the paid part scales with
new leads, not with searches.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

SEARCH_FIELDS = "places.id,places.displayName,places.formattedAddress,places.types,places.businessStatus,places.location,nextPageToken"
DETAILS_FIELDS = "websiteUri,nationalPhoneNumber"


@dataclass
class PlaceHit:
    place_id: str
    name: str
    address: str | None
    types: list[str]
    lat: float | None = None
    lng: float | None = None


class PlacesError(RuntimeError):
    pass


def _headers(field_mask: str) -> dict[str, str]:
    key = get_settings().google_places_api_key
    if not key:
        raise PlacesError("GOOGLE_PLACES_API_KEY ist nicht gesetzt")
    return {
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": field_mask,
        "Content-Type": "application/json",
    }


def search_text(
    query: str, max_results: int = 60, region: str = "CH", language: str = "de", bias: tuple[float, float, float] | None = None
) -> list[PlaceHit]:
    """Run a text search and page until max_results or no more pages (max 3 pages of 20).
    bias: (lat, lng, radius_m) prefers results around that point (Places allows max 50 km)."""
    hits: list[PlaceHit] = []
    page_token: str | None = None
    with httpx.Client(timeout=20) as client:
        while len(hits) < max_results:
            body: dict = {
                "textQuery": query,
                "languageCode": language,
                "regionCode": region,
                "pageSize": 20,
            }
            if page_token:
                body["pageToken"] = page_token
            if bias:
                lat, lng, radius = bias
                body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": min(radius, 50000.0)}}
            r = client.post(SEARCH_URL, headers=_headers(SEARCH_FIELDS), json=body)
            if r.status_code != 200:
                raise PlacesError(f"Places searchText {r.status_code}: {r.text[:300]}")
            data = r.json()
            for p in data.get("places", []):
                if p.get("businessStatus") not in (None, "OPERATIONAL"):
                    continue
                hits.append(
                    PlaceHit(
                        place_id=p["id"],
                        name=(p.get("displayName") or {}).get("text", ""),
                        address=p.get("formattedAddress"),
                        types=p.get("types", []),
                        lat=(p.get("location") or {}).get("latitude"),
                        lng=(p.get("location") or {}).get("longitude"),
                    )
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return hits[:max_results]


def details(place_id: str) -> tuple[str | None, str | None]:
    """Return (website, phone) for a place."""
    with httpx.Client(timeout=20) as client:
        r = client.get(DETAILS_URL.format(place_id=place_id), headers=_headers(DETAILS_FIELDS))
        if r.status_code != 200:
            raise PlacesError(f"Places details {r.status_code}: {r.text[:300]}")
        d = r.json()
        return d.get("websiteUri"), d.get("nationalPhoneNumber")


def locate(region: str, language: str = "de") -> tuple[float, float] | None:
    """Centre of a place name (town, canton, postcode) via the same text search."""
    hits = search_text(region, max_results=1, language=language)
    if hits and hits[0].lat is not None and hits[0].lng is not None:
        return hits[0].lat, hits[0].lng
    return None


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_POSTCODE_CITY = re.compile(r"\b(\d{4,5})\s+([^,\d][^,]*)")


def city_of(address: str | None) -> tuple[str | None, str | None]:
    """(postcode, town) from a formatted address like "Musterweg 1, 4000 Basel, Schweiz"."""
    if not address:
        return None, None
    m = _POSTCODE_CITY.search(address)
    if not m:
        return None, None
    return m.group(1), m.group(2).strip()
