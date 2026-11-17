"""Una estructura de 300 m es invisible a 50 m y visible a 1.5/5 km; un punto es visible a todas."""
import numpy as np

from arqueo.multiscale import zscore


def _scene(seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 1, (1200, 1200)).astype("float32")
    x += 0.003 * np.mgrid[:1200, :1200][1]           # gradiente regional
    x[600:630, 300:330] += 3                           # cuadrado de 300 m
    x[100:102, 100:102] += 6                           # objeto de 20 m
    x[rng.random(x.shape) < 0.3] = np.nan              # nubes
    return x


def test_scale_selectivity():
    x = _scene()
    z50, z1500 = zscore(x, 5), zscore(x, 150)
    assert np.nanmean(z50[600:630, 300:330]) < 1.0
    assert np.nanmean(z1500[600:630, 300:330]) > 2.0
    assert np.nanmean(z50[100:102, 100:102]) > 4.0


def test_background_is_standardized():
    z = zscore(_scene(1), 50)
    bg = z[900:1100, 900:1100]
    assert abs(np.nanmedian(bg)) < 0.1
    assert 0.8 < np.nanstd(bg) < 1.2


def test_nan_preserved():
    x = _scene(2)
    z = zscore(x, 15)
    assert np.isnan(z[np.isnan(x)]).all()
