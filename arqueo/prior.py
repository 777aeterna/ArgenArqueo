"""MÓDULO 1 — Prior de PRESERVACIÓN. Solo ordena la cola; nunca excluye una celda.

No es similitud con sitios conocidos. Responde: ¿dónde algo antiguo pudo
sobrevivir y seguir siendo visible?

Capas (todas desde datos abiertos, a 30 m):
- Huella glacial del último máximo. Sin un vectorial de límites del UMG
  accesible, se aproxima con proxies geomorfológicos en el DEM: densidad de
  depresiones cerradas (kettles, lagos de sobreexcavación), relieve
  "hummocky" de 100-500 m (morrenas) y anisotropía coherente del relieve
  (campos de drumlins). Más huella glacial => menor preservación.
- Tefra: índice de exposición por distancia a los centros eruptivos
  holocenos de los Andes australes, con sesgo hacia el E-SE (vientos del
  oeste). Es un proxy grueso, documentado como tal.
- Sedimentación activa: fondos de valle (HAND bajo y pendiente baja),
  pie de escarpe (proxy de conos aluviales) y campos de dunas (textura
  anisótropa fina sobre suelo desnudo de WorldCover).
- Superficies estables: pendiente baja, proxy de pavimento (suelo desnudo
  o ralo en plano) y baja densidad de drenaje.
- T3: longitud de margen de lagunas (pedido explícito de priorización).

Cada métrica cruda se guarda en ``prior_json``; el prior final se calcula
normalizando por rango entre TODAS las celdas (``finalize_priors``).
"""
from __future__ import annotations

import json
import logging

import numpy as np
from scipy import ndimage

from . import config, stac, state
from .grid import Cell, bbox_lonlat

log = logging.getLogger(__name__)

# Centros eruptivos holocenos con dispersión de tefra sobre la Patagonia austral (lon, lat)
VOLCANOES = {
    "Hudson": (-72.97, -45.90), "Lautaro": (-73.55, -49.02), "Viedma": (-73.28, -49.36),
    "Aguilera": (-73.75, -50.33), "Reclus": (-73.58, -50.96), "Burney": (-73.40, -52.33),
    "Fueguino": (-71.97, -54.95),
}
RES = 30.0


def _slope_deg(dem):
    gy, gx = np.gradient(dem, RES)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def _fill_depressions(dem):
    from skimage.morphology import reconstruction
    seed = dem.copy()
    seed[1:-1, 1:-1] = np.nanmax(dem)
    return reconstruction(seed, dem, method="erosion")


def _flow(dem):
    """Acumulación de flujo D8 y HAND con pysheds (en memoria)."""
    if not hasattr(np, "in1d"):     # pysheds 0.5 usa np.in1d, removido en numpy 2.x
        np.in1d = np.isin
    from pysheds.sview import Raster, ViewFinder
    from pysheds.grid import Grid
    from affine import Affine
    vf = ViewFinder(affine=Affine(RES, 0, 0, 0, -RES, dem.shape[0] * RES), shape=dem.shape,
                    nodata=np.float64(-9999))
    r = Raster(np.where(np.isfinite(dem), dem, -9999).astype(np.float64), viewfinder=vf)
    g = Grid(viewfinder=vf)
    filled = g.fill_depressions(g.fill_pits(r))
    inflated = g.resolve_flats(filled)
    fdir = g.flowdir(inflated)
    acc = g.accumulation(fdir)
    channels = np.asarray(acc) * RES * RES >= 0.5e6     # >= 0.5 km² de cuenca
    hand = g.compute_hand(fdir, inflated, Raster(channels, viewfinder=fdir.viewfinder))
    return np.asarray(acc), channels, np.asarray(hand)


def _coherence(dem, sigma_px=5):
    """Coherencia del tensor de estructura del relieve filtrado (0 isótropo, 1 lineado)."""
    hp = dem - ndimage.gaussian_filter(dem, 10)
    gy, gx = np.gradient(hp)
    Jxx = ndimage.gaussian_filter(gx * gx, sigma_px)
    Jyy = ndimage.gaussian_filter(gy * gy, sigma_px)
    Jxy = ndimage.gaussian_filter(gx * gy, sigma_px)
    lam = np.sqrt((Jxx - Jyy) ** 2 + 4 * Jxy ** 2)
    return lam / (Jxx + Jyy + 1e-9)


