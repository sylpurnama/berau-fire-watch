#!/usr/bin/env python3
"""Server lokal Berau Fire Watch (hanya pustaka standar Python).

Pengganti `python -m http.server`. Selain menyajikan dashboard, server ini:
  - mengambil hotspot NASA FIRMS langsung dari sumbernya saat dashboard meminta (/api/hotspots),
    dengan cache 10 menit. MAP_KEY hanya dibaca server dan tidak pernah dikirim ke browser.

MAP_KEY dibaca dari variabel lingkungan FIRMS_MAP_KEY, atau dari berkas firms_key.txt di folder ini.
Hanya index.html, metode.html, hotspots.json, serta folder data/ dan assets/ yang disajikan;
berkas lain (skrip, firms_key.txt, alert_state.json) tidak dapat diakses lewat web.

Pemakaian (PowerShell):
  $env:FIRMS_MAP_KEY="isi_key_anda"
  python server.py
lalu buka http://localhost:8000

HOST (bawaan 127.0.0.1) dan PORT (bawaan 8000) dapat diubah lewat variabel lingkungan.
"""
import http.server, json, os, sys, threading, time, urllib.parse

import update_data as ud

HERE = ud.HERE
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
HS_TTL = 600  # detik
ALLOW_FILES = {"/index.html", "/metode.html", "/hotspots.json", "/conditions.json"}
ALLOW_PREFIX = ("/data/", "/assets/")

_lock = threading.Lock()
_cache = {"t": 0.0, "body": None}


def read_key():
    k = os.environ.get("FIRMS_MAP_KEY")
    if k and k.strip():
        return k.strip()
    p = os.path.join(HERE, "firms_key.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as f:  # utf-8-sig: aman bila disimpan Notepad dengan BOM
            return f.read().strip() or None
    return None


def get_hotspots():
    """Hotspot terbaru dari FIRMS. Bila FIRMS gagal tetapi ada cache, kembalikan cache bertanda stale."""
    with _lock:
        if _cache["body"] and time.time() - _cache["t"] < HS_TTL:
            return _cache["body"]
        try:
            key = read_key()
            if not key:
                raise RuntimeError("FIRMS_MAP_KEY belum diatur di server")
            hs, stats = ud.fetch_hotspots(key)
            if not stats:
                raise RuntimeError("semua sumber FIRMS gagal dijangkau")
            body = ud.hotspot_payload(hs, stats)
            _cache.update(t=time.time(), body=body)
            ud.save_json(ud.HOTSPOTS_PATH, body)  # cadangan untuk hosting statis
            return body
        except Exception as e:
            if _cache["body"]:
                return dict(_cache["body"], stale=True, error=str(e))
            raise


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            self.path = "/index.html"
            return True
        return ".." not in path and (path in ALLOW_FILES or path.startswith(ALLOW_PREFIX))

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/hotspots":
            try:
                return self._json(200, get_hotspots())
            except Exception as e:
                return self._json(503, {"error": str(e)})
        if self._allowed():
            return super().do_GET()
        self.send_error(404)

    def do_HEAD(self):
        if self._allowed():
            return super().do_HEAD()
        self.send_error(404)

    def end_headers(self):
        if urllib.parse.urlsplit(self.path).path.endswith(".json"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    print("Berau Fire Watch: http://%s:%d" % ("localhost" if HOST in ("127.0.0.1", "0.0.0.0") else HOST, PORT))
    if not read_key():
        print("Peringatan: FIRMS_MAP_KEY belum diatur. Hotspot langsung tidak tersedia; dashboard memakai hotspots.json bila ada.")
    srv = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBerhenti.")


if __name__ == "__main__":
    main()
