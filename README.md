# israel-transit-mcp

An MCP server that gives Claude *live*, Israel-specific routing intelligence:
when to leave, which way to go, what's wrong on your usual commute *today*.

## The job

You ask Claude "should I leave for work now?" and Claude — via this MCP —
answers with: current driving ETA vs. your personal baseline, an anomaly flag,
the disruption events (RSS-confirmed) that explain the anomaly, and a
recommended departure time.

## Is there an existing API that does all this?

Short answer: **no.** Surveyed in May 2026: TomTom Traffic, HERE Traffic
v7, Mapbox Traffic Data, Google Routes, Bing / Azure Maps, INRIX
Roadway Analytics, Moovit (Mobileye), Otonomo, Waze for Cities. Every
one of them ships *one or two* of {live ETA, labeled disruption events,
personalized baseline} — none ships all three. The personalized layer
("is *today* unusual for *your* Wednesday at 08:00") is what nobody
packages because it requires per-user history. That's exactly what the
local SQLite store here does, on top of the commercial ETA + the news
sources nobody monetizes.

## Sources, picked deliberately

| Source | What it gives | TOS-clean? |
| --- | --- | --- |
| **Google Routes API v1** | Driving ETA, traffic-aware, departure-time prediction | Yes (5,000 free traffic-aware calls/month) |
| **Israeli news RSS** (Ynet flash + main, N12/Mako בארץ + צבא, Walla, ToI road-closures) | Road closures, protests, accidents — the *cause* behind anomalies | Yes (public RSS) |
| **Web crawler — mivzakim.net** | Flash-news headlines mirrored from outlets without RSS (Kan, Galei Tzahal, Telegram-first channels) | Public HTML, scraped politely (per-host lock, custom UA) |
| **IMS** (`weatheril`, planned) | Rain/storm warnings that explain delays | Yes (public, no key) |
| **MoT GTFS bundle** (planned) | Static bus + rail + light-rail schedules, all agencies | Yes (open data) |
| **Hasadna Open-Bus Stride** (planned) | Live bus positions, stop ETAs | Yes (community NGO mirror of MoT SIRI) |
| **`israel-rail-api`** (planned) | Live train arrivals + delays | Yes (well-known unofficial wrapper) |

Deliberately **not** integrated:
- **Waze** — TOS forbids automation. We rely on RSS + crawler + Google's
  Waze-fed traffic via the Routes API instead.
- **Moovit** — enterprise/paid only in 2026, no realistic personal API.
- **Pango** — no public developer program.
- **kan.org.il HTML lobby** — JS-injected, would need Playwright; not
  worth the dependency for v1. Better path: self-host RSSHub against
  Telegram channels `@kann_news` and `@mivzakim` for an RSS surface.

## Adding a new disruption source

Two shapes, both pluggable in one place.

**RSS feed** — add an entry to `DEFAULT_FEEDS` in
`sources/rss_news.py`. Done; aggregator picks it up.

**HTML scrape** — subclass `WebCrawlerSource`, define `name`, `urls`,
and `parse_items(soup, url) -> list[ScrapedItem]`. Add the class to
`ALL_CRAWLERS` in `sources/crawlers/__init__.py`. The aggregator's
default providers iterate that tuple, so registration is one line. The
base class owns HTTP, polite headers, per-host locking, the Hebrew
classifier, recency filtering, and `DisruptionEvent` construction.

## The hybrid that makes this work

There is **no machine-readable feed for Israeli protests / road closures**.
The MCP solves this with a hybrid:

1. The Routes API gives a *quantitative* signal — today's ETA is N minutes
   above your historical baseline for this (route, weekday, hour) bucket.
2. The RSS source provides *qualitative* candidates — recent news items
   mentioning the streets/cities along your route, filtered by Hebrew
   keywords (`חסימה`, `הפגנה`, `תאונה`, `פקקים`, `כביש סגור`).
3. The MCP returns both to Claude, which composes a human answer.

When RSS is silent and the ETA is still anomalous, the MCP tells Claude
exactly that: "anomalous delay, no news cause matched — consider asking
the user, or run web_search on `<street> חסימה`."

## RAG layer

Local SQLite at `~/.israel-transit-mcp/store.db`. Three small tables:
`saved_routes`, `eta_observations`, `user_prefs`. No vector store, no
embeddings — Hebrew place-name LIKE matching is enough at this scale.

The baseline is computed per (route, day_of_week, hour) bucket; anomaly is
declared when today's ETA exceeds p75 of the bucket by more than 5 minutes.

## Configuration

Copy `.env.example` to `.env` and set:

```
GOOGLE_MAPS_API_KEY=        # required for driving ETA
ISRAEL_TRANSIT_STORE_DIR=   # optional; default ~/.israel-transit-mcp
```

## Running

```bash
uv sync --extra transit --extra weather
israel-transit-mcp           # starts the MCP server on stdio

# verify the internals (no network):
python scripts/verify_end_to_end.py

# probe the live sources from your machine (network required):
python scripts/probe_live.py
```

## Connecting to Claude

Add to `~/.config/claude-code/mcp.json` (or your client's equivalent):

```json
{
  "mcpServers": {
    "israel-transit": {
      "command": "israel-transit-mcp"
    }
  }
}
```

Then ask Claude things like:
- "תכנן לי איך להגיע לעבודה עכשיו"
- "האם המסלול לעבודה חריג היום?"
- "מתי כדאי לי לצאת היום בערב לרכבת בתל אביב סבידור?"
- **"איך הכי נכון להגיע הביתה היום — ברכב או בתחבורה ציבורית?"**

## Tools surfaced to Claude

| Tool | What it answers |
| --- | --- |
| `save_route` / `list_routes` / `delete_route` | Persist commutes by name in the local RAG |
| `plan_route(mode=…)` | Driving / transit / walking — one-shot route plan with traffic |
| `check_disruptions` | RSS + web crawler events, cross-source deduped, optional location filter |
| `morning_briefing(name)` | Composed: route + disruptions + personal anomaly + Hebrew suggested_action for one mode |
| **`best_way(name)`** | **Compares driving vs transit in parallel, picks the winner, explains why** |

`best_way` is the headline tool: one call returns the side-by-side
result with a ranked winner, the personal-baseline-aware delta, and a
one-sentence Hebrew recommendation. Per-mode baselines stay isolated
(your usual driving 22 min and your usual bus 38 min don't pollute
each other), so the verdict is honest about which mode is unusual *today*.
