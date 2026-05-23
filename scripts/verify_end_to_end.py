"""End-to-end verification.

Runs the full pipeline (`morning_briefing` → `Aggregator` → injected
fake sources → SQLite store → anomaly math → severity ladder) without
touching the network. Exercises all four severity outcomes.

Run from the repo root:
    python -m israel_transit_mcp.scripts.verify_end_to_end

The script exits 0 if every assertion holds, non-zero otherwise. Use
this as the smoke gate before claiming "it works".
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator


def _bootstrap() -> None:
    """Force a clean store directory + a fake API key BEFORE the package
    is imported, so `Config.from_env()` (cached via lru_cache) picks them
    up. Must run before any `from israel_transit_mcp...` import."""
    tmp = Path(tempfile.mkdtemp(prefix="itm-verify-"))
    os.environ["ISRAEL_TRANSIT_STORE_DIR"] = str(tmp)
    os.environ["GOOGLE_MAPS_API_KEY"] = "FAKE-FOR-VERIFICATION-ONLY"
    os.environ["ANOMALY_THRESHOLD_MINUTES"] = "5"
    os.environ["BASELINE_MIN_SAMPLES"] = "5"


_bootstrap()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from israel_transit_mcp.aggregator import Aggregator
from israel_transit_mcp.app import get_config, get_store
from israel_transit_mcp.models import (
    DisruptionEvent,
    DisruptionKind,
    ETAObservation,
    LatLng,
    Place,
    Route,
    RouteLeg,
    SavedRoute,
    TransportMode,
)


# --- fakes ----------------------------------------------------------------


@dataclass
class FakeRouting:
    """Returns whatever Route list the test sets up — no HTTP.

    Pre-multi-modal callers pass `routes` and we return it for any mode.
    Multi-modal callers pass `routes_by_mode` and we return mode-specific
    canned data. The two forms are kept for backwards-compat with the
    earlier scenarios."""
    routes: list[Route] | None = None
    routes_by_mode: dict[TransportMode, list[Route]] | None = None
    name: str = "fake_routing"
    supports_modes: tuple = (TransportMode.DRIVING, TransportMode.TRANSIT)

    async def plan(
        self,
        origin: Place,
        destination: Place,
        mode: TransportMode,
        departure_time: datetime | None = None,
        **_extra: object,
    ) -> list[Route]:
        # Accept and ignore avoid_tolls / avoid_highways / avoid_ferries
        # via **_extra so the fake stays compatible with future signature
        # additions on the real source.
        if self.routes_by_mode is not None:
            return list(self.routes_by_mode.get(mode, []))
        return list(self.routes or [])


@dataclass
class FakeDisruption:
    """Returns the canned events. `recent` honors `window_hours`."""
    events: list[DisruptionEvent]
    name: str = "fake_rss"

    async def recent(
        self,
        window_hours: int = 6,
        min_confidence: float = 0.3,
    ) -> list[DisruptionEvent]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        return [e for e in self.events if (e.published_at or cutoff) >= cutoff]


def routing_provider_from(routing: FakeRouting):
    @asynccontextmanager
    async def cm() -> AsyncIterator[FakeRouting]:
        yield routing
    return lambda: cm()


def disruption_providers_from(rss: FakeDisruption):
    @asynccontextmanager
    async def cm() -> AsyncIterator[FakeDisruption]:
        yield rss
    return {"rss": lambda: cm()}


# --- canned data ----------------------------------------------------------


def _ayalon_route(duration_s: int) -> Route:
    """A route whose legs name איילון so location filtering can match."""
    return Route(
        mode=TransportMode.DRIVING,
        origin=Place(display_name="תל אביב"),
        destination=Place(display_name="הרצליה"),
        legs=[
            RouteLeg(
                mode=TransportMode.DRIVING,
                summary="עלייה לנתיבי איילון צפון",
                distance_m=5000,
                duration_s=duration_s // 3,
            ),
            RouteLeg(
                mode=TransportMode.DRIVING,
                summary="המשך באיילון צפון לכיוון הרצליה",
                distance_m=12000,
                duration_s=2 * duration_s // 3,
            ),
        ],
        total_duration_s=duration_s,
        total_distance_m=17000,
        summary=f"דרך איילון — {duration_s // 60} דק׳",
        source="fake_routing",
    )


def _ayalon_closure_event(when: datetime) -> DisruptionEvent:
    return DisruptionEvent(
        kind=DisruptionKind.CLOSURE,
        title="חסימה בנתיבי איילון צפון בעקבות תאונה קשה",
        description="תאונה רבת נפגעים, נתיב שמאלי נסגר",
        source="rss:ynet_flash",
        source_url="https://www.ynet.co.il/news/article/example",
        published_at=when,
        location_hint="נתיבי איילון",
    )


def _ramat_gan_unrelated_event(when: datetime) -> DisruptionEvent:
    return DisruptionEvent(
        kind=DisruptionKind.PROTEST,
        title="הפגנה ברמת גן ליד בורסת היהלומים",
        description="חוסמים את הצומת",
        source="rss:mako_israel",
        published_at=when,
        location_hint="רמת גן",
    )


# --- scenarios ------------------------------------------------------------


async def run_scenarios() -> int:
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        marker = "OK " if cond else "FAIL"
        print(f"  [{marker}] {label}" + (f"  — {detail}" if detail else ""))
        if not cond:
            failures.append(label)

    cfg = get_config()
    store = get_store()

    # Reset store between runs (the lru_cache holds it across scenarios).
    with store.tx() as c:
        c.execute("DELETE FROM eta_observations")
        c.execute("DELETE FROM saved_routes")

    route_id = store.save_route(
        SavedRoute(
            name="home->work",
            origin=Place(display_name="תל אביב"),
            destination=Place(display_name="הרצליה פיתוח"),
            mode=TransportMode.DRIVING,
        )
    )

    # Seed baseline: 10 observations of ~22 minutes (1320 s) at weekday=2
    # (Wednesday) hour=8. The test time below matches that bucket.
    test_when = datetime(2026, 5, 27, 8, 0, tzinfo=timezone.utc)  # a Wed @ 08:00
    assert test_when.weekday() == 2 and test_when.hour == 8
    for i in range(10):
        store.record_eta(
            ETAObservation(
                saved_route_id=route_id,
                observed_at=test_when - timedelta(days=i + 1),
                eta_s=1320 + (i % 3 - 1) * 60,  # 21-23 minute jitter
                weekday=2,
                hour=8,
            )
        )

    # Import the tool only AFTER the store exists; the @mcp.tool
    # wrapper is on the underlying function via `.fn` in FastMCP ≥ 0.4.
    from israel_transit_mcp.tools.morning_briefing import morning_briefing as mb_tool

    mb_fn = getattr(mb_tool, "fn", mb_tool)

    async def briefing(
        fake_route: Route,
        fake_events: list[DisruptionEvent],
    ) -> dict:
        # Build a custom aggregator with injected fakes, and monkey-patch
        # the tool's Aggregator construction. The cleanest way is to
        # swap the module-level Aggregator reference for one shot.
        import israel_transit_mcp.tools.morning_briefing as mb_mod

        routing = FakeRouting(routes=[fake_route])
        rss = FakeDisruption(events=fake_events)

        class _InjAggregator(Aggregator):
            def __init__(self, _cfg):
                super().__init__(
                    _cfg,
                    routing_provider=routing_provider_from(routing),
                    disruption_providers=disruption_providers_from(rss),
                )

        original = mb_mod.Aggregator
        mb_mod.Aggregator = _InjAggregator
        try:
            return await mb_fn(
                name="home->work",
                at_iso=test_when.isoformat(),
                window_hours=4,
                record_observation=False,  # don't pollute the baseline mid-test
            )
        finally:
            mb_mod.Aggregator = original

    # ── Scenario 1: LOW. Normal ETA, no relevant disruption.
    print("\nScenario 1 — LOW (normal ETA, no relevant disruption)")
    result = await briefing(
        fake_route=_ayalon_route(duration_s=1320),  # 22 min, on baseline
        fake_events=[_ramat_gan_unrelated_event(test_when - timedelta(minutes=30))],
    )
    check("ok=true", result.get("ok") is True)
    check("severity==low", result["briefing"]["severity"] == "low",
          detail=f"actual={result['briefing']['severity']}")
    check("anomaly.is_anomalous==false", result["briefing"]["anomaly"]["is_anomalous"] is False)
    check("no disruptions matched the route", len(result["briefing"]["disruptions"]) == 0,
          detail=f"matched={len(result['briefing']['disruptions'])}")
    check("disruption trace shows ramat_gan event was fetched",
          result["trace"]["disruption_events_total"] == 1)

    # ── Scenario 2: MED (disruption only, ETA normal).
    print("\nScenario 2 — MED (Ayalon closure reported, ETA still normal)")
    result = await briefing(
        fake_route=_ayalon_route(duration_s=1320),  # normal
        fake_events=[_ayalon_closure_event(test_when - timedelta(minutes=10))],
    )
    check("severity==med", result["briefing"]["severity"] == "med",
          detail=f"actual={result['briefing']['severity']}")
    check("anomaly.is_anomalous==false", result["briefing"]["anomaly"]["is_anomalous"] is False)
    check("1 disruption matched the route", len(result["briefing"]["disruptions"]) == 1)
    check(
        "matched disruption is the Ayalon closure",
        result["briefing"]["disruptions"][0]["kind"] == "closure",
    )

    # ── Scenario 3: MED (anomaly only, no matching disruption).
    print("\nScenario 3 — MED (slow ETA, no disruption explains it)")
    result = await briefing(
        fake_route=_ayalon_route(duration_s=2100),  # 35 min vs 22 baseline
        fake_events=[_ramat_gan_unrelated_event(test_when - timedelta(minutes=30))],
    )
    check("severity==med", result["briefing"]["severity"] == "med",
          detail=f"actual={result['briefing']['severity']}")
    check("anomaly.is_anomalous==true", result["briefing"]["anomaly"]["is_anomalous"] is True)
    check("no disruptions matched the route", len(result["briefing"]["disruptions"]) == 0)

    # ── Scenario 4: HIGH (anomaly AND matching disruption).
    print("\nScenario 4 — HIGH (slow ETA + Ayalon closure)")
    result = await briefing(
        fake_route=_ayalon_route(duration_s=2400),  # 40 min
        fake_events=[_ayalon_closure_event(test_when - timedelta(minutes=15))],
    )
    check("severity==high", result["briefing"]["severity"] == "high",
          detail=f"actual={result['briefing']['severity']}")
    check("anomaly.is_anomalous==true", result["briefing"]["anomaly"]["is_anomalous"] is True)
    check("1+ disruption matched", len(result["briefing"]["disruptions"]) >= 1)
    suggested = result["briefing"]["suggested_action"]
    check("suggested_action mentions earlier departure",
          ("מוקדם" in suggested) or ("עזוב" in suggested) or ("צאת" in suggested),
          detail=suggested[:80])

    # ── Sample print of the HIGH briefing so you can read what Claude sees.
    print("\n--- sample briefing JSON (HIGH scenario) ---")
    print(json.dumps(result["briefing"], indent=2, ensure_ascii=False))
    print("--- trace ---")
    print(json.dumps(result["trace"], indent=2, ensure_ascii=False))

    # ── Scenario 5: trace honesty when a source fails.
    print("\nScenario 5 — disruption source raises; routing still works")
    class _RaisingRss:
        async def recent(self, **kw):
            raise RuntimeError("simulated feed outage")
    @asynccontextmanager
    async def _bad_rss():
        yield _RaisingRss()
    import israel_transit_mcp.tools.morning_briefing as mb_mod

    class _PartialAggregator(Aggregator):
        def __init__(self, _cfg):
            super().__init__(
                _cfg,
                routing_provider=routing_provider_from(FakeRouting(routes=[_ayalon_route(1320)])),
                disruption_providers={"rss": lambda: _bad_rss()},
            )

    original = mb_mod.Aggregator
    mb_mod.Aggregator = _PartialAggregator
    try:
        result = await mb_fn(
            name="home->work",
            at_iso=test_when.isoformat(),
            window_hours=4,
            record_observation=False,
        )
    finally:
        mb_mod.Aggregator = original

    check("ok=true even with failed feed", result.get("ok") is True)
    check("trace.disruptions.failures names 'rss'", "rss" in result["trace"]["disruptions"]["failures"])
    check("error message captured", "RuntimeError" in result["trace"]["disruptions"]["failures"].get("rss", ""))
    print(f"  trace.disruptions.failures: {result['trace']['disruptions']['failures']}")

    # ── Scenario 6: web crawler parses real HTML with Hebrew traffic items.
    print("\nScenario 6 — MivzakimCrawler parses HTML through the full classifier")
    from bs4 import BeautifulSoup
    from israel_transit_mcp.sources.crawlers.mivzakim import MivzakimCrawler

    # A fixture shaped like a typical flash-news mirror page: <article>
    # blocks with h2>a titles and <time datetime=...> stamps. Mixes
    # traffic-relevant headlines with noise (sports + politics) so the
    # classifier has to discriminate.
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    fixture_html = f"""
    <html><body>
      <article>
        <h2><a href="/items/1">חסימה בנתיבי איילון צפון לאחר תאונה רבת נפגעים</a></h2>
        <time datetime="{fresh}">לפני 20 דקות</time>
      </article>
      <article>
        <h2><a href="/items/2">הפגנה גדולה ליד צומת רעננה, חוסמים את כביש 4</a></h2>
        <time datetime="{fresh}">לפני 20 דקות</time>
      </article>
      <article>
        <h2><a href="/items/3">משחק הליגה הסתיים 102:98 לטובת מכבי</a></h2>
        <time datetime="{fresh}">לפני 20 דקות</time>
      </article>
      <article>
        <h2><a href="/items/4">תאונה קטלנית בכביש 6 — הרוג אחד, שלושה פצועים</a></h2>
        <time datetime="{stale}">לפני יום</time>
      </article>
      <article>
        <h2><a href="/items/5">פגישת הממשלה אושרה ברוב גדול</a></h2>
        <time datetime="{fresh}">לפני 20 דקות</time>
      </article>
    </body></html>
    """

    crawler = MivzakimCrawler()
    parsed = crawler.parse_items(BeautifulSoup(fixture_html, "lxml"), "https://mivzakim.net/")
    check("parser found all 5 articles", len(parsed) == 5, detail=f"got {len(parsed)}")
    titles = [p.title for p in parsed]
    check(
        "first article title parsed correctly",
        "חסימה בנתיבי איילון" in titles[0],
        detail=titles[0][:60],
    )
    check(
        "links resolved against base URL",
        parsed[0].link.startswith("https://mivzakim.net/items/"),
        detail=parsed[0].link,
    )
    check(
        "datetime parsed from <time datetime=...>",
        parsed[0].published_at is not None,
        detail=str(parsed[0].published_at),
    )

    events = crawler._classify_and_filter(
        [("https://mivzakim.net/", p) for p in parsed],
        window_hours=6,
        min_confidence=0.3,
    )
    kinds_in_events = {e.kind.value for e in events}
    titles_in_events = {e.title for e in events}

    check(
        "stale article (20h old) was dropped",
        all("כביש 6" not in t for t in titles_in_events),
        detail=str(titles_in_events),
    )
    check(
        "sports headline filtered out",
        all("מכבי" not in t for t in titles_in_events),
        detail=str(titles_in_events),
    )
    check(
        "politics headline filtered out",
        all("ממשלה" not in t for t in titles_in_events),
        detail=str(titles_in_events),
    )
    check(
        "Ayalon closure classified as 'closure'",
        any(
            "איילון" in e.title and e.kind.value == "closure" for e in events
        ),
    )
    check(
        "protest classified as 'protest'",
        any(
            "הפגנה" in e.title and e.kind.value == "protest" for e in events
        ),
    )
    check(
        "every event carries source=web:mivzakim",
        all(e.source == "web:mivzakim" for e in events),
        detail=str({e.source for e in events}),
    )
    check(
        "every event has a source_url",
        all(e.source_url for e in events),
    )
    print(f"  {len(events)} event(s) passed: {sorted(kinds_in_events)}")

    # ── Scenarios 7-10: best_way (multi-modal compare).
    print("\nScenario 7 — best_way: driving 25min vs transit 45min, no events → driving wins")
    from israel_transit_mcp.tools.best_way import best_way as bw_tool
    bw_fn = getattr(bw_tool, "fn", bw_tool)

    def _transit_route(total_s: int, transfers: int = 1) -> Route:
        legs: list[RouteLeg] = [
            RouteLeg(mode=TransportMode.WALKING, summary="הליכה לתחנה", distance_m=400, duration_s=300),
        ]
        for i in range(transfers + 1):
            legs.append(
                RouteLeg(
                    mode=TransportMode.TRANSIT,
                    summary=f"אוטובוס 4{8+i}0",
                    distance_m=8000,
                    duration_s=(total_s - 600) // (transfers + 1),
                )
            )
        legs.append(
            RouteLeg(mode=TransportMode.WALKING, summary="הליכה ליעד", distance_m=300, duration_s=300)
        )
        return Route(
            mode=TransportMode.TRANSIT,
            origin=Place(display_name="תל אביב"),
            destination=Place(display_name="הרצליה"),
            legs=legs,
            total_duration_s=total_s,
            total_distance_m=17500,
            summary=f"תח״צ — {total_s // 60} דק׳",
            source="fake_routing",
        )

    async def _best_way(driving_route: Route, transit_route: Route, events: list[DisruptionEvent]) -> dict:
        import israel_transit_mcp.tools.best_way as bw_mod
        routing = FakeRouting(routes_by_mode={
            TransportMode.DRIVING: [driving_route],
            TransportMode.TRANSIT: [transit_route],
        })
        rss = FakeDisruption(events=events)

        class _MultiAggregator(Aggregator):
            def __init__(self, _cfg):
                super().__init__(
                    _cfg,
                    routing_provider=routing_provider_from(routing),
                    disruption_providers=disruption_providers_from(rss),
                )
        original = bw_mod.Aggregator
        bw_mod.Aggregator = _MultiAggregator
        try:
            return await bw_fn(
                name="home->work",
                at_iso=test_when.isoformat(),
                window_hours=4,
                modes=["driving", "transit"],
                record_observation=False,
            )
        finally:
            bw_mod.Aggregator = original

    result = await _best_way(
        driving_route=_ayalon_route(duration_s=1500),  # 25 min
        transit_route=_transit_route(total_s=2700, transfers=1),  # 45 min
        events=[],
    )
    check("ok=true", result.get("ok") is True)
    check("winner is driving", result["winner"]["mode"] == "driving",
          detail=f"actual={result['winner']['mode']}")
    check("recommendation names driving", "ברכב" in result["recommendation"],
          detail=result["recommendation"][:100])
    check("alternatives include transit", any(a["mode"] == "transit" for a in result["alternatives"]))
    check("baselines key carries both modes",
          set(result["baselines"].keys()) == {"driving", "transit"})

    # ── Scenario 8: Ayalon closure flips the verdict — transit beats stuck-on-road driving.
    print("\nScenario 8 — best_way: Ayalon closure + slow drive → transit wins")
    result = await _best_way(
        driving_route=_ayalon_route(duration_s=2700),   # 45 min driving today
        transit_route=_transit_route(total_s=2400, transfers=1),  # 40 min transit
        events=[_ayalon_closure_event(test_when - timedelta(minutes=10))],
    )
    check("winner is transit", result["winner"]["mode"] == "transit",
          detail=f"actual={result['winner']['mode']}")
    check("driving alternative shows the matched disruption",
          any(d["kind"] == "closure" for d in
              next(a for a in result["alternatives"] if a["mode"] == "driving")["matched_disruptions"]))
    check("recommendation explains why transit wins",
          ("בתח״צ" in result["recommendation"]) and (("דק׳" in result["recommendation"])),
          detail=result["recommendation"][:120])

    # ── Scenario 9: one mode fails — winner is the other.
    print("\nScenario 9 — best_way: transit source returns no routes → driving wins by default")
    result = await _best_way(
        driving_route=_ayalon_route(duration_s=1320),
        transit_route=_transit_route(total_s=0),  # zero is dropped by fake
        events=[],
    )
    # Override fake to return [] for transit specifically
    import israel_transit_mcp.tools.best_way as bw_mod
    routing = FakeRouting(routes_by_mode={
        TransportMode.DRIVING: [_ayalon_route(duration_s=1320)],
        TransportMode.TRANSIT: [],
    })
    rss = FakeDisruption(events=[])
    class _NoTransitAgg(Aggregator):
        def __init__(self, _cfg):
            super().__init__(
                _cfg,
                routing_provider=routing_provider_from(routing),
                disruption_providers=disruption_providers_from(rss),
            )
    original = bw_mod.Aggregator
    bw_mod.Aggregator = _NoTransitAgg
    try:
        result = await bw_fn(
            name="home->work",
            at_iso=test_when.isoformat(),
            modes=["driving", "transit"],
            record_observation=False,
        )
    finally:
        bw_mod.Aggregator = original
    check("ok=true even when transit empty", result.get("ok") is True)
    check("winner is driving", result["winner"]["mode"] == "driving")
    check("no transit alternative (it returned 0 routes)",
          not any(a["mode"] == "transit" for a in result["alternatives"]),
          detail=str([a["mode"] for a in result["alternatives"]]))

    # ── Scenario 10: per-mode baselines are isolated (drive history doesn't pollute bus).
    print("\nScenario 10 — best_way: per-mode baselines stay isolated")
    # Seed 6 transit observations at the same weekday/hour with eta=2400s.
    for i in range(6):
        store.record_eta(
            ETAObservation(
                saved_route_id=route_id,
                observed_at=test_when - timedelta(days=i + 1),
                eta_s=2400 + (i % 3 - 1) * 60,
                weekday=2,
                hour=8,
            ),
            mode="transit",
        )
    result = await _best_way(
        driving_route=_ayalon_route(duration_s=1320),
        transit_route=_transit_route(total_s=2400, transfers=1),
        events=[],
    )
    drive_baseline_min = result["baselines"]["driving"]["p50_min"]
    transit_baseline_min = result["baselines"]["transit"]["p50_min"]
    check(
        "driving baseline ~22 min (from earlier seed)",
        21 <= drive_baseline_min <= 23,
        detail=f"got {drive_baseline_min}",
    )
    check(
        "transit baseline ~40 min (fresh transit seed)",
        38 <= transit_baseline_min <= 42,
        detail=f"got {transit_baseline_min}",
    )

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run_scenarios()))
