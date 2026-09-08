import numpy as np
from shadow_pair import detect, overlay, to_latlon, to_geojson

rng = np.random.default_rng(0)
NP, NS = 1200, 900
RPS = 0.05          # m slant range per sample
H    = 4.0          # altitude, m
PSP  = 0.08         # m per ping

nadir = int(H / RPS)

# --- background: Rayleigh speckle x range-dependent gain, flat-ish bottom ---
gain = np.ones(NS)
r = np.arange(NS) * RPS
grazing = np.clip(H / np.maximum(r, 1e-3), 0, 1)      # falls off with range
gain = 0.25 + 0.75 * grazing**0.5
wf = rng.rayleigh(scale=1.0, size=(NP, NS)).astype(np.float32) * gain[None, :] * 80
wf[:, :nadir] *= 0.05                                  # water column
wf[:, nadir:nadir+4] *= 6.0                            # nadir return

def ground(s):  return np.sqrt(max((s*RPS)**2 - H*H, 0.0))
def sample_of(g): return np.sqrt(g*g + H*H) / RPS

def put_target(wf, ping_c, s_near, height, along_px, width_px):
    g0 = ground(s_near)
    g_end = g0 / max(1 - height/H, 1e-3)
    s_end = int(sample_of(g_end))
    p0, p1 = ping_c - along_px//2, ping_c + along_px//2
    wf[p0:p1, s_near:s_near+width_px] *= 4.5            # highlight
    wf[p0:p1, s_near+width_px:s_end] *= 0.10            # acoustic shadow
    return dict(ping=ping_c, s=s_near, h=height, s_end=s_end)

truth = [
    put_target(wf, 200,  300, 0.8,  22,  8),
    put_target(wf, 500,  420, 1.5,  35, 14),
    put_target(wf, 850,  620, 0.5,  16,  6),
    put_target(wf, 1050, 250, 2.5,  46, 18),
]

dets, norm = detect(wf, range_per_sample=RPS, ping_spacing=PSP, altitude=H)
print(f"{len(dets)} detections\n")
print(f"{'ping':>6}{'samp':>6}{'grng_m':>9}{'shad_m':>8}{'ht_m':>7}{'len_m':>7}{'score':>7}")
for d in dets[:12]:
    print(f"{d.ping:6.0f}{d.sample:6.0f}{d.ground_range_m:9.1f}{d.shadow_len_m:8.2f}"
          f"{d.height_m:7.2f}{d.length_m:7.2f}{d.score:7.2f}")

print("\ntruth:")
for t in truth:
    print(f"  ping {t['ping']:4d}  sample {t['s']:4d}  height {t['h']:.2f} m")

lat = 44.80 + np.arange(NP) * PSP / 111320.0
lon = np.full(NP, -68.77)
hdg = np.zeros(NP)
recs = to_latlon(dets, lat, lon, hdg, "starboard", RPS, H)
gj = to_geojson(recs)
print(f"\ngeojson features: {len(gj['features'])}  first: "
      f"{gj['features'][0]['geometry']['coordinates']}")
overlay(norm, dets, "detections.png")
print("wrote detections.png")
