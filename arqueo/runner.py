"""Ejecución de celdas: workers en paralelo que reclaman celdas del estado SQLite."""
from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import socket
import traceback

from . import anomaly, candidates, config, state

log = logging.getLogger(__name__)


def _worker(wid: int, tier: str | None, cell_ids: list[str] | None, max_dates: int | None) -> int:
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [w{wid}] %(message)s")
    for name in ("rasterio", "rasterio._env", "botocore"):
        logging.getLogger(name).setLevel(logging.ERROR)
    con = state.connect()
    phash = config.param_hash()
    worker = f"{socket.gethostname()}:{os.getpid()}"
    n = 0
    while True:
        cell = state.claim(con, worker, phash, tier=tier, cell_ids=cell_ids)
        if cell is None:
            return n
        log.info("celda %s (%s/%s)", cell.cell_id, cell.tier, cell.region)
        try:
            r = anomaly.run_cell(cell, max_dates=max_dates)
            nc = candidates.write(cell, r) if r["status"] == "done" else 0
            timings = dict(r.get("timings", {}))
            timings.update({k: r[k] for k in ("n_dates", "n_dates_catalog", "s1") if k in r})
            state.finish(con, cell.cell_id, r["status"], phash, timings, nc, r.get("error"),
                         forest_frac=r.get("forest_frac"), water_frac=r.get("water_frac"))
            log.info("celda %s -> %s, %d candidatos, %.0f s", cell.cell_id, r["status"], nc,
                     timings.get("total", 0))
        except Exception as e:
            log.error("celda %s falló: %s", cell.cell_id, e)
            state.finish(con, cell.cell_id, "error", phash, error=traceback.format_exc()[-2000:])
        n += 1


def run(tier: str | None = None, workers: int = 1, limit: int | None = None,
        cells: list[str] | None = None, max_dates: int | None = None, force: bool = False) -> int:
    config.ensure_dirs()
    con = state.connect()
    if force and cells:
        with state.tx(con):
            con.executemany("UPDATE cells SET status='pending' WHERE cell_id=?", [(c,) for c in cells])
    phash = config.param_hash()
    if limit and not cells:
        q = ("SELECT cell_id FROM cells WHERE (status IN ('pending','error') OR "
             "(status='done' AND (param_hash IS NULL OR param_hash != ?)))")
        args = [phash]
        if tier:
            q += " AND tier=?"
            args.append(tier)
        q += (" ORDER BY CASE tier WHEN 'T1' THEN 1 WHEN 'T2' THEN 2 WHEN 'T3' THEN 3 ELSE 4 END,"
              " COALESCE(prior,-1) DESC, scan_order LIMIT ?")
        cells = [r[0] for r in con.execute(q, args + [limit]).fetchall()]
    if workers <= 1:
        n = _worker(0, tier, cells, max_dates)
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers) as pool:
            n = sum(pool.starmap(_worker, [(i, tier, cells, max_dates) for i in range(workers)]))
    done = [r[0] for r in con.execute("SELECT cell_id FROM cells WHERE status='done'").fetchall()]
    total = candidates.merge_all(done)
    log.info("procesadas %d celdas; %d candidatos en el GeoPackage maestro", n, total)
    return n
