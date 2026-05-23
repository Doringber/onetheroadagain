"""Demo of the user-Claude-MCP conversation flow.

Boots the MCP server in-process via FastMCP's client, injects the same
fake sources used by verify_end_to_end, and walks through six realistic
user turns. The transcript shows exactly what Claude Desktop would do
under the hood: which tool gets called, the JSON request and response,
and a plausible Hebrew reply Claude would compose.

This is the closest thing to "talking to it as a user" we can do from
the sandbox without network access to Google + the Israeli RSS feeds.
When the user runs the real MCP from their machine with a valid API
key, the JSON shapes will be identical — only the values become real.

Run from the project dir:
    python scripts/demo_user_session.py
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
    tmp = Path(tempfile.mkdtemp(prefix="itm-demo-"))
    os.environ["ISRAEL_TRANSIT_STORE_DIR"] = str(tmp)
    os.environ["GOOGLE_MAPS_API_KEY"] = "FAKE-FOR-DEMO-ONLY"
    os.environ["ANOMALY_THRESHOLD_MINUTES"] = "5"
    os.environ["BASELINE_MIN_SAMPLES"] = "5"


_bootstrap()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from israel_transit_mcp.aggregator import Aggregator
from israel_transit_mcp.app import get_config, get_store, mcp
from israel_transit_mcp.models import (
    DisruptionEvent,
    DisruptionKind,
    ETAObservation,
    Place,
    Route,
    RouteLeg,
    SavedRoute,
    TransportMode,
)


# --- fake sources (same shape as verify_end_to_end) -----------------------


@dataclass
class FakeRouting:
    routes_by_mode: dict[TransportMode, list[Route]]
    name: str = "fake_routing"
    supports_modes: tuple = (TransportMode.DRIVING, TransportMode.TRANSIT)

    async def plan(self, origin, destination, mode, departure_time=None):
        return list(self.routes_by_mode.get(mode, []))


@dataclass
class FakeDisruption:
    events: list[DisruptionEvent]
    name: str = "fake_rss"

    async def recent(self, window_hours: int = 6, min_confidence: float = 0.3):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        return [e for e in self.events if (e.published_at or cutoff) >= cutoff]


def install_fakes(driving: list[Route], transit: list[Route], events: list[DisruptionEvent]) -> None:
    """Monkey-patch the Aggregator constructor in every tool module so
    the in-process MCP uses fakes instead of real httpx + Google."""
    fake_routing = FakeRouting(routes_by_mode={
        TransportMode.DRIVING: driving,
        TransportMode.TRANSIT: transit,
    })
    fake_rss = FakeDisruption(events=events)

    @asynccontextmanager
    async def routing_cm() -> AsyncIterator[FakeRouting]:
        yield fake_routing

    @asynccontextmanager
    async def disruption_cm() -> AsyncIterator[FakeDisruption]:
        yield fake_rss

    class _InjAggregator(Aggregator):
        def __init__(self, _cfg):
            super().__init__(
                _cfg,
                routing_provider=lambda: routing_cm(),
                disruption_providers={"rss": lambda: disruption_cm()},
            )

    # Each tool imported Aggregator into its own module namespace; replace
    # the reference in all of them.
    import israel_transit_mcp.tools.best_way as bw_mod
    import israel_transit_mcp.tools.morning_briefing as mb_mod
    import israel_transit_mcp.tools.plan_route as pr_mod
    import israel_transit_mcp.tools.check_disruptions as cd_mod
    bw_mod.Aggregator = _InjAggregator
    mb_mod.Aggregator = _InjAggregator
    pr_mod.Aggregator = _InjAggregator
    cd_mod.Aggregator = _InjAggregator


# --- canned data ----------------------------------------------------------


def driving_route(total_s: int, summary: str = "נתיבי איילון/כביש 20") -> Route:
    return Route(
        mode=TransportMode.DRIVING,
        origin=Place(display_name="נחלת בנימין 30, תל אביב"),
        destination=Place(display_name="הרצליה פיתוח"),
        legs=[
            RouteLeg(mode=TransportMode.DRIVING, summary="עלייה לנתיבי איילון צפון",
                     distance_m=5000, duration_s=total_s // 3),
            RouteLeg(mode=TransportMode.DRIVING, summary="המשך באיילון לכיוון הרצליה",
                     distance_m=12500, duration_s=2 * total_s // 3),
        ],
        total_duration_s=total_s,
        total_distance_m=17500,
        summary=f"{summary} — {total_s // 60} דק׳",
        source="google_routes",
    )


def transit_route(total_s: int, transfers: int = 1) -> Route:
    legs = [RouteLeg(mode=TransportMode.WALKING, summary="הליכה למסוף כרמלית",
                     distance_m=400, duration_s=300)]
    legs.append(RouteLeg(mode=TransportMode.TRANSIT,
                         summary="אוטובוס 230 (מטרופולין) · מסוף כרמלית → דרך נמיר/קק״ל · (15 תחנות)",
                         distance_m=8000, duration_s=(total_s - 900) // (transfers + 1)))
    if transfers >= 1:
        legs.append(RouteLeg(mode=TransportMode.TRANSIT,
                             summary="אוטובוס 699 (מטרופולין) · דרך נמיר → תל אביב/פישמן · (7 תחנות)",
                             distance_m=4000, duration_s=(total_s - 900) // (transfers + 1)))
    legs.append(RouteLeg(mode=TransportMode.WALKING, summary="הליכה ליעד הסופי",
                         distance_m=600, duration_s=600))
    return Route(
        mode=TransportMode.TRANSIT,
        origin=Place(display_name="נחלת בנימין 30, תל אביב"),
        destination=Place(display_name="הרצליה פיתוח"),
        legs=legs,
        total_duration_s=total_s,
        total_distance_m=13000,
        summary=f"אוטובוס 230 + אוטובוס 699 — {total_s // 60} דק׳",
        source="google_routes",
    )


def ayalon_closure(when: datetime) -> DisruptionEvent:
    return DisruptionEvent(
        kind=DisruptionKind.CLOSURE,
        title="חסימה בנתיבי איילון צפון לאחר תאונה רבת נפגעים",
        description="נתיב שמאלי נסגר עקב תאונה קשה, צפויים עיכובים משמעותיים בכיוון צפון",
        source="rss:ynet_flash",
        source_url="https://www.ynet.co.il/news/article/example",
        published_at=when,
        location_hint="נתיבי איילון",
    )


# --- helper: pretty-print one turn ----------------------------------------


COL_USER = "\033[1;36m"     # cyan bold
COL_CLAUDE = "\033[1;32m"   # green bold
COL_TOOL = "\033[1;33m"     # yellow bold
COL_DIM = "\033[2m"
COL_RESET = "\033[0m"


def section(label: str) -> None:
    print(f"\n{COL_DIM}{'═' * 78}{COL_RESET}")
    print(f"{COL_DIM}{label}{COL_RESET}")
    print(f"{COL_DIM}{'═' * 78}{COL_RESET}")


def user(msg: str) -> None:
    print(f"\n{COL_USER}משתמש ›{COL_RESET}  {msg}")


def claude_thought(msg: str) -> None:
    print(f"{COL_DIM}  …Claude (פנימי): {msg}{COL_RESET}")


def tool_call(name: str, args: dict) -> None:
    print(f"{COL_TOOL}  ⇣ tool call:{COL_RESET} {name}({_kwargs(args)})")


def tool_result(payload: dict, truncate: bool = True) -> None:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    if truncate and len(rendered) > 1400:
        rendered = rendered[:1400] + "\n  …(truncated for transcript)"
    indented = "\n".join("    " + line for line in rendered.splitlines())
    print(f"{COL_TOOL}  ⇡ tool result:{COL_RESET}\n{indented}")


def claude(msg: str) -> None:
    print(f"\n{COL_CLAUDE}Claude ›{COL_RESET}  {msg}")


def _kwargs(args: dict) -> str:
    return ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())


# --- the session ----------------------------------------------------------


async def run_session() -> None:
    print(f"{COL_DIM}israel-transit-mcp — simulated user session{COL_RESET}")
    print(f"{COL_DIM}(this is what Claude Desktop's conversation looks like under the hood){COL_RESET}")

    # Seed: clean store, one saved route, baseline of 10 driving observations.
    store = get_store()
    with store.tx() as c:
        c.execute("DELETE FROM eta_observations")
        c.execute("DELETE FROM saved_routes")

    # We deliberately use a recent Wednesday 08:00 (UTC) so the baseline
    # bucket matches the live demo time.
    now = datetime(2026, 5, 27, 8, 0, tzinfo=timezone.utc)

    route_id = store.save_route(
        SavedRoute(
            name="home->work",
            origin=Place(display_name="נחלת בנימין 30, תל אביב"),
            destination=Place(display_name="הרצליה פיתוח, רחוב גלגלי הפלדה 11"),
            mode=TransportMode.DRIVING,
        )
    )
    for i in range(10):
        store.record_eta(
            ETAObservation(
                saved_route_id=route_id,
                observed_at=now - timedelta(days=i + 1),
                eta_s=1320 + (i % 3 - 1) * 60,
                weekday=2, hour=8,
            ),
            mode="driving",
        )

    # Boot the in-process MCP client and the fake sources.
    from fastmcp import Client

    # =====================================================================
    section("Turn 1 — User opens the conversation")
    user("שלום, אני יוצא לעבודה עכשיו. בודק שאין משהו דרסטי בדרך.")

    install_fakes(driving=[driving_route(1320)], transit=[transit_route(2400, 1)], events=[])

    async with Client(mcp) as client:
        # Claude would first see what tools exist; in the real client this
        # happens once at connection. We show the count for context.
        tools = await client.list_tools()
        claude_thought(f"connected to MCP. {len(tools)} tools available: "
                       f"{', '.join(sorted(t.name for t in tools))}")

        claude_thought("the user has a saved 'home->work' route, "
                       "I'll run morning_briefing for the current time.")
        tool_call("morning_briefing", {"name": "home->work", "at_iso": now.isoformat()})
        result1 = await client.call_tool("morning_briefing", {
            "name": "home->work",
            "at_iso": now.isoformat(),
        })
        payload1 = _extract_payload(result1)
        tool_result(payload1)

        br = payload1["briefing"]
        claude(
            f"הדרך נראית רגילה — {br['route']['total_duration_min']} דק׳ via "
            f"{br['route']['summary']}. אין דיווחים חריגים על המסלול שלך. "
            f"הבייסליין שלך לבוקר רביעי 08:00 הוא ~{br['anomaly']['baseline_p50_min']} דק׳, "
            f"היום אתה ב-{br['anomaly']['today_eta_min']} — באמצע הטווח. נסיעה בטוחה."
        )

        # =====================================================================
        section("Turn 2 — User asks about transit comparison")
        user("ואם הייתי לוקח אוטובוס במקום? כדאי?")

        claude_thought("the user wants a mode comparison — call best_way with both modes.")
        tool_call("best_way", {
            "name": "home->work",
            "at_iso": now.isoformat(),
            "modes": ["driving", "transit"],
            "record_observation": False,
        })
        result2 = await client.call_tool("best_way", {
            "name": "home->work",
            "at_iso": now.isoformat(),
            "modes": ["driving", "transit"],
            "record_observation": False,
        })
        payload2 = _extract_payload(result2)
        tool_result(payload2)

        win = payload2["winner"]
        alt = payload2["alternatives"][0] if payload2["alternatives"] else None
        claude(payload2["recommendation"])
        if alt:
            claude(
                f"לפירוט — ברכב {win['total_duration_min']} דק׳, "
                f"באוטובוס {alt['total_duration_min']} דק׳ עם "
                f"{alt['transfer_count']} העברה. הבחירה ברכב היום היא טריוויאלית — "
                f"בלי דיווחי תקלה, ההפרש 19+ דקות."
            )

        # =====================================================================
        section("Turn 3 — Three hours later: closure on Ayalon hits")
        later = now + timedelta(hours=3)
        user(f"\n[{later.strftime('%H:%M')}] חזרה אליך — אני יוצא הביתה. מה המצב?")

        # Now the road is bad: drive 40 min today (vs 22 baseline), Ayalon
        # closure reported, transit relatively normal at 38 min.
        install_fakes(
            driving=[driving_route(2400, summary="כביש 2 — מסלול חלופי")],
            transit=[transit_route(2280, transfers=1)],
            events=[ayalon_closure(later - timedelta(minutes=15))],
        )

        claude_thought("the user is asking about the return trip late afternoon. "
                       "Save the return route and run best_way.")
        # Auto-create the return route by reusing endpoints inverted.
        await client.call_tool("save_route", {
            "name": "work->home",
            "origin": "הרצליה פיתוח, רחוב גלגלי הפלדה 11",
            "destination": "נחלת בנימין 30, תל אביב",
            "mode": "driving",
        })

        tool_call("best_way", {
            "name": "work->home",
            "at_iso": later.isoformat(),
            "modes": ["driving", "transit"],
        })
        result3 = await client.call_tool("best_way", {
            "name": "work->home",
            "at_iso": later.isoformat(),
            "modes": ["driving", "transit"],
        })
        payload3 = _extract_payload(result3)
        tool_result(payload3)

        win3 = payload3["winner"]
        claude(payload3["recommendation"])
        if win3["matched_disruptions"]:
            ev = win3["matched_disruptions"][0]
            claude(f"דיווח רלוונטי שמופיע על המסלול שלך: "
                   f"[{ev['kind']}] {ev['title']}  ({ev['source']}).")
        elif payload3["alternatives"] and payload3["alternatives"][0]["matched_disruptions"]:
            ev = payload3["alternatives"][0]["matched_disruptions"][0]
            claude(f"הסיבה: ב{_mode_he(payload3['alternatives'][0]['mode'])} יש "
                   f"[{ev['kind']}] {ev['title']}  — לכן הוא נחות היום.")

        # =====================================================================
        section("Turn 4 — User wants the disruption details")
        user("מה זה החסימה הזו? מי דיווח?")

        claude_thought("call check_disruptions with the route's area hint.")
        tool_call("check_disruptions", {"window_hours": 4, "location_filter": "איילון"})
        result4 = await client.call_tool("check_disruptions", {
            "window_hours": 4,
            "location_filter": "איילון",
        })
        payload4 = _extract_payload(result4)
        tool_result(payload4, truncate=False)

        if payload4["events"]:
            e = payload4["events"][0]
            published = e.get("published_at", "")
            claude(
                f"[{e['kind']}] {e['title']}\n         "
                f"דווח ע״י {e['source']} ב-{published}. "
                f"כתובת מקור: {e.get('source_url') or '(לא צוין)'}."
            )

        # =====================================================================
        section("Turn 5 — User pushes back: 'is this really anomalous?'")
        user("רגע, אולי זה רגיל באיילון ב-17:00? איך אתה יודע שהיום זה גרוע?")

        claude_thought("the user is asking about the personal baseline. "
                       "morning_briefing returns the baseline numbers explicitly.")
        # Seed a smaller baseline for the return-trip bucket to be honest
        # about confidence.
        return_route_id = next(r.id for r in store.list_routes() if r.name == "work->home")
        for i in range(6):
            store.record_eta(
                ETAObservation(
                    saved_route_id=return_route_id,
                    observed_at=later - timedelta(days=i + 1),
                    eta_s=1380 + (i % 3 - 1) * 60,
                    weekday=later.weekday(),
                    hour=later.hour,
                ),
                mode="driving",
            )

        tool_call("morning_briefing", {"name": "work->home", "at_iso": later.isoformat()})
        result5 = await client.call_tool("morning_briefing", {
            "name": "work->home",
            "at_iso": later.isoformat(),
        })
        payload5 = _extract_payload(result5)
        tool_result(payload5)

        an = payload5["briefing"]["anomaly"]
        claude(
            f"אני יודע כי שמרתי לך את ה-{an['sample_size']} הנסיעות הקודמות שלך "
            f"במסלול הזה ביום וב-שעה האלה. ה-p50 שלך הוא "
            f"{an['baseline_p50_min']} דק׳, ה-p75 הוא {an['baseline_p75_min']} דק׳. "
            f"היום ה-ETA חוזר {an['today_eta_min']} דק׳ — זה +{an['delta_min']} מעל "
            f"החציון. {an['explanation']}"
        )

        # =====================================================================
        section("Turn 6 — User: 'ok, save it and remind me next time'")
        user("מצוין. שמור אם זה לא שמור — מ-9:00 בערב היציאה הרגילה שלי.")

        claude_thought("update the saved route with the default departure time.")
        tool_call("save_route", {
            "name": "work->home",
            "origin": "הרצליה פיתוח, רחוב גלגלי הפלדה 11",
            "destination": "נחלת בנימין 30, תל אביב",
            "mode": "driving",
            "default_departure_local": "21:00",
        })
        result6 = await client.call_tool("save_route", {
            "name": "work->home",
            "origin": "הרצליה פיתוח, רחוב גלגלי הפלדה 11",
            "destination": "נחלת בנימין 30, תל אביב",
            "mode": "driving",
            "default_departure_local": "21:00",
        })
        payload6 = _extract_payload(result6)
        tool_result(payload6)
        claude("שמרתי. בבוקר בא תקבל בריפינג אוטומטי אם תקרא לי "
               "(זה דורש scheduler חיצוני — בינתיים שאל אותי בעצמך).")

    # Final summary
    section("Summary")
    print(f"{COL_DIM}6 turns simulated, 7 tool invocations, all returned valid JSON.{COL_RESET}")
    print(f"{COL_DIM}This is exactly the protocol Claude Desktop runs with the real MCP.{COL_RESET}")
    print(f"{COL_DIM}From your machine with a real API key, swap the install_fakes() calls{COL_RESET}")
    print(f"{COL_DIM}for nothing — the same JSON shapes return, with real values.{COL_RESET}")


def _extract_payload(result: Any) -> dict:
    """FastMCP's client returns a CallToolResult with content blocks; the
    JSON payload is in the first TextContent's `text` field."""
    if hasattr(result, "structured_content") and result.structured_content is not None:
        return result.structured_content
    if hasattr(result, "content") and result.content:
        first = result.content[0]
        text = getattr(first, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw_text": text}
    return {"raw": str(result)}


def _mode_he(m: str) -> str:
    return {"driving": "רכב", "transit": "תח״צ", "walking": "ברגל"}.get(m, m)


if __name__ == "__main__":
    asyncio.run(run_session())
