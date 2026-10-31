"""Company locations, verified with Geoapify. The map dialog only sends back a Geoapify place id (from its address search, or from
clicking the map); the server then asks Geoapify's Place Details API for that place itself and stores Geoapify's own address,
coordinates and country. So a location can't be typed in, made up, or edited in the page: only a real place Geoapify knows gets saved."""
import asyncio, json, re, urllib.error, urllib.parse, urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from system import GEOAPIFY_DETAILS_URL, GEOAPIFY_SERVER_KEY
PLACE_ID = re.compile(r"[A-Za-z0-9]{10,400}")  # Geoapify place ids are hex strings
class PlaceError(Exception): pass
def _lookup(place_id: str) -> dict:
    if not GEOAPIFY_SERVER_KEY: raise PlaceError("Company locations are verified with Geoapify, which isn't set up yet: add GEOAPIFY_API_KEY to .env.")
    if not PLACE_ID.fullmatch(place_id or ""): raise PlaceError("Pick the company's location on the map.")
    query = urllib.parse.urlencode({"id": place_id, "features": "details", "lang": "en", "apiKey": GEOAPIFY_SERVER_KEY})
    try:
        with urllib.request.urlopen(f"{GEOAPIFY_DETAILS_URL}?{query}", timeout=10) as r: data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (400, 404): raise PlaceError("Geoapify doesn't know that place: pick the location on the map again.") from None
        if e.code == 429: raise PlaceError("Geoapify's daily limit for this key is used up: try again later.") from None
        raise PlaceError("Geoapify refused the check: make sure the API key is valid (if you restricted it, the server needs GEOAPIFY_SERVER_KEY).") from None
    except (OSError, ValueError): raise PlaceError("Couldn't reach Geoapify to verify the location. Try again.") from None
    features = data.get("features") or []
    feature = next((f for f in features if (f.get("properties") or {}).get("feature_type") == "details"), features[0] if features else {})
    props, geometry = feature.get("properties") or {}, feature.get("geometry") or {}
    point = geometry.get("coordinates") if geometry.get("type") == "Point" else [None, None]
    lat, lng = props.get("lat", point[1]), props.get("lon", point[0])
    if not props.get("formatted") or lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise PlaceError("Geoapify returned no address for that place: pick another one.")
    address, name = props["formatted"], props.get("name") or ""  # a named place (a company, a building): its address leaves the name out
    if name and name.lower() not in address.lower(): address = f"{name}, {address}"
    tz = str((props.get("timezone") or {}).get("name") or "")[:64]
    try: ZoneInfo(tz)
    except (ValueError, ZoneInfoNotFoundError): tz = None  # unknown / missing: payroll.company_tz falls back by country
    return {"place_id": str(props.get("place_id") or place_id)[:300], "address": address[:300], "lat": float(lat), "lng": float(lng),
            "country": (props.get("country_code") or "").upper()[:2] or None, "timezone": tz}
async def verify_place(place_id: str) -> dict:
    """{place_id, address, lat, lng, country, timezone} straight from Geoapify, or PlaceError with a message for the form."""
    return await asyncio.to_thread(_lookup, place_id)
