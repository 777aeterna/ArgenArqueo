"""Acceso a datos por STAC leyendo VENTANAS de COG (nada de descarga masiva).

- Sentinel-2 L2A: Earth Search (element84).
- Sentinel-1 RTC: Microsoft Planetary Computer (firmado con planetary_computer).
- Copernicus DEM GLO-30: Earth Search (COGs en el bucket público de AWS).
- ESA WorldCover 2021 y Hansen GFC: COGs públicos leídos por ventana con rasterio.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict

import numpy as np
import pystac_client
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject
from rasterio.transform import from_origin

from . import config

log = logging.getLogger(__name__)

GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    AWS_NO_SIGN_REQUEST="YES",
    GDAL_HTTP_MULTIPLEX="YES",
    GDAL_HTTP_VERSION="2",
    GDAL_HTTP_MAX_RETRY="5",
    GDAL_HTTP_RETRY_DELAY="2",
    VSI_CACHE="TRUE",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff",
    GDAL_CACHEMAX=512,
)

S2_ASSETS = ["blue", "green", "red", "nir", "swir16", "swir22", "scl"]


def _client(url: str, retries: int = 4):
    for i in range(retries):
        try:
            if url == config.STAC_PLANETARY:
                import planetary_computer
                return pystac_client.Client.open(url, modifier=planetary_computer.sign_inplace)
            return pystac_client.Client.open(url)
        except Exception as e:  # red inestable: reintento con backoff
            if i == retries - 1:
                raise
            log.warning("STAC open falló (%s), reintento", e)
            time.sleep(2 ** i)


def _search(url: str, retries: int = 4, **kw):
    for i in range(retries):
        try:
            # dicts planos: serializables (cache/pickle/procesos) y sin resolver links
            return [it.to_dict(transform_hrefs=False) for it in _client(url).search(**kw).items()]
        except Exception as e:
            if i == retries - 1:
                raise
            log.warning("STAC search falló (%s), reintento", e)
            time.sleep(2 ** (i + 1))


def grid_spec(bounds, res):
    xmin, ymin, xmax, ymax = bounds
    w = int(round((xmax - xmin) / res))
    h = int(round((ymax - ymin) / res))
    return from_origin(xmin, ymax, res, res), (h, w)


# --------------------------------------------------------------------- Sentinel-2
def search_s2(bbox_ll, start=None, end=None, max_cloud=None):
    items = _search(config.STAC_EARTH_SEARCH, collections=["sentinel-2-l2a"], bbox=bbox_ll,
                    datetime=f"{start or config.PARAMS['s2_start']}/{end or config.PARAMS['s2_end']}",
                    query={"eo:cloud_cover": {"lt": max_cloud or config.PARAMS["s2_max_tile_cloud"]}})
    # Earth Search tiene reprocesamientos duplicados: uno por (fecha, tile, plataforma), el más nuevo.
    best = {}
    for it in items:
        if not all(str(it["assets"].get(a, {}).get("href", "")).startswith("http") and
                   str(it["assets"][a]["href"]).endswith(".tif") for a in S2_ASSETS):
            continue  # ítems viejos con JP2 en bucket requester-pays: fuera
        p = it["properties"]
        tile = str(p.get("grid:code", p.get("s2:mgrs_tile", ""))).replace("MGRS-", "")
        key = (p["datetime"][:10], tile, p.get("platform"))
        if key not in best or str(p.get("updated", "")) > str(best[key]["properties"].get("updated", "")):
            best[key] = it
    by_date = defaultdict(list)
    for (d, _, _), it in best.items():
        by_date[d].append(it)
    return dict(sorted(by_date.items()))


def _read_asset_window(href, bounds, res, epsg, resampling, dtype="float32"):
    transform, shape = grid_spec(bounds, res)
    dst = np.full(shape, np.nan if dtype.startswith("float") else 0, dtype=dtype)
    with rasterio.Env(**GDAL_ENV):
        with rasterio.open(href) as src:
            # Lee solo la ventana necesaria (con overviews si res es gruesa) vía WarpedVRT
            from rasterio.vrt import WarpedVRT
            with WarpedVRT(src, crs=f"EPSG:{epsg}", transform=transform, width=shape[1],
                           height=shape[0], resampling=resampling,
                           src_nodata=src.nodata if src.nodata is not None else 0,
                           nodata=np.nan if dtype.startswith("float") else 0,
                           dtype=dtype) as vrt:
                dst = vrt.read(1)
    return dst


def load_s2_date(items, bounds, epsg, res=10):
    """Mosaico de un día: dict banda->array float32 (reflectancia) + 'scl' uint8."""
    out = {}
    for a in S2_ASSETS:
        is_scl = a == "scl"
        acc = None
        for it in items:
            asset = it["assets"][a]
            arr = _read_asset_window(asset["href"], bounds, res, epsg,
                                     Resampling.nearest if is_scl else Resampling.bilinear,
                                     "uint8" if is_scl else "float32")
            if not is_scl:
                rb = (asset.get("raster:bands") or [{}])[0]
                scale, offset = rb.get("scale", 1e-4), rb.get("offset", 0.0)
                arr = arr * scale + offset
                arr[arr <= -0.09] = np.nan  # nodata (0 crudo) tras el offset
            if acc is None:
                acc = arr
            else:  # primer válido gana
                m = (acc == 0) if is_scl else np.isnan(acc)
                acc[m] = arr[m]
        out[a] = acc
    return out


# --------------------------------------------------------------------- Sentinel-1
def search_s1(bbox_ll, start=None, end=None):
    return _search(config.STAC_PLANETARY, collections=["sentinel-1-rtc"], bbox=bbox_ll,
                   datetime=f"{start or config.PARAMS['s2_start']}/{end or config.PARAMS['s2_end']}")


def select_s1(items, max_scenes):
    """Una sola órbita relativa (geometría constante) y submuestreo uniforme en el tiempo."""
    if not items:
        return []
    by_orbit = defaultdict(list)
    for it in items:
        by_orbit[(it["properties"].get("sat:relative_orbit"),
                  it["properties"].get("sat:orbit_state"))].append(it)
    orbit, its = max(by_orbit.items(), key=lambda kv: len(kv[1]))
    its.sort(key=lambda i: i["properties"]["datetime"])
    if len(its) > max_scenes:
        idx = np.linspace(0, len(its) - 1, max_scenes).round().astype(int)
        its = [its[i] for i in np.unique(idx)]
    return its


def load_s1_scene(item, bounds, epsg, res=10):
    out = {}
    for pol in ("vv", "vh"):
        if pol not in item["assets"]:
            continue
        a = _read_asset_window(item["assets"][pol]["href"], bounds, res, epsg, Resampling.bilinear)
        a[a <= 0] = np.nan
        out[pol] = 10 * np.log10(a)  # dB
    return out


# --------------------------------------------------------------------- DEM
def load_dem(bbox_ll, bounds, epsg, res=30):
    items = _search(config.STAC_EARTH_SEARCH, collections=["cop-dem-glo-30"], bbox=bbox_ll)
    acc = None
    for it in items:
        a = _read_asset_window(it["assets"]["data"]["href"], bounds, res, epsg, Resampling.bilinear)
        acc = a if acc is None else np.where(np.isnan(acc), a, acc)
    return acc


# --------------------------------------------------------------------- Cobertura
def _wc_tiles(bbox_ll):
    x0, y0, x1, y1 = bbox_ll
    tiles = []
    for lat in range(int(np.floor(y0 / 3) * 3), int(np.ceil(y1 / 3) * 3), 3):
        for lon in range(int(np.floor(x0 / 3) * 3), int(np.ceil(x1 / 3) * 3), 3):
            ns = f"{'S' if lat < 0 else 'N'}{abs(lat):02d}"
            ew = f"{'W' if lon < 0 else 'E'}{abs(lon):03d}"
            tiles.append(ns + ew)
    return tiles


def load_worldcover(bbox_ll, bounds, epsg, res=10):
    acc = None
    for t in _wc_tiles(bbox_ll):
        try:
            a = _read_asset_window(config.WORLDCOVER_URL.format(tile=t), bounds, res, epsg,
                                   Resampling.nearest, "uint8")
        except Exception as e:
            log.info("WorldCover %s no disponible: %s", t, e)
            continue
        acc = a if acc is None else np.where(acc == 0, a, acc)
    return acc


def load_hansen_treecover(bbox_ll, bounds, epsg, res=30):
    x0, y0, x1, y1 = bbox_ll
    acc = None
    for lat in range(int(np.ceil(y1 / 10) * 10), int(np.ceil(y0 / 10) * 10) - 1, -10):
        for lon in range(int(np.floor(x0 / 10) * 10), int(np.ceil(x1 / 10) * 10), 10):
            t = f"{abs(lat):02d}{'S' if lat < 0 else 'N'}_{abs(lon):03d}{'W' if lon < 0 else 'E'}"
            try:
                a = _read_asset_window(config.HANSEN_URL.format(tile=t), bounds, res, epsg,
                                       Resampling.nearest, "uint8")
            except Exception as e:
                log.info("Hansen %s no disponible: %s", t, e)
                continue
            acc = a if acc is None else np.maximum(acc, a)
    return acc


def resample_to(arr, src_bounds, src_res, dst_bounds, dst_res, epsg, resampling=Resampling.bilinear):
    st, _ = grid_spec(src_bounds, src_res)
    dt, dshape = grid_spec(dst_bounds, dst_res)
    dst = np.full(dshape, np.nan, dtype="float32")
    reproject(arr.astype("float32"), dst, src_transform=st, src_crs=f"EPSG:{epsg}",
              dst_transform=dt, dst_crs=f"EPSG:{epsg}", resampling=resampling,
              src_nodata=np.nan, dst_nodata=np.nan)
    return dst


# ------------------------------------------------- Carga paralela con stackstac
def _stackstac_env():
    import stackstac
    return stackstac.DEFAULT_GDAL_ENV.updated(GDAL_ENV)


def screen_s2_dates(by_date, bounds, epsg, res=20, batch=96, workers=24):
    """Lee SOLO la SCL (20 m) de todas las fechas y devuelve la fracción útil por fecha.

    Fracción útil = píxeles 4/5 sobre píxeles observables (se excluyen agua y nodata del
    denominador para no castigar celdas costeras). Evita bajar 6 bandas de fechas nubladas.
    """
    import stackstac
    dates = list(by_date)
    flat = [(d, it) for d in dates for it in by_date[d]]
    valid = set(config.PARAMS["scl_valid"])
    frac = defaultdict(float)
    for i in range(0, len(flat), batch):
        chunk = flat[i:i + batch]
        st = stackstac.stack([it for _, it in chunk], assets=["scl"], epsg=epsg, bounds=bounds,
                             resolution=res, dtype="uint8", fill_value=np.uint8(0),
                             rescale=False, gdal_env=_stackstac_env(), chunksize=4096)
        arr = st.compute(scheduler="threads", num_workers=workers)[:, 0].values
        for (d, _), a in zip(chunk, arr):
            obs = (a != 0) & (a != 6)
            n = obs.sum()
            if n:
                frac[d] = max(frac[d], float(np.isin(a[obs], list(valid)).mean() * n / max(n, 1)))
    return {d: frac[d] for d in dates}


def load_s2_batch(by_date, dates, bounds, epsg, res=10, workers=24):
    """Devuelve dict fecha -> dict banda -> array (mosaico del día, primer válido gana)."""
    import stackstac
    flat = [(d, it) for d in dates for it in by_date[d]]
    st = stackstac.stack([it for _, it in flat], assets=S2_ASSETS, epsg=epsg, bounds=bounds,
                         resolution=res, dtype="float32", fill_value=np.float32(np.nan),
                         rescale=False, gdal_env=_stackstac_env(), chunksize=4096,
                         resampling=Resampling.bilinear)
    arr = st.compute(scheduler="threads", num_workers=workers).values  # (t, band, y, x)
    # escala/offset por ítem y banda (raster:bands): reflectancia de superficie.
    # El 0 crudo es nodata y se convierte a NaN antes del offset.
    for ti, (_, it) in enumerate(flat):
        for bi, name in enumerate(S2_ASSETS):
            if name == "scl":
                continue
            rb = (it["assets"][name].get("raster:bands") or [{}])[0]
            a = arr[ti, bi]
            a[a == 0] = np.nan
            a *= np.float32(rb.get("scale", 1e-4))
            a += np.float32(rb.get("offset", 0.0))
    out: dict = {}
    for (d, _), a in zip(flat, arr):
        cur = out.get(d)
        if cur is None:
            out[d] = a.copy()
        else:
            m = np.isnan(cur)
            cur[m] = a[m]
    res_d = {}
    for d, a in out.items():
        bands = {name: a[i] for i, name in enumerate(S2_ASSETS)}
        scl = np.nan_to_num(bands.pop("scl"), nan=0).round().astype(np.uint8)
        bands["scl"] = scl
        res_d[d] = bands
    return res_d
