#!/usr/bin/env python3
"""LST harian VIIRS (S-NPP) dari NASA LP DAAC, produk VNP21A1D versi 2, untuk titik pantau Berau.

Alur:
  1. Cari granul terbaru lewat katalog NASA CMR (publik, tanpa login).
  2. Unduh berkas .h5 dari LP DAAC (butuh token Earthdata gratis) ke folder cache_lst/.
  3. Baca lapisan LST dan QC, buang piksel awan dan tidak valid, ambil median jendela 3x3 per titik.
  4. Untuk tiap titik pakai akuisisi terbaru yang bebas awan dalam 10 hari terakhir.

Persiapan:
  pip install numpy h5py
  Token : buat di https://urs.earthdata.nasa.gov (Profile > Generate Token), lalu
          PowerShell: $env:EARTHDATA_TOKEN="isi_token"   atau simpan di berkas earthdata_token.txt
          (token berlaku terbatas; bila ditolak, buat ulang dan pastikan akun sudah menyetujui aplikasi LP DAAC)

Pemakaian:
  python lst_viirs.py                    # ambil dan cetak LST tiap titik pantau
  python lst_viirs.py --inspect f.h5     # cetak isi satu berkas .h5 (untuk diagnosis)
  python lst_viirs.py --selftest         # uji tanpa internet (butuh numpy dan h5py)

Keterbatasan yang perlu diketahui:
  - Citra satelit orbit polar: LST baru tersedia beberapa hari setelah akuisisi dan hanya pada piksel bebas awan.
  - Nama lapisan dalam berkas dicari otomatis (LST_1KM dan QC), tetapi struktur berkas asli belum pernah
    diuji dari lingkungan pengembang. Bila gagal, jalankan --inspect dan periksa keluarannya.
  - Penafsiran QC (bit 0-1: 0 = terbaik, 1 = nominal, 2 = terkontaminasi awan, 3 = tidak dihasilkan)
    mengikuti dokumentasi keluarga produk MOD21/VNP21 dan perlu dicocokkan dengan panduan pengguna VNP21.
"""
import json, math, os, re, sys, tempfile, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache_lst")
CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
SHORT_NAME = "VNP21A1D"
VERSION = "002"
PRODUCT = "VNP21A1D v002 (VIIRS S-NPP, LST harian siang, 1 km)"

# Grid sinusoidal MODIS/VIIRS 1 km (konstanta standar NASA)
R_EARTH = 6371007.181
TILE_M = 1111950.5196666666
X0, Y0 = -20015109.354, 10007554.677
N = 1200
PIXEL_M = TILE_M / N

SCALE_DEFAULT = 0.02
VALID_MIN_DEFAULT = 7500
MIN_VALID = 3            # minimal piksel valid dalam jendela 3x3 (dari 9)
KELVIN_RANGE = (240.0, 350.0)  # di luar ini dianggap tidak masuk akal dan dibuang
MAX_AGE_DAYS = 10


class AuthError(RuntimeError):
    pass


# ---------------------------------------------------------------- geometri grid
def _xy(lat, lon):
    return (R_EARTH * math.radians(lon) * math.cos(math.radians(lat)), R_EARTH * math.radians(lat))


