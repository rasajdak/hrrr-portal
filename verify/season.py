from prep import *
D = lambda s: datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=timezone.utc)
REG = {"Erie belt": (-82.0, -78.0, 41.2, 43.25), "Ontario belt": (-77.8, -74.9, 42.9, 44.4)}
sub = np.zeros_like(LAND); sub[::3, ::3] = True
S, O, MX, DAY, RG = [], [], [], [], []
daily = []
for a, b in (("2022110212", "2023031512"), ("2024111612", "2025022812")):
    d = D(a)
    while d <= D(b):
        w = window(d - timedelta(days=1), d, 6); o = snow(d)
        if w is not None and o is not None and w[3] >= 3.5:
            mean, smean, mx, _ = w
            row = [d.strftime("%Y-%m-%d")]
            for ri, (rn, (W_, E_, S_, N_)) in enumerate(REG.items()):
                m = LAND & np.isfinite(o) & (LON2 >= W_) & (LON2 <= E_) & (LAT2 >= S_) & (LAT2 <= N_)
                mm = m & sub
                S.append(smean[mm]); O.append(o[mm]); MX.append(mx[mm]); RG.append(np.full(mm.sum(), ri)); DAY.append(np.full(mm.sum(), len(daily)))
                row += [float(np.quantile(smean[m], 0.98)), float(np.quantile(o[m], 0.98)), float(np.quantile(mean[m], 0.98))]
            daily.append(row)
        d += timedelta(days=1)
S, O, MX, RG = map(np.concatenate, (S, O, MX, RG))
print(len(daily), "days", len(S), "cell-days")
print("\nDaily mean snow-score bin -> observed 24 h snow (both belts)")
print("  bin        share   mean in   P>=1in  P>=4in  P>=8in  P>=12in")
edges = [0, 5, 10, 20, 30, 40, 50, 101]
for lo, hi in zip(edges[:-1], edges[1:]):
    m = (S >= lo) & (S < hi)
    if m.sum():
        print("  %3d-%-3d  %6.1f%%  %7.2f   %5.0f%%  %5.0f%%  %5.0f%%  %5.0f%%" % (lo, hi, 100 * m.mean(), O[m].mean(), *(100 * (O[m] >= t).mean() for t in (1, 4, 8, 12))))
for ri, rn in enumerate(REG):
    r = RG == ri
    print("\n%s: yes/no table, cell-days" % rn)
    for st in (15, 20, 30, 40):
        for ot in (2, 6, 12):
            f, o = (S >= st) & r, (O >= ot) & r
            h, fa, mi = (f & o).sum(), (f & ~o).sum(), (~f & o).sum()
            print("  score>=%2d vs snow>=%2d in: caught %3.0f%% of the snow | %3.0f%% of flagged got it | CSI %.2f" % (st, ot, 100 * h / max(h + mi, 1), 100 * h / max(h + fa, 1), h / max(h + fa + mi, 1)))
from scipy.stats import spearmanr, rankdata
print("\nWhatever the threshold (so two tunings of the score compare fairly):")
for ri, rn in enumerate(REG):
    r = RG == ri
    line = " %s  cell-day rho %.2f" % (rn, spearmanr(S[r], O[r]).correlation)
    for ot in (6, 12):
        y = O[r] >= ot
        rk = rankdata(S[r])
        auc = (rk[y].sum() - y.sum() * (y.sum() + 1) / 2) / (y.sum() * (~y).sum())
        best = max((((S[r] >= st) & y).sum() / max(((S[r] >= st) | y).sum(), 1), st) for st in range(5, 90, 5))
        line += " | snow>=%d in: AUC %.3f, best CSI %.2f at score %d" % (ot, auc, best[0], best[1])
    print(line)
print("\nDay-level (98th percentile in the belt): score vs snow")
for ri, rn in enumerate(REG):
    s = np.array([r[1 + 3 * ri] for r in daily]); o = np.array([r[2 + 3 * ri] for r in daily]); al = np.array([r[3 + 3 * ri] for r in daily])
    print(" %s  rho %.2f   days snow>=6: %d, of those score>=30: %d, >=20: %d   | days score>=30: %d, of those snow>=6: %d, >=2: %d, <1: %d" % (
        rn, spearmanr(s, o).correlation, (o >= 6).sum(), ((o >= 6) & (s >= 30)).sum(), ((o >= 6) & (s >= 20)).sum(), (s >= 30).sum(), ((s >= 30) & (o >= 6)).sum(), ((s >= 30) & (o >= 2)).sum(), ((s >= 30) & (o < 1)).sum()))
    order = np.argsort(-(s - 3 * o))
    print("   biggest false alarms (score, snow in, any-type score):", [(daily[i][0], round(s[i]), round(o[i], 1), round(al[i])) for i in order[:8]])
    order = np.argsort(-(o - s / 3))
    print("   biggest misses:", [(daily[i][0], round(s[i]), round(o[i], 1)) for i in order[:8]])
import json; json.dump(daily, open("daily_%s.json" % FR, "w"))
