import sys
from prep import *
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import spearmanr
from scipy.ndimage import gaussian_filter

D = lambda s: datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=timezone.utc)
STORMS = [
    ("Nov 17-21, 2022 (the storm it was tuned on)", "2022111712", "2022112112"),
    ("Dec 23-27, 2022 (Christmas blizzard)", "2022122312", "2022122712"),
    ("Nov 17-21, 2014 (Snowvember)", "2014111712", "2014112112"),
    ("Jan 13-19, 2024", "2024011312", "2024011912"),
    ("Nov 29-Dec 4, 2024", "2024112912", "2024120412"),
    ("Dec 24-28, 2017 (Erie PA record)", "2017122412", "2017122812"),
    ("Feb 27-Mar 1, 2020 (Tug Hill)", "2020022712", "2020030112"),
    ("Nov 19-22, 2016", "2016111912", "2016112212"),
]
BELTS = {  # W, E, S, N
    "Erie belt (Cleveland to Buffalo)": (-82.0, -78.0, 41.2, 43.25),
    "Ontario belt (Rochester to Tug Hill)": (-77.8, -74.9, 42.9, 44.4),
    "Michigan (west + UP)": (-90.5, -84.3, 41.6, 47.5),
}
PTS = [("Buffalo airport", 42.94, -78.73), ("Orchard Park", 42.77, -78.74), ("Hamburg", 42.72, -78.83),
       ("Lancaster", 42.90, -78.67), ("East Aurora", 42.77, -78.61), ("Fredonia", 42.44, -79.33),
       ("Erie PA", 42.08, -80.18), ("Watertown", 43.97, -75.91), ("Redfield", 43.54, -75.81),
       ("Oswego", 43.45, -76.51), ("Syracuse", 43.11, -76.10), ("Rochester", 43.12, -77.68)]


def km(la1, lo1, la2, lo2):
    return float(np.hypot((lo1 - lo2) * 111.19 * np.cos(np.radians((la1 + la2) / 2)), (la1 - la2) * 111.19))


def at(a, la, lo):
    return a[int(np.argmin(abs(LAT - la))), int(np.argmin(abs(LON - lo)))]


def main():
    rows = []
    fig, axs = plt.subplots(len(STORMS), 2, figsize=(11, 3.6 * len(STORMS)))
    for si, (label, a, b) in enumerate(STORMS):
        t0, t1 = D(a), D(b)
        mean, smean, mx, n = window(t0, t1, 3)
        tot, d, miss = 0.0, t0 + timedelta(days=1), 0
        while d <= t1:
            s = snow(d)
            if s is None: miss += 1
            else: tot = tot + s
            d += timedelta(days=1)
        ok = LAND & np.isfinite(tot) & np.isfinite(smean)
        print("\n== %s   frames %.0f, snow days missing %d" % (label, n, miss))
        out = dict(label=label)
        for bname, (w, e, s_, n_) in BELTS.items():
            m = ok & (LON2 >= w) & (LON2 <= e) & (LAT2 >= s_) & (LAT2 <= n_)
            if tot[m].max() < 6:
                print("  %-38s observed max %.0f in: no event here" % (bname, tot[m].max())); continue
            rho = spearmanr(smean[m], tot[m]).correlation
            to, sm = gaussian_filter(np.where(m, tot, 0), 1.5), gaussian_filter(np.where(m, smean, 0), 1.5)
            io, im = np.unravel_index(np.argmax(to), to.shape), np.unravel_index(np.argmax(sm), sm.shape)
            dist = km(LAT[io[0]], LON[io[1]], LAT[im[0]], LON[im[1]])
            # how much of the heaviest-snow tenth of the belt falls in the highest-score tenth
            q_o, q_s = np.quantile(tot[m], 0.9), np.quantile(smean[m], 0.9)
            overlap = float(((tot[m] >= q_o) & (smean[m] >= q_s)).sum() / max((tot[m] >= q_o).sum(), 1))
            print("  %-38s rho %.2f | obs max %.0f in at %.2f,%.2f (score there %.0f) | score max %.0f at %.2f,%.2f (snow there %.0f in) | apart %.0f km | top-tenth overlap %.0f%%"
                  % (bname, rho, tot[io], LAT[io[0]], LON[io[1]], smean[io], smean[im], LAT[im[0]], LON[im[1]], tot[im], dist, 100 * overlap))
            out[bname] = dict(rho=rho, dist=dist, overlap=overlap, omax=float(tot[io]))
        for nm, la, lo in PTS:
            print("     %-16s snow %5.1f in   mean snow-score %4.0f   mean score %4.0f   peak %3.0f" % (nm, at(tot, la, lo), at(smean, la, lo), at(mean, la, lo), at(mx, la, lo)))
        rows.append(out)
        ext = (-83.5, -74.5, 41.0, 45.0)
        for ax, arr, ttl, vm, cm in ((axs[si, 0], np.where(np.isfinite(tot), tot, np.nan), "Observed snowfall, in (NOHRSC)", None, "Blues"),
                                     (axs[si, 1], smean, "Mean lake effect score (snow only)", None, "Greens")):
            sel = (LON2 >= ext[0]) & (LON2 <= ext[1]) & (LAT2 >= ext[2]) & (LAT2 <= ext[3])
            vmax = np.nanquantile(arr[sel & LAND], 0.999)
            im = ax.pcolormesh(LON, LAT, arr, cmap=cm, vmin=0, vmax=vmax, shading="nearest")
            ax.contour(LON, LAT, LAND.astype(float), [0.5], colors="k", linewidths=0.6)
            ax.set_xlim(ext[0], ext[1]); ax.set_ylim(ext[2], ext[3]); ax.set_aspect(1 / np.cos(np.radians(43)))
            ax.set_title("%s\n%s" % (label, ttl), fontsize=9); plt.colorbar(im, ax=ax, shrink=0.8)
            for nm, la, lo in PTS[:2] + PTS[6:8]:
                ax.plot(lo, la, "r.", ms=4); ax.text(lo + 0.05, la, nm, fontsize=6, color="r")
    plt.tight_layout(); plt.savefig("storms_%s.png" % FR, dpi=110)
    import json; json.dump(rows, open("storms_%s.json" % FR, "w"), indent=1)


if __name__ == "__main__":
    main()
