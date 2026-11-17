"""MÓDULO 4 — Validación como CONTROL DE CORDURA, no como objetivo.

Set de referencia: sitios ya publicados (bibliografía CONICET / CADIC para
Santa Cruz y Tierra del Fuego), cargados a mano por el usuario en
``data/validation/sitios_publicados.csv`` con la cita de cada fila
(ver ``config/sitios_publicados.template.csv``). Ese archivo tiene
coordenadas: vive en data/ y nunca entra al repo.

Métricas (números crudos):
- recall@N: fracción de sitios (dentro de celdas procesadas) con algún
  candidato del top-N a <= radio + precisión declarada del sitio.
- Modelo nulo: las MISMAS geometrías del top-N trasladadas a posiciones al
  azar sobre tierra evaluable de las celdas procesadas, eligiendo la celda
  con probabilidad proporcional al MISMO prior. Preserva tamaños y prior.
- lift = recall / media(recall nulo); p = P(nulo >= observado).

PROHIBIDO ajustar hiperparámetros para maximizar este recall. Este módulo no
escribe nada en config: solo mide. Si el recall es 0 en todo N, el pipeline
está roto y hay que arreglarlo; más allá de eso no se toca.
"""
from __future__ import annotations

import json
import logging

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from shapely.affinity import translate

from . import candidates as cstore
from . import config
from .state import connect, row_to_cell

log = logging.getLogger(__name__)

SITES_CSV = config.DATA / "validation" / "sitios_publicados.csv"
REQUIRED = ["site_id", "lat", "lon", "precision_m", "reference"]


def load_sites(path=None) -> gpd.GeoDataFrame:
    path = path or SITES_CSV
    if not path.exists():
        raise FileNotFoundError(
            f"No existe {path}. Copiá config/sitios_publicados.template.csv, cargá sitios "
            "publicados con su referencia bibliográfica y volvé a correr.")
    df = pd.read_csv(path, comment="#")
    miss = [c for c in REQUIRED if c not in df]
    if miss:
        raise ValueError(f"Faltan columnas {miss} en {path}")
    if df["reference"].isna().any():
        raise ValueError("Cada sitio debe citar su referencia publicada (columna 'reference').")
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lon, df.lat), crs=4326)


def _land_sampler(cell_id: str):
    d = config.OUT / "cells" / cell_id
    try:
        with rasterio.open(d / "water_freq.tif") as src:
            wf = src.read(1)
            tr = src.transform
        with rasterio.open(d / "forest_mask.tif") as src:
            fm = src.read(1)
    except Exception:
        return None
    ok = np.isfinite(wf) & (wf < 0.5) & (fm != 1)
    rr, cc = np.nonzero(ok)
    return (rr, cc, tr) if len(rr) else None


def recall_at(cands_m: gpd.GeoDataFrame, sites_m: gpd.GeoDataFrame, radius: float) -> float:
    if len(sites_m) == 0 or len(cands_m) == 0:
        return 0.0
    buf = sites_m.copy()
    buf["geometry"] = sites_m.geometry.buffer(radius + sites_m["precision_m"].fillna(0))
    j = gpd.sjoin(buf[["site_id", "geometry"]], cands_m[["geometry"]], predicate="intersects")
    return j["site_id"].nunique() / len(sites_m)


