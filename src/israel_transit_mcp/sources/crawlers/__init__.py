"""Registry of concrete web crawlers.

Adding a new crawler = subclass `WebCrawlerSource`, drop the file in this
directory, and append the class to `ALL_CRAWLERS` below. The aggregator
iterates that list and turns each entry into its own TaskRunner job.
"""

from __future__ import annotations

from ..web_crawler import WebCrawlerSource
from .mivzakim import MivzakimCrawler


ALL_CRAWLERS: tuple[type[WebCrawlerSource], ...] = (
    MivzakimCrawler,
)


__all__ = ["ALL_CRAWLERS", "MivzakimCrawler"]
