"""Crawler for mivzakim.net — Israeli flash-news community mirror.

mivzakim.net aggregates breaking-news posts from across Israeli outlets
into a single, scrape-friendly page. Earlier research flagged it as
the practical way to ingest Kan 11 / Galei Tzahal / Telegram-first
channels that don't publish their own RSS.

Layout note (May 2026): the homepage renders a vertical list of
`<article>` elements. Each one carries:
  - a headline in `h2 > a` or `.title > a`
  - the publish timestamp in a `<time datetime="...">` attribute
  - an optional source label (Ynet / Kan / Mako) in `.source`

The selectors below are written *defensively*: we try several candidates
in order and accept the first that yields items. If a future redesign
breaks all of them, the crawler returns [] and the TaskRunner reports an
empty success — the rest of the disruption pipeline keeps working.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

from bs4 import BeautifulSoup, Tag

from ..web_crawler import ScrapedItem, WebCrawlerSource


class MivzakimCrawler(WebCrawlerSource):
    name = "web:mivzakim"
    label = "mivzakim.net"
    urls = ("https://mivzakim.net/",)
    per_page_limit = 60

    # Candidate selectors in priority order. We use the first one that
    # produces any results — site redesigns become a one-line fix.
    LIST_SELECTORS: tuple[str, ...] = (
        "article",
        ".post",
        ".mivzak",
        "li.news-item",
        ".story-card",
    )
    TITLE_SELECTORS: tuple[str, ...] = ("h2 a", "h3 a", ".title a", "a.title", "a")
    TIME_SELECTORS: tuple[str, ...] = ("time", ".date", ".time", ".pubdate")

    def parse_items(self, soup: BeautifulSoup, url: str) -> list[ScrapedItem]:
        articles = self._first_nonempty(soup, self.LIST_SELECTORS)
        out: list[ScrapedItem] = []
        for art in articles:
            item = self._parse_one(art, url)
            if item is None:
                continue
            out.append(item)
        return out

    def _first_nonempty(self, soup: BeautifulSoup, selectors: Iterable[str]) -> list[Tag]:
        for sel in selectors:
            matches = soup.select(sel)
            if matches:
                return matches
        return []

    def _parse_one(self, art: Tag, base_url: str) -> ScrapedItem | None:
        title_el = self._find_first(art, self.TITLE_SELECTORS)
        if title_el is None or not title_el.get_text(strip=True):
            return None
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "") if hasattr(title_el, "get") else ""
        if link and not link.startswith("http"):
            # join to base — keep it minimal so we don't import urllib here
            base = base_url.rstrip("/")
            link = base + ("" if link.startswith("/") else "/") + link
        published = self._extract_time(art)
        # mivzakim items are headlines only — description is rarely
        # present, so we leave it blank. The classifier still works on
        # the title alone.
        return ScrapedItem(
            title=title,
            link=str(link) if link else "",
            description="",
            published_at=published,
            location_hint="",
        )

    def _find_first(self, art: Tag, selectors: Iterable[str]) -> Tag | None:
        for sel in selectors:
            el = art.select_one(sel)
            if el is not None:
                return el
        return None

    def _extract_time(self, art: Tag) -> datetime | None:
        for sel in self.TIME_SELECTORS:
            el = art.select_one(sel)
            if el is None:
                continue
            dt_attr = el.get("datetime") if hasattr(el, "get") else None
            if dt_attr:
                parsed = _parse_iso_or_rfc(str(dt_attr))
                if parsed is not None:
                    return parsed
            text = el.get_text(strip=True)
            if text:
                parsed = _parse_iso_or_rfc(text)
                if parsed is not None:
                    return parsed
        return None


def _parse_iso_or_rfc(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None