def validate(Ns=(10, 50, 100, 500, 1000, 5000), radius=None, trials=None, seed=None) -> dict:
    radius = radius if radius is not None else config.PARAMS["validation_radius_m"]
    trials = trials if trials is not None else config.PARAMS["validation_null_trials"]
    rng = np.random.default_rng(seed if seed is not None else config.PARAMS["random_seed"])
    con = connect()
    done = {r["cell_id"]: row_to_cell(r) for r in
            con.execute("SELECT * FROM cells WHERE status='done'").fetchall()}
    priors = {r["cell_id"]: (r["prior"] if r["prior"] is not None else 1.0) for r in
              con.execute("SELECT cell_id, prior FROM cells WHERE status='done'").fetchall()}
    cands = cstore.read()
    sites = load_sites()
    if not done or len(cands) == 0:
        raise RuntimeError("No hay celdas procesadas o candidatos: correr run + triage primero.")
    sort_cols = ["rank"] if "rank" in cands else ["score_max"]
    cands = cands.sort_values(sort_cols, ascending="rank" in cands)
    # trabajar por zona UTM para medir en metros
    epsgs = sorted({c.epsg for c in done.values()})
    report = {"radius_m": radius, "trials": trials, "n_cells_done": len(done), "results": []}
    # sitios dentro de celdas procesadas
    from .grid import cell_polygon_lonlat
    polys = gpd.GeoDataFrame({"cell_id": list(done)}, geometry=[cell_polygon_lonlat(c) for c in done.values()], crs=4326)
    s_in = gpd.sjoin(sites, polys, predicate="within")
    report["n_sites_total"] = int(len(sites))
    report["n_sites_in_processed_cells"] = int(len(s_in))
    if len(s_in) == 0:
        report["warning"] = "Ningún sitio de referencia cae en celdas procesadas."
        return report
    samplers = {cid: _land_sampler(cid) for cid in done}
    cell_ids = [c for c in done if samplers[c] is not None]
    w = np.array([max(priors[c], 1e-3) for c in cell_ids], float)
    w /= w.sum()
    for N in Ns:
        top = cands.head(N)
        rec_obs, rec_null = [], []
        nsites = 0
        for epsg in epsgs:
            s_e = s_in[s_in["cell_id"].map(lambda c: done[c].epsg) == epsg].to_crs(epsg)
            if len(s_e) == 0:
                continue
            nsites += len(s_e)
            top_e = top.to_crs(epsg)
            rec_obs.append(recall_at(top_e, s_e, radius) * len(s_e))
            null_vals = []
            cent = top_e.geometry.centroid
            ce_ids = [c for c in cell_ids if done[c].epsg == epsg]
            if not ce_ids:
                continue
            we = np.array([w[cell_ids.index(c)] for c in ce_ids]); we /= we.sum()
            for _ in range(trials):
                chosen = rng.choice(len(ce_ids), size=len(top_e), p=we)
                geoms = []
                for g, c0, k in zip(top_e.geometry, cent, chosen):
                    rr, cc, tr = samplers[ce_ids[k]]
                    i = rng.integers(len(rr))
                    x, y = tr * (cc[i] + 0.5, rr[i] + 0.5)
                    geoms.append(translate(g, x - c0.x, y - c0.y))
                null_vals.append(recall_at(gpd.GeoDataFrame(geometry=geoms, crs=epsg), s_e, radius) * len(s_e))
            rec_null.append(np.array(null_vals))
        obs = sum(rec_obs) / nsites
        nul = (np.sum(rec_null, axis=0) / nsites) if rec_null else np.zeros(trials)
        report["results"].append(dict(
            N=int(N), n_candidates_used=int(len(top)), recall=round(float(obs), 4),
            null_mean=round(float(nul.mean()), 4), null_p95=round(float(np.percentile(nul, 95)), 4),
            lift=(round(float(obs / nul.mean()), 3) if nul.mean() > 0 else None),
            p_value=round(float((np.sum(nul >= obs) + 1) / (len(nul) + 1)), 4),
            sites_hit=int(round(obs * nsites)), n_sites=int(nsites)))
    if all(r["recall"] == 0 for r in report["results"]):
        report["verdict"] = ("ROTO: no recupera NINGÚN sitio ya mapeado. Revisar carga de datos, "
                             "máscaras y georreferenciación (no 'tunear' umbrales).")
    else:
        report["verdict"] = "Control de cordura superado (recupera algo). No ajustar contra estos números."
    out = config.OUT / "validation_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report
