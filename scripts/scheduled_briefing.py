"""Scheduled briefing — runs best_way on every configured route and prints
a concise Hebrew summary. Optional push to Telegram and / or SMTP email.

Designed to be called from cron / launchd / systemd-timer at the times
the user actually leaves the house. Always prints to stdout (so cron
log capture keeps working); additionally delivers via every configured
channel:

  - Telegram bot     — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  - SMTP email       — set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_TO

Channels are independent: if Telegram is configured and email isn't,
only Telegram fires. If a delivery fails, the others still try, and
the stdout output is preserved.

Usage:
    python scripts/scheduled_briefing.py <route_name>[,<route_name>...] [--avoid-tolls] [--dry-run]

Required env vars (always):
    GOOGLE_MAPS_API_KEY        — same as the MCP server
    ISRAEL_TRANSIT_STORE_DIR   — same store the MCP writes to

Optional env vars (for delivery channels):
    TELEGRAM_BOT_TOKEN         — from @BotFather
    TELEGRAM_CHAT_ID           — your chat id with the bot (use @userinfobot)
    SMTP_HOST, SMTP_PORT       — e.g. smtp.gmail.com, 587
    SMTP_USER, SMTP_PASSWORD   — your SMTP credentials (Gmail: use an App Password)
    SMTP_FROM                  — default: SMTP_USER
    SMTP_TO                    — recipient
    SMTP_USE_TLS               — default: 1 (set to "0" to disable STARTTLS)

Example launchd plist embeds the env vars in <EnvironmentVariables>;
example crontab lines use a wrapper script that `source ~/.commute-env`
before running this script.

  30 9  * * 0-4 . ~/.commute-env && python scripts/scheduled_briefing.py home->work,shilat->work --avoid-tolls
   0 10 * * 0-4 . ~/.commute-env && python scripts/scheduled_briefing.py home->work,shilat->work --avoid-tolls
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from io import StringIO
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


# --- delivery channels ----------------------------------------------------


def _telegram_enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _smtp_enabled() -> bool:
    return bool(
        os.environ.get("SMTP_HOST")
        and os.environ.get("SMTP_USER")
        and os.environ.get("SMTP_PASSWORD")
        and os.environ.get("SMTP_TO")
    )


def deliver_to_telegram(body: str) -> str:
    """POST the briefing to Telegram. Returns a one-line status string."""
    import httpx  # imported lazily so the script runs without the dep

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    # Telegram message cap = 4096 chars. Trim with a marker rather than fail.
    text = body
    if len(text) > 3900:
        text = text[:3900] + "\n…(נחתך בשל מגבלת טלגרם 4096)"
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
            timeout=15.0,
        )
        if resp.status_code == 200:
            return "Telegram: OK"
        return f"Telegram: HTTP {resp.status_code} — {resp.text[:200]}"
    except httpx.HTTPError as e:
        return f"Telegram: {type(e).__name__}: {e}"


def deliver_to_smtp(body: str, subject: str) -> str:
    """Send the briefing via SMTP. Returns a one-line status string."""
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("SMTP_FROM", user)
    recipient = os.environ["SMTP_TO"]
    use_tls = os.environ.get("SMTP_USE_TLS", "1") not in {"0", "false", "False", ""}

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")

    try:
        with smtplib.SMTP(host, port, timeout=15) as srv:
            srv.ehlo()
            if use_tls:
                srv.starttls()
                srv.ehlo()
            srv.login(user, password)
            srv.send_message(msg)
        return f"SMTP: OK → {recipient}"
    except Exception as e:
        return f"SMTP: {type(e).__name__}: {e}"


# --- briefing rendering ---------------------------------------------------


def _payload(result: Any) -> dict:
    if hasattr(result, "structured_content") and result.structured_content is not None:
        return result.structured_content
    if hasattr(result, "content") and result.content:
        text = getattr(result.content[0], "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw_text": text}
    return {"raw": str(result)}


def _mode_he(m: str) -> str:
    return {"driving": "🚗 רכב", "transit": "🚌 תח״צ", "walking": "🚶 ברגל"}.get(m, m)


def _render_route(name: str, payload: dict, out) -> None:
    out.write(f"\n--- {name} ---\n")
    if not payload.get("ok"):
        out.write(f"  שגיאה: {payload.get('error', 'לא ידוע')}\n")
        return
    rec = payload.get("recommendation", "")
    out.write(f"  ► {rec}\n")
    win = payload["winner"]
    win_mode = _mode_he(win["mode"])
    out.write(f"    {win_mode}: {win['total_duration_min']} דק׳"
              f"  ({win['summary'][:60]})\n")
    for alt in payload.get("alternatives", []):
        alt_mode = _mode_he(alt["mode"])
        out.write(f"    {alt_mode}: {alt['total_duration_min']} דק׳"
                  f"  ({alt['summary'][:60]})\n")
    matched = win.get("matched_disruptions", [])
    if matched:
        out.write(f"\n  ⚠️  {len(matched)} דיווח/ים על המסלול:\n")
        for d in matched[:3]:
            out.write(f"     [{d['kind']}] {d['title']}  ({d['source']})\n")
    baselines = payload.get("baselines", {})
    for mode, b in baselines.items():
        n = b.get("sample_size", 0)
        if n >= 5:
            anomaly = " ⚡ חריג" if b.get("is_anomalous") else ""
            out.write(f"  בייסליין {_mode_he(mode)}: p50={b['p50_min']}m "
                      f"p75={b['p75_min']}m היום={b['today_min']}m"
                      f"  ({n} דגימות){anomaly}\n")


# --- main flow ------------------------------------------------------------


async def build_briefing(route_names: list[str], avoid_tolls: bool) -> str:
    """Run best_way for each route and return the full text body."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    out = StringIO()
    now = datetime.now().astimezone()
    out.write(f"=== בריפינג {now.strftime('%H:%M  %a %d/%m/%Y')} ===\n")
    if avoid_tolls:
        out.write("(ללא כבישי אגרה)\n")

    transport = StdioTransport(
        command="israel-transit-mcp",
        args=[],
        env=os.environ.copy(),
    )

    async with Client(transport) as client:
        for name in route_names:
            try:
                result = await client.call_tool("best_way", {
                    "name": name,
                    "modes": ["driving", "transit"],
                    "avoid_tolls": avoid_tolls,
                    "record_observation": True,
                })
                _render_route(name, _payload(result), out)
            except Exception as e:
                out.write(f"\n--- {name} ---\n  שגיאה: {type(e).__name__}: {e}\n")
    return out.getvalue()


