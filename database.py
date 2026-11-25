import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


class DatabaseManager:
    def __init__(self, db_path: str = "firsat_istihbarat.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._tablolari_olustur()

    def _baglanti_al(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _tablolari_olustur(self) -> None:
        with self._lock, self._baglanti_al() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS fiyat_gecmisi (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    urun_anahtari TEXT NOT NULL,
                    kaynak TEXT NOT NULL,
                    fiyat REAL NOT NULL,
                    tarih TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_fiyat_anahtar ON fiyat_gecmisi(urun_anahtari, tarih);

                CREATE TABLE IF NOT EXISTS gonderilen_firsatlar (
                    urun_anahtari TEXT PRIMARY KEY,
                    urun_adi TEXT NOT NULL,
                    son_gonderilen_fiyat REAL NOT NULL,
                    gonderim_tarihi TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def fiyat_kaydet_ve_analiz_et(
        self, urun_anahtari: str, kaynak: str, guncel_fiyat: float
    ) -> Dict[str, Any]:
        """Ürünün son 90 günlük fiyat geçmişini kaydeder; dip, medyan ve kayıt sayısını döndürür."""
        sinir_tarih = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._baglanti_al() as conn:
            conn.execute(
                "INSERT INTO fiyat_gecmisi (urun_anahtari, kaynak, fiyat) VALUES (?, ?, ?)",
                (urun_anahtari, kaynak, guncel_fiyat),
            )
            satirlar = conn.execute(
                "SELECT fiyat FROM fiyat_gecmisi WHERE urun_anahtari = ? AND tarih >= ? ORDER BY fiyat ASC",
                (urun_anahtari, sinir_tarih),
            ).fetchall()

        fiyatlar = [float(r["fiyat"]) for r in satirlar] or [guncel_fiyat]
        min_fiyat = min(fiyatlar)
        medyan_fiyat = fiyatlar[len(fiyatlar) // 2]

        return {
            "kayit_sayisi": len(fiyatlar),
            "db_min_fiyat": round(min_fiyat, 2),
            "db_medyan_fiyat": round(medyan_fiyat, 2),
            "db_dip_mi": guncel_fiyat <= min_fiyat * 1.02,
        }

    def son_48_saatte_gonderildi_mi(self, urun_anahtari: str, guncel_fiyat: float) -> bool:
        """Ürün son 48 saat içinde raporlandıysa ve fiyatı en az %5 daha düşmediyse True döner."""
        sinir = (datetime.utcnow() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._baglanti_al() as conn:
            row = conn.execute(
                "SELECT son_gonderilen_fiyat, gonderim_tarihi FROM gonderilen_firsatlar WHERE urun_anahtari = ?",
                (urun_anahtari,),
            ).fetchone()

        if not row:
            return False
        if str(row["gonderim_tarihi"]) < sinir:
            return False
        # Eğer ürün dün gönderildikten sonra ekstra %5+ daha ucuzladıysa tekrar göster
        if guncel_fiyat < float(row["son_gonderilen_fiyat"]) * 0.95:
            return False
        return True

    def gonderilen_olarak_isaretle(self, firsatlar: List[Dict[str, Any]]) -> None:
        simdi = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._baglanti_al() as conn:
            for item in firsatlar:
                conn.execute(
                    """
                    INSERT INTO gonderilen_firsatlar (urun_anahtari, urun_adi, son_gonderilen_fiyat, gonderim_tarihi)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(urun_anahtari) DO UPDATE SET
                        urun_adi = excluded.urun_adi,
                        son_gonderilen_fiyat = excluded.son_gonderilen_fiyat,
                        gonderim_tarihi = excluded.gonderim_tarihi
                    """,
                    (item["urun_anahtari"], item["urun_adi"], item["fiyat"], simdi),
                )