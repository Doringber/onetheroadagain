"""Live probe — runs the real sources against the real internet.

Use this from a machine with network access to the Israeli sites
(this sandbox is geo-blocked). Reports, for each configured source:
- how long the fetch took
- how many raw items came back
- how many survived the Hebrew traffic-keyword classifier
- the top 5 matched events with their inferred kind and URL

No API key required for RSS / crawlers; Google Routes is skipped
unless GOOGLE_MAPS_API_KEY is set in the environment.

Run from the project root:
    python scripts/probe_live.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from israel_transit_mcp.runner import TaskRunner, successes, failures  # noqa: E402
from israel_transit_mcp.sources.rss_news import RssNewsSource, DEFAULT_FEEDS  # noqa: E402
from israel_transit_mcp.sources.crawlers import ALL_CRAWLERS  # noqa: E402


async def probe_rss() -> None:
    print("\n=== RSS feeds ===")
    async with RssNewsSource() as src:
        runner = TaskRunner(max_concurrency=6, task_timeout_s=8.0, overall_timeout_s=20.0)
        tasks = {f.key: (lambda f=f: src.fetch_feed(f)) for f in DEFAULT_FEEDS}
        results = await runner.run(tasks)
        for name, r in results.items():
            if r.ok and r.value is not None:
                print(f"  [OK ] {name:22s}  {len(r.value):3d} items  {r.duration_ms:5d}ms")
            else:
                print(f"  [ERR] {name:22s}  {r.error}")

        events = await src.recent(window_hours=24)
        print(f"\n  {len(events)} item(s) passed the Hebrew traffic classifier:")
        for e in events[:10]:
            ts = e.published_at.astimezone().strftime("%H:%M") if e.published_at else "  ?  "
            print(f"  [{ts}] [{e.kind.value:8s}] {e.source}")
            print(f"           {e.title[:90]}")


async def probe_crawlers() -> None:
    print("\n=== Web crawlers ===")
    for cls in ALL_CRAWLERS:
        async with cls() as crawler:
            print(f"\n  {cls.name} → {', '.join(cls.urls)}")
            t0 = datetime.now(timezone.utc)
            events = await crawler.recent(window_hours=12)
            elapsed = (datetime.now(timezone.utc) - t0).total_seconds() * 1000
            print(f"  {len(events)} event(s) matched the classifier  ({elapsed:.0f}ms)")
            for e in events[:5]:
                ts = e.published_at.astimezone().strftime("%H:%M") if e.published_at else "  ?  "
                print(f"    [{ts}] [{e.kind.value:8s}] {e.title[:90]}")
                if e.source_url:
                    print(f"             → {e.source_url}")


async def probe_google() -> None:
    print("\n=== Google Routes ===")
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("  GOOGLE_MAPS_API_KEY not set — skipping")
        return
    from israel_transit_mcp.sources.google_routes import GoogleRoutesSource
    from israel_transit_mcp.models import Place, TransportMode

    async with GoogleRoutesSource(api_key) as src:
        routes = await src.plan(
            Place(display_name="נחלת בנימין 30, תל אביב"),
            Place(display_name="הרצליה פיתוח"),
            TransportMode.DRIVING,
        )
        if not routes:
            print("  no routes returned (check API key + billing)")
            return
        for r in routes[:3]:
            mins = r.total_duration_s // 60
            km = r.total_distance_m / 1000
            print(f"  [{mins:3d} min  {km:5.1f} km]  {r.summary[:80]}")


async def main() -> None:
    print("israel-transit-mcp — live source probe")
    print(f"started {datetime.now().isoformat(timespec='seconds')}")
    await probe_rss()
    await probe_crawlers()
    await probe_google()


if __name__ == "__main__":
    asyncio.run(main())
