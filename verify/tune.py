"""
Try terrain weightings on the stored ingredients (tune_fetch.py) of the storms
in storms.py: for each (gain, full rise) the belt-by-belt rank correlation with
observed snowfall, how far the score's peak sits from the snow's, and how much
of the snowiest tenth of the belt the highest-scoring tenth covers.
"""
import itertools
from prep import *
from storms import STORMS, BELTS, D, km
from scipy.stats import spearmanr
from scipy.ndimage import gaussian_filter
import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lake_effect as le

GAINS, FULLS = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0), (250.0, 400.0, 600.0)
combos = [(0.0, 400.0)] + [c for c in itertools.product(GAINS[1:], FULLS)]


def means(t0, t1):
    acc, n, t = {c: 0.0 for c in combos}, 0, t0
    while t <= t1:
        p = "parts/hrrr_%s_f000.npz" % t.strftime("%Y%m%d%H")
        if os.path.exists(p):
            z = np.load(p)
            fetch, rise = z["fetch"].astype("float32"), z["rise"].astype("float32")
            trim = z["trim"] / 200.0 * z["snow"]
            for g_, f_ in combos:
                s = 100.0 * (1.0 - np.exp(-fetch * (1.0 + g_ * np.clip(rise / f_, 0, 1)) / le.FETCH_KM)) * trim
                acc[(g_, f_)] = acc[(g_, f_)] + s.ravel()[IDX].reshape(LAT2.shape)
            n += 1
        t += timedelta(hours=3)
    return {c: a / n for c, a in acc.items()}


res = {c: {b: [] for b in BELTS} for c in combos}
for label, a, b in STORMS:
    t0, t1 = D(a), D(b)
    tot, d = 0.0, t0 + timedelta(days=1)
    while d <= t1:
        tot = tot + snow(d); d += timedelta(days=1)
    mm = means(t0, t1)
    for bn, (w, e, s_, n_) in BELTS.items():
        m = LAND & np.isfinite(tot) & (LON2 >= w) & (LON2 <= e) & (LAT2 >= s_) & (LAT2 <= n_)
        if tot[m].max() < 20:
            continue
        to = gaussian_filter(np.where(m, tot, 0), 1.5); io = np.unravel_index(np.argmax(to), to.shape)
        for c in combos:
            sm_ = mm[c]
            sm = gaussian_filter(np.where(m, sm_, 0), 1.5); im = np.unravel_index(np.argmax(sm), sm.shape)
            qo, qs = np.quantile(tot[m], 0.9), np.quantile(sm_[m], 0.9)
            res[c][bn].append((spearmanr(sm_[m], tot[m]).correlation, km(LAT[io[0]], LON[io[1]], LAT[im[0]], LON[im[1]]),
                               ((tot[m] >= qo) & (sm_[m] >= qs)).sum() / (tot[m] >= qo).sum()))
print("gain full | " + " | ".join("%-34s" % b[:34] for b in BELTS))
print("          | " + " | ".join("n  rho   km-off  top-tenth        " for b in BELTS))
for c in combos:
    cells = []
    for bn in BELTS:
        r = np.array(res[c][bn])
        cells.append("%d  %.2f  %5.0f   %3.0f%%            " % (len(r), r[:, 0].mean(), r[:, 1].mean(), 100 * r[:, 2].mean()))
    print("%4.1f %4.0f | " % c + " | ".join(cells))
