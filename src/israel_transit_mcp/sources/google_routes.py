"""Google Maps Routes API v1 — driving, transit, and walking.

Single source for all routing because Google ingests both live traffic
(via Waze) and Israeli GTFS-RT (via the MoT feed) — building a separate
transit router would mean reinventing what Google already does well.
For Israel specifically, MoT-published GTFS-RT means transit ETAs from
Google reflect real-time bus and rail delays.

Per-mode adjustments are kept tight:
- DRIVING uses TRAFFIC_AWARE_OPTIMAL + alternatives; legs are one-per-route
  with a Hebrew summary from the description field.
- TRANSIT expands Google's `steps[]` into one RouteLeg per sub-step
  (walk to stop, ride bus, walk to train, ride train, walk to door).
- WALKING is single-mode like driving but with a walk summary.

Knows nothing about MCP, store, or other sources.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import Place, Route, RouteLeg, TransportMode


_ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"

# Per-mode field masks: transit needs the transitDetails subtree that
# driving doesn't (saves cost on the common driving query).
_DRIVING_FIELD_MASK = ",".join([
    "routes.duration",
    "routes.staticDuration",
    "routes.distanceMeters",
    "routes.polyline.encodedPolyline",
    "routes.description",
    "routes.warnings",
    "routes.legs.duration",
    "routes.legs.staticDuration",
    "routes.legs.distanceMeters",
    "routes.legs.steps.navigationInstruction.instructions",
    "routes.legs.steps.distanceMeters",
])

_TRANSIT_FIELD_MASK = ",".join([
    "routes.duration",
    "routes.distanceMeters",
    "routes.description",
    "routes.warnings",
    "routes.legs.steps.travelMode",
    "routes.legs.steps.distanceMeters",
    "routes.legs.steps.staticDuration",
    "routes.legs.steps.navigationInstruction.instructions",
    "routes.legs.steps.transitDetails.stopDetails.arrivalStop.name",
    "routes.legs.steps.transitDetails.stopDetails.departureStop.name",
    "routes.legs.steps.transitDetails.stopDetails.arrivalTime",
    "routes.legs.steps.transitDetails.stopDetails.departureTime",
    "routes.legs.steps.transitDetails.headway",
    "routes.legs.steps.transitDetails.headsign",
    "routes.legs.steps.transitDetails.stopCount",
    "routes.legs.steps.transitDetails.tripShortText",
    "routes.legs.steps.transitDetails.transitLine.name",
    "routes.legs.steps.transitDetails.transitLine.nameShort",
    "routes.legs.steps.transitDetails.transitLine.vehicle.type",
    "routes.legs.steps.transitDetails.transitLine.vehicle.name.text",
    "routes.legs.steps.transitDetails.transitLine.agencies.name",
])


class GoogleRoutesSource:
    name = "google_routes"
    supports_modes = (TransportMode.DRIVING, TransportMode.TRANSIT, TransportMode.WALKING)

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("GOOGLE_MAPS_API_KEY is required for GoogleRoutesSource")
        self._api_key = api_key
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "GoogleRoutesSource":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def plan(
        self,
        origin: Place,
        destination: Place,
        mode: TransportMode = TransportMode.DRIVING,
        departure_time: datetime | None = None,
    ) -> list[Route]:
        if mode not in self.supports_modes:
            return []
        body, field_mask = self._build_body(origin, destination, mode, departure_time)
        client = await self._ensure_client()
        resp = await client.post(
            _ENDPOINT,
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self._api_key,
                "X-Goog-FieldMask": field_mask,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            _parse_route(r, origin, destination, mode, departure_time)
            for r in data.get("routes", [])
        ]

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    def _build_body(
        self,
        origin: Place,
        destination: Place,
        mode: TransportMode,
        departure_time: datetime | None,
    ) -> tuple[dict[str, Any], str]:
        body: dict[str, Any] = {
            "origin": _waypoint(origin),
            "destination": _waypoint(destination),
            "languageCode": "he",
            "regionCode": "IL",
            "units": "METRIC",
        }
        if departure_time is not None:
            if departure_time.tzinfo is None:
                departure_time = departure_time.replace(tzinfo=timezone.utc)
            body["departureTime"] = departure_time.isoformat().replace("+00:00", "Z")

        if mode is TransportMode.DRIVING:
            body["travelMode"] = "DRIVE"
            body["routingPreference"] = "TRAFFIC_AWARE_OPTIMAL"
            body["computeAlternativeRoutes"] = True
            return body, _DRIVING_FIELD_MASK

        if mode is TransportMode.TRANSIT:
            body["travelMode"] = "TRANSIT"
            body["computeAlternativeRoutes"] = True
            # Default mix prefers rail when available — Israelis with a
            # choice usually take the train over a bus.
            body["transitPreferences"] = {
                "allowedTravelModes": ["TRAIN", "SUBWAY", "LIGHT_RAIL", "BUS"],
                "routingPreference": "LESS_WALKING",
            }
            return body, _TRANSIT_FIELD_MASK

        # WALKING
        body["travelMode"] = "WALK"
        return body, _DRIVING_FIELD_MASK


def _waypoint(p: Place) -> dict[str, Any]:
    if p.coords is not None:
        return {
            "location": {
                "latLng": {"latitude": p.coords.lat, "longitude": p.coords.lng}
            }
        }
    return {"address": p.display_name}


def _seconds(raw: str | None) -> int:
    """Google's `duration` fields look like `"1234s"`."""
    if not raw:
        return 0
    if raw.endswith("s"):
        try:
            return int(float(raw[:-1]))
        except ValueError:
            return 0
    return 0


