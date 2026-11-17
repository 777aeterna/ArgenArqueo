"""Parámetros globales del pipeline.

Todo lo que afecta el resultado de una celda entra en ``PARAMS`` y por lo tanto
en ``param_hash()``: si cambia un parámetro, las celdas procesadas con el hash
viejo quedan marcadas como desactualizadas y se reprocesan.

REGLA DE VALIDACIÓN (módulo 4): ninguno de estos valores se ajusta para
maximizar el recall sobre sitios conocidos. Se fijan por razones físicas
(resolución, ruido, revisita) y se documenta la razón al lado.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("ARQUEO_ROOT", Path(__file__).resolve().parent.parent))
DATA = ROOT / "data"          # cachés, estado, insumos (ignorado por git)
OUT = ROOT / "outputs"        # productos con coordenadas (ignorado por git)
DB_PATH = Path(os.environ.get("ARQUEO_DB", DATA / "state.sqlite"))
CANDIDATES_GPKG = OUT / "candidates.gpkg"

STAC_EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
STAC_PLANETARY = "https://planetarycomputer.microsoft.com/api/stac/v1"
OVERPASS_URL = os.environ.get("ARQUEO_OVERPASS", "https://overpass-api.de/api/interpreter")
OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
WORLDCOVER_URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
                  "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif")
HANSEN_URL = ("https://storage.googleapis.com/earthenginepartners-hansen/GFC-2023-v1.11/"
              "Hansen_GFC-2023-v1.11_treecover2000_{tile}.tif")

# Hilos de cómputo por worker (no afecta resultados: fuera de PARAMS y del hash)
THREADS = int(os.environ.get("ARQUEO_THREADS", 4))

PARAMS: dict = {
    # --- Grilla ---
    "cell_km": 10,
    # margen leído alrededor de la celda para que el fondo local en los bordes
    # tenga contexto; se recorta al final. 1 km cubre las escalas <=1.5 km;
    # la escala de 5 km queda con efecto de borde atenuado (documentado).
    "margin_m": 1000,
    "res_m": 10,
    # --- Sentinel-2 ---
    "s2_start": "2019-01-01",
    "s2_end": "2025-12-31",           # 7 años (pedido: 6+)
    "s2_max_tile_cloud": 80,           # filtro grueso; el fino es SCL por píxel
    "s2_min_valid_frac": 0.15,         # fechas con <15% de píxeles útiles se descartan
    # SCL: 4 vegetación, 5 suelo desnudo. 6 agua se trata aparte (máscara de agua
    # permanente); 3 sombra, 8-10 nubes/cirros, 11 nieve, 0-2,7 dudosos: fuera.
    "scl_valid": [4, 5],
    "scl_water": [6],
    # --- Sentinel-1 RTC (Planetary Computer) ---
    "s1_max_scenes": 60,               # submuestreo temporal uniforme (costo)
    # --- Multiescala ---
    # ventanas de fondo local, en metros. TODAS corren siempre.
    "scales_m": [50, 150, 500, 1500, 5000],
    # umbral de excedencia por fecha (sigmas robustos)
    "k_sigma": 2.5,
    # umbral para formar componentes: z binomial de persistencia o |z| de compuesto
    "persist_z_thr": 6.0,
    "composite_z_thr": 4.0,
    "min_component_px": 2,
    # --- Bosque ---
    "forest_classes_worldcover": [10],  # Tree cover
    "hansen_treecover_thr": 60,        # % dosel => cerrado
    # --- Triaje ---
    "osm_buffer_m": 30,
    "rarity_contamination": "auto",
    "random_seed": 20261117,
    # --- Validación ---
    "validation_radius_m": 250,
    "validation_null_trials": 1000,
}

# Rasgos por fecha (persistencia) y compuestos temporales (un solo mapa)
PER_DATE_FEATURES = ["ndvi", "bright", "r1112", "r42"]
COMPOSITE_FEATURES = [
    "ndvi_med", "ndvi_amp", "ndvi_peak", "bright_med", "r1112_med", "r42_med",
    "s1vv_med", "s1vv_var", "s1vh_med", "s1vh_var",
]


def param_hash(extra: dict | None = None) -> str:
    p = dict(PARAMS)
    if extra:
        p.update(extra)
    blob = json.dumps(p, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:12]


def ensure_dirs() -> None:
    for d in (DATA, OUT, DATA / "cache", DATA / "osm", DATA / "validation",
              OUT / "cells", OUT / "chips", OUT / "export"):
        d.mkdir(parents=True, exist_ok=True)
