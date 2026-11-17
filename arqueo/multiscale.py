"""Fondo local robusto multiescala y residuos normalizados.

Para cada ventana W (50 m ... 5 km) se estima el fondo local con la MEDIANA de
bloques de W x W, calculada sobre dos grillas desfasadas W/2 e interpolada
bilinealmente a resolución completa (aproximación eficiente de un filtro de
mediana deslizante, que a 5 km = 500 px sería prohibitivo por fecha). La
dispersión local es el MAD de los residuos en bloques de max(W, 500 m).

    z_W = (x - mediana_local_W) / (1.4826 * MAD_local + eps)

No hay ninguna forma ni tamaño presupuesto: el residuo a cada escala solo dice
"este píxel se aparta de su entorno de tamaño W".
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy import ndimage

try:
    import bottleneck as bn
    _nanmedian = bn.nanmedian
except ImportError:  # pragma: no cover
    _nanmedian = np.nanmedian


def _block_reduce(x: np.ndarray, b: int, off: int, fn=_nanmedian):
    """Reduce por bloques b x b con desfase `off`. Devuelve (coarse, pad_top_left)."""
    H, W = x.shape
    pt = off
    Hp = int(np.ceil((H + pt) / b) * b)
    Wp = int(np.ceil((W + pt) / b) * b)
    xp = np.full((Hp, Wp), np.nan, dtype=np.float32)
    xp[pt:pt + H, pt:pt + W] = x
    blocks = xp.reshape(Hp // b, b, Wp // b, b).transpose(0, 2, 1, 3).reshape(Hp // b, Wp // b, b * b)
    with np.errstate(all="ignore"):
        coarse = fn(blocks, axis=2)
    return coarse.astype(np.float32), pt


def _fill_nan_nearest(c: np.ndarray) -> np.ndarray:
    m = np.isnan(c)
    if not m.any():
        return c
    if m.all():
        return c
    idx = ndimage.distance_transform_edt(m, return_distances=False, return_indices=True)
    return c[tuple(idx)]


def _upsample(coarse: np.ndarray, b: int, pt: int, shape) -> np.ndarray:
    """Interpola bilinealmente valores de centros de bloque a la grilla fina.

    cv2.resize con factor entero b usa centros de píxel: fino_i -> (i+0.5)/b-0.5 en la
    grilla gruesa, que es exactamente la posición del centro de cada bloque.
    """
    H, W = shape
    c = _fill_nan_nearest(coarse)
    if np.isnan(c).all():
        return np.full(shape, np.nan, np.float32)
    up = cv2.resize(c, (c.shape[1] * b, c.shape[0] * b), interpolation=cv2.INTER_LINEAR)
    return up[pt:pt + H, pt:pt + W]


def background(x: np.ndarray, w_px: int) -> np.ndarray:
    """Mediana local robusta de ventana ~w_px (promedio de dos grillas desfasadas)."""
    b = max(int(w_px), 3)
    acc = np.zeros(x.shape, np.float32)
    offs = (0, b // 2)
    for off in offs:
        coarse, pt = _block_reduce(x, b, off)
        acc += _upsample(coarse, b, pt, x.shape)
    return acc / len(offs)


def local_sigma(resid: np.ndarray, w_px: int, min_px: int = 50) -> np.ndarray:
    b = max(int(w_px), min_px)
    coarse, pt = _block_reduce(np.abs(resid), b, 0)
    s = 1.4826 * _upsample(coarse, b, pt, resid.shape)
    g = 1.4826 * np.nanmedian(np.abs(resid))
    floor = 0.1 * g if np.isfinite(g) and g > 0 else 1e-6
    return np.maximum(s, floor)


def zscore(x: np.ndarray, w_px: int) -> np.ndarray:
    """Residuo normalizado contra el fondo local a escala w_px. NaN se preserva."""
    if np.isnan(x).all():
        return np.full(x.shape, np.nan, np.float32)
    r = x - background(x, w_px)
    return (r / local_sigma(r, w_px)).astype(np.float32)
