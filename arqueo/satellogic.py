"""Satellogic EarthView (S3 público, 1 m, 2022): recortes SOLO sobre los mejores candidatos.

Estructura del bucket: data/tif/zone=19S/region={E}_{N}/date=YYYY-MM-DD/*_VISUAL.tif,
con regiones de 384 x 384 m en UTM. Para no listar zonas enteras se lista por
prefijo de easting (3 dígitos = franjas de 1 km) alrededor del punto.
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET

import numpy as np
import rasterio
import requests
from rasterio.windows import from_bounds

from . import config
from .stac import GDAL_ENV

log = logging.getLogger(__name__)
BUCKET = "https://satellogic-earthview.s3.us-west-2.amazonaws.com"
TILE = 384.0
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def _list(prefix: str, delimiter="/") -> tuple[list[str], list[str]]:
    keys, prefixes, token = [], [], None
    while True:
        params = {"list-type": "2", "prefix": prefix, "delimiter": delimiter, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        r = requests.get(BUCKET + "/", params=params, timeout=60)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        keys += [e.text for e in root.findall("s3:Contents/s3:Key", NS)]
        prefixes += [e.text for e in root.findall("s3:CommonPrefixes/s3:Prefix", NS)]
        if root.findtext("s3:IsTruncated", namespaces=NS) != "true":
            return keys, prefixes
        token = root.findtext("s3:NextContinuationToken", namespaces=NS)


def _regions_near(zone: str, e: float, n: float) -> list[tuple[int, int, str]]:
    cache = config.DATA / "satellogic" / f"zone={zone}.json"
    idx = json.loads(cache.read_text()) if cache.exists() else {}
    out = []
    for e3 in {int(e // 1000) - 1, int(e // 1000), int(e // 1000) + 1}:
        key = str(e3)
        if key not in idx:
            _, prefs = _list(f"data/tif/zone={zone}/region={e3}")
            idx[key] = prefs
        for p in idx[key]:
            m = re.search(r"region=(\d+)_(\d+)/", p)
            if m:
                out.append((int(m.group(1)), int(m.group(2)), p))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(idx))
    return out


def crop(epsg: int, x: float, y: float, half_m: float = 150.0):
    """Devuelve (rgb uint8 HxWx3, meta) del recorte VISUAL más cercano, o None."""
    zone = f"{epsg - 32700}S" if epsg > 32700 else f"{epsg - 32600}N"
    cands = _regions_near(zone, x, y)
    # el nombre de región puede ser esquina inferior o superior: se prueba por cercanía
    cands.sort(key=lambda t: (t[0] + TILE / 2 - x) ** 2 + (t[1] - y) ** 2)
    for e0, n0, pref in cands[:6]:
        keys, dates = _list(pref)
        for dp in sorted(dates):
            ks, _ = _list(dp, delimiter="")
            vis = [k for k in ks if k.endswith("_VISUAL.tif")]
            if not vis:
                continue
            url = f"{BUCKET}/{vis[0]}"
            with rasterio.Env(**GDAL_ENV):
                with rasterio.open(url) as src:
                    b = src.bounds
                    if not (b.left <= x <= b.right and b.bottom <= y <= b.top):
                        break
                    w = from_bounds(x - half_m, y - half_m, x + half_m, y + half_m, src.transform)
                    a = src.read([1, 2, 3], window=w, boundless=True, fill_value=0)
            return np.moveaxis(a, 0, -1), dict(url=url, date=dp.rstrip("/").split("=")[-1])
    return None
