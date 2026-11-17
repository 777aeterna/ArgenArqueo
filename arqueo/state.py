"""Estado del barrido en SQLite: una fila por celda.

Idempotente y reanudable: una celda con status ``done`` y el mismo
``param_hash`` no se reprocesa. Paralelizable: cada worker reclama celdas con
una transacción ``BEGIN IMMEDIATE`` (lock de escritura), de modo que dos
workers nunca toman la misma. Las celdas ``running`` con lease vencido (worker
muerto) vuelven a la cola.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config
from .grid import Cell

SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
    cell_id     TEXT PRIMARY KEY,
    tier        TEXT NOT NULL,
    region      TEXT,
    epsg        INTEGER NOT NULL,
    xmin REAL, ymin REAL, xmax REAL, ymax REAL,
    lon REAL, lat REAL,
    scan_order  INTEGER,
    prior       REAL,              -- módulo 1: solo ordena la cola
    prior_json  TEXT,
    forest_frac REAL,              -- fracción de bosque cerrado (no evaluable sin LiDAR)
    water_frac  REAL,
    status      TEXT NOT NULL DEFAULT 'pending',
        -- pending | running | done | error | water | forest_gap
    param_hash  TEXT,
    updated_at  REAL,
    worker      TEXT,
    lease_until REAL,
    n_candidates INTEGER,
    timings_json TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_cells_status ON cells(status);
CREATE INDEX IF NOT EXISTS ix_cells_tier ON cells(tier, scan_order);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.executescript(SCHEMA)
    return con


@contextmanager
def tx(con: sqlite3.Connection):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def upsert_cells(con: sqlite3.Connection, cells: list[Cell]) -> int:
    n = 0
    with tx(con):
        for i, c in enumerate(cells):
            cur = con.execute(
                """INSERT INTO cells(cell_id,tier,region,epsg,xmin,ymin,xmax,ymax,lon,lat,scan_order,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cell_id) DO UPDATE SET scan_order=excluded.scan_order""",
                (c.cell_id, c.tier, c.region, c.epsg, c.xmin, c.ymin, c.xmax, c.ymax,
                 c.lon, c.lat, i, time.time()))
            n += cur.rowcount
    return n


def row_to_cell(r: sqlite3.Row) -> Cell:
    return Cell(r["cell_id"], r["tier"], r["region"], r["epsg"], r["xmin"], r["ymin"],
                r["xmax"], r["ymax"], r["lon"], r["lat"])


def claim(con: sqlite3.Connection, worker: str, phash: str, tier: str | None = None,
          lease_s: float = 6 * 3600, cell_ids: list[str] | None = None) -> Cell | None:
    """Reclama atómicamente la próxima celda. Orden: prior desc, luego sur->norte."""
    now = time.time()
    with tx(con):
        q = ["SELECT * FROM cells WHERE ("
             "status IN ('pending','error') "
             "OR (status='done' AND (param_hash IS NULL OR param_hash != ?)) "
             "OR (status='running' AND lease_until < ?))"]
        args: list = [phash, now]
        if tier:
            q.append("AND tier = ?")
            args.append(tier)
        if cell_ids:
            q.append(f"AND cell_id IN ({','.join('?' * len(cell_ids))})")
            args += cell_ids
        # el prior ordena la cola; sin prior calculado, orden de barrido S->N
        q.append("ORDER BY CASE tier WHEN 'T1' THEN 1 WHEN 'T2' THEN 2 WHEN 'T3' THEN 3 ELSE 4 END,"
                 " COALESCE(prior, -1) DESC, scan_order ASC LIMIT 1")
        r = con.execute(" ".join(q), args).fetchone()
        if r is None:
            return None
        con.execute("UPDATE cells SET status='running', worker=?, lease_until=?, updated_at=? "
                    "WHERE cell_id=?", (worker, now + lease_s, now, r["cell_id"]))
        return row_to_cell(r)


def finish(con: sqlite3.Connection, cell_id: str, status: str, phash: str,
           timings: dict | None = None, n_candidates: int | None = None,
           error: str | None = None, **extra) -> None:
    sets = ["status=?", "param_hash=?", "updated_at=?", "lease_until=NULL",
            "timings_json=?", "n_candidates=?", "error=?"]
    args = [status, phash, time.time(), json.dumps(timings or {}), n_candidates, error]
    for k, v in extra.items():
        sets.append(f"{k}=?")
        args.append(v)
    with tx(con):
        con.execute(f"UPDATE cells SET {', '.join(sets)} WHERE cell_id=?", args + [cell_id])


def set_prior(con: sqlite3.Connection, cell_id: str, prior: float, detail: dict) -> None:
    with tx(con):
        con.execute("UPDATE cells SET prior=?, prior_json=? WHERE cell_id=?",
                    (prior, json.dumps(detail), cell_id))


def summary(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT tier, status, COUNT(*) n, SUM(n_candidates) cands FROM cells "
        "GROUP BY tier, status ORDER BY tier, status").fetchall()
