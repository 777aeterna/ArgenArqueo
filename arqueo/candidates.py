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


def cell_path(cell_id: str):
    return config.OUT / "cells" / cell_id / "candidates.gpkg"


def write(cell, result: dict) -> int:
    """Escribe los candidatos de UNA celda en su propio GeoPackage (seguro con varios
    workers en paralelo; idempotente: reemplaza el archivo de la celda)."""
    g = to_gdf(cell, result)
    path = cell_path(cell.cell_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    if len(g):
        tmp = path.with_suffix(".tmp.gpkg")
        g.to_file(tmp, layer=LAYER, driver="GPKG")
        tmp.replace(path)
    return len(g)


def merge_all(cell_ids: list[str], path=None) -> int:
    """Une los GeoPackages por celda (solo celdas 'done') en el archivo maestro."""
    path = path or config.CANDIDATES_GPKG
    parts = [gpd.read_file(cell_path(c), layer=LAYER) for c in cell_ids if cell_path(c).exists()]
    if not parts:
        return 0
    g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=4326)
    save_all(g, path)
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
