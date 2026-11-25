import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Type, TypeVar

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from database import DatabaseManager

load_dotenv()

T = TypeVar("T", bound=BaseModel)


class OnaylananFirsatItem(BaseModel):
    aday_index: int = Field(description="Girdi listesindeki ürünün 'index' numarası")
    gercek_indirim_mi: bool = Field(default=False)
    gercek_indirim_yuzdesi: float = Field(default=0.0)
    kisa_aciklama: str = Field(
        default="",
        description="Ürünün ne olduğunu, piyasa ortalamasını ve sağlanan gerçek avantajı özetleyen 1-2 cümle.",
    )


class TopluDenetimSonucu(BaseModel):
    sonuclar: List[OnaylananFirsatItem] = Field(default_factory=list)


class AIEngine:
    def __init__(self, db: Optional[DatabaseManager] = None):
        self.db = db or DatabaseManager()
        self._api_lock = threading.Lock()
        self._son_istek_zamani = 0.0
        self.min_istek_araligi = 1.5
        self._gemini_yoruldu_bitis = 0.0

        gemini_keys = [
            k.strip()
            for k in (os.getenv("GEMINI_API_KEY", ""), os.getenv("GEMINI_API_KEY_YEDEK", ""))
            if k.strip()
        ]
        self.gemini_istemciler = [genai.Client(api_key=k) for k in gemini_keys]
        self.gemini_modeller = ["gemini-3.5-flash"]

        self.groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.groq_url = "".join(["ht", "tps://api.", "groq.", "com/openai/v1/chat/completions"])
        self.models_url = "".join(["ht", "tps://api.", "groq.", "com/openai/v1/models"])
        self.groq_modeller = self._aktif_groq_modelleri_getir() if self.groq_api_key else []
        print(
            f"[Multi-Agent AI] Gemini key: {len(self.gemini_istemciler)} | "
            f"Groq ajan havuzu: {self.groq_modeller}"
        )

    def _aktif_groq_modelleri_getir(self) -> List[str]:
        varsayilanlar = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    self.models_url,
                    headers={"Authorization": f"Bearer {self.groq_api_key}"},
                )
            if resp.status_code != 200:
                return varsayilanlar

            ham = [
                m["id"]
                for m in resp.json().get("data", [])
                if m.get("id")
                and not any(
                    y in m["id"].lower()
                    for y in ("whisper", "guard", "tts", "safeguard", "embed", "vision", "orpheus", "allam")
                )
            ]
            oncelik = ("120b", "70b", "32b", "27b", "versatile", "qwen", "gpt-oss")
            sirali = sorted(
                ham,
                key=lambda mid: next((i for i, a in enumerate(oncelik) if a in mid.lower()), 99),
            )
            return sirali[:3] or varsayilanlar
        except Exception:
            return varsayilanlar

    @staticmethod
    def _json_metni_ayikla(ham_metin: str) -> str:
        temiz = ham_metin.strip()
        if temiz.startswith("```"):
            temiz = temiz.strip("`").strip()
            if temiz.lower().startswith("json"):
                temiz = temiz[4:].strip()
        bas = temiz.find("{")
        son = temiz.rfind("}")
        if bas != -1 and son != -1 and son > bas:
            return temiz[bas : son + 1]
        return temiz

    def _ai_json_cagri(self, ajan_rolu: str, prompt: str, schema_cls: Type[T]) -> T:
        sistem_mesaji = (
            f"{ajan_rolu} "
            "Yanıtını SADECE geçerli tek bir JSON nesnesi olarak döndür (markdown veya açıklama ekleme). "
            'Çıktı formatı: {"sonuclar": [{"aday_index": 0, "gercek_indirim_mi": true, "gercek_indirim_yuzdesi": 25.5, "kisa_aciklama": "..."}]}'
        )

        # 1. Katman: Gemini
        if self.gemini_istemciler and time.time() >= self._gemini_yoruldu_bitis:
            for key_idx, istemci in enumerate(self.gemini_istemciler, start=1):
                for model_adi in self.gemini_modeller:
                    try:
                        response = istemci.models.generate_content(
                            model=model_adi,
                            contents=f"{sistem_mesaji}\n\n{prompt}",
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                                response_schema=schema_cls,
                                temperature=0.1,
                            ),
                        )
                        if response.parsed:
                            print(f"[AI] Gemini Key#{key_idx} ({model_adi}) paket denetimini tamamladı.")
                            return response.parsed
                        return schema_cls.model_validate_json(self._json_metni_ayikla(response.text or ""))
                    except Exception as e:
                        hata_str = str(e)
                        if "429" in hata_str or "RESOURCE_EXHAUSTED" in hata_str:
                            self._gemini_yoruldu_bitis = time.time() + 300
                            print("[Multi-Agent] Gemini kota doldu, görev Groq ajan havuzuna devredildi.")
                            break
                        print(f"[Gemini Uyarı] Key#{key_idx}/{model_adi}: {hata_str[:90]}")

        # 2. Katman: Groq Modelleri (Aralarında hız limiti korumalı geçiş)
        son_hata: Optional[Exception] = None
        for model_adi in self.groq_modeller:
            with self._api_lock:
                gecen = time.time() - self._son_istek_zamani
                if gecen < self.min_istek_araligi:
                    time.sleep(self.min_istek_araligi - gecen)
                self._son_istek_zamani = time.time()

            try:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(
                        self.groq_url,
                        headers={
                            "Authorization": f"Bearer {self.groq_api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model_adi,
                            "messages": [
                                {"role": "system", "content": sistem_mesaji},
                                {"role": "user", "content": prompt},
                            ],
                            "temperature": 0.1,
                        },
                    )
                if resp.status_code == 429:
                    print(f"[Ajan Devri] {model_adi} hız limitine ulaştı, 3 sn dinlenip sıradaki modele geçiliyor...")
                    time.sleep(3.0)
                    raise RuntimeError("Rate limit 429")
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")

                icerik = resp.json()["choices"][0]["message"]["content"]
                parsed = schema_cls.model_validate_json(self._json_metni_ayikla(icerik))
                print(f"[AI] Groq ({model_adi}) paket denetimini tamamladı.")
                return parsed
            except Exception as e:
                son_hata = e

        raise RuntimeError(f"Tüm yapay zeka ajanları yanıt veremedi: {son_hata}")

    def toplu_indirim_denetle(self, paket: List[Dict[str, Any]]) -> List[OnaylananFirsatItem]:
        """10-12 ürünü tek API isteğinde Şeytanın Avukatı süzgecinden geçirir (429 kota dostu)."""
        rol = (
            "Sen Kıdemli Fiyat Denetçisi ve Şeytanın Avukatısın. Sana verilen ürün listesini tek tek "
            "piyasa medyanı ve en ucuz rakip satıcı fiyatlarıyla kıyaslayıp gerçek indirimleri onaylarsın."
        )
        prompt = f"""
        Aşağıdaki ürünlerin her biri için gerçek indirim denetimi yap:
        {json.dumps(paket, ensure_ascii=False)}

        DENETİM KURALLARI:
        1. SAHTE İNDİRİM TUZAĞI: 1.700 TL'lik ürünü 1.600 TL'ye satıp 'Son 30 günün en düşüğü' diye gösteriyorlarsa veya piyasa medyanına göre gerçek indirim %15'in altındaysa `gercek_indirim_mi = false` ver.
        2. RAKİP SATICI KONTROLÜ: `piyasa_en_ucuz` zaten bu fiyata çok yakınsa veya daha ucuzu varsa `gercek_indirim_mi = false` ver.
        3. BOŞ KUTU / AKSESUAR / ANLAMSIZ BAŞLIK: Sadece fiyat yazan başlıkları (`12.699,00 TL` gibi) veya tekli aksesuarları `gercek_indirim_mi = false` yap.
        4. ONAYLANANLAR: Gerçekten piyasa ortalamasının en az %15 altında olan ürünler için `gercek_indirim_mi = true`, `gercek_indirim_yuzdesi` ve `kisa_aciklama` (1-2 cümle net ürün + tasarruf özeti) doldur.
        """
        sonuc = self._ai_json_cagri(rol, prompt, TopluDenetimSonucu)
        return [s for s in sonuc.sonuclar if s.gercek_indirim_mi and s.gercek_indirim_yuzdesi >= 15.0]