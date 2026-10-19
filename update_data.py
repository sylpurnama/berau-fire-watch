#!/usr/bin/env python3
"""Pembaruan data Berau Fire Watch (hanya pustaka standar Python).

Tugas:
  1. Hotspot NASA FIRMS (VIIRS SNPP, NOAA-20, NOAA-21), 2 hari terakhir -> hotspots.json
  2. Cuaca dan polutan udara (Open-Meteo), hitung skor risiko dan ISPU estimasi
  3. Kirim alert Telegram bila risiko naik ke Tinggi/Ekstrem atau ISPU masuk kategori Tidak Sehat

Variabel lingkungan:
  FIRMS_MAP_KEY       kunci gratis dari https://firms.modaps.eosdis.nasa.gov/api/map_key/
  TELEGRAM_BOT_TOKEN  token dari @BotFather
  TELEGRAM_CHAT_ID    ID chat/grup/kanal tujuan

Pemakaian:
  python update_data.py            # jalan normal
  python update_data.py --dry      # cetak alert, tidak kirim
  python update_data.py --publish DIR  # tulis hotspots.json dan conditions.json ke DIR (dipakai GitHub Actions)
  python update_data.py --selftest # uji tanpa internet

Untuk dashboard langsung (hotspot realtime) jalankan server.py, bukan skrip ini.
Jadwalkan skrip ini tiap 1-3 jam bila ingin alert Telegram.
"""
import csv, io, json, math, os, sys, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
HOTSPOTS_PATH = os.path.join(HERE, "hotspots.json")


def _load_json(path, hint):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit("%s belum ada. %s" % (os.path.relpath(path, HERE), hint))


# Titik pantau (satu per kecamatan): berkas yang sama dengan yang dipakai dashboard.
_kec = _load_json(os.path.join(HERE, "data", "berau_kecamatan.json"), "Jalankan prepare_geodata.py dulu.")
LOCS = [(r["name"], r["lat"], r["lon"]) for r in _kec]
PEAT = {r["name"]: r.get("gambut_pct", 0) for r in _kec}
# Ambang ISPU: berkas yang sama dengan yang dipakai dashboard (memuat status verifikasi per parameter).
ISPU = _load_json(os.path.join(HERE, "data", "ispu_breakpoints.json"), "Berkas ambang ISPU diperlukan.")

LEVEL_NAMES = ["Rendah", "Sedang", "Tinggi", "Ekstrem"]
FIRMS_BBOX = "116,0.9,119.2,2.7"  # Kabupaten Berau


def clamp(x):
    return max(0.0, min(1.0, x))


def level(score):
    return 0 if score < 30 else 1 if score < 50 else 2 if score < 70 else 3


def risk_score(temp, rh, wind, dry_days, soil, near_hotspots, peat_pct=0):
    """Sama dengan fungsi score() di index.html. 'soil' = suhu tanah 0 cm dari model."""
    s = 100 * (0.25 * clamp((temp - 26) / 10) + 0.30 * clamp((80 - rh) / 40)
               + 0.15 * clamp(wind / 30) + 0.20 * clamp(dry_days / 7)
               + 0.10 * (0 if soil is None else clamp((soil - 28) / 14)))
    s += min(20, near_hotspots * 5) + min(10, peat_pct * 2)
    return round(min(100, s))


