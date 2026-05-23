"""Web-page scraper base for sites that publish disruption info as HTML.

Several official Israeli sources publish road-closure / incident
information *only* as HTML pages — Kan 11 dropped their RSS years ago,
the Israel Police only posts on Facebook, mivzakim.net mirrors flash
news in a scrape-friendly layout. To pick those up we need a small,
polite HTML crawler that produces the same `DisruptionEvent` shape as
RSS, so the rest of the pipeline (aggregator dedup, Hebrew classifier,
route-localized filter, anomaly composition) keeps working unchanged.

Design:

- `WebCrawlerSource` is an abstract base implementing `DisruptionSource`.
  Concrete crawlers subclass it and define ONE method: `parse_items`,
  turning a `BeautifulSoup` doc into raw `ScrapedItem`s. The base owns
  HTTP, polite headers, robots respect, the keyword classifier pass,
  the recency window, and the `DisruptionEvent` construction.

- `Crawler` registry: each concrete crawler is added to a single list
  the aggregator iterates. Adding a new site = subclass + register, no
  changes anywhere else (OCP).

- Polite by default: custom User-Agent identifying the project,
  `Accept-Language: he`, follow redirects, 1 in-flight request per
  host (the TaskRunner runs separate hosts in parallel — concurrent
  fetches against the same host go sequential via per-host semaphore
  here).

- Resilient: HTTP errors, parse errors, and empty pages all become
  empty result lists. Never raises. The TaskRunner records the empty
  return as a successful task with zero items, which the aggregator
  reports honestly in trace.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..keywords import classify
from ..models import DisruptionEvent


_HOST_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(host: str) -> asyncio.Lock:
    """Per-host lock so a single crawler instance doesn't hammer one
    site even if it lists multiple URLs. Different hosts run in parallel
    via the outer TaskRunner."""
    if host not in _HOST_LOCKS:
        _HOST_LOCKS[host] = asyncio.Lock()
    return _HOST_LOCKS[host]


@dataclass
class ScrapedItem:
    """Raw item from a page — fields are nullable because not every site
    publishes every field; the base class fills in sane defaults."""
    title: str
    link: str = ""
    description: str = ""
    published_at: datetime | None = None
    location_hint: str = ""


class WebCrawlerSource(ABC):
    """Abstract base. Subclass and override `name`, `urls`, `parse_items`."""

    #: stable id used in DisruptionEvent.source, e.g. `web:mivzakim`.
    name: str = "web:unknown"

    #: human label for trace output.
    label: str = "unknown"

    #: pages this crawler fetches. Most crawlers have one; some (a site
    #: with traffic + military sub-sections) might have a few.
    urls: tuple[str, ...] = ()

    #: how many items to keep per page after parsing.
    per_page_limit: int = 40

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "WebCrawlerSource":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=8.0,
                headers={
                    "User-Agent": (
                        "israel-transit-mcp/0.1 (+https://github.com; "
                        "personal commute monitor)"
                    ),
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "he,en;q=0.7",
                },
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def recent(
        self,
        window_hours: int = 6,
        min_confidence: float = 0.3,
    ) -> list[DisruptionEvent]:
        items: list[tuple[str, ScrapedItem]] = []
        for url in self.urls:
            try:
                page_items = await self._fetch_and_parse(url)
            except Exception:
                # Any failure on a single URL is non-fatal; other URLs in
                # this crawler still run.
                continue
            for it in page_items[: self.per_page_limit]:
                items.append((url, it))
        return self._classify_and_filter(items, window_hours, min_confidence)

    async def _fetch_and_parse(self, url: str) -> list[ScrapedItem]:
        client = await self._ensure_client()
        host = httpx.URL(url).host or url
        async with _lock_for(host):
            resp = await client.get(url)
        if resp.status_code >= 400:
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        return list(self.parse_items(soup, url))

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=8.0)
        return self._client

    @abstractmethod
    def parse_items(self, soup: BeautifulSoup, url: str) -> list[ScrapedItem]:
        """Turn a parsed page into the list of items to classify.

        Implementations should be defensive: missing elements → skip the
        item; unexpected layout → return an empty list. Never raise."""

    def _classify_and_filter(
        self,
        items: list[tuple[str, ScrapedItem]],
        window_hours: int,
        min_confidence: float,
    ) -> list[DisruptionEvent]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        out: list[DisruptionEvent] = []
        for url, it in items:
            if it.published_at and it.published_at < cutoff:
                continue
            text = f"{it.title}\n{it.description}".strip()
            cls = classify(text)
            if not cls.matched or cls.confidence < min_confidence:
                continue
            location = it.location_hint or (cls.tier2_hits[0] if cls.tier2_hits else "")
            out.append(
                DisruptionEvent(
                    kind=cls.kind,
                    title=it.title,
                    description=it.description[:500],
                    source=self.name,
                    source_url=it.link or url,
                    published_at=it.published_at,
                    location_hint=location,
                )
            )
        return out
