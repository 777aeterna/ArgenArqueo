"""Rasgos espectrales por fecha y compuestos temporales por píxel."""
from __future__ import annotations

import datetime as dt

import numpy as np

EPS = 1e-6


def nanquantiles(a: np.ndarray, qs, axis: int = 0) -> list[np.ndarray]:
    """Cuantiles ignorando NaN por ordenamiento (mucho más rápido que np.nanpercentile).

    NaN se manda al final (+inf); el cuantil q toma el índice floor(q*(n-1)) de los n válidos.
    """
    a = np.moveaxis(np.asarray(a, np.float32), axis, 0)
    n = np.isfinite(a).sum(0)
    srt = np.sort(np.where(np.isfinite(a), a, np.inf), axis=0)
    out = []
    for q in qs:
        idx = np.clip(np.floor(q * (n - 1)).astype(np.int64), 0, a.shape[0] - 1)
        v = np.take_along_axis(srt, idx[None], 0)[0]
        v[n == 0] = np.nan
        out.append(v.astype(np.float32))
    return out


def per_date(b: dict, valid: np.ndarray) -> dict[str, np.ndarray]:
    """NDVI, brillo, razones de suelo B11/B12 y B4/B2 para una fecha (NaN fuera de `valid`)."""
    red, nir, blue = b["red"], b["nir"], b["blue"]
    with np.errstate(all="ignore"):
        f = {
            "ndvi": (nir - red) / (nir + red + EPS),
            "bright": (b["blue"] + b["green"] + red + nir + b["swir16"] + b["swir22"]) / 6.0,
            # razones en log: simétricas y estables frente a escala
            "r1112": np.log((b["swir16"] + EPS) / (b["swir22"] + EPS)),
            "r42": np.log((red + EPS) / (blue + EPS)),
        }
    # NDVI indefinido con reflectancias casi nulas (agua, sombra residual): fuera de [-1, 1]
    dark = (nir + red) < 0.02
    f["ndvi"] = np.where(dark | (f["ndvi"] < -1) | (f["ndvi"] > 1), np.nan, f["ndvi"])
    for k in f:
        a = f[k].astype(np.float32)
        a[~valid | ~np.isfinite(a)] = np.nan
        f[k] = a
    return f


def doy_austral(d: dt.date) -> float:
    """Día del año desplazado medio año: el verano austral queda en el centro (≈182)."""
    return float((d.timetuple().tm_yday + 182) % 365)


def temporal_composites(stack: dict[str, np.ndarray], dates: list[dt.date],
                        row_block: int = 100) -> dict[str, np.ndarray]:
    """Compuestos por píxel desde stacks (t, y, x) float16 (memmap), por bloques de filas.

    ndvi_med, ndvi_amp (p90-p10), ndvi_peak (centroide fenológico: fecha media
    ponderada por el exceso de NDVI sobre su mediana, en día austral),
    bright_med, r1112_med, r42_med.
    """
    T, H, W = stack["ndvi"].shape
    theta = np.array([2 * np.pi * doy_austral(d) / 365.0 for d in dates], np.float32)[:, None, None]
    out = {k: np.full((H, W), np.nan, np.float32) for k in
           ["ndvi_med", "ndvi_amp", "ndvi_peak", "bright_med", "r1112_med", "r42_med"]}
    for r0 in range(0, H, row_block):
        sl = slice(r0, min(H, r0 + row_block))
        nd = np.asarray(stack["ndvi"][:, sl], np.float32)
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                q10, q50, q90 = nanquantiles(nd, [0.1, 0.5, 0.9])
                out["ndvi_med"][sl] = q50
                out["ndvi_amp"][sl] = q90 - q10
                w = np.clip(nd - q50[None], 0, None)
                w = np.nan_to_num(w)
                s = (w * np.sin(theta)).sum(0)
                c = (w * np.cos(theta)).sum(0)
                ang = np.arctan2(s, c)
                peak = (ang % (2 * np.pi)) * 365.0 / (2 * np.pi)
                peak[w.sum(0) <= 0] = np.nan
                out["ndvi_peak"][sl] = peak
                for k in ("bright", "r1112", "r42"):
                    out[f"{k}_med"][sl] = nanquantiles(stack[k][:, sl], [0.5])[0]
    return out


def s1_composites(vv: list[np.ndarray], vh: list[np.ndarray]) -> dict[str, np.ndarray]:
    """Mediana y varianza temporal de retrodispersión (dB)."""
    out = {}
    import warnings
    for name, lst in (("s1vv", vv), ("s1vh", vh)):
        if not lst:
            continue
        a = np.stack(lst).astype(np.float32)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            q25, q50, q75 = nanquantiles(a, [0.25, 0.5, 0.75])
            out[f"{name}_med"] = q50
            # varianza robusta (IQR/1.349)^2: no la dominan speckle aislado ni outliers
            out[f"{name}_var"] = ((q75 - q25) / 1.349) ** 2
    return out
