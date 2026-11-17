"""MÓDULO 2 — Detector de anomalías multiescala, no supervisado, sin plantilla.

Por celda:
 1. 7 años de Sentinel-2 L2A, nubes/sombras/nieve enmascaradas con SCL.
 2. Rasgos por fecha (NDVI, brillo, B11/B12, B4/B2) y compuestos temporales
    (mediana y amplitud de NDVI, fecha del pico fenológico, medianas de brillo
    y razones; Sentinel-1: mediana y varianza de retrodispersión VV/VH).
 3. Residuo contra el fondo local (mediana robusta) a 50 m, 150 m, 500 m,
    1.5 km y 5 km. Todas las escalas corren siempre.
 4. Persistencia: en cuántas fechas independientes el residuo supera k sigmas
    con el mismo signo, contrastado con la tasa base del propio paisaje
    (z binomial). El ruido no persiste.
 5. Componentes conexas por escala + descriptores geométricos genéricos.

No hay plantillas, ni tamaños ni formas privilegiadas.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from scipy import ndimage

from . import config, stac
from .descriptors import describe
from .features import per_date, s1_composites, temporal_composites
from .grid import Cell, bbox_lonlat
from .multiscale import zscore

log = logging.getLogger(__name__)


class Timer:
    def __init__(self):
        self.t = defaultdict(float)
        self._lock = threading.Lock()

    def add(self, key, sec):
        with self._lock:
            self.t[key] += sec

    def __call__(self, key):
        timer = self

        class _C:
            def __enter__(self_):
                self_.t0 = time.perf_counter()

            def __exit__(self_, *a):
                timer.add(key, time.perf_counter() - self_.t0)
        return _C()

    def as_dict(self):
        return {k: round(v, 3) for k, v in self.t.items()}


def split_components(score: np.ndarray, mask: np.ndarray, h: float) -> np.ndarray:
    """Componentes conexas separadas por watershed sobre el score.

    Un umbral fijo hace percolar anomalías vecinas en redes de km². Se usan como
    marcadores los h-máximos del score (picos con prominencia >= h, en unidades
    de umbral): una región con un solo pico queda entera; una red con varios
    picos prominentes se divide en sus cuencas. No hay tamaño presupuesto.
    """
    from skimage.morphology import h_maxima
    from skimage.segmentation import watershed
    sc = np.where(mask, np.nan_to_num(score), 0).astype(np.float32)
    lab0, n0 = ndimage.label(mask, structure=np.ones((3, 3)))
    if n0 == 0:
        return lab0
    sm = ndimage.gaussian_filter(sc, 1.0)
    peaks = h_maxima(sm, h) & mask
    markers, _ = ndimage.label(peaks, structure=np.ones((3, 3)))
    # componentes sin marcador (meseta plana) conservan su etiqueta original
    nomark = np.setdiff1d(np.unique(lab0[mask]), np.unique(lab0[markers > 0]))
    if len(nomark):
        extra = np.isin(lab0, nomark)
        markers[extra] = lab0[extra] + markers.max()
    ws = watershed(-sm, markers, mask=mask, connectivity=2)
    return ws.astype(np.int32)


def _popcount8(a: np.ndarray) -> np.ndarray:
    return np.unpackbits(a[..., None], axis=-1).sum(-1)


def coverage_masks(cell: Cell, bounds, shape, res, timer: Timer):
    """Bosque de dosel cerrado (WorldCover 10 ∪ Hansen >= umbral) y agua permanente WC 80."""
    bl = bbox_lonlat(cell, config.PARAMS["margin_m"])
    with timer("io_coverage"):
        wc = stac.load_worldcover(bl, bounds, cell.epsg, res)
        try:
            han = stac.load_hansen_treecover(bl, bounds, cell.epsg, 30)
        except Exception as e:  # opcional
            log.info("Hansen no disponible: %s", e)
            han = None
    forest = np.zeros(shape, bool)
    water = np.zeros(shape, bool)
    if wc is not None:
        forest |= np.isin(wc, config.PARAMS["forest_classes_worldcover"])
        water |= wc == 80
    if han is not None:
        h = stac.resample_to(han.astype("float32"), bounds, 30, bounds, res, cell.epsg)
        forest |= np.nan_to_num(h) >= config.PARAMS["hansen_treecover_thr"]
    return forest, water, wc


def run_cell(cell: Cell, workdir: Path | None = None, max_dates: int | None = None,
             keep_cache: bool = False) -> dict:
    P = config.PARAMS
    timer = Timer()
    t_all = time.perf_counter()
    res = P["res_m"]
    margin = P["margin_m"]
    bounds = cell.bounds(margin)
    transform, shape = stac.grid_spec(bounds, res)
    H, W = shape
    m_px = int(margin / res)
    core = (slice(m_px, H - m_px), slice(m_px, W - m_px))
    scales_px = [max(1, int(round(s / res))) for s in P["scales_m"]]
    workdir = Path(workdir or config.DATA / "cache" / cell.cell_id)
    workdir.mkdir(parents=True, exist_ok=True)
    outdir = config.OUT / "cells" / cell.cell_id
    outdir.mkdir(parents=True, exist_ok=True)
    bl = bbox_lonlat(cell, margin)

    # ---------------------------------------------------------------- cobertura
    forest, water_wc, wc = coverage_masks(cell, bounds, shape, res, timer)
    core_forest_frac = float(forest[core].mean())
    core_water_frac = float(water_wc[core].mean()) if wc is not None else np.nan
    if wc is not None and core_water_frac > 0.98:
        return dict(status="water", forest_frac=core_forest_frac, water_frac=core_water_frac,
                    timings=timer.as_dict(), candidates=[])
    if core_forest_frac > 0.98:
        # Brecha de cobertura: no evaluable sin LiDAR. Queda registrada, no se borra.
        return dict(status="forest_gap", forest_frac=core_forest_frac, water_frac=core_water_frac,
                    timings=timer.as_dict(), candidates=[])

    # ---------------------------------------------------------------- contexto topográfico
    # (solo contexto para el triaje; no entra en la detección)
    with timer("io_dem"):
        try:
            dem30 = stac.load_dem(bl, bounds, cell.epsg, 30)
            dem = stac.resample_to(dem30, bounds, 30, bounds, res, cell.epsg) if dem30 is not None else None
        except Exception as e:
            log.warning("DEM no disponible: %s", e)
            dem = None
    if dem is not None:
        dfill = np.where(np.isfinite(dem), dem, np.nanmedian(dem) if np.isfinite(dem).any() else 0)
        tpi = (dfill - ndimage.uniform_filter(dfill, 31)).astype(np.float32)        # ±150 m
        gy, gx = np.gradient(dfill, res)
        slope = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)
    else:
        tpi = slope = np.full(shape, np.nan, np.float32)

    # ---------------------------------------------------------------- Sentinel-2
    with timer("s2_search"):
        by_date = stac.search_s2(bl)
    with timer("s2_screen_scl"):
        frac = stac.screen_s2_dates(by_date, bounds, cell.epsg)
    good = [d for d in by_date if frac.get(d, 0) >= P["s2_min_valid_frac"]]
    if max_dates and len(good) > max_dates:   # solo para pruebas de costo
        idx = np.linspace(0, len(good) - 1, max_dates).round().astype(int)
        good = [good[i] for i in np.unique(idx)]
    dates = [dt.date.fromisoformat(d) if isinstance(d, str) else d for d in good]
    T = len(dates)
    log.info("%s: %d fechas en catálogo, %d útiles", cell.cell_id, len(by_date), T)
    if T < 10:
        return dict(status="error", error=f"solo {T} fechas útiles", timings=timer.as_dict(),
                    candidates=[], forest_frac=core_forest_frac, water_frac=core_water_frac)
    years = sorted({d.year for d in dates})
    year_bit = {y: np.uint8(1 << (i % 8)) for i, y in enumerate(years)}

    F = config.PER_DATE_FEATURES
    S = len(scales_px)
    stacks = {f: np.lib.format.open_memmap(workdir / f"{f}.npy", "w+", np.float16, (T, H, W))
              for f in F}
    pos = np.zeros((len(F), S, H, W), np.uint16)
    neg = np.zeros((len(F), S, H, W), np.uint16)
    posy = np.zeros((len(F), S, H, W), np.uint8)
    negy = np.zeros((len(F), S, H, W), np.uint8)
    nvalid = np.zeros((len(F), H, W), np.uint16)
    nwater = np.zeros((H, W), np.uint16)
    nobs = np.zeros((H, W), np.uint16)
    base_exc = np.zeros((len(F), S, 2), np.float64)  # tasas base del paisaje
    base_n = np.zeros(len(F), np.float64)
    rgb_keep = []           # fechas más limpias para el compuesto RGB de las fichas
    k = P["k_sigma"]

    def process_date(ti, d, b):
        scl = b["scl"]
        valid = np.isin(scl, P["scl_valid"]) & ~forest
        obs = (scl != 0) & ~np.isin(scl, [3, 8, 9, 10, 11])
        nobs[obs] += 1
        nwater[obs & (scl == 6)] += 1
        with timer("features"):
            f = per_date(b, valid)
        for fi, name in enumerate(F):
            x = f[name]
            with np.errstate(all="ignore"):
                stacks[name][ti] = x.astype(np.float16)
            ok = np.isfinite(x)
            nvalid[fi][ok] += 1
            base_n[fi] += ok.sum()

        def one(fi_si):
            fi, si = fi_si
            t0 = time.perf_counter()
            z = zscore(f[F[fi]], scales_px[si])
            timer.add(f"scale_{P['scales_m'][si]}m_perdate", time.perf_counter() - t0)
            hp = z > k
            hn = z < -k
            # cada (fi, si) escribe en su propia rebanada: sin carreras entre hilos
            pos[fi, si][hp] += 1
            neg[fi, si][hn] += 1
            posy[fi, si][hp] |= year_bit[d.year]
            negy[fi, si][hn] |= year_bit[d.year]
            base_exc[fi, si, 0] += hp.sum()
            base_exc[fi, si, 1] += hn.sum()
        t0 = time.perf_counter()
        list(cpu_pool.map(one, [(fi, si) for fi in range(len(F)) for si in range(S)]))
        timer.add("multiscale_perdate_wall", time.perf_counter() - t0)
        if frac.get(d.isoformat(), 0) > 0.9 and len(rgb_keep) < 12:
            rgb_keep.append(np.stack([b["red"], b["green"], b["blue"]]).astype(np.float16))

    # Lectura en lotes con prefetch: la E/S del lote i+1 se solapa con el cómputo del i.
    batch = 8
    cpu_pool = ThreadPoolExecutor(max_workers=config.THREADS)
    batches = [dates[i:i + batch] for i in range(0, T, batch)]
    iso = lambda ds: [d.isoformat() for d in ds]  # noqa: E731
    with ThreadPoolExecutor(max_workers=1) as io_pool:
        fut = io_pool.submit(lambda ds: (time.perf_counter(), stac.load_s2_batch(by_date, iso(ds), bounds, cell.epsg, res)), batches[0])
        ti = 0
        for bi in range(len(batches)):
            t0 = time.perf_counter()
            _, data = fut.result()
            timer.add("s2_io_wait", time.perf_counter() - t0)
            if bi + 1 < len(batches):
                fut = io_pool.submit(lambda ds: (time.perf_counter(), stac.load_s2_batch(by_date, iso(ds), bounds, cell.epsg, res)), batches[bi + 1])
            for d in batches[bi]:
                process_date(ti, d, data[d.isoformat()])
                ti += 1
            del data
            log.info("%s: %d/%d fechas procesadas", cell.cell_id, ti, T)
    cpu_pool.shutdown()
    for s in stacks.values():
        s.flush()

    # ---------------------------------------------------------------- compuestos
    with timer("composites_s2"):
        comp = temporal_composites(stacks, dates)
    water_freq = np.where(nobs > 0, nwater / np.maximum(nobs, 1), np.nan).astype(np.float32)
    perm_water = water_wc | (water_freq > 0.5)
    # Píxeles mixtos agua/tierra (efecto de adyacencia de S2): no evaluables, no "sin hallazgos"
    shore = ndimage.binary_dilation(perm_water, iterations=P["shore_buffer_px"]) & ~perm_water \
        if perm_water.any() else np.zeros(shape, bool)
    dist_water = (ndimage.distance_transform_edt(~perm_water) * res).astype(np.float32) \
        if perm_water.any() else np.full(shape, 1e6, np.float32)

    s1_info = {}
    try:
        with timer("s1_search"):
            s1_items = stac.select_s1(stac.search_s1(bl), P["s1_max_scenes"])
        with timer("s1_io"):
            with ThreadPoolExecutor(max_workers=12) as ex:
                scenes = list(ex.map(lambda it: stac.load_s1_scene(it, bounds, cell.epsg, res), s1_items))
        with timer("composites_s1"):
            comp.update(s1_composites([s["vv"] for s in scenes if "vv" in s],
                                      [s["vh"] for s in scenes if "vh" in s]))
        s1_info = dict(n_scenes=len(scenes),
                       orbit=s1_items[0]["properties"].get("sat:relative_orbit") if s1_items else None)
    except Exception as e:
        log.warning("Sentinel-1 no disponible para %s: %s", cell.cell_id, e)
        s1_info = dict(error=str(e))

    for kname in comp:
        comp[kname][perm_water | forest | shore] = np.nan

    # ---------------------------------------------------------------- scores por escala
    zthr, cthr = P["persist_z_thr"], P["composite_z_thr"]
    p_base = base_exc / np.maximum(base_n[:, None, None], 1)
    candidates = []
    score_maps = {}
    comp_names = [c for c in config.COMPOSITE_FEATURES if c in comp]
    for si, w in enumerate(scales_px):
        sm = P["scales_m"][si]
        t0 = time.perf_counter()
        n = nvalid.astype(np.float32)
        zp = np.full((len(F), H, W), -np.inf, np.float32)
        zp_sign = np.zeros((len(F), H, W), np.int8)
        for fi in range(len(F)):
            best = None
            for sg, cnt in ((1, pos[fi, si]), (-1, neg[fi, si])):
                p = np.clip(p_base[fi, si, 0 if sg > 0 else 1], 1e-4, 0.5)
                zb = (cnt - n[fi] * p) / np.sqrt(np.maximum(n[fi] * p * (1 - p), 1e-6))
                zb[n[fi] < 10] = np.nan
                if best is None:
                    best = zb; zp_sign[fi] = sg
                else:
                    sel = zb > best
                    best = np.where(sel, zb, best); zp_sign[fi][sel] = sg
            zp[fi] = np.nan_to_num(best, nan=-np.inf)
        timer.add(f"scale_{sm}m_persistence", time.perf_counter() - t0)

        t0 = time.perf_counter()
        zc = np.stack([zscore(comp[c], w) for c in comp_names]) if comp_names else np.zeros((0, H, W))
        timer.add(f"scale_{sm}m_composites", time.perf_counter() - t0)

        t0 = time.perf_counter()
        a_p = zp.max(0) / zthr
        a_c = np.nanmax(np.abs(np.nan_to_num(zc, nan=0)), axis=0) / cthr if len(zc) else np.zeros_like(a_p)
        score = np.maximum(a_p, a_c).astype(np.float32)
        score[perm_water | forest | shore] = np.nan
        score_maps[sm] = score
        mask = np.nan_to_num(score) >= 1.0
        labels = split_components(score, mask, P["watershed_h"])
        sizes = np.bincount(labels.ravel())
        small = np.where(sizes < P["min_component_px"])[0]
        small = small[small > 0]
        if len(small):
            labels[np.isin(labels, small)] = 0
        timer.add(f"scale_{sm}m_components", time.perf_counter() - t0)

        t0 = time.perf_counter()
        descs = describe(labels, score, res)
        # rasgo dominante y signo dentro de la componente
        objs = ndimage.find_objects(labels)
        for dsc in descs:
            r, c = int(round(dsc["row"])), int(round(dsc["col"]))
            if not (core[0].start <= r < core[0].stop and core[1].start <= c < core[1].stop):
                dsc["_drop"] = True     # centroide en el margen: lo procesa la celda vecina
                continue
            sl = objs[dsc["label"] - 1]
            m = labels[sl] == dsc["label"]
            pers_means = [np.mean(zp[fi][sl][m]) for fi in range(len(F))]
            comp_means = [np.nanmean(np.abs(zc[ci][sl][m])) if np.isfinite(zc[ci][sl][m]).any() else -np.inf
                          for ci in range(len(comp_names))]
            fp = int(np.argmax(pers_means)); cp = int(np.argmax(comp_means)) if comp_names else -1
            if comp_names and comp_means[cp] / cthr > pers_means[fp] / zthr:
                dom = comp_names[cp]
                sign = int(np.sign(np.nanmean(zc[cp][sl][m])))
            else:
                dom = F[fp]
                sign = int(np.sign(np.mean(zp_sign[fp][sl][m])))
            iy = np.unravel_index(np.argmax(np.where(m, score[sl], -np.inf)), m.shape)
            py, px_ = sl[0].start + iy[0], sl[1].start + iy[1]
            # años con excedencia: del rasgo de persistencia más fuerte, con SU signo
            psign = zp_sign[fp][py, px_]
            yrs = posy[fp, si, py, px_] if psign >= 0 else negy[fp, si, py, px_]
            dsc.update(
                scale_m=sm, dominant_feature=dom, sign=sign,
                persist_z=float(pers_means[fp]),
                persist_frac=float(max(pos[fp, si][sl][m].mean(), neg[fp, si][sl][m].mean())
                                   / max(nvalid[fp][sl][m].mean(), 1)),
                n_valid_dates=int(nvalid[fp][sl][m].mean()),
                years_hit=int(_popcount8(np.array([yrs], np.uint8))[0]),
                water_freq=float(np.nanmean(water_freq[sl][m])),
                dist_water_m=float(dist_water[py, px_]),
                tpi_m=float(tpi[py, px_]),
                slope_deg=float(slope[py, px_]),
                peak_row=int(py), peak_col=int(px_),
            )
            for ci, cn in enumerate(comp_names):
                dsc[f"z_{cn}"] = float(np.nanmean(zc[ci][sl][m])) if np.isfinite(zc[ci][sl][m]).any() else np.nan
            dsc["_mask_slice"] = (sl, m)
        descs = [d for d in descs if not d.get("_drop")]
        timer.add(f"scale_{sm}m_descriptors", time.perf_counter() - t0)
        for d in descs:
            d["cell_id"] = cell.cell_id
        candidates.extend(descs)
        log.info("%s escala %dm: %d componentes", cell.cell_id, sm, len(descs))

    # ---------------------------------------------------------------- multiescala cruzada
    with timer("cross_scale"):
        masks = {sm: np.nan_to_num(s) >= 1 for sm, s in score_maps.items()}
        for d in candidates:
            sl, m = d["_mask_slice"]
            hits = [sm for sm, mk in masks.items() if mk[sl][m].mean() >= 0.3]
            d["n_scales"] = len(hits)
            d["scales_hit"] = ",".join(str(h) for h in hits)
    with timer("vectorize"):
        from rasterio.features import shapes
        from rasterio.transform import Affine
        from shapely.geometry import shape as shp_shape
        from shapely.ops import unary_union
        for d in candidates:
            sl, m = d.pop("_mask_slice")
            tr = transform * Affine.translation(sl[1].start, sl[0].start)
            polys = [shp_shape(g) for g, v in shapes(m.astype(np.uint8), mask=m, transform=tr) if v]
            d["geometry"] = unary_union(polys) if len(polys) > 1 else polys[0]
            d["x"], d["y"] = transform * (d["col"] + 0.5, d["row"] + 0.5)

    # ---------------------------------------------------------------- persistencia en disco
    with timer("save"):
        save_cell_products(cell, outdir, transform, core, score_maps, comp, rgb_keep, stacks,
                           dates, candidates, water_freq, forest)

    if not keep_cache:
        del stacks
        shutil.rmtree(workdir, ignore_errors=True)

    timer.add("total", time.perf_counter() - t_all)
    return dict(status="done", candidates=candidates, timings=timer.as_dict(),
                forest_frac=core_forest_frac, water_frac=core_water_frac,
                n_dates=T, n_dates_catalog=len(by_date), s1=s1_info,
                transform=list(transform)[:6], shape=[H, W], margin_px=m_px)


def save_cell_products(cell, outdir, transform, core, score_maps, comp, rgb_keep, stacks,
                       dates, candidates, water_freq, forest):
    import rasterio
    from rasterio.transform import Affine
    m = core[0].start
    t_core = transform * Affine.translation(m, m)
    prof = dict(driver="GTiff", crs=f"EPSG:{cell.epsg}", transform=t_core, compress="deflate",
                tiled=True, blockxsize=256, blockysize=256)

    def wr(name, arr, dtype="float32", nodata=np.nan):
        a = arr[core] if arr.ndim == 2 else arr[:, core[0], core[1]]
        if a.ndim == 2:
            a = a[None]
        with rasterio.open(outdir / f"{name}.tif", "w", height=a.shape[1], width=a.shape[2],
                           count=a.shape[0], dtype=dtype, nodata=nodata, **prof) as dst:
            dst.write(a.astype(dtype))
    for sm, s in score_maps.items():
        wr(f"score_{sm}m", s)
    for k, v in comp.items():
        wr(f"comp_{k}", v)
    wr("water_freq", water_freq)
    wr("forest_mask", forest.astype("uint8"), "uint8", 255)
    if rgb_keep:
        rgb = np.nanmedian(np.stack(rgb_keep).astype(np.float32), axis=0)
        wr("rgb_median", rgb)
    # Serie temporal del píxel pico de cada candidato (para las fichas)
    rows = np.array([d["peak_row"] for d in candidates], int)
    cols = np.array([d["peak_col"] for d in candidates], int)
    # float16: la serie es para inspección visual en las fichas; la mitad de disco
    ts = {f: np.asarray(stacks[f][:, rows, cols], np.float16) if len(rows) else np.zeros((len(dates), 0), np.float16)
          for f in stacks}
    np.savez_compressed(outdir / "timeseries.npz", dates=np.array([d.isoformat() for d in dates]),
                        rows=rows, cols=cols, **ts)
    with open(outdir / "meta.json", "w") as fh:
        json.dump(dict(cell_id=cell.cell_id, epsg=cell.epsg, transform_core=list(t_core)[:6],
                       n_dates=len(dates)), fh)
