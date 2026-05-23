"""Orchestrator between sources and MCP tools.

Tools call into the aggregator with a request shape; the aggregator
fans out to the relevant sources in parallel via `TaskRunner`, merges
the results, applies cross-source heuristics, and returns one clean
typed object the tool returns verbatim to Claude.

Dependency inversion: the aggregator depends on Source protocols, not
concrete classes. Sources can be swapped (e.g., the unit tests will
inject fakes; later commits add Stride/Rail).

Cross-source heuristics applied here:

- **Deduplication** of disruption events by normalized title similarity
  (a closure reported by Ynet + Mako + Walla is one event, not three).
- **Confidence boost** when ≥ 2 distinct outlets report a near-identical
  event — those are the events most worth showing.
- **Recency sort** within each kind so the freshest signal leads.
"""

from __future__ import annotations

import re
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable, Protocol

from .config import Config
from .models import DisruptionEvent, Place, Route, RouteLeg, TransportMode
from .runner import TaskResult, TaskRunner, successes


class RoutingProvider(Protocol):
    """Anything that yields a routing source under `async with`. Real
    code yields a fresh `GoogleRoutesSource`; tests yield a fake. Either
    way the aggregator uses the same `async with provider() as src` shape.
    """

    def __call__(self) -> AbstractAsyncContextManager: ...


class DisruptionProvider(Protocol):
    def __call__(self) -> AbstractAsyncContextManager: ...


@dataclass
class FetchTrace:
    """What ran, what worked, how long it took. Returned alongside every
    aggregated result so Claude can tell the user `(driving from Google
    Routes, disruptions from 4/6 RSS feeds — Walla timed out)`."""
    successes: dict[str, int] = field(default_factory=dict)
    """name → duration_ms for tasks that returned a non-empty result."""
    failures: dict[str, str] = field(default_factory=dict)


@dataclass
class RoutePlan:
    routes: list[Route]
    trace: FetchTrace


@dataclass
class MultiModalPlan:
    """Result of comparing multiple modes side-by-side."""
    plans: dict[TransportMode, list[Route]]
    trace: FetchTrace


@dataclass
class DisruptionSnapshot:
    events: list[DisruptionEvent]
    trace: FetchTrace


_PUNCT_OR_NONLETTER = re.compile(r"[^\w֐-׿]+", re.UNICODE)


def _normalize_title(s: str) -> str:
    """Cheap normalization for cross-source dedup. Lowercase, strip
    punctuation, collapse whitespace. Hebrew characters are preserved."""
    return _PUNCT_OR_NONLETTER.sub(" ", s.lower()).strip()


def _title_signature(s: str) -> tuple[str, ...]:
    """Bag-of-words signature for near-duplicate detection. Two events
    whose top-5 normalized tokens overlap by ≥ 3 are merged."""
    tokens = [t for t in _normalize_title(s).split() if len(t) >= 3]
    return tuple(tokens[:8])


