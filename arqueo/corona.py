"""CORONA/HEXAGON (1960-1972) desde la API M2M de USGS, si hay credenciales.

Requiere en el entorno USGS_M2M_USERNAME y USGS_M2M_TOKEN (token de aplicación
de ERS). Sin credenciales, este módulo se saltea limpiamente.

Corregistro automático contra Sentinel-2: ORB (o SIFT si está disponible) en
ambas imágenes, emparejamiento con test de razón de Lowe, homografía robusta
por RANSAC. Se reporta el número de inliers y el error de reproyección; si la
homografía no es confiable, el recorte se guarda marcado como no corregistrado.
"""
from __future__ import annotations

import logging
import os

import cv2
import numpy as np
import requests

log = logging.getLogger(__name__)
M2M = "https://m2m.cr.usgs.gov/api/api/json/stable/"
DATASETS = ["corona2", "declassii", "declassiii"]   # CORONA, HEXAGON KH-9 (Declass 2/3)


def available() -> bool:
    return bool(os.environ.get("USGS_M2M_USERNAME") and os.environ.get("USGS_M2M_TOKEN"))


class M2MClient:
    def __init__(self):
        r = requests.post(M2M + "login-token", json={
            "username": os.environ["USGS_M2M_USERNAME"], "token": os.environ["USGS_M2M_TOKEN"]},
            timeout=60)
        r.raise_for_status()
        self.key = r.json()["data"]
        self.h = {"X-Auth-Token": self.key}

    def _post(self, ep, payload):
        r = requests.post(M2M + ep, json=payload, headers=self.h, timeout=120)
        r.raise_for_status()
        j = r.json()
        if j.get("errorCode"):
            raise RuntimeError(f"M2M {ep}: {j['errorCode']} {j.get('errorMessage')}")
        return j["data"]

    def search(self, lon, lat, dataset):
        sf = {"spatialFilter": {"filterType": "mbr",
                                "lowerLeft": {"latitude": lat - 0.01, "longitude": lon - 0.01},
                                "upperRight": {"latitude": lat + 0.01, "longitude": lon + 0.01}}}
        return self._post("scene-search", {"datasetName": dataset, "sceneFilter": sf,
                                           "maxResults": 20}).get("results", [])

    def logout(self):
        try:
            requests.post(M2M + "logout", headers=self.h, timeout=30)
        except Exception:
            pass


def register_to_reference(img: np.ndarray, ref: np.ndarray, min_inliers: int = 15):
    """Homografía img->ref por ORB/SIFT + RANSAC. Devuelve (H, info) o (None, info)."""
    def u8(a):
        a = np.nan_to_num(a.astype(np.float32))
        lo, hi = np.percentile(a, [2, 98])
        return np.clip((a - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
    a, b = u8(img), u8(ref)
    clahe = cv2.createCLAHE(2.0, (8, 8))
    a, b = clahe.apply(a), clahe.apply(b)
    try:
        det = cv2.SIFT_create(4000)
        norm = cv2.NORM_L2
    except AttributeError:
        det = cv2.ORB_create(5000)
        norm = cv2.NORM_HAMMING
    ka, da = det.detectAndCompute(a, None)
    kb, db = det.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < 10 or len(kb) < 10:
        return None, {"inliers": 0, "reason": "pocos keypoints"}
    m = cv2.BFMatcher(norm).knnMatch(da, db, k=2)
    good = [p[0] for p in m if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < min_inliers:
        return None, {"inliers": 0, "matches": len(good), "reason": "pocos matches"}
    src = np.float32([ka[g.queryIdx].pt for g in good])
    dst = np.float32([kb[g.trainIdx].pt for g in good])
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
    if H is None:
        return None, {"inliers": 0, "reason": "RANSAC falló"}
    inl = int(mask.sum())
    proj = cv2.perspectiveTransform(src[mask.ravel() == 1][None], H)[0]
    err = float(np.sqrt(((proj - dst[mask.ravel() == 1]) ** 2).sum(1)).mean())
    info = {"inliers": inl, "matches": len(good), "reproj_px": round(err, 2)}
    return (H if inl >= min_inliers else None), info


def find_scenes(lon: float, lat: float) -> list[dict]:
    """Lista escenas CORONA/HEXAGON que cubren el punto (metadatos; sin descarga)."""
    if not available():
        log.info("Sin credenciales USGS M2M: CORONA/HEXAGON se saltea.")
        return []
    c = M2MClient()
    try:
        out = []
        for ds in DATASETS:
            try:
                for s in c.search(lon, lat, ds):
                    out.append(dict(dataset=ds, entity_id=s.get("entityId"),
                                    display_id=s.get("displayId"),
                                    date=(s.get("temporalCoverage") or {}).get("startDate"),
                                    browse=[b.get("browsePath") for b in s.get("browse", [])][:1]))
            except Exception as e:
                log.warning("M2M %s: %s", ds, e)
        return out
    finally:
        c.logout()
