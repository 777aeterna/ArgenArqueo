"""Supresión de lo moderno vía OpenStreetMap (Overpass).

NO elimina candidatos: los MARCA como histórico-reciente con la etiqueta OSM
que coincide. Un corral del siglo XX puede estar encima de algo anterior.
Las respuestas se cachean por celda en data/osm/ (contienen coordenadas).
"""
from __future__ import annotations

import json
import logging
import time

import geopandas as gpd
import requests
from shapely.geometry import LineString, Point, Polygon

from . import config
from .grid import Cell, bbox_lonlat

log = logging.getLogger(__name__)

# Qué se considera infraestructura moderna o histórico-reciente
QUERY_TAGS = [
    'way["highway"]', 'way["railway"]', 'way["waterway"~"canal|ditch|drain"]',
    'way["building"]', 'node["building"]', 'way["barrier"]', 'node["man_made"]',
    'way["man_made"]', 'way["landuse"~"farmyard|industrial|residential|quarry|landfill|military|cemetery"]',
    'node["place"~"farm|isolated_dwelling|hamlet|village|locality"]', 'way["power"]',
    'way["pipeline"]', 'node["man_made"="petroleum_well"]', 'way["aeroway"]', 'way["amenity"]',
    'node["historic"]', 'way["historic"]',
]


def _overpass(bbox_ll, retries=4):
    s, w, n, e = bbox_ll[1], bbox_ll[0], bbox_ll[3], bbox_ll[2]
    body = "".join(f"{t}({s},{w},{n},{e});" for t in QUERY_TAGS)
    q = f"[out:json][timeout:120];({body});out geom;"
    urls = [config.OVERPASS_URL] + [u for u in config.__dict__.get("OVERPASS_MIRRORS", [])]
    last = None
    for i in range(retries):
        for url in urls:
            try:
                r = requests.post(url, data={"data": q}, timeout=180,
                                  headers={"User-Agent": "arqueo-patagonia/0.1"})
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}"
            except Exception as ex:
                last = str(ex)
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"Overpass no respondió: {last}")


def _classify(tags: dict) -> str:
    for k in ("highway", "railway", "waterway", "building", "barrier", "landuse", "place",
              "power", "pipeline", "aeroway", "amenity", "historic", "man_made"):
        if k in tags:
            return f"{k}={tags[k]}"
    return "osm"


def fetch_cell(cell: Cell, refresh: bool = False) -> gpd.GeoDataFrame:
    cache = config.DATA / "osm" / f"{cell.cell_id}.json"
    if cache.exists() and not refresh:
        data = json.loads(cache.read_text())
    else:
        data = _overpass(bbox_lonlat(cell, 500))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
    rows = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        try:
            if el["type"] == "node":
                g = Point(el["lon"], el["lat"])
            elif "geometry" in el:
                xy = [(p["lon"], p["lat"]) for p in el["geometry"]]
                g = Polygon(xy) if len(xy) >= 4 and xy[0] == xy[-1] else LineString(xy)
            else:
                continue
        except Exception:
            continue
        rows.append(dict(osm_id=f"{el['type']}/{el['id']}", osm_class=_classify(tags), geometry=g))
    if not rows:
        return gpd.GeoDataFrame(columns=["osm_id", "osm_class", "geometry"], geometry="geometry", crs=4326)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)


def flag_modern(cands: gpd.GeoDataFrame, osm: gpd.GeoDataFrame, epsg: int,
                buffer_m: float | None = None) -> gpd.GeoDataFrame:
    """Agrega columnas modern_flag (bool) y modern_tags (str). No borra filas."""
    buffer_m = buffer_m if buffer_m is not None else config.PARAMS["osm_buffer_m"]
    cands = cands.copy()
    cands["modern_flag"] = False
    cands["modern_tags"] = ""
    cands["modern_overlap"] = 0.0
    if osm is None or len(osm) == 0 or len(cands) == 0:
        return cands
    c = cands.to_crs(epsg)
    o = osm.to_crs(epsg)
    o["geometry"] = o.geometry.buffer(buffer_m)
    j = gpd.sjoin(c[["geometry"]].reset_index(), o[["osm_class", "geometry"]], predicate="intersects")
    if len(j) == 0:
        return cands
    tags = j.groupby("index")["osm_class"].apply(lambda s: ";".join(sorted(set(s))[:6]))
    union = o.geometry.union_all()
    ov = c.loc[tags.index].geometry.apply(lambda g: g.intersection(union).area / max(g.area, 1e-6))
    cands.loc[tags.index, "modern_flag"] = True
    cands.loc[tags.index, "modern_tags"] = tags.values
    cands.loc[tags.index, "modern_overlap"] = ov.values
    return cands