def _parse_iso(raw: str | dict | None) -> datetime | None:
    """Transit stop times come either as ISO strings or {time:..., timeZone:...}."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        s = raw.get("time")
    else:
        s = raw
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_route(
    raw: dict[str, Any],
    origin: Place,
    destination: Place,
    mode: TransportMode,
    departure_time: datetime | None,
) -> Route:
    if mode is TransportMode.TRANSIT:
        return _parse_transit_route(raw, origin, destination, departure_time)
    return _parse_driving_route(raw, origin, destination, mode, departure_time)


def _parse_driving_route(
    raw: dict[str, Any],
    origin: Place,
    destination: Place,
    mode: TransportMode,
    departure_time: datetime | None,
) -> Route:
    duration_traffic = _seconds(raw.get("duration"))
    duration_static = _seconds(raw.get("staticDuration"))
    distance_m = int(raw.get("distanceMeters") or 0)
    legs_in = raw.get("legs") or []
    legs_out: list[RouteLeg] = []
    for leg in legs_in:
        leg_traffic = _seconds(leg.get("duration"))
        leg_static = _seconds(leg.get("staticDuration"))
        leg_distance = int(leg.get("distanceMeters") or 0)
        summary = "Drive" if mode is TransportMode.DRIVING else "Walk"
        for step in leg.get("steps") or []:
            ni = (step.get("navigationInstruction") or {}).get("instructions")
            if ni:
                summary = str(ni)
                break
        legs_out.append(
            RouteLeg(
                mode=mode,
                summary=summary,
                distance_m=leg_distance,
                duration_s=leg_static or leg_traffic,
                duration_in_traffic_s=leg_traffic if leg_static else None,
                departure_time=departure_time,
                arrival_time=None,
            )
        )
    description = raw.get("description") or ""
    minutes = max(1, duration_traffic // 60)
    label = "דרך" if mode is TransportMode.DRIVING else "ברגל"
    suffix = " בתנועה" if mode is TransportMode.DRIVING else ""
    summary = description or f"{label} — {minutes} דק׳{suffix}"
    return Route(
        mode=mode,
        origin=origin,
        destination=destination,
        legs=legs_out,
        total_duration_s=duration_traffic or duration_static,
        total_distance_m=distance_m,
        departure_time=departure_time,
        arrival_time=None,
        summary=summary,
        warnings=[str(w) for w in raw.get("warnings") or []],
        source="google_routes",
    )


def _parse_transit_route(
    raw: dict[str, Any],
    origin: Place,
    destination: Place,
    departure_time: datetime | None,
) -> Route:
    duration = _seconds(raw.get("duration"))
    distance_m = int(raw.get("distanceMeters") or 0)
    legs_out: list[RouteLeg] = []
    transit_summary_parts: list[str] = []
    for google_leg in raw.get("legs") or []:
        for step in google_leg.get("steps") or []:
            leg, label = _parse_transit_step(step, departure_time)
            if leg is None:
                continue
            legs_out.append(leg)
            if label:
                transit_summary_parts.append(label)
    description = raw.get("description") or ""
    minutes = max(1, duration // 60)
    if description:
        summary = description
    elif transit_summary_parts:
        summary = " + ".join(transit_summary_parts) + f" — {minutes} דק׳"
    else:
        summary = f"תח״צ — {minutes} דק׳"
    return Route(
        mode=TransportMode.TRANSIT,
        origin=origin,
        destination=destination,
        legs=legs_out,
        total_duration_s=duration,
        total_distance_m=distance_m,
        departure_time=departure_time,
        arrival_time=None,
        summary=summary,
        warnings=[str(w) for w in raw.get("warnings") or []],
        source="google_routes",
    )


def _parse_transit_step(
    step: dict[str, Any],
    departure_time: datetime | None,
) -> tuple[RouteLeg | None, str]:
    """Turn a Google step into one RouteLeg + a short Hebrew label for
    the route summary line."""
    travel_mode = step.get("travelMode", "WALK").upper()
    distance_m = int(step.get("distanceMeters") or 0)
    duration_s = _seconds(step.get("staticDuration"))
    instructions = (step.get("navigationInstruction") or {}).get("instructions") or ""
    transit = step.get("transitDetails") or {}
    if travel_mode == "TRANSIT" and transit:
        line = transit.get("transitLine") or {}
        line_short = line.get("nameShort") or ""
        line_name = line.get("name") or ""
        vehicle = ((line.get("vehicle") or {}).get("name") or {}).get("text", "")
        vtype = ((line.get("vehicle") or {}).get("type") or "").upper()
        agencies = line.get("agencies") or []
        agency_name = agencies[0]["name"] if agencies and isinstance(agencies[0], dict) else ""
        stops = transit.get("stopDetails") or {}
        dep_stop = (stops.get("departureStop") or {}).get("name") or ""
        arr_stop = (stops.get("arrivalStop") or {}).get("name") or ""
        dep_time = _parse_iso(stops.get("departureTime"))
        arr_time = _parse_iso(stops.get("arrivalTime"))
        stop_count = transit.get("stopCount")
        # Hebrew label like "אוטובוס 480 (דן)" or "רכבת תל אביב→הרצליה"
        vehicle_he = {
            "BUS": "אוטובוס",
            "RAIL": "רכבת",
            "HEAVY_RAIL": "רכבת",
            "COMMUTER_TRAIN": "רכבת",
            "LIGHT_RAIL": "רכבת קלה",
            "SUBWAY": "רכבת תחתית",
            "METRO_RAIL": "רכבת קלה",
            "TRAM": "רכבת קלה",
            "SHARE_TAXI": "שירות",
        }.get(vtype, vehicle or "תח״צ")
        label_bits = [vehicle_he]
        if line_short:
            label_bits.append(line_short)
        elif line_name:
            label_bits.append(line_name)
        if agency_name:
            label_bits.append(f"({agency_name})")
        short_label = " ".join(label_bits)
        summary_parts = [short_label]
        if dep_stop and arr_stop:
            summary_parts.append(f"{dep_stop} → {arr_stop}")
        if stop_count:
            summary_parts.append(f"({stop_count} תחנות)")
        summary = " · ".join(summary_parts)
        return (
            RouteLeg(
                mode=TransportMode.TRANSIT,
                summary=summary,
                distance_m=distance_m,
                duration_s=duration_s,
                departure_time=dep_time or departure_time,
                arrival_time=arr_time,
            ),
            short_label,
        )
    # Walk step
    minutes = max(1, duration_s // 60)
    summary = instructions or f"הליכה {minutes} דק׳"
    return (
        RouteLeg(
            mode=TransportMode.WALKING,
            summary=summary,
            distance_m=distance_m,
            duration_s=duration_s,
        ),
        f"הליכה {minutes}״",
    )
