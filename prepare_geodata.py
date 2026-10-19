#!/usr/bin/env python3
"""Siapkan data spasial Berau untuk dashboard Berau Fire Watch.

Masukan (GeoJSON, koordinat lon/lat WGS84 / CRS84):
  --boundary  batas kampung/desa (punya atribut NAMOBJ dan WADMKC)
  --peat      lapisan gambut (boleh belum dipotong; akan dipotong ke batas Berau)

Keluaran di folder --out (default: data/):
  berau_batas_kampung.geojson  batas desa, disederhanakan, + luas dan persen gambut
  berau_gambut.geojson         gambut terpotong batas Berau, disederhanakan
  berau_kecamatan.json         titik pantau per kecamatan (untuk dashboard dan update_data.py)

Butuh: pip install shapely
Contoh: python3 prepare_geodata.py --boundary batas_kampung.geojson --peat gambut_berau.geojson
Bila gambut masih berformat SHP, ubah dulu ke GeoJSON, misalnya:
  ogr2ogr -f GeoJSON -t_srs EPSG:4326 gambut.geojson gambut.shp
"""
import argparse, collections, json, math, os, sys
from datetime import date
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from shapely.validation import make_valid
from shapely import set_precision


def load(path):
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)
    out = []
    for ft in fc["features"]:
        if not ft.get("geometry"):
            continue
        g = shape(ft["geometry"])
        x0, y0, x1, y1 = g.bounds
        if not (-180 <= x0 <= 180 and -90 <= y0 <= 90 and -180 <= x1 <= 180 and -90 <= y1 <= 90):
            sys.exit("Koordinat di %s bukan lon/lat derajat. Ubah dulu ke EPSG:4326." % path)
        out.append((make_valid(g) if not g.is_valid else g, ft.get("properties") or {}))
    return out


def polys(g):
    """Ambil hanya bagian poligon (hasil intersect bisa memuat garis/titik)."""
    if g.is_empty:
        return g
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    parts = [p for p in getattr(g, "geoms", []) if p.geom_type in ("Polygon", "MultiPolygon")]
    return unary_union(parts) if parts else g.__class__()


def area_ha(g):
    """Luas kasar (hektare) dari derajat: cukup akurat di sekitar ekuator."""
    if g.is_empty:
        return 0.0
    lat = g.centroid.y
    kx, ky = 111.320 * math.cos(math.radians(lat)), 110.574  # km per derajat
    return sum(_ring_area(p) for p in (g.geoms if hasattr(g, "geoms") else [g])) * kx * ky * 100


def _ring_area(p):
    return p.area  # derajat^2, dikalikan skala km di area_ha