def haversine(a, b, c, d):
    r = math.pi / 180
    x, y = (c - a) * r, (d - b) * r
    h = math.sin(x / 2) ** 2 + math.cos(a * r) * math.cos(c * r) * math.sin(y / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def get(url, timeout=30, accept=None):
    headers = {"User-Agent": "berau-fire-watch/1.0"}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def as_list(x):
    return x if isinstance(x, list) else [x]


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _short(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            body = ""
        return ("HTTP %s %s" % (e.code, body)).strip()
    return ("%s: %s" % (type(e).__name__, e))[:200]


# ---------------------------------------------------------------- ISPU
AQ_VARS = {"pm25": "pm2_5", "pm10": "pm10", "o3": "ozone", "no2": "nitrogen_dioxide",
           "so2": "sulphur_dioxide", "co": "carbon_monoxide"}


def ispu_sub(key, conc, cfg=None):
    """Sub-indeks ISPU satu parameter: I = (Ia-Ib)/(Xa-Xb) * (Xx-Xb) + Ib (Permen LHK P.14/2020).
    Ib = 0 pada pita pertama, selanjutnya batas atas pita sebelumnya + 1."""
    cfg = cfg or ISPU
    if conc is None or conc < 0:
        return None
    xs = [0.0] + list(cfg["parameter"][key]["batas"])
    top = cfg["batas_ispu"]
    if conc >= xs[-1]:
        return float(top[-1])
    for k in range(1, len(xs)):
        if conc <= xs[k]:
            ia = top[k - 1]
            ib = 0 if k == 1 else top[k - 2] + 1
            xa, xb = xs[k], xs[k - 1]
            return (ia - ib) / (xa - xb) * (conc - xb) + ib
    return None


def ispu_usable(key, cfg=None):
    """Parameter ikut menentukan ISPU hanya bila ambangnya terverifikasi (primer atau sekunder)."""
    cfg = cfg or ISPU
    return cfg["parameter"][key].get("verifikasi") in cfg.get("dipakai_untuk_ispu", ["primer", "sekunder"])


def window_mean(series, idx, n, min_frac=0.75):
    """Rata-rata n jam terakhir sampai indeks idx; butuh data minimal 75% dari jendela."""
    w = [v for v in series[max(0, idx - n + 1): idx + 1] if v is not None]
    return sum(w) / len(w) if len(w) >= max(1, math.ceil(n * min_frac)) else None


def compute_ispu(hourly, idx, cfg=None):
    """ISPU = sub-indeks terbesar dari parameter yang tersedia. 'hourly' berformat Open-Meteo."""
    cfg = cfg or ISPU
    conc, sub = {}, {}
    for key, var in AQ_VARS.items():
        c = window_mean(hourly.get(var) or [], idx, cfg["parameter"][key]["rata_rata_jam"])
        conc[key] = c
        sub[key] = ispu_sub(key, c, cfg)
        if not ispu_usable(key, cfg):
            sub[key] = None  # ditampilkan sebagai konsentrasi, tidak dihitung ke ISPU
    valid = {k: v for k, v in sub.items() if v is not None}
    if not valid:
        return None
    top = max(valid, key=valid.get)
    return {"ispu": round(valid[top]), "param": top, "conc": conc, "sub": sub}


def ispu_category(v, cfg=None):
    cfg = cfg or ISPU
    for k in cfg["kategori"]:
        if v <= k["max"]:
            return k["nama"]
    return cfg["kategori"][-1]["nama"]


# ---------------------------------------------------------------- Hotspot
SOURCES = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]
SKIP_CONF = {"l", "low"}  # keyakinan rendah dibuang (praktik umum untuk VIIRS)


def fetch_hotspots(key):
    """Gabungkan 3 satelit VIIRS. Deteksi di sel ~1 km pada tanggal yang sama dianggap satu (ambil FRP terbesar)."""
    best, stats = {}, {}
    for src in SOURCES:
        try:
            url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{src}/{FIRMS_BBOX}/2"
            rows = list(csv.DictReader(io.StringIO(get(url))))
            found = [{"lat": float(r["latitude"]), "lon": float(r["longitude"]),
                      "date": r["acq_date"], "time": r["acq_time"],
                      "conf": r["confidence"], "frp": r["frp"], "src": src.replace("_NRT", "")}
                     for r in rows]
        except Exception as e:
            print(f"FIRMS {src} gagal: {_short(e)}", file=sys.stderr)
            continue
        kept = 0
        for h in found:
            if h["conf"].strip().lower() in SKIP_CONF:
                continue
            kept += 1
            k = (round(h["lat"], 2), round(h["lon"], 2), h["date"])
            if k not in best or float(h["frp"]) > float(best[k]["frp"]):
                best[k] = h
        stats[src] = {"raw": len(found), "kept": kept}
        print(f"  {src}: {len(found)} deteksi ({kept} setelah buang keyakinan rendah)")
    return list(best.values()), stats


def hotspot_payload(hs, stats):
    return {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window_days": 2, "bbox": FIRMS_BBOX, "min_confidence": "nominal (low dibuang)",
            "sources": stats, "hotspots": hs}


def cells(hs):
    """Jumlah kelompok hotspot (sel ~5 km), supaya piksel berdekatan tidak dihitung berulang."""
    return len({(round(h["lat"] / 0.05), round(h["lon"] / 0.05)) for h in hs})


def load_cached_hotspots(max_age_h=12):
    try:
        with open(HOTSPOTS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(d["updated"])).total_seconds() / 3600
        return d.get("hotspots", []) if age <= max_age_h else []
    except Exception:
        return []


# ---------------------------------------------------------------- Cuaca dan udara
def cond_urls():
    la = ",".join(str(l[1]) for l in LOCS)
    lo = ",".join(str(l[2]) for l in LOCS)
    w = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
         "&current=temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation&hourly=soil_temperature_0cm"
         "&daily=precipitation_sum&past_days=7&forecast_days=1"
         "&wind_speed_unit=kmh&timezone=Asia%%2FMakassar" % (la, lo))
    a = ("https://air-quality-api.open-meteo.com/v1/air-quality?latitude=%s&longitude=%s"
         "&current=pm2_5&hourly=pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone"
         "&past_days=1&forecast_days=1&timezone=Asia%%2FMakassar" % (la, lo))
    return w, a