def _haversine_km(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(np.radians, (lon1, lat1, lon2, lat2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * np.arcsin(np.sqrt(a))


def tephra_index(lon, lat, L_km=150.0):
    tot = 0.0
    for vlon, vlat in VOLCANOES.values():
        d = _haversine_km(vlon, vlat, lon, lat)
        bearing = np.degrees(np.arctan2(lon - vlon, lat - vlat)) % 360   # 0=N, 90=E
        # dispersión dominante hacia ~110° (E-SE) por los vientos del oeste
        directional = 0.5 * (1 + np.cos(np.radians(bearing - 110)))
        tot += np.exp(-d / L_km) * (0.25 + directional)
    return float(tot)


def cell_metrics(cell: Cell) -> dict:
    margin = 1500
    bounds = cell.bounds(margin)
    bl = bbox_lonlat(cell, margin)
    dem = stac.load_dem(bl, bounds, cell.epsg, RES)
    if dem is None:     # sin tiles de DEM: mar abierto
        return {"land_frac": 0.0, "tephra": tephra_index(cell.lon, cell.lat)}
    wc = stac.load_worldcover(bl, bounds, cell.epsg, RES)
    m = int(margin / RES)
    core = (slice(m, dem.shape[0] - m), slice(m, dem.shape[1] - m))
    sea = ~np.isfinite(dem) | (dem <= 0.5)
    if wc is not None:
        sea |= (wc == 80) & (dem < 5)
    land = ~sea
    out = {"land_frac": float(land[core].mean())}
    if land[core].mean() < 0.02:
        out.update(dict(glacial=np.nan, tephra=tephra_index(cell.lon, cell.lat)))
        return out
    d = np.where(land, dem, np.nan)
    dfill = np.where(np.isfinite(d), d, np.nanmin(d))
    slope = _slope_deg(dfill)
    # --- glacial
    filled = _fill_depressions(dfill)
    depth = filled - dfill
    dep = (depth > 1.5) & land
    lab, n = ndimage.label(dep)
    sizes = ndimage.sum_labels(dep, lab, np.arange(1, n + 1)) if n else np.array([])
    n_dep = int((sizes * RES * RES >= 5000).sum())
    hummock = np.nanstd((dfill - ndimage.gaussian_filter(dfill, 300 / RES / 2))[core][
        (slope[core] < 8) & land[core]]) if land[core].any() else np.nan
    coh = _coherence(dfill)
    km2 = land[core].sum() * RES * RES / 1e6
    out.update(
        dep_density_km2=n_dep / max(km2, 1e-3),
        dep_area_frac=float(dep[core].mean()),
        hummock_m=float(hummock),
        lineation=float(np.nanmean(coh[core][land[core]])),
    )
    # --- sedimentación activa y estabilidad
    try:
        acc, channels, hand = _flow(dfill)
        valley_floor = (hand < 3) & (slope < 1.5) & land
        out["drainage_density_km_km2"] = float(channels[core].sum() * RES / 1000 / max(km2, 1e-3))
        out["valley_floor_frac"] = float(valley_floor[core].mean())
    except Exception as e:
        log.warning("flujo falló en %s: %s", cell.cell_id, e)
        out["drainage_density_km_km2"] = np.nan
        out["valley_floor_frac"] = np.nan
    steep = slope > 15
    near_steep = ndimage.binary_dilation(steep, iterations=int(1000 / RES))
    out["piedmont_frac"] = float((near_steep & ~steep & (slope > 1) & (slope < 6) & land)[core].mean())
    if wc is not None:
        bare = np.isin(wc, [60])
        tex = np.abs(dfill - ndimage.gaussian_filter(dfill, 3))
        out["dune_frac"] = float((bare & (coh > 0.6) & (tex > 0.5) & land)[core].mean())
        out["pavement_frac"] = float((np.isin(wc, [60, 30]) & (slope < 3) & land)[core].mean())
        inland_water = (wc == 80) & (dem > 5)
        edge = inland_water ^ ndimage.binary_erosion(inland_water)
        out["lagoon_margin_km"] = float(edge[core].sum() * RES / 1000)
    out["low_slope_frac"] = float(((slope < 3) & land)[core].mean())
    out["tephra"] = tephra_index(cell.lon, cell.lat)
    return out


def _rank01(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, float)
    ok = np.isfinite(v)
    r = np.full(v.shape, 0.5)
    if ok.sum() > 1:
        from scipy.stats import rankdata
        r[ok] = (rankdata(v[ok]) - 1) / (ok.sum() - 1)
    return r


def finalize_priors(con) -> int:
    """Combina métricas crudas -> prior en [0,1], normalizando por rango entre celdas."""
    rows = con.execute("SELECT cell_id, tier, prior_json FROM cells WHERE prior_json IS NOT NULL").fetchall()
    if not rows:
        return 0
    M = [json.loads(r["prior_json"]) for r in rows]
    g = lambda k: np.array([m.get(k, np.nan) if m.get(k) is not None else np.nan for m in M], float)  # noqa
    glacial = np.nanmean(np.vstack([_rank01(g("dep_density_km2")), _rank01(g("hummock_m")),
                                    _rank01(g("lineation"))]), axis=0)
    tephra = _rank01(g("tephra"))
    active = np.nanmean(np.vstack([_rank01(g("valley_floor_frac")), _rank01(g("piedmont_frac")),
                                   _rank01(g("dune_frac"))]), axis=0)
    stable = np.nanmean(np.vstack([_rank01(g("low_slope_frac")), _rank01(g("pavement_frac")),
                                   1 - _rank01(g("drainage_density_km_km2"))]), axis=0)
    lagoon = _rank01(g("lagoon_margin_km"))
    land = g("land_frac")
    eps = 0.05
    terms = np.vstack([1 - glacial + eps, 1 - tephra + eps, 1 - active + eps, stable + eps])
    prior = np.exp(np.log(terms).mean(0))
    is_t3 = np.array([r["tier"] == "T3" for r in rows])
    prior = np.where(is_t3, prior * (0.75 + 0.5 * lagoon), prior)  # márgenes de lagunas primero
    prior = np.where(np.nan_to_num(land) < 0.02, 0.0, prior)        # mar: al final, no excluida
    for r, p, gl, te, ac, st, m in zip(rows, prior, glacial, tephra, active, stable, M):
        m.update(score_glacial=float(gl), score_tephra=float(te), score_active=float(ac),
                 score_stable=float(st))
        state.set_prior(con, r["cell_id"], float(p), m)
    return len(rows)


def compute_prior(con, cell: Cell) -> dict:
    m = cell_metrics(cell)
    state.set_prior(con, cell.cell_id, None, {k: (None if isinstance(v, float) and not np.isfinite(v) else v)
                                              for k, v in m.items()})
    return m
