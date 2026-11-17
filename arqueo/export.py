"""Export de candidatos. La clase RESTRINGIDA (posibles chenques) NUNCA sale.

El export se escribe en outputs/export/ (fuera del repo). Se valida dos veces
que no haya filas restringidas antes de escribir.
"""
from __future__ import annotations

import logging

from . import candidates as cstore
from . import config

log = logging.getLogger(__name__)

EXPORT_COLS = [
    "cand_id", "rank", "bucket", "literature_class", "rarity", "tier", "region", "cell_id",
    "scale_m", "n_scales", "scales_hit", "dominant_feature", "sign", "score_max", "persist_z",
    "persist_frac", "years_hit", "area_m2", "eccentricity", "compactness", "orientation_deg",
    "edge_straightness", "symmetry_mirror", "symmetry_rot180", "internal_cv", "hole_frac",
    "modern_flag", "modern_tags", "geometry",
]


def export(top: int | None = None, fmt: str = "gpkg", include_modern: bool = True) -> dict:
    g = cstore.read()
    if len(g) == 0:
        return {"written": 0}
    if "restricted" not in g:
        raise RuntimeError("Correr 'triage' antes de exportar: sin triaje no se sabe qué es restringido.")
    n_restricted = int(g["restricted"].astype(bool).sum())
    g = g[~g["restricted"].astype(bool)]
    if not include_modern:
        g = g[g["bucket"] != "moderno_historico"]
    g = g.sort_values("rank")
    if top:
        g = g.head(top)
    assert not g["restricted"].astype(bool).any(), "fila restringida en export"
    g = g[[c for c in EXPORT_COLS if c in g]]
    outdir = config.OUT / "export"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"candidatos_export.{ 'gpkg' if fmt == 'gpkg' else 'geojson'}"
    g.to_file(path, driver="GPKG" if fmt == "gpkg" else "GeoJSON")
    return {"written": len(g), "excluded_restricted": n_restricted, "path": str(path)}