def publish(out_dir):
    """Tulis hotspots.json dan conditions.json (respons mentah Open-Meteo) ke out_dir untuk hosting statis.
    Gagal (kode keluar 1) bila FIRMS atau cuaca tidak dapat diambil, supaya situs yang sedang tayang tidak ditimpa data kosong."""
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if not key:
        sys.exit("FIRMS_MAP_KEY belum diatur (di GitHub: Settings > Secrets and variables > Actions).")
    hs, stats = fetch_hotspots(key)
    if not stats:
        sys.exit("Semua sumber FIRMS gagal dijangkau; tidak ada yang dipublikasikan.")
    w, a = cond_urls()
    try:
        W = json.loads(get(w))
    except Exception as e:
        sys.exit("Open-Meteo (cuaca) gagal: %s" % _short(e))
    try:
        A = json.loads(get(a))
    except Exception as e:
        print("Open-Meteo (kualitas udara) gagal:", _short(e), file=sys.stderr)
        A = None
    os.makedirs(out_dir, exist_ok=True)
    save_json(os.path.join(out_dir, "hotspots.json"), hotspot_payload(hs, stats))
    save_json(os.path.join(out_dir, "conditions.json"),
              {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "weather": W, "air": A})
    print("Dipublikasikan: %d hotspot, cuaca %d lokasi, udara %s." % (len(hs), len(LOCS), "ya" if A else "tidak"))


def fetch_conditions():
    w, a = cond_urls()
    W = as_list(json.loads(get(w)))
    try:
        A = as_list(json.loads(get(a)))
    except Exception as e:
        print("Kualitas udara gagal:", _short(e), file=sys.stderr)
        A = [None] * len(LOCS)
    out = []
    for i, (name, lat, lon) in enumerate(LOCS):
        c, h, d = W[i]["current"], W[i]["hourly"], W[i]["daily"]["precipitation_sum"]
        stamp = c["time"][:13] + ":00"
        soil = h["soil_temperature_0cm"][h["time"].index(stamp)] if stamp in h["time"] else None
        dry = 0
        for v in reversed(d):
            if (v or 0) < 1:
                dry += 1
            else:
                break
        ip = None
        if A[i]:
            ah = A[i]["hourly"]
            sa = ((A[i].get("current") or {}).get("time") or c["time"])[:13] + ":00"
            if sa in ah["time"]:
                ip = compute_ispu(ah, ah["time"].index(sa))
        out.append({"name": name, "lat": lat, "lon": lon, "temp": c["temperature_2m"],
                    "rh": c["relative_humidity_2m"], "wind": c["wind_speed_10m"],
                    "soil": soil, "dry": dry, "ispu": ip})
    return out


