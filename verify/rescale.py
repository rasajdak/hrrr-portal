"""
The terrain gain only ever adds, so with it every map reads hotter. This finds
the FETCH_KM that puts the scale back: the value at which cells the old score
(no terrain, FETCH_KM 170) called 20 or more average the same as before, over
the stored storm frames.
"""
import glob, os
import numpy as np
import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lake_effect as le
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
F0 = 170.0
tries = np.arange(170.0, 330.0, 10.0)
old_sum, new_sum, n = 0.0, np.zeros(len(tries)), 0
for p in sorted(glob.glob("parts/hrrr_*.npz"))[::2]:
    z = np.load(p)
    fetch, trim = z["fetch"].astype("float32"), z["trim"] / 200.0
    land = z["rise"] > 0
    old = 100 * (1 - np.exp(-fetch / F0)) * trim
    m = (old >= 20) & land
    boosted = fetch[m] * (1 + le.RISE_GAIN * np.clip(z["rise"][m] / le.RISE_FULL, 0, 1))
    old_sum += old[m].sum(); n += m.sum()
    new_sum += np.array([(100 * (1 - np.exp(-boosted / f)) * trim[m]).sum() for f in tries])
print("old mean %.1f over %d cells" % (old_sum / n, n))
for f, s in zip(tries, new_sum):
    print("  FETCH_KM %3.0f -> %.1f" % (f, s / n))