def write(path, features, digits=5):
    fc = {"type": "FeatureCollection", "features": features}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, separators=(",", ":"))
    print("  %-34s %6.0f KB  (%d fitur)" % (os.path.basename(path), os.path.getsize(path) / 1024, len(features)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boundary", required=True)
    ap.add_argument("--peat", required=True)
    ap.add_argument("--out", default="data")
    ap.add_argument("--tol", type=float, default=0.0003, help="toleransi penyederhanaan (derajat, ~33 m)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    bound = load(a.boundary)
    peat = load(a.peat)
    print("Batas: %d desa | Gambut: %d poligon" % (len(bound), len(peat)))

    berau = unary_union([g for g, _ in bound])
    peat_all = unary_union([g for g, _ in peat])
    clipped = polys(peat_all.intersection(berau))
    before, after = area_ha(peat_all), area_ha(clipped)
    print("Gambut sebelum dipotong: %.0f ha | sesudah dipotong ke batas Berau: %.0f ha" % (before, after))
    if before - after > 1:
        print("  %.0f ha gambut berada di luar batas Berau dan dibuang." % (before - after))

    # Atribut per desa: luas dan persen gambut
    feats, per_kec = [], {}
    for g, p in bound:
        pg = polys(g.intersection(clipped))
        ha_peat, ha_all = area_ha(pg), area_ha(g)
        kec = (p.get("WADMKC") or "").title()
        k = per_kec.setdefault(kec, {"geoms": [], "peat": 0.0, "all": 0.0})
        k["geoms"].append(g); k["peat"] += ha_peat; k["all"] += ha_all
        gs = set_precision(g.simplify(a.tol, preserve_topology=True), 1e-5)
        rp = g.representative_point()  # titik di dalam desa, dipakai untuk jarak ke hotspot
        feats.append({"type": "Feature", "geometry": mapping(gs), "properties": {
            "desa": (p.get("NAMOBJ") or "").title(), "kec": kec,
            "lat": round(rp.y, 4), "lon": round(rp.x, 4),
            "luas_ha": round(ha_all), "gambut_ha": round(ha_peat),
            "gambut_pct": round(100 * ha_peat / ha_all, 1) if ha_all else 0}})
    write(os.path.join(a.out, "berau_batas_kampung.geojson"), feats)

    pf = []
    for i, part in enumerate(getattr(clipped, "geoms", [clipped]) if not clipped.is_empty else []):
        gs = set_precision(part.simplify(a.tol / 3, preserve_topology=True), 1e-5)
        if not gs.is_empty:
            pf.append({"type": "Feature", "geometry": mapping(gs),
                       "properties": {"gambut_ha": round(area_ha(part))}})
    write(os.path.join(a.out, "berau_gambut.geojson"), pf)

    pts = []
    for kec, k in sorted(per_kec.items()):
        rp = unary_union(k["geoms"]).representative_point()
        pts.append({"name": kec, "lat": round(rp.y, 4), "lon": round(rp.x, 4),
                    "gambut_pct": round(100 * k["peat"] / k["all"], 2) if k["all"] else 0})
    with open(os.path.join(a.out, "berau_kecamatan.json"), "w", encoding="utf-8") as f:
        json.dump(pts, f, ensure_ascii=False, indent=1)
    print("  berau_kecamatan.json               (%d titik pantau)" % len(pts))

    # Metadata/provenance untuk halaman metode. Isian manual (sumber, instansi, tahun, catatan)
    # dipertahankan bila file sudah ada, jadi cukup diisi sekali.
    meta_path = os.path.join(a.out, "metadata.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        old = {}
    keep = lambda sec, k: (old.get(sec) or {}).get(k, "")
    status = collections.Counter((p.get("keterangan") or "tidak diisi") for _, p in bound)

    def kategori(k):
        t = k.lower()
        if "raperbup" in t:
            return "Rancangan Perbup (RAPERBUP)"
        if "indikatif" in t:
            return "Indikatif"
        if "harmonisasi" in t:
            return "Dalam harmonisasi Perbup"
        if "penetapan" in t or t.startswith("sk.") or t.startswith("perbup"):
            return "Ditetapkan (SK/Perbup Bupati)"
        return "Tidak diisi" if t == "tidak diisi" else "Lainnya"
    ringkas = collections.Counter()
    for k, n in status.items():
        ringkas[kategori(k)] += n
    meta = {
        "diproses": date.today().isoformat(),
        "gambut": {
            "sumber": keep("gambut", "sumber"), "instansi": keep("gambut", "instansi"),
            "tahun": keep("gambut", "tahun"), "catatan": keep("gambut", "catatan"),
            "jumlah_poligon_asli": len(peat), "luas_asli_ha": round(before),
            "jumlah_poligon_dipotong": len(pf), "luas_dipotong_ha": round(after)},
        "batas_kampung": {
            "sumber": keep("batas_kampung", "sumber"), "instansi": keep("batas_kampung", "instansi"),
            "tahun": keep("batas_kampung", "tahun"), "catatan": keep("batas_kampung", "catatan"),
            "jumlah_desa": len(bound), "jumlah_kecamatan": len(per_kec),
            "status_ringkas": dict(ringkas.most_common()),
            "status_penetapan": dict(status.most_common())},
    }
    for k, v in old.items():  # bagian dari skrip lain (mis. penutup_lahan) dipertahankan
        meta.setdefault(k, v)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print("  metadata.json                      (isi sumber/instansi/tahun secara manual)")

    print("\nDesa yang bersinggungan dengan gambut:")
    for ft in sorted(feats, key=lambda x: -x["properties"]["gambut_ha"]):
        q = ft["properties"]
        if q["gambut_ha"] > 0:
            print("  %-22s kec. %-16s %6d ha (%.1f%% desa)" % (q["desa"], q["kec"], q["gambut_ha"], q["gambut_pct"]))


if __name__ == "__main__":
    main()
