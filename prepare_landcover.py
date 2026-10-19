#!/usr/bin/env python3
"""Siapkan penutup lahan KLHK (PL 2025, skala 1:50.000) untuk dashboard Berau Fire Watch.

Masukan  : --pl         GeoJSON PL (atribut NAMA_LAHAN, PL2025_ID_), lon/lat WGS84
           --boundary   batas kampung/desa (sama dengan prepare_geodata.py)
Keluaran : data/berau_penutup_lahan.geojson  poligon digabung per kelas, dipotong batas Berau, disederhanakan
           data/berau_lahan_desa.json        komposisi kelas (ha dan persen) per desa dan per kecamatan
Memperbarui data/metadata.json (bagian penutup_lahan), isian manual dipertahankan.
Butuh: pip install shapely
"""
import argparse, json, math, os, sys
from datetime import date
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from shapely.validation import make_valid
from shapely import set_precision
from shapely.strtree import STRtree
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prepare_geodata import polys, area_ha

# kelompok tampilan: id kelas PL -> (kelompok, warna). Warna adalah pilihan tampilan.
GROUP = {
 2001: "Hutan lahan kering", 2002: "Hutan lahan kering", 2006: "Hutan tanaman",
 2004: "Mangrove", 20041: "Mangrove", 20051: "Rawa", 50011: "Rawa",
 2007: "Semak belukar", 20071: "Semak belukar",
 2010: "Perkebunan", 20091: "Pertanian", 20092: "Pertanian", 20093: "Pertanian", 20094: "Tambak",
 2012: "Permukiman", 20121: "Permukiman", 20122: "Permukiman",
 2014: "Lahan terbuka", 20141: "Pertambangan", 5001: "Tubuh air"}
COLOR = {"Hutan lahan kering": "#2f6b3a", "Hutan tanaman": "#5f8f3f", "Mangrove": "#3a8f7a", "Rawa": "#6a8fa8",
         "Semak belukar": "#b4b04a", "Perkebunan": "#c98a2b", "Pertanian": "#d9c26a", "Tambak": "#7fb5c8",
         "Permukiman": "#c0392b", "Lahan terbuka": "#d8b48a", "Pertambangan": "#7a6a8a", "Tubuh air": "#4a90c8"}


def load(path):
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)
    return [(make_valid(shape(x["geometry"])), x.get("properties") or {}) for x in fc["features"] if x.get("geometry")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pl", required=True)
    ap.add_argument("--boundary", required=True)
    ap.add_argument("--out", default="data")
    ap.add_argument("--tol", type=float, default=0.0004, help="toleransi penyederhanaan (derajat, ~44 m)")
    a = ap.parse_args()

    bound = load(a.boundary)
    pl = load(a.pl)
    berau = unary_union([g for g, _ in bound])
    print("Batas: %d desa | PL: %d poligon" % (len(bound), len(pl)))
    unknown = {p.get("PL2025_ID_") for _, p in pl} - set(GROUP)
    if unknown:
        sys.exit("Kelas PL belum dipetakan ke kelompok: %s" % sorted(unknown))

    # 1) potong PL ke batas Berau, kumpulkan per kelompok
    tree_b = STRtree([berau]); by = {}
    asli_ha = sum(area_ha(g) for g, _ in pl)
    for g, p in pl:
        if not g.intersects(berau):
            continue
        c = polys(g.intersection(berau))
        if c.is_empty:
            continue
        by.setdefault(GROUP[p["PL2025_ID_"]], []).append((c, p))
    parts = {k: polys(unary_union([c for c, _ in v])) for k, v in by.items()}
    potong_ha = sum(area_ha(g) for g in parts.values())
    berau_ha = area_ha(berau)
    print("PL asli %.0f ha | di dalam Berau %.0f ha | luas batas desa %.0f ha | selisih %.1f%%" %
          (asli_ha, potong_ha, berau_ha, 100 * (potong_ha - berau_ha) / berau_ha))

    # 2) komposisi per desa (dari geometri tanpa disederhanakan)
    names = sorted(parts)
    trees = {k: STRtree([parts[k]]) for k in names}
    desa, kec = [], {}
    for g, p in bound:
        tot = area_ha(g); comp = {}
        for k in names:
            if not parts[k].intersects(g):
                continue
            ha = area_ha(polys(parts[k].intersection(g)))
            if ha >= 0.5:
                comp[k] = round(ha, 1)
        kn = (p.get("WADMKC") or "").title()
        row = {"desa": (p.get("NAMOBJ") or "").title(), "kec": kn, "luas_ha": round(tot),
               "kelas_ha": comp, "tertutup_pct": round(100 * sum(comp.values()) / tot, 1) if tot else 0}
        desa.append(row)
        kk = kec.setdefault(kn, {"luas_ha": 0.0, "kelas_ha": {}})
        kk["luas_ha"] += tot
        for k, v in comp.items():
            kk["kelas_ha"][k] = kk["kelas_ha"].get(k, 0) + v
    for kn, kk in kec.items():
        kk["luas_ha"] = round(kk["luas_ha"]); kk["kelas_ha"] = {k: round(v) for k, v in kk["kelas_ha"].items()}
    out = {"tahun": 2025, "skala": "1:50.000", "kelompok": {k: COLOR[k] for k in names},
           "desa": desa, "kecamatan": kec}
    with open(os.path.join(a.out, "berau_lahan_desa.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    # 3) lapisan peta: satu fitur per kelompok (MultiPolygon), disederhanakan
    feats = []
    for k in names:
        g = set_precision(parts[k].simplify(a.tol, preserve_topology=True), 1e-5)
        g = polys(g)
        if g.is_empty:
            continue
        feats.append({"type": "Feature", "geometry": mapping(g),
                      "properties": {"kelas": k, "warna": COLOR[k], "ha": round(area_ha(parts[k]))}})
    path = os.path.join(a.out, "berau_penutup_lahan.geojson")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False, separators=(",", ":"))
    print("  %s %.0f KB" % (os.path.basename(path), os.path.getsize(path) / 1024))

    # 4) metadata
    mp = os.path.join(a.out, "metadata.json")
    try:
        meta = json.load(open(mp, encoding="utf-8"))
    except Exception:
        meta = {}
    old = meta.get("penutup_lahan") or {}
    meta["penutup_lahan"] = {
        "sumber": old.get("sumber") or "Penutupan Lahan 2025 skala 1:50.000 (berkas PL2025_AR_50K)",
        "instansi": old.get("instansi") or "KLHK (sesuai keterangan pengguna)",
        "tahun": old.get("tahun") or "2025", "catatan": old.get("catatan", ""),
        "jumlah_poligon_asli": len(pl), "luas_asli_ha": round(asli_ha), "luas_dalam_berau_ha": round(potong_ha),
        "diproses": date.today().isoformat(),
        "luas_per_kelompok_ha": {k: round(area_ha(parts[k])) for k in names}}
    json.dump(meta, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nLuas per kelompok di Berau:")
    for k in sorted(names, key=lambda k: -area_ha(parts[k])):
        print("  %-20s %9.0f ha  %4.1f%%" % (k, area_ha(parts[k]), 100 * area_ha(parts[k]) / potong_ha))


if __name__ == "__main__":
    main()
