# Menerbitkan Berau Fire Watch di GitHub Pages

Situs berjalan tanpa server sendiri. GitHub Actions mengambil hotspot NASA FIRMS dan cuaca/kualitas udara
Open-Meteo tiap sekitar 30 menit, lalu menerbitkan ulang situs. Kunci FIRMS hanya ada di GitHub Secrets.

## 1. Siapkan repositori
1. Buat akun GitHub (bila belum), lalu buat repositori baru, mis. `berau-fire-watch`.
2. Unggah isi folder `D:\fire_berau` ke repositori (tombol **Add file > Upload files**, atau Git).
   Folder `.github/workflows/` (berisi `deploy.yml`) harus ikut. Berkas `firms_key.txt` **tidak** diunggah
   (sudah dikecualikan oleh `.gitignore`; bila memakai unggah lewat web, jangan pilih berkas itu).
3. Pastikan cabang utamanya bernama `main`.

## 2. Simpan kunci FIRMS sebagai Secret
Buat **kunci FIRMS baru** (kunci lama pernah tampil di percakapan): https://firms.modaps.eosdis.nasa.gov/api/map_key/
Lalu di repositori: **Settings > Secrets and variables > Actions > New repository secret**
- Name: `FIRMS_MAP_KEY`
- Secret: kunci FIRMS yang baru

## 3. Aktifkan Pages
**Settings > Pages > Build and deployment > Source: GitHub Actions.**

## 4. Jalankan pertama kali
Tab **Actions > Perbarui dan terbitkan > Run workflow**. Setelah hijau, alamat situs muncul di langkah
terakhir dan di Settings > Pages (biasanya `https://NAMA-AKUN.github.io/berau-fire-watch/`).
Sesudahnya situs diperbarui otomatis tiap sekitar 30 menit.

## 5. Menampilkan di website prodi
Cara paling mudah, tautan: `<a href="https://NAMA-AKUN.github.io/berau-fire-watch/">Berau Fire Watch</a>`

Atau disematkan:
```html
<iframe src="https://NAMA-AKUN.github.io/berau-fire-watch/" title="Berau Fire Watch"
        style="width:100%;height:85vh;border:0" loading="lazy"></iframe>
```
Bila domain prodi memakai HTTPS (disarankan), alamat Pages juga HTTPS sehingga tidak diblokir browser.

## Catatan penting
- **Repositori publik** gratis dan tanpa batas menit Actions, tetapi semua berkas (termasuk batas desa, gambut,
  dan PL KLHK di folder `data/`) menjadi publik. Pastikan data itu boleh disebarkan. Repositori privat dengan
  Pages memerlukan paket GitHub berbayar.
- GitHub **menonaktifkan jadwal otomatis** pada repositori publik yang tidak ada aktivitas selama 60 hari.
  Bila itu terjadi, aktifkan lagi di tab Actions (atau commit kecil apa pun).
- Jadwal GitHub dapat tertunda beberapa menit sampai lebih dari sejam pada jam sibuk. Dashboard menampilkan
  waktu data dan memberi tanda bila data lebih dari beberapa jam.
- Bila FIRMS atau Open-Meteo gagal, jalannya Actions ditandai merah dan situs tetap menampilkan data terakhir.
  Aktifkan notifikasi email GitHub untuk mengetahuinya.
- Peta dasar memakai server tile OpenStreetMap. Kebijakannya melarang pemakaian berat; untuk lalu lintas
  rendah (situs prodi) wajar, tetapi bila pengunjung banyak ganti ke penyedia tile dengan kunci sendiri.
- Perbarui data statis (batas desa, gambut, PL) dengan menjalankan `prepare_geodata.py` dan
  `prepare_landcover.py` di komputer, lalu unggah ulang berkas `data/` yang berubah.
