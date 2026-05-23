"""SQLite-backed RAG. Three tables, no embeddings.

Schema deliberately tiny. Adding a column requires a migration but the
shape isn't going to grow much — this isn't a CRM.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..models import ETAObservation, Place, SavedRoute, TransportMode


_SCHEMA = """
CREATE TABLE IF NOT EXISTS saved_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    origin_name TEXT NOT NULL,
    origin_lat REAL,
    origin_lng REAL,
    dest_name TEXT NOT NULL,
    dest_lat REAL,
    dest_lng REAL,
    mode TEXT NOT NULL,
    default_departure_local TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS eta_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_route_id INTEGER NOT NULL REFERENCES saved_routes(id) ON DELETE CASCADE,
    mode TEXT NOT NULL DEFAULT 'driving',
    observed_at TEXT NOT NULL,
    eta_s INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    hour INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_eta_route_bucket
    ON eta_observations (saved_route_id, mode, weekday, hour);

CREATE TABLE IF NOT EXISTS user_prefs (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # --- saved routes --------------------------------------------------

    def save_route(self, route: SavedRoute) -> int:
        with self.tx() as c:
            cur = c.execute(
                """
                INSERT INTO saved_routes
                    (name, origin_name, origin_lat, origin_lng,
                     dest_name, dest_lat, dest_lng, mode,
                     default_departure_local, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    origin_name=excluded.origin_name,
                    origin_lat=excluded.origin_lat,
                    origin_lng=excluded.origin_lng,
                    dest_name=excluded.dest_name,
                    dest_lat=excluded.dest_lat,
                    dest_lng=excluded.dest_lng,
                    mode=excluded.mode,
                    default_departure_local=excluded.default_departure_local,
                    notes=excluded.notes
                RETURNING id
                """,
                (
                    route.name,
                    route.origin.display_name,
                    route.origin.coords.lat if route.origin.coords else None,
                    route.origin.coords.lng if route.origin.coords else None,
                    route.destination.display_name,
                    route.destination.coords.lat if route.destination.coords else None,
                    route.destination.coords.lng if route.destination.coords else None,
                    route.mode.value,
                    route.default_departure_local,
                    route.notes,
                ),
            )
            row = cur.fetchone()
            return int(row[0])

    def list_routes(self) -> list[SavedRoute]:
        cur = self._conn.execute("SELECT * FROM saved_routes ORDER BY name")
        return [_row_to_saved_route(r) for r in cur.fetchall()]

    def get_route(self, name: str) -> SavedRoute | None:
        cur = self._conn.execute(
            "SELECT * FROM saved_routes WHERE name = ?", (name,)
        )
        row = cur.fetchone()
        return _row_to_saved_route(row) if row else None

    def delete_route(self, name: str) -> bool:
        with self.tx() as c:
            cur = c.execute("DELETE FROM saved_routes WHERE name = ?", (name,))
            return cur.rowcount > 0

    # --- eta observations ----------------------------------------------

    def record_eta(self, obs: ETAObservation, mode: str = "driving") -> None:
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO eta_observations
                    (saved_route_id, mode, observed_at, eta_s, weekday, hour)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    obs.saved_route_id,
                    mode,
                    obs.observed_at.isoformat(),
                    obs.eta_s,
                    obs.weekday,
                    obs.hour,
                ),
            )

    def bucket_observations(
        self,
        saved_route_id: int,
        weekday: int,
        hour: int,
        mode: str = "driving",
    ) -> list[int]:
        """Return raw eta_s samples for a (route, mode, weekday, hour) bucket."""
        cur = self._conn.execute(
            """
            SELECT eta_s FROM eta_observations
            WHERE saved_route_id = ? AND mode = ? AND weekday = ? AND hour = ?
            ORDER BY observed_at DESC
            LIMIT 200
            """,
            (saved_route_id, mode, weekday, hour),
        )
        return [int(r[0]) for r in cur.fetchall()]

    # --- prefs ---------------------------------------------------------

    def set_pref(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO user_prefs (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def get_pref(self, key: str) -> str | None:
        cur = self._conn.execute(
            "SELECT value FROM user_prefs WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def _row_to_saved_route(row: sqlite3.Row) -> SavedRoute:
    from ..models import LatLng

    origin_coords = (
        LatLng(lat=row["origin_lat"], lng=row["origin_lng"])
        if row["origin_lat"] is not None and row["origin_lng"] is not None
        else None
    )
    dest_coords = (
        LatLng(lat=row["dest_lat"], lng=row["dest_lng"])
        if row["dest_lat"] is not None and row["dest_lng"] is not None
        else None
    )
    return SavedRoute(
        id=int(row["id"]),
        name=row["name"],
        origin=Place(display_name=row["origin_name"], coords=origin_coords),
        destination=Place(display_name=row["dest_name"], coords=dest_coords),
        mode=TransportMode(row["mode"]),
        default_departure_local=row["default_departure_local"],
        notes=row["notes"] or "",
    )


def open_store(path: Path) -> Store:
    """Open the SQLite store at `path`, creating parent dirs + schema as
    needed. Idempotent — safe to call on every server startup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return Store(conn)