async def run(route_names: list[str], avoid_tolls: bool, dry_run: bool) -> int:
    if not os.environ.get("GOOGLE_MAPS_API_KEY"):
        print("ERROR: GOOGLE_MAPS_API_KEY not set", file=sys.stderr)
        return 2

    body = await build_briefing(route_names, avoid_tolls)
    print(body)

    if dry_run:
        print("\n[dry-run] לא שולח Telegram/SMTP", file=sys.stderr)
        if _telegram_enabled():
            print("  Telegram: היה נשלח", file=sys.stderr)
        if _smtp_enabled():
            print(f"  SMTP: היה נשלח אל {os.environ.get('SMTP_TO')}", file=sys.stderr)
        return 0

    subject_time = datetime.now().astimezone().strftime("%H:%M %d/%m")
    subject = f"בריפינג בוקר · {subject_time}"

    statuses: list[str] = []
    if _telegram_enabled():
        statuses.append(deliver_to_telegram(body))
    if _smtp_enabled():
        statuses.append(deliver_to_smtp(body, subject))
    if not statuses:
        print("\n[delivery] לא הוגדר טלגרם/SMTP — רק stdout", file=sys.stderr)
    for s in statuses:
        print(f"[delivery] {s}", file=sys.stderr)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scheduled morning commute briefing")
    parser.add_argument(
        "routes",
        help="Comma-separated saved route names (e.g. home->work,shilat->work)",
    )
    parser.add_argument(
        "--avoid-tolls",
        action="store_true",
        help="Exclude toll roads (כביש 6 etc.)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the briefing and which channels would fire — do not deliver.",
    )
    args = parser.parse_args()
    route_names = [r.strip() for r in args.routes.split(",") if r.strip()]
    if not route_names:
        print("ERROR: no routes provided", file=sys.stderr)
        return 2
    return asyncio.run(run(route_names, args.avoid_tolls, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
