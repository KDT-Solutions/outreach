"""Google Places API (New): text search for businesses, details for website/phone.

Cost model (per SKU, free monthly quota first):
- Text Search Pro: id, displayName, formattedAddress, types  -> 5'000 free/month
- Place Details Enterprise: websiteUri, nationalPhoneNumber -> 1'000 free/month
We only call details for places not seen before, so the paid part scales with
new leads, not with searches.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

SEARCH_FIELDS = "places.id,places.displayName,places.formattedAddress,places.types,places.businessStatus,nextPageToken"
DETAILS_FIELDS = "websiteUri,nationalPhoneNumber"


@dataclass
class PlaceHit:
    place_id: str
    name: str
    address: str | None
    types: list[str]


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


def search_text(query: str, max_results: int = 60, region: str = "CH", language: str = "de") -> list[PlaceHit]:
    """Run a text search and page until max_results or no more pages (max 3 pages of 20)."""
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
