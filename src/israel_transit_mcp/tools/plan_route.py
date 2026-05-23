"""MCP tool: `plan_route` — driving-mode routing with live traffic.

Returns 1–3 alternative routes via Google Routes API v1. Each route
carries total ETA in seconds, distance in meters, a one-line Hebrew
summary, and the underlying leg-by-leg breakdown. Trace metadata tells
Claude exactly which source produced each number and how long the call
took, so failures are diagnosable from the conversation.

Transit-mode routing lands in a follow-up commit when the GTFS bundle
+ Stride sources are wired.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field

from ..aggregator import Aggregator
from ..app import get_config, mcp
from ..models import LatLng, Place, TransportMode


@mcp.tool()
async def plan_route(
    origin: Annotated[str, Field(description="Free-text origin — Hebrew or English address. Example: 'נחלת בנימין 30, תל אביב'.")],
    destination: Annotated[str, Field(description="Free-text destination address.")],
    mode: Annotated[str, Field(description="Travel mode. Currently only 'driving' is wired; 'transit' lands in a follow-up commit.")] = "driving",
    departure_iso: Annotated[str | None, Field(description="Optional ISO-8601 future departure time for traffic prediction. Omit for 'leave now'.")] = None,
    origin_lat: Annotated[float | None, Field(description="Optional origin latitude when you already have coordinates (skip geocoding).")] = None,
    origin_lng: Annotated[float | None, Field(description="Optional origin longitude.")] = None,
    destination_lat: Annotated[float | None, Field(description="Optional destination latitude.")] = None,
    destination_lng: Annotated[float | None, Field(description="Optional destination longitude.")] = None,
    avoid_tolls: Annotated[bool, Field(description="When true, exclude toll roads (כביש 6 etc.). Driving only.")] = False,
    avoid_highways: Annotated[bool, Field(description="When true, prefer surface streets over highways. Driving only.")] = False,
) -> dict:
    """Plan a route between two places using live Israeli traffic data.

    Currently supports driving via Google Routes API v1 (requires
    GOOGLE_MAPS_API_KEY). Returns up to 3 alternatives sorted by ETA
    with traffic factored in. Use departure_iso to predict the ETA at a
    future time (e.g. tomorrow 08:00) — the response then represents the
    traffic-aware ETA for that departure.
    """
    cfg = get_config()
    if not cfg.driving_available:
        return {
            "ok": False,
            "error": "GOOGLE_MAPS_API_KEY is not set in the MCP server environment.",
            "remedy": "Set GOOGLE_MAPS_API_KEY in your .env and restart the MCP server.",
        }
    try:
        mode_enum = TransportMode(mode.lower())
    except ValueError:
        return {"ok": False, "error": f"unknown mode '{mode}', try 'driving', 'transit', or 'walking'."}
    if mode_enum not in {TransportMode.DRIVING, TransportMode.TRANSIT, TransportMode.WALKING}:
        return {"ok": False, "error": f"mode '{mode_enum.value}' not supported."}
    departure: datetime | None = None
    if departure_iso:
        try:
            departure = datetime.fromisoformat(departure_iso.replace("Z", "+00:00"))
        except ValueError:
            return {"ok": False, "error": f"departure_iso '{departure_iso}' is not valid ISO-8601."}

    origin_place = Place(
        display_name=origin,
        coords=LatLng(lat=origin_lat, lng=origin_lng) if origin_lat is not None and origin_lng is not None else None,
    )
    dest_place = Place(
        display_name=destination,
        coords=LatLng(lat=destination_lat, lng=destination_lng)
        if destination_lat is not None and destination_lng is not None
        else None,
    )
    agg = Aggregator(cfg)
    plan = await agg.plan_in_mode(
        origin_place, dest_place, mode_enum,
        departure_time=departure,
        avoid_tolls=avoid_tolls,
        avoid_highways=avoid_highways,
    )
    return {
        "ok": True,
        "mode": mode_enum.value,
        "alternatives": [_route_to_json(r) for r in plan.routes[:3]],
        "trace": _trace_to_json(plan.trace),
    }


def _route_to_json(r) -> dict:
    return {
        "summary": r.summary,
        "total_duration_s": r.total_duration_s,
        "total_duration_min": round(r.total_duration_s / 60),
        "total_distance_m": r.total_distance_m,
        "total_distance_km": round(r.total_distance_m / 1000, 1),
        "warnings": r.warnings,
        "source": r.source,
        "legs": [
            {
                "mode": leg.mode.value,
                "summary": leg.summary,
                "distance_m": leg.distance_m,
                "duration_s": leg.duration_s,
                "duration_in_traffic_s": leg.duration_in_traffic_s,
                "departure_time": leg.departure_time.isoformat() if leg.departure_time else None,
                "arrival_time": leg.arrival_time.isoformat() if leg.arrival_time else None,
            }
            for leg in r.legs
        ],
    }


def _trace_to_json(t) -> dict:
    return {"successes": t.successes, "failures": t.failures}
