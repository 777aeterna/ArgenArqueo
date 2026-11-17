"""Teselado en celdas de 10x10 km, ordenadas de sur a norte, en cuatro tiers.

Cada celda vive en la zona UTM (hemisferio sur) que contiene su centro, alineada
a múltiplos de 10 km en esa proyección (estilo MGRS). Así el procesamiento es
en metros, sin reproyecciones ad-hoc, y los ids son estables:

    cell_id = "{zona}S_{easting_km}_{northing_km}"   p.ej. "19S_510_4060"

Las áreas de cada tier son polígonos lon/lat GRUESOS (editables abajo). Las
celdas que caen íntegramente en el mar se detectan al procesar (máscara de
agua) y quedan con status ``water``: no se borran del índice.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon, box
from shapely.ops import transform as shp_transform

# (nombre, lon_min, lat_min, lon_max, lat_max). Aproximaciones de trabajo.
TIERS: dict[str, list[tuple[str, float, float, float, float]]] = {
    "T1": [  # Costas de Tierra del Fuego
        ("canal_beagle", -68.62, -55.08, -66.75, -54.72),
        ("peninsula_mitre", -66.75, -55.08, -65.05, -54.45),
        ("bahia_san_sebastian", -68.75, -53.45, -68.05, -52.62),
        ("costa_rio_grande", -68.30, -54.45, -66.70, -53.45),
    ],
    "T2": [  # Costa atlántica de Santa Cruz
        ("deseado_cabo_blanco", -66.40, -48.10, -65.70, -47.00),
        ("san_julian", -68.10, -49.60, -67.45, -48.90),
        ("monte_leon", -69.30, -50.45, -68.60, -50.00),
    ],
    "T3": [  # Mesetas basálticas de Santa Cruz
        ("meseta_strobel", -71.70, -48.90, -70.55, -48.10),
        ("meseta_lago_buenos_aires", -71.40, -47.25, -70.30, -46.55),
        ("meseta_central", -69.60, -48.60, -68.00, -47.50),
    ],
    "T4": [  # Escarpes y cañadones (ranking de segmentos de paredón)
        ("rio_pinturas", -70.95, -47.40, -70.40, -46.90),
        ("canadon_rio_deseado", -69.50, -47.50, -67.40, -46.95),
        ("la_maria_canadones", -69.20, -48.65, -68.55, -48.20),
        ("rio_chico_santa_cruz", -70.30, -49.90, -68.90, -49.40),
    ],
}
TIER_ORDER = ["T1", "T2", "T3", "T4"]


@dataclass(frozen=True)
class Cell:
    cell_id: str
    tier: str
    region: str
    epsg: int
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    lon: float
    lat: float

    def bounds(self, margin: float = 0.0) -> tuple[float, float, float, float]:
        return (self.xmin - margin, self.ymin - margin, self.xmax + margin, self.ymax + margin)


def utm_epsg_for(lon: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return 32700 + zone


def parse_cell_id(cell_id: str) -> tuple[int, float, float]:
    z, e, n = cell_id.split("_")
    return 32700 + int(z[:-1]), float(e) * 1000, float(n) * 1000


def cells_for_tiers(tiers: list[str] | None = None, cell_km: int = 10) -> list[Cell]:
    size = cell_km * 1000.0
    out: dict[str, Cell] = {}
    for tier in (tiers or TIER_ORDER):
        for region, x0, y0, x1, y1 in TIERS[tier]:
            ll = box(x0, y0, x1, y1)
            zones = sorted({utm_epsg_for(x) for x in np.linspace(x0, x1, 8)})
            for epsg in zones:
                fwd = Transformer.from_crs(4326, epsg, always_xy=True).transform
                inv = Transformer.from_crs(epsg, 4326, always_xy=True).transform
                poly = shp_transform(fwd, ll)
                bx0, by0, bx1, by1 = poly.bounds
                for xs in np.arange(np.floor(bx0 / size) * size, bx1, size):
                    for ys in np.arange(np.floor(by0 / size) * size, by1, size):
                        cpoly = box(xs, ys, xs + size, ys + size)
                        if not cpoly.intersects(poly):
                            continue
                        cx, cy = xs + size / 2, ys + size / 2
                        lon, lat = inv(cx, cy)
                        if utm_epsg_for(lon) != epsg:   # la celda pertenece a otra zona
                            continue
                        cid = f"{epsg - 32700}S_{int(xs // 1000)}_{int(ys // 1000)}"
                        if cid in out:                   # ya asignada a un tier anterior
                            continue
                        out[cid] = Cell(cid, tier, region, epsg, float(xs), float(ys), float(xs + size), float(ys + size),
                                        float(lon), float(lat))
    # Orden de barrido: tier, y dentro del tier de sur a norte (latitud ascendente)
    return sorted(out.values(), key=lambda c: (TIER_ORDER.index(c.tier), c.lat, c.lon))


def cell_polygon_lonlat(c: Cell) -> Polygon:
    inv = Transformer.from_crs(c.epsg, 4326, always_xy=True).transform
    return shp_transform(inv, box(c.xmin, c.ymin, c.xmax, c.ymax))


def bbox_lonlat(c: Cell, margin: float = 0.0) -> tuple[float, float, float, float]:
    inv = Transformer.from_crs(c.epsg, 4326, always_xy=True).transform
    return shp_transform(inv, box(*c.bounds(margin))).bounds
