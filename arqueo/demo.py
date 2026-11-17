"""Demo pública SIN georreferencia: muestra cómo trabaja el pipeline sin revelar dónde.

Qué se publica:
- tabla del top-N (sin clase restringida) con descriptores y scores, y
  coordenadas RELATIVAS en metros con un corrimiento aleatorio secreto: las
  distancias entre candidatos se conservan, la ubicación absoluta no;
- ids anonimizados (sin id de celda, que codifica la posición UTM);
- fichas con recortes RGB de solo 500 m (ventanas grandes con costa permiten
  ubicar el lugar comparando con un mapa), mapas de score de 1.5 km y la
  serie temporal del píxel;
- un resumen estadístico (conteos por escala y bucket, tiempos por celda).

Qué NO se publica nunca: candidatos restringidos (posibles chenques),
rasters completos, GeoPackages, CRS ni coordenadas absolutas.
El corrimiento secreto se guarda solo en outputs/ (fuera del repo).
"""
from __future__ import annotations

import json
import secrets
import shutil

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from . import candidates as cstore  # noqa: E402
from . import config  # noqa: E402
from .chips import _read_win, _rgb  # noqa: E402
from .state import connect  # noqa: E402

PUBLIC_COLS = [
    "demo_id", "rank", "bucket", "literature_class", "rarity", "scale_m", "n_scales",
    "scales_hit", "dominant_feature", "sign", "score_max", "persist_z", "persist_frac",
    "years_hit", "area_m2", "eccentricity", "compactness", "orientation_deg",
    "edge_straightness", "symmetry_mirror", "symmetry_rot180", "internal_cv", "hole_frac",
    "modern_flag", "modern_tags", "x_rel_m", "y_rel_m",
]


def _offset():
    p = config.OUT / "demo_secret_offset.json"
    if p.exists():
        return json.loads(p.read_text())
    off = {"dx": secrets.randbelow(200_000) - 100_000, "dy": secrets.randbelow(200_000) - 100_000}
    p.write_text(json.dumps(off))
    return off


def _public_chip(row, demo_id, out_png):
    d = config.OUT / "cells" / row["cell_id"]
    x, y = float(row["x"]), float(row["y"])
    fig = plt.figure(figsize=(14, 7))
    gs = fig.add_gridspec(2, 6)
    ax = fig.add_subplot(gs[0, 0])
    if (d / "rgb_median.tif").exists():
        ax.imshow(_rgb(_read_win(d / "rgb_median.tif", x, y, 250)))
    ax.set_title("S2 mediana 500 m", fontsize=8); ax.set_xticks([]); ax.set_yticks([])
    for i, sm in enumerate(config.PARAMS["scales_m"]):
        ax = fig.add_subplot(gs[0, i + 1])
        p = d / f"score_{sm}m.tif"
        if p.exists():
            ax.imshow(_read_win(p, x, y, 750)[0], vmin=0, vmax=3, cmap="magma")
        ax.set_title(f"score {sm} m (1.5 km)", fontsize=8); ax.set_xticks([]); ax.set_yticks([])
    ax = fig.add_subplot(gs[1, :4])
    ts_path = d / "timeseries.npz"
    if ts_path.exists():
        ts = np.load(ts_path)
        idx = np.where((ts["rows"] == int(row["peak_row"])) & (ts["cols"] == int(row["peak_col"])))[0]
        if len(idx):
            dates = np.array(ts["dates"], dtype="datetime64[D]")
            for f in config.PER_DATE_FEATURES:
                v = ts[f][:, idx[0]].astype(np.float32)
                ok = np.isfinite(v)
                ax.plot(dates[ok], v[ok], ".", ms=3, label=f)
            ax.legend(fontsize=7, ncol=4)
    ax.set_title("serie temporal del píxel pico (2019-2025)", fontsize=8)
    ax = fig.add_subplot(gs[1, 4:])
    ax.axis("off")
    keys = ["rank", "bucket", "literature_class", "rarity", "scale_m", "n_scales", "dominant_feature",
            "sign", "score_max", "persist_z", "years_hit", "area_m2", "eccentricity", "compactness",
            "edge_straightness", "symmetry_mirror", "hole_frac", "modern_tags"]
    txt = "\n".join(f"{k}: {row[k]:.3g}" if isinstance(row.get(k), float) else f"{k}: {row.get(k)}"
                    for k in keys if k in row)
    ax.text(0, 1, txt, va="top", family="monospace", fontsize=7)
    fig.suptitle(f"{demo_id} — demo sin georreferencia", fontsize=10)
    fig.tight_layout()
    fig.savefig(out_png, dpi=100)
    plt.close(fig)


def build(dest, top: int = 500, n_chips: int = 30, title: str = "") -> dict:
    g = cstore.read()
    if "restricted" not in g:
        raise RuntimeError("Correr 'triage' antes.")
    n_restricted = int(g["restricted"].astype(bool).sum())
    g = g[~g["restricted"].astype(bool)].sort_values("rank").copy()
    off = _offset()
    g["x_rel_m"] = (g["x"] + off["dx"]).round(-1)
    g["y_rel_m"] = (g["y"] + off["dy"]).round(-1)
    g["demo_id"] = [f"cand_{i:05d}" for i in range(1, len(g) + 1)]
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "fichas").exists() and shutil.rmtree(dest / "fichas")
    (dest / "fichas").mkdir()
    top_df = g.head(top)
    pd.DataFrame(top_df[[c for c in PUBLIC_COLS if c in top_df]]).to_csv(
        dest / "candidatos_top.csv", index=False, float_format="%.4g")
    for _, row in top_df.head(n_chips).iterrows():
        _public_chip(row, row["demo_id"], dest / "fichas" / f"{row['demo_id']}.png")
    con = connect()
    cells = con.execute("SELECT timings_json FROM cells WHERE status='done'").fetchall()
    tim = [json.loads(r[0]) for r in cells]
    all_g = cstore.read()
    summary = dict(
        n_celdas=len(cells), area_km2=len(cells) * 100,
        n_candidatos=int(len(all_g)), n_restringidos_no_publicados=n_restricted,
        por_escala=all_g["scale_m"].value_counts().sort_index().to_dict(),
        por_bucket=all_g["bucket"].value_counts().to_dict(),
        clases_bibliograficas={k: int(v) for k, v in all_g["literature_class"].replace("", "ninguna")
                               .value_counts().items() if k != "posible_chenque_o_estructura_de_piedra"},
        segundos_por_celda=[t.get("total") for t in tim],
        fechas_utiles_por_celda=[t.get("n_dates") for t in tim],
    )
    (dest / "resumen.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=int))
    return summary
