"""MCP tool: `best_way` — multi-modal "should I drive or take the bus?".

The headline tool. Takes a saved route, runs driving + transit + the
disruption fan-out — all in parallel via TaskRunner — and returns ONE
ranked recommendation that names the winner, the gap, and the reason.

This is what replaces "open Waze, then open Moovit, then compare in
your head" with a single Claude question.

Ranking algorithm (intentionally simple, written so a human can audit
it from the JSON):

  1. score = total_duration_s
  2. + penalty if matching disruptions on this mode's path  (driving
     gets road-event penalties; transit gets service-alert penalties)
  3. + small comfort tax for transit transfers (each transfer = +3 min)
  4. - comfort credit if the mode beats the user's personal baseline
     for THIS commute by > 5 min

The lower the score the better. Both candidates are returned with their
scores so Claude can read out the comparison verbatim.

Baseline learning is mode-aware: ETAs are recorded per (saved_route,
weekday, hour) bucket, so over time the MCP knows your usual driving
22 min Wednesday 17:30 separately from your usual bus 38 min same
slot — and can answer "today is unusually slow for either" honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from pydantic import Field

from ..aggregator import Aggregator
from ..app import get_config, get_store, mcp
from ..models import DisruptionEvent, ETAObservation, Route, TransportMode
from ..store.baselines import compute_anomaly


_TRANSFER_PENALTY_S = 180


@dataclass
class ModeCandidate:
    mode: TransportMode
    route: Route
    matched_disruptions: list[DisruptionEvent]
    anomaly_delta_s: int
    """today - p50; positive means slower than usual."""
    sample_size: int
    score_s: int
    """Lower is better. Computed by `_score`."""

    @property
    def transfer_count(self) -> int:
        if self.mode is not TransportMode.TRANSIT:
            return 0
        return max(0, sum(1 for l in self.route.legs if l.mode is TransportMode.TRANSIT) - 1)


@mcp.tool()
async def best_way(
    name: Annotated[str, Field(description="Name of the saved route. List with `list_routes`.")],
    at_iso: Annotated[str | None, Field(description="Optional ISO-8601 departure time. Default: now.")] = None,
    window_hours: Annotated[int, Field(ge=1, le=24, description="Disruption lookback window.")] = 4,
    modes: Annotated[list[str] | None, Field(description="Modes to compare. Default: ['driving', 'transit'].")] = None,
    record_observation: Annotated[bool, Field(description="Record today's ETA into the baseline (per mode).")] = True,
    avoid_tolls: Annotated[bool, Field(description="When true, exclude toll roads from driving mode (כביש 6 etc.).")] = False,
    avoid_highways: Annotated[bool, Field(description="When true, prefer surface streets over highways.")] = False,
) -> dict:
    """Compare driving and transit for a saved commute and return one
    ranked recommendation.

    Runs Google Routes in driving mode AND transit mode AND the
    disruption fan-out — all in parallel. Joins the result with your
    personal per-mode baseline (so the verdict knows your usual Wed
    17:30 bus is 38 min, not the same as your usual Wed 17:30 drive).
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
    if at_iso:
        try:
            when = datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
        except ValueError:
            return {"ok": False, "error": f"at_iso {at_iso!r} is not valid ISO-8601."}
    else:
        when = datetime.now(timezone.utc)

    mode_enums: list[TransportMode] = []
    for raw in modes or ["driving", "transit"]:
        try:
            mode_enums.append(TransportMode(raw.lower()))
        except ValueError:
            return {"ok": False, "error": f"unknown mode {raw!r}"}

    agg = Aggregator(cfg)
    multi = await agg.compare_modes(
        saved.origin, saved.destination, tuple(mode_enums),
        departure_time=when,
        avoid_tolls=avoid_tolls,
        avoid_highways=avoid_highways,
    )
    snap = await agg.gather_disruptions(window_hours=window_hours)

    candidates: list[ModeCandidate] = []
    per_mode_baselines: dict[str, dict] = {}
    for mode in mode_enums:
        routes = multi.plans.get(mode, [])
        if not routes:
            continue
        best_route = routes[0]
        matched = agg.disruptions_for_route(snap, best_route)
        anomaly = compute_anomaly(
            store,
            saved_route_id=saved.id or 0,
            when=when,
            today_eta_s=best_route.total_duration_s,
            threshold_minutes=cfg.anomaly_threshold_minutes,
            min_samples=cfg.baseline_min_samples,
            mode=mode.value,
        )
        per_mode_baselines[mode.value] = {
            "today_min": best_route.total_duration_s // 60,
            "p50_min": anomaly.baseline_p50_s // 60,
            "p75_min": anomaly.baseline_p75_s // 60,
            "delta_min": anomaly.delta_s // 60,
            "sample_size": anomaly.sample_size,
            "is_anomalous": anomaly.is_anomalous,
        }
        if record_observation and saved.id is not None:
            store.record_eta(
                ETAObservation(
                    saved_route_id=saved.id,
                    observed_at=when,
                    eta_s=best_route.total_duration_s,
                    weekday=when.weekday(),
                    hour=when.hour,
                ),
                mode=mode.value,
            )
        score = _score(
            best_route,
            matched,
            anomaly.delta_s,
            mode,
        )
        candidates.append(
            ModeCandidate(
                mode=mode,
                route=best_route,
                matched_disruptions=matched,
                anomaly_delta_s=anomaly.delta_s,
                sample_size=anomaly.sample_size,
                score_s=score,
            )
        )

    if not candidates:
        return {
            "ok": False,
            "error": "no mode produced a usable route",
            "trace": {
                "modes": _trace_to_json(multi.trace),
                "disruptions": _trace_to_json(snap.trace),
            },
        }

    candidates.sort(key=lambda c: c.score_s)
    winner = candidates[0]
    rest = candidates[1:]
    return {
        "ok": True,
        "winner": _candidate_to_json(winner),
        "alternatives": [_candidate_to_json(c) for c in rest],
        "recommendation": _recommendation(winner, rest),
        "baselines": per_mode_baselines,
        "trace": {
            "modes": _trace_to_json(multi.trace),
            "disruptions": _trace_to_json(snap.trace),
            "disruption_events_total": len(snap.events),
        },
    }


