from arqueo import state
from arqueo.grid import cells_for_tiers


def test_claim_is_exclusive_and_idempotent(tmp_path):
    con = state.connect(tmp_path / "s.sqlite")
    cells = cells_for_tiers(["T1"])[:3]
    state.upsert_cells(con, cells)
    a = state.claim(con, "w1", "h1")
    b = state.claim(con, "w2", "h1")
    assert a.cell_id != b.cell_id
    state.finish(con, a.cell_id, "done", "h1")
    # mismo hash: no se vuelve a reclamar; hash nuevo: sí
    ids = {state.claim(con, "w3", "h1").cell_id}
    assert a.cell_id not in ids
    con.execute("UPDATE cells SET status='done', param_hash='h1'")
    assert state.claim(con, "w4", "h1") is None
    assert state.claim(con, "w5", "h2") is not None


def test_scan_order_south_to_north():
    cells = [c for c in cells_for_tiers(["T1"])]
    lats = [c.lat for c in cells]
    assert lats == sorted(lats)
