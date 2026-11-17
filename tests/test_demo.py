from arqueo.demo import PUBLIC_COLS


def test_demo_has_no_absolute_location_columns():
    forbidden = {"x", "y", "cell_id", "geometry", "epsg", "peak_row", "peak_col", "row", "col",
                 "cand_id", "region", "restricted"}
    assert not forbidden & set(PUBLIC_COLS)
