"""Fichas de candidatos: recortes multiescala, mapas de score por escala, serie
temporal del píxel, descriptores y scores. Opcional: recorte Satellogic 1 m.

Salida local: outputs/chips/<cand_id>.png + .json. Los candidatos de clase
RESTRINGIDA van a outputs/chips/RESTRINGIDO/ y nunca se exportan.
"""
from __future__ import annotations

import json
import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.windows import from_bounds  # noqa: E402

from . import candidates as cstore  # noqa: E402
from . import config  # noqa: E402

log = logging.getLogger(__name__)
WINDOWS_M = [500, 1500, 5000]


def _read_win(path, x, y, half):
    with rasterio.open(path) as src:
        w = from_bounds(x - half, y - half, x + half, y + half, src.transform)
        a = src.read(window=w, boundless=True, fill_value=np.nan if src.dtypes[0].startswith("float") else 0)
    return a


def _rgb(a):
    a = np.moveaxis(np.nan_to_num(a.astype(np.float32)), 0, -1)
    lo, hi = np.nanpercentile(a, [2, 98]) if np.isfinite(a).any() else (0, 1)
    return np.clip((a - lo) / max(hi - lo, 1e-6), 0, 1)


def make_chip(row, with_satellogic=False) -> dict:
    cid = row["cell_id"]
    d = config.OUT / "cells" / cid
    x, y = float(row["x"]), float(row["y"])
    epsg = int(row["epsg"])
    geom = row.geometry
    try:
        import geopandas as gpd
        gl = gpd.GeoSeries([geom], crs=4326).to_crs(epsg).iloc[0]
    except Exception:
        gl = None
    restricted = bool(row.get("restricted", False))
    outdir = config.OUT / "chips" / ("RESTRINGIDO" if restricted else "")
    outdir.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(16, 10))
    gs = fig.add_gridspec(3, 6)
    # recortes RGB multiescala
    for i, wm in enumerate(WINDOWS_M):
        ax = fig.add_subplot(gs[0, i])
        if (d / "rgb_median.tif").exists():
            a = _read_win(d / "rgb_median.tif", x, y, wm / 2)
            ax.imshow(_rgb(a), extent=(x - wm / 2, x + wm / 2, y - wm / 2, y + wm / 2))
        if gl is not None:
            for g in getattr(gl, "geoms", [gl]):
                xs, ys = g.exterior.xy
                ax.plot(xs, ys, "y-", lw=0.8)
        ax.set_xlim(x - wm / 2, x + wm / 2); ax.set_ylim(y - wm / 2, y + wm / 2)
        ax.set_title(f"S2 mediana {wm} m", fontsize=8); ax.set_xticks([]); ax.set_yticks([])
    # Satellogic
    ax = fig.add_subplot(gs[0, 3])
    sat_meta = None
    if with_satellogic:
        try:
            from . import satellogic
            r = satellogic.crop(epsg, x, y, 150)
            if r is not None:
                ax.imshow(r[0]); sat_meta = r[1]
                ax.set_title(f"Satellogic 1 m {r[1]['date']}", fontsize=8)
        except Exception as e:
            log.info("Satellogic no disponible: %s", e)
    if sat_meta is None:
        ax.text(0.5, 0.5, "Satellogic: sin cobertura", ha="center", va="center", fontsize=8)
    ax.set_xticks([]); ax.set_yticks([])
    # mapas de score por escala (ventana 1.5 km)
    for i, sm in enumerate(config.PARAMS["scales_m"]):
        ax = fig.add_subplot(gs[1, i])
        p = d / f"score_{sm}m.tif"
        if p.exists():
            a = _read_win(p, x, y, 750)[0]
            ax.imshow(a, vmin=0, vmax=3, cmap="magma", extent=(x - 750, x + 750, y - 750, y + 750))
        ax.set_title(f"score {sm} m", fontsize=8); ax.set_xticks([]); ax.set_yticks([])
    # serie temporal del píxel pico
    ax = fig.add_subplot(gs[2, :4])
    ts_path = d / "timeseries.npz"
    if ts_path.exists():
        ts = np.load(ts_path)
        idx = np.where((ts["rows"] == int(row["peak_row"])) & (ts["cols"] == int(row["peak_col"])))[0]
        if len(idx):
            dates = np.array(ts["dates"], dtype="datetime64[D]")
            for f in config.PER_DATE_FEATURES:
                v = ts[f][:, idx[0]]
                ok = np.isfinite(v)
                ax.plot(dates[ok], v[ok], ".", ms=3, label=f)
            ax.legend(fontsize=7, ncol=4)
    ax.set_title("serie temporal del píxel pico", fontsize=8)
    # descriptores
    ax = fig.add_subplot(gs[1:, 4:])
    ax.axis("off")
    keys = ["cand_id", "rank", "bucket", "literature_class", "restricted", "rarity", "scale_m",
            "n_scales", "scales_hit", "dominant_feature", "sign", "score_max", "persist_z",
            "persist_frac", "years_hit", "area_m2", "eccentricity", "compactness", "orientation_deg",
            "edge_straightness", "symmetry_mirror", "symmetry_rot180", "internal_cv", "hole_frac",
            "modern_tags", "dist_water_m"]
    txt = "\n".join(f"{k}: {row[k]:.3g}" if isinstance(row.get(k), float) else f"{k}: {row.get(k)}"
                    for k in keys if k in row)
    ax.text(0, 1, txt, va="top", family="monospace", fontsize=7)
    fig.suptitle(f"{row['cand_id']}  {'[RESTRINGIDO]' if restricted else ''}", fontsize=10)
    fig.tight_layout()
    png = outdir / f"{row['cand_id']}.png"
    fig.savefig(png, dpi=110)
    plt.close(fig)
    meta = {k: (row[k] if not isinstance(row[k], (np.floating, np.integer)) else row[k].item())
            for k in keys if k in row}
    meta["satellogic"] = sat_meta
    (outdir / f"{row['cand_id']}.json").write_text(json.dumps(meta, default=str, indent=1))
    return {"png": str(png), "restricted": restricted}


def make_chips(top: int = 50, with_satellogic: bool = True) -> list[dict]:
    g = cstore.read()
    if len(g) == 0:
        return []
    if "rank" in g:
        g = g.sort_values("rank")
    out = []
    for _, row in g.head(top).iterrows():
        try:
            out.append(make_chip(row, with_satellogic))
        except Exception as e:
            log.warning("ficha %s falló: %s", row.get("cand_id"), e)
    return out
