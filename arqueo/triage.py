"""MÓDULO 3 — Rareza y triaje.

1. Supresión de lo moderno: coincidencia con OSM => se MARCA, no se elimina.
2. Rareza: Isolation Forest + Mahalanobis robusta (MCD) en el espacio de
   descriptores, contra la población de anomalías del PROPIO paisaje (región
   del tier; si es chica, el tier entero). Lo estadísticamente raro sube.
3. Tres buckets, en este orden de ranking:
     0 "NO_CLASIFICABLE"           <- primero
     1 "morfologia_descrita"       (clases descritas en la bibliografía regional)
     2 "moderno_historico"         (coincide con infraestructura OSM)
   Dentro de cada bucket, por rareza.

Las reglas de "morfología descrita" son ETIQUETAS de triaje, no detectores:
la detección ya ocurrió sin plantilla en el módulo 2. Una anomalía que no
encaja en ninguna regla NO se descarta: va al bucket que se ranquea primero.

Clase RESTRINGIDA: posibles chenques (entierros). Se marcan con
``restricted=True``, quedan en el archivo local y NUNCA salen en un export.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from sklearn.covariance import MinCovDet
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

from . import candidates as cstore
from . import config, modern
from .grid import parse_cell_id
from .state import connect, row_to_cell

log = logging.getLogger(__name__)

BUCKET_ORDER = {"NO_CLASIFICABLE": 0, "morfologia_descrita": 1, "moderno_historico": 2}

RARITY_FEATURES = [
    "log_area", "eccentricity", "compactness", "solidity", "extent", "edge_straightness",
    "symmetry_mirror", "symmetry_rot180", "internal_cv", "hole_frac", "score_max",
    "persist_frac", "years_hit", "n_scales", "log_scale", "sign", "orient_cos2", "orient_sin2",
    "z_ndvi_med", "z_ndvi_amp", "z_bright_med", "z_r1112_med", "z_r42_med", "z_s1vv_med",
    "z_s1vv_var",
]


# Morfologías descritas en la bibliografía arqueológica de Santa Cruz y Tierra del
# Fuego, expresadas como condiciones gruesas sobre descriptores ya calculados.
# Orden = prioridad de asignación. 'restricted' = no sale nunca en exports.
def _literature_rules(d: pd.DataFrame) -> list[tuple[str, pd.Series, bool]]:
    a = d["area_m2"]
    bright_pos = d.get("z_bright_med", pd.Series(0, index=d.index)).fillna(0) > 1.0
    ndvi_neg = d.get("z_ndvi_med", pd.Series(0, index=d.index)).fillna(0) < -1.0
    near_water = d.get("dist_water_m", pd.Series(1e6, index=d.index)).fillna(1e6) < 1500
    compact = d["compactness"].fillna(0) > 0.5
    return [
        # chenque: estructura de piedra pequeña y compacta (a 10 m apenas 1-6 px).
        # Conservador a propósito: ante la duda se restringe.
        ("posible_chenque_o_estructura_de_piedra", (a <= 600) & compact & (bright_pos | (d["sign"] > 0)), True),
        # conchero/basural costero: claro, poco vegetado, cerca de agua.
        ("conchero_costero", (a > 600) & (a <= 50_000) & bright_pos & near_water, False),
        # hoyada de deflación (blowout) con material expuesto
        ("hoyada_de_deflacion", (a > 5_000) & (a <= 500_000) & bright_pos & ndvi_neg
         & (d["compactness"].fillna(0) > 0.3), False),
        # rastrillada / senda: muy elongada
        ("rastrillada_o_senda", (d["eccentricity"].fillna(0) > 0.985) & (d["major_m"] > 300), False),
        # pirca / corral de piedra: bordes rectos y forma rectangular
        ("pirca_o_recinto_rectilineo", (d["edge_straightness"].fillna(0) > 0.7)
         & (d["extent"].fillna(0) > 0.7) & (a > 600) & (a < 40_000), False),
    ]


def _prepare(d: pd.DataFrame) -> pd.DataFrame:
    X = pd.DataFrame(index=d.index)
    X["log_area"] = np.log10(d["area_m2"].clip(lower=1))
    X["log_scale"] = np.log10(d["scale_m"])
    o = np.radians(d["orientation_deg"].fillna(0))
    X["orient_cos2"] = np.cos(2 * o)
    X["orient_sin2"] = np.sin(2 * o)
    for c in RARITY_FEATURES:
        if c not in X:
            X[c] = d[c] if c in d else 0.0
    X = X[RARITY_FEATURES].astype(float)
    return X.fillna(X.median()).fillna(0)


def rarity(d: pd.DataFrame, seed: int | None = None) -> pd.Series:
    """Rareza en [0,1] (rango promedio de IsolationForest y Mahalanobis robusta)."""
    seed = seed if seed is not None else config.PARAMS["random_seed"]
    if len(d) < 20:
        return pd.Series(0.5, index=d.index)
    X = RobustScaler().fit_transform(_prepare(d))
    X = np.clip(X, -20, 20)
    iso = IsolationForest(n_estimators=300, contamination=config.PARAMS["rarity_contamination"],
                          random_state=seed).fit(X)
    s_iso = -iso.score_samples(X)
    rng = np.random.default_rng(seed)
    sub = X[rng.choice(len(X), min(len(X), 5000), replace=False)]
    try:
        mcd = MinCovDet(random_state=seed, support_fraction=0.75).fit(sub)
        s_mah = mcd.mahalanobis(X)
    except Exception:
        s_mah = ((X - np.median(X, 0)) ** 2).sum(1)
    r = (pd.Series(s_iso).rank(pct=True).values + pd.Series(s_mah).rank(pct=True).values) / 2
    return pd.Series(r, index=d.index)


def triage(refresh_osm: bool = False, min_population: int = 200) -> pd.DataFrame:
    g = cstore.read()
    if len(g) == 0:
        log.warning("No hay candidatos.")
        return g
    con = connect()
    cells = {r["cell_id"]: row_to_cell(r) for r in con.execute("SELECT * FROM cells").fetchall()}
    # 1) moderno (por celda, con caché de Overpass)
    parts = []
    for cid, sub in g.groupby("cell_id"):
        cell = cells.get(cid)
        epsg = cell.epsg if cell else parse_cell_id(cid)[0]
        try:
            osm = modern.fetch_cell(cell, refresh=refresh_osm) if cell else None
            sub = modern.flag_modern(sub, osm, epsg)
            sub["osm_status"] = "ok"
        except Exception as e:
            log.warning("OSM no disponible para %s: %s (queda sin marcar)", cid, e)
            sub = sub.copy()
            sub["modern_flag"] = False
            sub["modern_tags"] = ""
            sub["modern_overlap"] = 0.0
            sub["osm_status"] = "unavailable"
        parts.append(sub)
    g = pd.concat(parts)
    # 2) rareza contra el propio paisaje
    g["rarity"] = np.nan
    g["rarity_population"] = ""
    for (tier, region), sub in g.groupby(["tier", "region"]):
        pop = sub if len(sub) >= min_population else g[g["tier"] == tier]
        r = rarity(pop)
        g.loc[sub.index, "rarity"] = r.loc[sub.index]
        g.loc[sub.index, "rarity_population"] = region if len(sub) >= min_population else tier
    # 3) buckets
    g["bucket"] = "NO_CLASIFICABLE"
    g["literature_class"] = ""
    g["restricted"] = False
    for name, cond, restricted in _literature_rules(g):
        sel = cond.fillna(False) & (g["literature_class"] == "")
        g.loc[sel, "literature_class"] = name
        g.loc[sel, "bucket"] = "morfologia_descrita"
        if restricted:
            g.loc[sel, "restricted"] = True
    mod = g["modern_flag"] & (g["modern_overlap"] >= 0.2)
    g.loc[mod, "bucket"] = "moderno_historico"     # marcado; la etiqueta bibliográfica se conserva
    g["bucket_order"] = g["bucket"].map(BUCKET_ORDER)
    g = g.sort_values(["bucket_order", "rarity"], ascending=[True, False])
    g["rank"] = np.arange(1, len(g) + 1)
    cstore.save_all(g)
    return g
