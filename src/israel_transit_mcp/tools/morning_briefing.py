"""MCP tool: `morning_briefing` — one-call commute readout.

Composes the whole pipeline for a saved route:

1. Load the saved route from the local RAG.
2. Fan out in parallel — plan driving with traffic for the chosen time,
   gather current disruption events.
3. Localize disruptions to the route's legs.
4. Record today's ETA observation into `eta_observations` so the
   personal baseline learns over time.
5. Compute the anomaly verdict for the (route, weekday, hour) bucket.
6. Compose severity + a suggested_action sentence Claude can read out.

Severity ladder:
  HIGH = anomaly AND a matched disruption explains it
  MED  = anomaly with no disruption match  OR  disruption with no
         anomaly yet (baseline too small / not exceeded)
  LOW  = neither

Suggested action follows from severity + the kind of disruption matched.
Always honest about uncertainty: if the baseline is below
`baseline_min_samples`, the briefing says so instead of inventing
confidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import Field

from ..aggregator import Aggregator
from ..app import get_config, get_store, mcp
from ..models import CommuteBriefing, ETAObservation, TransportMode
from ..store.baselines import compute_anomaly


@mcp.tool()
async def morning_briefing(
    name: Annotated[str, Field(description="Name of the saved route to check. Use `list_routes` if you forget which ones exist.")],
    at_iso: Annotated[str | None, Field(description="Optional ISO-8601 time to check for (default: now). Use this to ask 'what's my commute looking like at 17:30 today?'.")] = None,
    window_hours: Annotated[int, Field(ge=1, le=24, description="Disruption lookback window in hours.")] = 4,
    record_observation: Annotated[bool, Field(description="Whether to record today's ETA into eta_observations. Default true — this is how the baseline learns.")] = True,
) -> dict:
    """Return a composed commute briefing: ETA + anomaly + disruptions + action.

    This is the tool to call when the user asks "should I leave for
    work now?" or "what's the situation on my way home today?". It
    answers in one round-trip — Claude does not need to chain
    plan_route + check_disruptions itself.
    """
    cfg = get_config()
    store = get_store()
    saved = store.get_route(name)
    if saved is None:
        return {
            "ok": False,
            "error": f"no saved route named {name!r}",
            "remedy": "Save it first with save_route(name=..., origin=..., destination=...).",
        }
    when: datetime
    if at_iso:
        try:
            when = datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
        except ValueError:
            return {"ok": False, "error": f"at_iso {at_iso!r} is not valid ISO-8601."}
    else:
        when = datetime.now(timezone.utc)

    agg = Aggregator(cfg)
    plan = await agg.plan_in_mode(
        saved.origin, saved.destination, saved.mode, departure_time=when
    )
    snap = await agg.gather_disruptions(window_hours=window_hours)

    if not plan.routes:
        return {
            "ok": False,
            "error": "could not produce a route plan",
            "trace": {
                "plan": _trace_to_json(plan.trace),
                "disruptions": _trace_to_json(snap.trace),
            },
        }
    best = plan.routes[0]
    matching = agg.disruptions_for_route(snap, best)

    if record_observation and saved.id is not None:
        store.record_eta(
            ETAObservation(
                saved_route_id=saved.id,
                observed_at=when,
                eta_s=best.total_duration_s,
                weekday=when.weekday(),
                hour=when.hour,
            ),
            mode=saved.mode.value,
        )

    verdict = compute_anomaly(
        store,
        saved_route_id=saved.id or 0,
        when=when,
        today_eta_s=best.total_duration_s,
        threshold_minutes=cfg.anomaly_threshold_minutes,
        min_samples=cfg.baseline_min_samples,
        mode=saved.mode.value,
    )

    severity = _severity(verdict.is_anomalous, matching)
    suggested = _suggested_action(severity, verdict, matching, best.total_duration_s)

    briefing = CommuteBriefing(
        saved_route_name=saved.name,
        route=best,
        anomaly=verdict,
        disruptions=matching,
        suggested_action=suggested,
        severity=severity,
    )
    return {
        "ok": True,
        "briefing": _briefing_to_json(briefing),
        "alternatives": [_route_to_json(r) for r in plan.routes[1:3]],
        "trace": {
            "plan": _trace_to_json(plan.trace),
            "disruptions": _trace_to_json(snap.trace),
            "disruption_events_total": len(snap.events),
            "disruption_events_matching_route": len(matching),
        },
    }


def _severity(is_anomalous: bool, matching: list) -> str:
    if is_anomalous and matching:
        return "high"
    if is_anomalous or matching:
        return "med"
    return "low"


def _suggested_action(
    severity: str,
    verdict,
    matching: list,
    today_eta_s: int,
) -> str:
    if severity == "high":
        delta_min = max(5, verdict.delta_s // 60)
        kind = matching[0].kind.value if matching else "disruption"
        loc = matching[0].location_hint or "the route"
        return (
            f"חריג ({delta_min} דק׳ מעל הבייסליין) + {kind} מדווח ב-{loc}. "
            f"מומלץ לצאת {delta_min}–{delta_min + 10} דקות מוקדם יותר, או לשקול חלופה."
        )
    if severity == "med" and verdict.is_anomalous:
        delta_min = max(3, verdict.delta_s // 60)
        return (
            f"איטי מהרגיל ({delta_min} דק׳ מעל הבייסליין) אבל ללא דיווח חדשות תואם. "
            f"שקול לצאת ~{delta_min} דקות מוקדם יותר."
        )
    if severity == "med" and matching:
        kind = matching[0].kind.value
        loc = matching[0].location_hint or "אזור המסלול"
        return (
            f"{kind} מדווח ב-{loc} אבל ה-ETA עדיין בטווח הרגיל — שים לב, "
            f"ייתכן שטרם השפיע על הזמן."
        )
    if verdict.sample_size < 5:
        return (
            f"המסלול נראה רגיל ({today_eta_s // 60} דק׳), אבל יש רק "
            f"{verdict.sample_size} תצפיות בבייסליין הזה — תן עוד כמה ימים "
            f"כדי שהאנומליות יהיו אמינות."
        )
    return f"הדרך נראית רגילה — {today_eta_s // 60} דק׳, בלי דיווחים חריגים."


def _briefing_to_json(b: CommuteBriefing) -> dict:
    return {
        "saved_route_name": b.saved_route_name,
        "severity": b.severity,
        "suggested_action": b.suggested_action,
        "route": _route_to_json(b.route),
        "anomaly": {
            "is_anomalous": b.anomaly.is_anomalous,
            "today_eta_min": b.anomaly.today_eta_s // 60,
            "baseline_p50_min": b.anomaly.baseline_p50_s // 60,
            "baseline_p75_min": b.anomaly.baseline_p75_s // 60,
            "delta_min": b.anomaly.delta_s // 60,
            "sample_size": b.anomaly.sample_size,
            "explanation": b.anomaly.explanation,
        },
        "disruptions": [_event_to_json(e) for e in b.disruptions],
    }


def _route_to_json(r) -> dict:
    return {
        "summary": r.summary,
        "total_duration_min": round(r.total_duration_s / 60),
        "total_distance_km": round(r.total_distance_m / 1000, 1),
        "source": r.source,
        "warnings": r.warnings,
        "legs": [
            {"summary": l.summary, "distance_m": l.distance_m, "duration_s": l.duration_s}
            for l in r.legs
        ],
    }


def _event_to_json(e) -> dict:
    return {
        "kind": e.kind.value,
        "title": e.title,
        "source": e.source,
        "source_url": e.source_url,
        "published_at": e.published_at.isoformat() if e.published_at else None,
        "location_hint": e.location_hint,
    }


def _trace_to_json(t) -> dict:
    return {"successes": t.successes, "failures": t.failures}