def telegram(text, dry):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if dry or not (token and chat):
        print("[alert%s]\n%s\n" % (" (dry)" if dry else " (Telegram belum diatur)", text))
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30)


def main():
    if "--publish" in sys.argv:
        return publish(sys.argv[sys.argv.index("--publish") + 1])
    dry = "--dry" in sys.argv
    hotspots, stats, key = [], {}, os.environ.get("FIRMS_MAP_KEY")
    if not (key and key.strip()):  # cadangan: berkas firms_key.txt di folder ini
        try:
            with open(os.path.join(HERE, "firms_key.txt"), encoding="utf-8-sig") as f:
                key = f.read().strip() or None
        except OSError:
            key = None
    if key:
        try:
            hotspots, stats = fetch_hotspots(key)
        except Exception as e:
            print("FIRMS gagal:", _short(e), file=sys.stderr)
    else:
        print("FIRMS_MAP_KEY belum diatur: hotspot dilewati.", file=sys.stderr)
    if stats:
        save_json(HOTSPOTS_PATH, hotspot_payload(hotspots, stats))
    else:
        # Jangan menimpa berkas lama dengan data kosong, dan jangan menganggap "tidak ada hotspot":
        # pakai data terakhir (maks 12 jam) agar skor dan alert tidak naik-turun karena gangguan jaringan.
        hotspots = load_cached_hotspots()
        print("Tidak ada data FIRMS baru; memakai hotspots.json lama (%d titik)." % len(hotspots), file=sys.stderr)

    try:
        rows = fetch_conditions()
    except Exception as e:
        sys.exit("Gagal mengambil cuaca/kualitas udara dari Open-Meteo: %s\nPeriksa koneksi internet." % _short(e))
    state_path = os.path.join(HERE, "alert_state.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            last = json.load(f)
    except Exception:
        last = {}
    now = {}
    for r in rows:
        near = cells([h for h in hotspots if haversine(r["lat"], r["lon"], h["lat"], h["lon"]) <= 25])
        r["score"] = risk_score(r["temp"], r["rh"], r["wind"], r["dry"], r["soil"], near, PEAT.get(r["name"], 0))
        lv = level(r["score"])
        ip = r["ispu"]
        smoke = ip is not None and ip["ispu"] > 100
        now[r["name"]] = {"level": lv, "smoke": smoke}
        prev = last.get(r["name"], {"level": 0, "smoke": False})
        msgs = []
        if lv >= 2 and lv > prev["level"]:
            msgs.append("Risiko kebakaran lahan %s (skor %d/100)%s.\n"
                        "Jangan membuang puntung rokok sembarangan dan jangan membakar sampah atau lahan."
                        % (LEVEL_NAMES[lv].upper(), r["score"],
                           f", {near} kelompok hotspot dalam 25 km" if near else ""))
        if smoke and not prev["smoke"]:
            msgs.append("Kualitas udara %s (ISPU estimasi %d, penentu %s; perkiraan model CAMS, bukan hasil pengukuran). "
                        "Kurangi aktivitas luar ruangan, gunakan masker N95/KN95 bila harus keluar."
                        % (ispu_category(ip["ispu"]), ip["ispu"], ISPU["parameter"][ip["param"]]["nama"]))
        for m in msgs:
            telegram("Berau Fire Watch - %s\n%s" % (r["name"], m), dry)
    if not dry:
        save_json(state_path, now)
    print("Selesai: %d hotspot, %d wilayah dinilai." % (len(hotspots), len(rows)))


def selftest():
    global get
    # --- skor risiko
    assert risk_score(26, 80, 0, 0, 28, 0) == 0
    assert risk_score(36, 40, 30, 7, 42, 0) == 100
    assert level(29) == 0 and level(30) == 1 and level(50) == 2 and level(70) == 3
    base = round(100 * (0.25 * 0.4 + 0.30 * 0.25 + 0.15 * (10 / 30) + 0.20 * (2 / 7) + 0.10 * (4 / 14)))
    assert risk_score(30, 70, 10, 2, 32, 0) == base
    assert risk_score(30, 70, 10, 2, 32, 2, 3) == base + 10 + 6
    assert risk_score(36, 40, 30, 7, 42, 9, 9) == 100
    assert 0 < haversine(-0.5, 117.1, -0.4, 117.0) < 20
    assert cells([{"lat": 1.188, "lon": 117.144}, {"lat": 1.1884, "lon": 117.147}, {"lat": 1.1885, "lon": 117.149}]) == 1
    assert cells([{"lat": 1.188, "lon": 117.144}, {"lat": 1.30, "lon": 117.144}]) == 2

    # --- ISPU: titik batas, interpolasi, pembatasan, jendela rata-rata
    assert ispu_sub("pm10", 0) == 0 and ispu_sub("pm10", 50) == 50 and ispu_sub("pm10", 150) == 100
    assert abs(ispu_sub("pm10", 100) - 75.5) < 1e-9          # (100-51)/(150-50)*(100-50)+51
    assert ispu_sub("pm25", 15.5) == 50 and ispu_sub("pm25", 600) == 500 and ispu_sub("pm25", None) is None
    assert ispu_category(50) == "Baik" and ispu_category(51) == "Sedang" and ispu_category(150) == "Tidak Sehat"
    assert ispu_category(250) == "Sangat Tidak Sehat" and ispu_category(400) == "Berbahaya"
    hourly = {"pm2_5": [25.0] * 48, "pm10": [30.0] * 48, "ozone": [10.0] * 48, "nitrogen_dioxide": [5.0] * 48,
              "sulphur_dioxide": [2.0] * 48, "carbon_monoxide": [200.0] * 48}
    r = compute_ispu(hourly, 30)
    assert r["param"] == "pm25" and r["ispu"] == 63 and ispu_category(r["ispu"]) == "Sedang"   # 49/39.9*9.5+51
    hourly["pm2_5"] = [25.0] * 20 + [None] * 28            # jendela 24 jam terlalu bolong -> PM2.5 diabaikan
    assert compute_ispu(hourly, 30)["param"] == "pm10"
    assert window_mean([1, 2, 3, 4], 3, 2) == 3.5 and window_mean([None] * 5, 4, 3) is None

    # --- Alur FIRMS tanpa internet: get() diganti data palsu
    real_get, hdr = get, "latitude,longitude,acq_date,acq_time,confidence,frp\n"

    def fake_firms(url, timeout=30, accept=None):
        if "SNPP" in url:
            return hdr + "1.000,117.000,2026-10-13,0457,n,2.0\n1.200,117.200,2026-10-13,0457,l,1.0\n"
        if "NOAA20" in url:
            return hdr + "1.004,117.004,2026-10-13,0457,n,5.0\n"
        raise OSError("diblokir")
    get = fake_firms
    try:
        hs, st = fetch_hotspots("KEY")
    finally:
        get = real_get
    assert len(hs) == 1 and hs[0]["frp"] == "5.0" and hs[0]["src"] == "VIIRS_NOAA20"
    assert st["VIIRS_SNPP_NRT"] == {"raw": 2, "kept": 1} and "VIIRS_NOAA21_NRT" not in st

    print("selftest OK")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
