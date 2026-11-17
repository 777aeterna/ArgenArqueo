"""Persistencia de candidatos en el GeoPackage local (con coordenadas: nunca al repo)."""
from __future__ import annotations

import json

import geopandas as gpd
import pandas as pd

from . import config

LAYER = "candidates"


def to_gdf(cell, result: dict) -> gpd.GeoDataFrame:
    rows = result.get("candidates") or []
    if not rows:
        return gpd.GeoDataFrame()
    df = pd.DataFrame(rows)
    g = gpd.GeoDataFrame(df.drop(columns=["geometry"]), geometry=df["geometry"], crs=f"EPSG:{cell.epsg}")
    g["cand_id"] = [f"{cell.cell_id}_{int(s)}m_{int(l)}" for s, l in zip(g["scale_m"], g["label"])]
    g["tier"] = cell.tier
    g["region"] = cell.region
    g["epsg"] = cell.epsg
    g["param_hash"] = config.param_hash()
    return g.to_crs(4326)


def write(cell, result: dict, path=None) -> int:
    """Reemplaza las filas de la celda (idempotente) en el GeoPackage."""
    path = path or config.CANDIDATES_GPKG
    path.parent.mkdir(parents=True, exist_ok=True)
    g = to_gdf(cell, result)
    old = read(path)
    if len(old):
        old = old[old["cell_id"] != cell.cell_id]
    if len(g) == 0 and len(old) == 0:
        return 0
    allg = pd.concat([old, g], ignore_index=True) if len(old) else g
    allg = gpd.GeoDataFrame(allg, geometry="geometry", crs=4326)
    tmp = path.with_suffix(".tmp.gpkg")
    allg.to_file(tmp, layer=LAYER, driver="GPKG")
    tmp.replace(path)
    return len(g)


def read(path=None) -> gpd.GeoDataFrame:
    path = path or config.CANDIDATES_GPKG
    if not path.exists():
        return gpd.GeoDataFrame()
    return gpd.read_file(path, layer=LAYER)


def save_all(gdf: gpd.GeoDataFrame, path=None) -> None:
    path = path or config.CANDIDATES_GPKG
    tmp = path.with_suffix(".tmp.gpkg")
    gdf.to_file(tmp, layer=LAYER, driver="GPKG")
    tmp.replace(path)