def _score(
    route: Route,
    matched: list[DisruptionEvent],
    anomaly_delta_s: int,
    mode: TransportMode,
) -> int:
    """Compose the ranking score. Lower is better."""
    score = route.total_duration_s
    # Per-disruption penalty: each matched event implies slowdown beyond
    # what Google's ETA already reflects.
    score += 120 * len(matched)
    # Transfer comfort tax.
    if mode is TransportMode.TRANSIT:
        transfers = max(0, sum(1 for l in route.legs if l.mode is TransportMode.TRANSIT) - 1)
        score += _TRANSFER_PENALTY_S * transfers
    # Personal-baseline anomaly already lives inside route.total_duration_s
    # (today's ETA from Google), so we don't double-count it. We only use
    # it to inform Claude's reasoning, not the score.
    return score




def _recommendation(winner: ModeCandidate, rest: list[ModeCandidate]) -> str:
    """One Hebrew sentence summarising the verdict, named modes,
    delta, and the dominant reason."""
    win_min = winner.route.total_duration_s // 60
    win_mode = _mode_he(winner.mode)
    if not rest:
        return f"{win_mode} — {win_min} דק׳. אין מצב חלופי בנתונים שאספתי."
    runner_up = rest[0]
    other_min = runner_up.route.total_duration_s // 60
    other_mode = _mode_he(runner_up.mode)
    delta = other_min - win_min
    reason_bits: list[str] = []
    if winner.matched_disruptions:
        reason_bits.append("למרות " + winner.matched_disruptions[0].kind.value + " מדווח על המסלול")
    if runner_up.matched_disruptions and not winner.matched_disruptions:
        reason_bits.append(
            f"ב{other_mode} יש "
            f"{runner_up.matched_disruptions[0].kind.value} מדווח — לכן נחות היום"
        )
    # Only cite a "slower than usual" angle when we have enough baseline
    # samples to trust the delta; otherwise the comparison is noise.
    _MIN_TRUSTED = 5
    if winner.sample_size >= _MIN_TRUSTED and winner.anomaly_delta_s > 5 * 60:
        reason_bits.append(
            f"גם {win_mode} איטי מהרגיל ב-{winner.anomaly_delta_s // 60} דק׳"
        )
    reason = " · ".join(reason_bits) or "אין דיווחים חריגים על שני המסלולים"
    if delta <= 0:
        return f"{win_mode} ו-{other_mode} צמודים ({win_min}–{other_min} דק׳). {reason}."
    return (
        f"מומלץ {win_mode} — {win_min} דק׳, "
        f"קצר ב-{delta} דק׳ מ-{other_mode} ({other_min} דק׳). {reason}."
    )


def _candidate_to_json(c: ModeCandidate) -> dict:
    return {
        "mode": c.mode.value,
        "score_s": c.score_s,
        "total_duration_min": c.route.total_duration_s // 60,
        "total_distance_km": round(c.route.total_distance_m / 1000, 1),
        "summary": c.route.summary,
        "transfer_count": c.transfer_count,
        "anomaly_delta_min": c.anomaly_delta_s // 60,
        "baseline_sample_size": c.sample_size,
        "warnings": c.route.warnings,
        "matched_disruptions": [
            {
                "kind": e.kind.value,
                "title": e.title,
                "source": e.source,
                "location_hint": e.location_hint,
            }
            for e in c.matched_disruptions
        ],
        "legs": [
            {
                "mode": l.mode.value,
                "summary": l.summary,
                "duration_s": l.duration_s,
                "distance_m": l.distance_m,
            }
            for l in c.route.legs
        ],
    }


def _mode_he(mode: TransportMode) -> str:
    return {
        TransportMode.DRIVING: "ברכב",
        TransportMode.TRANSIT: "בתח״צ",
        TransportMode.WALKING: "ברגל",
    }.get(mode, mode.value)


def _trace_to_json(t) -> dict:
    return {"successes": t.successes, "failures": t.failures}