def tile_of(lat, lon):
    x, y = _xy(lat, lon)
    return int((x - X0) // TILE_M), int((Y0 - y) // TILE_M)


def pixel_of(lat, lon, h, v):
    x, y = _xy(lat, lon)
    col = int((x - (X0 + h * TILE_M)) // PIXEL_M)
    row = int(((Y0 - v * TILE_M) - y) // PIXEL_M)
    return row, col


# ---------------------------------------------------------------- jaringan
class _DropAuthCrossHost(urllib.request.HTTPRedirectHandler):
    """Token hanya dikirim ke host asal. Pengalihan ke host lain (URL S3 bertanda tangan) tanpa token,
    karena S3 menolak permintaan yang membawa dua mekanisme autentikasi."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for k in list(new.headers):
                if k.lower() == "authorization":
                    del new.headers[k]
        return new


def _open(url, token=None, timeout=120):
    headers = {"User-Agent": "berau-fire-watch/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return urllib.request.build_opener(_DropAuthCrossHost).open(urllib.request.Request(url, headers=headers), timeout=timeout)


def http_get_json(url, timeout=60):
    with _open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"


def download(url, token, dest, timeout=180):
    """Unduh ke dest (tulis atomik). Menolak isi yang bukan HDF5, misalnya halaman login."""
    try:
        with _open(url, token, timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError("token Earthdata ditolak (HTTP %d): kedaluwarsa, salah, atau akun belum menyetujui aplikasi LP DAAC" % e.code)
        raise
    if not data.startswith(HDF5_MAGIC):
        raise AuthError("unduhan bukan berkas HDF5 (kemungkinan halaman login): token tidak diterima")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".part")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------- katalog CMR
TITLE_RE = re.compile(r"^%s\.A(\d{4})(\d{3})\.h(\d{2})v(\d{2})\.(\d{3})\.(\d+)$" % SHORT_NAME)


def parse_cmr(feed):
    """Respons CMR granules.json -> daftar granul terbaru dulu, satu per (tanggal, tile)."""
    best = {}
    for e in (feed.get("feed") or {}).get("entry", []):
        m = TITLE_RE.match(e.get("title", ""))
        if not m or m.group(5) != VERSION:
            continue
        url = next((l.get("href") for l in e.get("links", [])
                    if str(l.get("rel", "")).endswith("/data#") and str(l.get("href", "")).startswith("https")
                    and str(l.get("href", "")).endswith(".h5")), None)
        if not url:
            continue
        d = date(int(m.group(1)), 1, 1) + timedelta(days=int(m.group(2)) - 1)
        key = (d, int(m.group(3)), int(m.group(4)))
        g = {"title": e["title"], "date": d, "tile": (int(m.group(3)), int(m.group(4))), "url": url, "prod": int(m.group(6))}
        if key not in best or g["prod"] > best[key]["prod"]:
            best[key] = g
    return sorted(best.values(), key=lambda g: (g["date"], g["tile"]), reverse=True)


def latest_granules(points, limit=40):
    lats = [p[1] for p in points]
    lons = [p[2] for p in points]
    bbox = "%.3f,%.3f,%.3f,%.3f" % (min(lons) - 0.05, min(lats) - 0.05, max(lons) + 0.05, max(lats) + 0.05)
    q = urllib.parse.urlencode({"short_name": SHORT_NAME, "version": VERSION, "bounding_box": bbox,
                                "sort_key": "-start_date", "page_size": limit})
    return parse_cmr(http_get_json(CMR + "?" + q))


# ---------------------------------------------------------------- baca berkas
def _attr(ds, name, default):
    try:
        v = ds.attrs[name]
        import numpy as np
        return float(np.ravel(v)[0])
    except Exception:
        return default


def read_granule(path):
    """Kembalikan (suhu_K dengan NaN pada piksel tidak valid, nama_lst, nama_qc)."""
    import h5py, numpy as np
    found = {"lst": None, "qc": None}
    with h5py.File(path, "r") as f:
        def visit(name, obj):
            if not isinstance(obj, h5py.Dataset) or obj.ndim != 2 or obj.shape != (N, N):
                return None
            base = name.rsplit("/", 1)[-1].lower()
            if found["lst"] is None and (base in ("lst_1km", "lst") or (base.startswith("lst") and "err" not in base and "night" not in base)):
                found["lst"] = name
            elif found["qc"] is None and (base == "qc" or base.startswith("qc")) and "night" not in base:
                found["qc"] = name
            return None
        f.visititems(visit)
        if not found["lst"]:
            raise ValueError("lapisan LST (%dx%d) tidak ditemukan; jalankan --inspect untuk melihat isi berkas" % (N, N))
        ds = f[found["lst"]]
        dn = ds[()].astype("int64")
        scale = _attr(ds, "scale_factor", SCALE_DEFAULT)
        fill = _attr(ds, "_FillValue", 0)
        vmin = VALID_MIN_DEFAULT
        try:
            vmin = int(np.ravel(ds.attrs["valid_range"])[0])
        except Exception:
            pass
        ok = (dn >= vmin) & (dn != int(fill))
        if found["qc"]:
            qc = f[found["qc"]][()].astype("int64")
            ok &= (qc & 3) <= 1
        kelvin = np.where(ok, dn * scale, np.nan)
    kelvin = np.where((kelvin >= KELVIN_RANGE[0]) & (kelvin <= KELVIN_RANGE[1]), kelvin, np.nan)
    return kelvin, found["lst"], found["qc"]


def sample_granule(path, points, tile):
    """{nama: (celsius, jumlah_piksel)} untuk titik yang berada di tile ini dan punya >= MIN_VALID piksel valid."""
    import numpy as np
    kelvin, _, _ = read_granule(path)
    h, v = tile
    out = {}
    for name, lat, lon in points:
        if tile_of(lat, lon) != (h, v):
            continue
        r, c = pixel_of(lat, lon, h, v)
        if not (0 <= r < N and 0 <= c < N):
            continue
        w = kelvin[max(0, r - 1):r + 2, max(0, c - 1):c + 2]
        vals = w[np.isfinite(w)]
        if vals.size >= MIN_VALID:
            out[name] = (round(float(np.median(vals)) - 273.15, 1), int(vals.size))
    return out


def inspect(path):
    import h5py
    with h5py.File(path, "r") as f:
        def show(name, obj):
            if isinstance(obj, h5py.Dataset):
                a = {k: (v.tolist() if hasattr(v, "tolist") else v) for k, v in obj.attrs.items()
                     if k in ("scale_factor", "add_offset", "_FillValue", "valid_range", "units", "long_name")}
                print("%-70s %-14s %-8s %s" % (name, obj.shape, obj.dtype, a))
            return None
        f.visititems(show)


# ---------------------------------------------------------------- penggerak
def fetch_viirs_lst(points, token, cache_dir=CACHE_DIR, today=None, max_age_days=MAX_AGE_DAYS):
    """points: [(nama, lat, lon)]. Hasil: {"points": [...], "errors": [...], "granules": [judul]}."""
    today = today or datetime.now(timezone.utc).date()
    errors, used, got = [], [], {}
    remaining = {p[0]: p for p in points}
    try:
        grans = latest_granules(points)
    except Exception as e:
        return {"points": [], "errors": ["CMR gagal: %s" % e], "granules": []}
    if not grans:
        errors.append("CMR tidak mengembalikan granul %s untuk area ini" % SHORT_NAME)
    for g in grans:
        if not remaining:
            break
        if (today - g["date"]).days > max_age_days:
            break
        dest = os.path.join(cache_dir, g["title"] + ".h5")
        try:
            if not os.path.exists(dest):
                download(g["url"], token, dest)
            vals = sample_granule(dest, list(remaining.values()), g["tile"])
        except AuthError as e:
            errors.append(str(e))
            break  # token bermasalah: tidak ada gunanya mencoba granul lain
        except Exception as e:
            errors.append("%s: %s: %s" % (g["title"], type(e).__name__, str(e)[:160]))
            continue
        used.append(g["title"])
        for name, (c, n) in vals.items():
            _, lat, lon = remaining.pop(name)
            got[name] = {"name": name, "lat": lat, "lon": lon, "day_c": c, "day_n": n,
                         "day_date": g["date"].isoformat(), "day_obs_end": g["date"].isoformat(),
                         "day_sat": "S-NPP", "day_prod": "VNP21A1D", "day_kind": "harian"}
    _prune(cache_dir, keep=24)
    return {"points": [got[p[0]] for p in points if p[0] in got], "errors": errors, "granules": used}


def _prune(cache_dir, keep):
    try:
        files = sorted((os.path.join(cache_dir, f) for f in os.listdir(cache_dir) if f.endswith(".h5")),
                       key=os.path.getmtime, reverse=True)
        for f in files[keep:]:
            os.remove(f)
    except OSError:
        pass


def read_token():
    t = os.environ.get("EARTHDATA_TOKEN")
    if t and t.strip():
        return t.strip()
    p = os.path.join(HERE, "earthdata_token.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip() or None
    return None


# ---------------------------------------------------------------- uji
def selftest():
    global latest_granules, download, sample_granule
    import http.server, threading
    import numpy as np
    import h5py

    # 1. Grid: seluruh titik pantau Berau harus jatuh di tile h29v08 (sesuai granul yang dikembalikan katalog NASA)
    with open(os.path.join(HERE, "data", "berau_kecamatan.json"), encoding="utf-8") as f:
        kec = json.load(f)
    for k in kec:
        assert tile_of(k["lat"], k["lon"]) == (29, 8), k
        r, c = pixel_of(k["lat"], k["lon"], 29, 8)
        assert 1 <= r < N - 1 and 1 <= c < N - 1, (k, r, c)
    assert pixel_of(2.1445, 117.4952, 29, 8) == (942, 889)  # hitung manual: x=13.0557 juta m, y=238,5 ribu m -> kolom 889,5, baris 942,7 (baris, kolom)

    # 2. Katalog CMR: bentuk respons seperti yang dikembalikan NASA (judul dan tautan nyata dari 11 Okt 2026)
    def entry(title, tile="h29v08"):
        base = "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/VNP21A1D.002/%s/%s.h5" % (title, title)
        return {"title": title, "links": [
            {"rel": "http://esipfed.org/ns/fedsearch/1.1/data#", "href": base},
            {"rel": "http://esipfed.org/ns/fedsearch/1.1/s3#", "href": "s3://lp-prod-protected/x.h5"},
            {"rel": "http://esipfed.org/ns/fedsearch/1.1/metadata#", "href": "https://example.org/meta"}]}
    feed = {"feed": {"entry": [entry("VNP21A1D.A2026284.h29v08.002.2026286234254"),
                               entry("VNP21A1D.A2026283.h29v08.002.2026286220621"),
                               entry("VNP21A1D.A2026283.h29v08.002.2026287000000"),   # produksi ulang: yang lebih baru dipakai
                               entry("VNP21A1D.A2026282.h29v08.001.2026286193000"),   # versi lain: dibuang
                               {"title": "bukan-granul", "links": []}]}}
    gs = parse_cmr(feed)
    assert [g["title"] for g in gs] == ["VNP21A1D.A2026284.h29v08.002.2026286234254", "VNP21A1D.A2026283.h29v08.002.2026287000000"]
    assert gs[0]["date"] == date(2026, 10, 11) and gs[0]["tile"] == (29, 8) and gs[0]["url"].endswith(".h5")

    # 3. Baca berkas HDF5 sintetis: struktur mirip HDF-EOS5, LST_1KM (uint16, skala 0,02), QC, piksel awan, isian kosong
    tmp = tempfile.mkdtemp(prefix="lstsel_")
    path = os.path.join(tmp, "VNP21A1D.A2026284.h29v08.002.1.h5")
    lat, lon = 2.1445, 117.4952
    r, c = pixel_of(lat, lon, 29, 8)
    dn = np.zeros((N, N), dtype="uint16")
    qc = np.full((N, N), 3, dtype="uint16")
    dn[r - 1:r + 2, c - 1:c + 2] = 15250          # 305,0 K = 31,85 C
    qc[r - 1:r + 2, c - 1:c + 2] = 0
    dn[r, c] = 16000; qc[r, c] = 1                # nominal: ikut, tetapi median tetap 15250
    dn[r - 1, c - 1] = 15900; qc[r - 1, c - 1] = 2   # terkontaminasi awan: dibuang
    dn[r + 1, c + 1] = 0                          # isian kosong: dibuang
    with h5py.File(path, "w") as f:
        g = f.create_group("HDFEOS/GRIDS/VIIRS_Grid_Daily_1km_LST21/Data Fields")
        d = g.create_dataset("LST_1KM", data=dn); d.attrs["scale_factor"] = np.array([0.02]); d.attrs["_FillValue"] = np.array([0], dtype="uint16")
        g.create_dataset("QC", data=qc)
        g.create_dataset("Emis_14", data=dn)       # lapisan lain tidak boleh tertukar
    kel, nl, nq = read_granule(path)
    assert nl.endswith("LST_1KM") and nq.endswith("QC")
    out = sample_granule(path, [("Uji", lat, lon), ("DiLuarTile", -1.0, 100.0)], (29, 8))
    assert out == {"Uji": (31.9, 7)}, out          # median(15250 x6 dan 16000)=15250 -> 305,0 K -> 31,85 -> 31,9 ; 7 piksel valid
    kel0 = np.nanmedian(kel[r - 1:r + 2, c - 1:c + 2])
    assert abs(kel0 - 305.0) < 1e-6
    # terlalu sedikit piksel valid -> titik dilewati
    qc2 = qc.copy(); qc2[r - 1:r + 2, c - 1:c + 2] = 2; qc2[r, c] = 0; qc2[r, c + 1] = 0
    path2 = os.path.join(tmp, "VNP21A1D.A2026283.h29v08.002.1.h5")
    with h5py.File(path2, "w") as f:
        g = f.create_group("Data Fields")
        g.create_dataset("LST_1KM", data=dn); g.create_dataset("QC", data=qc2)
    assert sample_granule(path2, [("Uji", lat, lon)], (29, 8)) == {}
    # bentuk tak terduga -> galat yang jelas
    path3 = os.path.join(tmp, "x.h5")
    with h5py.File(path3, "w") as f:
        f.create_dataset("LST_1KM", data=np.zeros((10, 10), dtype="uint16"))
    try:
        read_granule(path3); raise SystemExit("seharusnya gagal")
    except ValueError as e:
        assert "--inspect" in str(e)

    # 4. Pengalihan: token dikirim ke host asal, tetapi dibuang saat dialihkan ke host lain
    seen = {}
    class B(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            seen["b"] = self.headers.get("Authorization")
            body = HDF5_MAGIC + b"data"
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    class A(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            seen["a"] = self.headers.get("Authorization")
            if self.path == "/login":
                body = b"<html>login</html>"
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if self.path == "/denied":
                self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_response(302); self.send_header("Location", "http://127.0.0.1:%d/obj" % sb.server_port); self.end_headers()
    sb = http.server.HTTPServer(("127.0.0.1", 0), B); sa = http.server.HTTPServer(("127.0.0.1", 0), A)
    for s in (sb, sa):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        dest = os.path.join(tmp, "dl", "f.h5")
        download("http://localhost:%d/file" % sa.server_port, "TOK", dest)     # localhost -> 127.0.0.1 = host berbeda
        assert seen["a"] == "Bearer TOK" and seen["b"] is None, seen
        assert open(dest, "rb").read().startswith(HDF5_MAGIC)
        for p in ("/login", "/denied"):
            try:
                download("http://localhost:%d%s" % (sa.server_port, p), "TOK", os.path.join(tmp, "dl", "g.h5")); raise SystemExit("seharusnya ditolak")
            except AuthError:
                pass
        assert not os.path.exists(os.path.join(tmp, "dl", "g.h5"))
    finally:
        sa.shutdown(); sb.shutdown()

    # 5. Penggerak: granul terbaru berawan, yang lebih lama bebas awan; satu unduhan gagal; token ditolak menghentikan proses
    calls = []
    real = (latest_granules, download, sample_granule)
    today = date(2026, 10, 15)
    gl = [{"title": "G%d" % i, "date": today - timedelta(days=d), "tile": (29, 8), "url": "u", "prod": 1} for i, d in enumerate([4, 5, 6, 12])]
    try:
        latest_granules = lambda pts, limit=40: gl
        def dl(url, token, dest, timeout=180):
            calls.append(os.path.basename(dest)); os.makedirs(os.path.dirname(dest), exist_ok=True); open(dest, "wb").write(b"x"); return dest
        download = dl
        def sg(path, pts, tile):
            n = os.path.basename(path)
            if n.startswith("G0"): return {}                      # 11 Okt: semua awan
            if n.startswith("G1"): raise ValueError("rusak")      # 10 Okt: berkas rusak
            return {p[0]: (33.3, 9) for p in pts}                  # 9 Okt: bagus
        sample_granule = sg
        res = fetch_viirs_lst([("A", 2.1, 117.4), ("B", 1.3, 118.3)], "T", cache_dir=os.path.join(tmp, "c"), today=today)
        assert [p["day_date"] for p in res["points"]] == ["2026-10-09", "2026-10-09"] and res["points"][0]["day_c"] == 33.3
        assert "G3" not in calls and len(res["errors"]) == 1 and "rusak" in res["errors"][0]   # G3 berumur 12 hari: tidak diambil
        def dl_auth(url, token, dest, timeout=180):
            raise AuthError("token ditolak")
        download = dl_auth
        res = fetch_viirs_lst([("A", 2.1, 117.4)], "T", cache_dir=os.path.join(tmp, "c2"), today=today)
        assert res["points"] == [] and res["errors"] == ["token ditolak"]
    finally:
        latest_granules, download, sample_granule = real
    print("lst_viirs selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--inspect" in sys.argv:
        inspect(sys.argv[sys.argv.index("--inspect") + 1])
    else:
        tok = read_token()
        if not tok:
            sys.exit("EARTHDATA_TOKEN belum diatur (lihat petunjuk di bagian atas berkas ini).")
        with open(os.path.join(HERE, "data", "berau_kecamatan.json"), encoding="utf-8") as f:
            pts = [(k["name"], k["lat"], k["lon"]) for k in json.load(f)]
        r = fetch_viirs_lst(pts, tok)
        print("Granul dipakai:", ", ".join(r["granules"]) or "-")
        for p in r["points"]:
            print("  %-18s %5.1f C  (%d piksel, %s)" % (p["name"], p["day_c"], p["day_n"], p["day_date"]))
        print("Titik tanpa data valid:", ", ".join(n for n, _, _ in pts if n not in {p["name"] for p in r["points"]}) or "-")
        for e in r["errors"]:
            print("  galat:", e)
