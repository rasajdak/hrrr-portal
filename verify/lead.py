import prep
from prep import *
from scipy.stats import spearmanr
from scipy.ndimage import gaussian_filter
D = lambda s: datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=timezone.utc)
g = np.load(sorted(glob.glob("*/grid_gfs.npz"))[0]); k = np.cos(np.radians(45.5))
_, GI = cKDTree(np.c_[g["lon"].ravel() * k, g["lat"].ravel()]).query(np.c_[LON2.ravel() * k, LAT2.ravel()])
def gmean(t0, t1, fxx):
    acc, n, t = 0.0, 0, t0
    while t <= t1:
        p = FR + "/gfs_%s_f%03d.npz" % ((t - timedelta(hours=fxx)).strftime("%Y%m%d%H"), fxx)
        if os.path.exists(p):
            z = np.load(p); s = z["score"].ravel()[GI].reshape(LAT2.shape).astype("float32"); s[s == 255] = np.nan
            acc = acc + s * (z["group"].ravel()[GI].reshape(LAT2.shape) == 2); n += 1
        t += timedelta(hours=6)
    return acc / n, n
def km(la1, lo1, la2, lo2):
    return float(np.hypot((lo1 - lo2) * 111.19 * np.cos(np.radians((la1 + la2) / 2)), (la1 - la2) * 111.19))
BELTS = {"Erie": (-82.0, -78.0, 41.2, 43.25), "Ontario": (-77.8, -74.9, 42.9, 44.4)}
for label, a, b in (("Nov 2022", "2022111712", "2022112112"), ("Dec 2022", "2022122312", "2022122712"), ("Jan 2024", "2024011312", "2024011912"), ("Nov-Dec 2024", "2024112912", "2024120412")):
    t0, t1 = D(a), D(b)
    tot, d = 0.0, t0 + timedelta(days=1)
    while d <= t1:
        tot = tot + snow(d); d += timedelta(days=1)
    hm = window(t0, t1, 3)[1]
    print("\n==", label)
    for bn, (w, e, s_, n_) in BELTS.items():
        m = LAND & np.isfinite(tot) & (LON2 >= w) & (LON2 <= e) & (LAT2 >= s_) & (LAT2 <= n_)
        to = gaussian_filter(np.where(m, tot, 0), 1.5); io = np.unravel_index(np.argmax(to), to.shape)
        line = "  %-8s obs max %.0f in | HRRR analysis: rho %.2f, belt peak %.0f" % (bn, tot[io], spearmanr(hm[m], tot[m]).correlation, np.nanmax(hm[m]))
        for fxx in (24, 72, 120, 168):
            gm, n = gmean(t0, t1, fxx)
            sm = gaussian_filter(np.where(m, gm, 0), 1.5); im = np.unravel_index(np.argmax(sm), sm.shape)
            line += " | GFS %dd: rho %.2f, peak %.0f, %3.0f km off" % (fxx // 24, spearmanr(gm[m], tot[m]).correlation, np.nanmax(gm[m]), km(LAT[io[0]], LON[io[1]], LAT[im[0]], LON[im[1]]))
        print(line)
