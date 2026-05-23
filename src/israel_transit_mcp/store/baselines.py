"""Anomaly detection against a personal ETA baseline.

Given a saved route + today's observed ETA, decide whether today is
unusually slow. The decision uses p50 and p75 of the matching
(weekday, hour) bucket; an empty / undersized bucket suppresses the
verdict so we don't cry wolf on day one.
"""

from __future__ import annotations

from datetime import datetime

from ..models import AnomalyVerdict
from .db import Store


def _percentile(sorted_samples: list[int], p: float) -> int:
    if not sorted_samples:
        return 0
    if p <= 0:
        return sorted_samples[0]
    if p >= 1:
        return sorted_samples[-1]
    k = (len(sorted_samples) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_samples) - 1)
    if lo == hi:
        return sorted_samples[lo]
    weight = k - lo
    return int(sorted_samples[lo] * (1 - weight) + sorted_samples[hi] * weight)


def compute_anomaly(
    store: Store,
    saved_route_id: int,
    when: datetime,
    today_eta_s: int,
    threshold_minutes: int,
    min_samples: int,
    mode: str = "driving",
) -> AnomalyVerdict:
    weekday = when.weekday()
    hour = when.hour
    samples = sorted(store.bucket_observations(saved_route_id, weekday, hour, mode))
    p50 = _percentile(samples, 0.50)
    p75 = _percentile(samples, 0.75)
    delta = today_eta_s - p50
    threshold_s = threshold_minutes * 60
    if len(samples) < min_samples:
        return AnomalyVerdict(
            is_anomalous=False,
            today_eta_s=today_eta_s,
            baseline_p50_s=p50,
            baseline_p75_s=p75,
            delta_s=delta,
            sample_size=len(samples),
            explanation=(
                f"baseline has {len(samples)} samples (< {min_samples} required); "
                f"no anomaly verdict yet — keep observing."
            ),
        )
    is_anomalous = today_eta_s > p75 + threshold_s
    if is_anomalous:
        minutes_over = (today_eta_s - p75) // 60
        explanation = (
            f"today's ETA is {today_eta_s // 60} min vs p75 of {p75 // 60} min "
            f"(+{minutes_over} min over threshold)."
        )
    else:
        explanation = (
            f"today's ETA is {today_eta_s // 60} min, "
            f"within normal range (p50={p50 // 60}, p75={p75 // 60} min)."
        )
    return AnomalyVerdict(
        is_anomalous=is_anomalous,
        today_eta_s=today_eta_s,
        baseline_p50_s=p50,
        baseline_p75_s=p75,
        delta_s=delta,
        sample_size=len(samples),
        explanation=explanation,
    )
