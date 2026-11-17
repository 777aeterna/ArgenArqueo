"""T4 — Ranking de segmentos de paredón por probabilidad de alero o cueva.

Esto NO es detección de sitios: es un prior físico sobre dónde una pared de
roca puede ofrecer reparo. Términos, todos desde el DEM y WorldCover:
- altura de pared (relieve local a través del escarpe),
- orientación: reparo de los vientos dominantes del O-SO (paredes que miran
  al E-NE quedan a sotavento) y exposición solar al N en el hemisferio sur,
- distancia a agua (lagunas/ríos de WorldCover y red de drenaje del DEM),
- litología: sin un mapa geológico abierto accesible por ventana, se usa un
  proxy de "roca de coronamiento" (borde de meseta: tope plano + caída
  abrupta, típico de coladas basálticas e ignimbritas). Se reporta como proxy.

Salida: capa ``escarpments`` del GeoPackage local.
"""
from __future__ import annotations

import numpy as np
import geopandas as gpd
from scipy import ndimage
from shapely.geometry import LineString

from . import stac
from .grid import Cell, bbox_lonlat
from .prior import RES, _flow, _slope_deg

SEG_LEN_M = 300.0


def _aspect_deg(dem):
    gy, gx = np.gradient(dem, RES)
    # filas crecen hacia el sur: la pendiente hacia abajo apunta a (-gx, +gy) en (E, N)
    return (np.degrees(np.arctan2(-gx, gy)) + 360) % 360


def rank_cell(cell: Cell) -> gpd.GeoDataFrame:
    from skimage.morphology import skeletonize
    margin = 600
    bounds = cell.bounds(margin)
    bl = bbox_lonlat(cell, margin)
    dem = stac.load_dem(bl, bounds, cell.epsg, RES)
    wc = stac.load_worldcover(bl, bounds, cell.epsg, RES)
    d = np.where(np.isfinite(dem), dem, np.nanmin(dem))
    slope = _slope_deg(d)
    aspect = _aspect_deg(d)
    relief = ndimage.maximum_filter(d, 11) - ndimage.minimum_filter(d, 11)   # ±150 m
    steep = (slope > 25) & (relief > 8)
    steep = ndimage.binary_opening(steep, iterations=1)
    if steep.sum() < 3:
        return gpd.GeoDataFrame()
    skel = skeletonize(steep)
    water = np.zeros(d.shape, bool)
    if wc is not None:
        water |= wc == 80
    try:
        _, channels, _ = _flow(d)
        water |= channels
    except Exception:
        pass
    dist_water = ndimage.distance_transform_edt(~water) * RES if water.any() else np.full(d.shape, 1e5)
    # proxy de coronamiento: tope plano (pendiente < 4°) a <=150 m y >=5 m por encima
    flat_top = ndimage.maximum_filter(np.where(slope < 4, d, -1e9), 11) >= d + 5
    lab, n = ndimage.label(skel, structure=np.ones((3, 3)))
    rows = []
    x0, y0 = bounds[0], bounds[3]
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        rr, cc = np.nonzero(lab[sl] == i)
        rr = rr + sl[0].start
        cc = cc + sl[1].start
        if len(rr) < 3:
            continue
        # trocear en segmentos de ~300 m por proyección sobre el eje principal
        pts = np.c_[cc, rr].astype(float)
        u = pts - pts.mean(0)
        ax = np.linalg.svd(u, full_matrices=False)[2][0]
        t = u @ ax
        order = np.argsort(t)
        nseg = max(1, int(np.ceil((t.max() - t.min()) * RES / SEG_LEN_M)))
        for chunk in np.array_split(order, nseg):
            if len(chunk) < 3:
                continue
            r_, c_ = rr[chunk], cc[chunk]
            h = float(np.median(relief[r_, c_]))
            asp = float(np.degrees(np.angle(np.mean(np.exp(1j * np.radians(aspect[r_, c_]))))) % 360)
            lee = 0.5 * (1 + np.cos(np.radians(asp - 67.5)))       # mira al ENE: sotavento
            sun = 0.5 * (1 + np.cos(np.radians(asp - 0)))          # mira al N: sol austral
            dw = float(np.median(dist_water[r_, c_]))
            cap = float(flat_top[r_, c_].mean())
            if not (cell.xmin <= x0 + (c_.mean() + .5) * RES < cell.xmax and
                    cell.ymin <= y0 - (r_.mean() + .5) * RES < cell.ymax):
                continue
            o = np.argsort((pts[chunk] - pts[chunk].mean(0)) @ ax)
            xy = [(x0 + (c_[j] + .5) * RES, y0 - (r_[j] + .5) * RES) for j in o]
            geom = LineString(xy) if len(xy) > 1 else None
            score = (np.clip(h / 40, 0, 1) ** 0.7) * (0.4 + 0.4 * lee + 0.2 * sun) * \
                np.exp(-dw / 2000) ** 0.5 * (0.6 + 0.4 * cap)
            rows.append(dict(cell_id=cell.cell_id, wall_height_m=h, aspect_deg=asp, leeward=lee,
                             sun=sun, dist_water_m=dw, caprock_proxy=cap, length_m=len(chunk) * RES,
                             shelter_score=float(score), geometry=geom))
    if not rows:
        return gpd.GeoDataFrame()
    g = gpd.GeoDataFrame(rows, geometry="geometry", crs=f"EPSG:{cell.epsg}").to_crs(4326)
    return g.sort_values("shelter_score", ascending=False)
