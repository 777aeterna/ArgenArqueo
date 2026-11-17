"""Descriptores morfológicos de componentes conexas de anomalía.

Son medidas geométricas genéricas, NO comparaciones contra plantillas:
área, excentricidad, compacidad, orientación, rectitud de bordes, simetría,
patrón interno. Sirven al modelo de rareza (módulo 3), que ranquea alto lo
estadísticamente inusual dentro del propio paisaje.
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy import ndimage
from skimage.measure import regionprops


def _straightness(mask: np.ndarray) -> float:
    """Fracción del perímetro explicada por segmentos rectos largos (>=4 px)."""
    m = np.pad(mask.astype(np.uint8), 2)
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return np.nan
    c = max(cnts, key=cv2.contourArea)
    P = cv2.arcLength(c, True)
    if P < 8:
        return np.nan
    poly = cv2.approxPolyDP(c, 1.0, True)[:, 0, :].astype(float)
    seg = np.linalg.norm(np.roll(poly, -1, axis=0) - poly, axis=1)
    return float(np.clip(seg[seg >= 4].sum() / P, 0, 1))


def _iou(a, b):
    u = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / u) if u else np.nan


def _symmetry(mask: np.ndarray, orientation: float) -> tuple[float, float]:
    """(simetría especular respecto del eje mayor, simetría rotacional 180°)."""
    if mask.sum() < 6:
        return np.nan, np.nan
    m = np.pad(mask.astype(np.uint8), max(mask.shape))
    cy, cx = ndimage.center_of_mass(m)
    # skimage: orientation = ángulo entre eje 0 (filas) y eje mayor
    ang = 90 - np.degrees(orientation)
    R = cv2.getRotationMatrix2D((cx, cy), -ang, 1.0)
    R[0, 2] += m.shape[1] / 2 - cx
    R[1, 2] += m.shape[0] / 2 - cy
    r = cv2.warpAffine(m, R, (m.shape[1], m.shape[0]), flags=cv2.INTER_NEAREST) > 0
    mirror = max(_iou(r, r[::-1, :]), _iou(r, r[:, ::-1]))
    rot = _iou(r, r[::-1, ::-1])
    return mirror, rot


def describe(labels: np.ndarray, score: np.ndarray, res_m: float) -> list[dict]:
    """Descriptores para cada etiqueta >0 de `labels` (score = intensidad de anomalía)."""
    out = []
    for rp in regionprops(labels, intensity_image=np.nan_to_num(score)):
        mask = rp.image
        area_px = rp.area
        perim = rp.perimeter if rp.perimeter > 0 else np.sqrt(area_px) * 4
        filled = rp.image_filled.sum() if hasattr(rp, "image_filled") else rp.filled_area
        vals = rp.image_intensity[mask]
        if area_px >= 3:
            mirror, rot = _symmetry(mask, rp.orientation)
            ecc = float(rp.eccentricity)
            orient = float((np.degrees(rp.orientation) + 180) % 180)
        else:
            mirror = rot = ecc = orient = np.nan
        out.append(dict(
            label=int(rp.label),
            area_m2=float(area_px * res_m ** 2),
            perimeter_m=float(perim * res_m),
            major_m=float(rp.major_axis_length * res_m),
            minor_m=float(rp.minor_axis_length * res_m),
            eccentricity=ecc,
            compactness=float(np.clip(4 * np.pi * area_px / perim ** 2, 0, 1)),
            orientation_deg=orient,
            solidity=float(rp.solidity),
            extent=float(rp.extent),
            edge_straightness=_straightness(mask),
            symmetry_mirror=mirror,
            symmetry_rot180=rot,
            internal_cv=float(np.std(vals) / (np.mean(np.abs(vals)) + 1e-6)),
            hole_frac=float((filled - area_px) / filled) if filled else 0.0,
            euler=int(rp.euler_number),
            score_max=float(np.max(vals)),
            score_mean=float(np.mean(vals)),
            row=float(rp.centroid[0]), col=float(rp.centroid[1]),
        ))
    return out