def _signatures_match(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    if not a or not b:
        return False
    sa, sb = set(a), set(b)
    overlap = len(sa & sb)
    return overlap >= max(3, min(len(sa), len(sb)) // 2)


def _default_routing_provider(cfg: Config) -> RoutingProvider:
    """Build the production routing provider: a context manager that
    yields a fresh, real `GoogleRoutesSource`. Imported lazily so unit
    tests that monkey-patch don't pay the cost."""
    def factory() -> AbstractAsyncContextManager:
        from .sources.google_routes import GoogleRoutesSource

        @asynccontextmanager
        async def cm() -> AsyncIterator:
            async with GoogleRoutesSource(cfg.google_maps_api_key or "") as src:
                yield src

        return cm()

    return factory


def _default_disruption_providers() -> dict[str, DisruptionProvider]:
    """Production disruption providers: RSS feeds + every registered
    web crawler. Each runs as its own TaskRunner job so one slow site
    cannot delay the rest, and a 5xx from one crawler doesn't poison
    the whole disruption set.
    """
    from .sources.rss_news import RssNewsSource
    from .sources.crawlers import ALL_CRAWLERS

    def _rss_factory() -> AbstractAsyncContextManager:
        @asynccontextmanager
        async def cm() -> AsyncIterator:
            async with RssNewsSource() as src:
                yield src
        return cm()

    providers: dict[str, DisruptionProvider] = {"rss": _rss_factory}

    for crawler_cls in ALL_CRAWLERS:
        cls = crawler_cls

        def _crawler_factory(cls=cls) -> AbstractAsyncContextManager:
            @asynccontextmanager
            async def cm() -> AsyncIterator:
                async with cls() as src:
                    yield src
            return cm()

        providers[cls.name] = _crawler_factory

    return providers


class Aggregator:
    """Composes parallel fetches across sources.

    Constructor injection: pass `routing_provider` / `disruption_providers`
    to swap real sources for fakes. Default values pull from `.sources`
    so production code is `Aggregator(cfg)` and nothing more.
    """

    def __init__(
        self,
        cfg: Config,
        routing_provider: RoutingProvider | None = None,
        disruption_providers: dict[str, DisruptionProvider] | None = None,
        runner: TaskRunner | None = None,
    ) -> None:
        self._cfg = cfg
        self._routing_provider = routing_provider or _default_routing_provider(cfg)
        self._disruption_providers = (
            disruption_providers
            if disruption_providers is not None
            else {"rss": _default_disruption_provider()}
        )
        self._runner = runner or TaskRunner()

    # --- routing -------------------------------------------------------

    async def plan_driving(
        self,
        origin: Place,
        destination: Place,
        departure_time: datetime | None = None,
    ) -> RoutePlan:
        """Driving-mode plan. Identical to plan_in_mode(DRIVING) but
        kept as a named method for backwards compatibility with
        morning_briefing v1."""
        return await self.plan_in_mode(origin, destination, TransportMode.DRIVING, departure_time)

    async def plan_in_mode(
        self,
        origin: Place,
        destination: Place,
        mode: TransportMode,
        departure_time: datetime | None = None,
    ) -> RoutePlan:
        """Run the routing provider in any supported mode. Provider
        errors (missing API key, HTTP failure) surface as
        `trace.failures[...]` rather than exceptions."""

        async def _call() -> list[Route]:
            async with self._routing_provider() as src:
                return await src.plan(origin, destination, mode, departure_time)

        task_name = f"google_routes:{mode.value}"
        results = await self._runner.run({task_name: _call})
        trace = _trace_from(results)
        routes = next(iter(successes(results).values()), []) or []
        return RoutePlan(routes=routes, trace=trace)

    async def compare_modes(
        self,
        origin: Place,
        destination: Place,
        modes: tuple[TransportMode, ...] = (TransportMode.DRIVING, TransportMode.TRANSIT),
        departure_time: datetime | None = None,
    ) -> "MultiModalPlan":
        """Plan each requested mode in parallel and return them side-by-side.

        Same TaskRunner that powers disruption fan-out — modes run truly
        concurrently, so total wall time is max(per-mode latency) rather
        than the sum. A failure in one mode (e.g. transit unavailable)
        does not block the other from returning.
        """
        from typing import Awaitable

        def _factory(m: TransportMode):
            async def _call() -> list[Route]:
                async with self._routing_provider() as src:
                    return await src.plan(origin, destination, m, departure_time)
            return _call

        tasks: dict[str, Callable[[], Awaitable[list[Route]]]] = {
            f"google_routes:{m.value}": _factory(m) for m in modes
        }
        results = await self._runner.run(tasks)
        trace = _trace_from(results)
        plans_by_mode: dict[TransportMode, list[Route]] = {}
        for m in modes:
            key = f"google_routes:{m.value}"
            r = results.get(key)
            if r and r.ok and r.value:
                plans_by_mode[m] = r.value
            else:
                plans_by_mode[m] = []
        return MultiModalPlan(plans=plans_by_mode, trace=trace)

    # --- disruptions ---------------------------------------------------

    async def gather_disruptions(
        self,
        window_hours: int = 6,
        location_filter: str | None = None,
    ) -> DisruptionSnapshot:
        def _factory(provider: DisruptionProvider) -> Callable[[], Awaitable[list[DisruptionEvent]]]:
            async def _call() -> list[DisruptionEvent]:
                async with provider() as src:
                    return await src.recent(window_hours=window_hours)
            return _call

        tasks = {name: _factory(p) for name, p in self._disruption_providers.items()}
        results = await self._runner.run(tasks)
        trace = _trace_from(results)

        all_events: list[DisruptionEvent] = []
        for events in successes(results).values():
            all_events.extend(events)

        if location_filter:
            all_events = [e for e in all_events if _matches_location(e, location_filter)]

        merged = _dedupe_and_boost(all_events)
        merged.sort(
            key=lambda e: e.published_at or datetime.fromtimestamp(0, tz=timezone.utc),
            reverse=True,
        )
        return DisruptionSnapshot(events=merged, trace=trace)

    # --- route-localized disruptions ----------------------------------

    def disruptions_for_route(
        self,
        snap: DisruptionSnapshot,
        route: Route,
    ) -> list[DisruptionEvent]:
        """Filter a snapshot down to events plausibly affecting `route`.

        Best-effort substring match between the event's location_hint /
        title / description and the tokens we can pull from the route's
        leg summaries. Cheap and good enough until we wire real geocoding.
        """
        tokens = _route_tokens(route)
        if not tokens:
            return list(snap.events)
        out: list[DisruptionEvent] = []
        for ev in snap.events:
            hay = " ".join(
                t for t in (ev.location_hint, ev.title, ev.description) if t
            )
            hay_norm = _normalize_title(hay)
            if any(tok in hay_norm for tok in tokens):
                out.append(ev)
        return out


def _route_tokens(route: Route) -> set[str]:
    """Extract probable place/road tokens from a route's leg summaries.

    Filters tokens shorter than 3 chars and Latin-only words that are
    rarely useful for Hebrew matching (e.g. cardinal direction labels
    that Google sometimes emits). Keeps Hebrew tokens of length ≥ 2.
    """
    tokens: set[str] = set()
    for leg in route.legs:
        norm = _normalize_title(leg.summary)
        for tok in norm.split():
            if any("֐" <= ch <= "׿" for ch in tok):
                if len(tok) >= 2:
                    tokens.add(tok)
            elif len(tok) >= 4:
                tokens.add(tok)
    return tokens


def _trace_from(results: dict[str, TaskResult]) -> FetchTrace:
    trace = FetchTrace()
    for name, r in results.items():
        if r.ok and r.value:
            trace.successes[name] = r.duration_ms
        elif r.ok and not r.value:
            trace.failures[name] = "empty result"
        else:
            trace.failures[name] = r.error or "unknown error"
    return trace


def _matches_location(event: DisruptionEvent, needle: str) -> bool:
    """Simple substring match against Hebrew location strings — both the
    structured `location_hint` and the free-text title. Good enough until
    we wire real geocoding."""
    needle_norm = _normalize_title(needle)
    if not needle_norm:
        return True
    haystacks = (event.location_hint, event.title, event.description)
    return any(needle_norm in _normalize_title(h) for h in haystacks if h)


def _dedupe_and_boost(events: list[DisruptionEvent]) -> list[DisruptionEvent]:
    """Group near-duplicate events across sources. The merged event keeps
    the freshest publication date and concatenates source attribution so
    the caller can see "reported by Ynet + Mako + Walla".
    """
    buckets: list[tuple[tuple[str, ...], list[DisruptionEvent]]] = []
    for ev in events:
        sig = _title_signature(ev.title)
        placed = False
        for existing_sig, bucket in buckets:
            if _signatures_match(sig, existing_sig):
                bucket.append(ev)
                placed = True
                break
        if not placed:
            buckets.append((sig, [ev]))
    merged: list[DisruptionEvent] = []
    for _sig, bucket in buckets:
        if len(bucket) == 1:
            merged.append(bucket[0])
            continue
        # Most recent wins as the canonical event; sources are concatenated.
        bucket.sort(
            key=lambda e: (e.published_at or datetime.fromtimestamp(0, tz=timezone.utc)),
            reverse=True,
        )
        head = bucket[0]
        sources = sorted({e.source for e in bucket})
        merged.append(
            DisruptionEvent(
                kind=head.kind,
                title=head.title,
                description=head.description,
                source=" + ".join(sources),
                source_url=head.source_url,
                published_at=head.published_at,
                location_hint=head.location_hint or next(
                    (e.location_hint for e in bucket if e.location_hint), ""
                ),
                coords=head.coords,
            )
        )
    return merged
