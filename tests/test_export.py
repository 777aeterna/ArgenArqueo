import geopandas as gpd
from shapely.geometry import Point

from arqueo import candidates, config, export


def test_restricted_never_exported(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUT", tmp_path)
    monkeypatch.setattr(config, "CANDIDATES_GPKG", tmp_path / "c.gpkg")
    g = gpd.GeoDataFrame(dict(cand_id=["a", "b", "c"], rank=[1, 2, 3], restricted=[True, False, False],
                              bucket=["morfologia_descrita", "NO_CLASIFICABLE", "moderno_historico"]),
                         geometry=[Point(0, 0), Point(1, 1), Point(2, 2)], crs=4326)
    candidates.save_all(g, tmp_path / "c.gpkg")
    r = export.export()
    out = gpd.read_file(r["path"])
    assert "a" not in set(out["cand_id"])
    assert r["excluded_restricted"] == 1 and r["written"] == 2
