"""CLI: init-grid, prior, rank, run, status, triage, validate, chips, export."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import config, state


def _setup_logging(v: bool):
    logging.basicConfig(level=logging.DEBUG if v else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    for name in ("rasterio", "rasterio._env", "botocore", "urllib3", "fiona", "pyogrio"):
        logging.getLogger(name).setLevel(logging.ERROR)


def cmd_init_grid(a):
    from .grid import cells_for_tiers
    cells = cells_for_tiers(a.tiers)
    con = state.connect()
    state.upsert_cells(con, cells)
    from collections import Counter
    print(f"{len(cells)} celdas registradas:", dict(Counter(c.tier for c in cells)))


def _cells(con, tier=None, limit=None, where=""):
    q = "SELECT * FROM cells WHERE 1=1 " + where
    args = []
    if tier:
        q += " AND tier=?"
        args.append(tier)
    q += " ORDER BY scan_order"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [state.row_to_cell(r) for r in con.execute(q, args).fetchall()]


def cmd_prior(a):
    from concurrent.futures import ProcessPoolExecutor
    from . import prior
    con = state.connect()
    todo = _cells(con, a.tier, a.limit, "" if a.force else "AND (prior_json IS NULL OR prior_json LIKE '%\"error\"%')")
    print(f"prior: {len(todo)} celdas")
    if a.workers > 1:
        with ProcessPoolExecutor(a.workers) as ex:
            for cid, m in ex.map(_prior_worker, todo):
                state.set_prior(con, cid, None, m)
    else:
        for c in todo:
            cid, m = _prior_worker(c)
            state.set_prior(con, cid, None, m)
    n = prior.finalize_priors(con)
    print(f"prior normalizado para {n} celdas")


def _prior_worker(c):
    import math
    from . import prior
    try:
        m = prior.cell_metrics(c)
    except Exception as e:  # el prior nunca excluye: si falla, la celda queda neutra
        m = {"error": str(e)}
    return c.cell_id, {k: (None if isinstance(v, float) and not math.isfinite(v) else
                           (float(v) if hasattr(v, "item") else v)) for k, v in m.items()}


def cmd_rank(a):
    con = state.connect()
    if a.escarpments:
        import geopandas as gpd
        import pandas as pd
        from . import escarpments
        parts = []
        for c in _cells(con, "T4", a.limit):
            g = escarpments.rank_cell(c)
            if len(g):
                parts.append(g)
            print(c.cell_id, len(g), "segmentos")
        if parts:
            g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326)
            g = g.sort_values("shelter_score", ascending=False)
            g["rank"] = range(1, len(g) + 1)
            config.ensure_dirs()
            g.to_file(config.CANDIDATES_GPKG.with_name("escarpments.gpkg"), layer="escarpments",
                      driver="GPKG")
            print(f"{len(g)} segmentos de paredón ranqueados -> outputs/escarpments.gpkg")
        return
    rows = con.execute(
        "SELECT cell_id, tier, region, round(lat,3) lat, round(prior,3) prior, status FROM cells "
        "ORDER BY CASE tier WHEN 'T1' THEN 1 WHEN 'T2' THEN 2 WHEN 'T3' THEN 3 ELSE 4 END, "
        "COALESCE(prior,-1) DESC, scan_order LIMIT ?", (a.limit or 30,)).fetchall()
    print("cola de procesamiento (tier, prior desc, sur->norte):")
    for r in rows:
        print(dict(r))


def cmd_run(a):
    from . import runner
    runner.run(a.tier, a.workers, a.limit, a.cells, a.max_dates, a.force)


def cmd_status(a):
    con = state.connect()
    print(f"param_hash actual: {config.param_hash()}")
    for r in state.summary(con):
        print(f"{r['tier']:3s} {r['status']:11s} {r['n']:5d} celdas  {r['cands'] or 0:7d} candidatos")
    if a.timings:
        for r in con.execute("SELECT cell_id, timings_json FROM cells WHERE status='done'"):
            print(r["cell_id"], r["timings_json"])


def cmd_triage(a):
    from . import triage
    g = triage.triage(refresh_osm=a.refresh_osm)
    if len(g):
        print(g["bucket"].value_counts().to_string())
        print("restringidos:", int(g["restricted"].sum()))


def cmd_validate(a):
    from . import validate
    print(json.dumps(validate.validate(radius=a.radius, trials=a.trials), indent=2, ensure_ascii=False))


def cmd_chips(a):
    from . import chips
    r = chips.make_chips(a.top, not a.no_satellogic)
    print(f"{len(r)} fichas en outputs/chips/ ({sum(x['restricted'] for x in r)} restringidas)")


def cmd_export(a):
    from . import export
    print(export.export(a.top, a.format, not a.no_modern))


def cmd_demo(a):
    from pathlib import Path
    from . import demo
    print(json.dumps(demo.build(Path(a.dest), a.top, a.chips), indent=1, ensure_ascii=False, default=int))


def main(argv=None):
    p = argparse.ArgumentParser("arqueo", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("init-grid"); s.add_argument("--tiers", nargs="*"); s.set_defaults(f=cmd_init_grid)
    s = sp.add_parser("prior"); s.add_argument("--tier"); s.add_argument("--workers", type=int, default=1)
    s.add_argument("--limit", type=int); s.add_argument("--force", action="store_true"); s.set_defaults(f=cmd_prior)
    s = sp.add_parser("rank"); s.add_argument("--limit", type=int)
    s.add_argument("--escarpments", action="store_true", help="T4: ranquear segmentos de paredón")
    s.set_defaults(f=cmd_rank)
    s = sp.add_parser("run"); s.add_argument("--tier"); s.add_argument("--workers", type=int, default=1)
    s.add_argument("--limit", type=int); s.add_argument("--cells", nargs="*")
    s.add_argument("--max-dates", type=int, help="solo pruebas de costo: submuestrea fechas")
    s.add_argument("--force", action="store_true", help="reprocesa --cells aunque estén al día")
    s.set_defaults(f=cmd_run)
    s = sp.add_parser("status"); s.add_argument("--timings", action="store_true"); s.set_defaults(f=cmd_status)
    s = sp.add_parser("triage"); s.add_argument("--refresh-osm", action="store_true"); s.set_defaults(f=cmd_triage)
    s = sp.add_parser("validate"); s.add_argument("--radius", type=float); s.add_argument("--trials", type=int)
    s.set_defaults(f=cmd_validate)
    s = sp.add_parser("chips"); s.add_argument("--top", type=int, default=50)
    s.add_argument("--no-satellogic", action="store_true"); s.set_defaults(f=cmd_chips)
    s = sp.add_parser("export"); s.add_argument("--top", type=int)
    s.add_argument("--format", choices=["gpkg", "geojson"], default="gpkg")
    s.add_argument("--no-modern", action="store_true"); s.set_defaults(f=cmd_export)
    s = sp.add_parser("demo", help="demo pública sin georreferencia (sin restringidos)")
    s.add_argument("--dest", default="ejemplo"); s.add_argument("--top", type=int, default=500)
    s.add_argument("--chips", type=int, default=30); s.set_defaults(f=cmd_demo)
    a = p.parse_args(argv)
    _setup_logging(a.verbose)
    config.ensure_dirs()
    a.f(a)


if __name__ == "__main__":
    sys.exit(main())
