import os
import random
import re
import threading
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from ai_engine import AIEngine
from database import DatabaseManager


class OtonomFirsatMotoru:
    """7 pazaryeri vitrininden keşif yapıp, en güçlü 10 adayı doğal etkileşimli Google Alışveriş ile teyit eden motor."""

    MIN_URUN_FIYATI = 150.0
    MIN_VITRIN_INDIRIM_ORANI = 15.0
    MAX_ADAY_KAYNAK_BASINA = 30
    GOOGLE_TEYIT_LIMITI = 10  # Hız sınırına takılmamak için yalnızca en güçlü 10 adayı Google'da teyit eder
    TEST_MODU_DB_ENGEL_DEVRE_DISI = True
    CHROME_PROFIL_KLASORU = os.path.join(os.getcwd(), "test_chrome_profili")

    IKINCI_EL_SATICILAR = ("letgo", "sahibinden", "dolap", "gardrops", "zebramo", "modacruz")

    STANDART_VITRINLER = [
        ("n11 Süper Fırsatlar", ("www", "n11", "com"), "/super-firsatlar"),
        ("Trendyol Flaş Ürünler", ("www", "trendyol", "com"), "/sr?fl=flas-urunler"),
        ("PttAVM Kampanyalar", ("www", "pttavm", "com"), "/arama?q=indirim"),
        ("Pazarama Fırsatlar", ("www", "pazarama", "com"), "/kampanyalar"),
    ]

    def __init__(self, db: Optional[DatabaseManager] = None, ai: Optional[AIEngine] = None):
        self.db = db or DatabaseManager()
        self.ai = ai or AIEngine(self.db)
        self._tarama_kilidi = threading.Lock()

    @staticmethod
    def _url_kur(domain_parcalari: tuple, yol: str = "") -> str:
        proto = "".join(["ht", "tps://"])
        domain = ".".join(domain_parcalari)
        return f"{proto}{domain}{yol}"

    @staticmethod
    def _kategori_veya_kampanya_linki_mi(url: str, baslik: str) -> bool:
        u = (url or "").lower()
        b = (baslik or "").lower()

        if any(
            k in u
            for k in (
                "/kampanya",
                "/kampanyalar",
                "/kategori",
                "/kategoriler",
                "/arama",
                "/markalar",
                "/koleksiyon",
                "/firsat-sayfalari",
                "fl=flas-urunler",
                "/super-firsatlar",
            )
        ):
            return True

        kampanya_kaliplari = (
            "kampanyayı keşfet",
            "kampanyayi kesfet",
            "büyük final",
            "buyuk final",
            "en ucuz laptoplar",
            "en ucuz telefonlar",
            "tümünü gör",
            "tumunu gor",
            "fırsatları gör",
            "firsatlari gor",
            "ürün listeleniyor",
            "urun listeleniyor",
            "tl'ye ",
            "tl’ye ",
            "sepette ",
            "ve kişisel bakımda",
            "ve kisisel bakimda",
            "altı",
            "üstü",
            "fiyatları",
            "modelleri",
        )
        if any(k in b for k in kampanya_kaliplari):
            return True

        kelimeler = [w for w in re.findall(r"[a-zçğıöşü0-9]+", b) if len(w) > 1]
        if len(kelimeler) < 3:
            return True

        return False

    @staticmethod
    def _tr_fiyatlari_coz(metin: str, zorunlu_birim: bool = True) -> List[float]:
        temiz = re.sub(
            r"\d{1,3}(?:\.\d{3})*(?:,\d{2})?\s*(?:TL|₺)['’]?(?:ye|ya|dan|den)\b",
            " ",
            metin,
            flags=re.IGNORECASE,
        )
        temiz = re.sub(
            r"\d{1,3}(?:\.\d{3})*(?:,\d{2})?\s*(?:TL|₺)\s*(?:altı|alti|üstü|ustu)\b",
            " ",
            temiz,
            flags=re.IGNORECASE,
        )
        temiz = re.sub(r"\s*,\s*", ",", temiz)
        fiyatlar: List[float] = []

        if zorunlu_birim:
            desen = r"(?:(?:₺|TRY)\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)|(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:TL|₺))"
            for m in re.finditer(desen, temiz, re.IGNORECASE):
                ham_sayi = m.group(1) or m.group(2)
                if not ham_sayi:
                    continue
                try:
                    val = float(ham_sayi.replace(".", "").replace(",", "."))
                    if 50.0 <= val <= 300000.0:
                        fiyatlar.append(val)
                except ValueError:
                    pass
        else:
            for m in re.findall(r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)", temiz):
                try:
                    val = float(m.replace(".", "").replace(",", "."))
                    if 50.0 <= val <= 300000.0:
                        fiyatlar.append(val)
                except ValueError:
                    pass

        return sorted(set(fiyatlar))

    @staticmethod
    def _baslik_temizle(baslik: str) -> str:
        temiz = re.sub(r"\s+", " ", baslik).strip()
        temiz = re.sub(r"^\d{1,3}(?:\.\d{3})*(?:,\d{2})?\s*(?:TL|₺)\s*[-–]?\s*", "", temiz, flags=re.IGNORECASE)
        temiz = re.split(
            r"(?:Gülümseten Kasım|Süper Fiyat|Günün Fırsatı|Son 30 Günün|\d{1,3}(?:\.\d{3})*,\d{2}\s*TL|\s-\sHepsiburada|\s:\sAmazon\.com\.tr|\s-\sAmazon\.com\.tr)",
            temiz,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" -|•:")
        return temiz

    @staticmethod
    def _gecerli_urun_basligi_mi(baslik: str) -> bool:
        temiz = baslik.strip()
        if len(temiz) < 15 or len(temiz) > 180:
            return False
        if re.match(r"^[\d\.,\s%₺TLtlİndirimfırsatıGünün]+$", temiz):
            return False
        alt = temiz.lower()
        yasak_baslangic = (
            "%",
            "fiyat düşüşü",
            "düşük fiyat",
            "gülümseten kasım",
            "son 30 gün",
            "günün fırsatı",
            "süper fiyat",
            "tümünü gör",
        )
        if alt.startswith(yasak_baslangic):
            return False
        if any(
            k in alt
            for k in (
                "buket kağıtlı",
                "polimer vazoda",
                "çizgili vazoda",
                "aynı gün ücretsiz teslimat",
                "kampanyayı keşfet",
                "büyük final",
            )
        ):
            return False
        return True

    def _yeni_gercek_tarayici_sayfasi(self, p, gercek_chrome_dene: bool = True):
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1366,900",
        ]
        browser = None
        if gercek_chrome_dene:
            for kanal in ("chrome", "msedge"):
                try:
                    browser = p.chromium.launch(channel=kanal, headless=True, args=launch_args)
                    break
                except Exception:
                    continue
        if browser is None:
            browser = p.chromium.launch(headless=True, args=launch_args)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
        )
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', {get: () => ['tr-TR', 'tr', 'en-US', 'en']});
            """
        )
        page = context.new_page()
        return browser, page

    def _kalici_google_context_ac(self, p):
        """Kalıcı profilli gerçek Chrome oturumunu açar."""
        os.makedirs(self.CHROME_PROFIL_KLASORU, exist_ok=True)
        for kanal in ("chrome", "msedge", None):
            try:
                kwargs: Dict[str, Any] = {
                    "user_data_dir": self.CHROME_PROFIL_KLASORU,
                    "headless": False,
                    "locale": "tr-TR",
                    "timezone_id": "Europe/Istanbul",
                    "viewport": {"width": 1366, "height": 850},
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                if kanal:
                    kwargs["channel"] = kanal
                context = p.chromium.launch_persistent_context(**kwargs)
                page = context.pages[0] if context.pages else context.new_page()
                return context, page
            except Exception:
                continue
        raise RuntimeError("Kalıcı Chrome profili açılamadı.")

    def _google_cerez_onayla(self, page) -> None:
        for btn_text in ("Tümünü kabul et", "Kabul et", "Accept all"):
            try:
                btn = page.locator(f"button:has-text('{btn_text}')").first
                if btn.is_visible(timeout=900):
                    btn.click()
                    page.wait_for_timeout(random.randint(900, 1400))
                    break
            except Exception:
                continue

    def _insan_gibi_arama_yap(self, page, sorgu: str, ilk_sorgu: bool, google_base: str) -> None:
        """URL zıplaması yerine mümkün olduğunca sayfa içi arama kutusunu doğal yazım hızıyla kullanır."""
        if not ilk_sorgu:
            try:
                arama_kutusu = page.locator("textarea[name='q'], input[name='q']").first
                if arama_kutusu.is_visible(timeout=2000):
                    box = arama_kutusu.bounding_box()
                    if box:
                        page.mouse.move(
                            box["x"] + box["width"] * random.uniform(0.3, 0.7),
                            box["y"] + box["height"] * random.uniform(0.3, 0.7),
                            steps=random.randint(6, 12),
                        )
                    arama_kutusu.click()
                    page.wait_for_timeout(random.randint(250, 500))
                    page.keyboard.press("Control+A")
                    page.wait_for_timeout(random.randint(120, 250))
                    page.keyboard.press("Backspace")
                    page.wait_for_timeout(random.randint(200, 400))
                    arama_kutusu.type(sorgu, delay=random.randint(40, 85))
                    page.wait_for_timeout(random.randint(300, 600))
                    page.keyboard.press("Enter")
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    return
            except Exception:
                pass

        # İlk sorguda veya arama kutusu görünmüyorsa udm=28 sayfasını aç
        hedef = f"{google_base}?q={quote_plus(sorgu)}&udm=28&hl=tr&gl=tr"
        page.goto(hedef, wait_until="domcontentloaded", timeout=22000)

    def _google_udm28_satici_fiyatlarini_ayikla(self, html: str, hedef_urun_adi: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        kartlar = soup.select("div.UC8ZCe, div.MUWJ8c")
        yasak_etiketler = {"DÜŞÜK FİYAT", "FİYAT DÜŞÜŞÜ", "İNDİRİM", "FIRSAT", "YENİ", "SPONSORLU"}

        hedef_kelimeler = set(re.findall(r"[a-zçğıöşü0-9]{3,}", hedef_urun_adi.lower()))
        teklifler: List[Dict[str, Any]] = []
        gorulen = set()
        google_base = self._url_kur(("www", "google", "com", "tr"), "/search")

        for kart in kartlar:
            parcalar = [p.strip() for p in kart.stripped_strings if p.strip()]
            temiz_parcalar = [
                p
                for p in parcalar
                if p.upper() not in yasak_etiketler and not re.match(r"^%\d+\s*İNDİRİM$", p, re.IGNORECASE)
            ]
            if len(temiz_parcalar) < 3:
                continue

            kart_baslik = self._baslik_temizle(temiz_parcalar[0])
            kart_kelimeler = set(re.findall(r"[a-zçğıöşü0-9]{3,}", kart_baslik.lower()))

            if hedef_kelimeler and len(hedef_kelimeler & kart_kelimeler) < min(2, len(hedef_kelimeler)):
                continue

            guncel_fiyat: Optional[float] = None
            eski_fiyat = 0.0
            satici = ""

            for p in temiz_parcalar[1:]:
                if (p.startswith("₺") or p.endswith("TL")) and guncel_fiyat is None:
                    f_list = self._tr_fiyatlari_coz(p, zorunlu_birim=True)
                    if f_list:
                        guncel_fiyat = f_list[0]
                elif "genellikle" in p.lower() and ("₺" in p or "tl" in p.lower()):
                    f_list = self._tr_fiyatlari_coz(p, zorunlu_birim=True)
                    if f_list:
                        eski_fiyat = f_list[-1]
                elif (
                    guncel_fiyat is not None
                    and not p.startswith("₺")
                    and not p.startswith("(")
                    and not re.match(r"^\d,\d$", p)
                ):
                    alt_p = p.lower()
                    if not any(
                        k in alt_p
                        for k in ("iade", "teslimat", "mağaza", "daha fazlası", "kargo", "taksit", "genellikle")
                    ):
                        satici = p.split(" - ")[0].strip()
                        break

            if not guncel_fiyat or guncel_fiyat < self.MIN_URUN_FIYATI:
                continue

            if any(ik in satici.lower() for ik in self.IKINCI_EL_SATICILAR):
                continue

            anahtar = f"{satici.lower()}_{guncel_fiyat}"
            if anahtar in gorulen:
                continue
            gorulen.add(anahtar)

            ust = kart
            link = ""
            for _ in range(5):
                if not ust:
                    break
                if ust.name == "a" and ust.get("href", "").startswith("http") and "google." not in ust["href"]:
                    link = ust["href"]
                    break
                a_ic = ust.find("a", href=True)
                if a_ic and a_ic.get("href", "").startswith("http") and "google." not in a_ic["href"]:
                    link = a_ic["href"]
                    break
                ust = ust.parent

            if not link:
                link = f"{google_base}?q={quote_plus(kart_baslik)}&udm=28&hl=tr&gl=tr"

            teklifler.append({
                "baslik": kart_baslik,
                "fiyat": guncel_fiyat,
                "eski_fiyat": eski_fiyat if eski_fiyat > guncel_fiyat else 0.0,
                "satici": satici or "Google Alışveriş",
                "url": link,
            })

        return teklifler

    def _google_shopping_teyit_ve_ucuz_site_avla(
        self, adaylar: List[Dict[str, Any]], ilerleme_callback: Optional[Callable[[str], None]] = None
    ) -> Dict[int, Dict[str, Any]]:
        """En güçlü 10 adayı insan temposuyla Google Alışveriş'te teyit eder; engel çıkarsa döngüyü anında keser."""
        sonuc_haritasi: Dict[int, Dict[str, Any]] = {}
        if not adaylar:
            return sonuc_haritasi

        google_base = self._url_kur(("www", "google", "com", "tr"), "/search")

        with sync_playwright() as p:
            context, page = self._kalici_google_context_ac(p)
            try:
                page.goto(self._url_kur(("www", "google", "com", "tr"), ""), wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(random.randint(1500, 2500))
                self._google_cerez_onayla(page)

                for idx, aday in enumerate(adaylar):
                    if ilerleme_callback:
                        ilerleme_callback(f"🔎 Google Alışveriş fiyat teyidi ({idx + 1}/{len(adaylar)})...")

                    sorgu = aday["urun_adi"][:75]
                    try:
                        self._insan_gibi_arama_yap(page, sorgu, ilk_sorgu=(idx == 0), google_base=google_base)
                        page.wait_for_timeout(random.randint(2500, 4000))

                        # Doğrulama duvarı kontrolü ve kesin devre kesici (Circuit Breaker)
                        if "sorry/index" in page.url:
                            print("⚠️ Google doğrulama istedi. Açık pencerede çözülmesi bekleniyor (60 sn)...")
                            try:
                                page.wait_for_url("**/search**", timeout=60000)
                            except Exception:
                                print("🛑 Doğrulama tamamlanmadı; Google sorguları bu tur için durduruluyor.")
                                break

                        # Doğal sayfa etkileşimi (fare hareketi ve hafif kaydırma)
                        page.mouse.move(random.randint(250, 900), random.randint(200, 600), steps=random.randint(5, 10))
                        page.mouse.wheel(0, random.randint(400, 800))
                        page.wait_for_timeout(random.randint(800, 1400))

                        teklifler = self._google_udm28_satici_fiyatlarini_ayikla(page.content(), aday["urun_adi"])
                        gecerli_teklifler = [
                            t
                            for t in teklifler
                            if aday["vitrin_fiyati"] * 0.50 <= t["fiyat"] <= aday["vitrin_fiyati"] * 2.5
                        ]

                        rakip_fiyatlar: List[float] = []
                        en_ucuz_dis_teklif: Optional[Dict[str, Any]] = None

                        for t in gecerli_teklifler:
                            rakip_fiyatlar.append(t["fiyat"])
                            if t["eski_fiyat"] > t["fiyat"]:
                                rakip_fiyatlar.append(t["eski_fiyat"])

                            if t["fiyat"] < aday["vitrin_fiyati"] * 0.97:
                                if en_ucuz_dis_teklif is None or t["fiyat"] < en_ucuz_dis_teklif["fiyat"]:
                                    en_ucuz_dis_teklif = t

                        sonuc_haritasi[id(aday)] = {
                            "rakip_fiyatlar": rakip_fiyatlar,
                            "daha_ucuz_teklif": en_ucuz_dis_teklif,
                        }

                        # Sorgular arası insan benzeri değişken bekleme (Jitter)
                        if idx < len(adaylar) - 1:
                            bekleme = random.randint(4500, 8000)
                            page.wait_for_timeout(bekleme)

                    except Exception as e:
                        print(f"[Google Teyit Uyarı] {str(e)[:70]}")
                        sonuc_haritasi[id(aday)] = {"rakip_fiyatlar": [], "daha_ucuz_teklif": None}
            finally:
                context.close()

        return sonuc_haritasi

    def _amazon_ozel_tara(self, max_aday: int = 30) -> List[Dict[str, Any]]:
        adaylar: List[Dict[str, Any]] = []
        gorulen_asinler = set()
        ana_url = self._url_kur(("www", "amazon", "com", "tr"), "")

        with sync_playwright() as p:
            browser, page = self._yeni_gercek_tarayici_sayfasi(p, gercek_chrome_dene=False)
            try:
                page.goto(f"{ana_url}/gp/goldbox", wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_selector(
                        "[data-testid='deal-card'], [data-testid='product-card'], a[href*='/dp/']",
                        timeout=10000,
                    )
                except Exception:
                    pass

                page.mouse.move(500, 400)
                for _ in range(5):
                    page.mouse.wheel(0, 1400)
                    page.wait_for_timeout(700)

                soup = BeautifulSoup(page.content(), "html.parser")
                kartlar = soup.select(
                    "[data-testid='deal-card'], [data-testid='product-card'], "
                    "[class*='DealGridItem'], [data-deal-id]"
                )
                if len(kartlar) < 5:
                    kartlar = [a.parent.parent for a in soup.select("a[href*='/dp/']") if a.parent and a.parent.parent]

                for kart in kartlar:
                    asin_kodu = ""
                    for a_tag in kart.select("a[href]"):
                        href = a_tag.get("href", "")
                        m_asin = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", href)
                        if m_asin:
                            asin_kodu = m_asin.group(1)
                            break

                    if not asin_kodu or asin_kodu in gorulen_asinler:
                        continue

                    tam_url = f"{ana_url}/dp/{asin_kodu}"

                    guncel_el = kart.select_one(".a-price:not(.a-text-price) .a-offscreen, .a-price-whole")
                    eski_el = kart.select_one(".a-price.a-text-price .a-offscreen")

                    guncel_metin = (
                        f"{guncel_el.get_text(' ', strip=True)} TL"
                        if guncel_el
                        else kart.get_text(" ", strip=True)
                    )
                    guncel_fiyatlar = self._tr_fiyatlari_coz(guncel_metin, zorunlu_birim=True)
                    if not guncel_fiyatlar or guncel_fiyatlar[0] < self.MIN_URUN_FIYATI:
                        continue

                    guncel_fiyat = guncel_fiyatlar[0]
                    eski_fiyat = 0.0
                    if eski_el:
                        eski_list = self._tr_fiyatlari_coz(f"{eski_el.get_text(' ', strip=True)} TL", zorunlu_birim=True)
                        if eski_list and eski_list[-1] > guncel_fiyat:
                            eski_fiyat = eski_list[-1]
                    elif len(guncel_fiyatlar) >= 2 and guncel_fiyatlar[-1] > guncel_fiyat:
                        eski_fiyat = guncel_fiyatlar[-1]

                    ad_el = kart.select_one(
                        "[data-testid='deal-title'], [class*='DealTitle'], [class*='Title'], h2, h3, img[alt]"
                    )
                    ham_baslik = ""
                    if ad_el:
                        ham_baslik = ad_el.get("alt") if ad_el.name == "img" else ad_el.get_text(" ", strip=True)
                    if not ham_baslik:
                        ilk_a = kart.select_one("a[href*='/dp/']")
                        if ilk_a:
                            ham_baslik = ilk_a.get("aria-label") or ilk_a.get("title") or ilk_a.get_text(" ", strip=True)

                    baslik = self._baslik_temizle(ham_baslik)
                    if not self._gecerli_urun_basligi_mi(baslik) or self._kategori_veya_kampanya_linki_mi(
                        tam_url, baslik
                    ):
                        continue

                    gorulen_asinler.add(asin_kodu)
                    adaylar.append({
                        "platform": "Amazon.com.tr",
                        "vitrin_kaynagi": "Amazon Günün Fırsatları",
                        "urun_adi": baslik,
                        "vitrin_fiyati": guncel_fiyat,
                        "eski_fiyat": eski_fiyat,
                        "url": tam_url,
                    })
                    if len(adaylar) >= max_aday:
                        break
            except Exception as e:
                print(f"[Amazon Uyarı] {str(e)[:80]}")
            finally:
                browser.close()

        print(f"[Keşif] Amazon Günün Fırsatları: {len(adaylar)} aday toplandı (>= {self.MIN_URUN_FIYATI:.0f} TL).")
        return adaylar

    def _hepsiburada_ozel_tara(self, max_aday: int = 30) -> List[Dict[str, Any]]:
        adaylar: List[Dict[str, Any]] = []
        gorulen_urller = set()
        ana_url = self._url_kur(("www", "hepsiburada", "com"), "")
        yakalanan_jsonlar: List[Any] = []

        with sync_playwright() as p:
            browser, page = self._yeni_gercek_tarayici_sayfasi(p, gercek_chrome_dene=True)

            def _yanit_dinle(response):
                try:
                    if "hepsiburada.com" in response.url and response.status == 200:
                        ct = response.headers.get("content-type", "")
                        if "application/json" in ct:
                            yakalanan_jsonlar.append(response.json())
                except Exception:
                    pass

            page.on("response", _yanit_dinle)

            try:
                page.goto(ana_url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(2000)
                page.mouse.move(320, 280)

                for yol in ("/kampanyalar/super-fiyat-super-teklif", "/gunun-firsati-teklifleri"):
                    if len(adaylar) >= max_aday:
                        break
                    page.goto(f"{ana_url}{yol}", wait_until="domcontentloaded", timeout=28000)
                    page.wait_for_timeout(3200)
                    for _ in range(5):
                        page.mouse.wheel(0, 1400)
                        page.wait_for_timeout(700)

                    soup = BeautifulSoup(page.content(), "html.parser")
                    kartlar = soup.select("li[id^='i'], [data-test-id='product-card-item'], a[href*='-p-']")

                    for el in kartlar:
                        a_tag = el if el.name == "a" else el.select_one("a[href*='-p-'], a[href]")
                        if not a_tag:
                            continue
                        href = a_tag.get("href", "").strip()
                        if not href or "-p-" not in href:
                            continue

                        temiz_href = href.split("?")[0]
                        tam_url = temiz_href if temiz_href.startswith("http") else f"{ana_url}{temiz_href}"
                        if tam_url in gorulen_urller:
                            continue

                        kart = (
                            el
                            if el.name != "a"
                            else (a_tag.parent.parent if a_tag.parent and a_tag.parent.parent else a_tag)
                        )
                        fiyat_el = kart.select_one("[data-test-id='price-current-price']")
                        fiyat_metni = fiyat_el.get_text(" ", strip=True) if fiyat_el else kart.get_text(" ", strip=True)
                        fiyatlar = self._tr_fiyatlari_coz(fiyat_metni, zorunlu_birim=True)
                        if not fiyatlar or fiyatlar[0] < self.MIN_URUN_FIYATI:
                            continue

                        ad_el = kart.select_one("[data-test-id='product-card-name'], h3")
                        baslik = self._baslik_temizle(
                            (ad_el.get_text(" ", strip=True) if ad_el else "")
                            or a_tag.get("title")
                            or a_tag.get_text(" ", strip=True)
                        )
                        if not self._gecerli_urun_basligi_mi(baslik) or self._kategori_veya_kampanya_linki_mi(
                            tam_url, baslik
                        ):
                            continue

                        gorulen_urller.add(tam_url)
                        adaylar.append({
                            "platform": "Hepsiburada",
                            "vitrin_kaynagi": "Hepsiburada Süper Teklif",
                            "urun_adi": baslik,
                            "vitrin_fiyati": fiyatlar[0],
                            "eski_fiyat": fiyatlar[-1] if len(fiyatlar) > 1 else 0.0,
                            "url": tam_url,
                        })
                        if len(adaylar) >= max_aday:
                            break
            except Exception as e:
                print(f"[Hepsiburada Uyarı] {str(e)[:80]}")
            finally:
                browser.close()

        def _json_tara(veri: Any):
            if len(adaylar) >= max_aday:
                return
            if isinstance(veri, dict):
                ad = veri.get("name") or veri.get("productName") or veri.get("title")
                url_yolu = veri.get("url") or veri.get("productUrl") or veri.get("clickUrl")
                fiyat_val = veri.get("price") or veri.get("finalPrice") or veri.get("discountedPrice")
                if isinstance(fiyat_val, dict):
                    fiyat_val = fiyat_val.get("value") or fiyat_val.get("amount")
                if isinstance(ad, str) and isinstance(url_yolu, str) and "-p-" in url_yolu and fiyat_val:
                    try:
                        f_num = float(str(fiyat_val).replace(",", "."))
                        tam_u = url_yolu if url_yolu.startswith("http") else f"{ana_url}{url_yolu.split('?')[0]}"
                        baslik = self._baslik_temizle(ad)
                        if (
                            f_num >= self.MIN_URUN_FIYATI
                            and tam_u not in gorulen_urller
                            and self._gecerli_urun_basligi_mi(baslik)
                            and not self._kategori_veya_kampanya_linki_mi(tam_u, baslik)
                        ):
                            gorulen_urller.add(tam_u)
                            adaylar.append({
                                "platform": "Hepsiburada",
                                "vitrin_kaynagi": "Hepsiburada API Akışı",
                                "urun_adi": baslik,
                                "vitrin_fiyati": round(f_num, 2),
                                "eski_fiyat": 0.0,
                                "url": tam_u,
                            })
                    except Exception:
                        pass
                for v in veri.values():
                    _json_tara(v)
            elif isinstance(veri, list):
                for item in veri:
                    _json_tara(item)

        for paket in yakalanan_jsonlar:
            _json_tara(paket)

        print(f"[Keşif] Hepsiburada Fırsatlar: {len(adaylar)} aday toplandı (>= {self.MIN_URUN_FIYATI:.0f} TL).")
        return adaylar

    def _idefix_ozel_tara(self, max_aday: int = 30) -> List[Dict[str, Any]]:
        adaylar: List[Dict[str, Any]] = []
        gorulen_urller = set()
        ana_url = self._url_kur(("www", "idefix", "com"), "")

        with sync_playwright() as p:
            browser, page = self._yeni_gercek_tarayici_sayfasi(p, gercek_chrome_dene=True)
            try:
                for yol in ("/arama?q=f%C4%B1rsat", "/arama?q=kampanya", "/arama?q=indirim"):
                    if len(adaylar) >= max_aday:
                        break
                    page.goto(f"{ana_url}{yol}", wait_until="domcontentloaded", timeout=28000)
                    page.wait_for_timeout(3200)
                    for _ in range(4):
                        page.mouse.wheel(0, 1400)
                        page.wait_for_timeout(600)

                    soup = BeautifulSoup(page.content(), "html.parser")
                    for a_tag in soup.select("a[href*='-p-']"):
                        href = a_tag.get("href", "").strip()
                        if not href or "-p-" not in href:
                            continue

                        temiz_href = href.split("?")[0].split("#")[0]
                        tam_url = temiz_href if temiz_href.startswith("http") else f"{ana_url}{temiz_href}"
                        if tam_url in gorulen_urller:
                            continue

                        kart = a_tag
                        fiyatlar: List[float] = []
                        for _ in range(6):
                            if not kart.parent:
                                break
                            kart = kart.parent
                            metin = kart.get_text(" ", strip=True)
                            if "TL" in metin or "₺" in metin:
                                aday_fiyatlar = self._tr_fiyatlari_coz(metin, zorunlu_birim=True)
                                if aday_fiyatlar:
                                    fiyatlar = aday_fiyatlar
                                    break

                        if not fiyatlar or fiyatlar[0] < self.MIN_URUN_FIYATI:
                            continue

                        kart_metni = kart.get_text(" ", strip=True)
                        ham_baslik = (
                            a_tag.get("title")
                            or a_tag.get("aria-label")
                            or a_tag.get_text(" ", strip=True)
                        )
                        if len(ham_baslik.strip()) < 10:
                            ham_baslik = re.split(r"\d{1,3}(?:\.\d{3})*(?:,\d{2})?\s*(?:TL|₺)", kart_metni)[0]

                        baslik = self._baslik_temizle(ham_baslik)
                        if not self._gecerli_urun_basligi_mi(baslik):
                            continue

                        gorulen_urller.add(tam_url)
                        adaylar.append({
                            "platform": "idefix",
                            "vitrin_kaynagi": "idefix Fırsatlar",
                            "urun_adi": baslik,
                            "vitrin_fiyati": fiyatlar[0],
                            "eski_fiyat": fiyatlar[-1] if len(fiyatlar) >= 2 and fiyatlar[-1] > fiyatlar[0] else 0.0,
                            "url": tam_url,
                        })
                        if len(adaylar) >= max_aday:
                            break
            except Exception as e:
                print(f"[idefix Uyarı] {str(e)[:80]}")
            finally:
                browser.close()

        print(f"[Keşif] idefix Fırsatlar: {len(adaylar)} aday toplandı (>= {self.MIN_URUN_FIYATI:.0f} TL).")
        return adaylar

    def _vitrin_sayfasi_tara(
        self, kaynak_adi: str, domain_parcalari: tuple, yol: str, max_aday: int = 30
    ) -> List[Dict[str, Any]]:
        adaylar: List[Dict[str, Any]] = []
        gorulen_urller = set()
        hedef_url = self._url_kur(domain_parcalari, yol)
        ana_domain_url = self._url_kur(domain_parcalari, "")

        with sync_playwright() as p:
            browser, page = self._yeni_gercek_tarayici_sayfasi(p, gercek_chrome_dene=False)
            try:
                page.goto(hedef_url, wait_until="domcontentloaded", timeout=28000)
                page.wait_for_timeout(3000)
                for _ in range(5):
                    page.mouse.wheel(0, 1400)
                    page.wait_for_timeout(650)

                soup = BeautifulSoup(page.content(), "html.parser")
                kart_adaylari = soup.select(
                    "article, [data-test-id*='product'], "
                    "[data-testid*='deal'], [class*='DealGridItem'], "
                    "[class*='product-card'], [class*='ProductCard'], [class*='p-card'], [class*='prd']"
                )
                hedef_elemanlar = kart_adaylari if len(kart_adaylari) >= 4 else soup.select("a[href]")

                for el in hedef_elemanlar:
                    a_tag = el if el.name == "a" else el.select_one("a[href]")
                    if not a_tag:
                        continue

                    href = a_tag.get("href", "").strip()
                    if not href or href.startswith("#") or href.startswith("javascript"):
                        continue

                    tam_url = href if href.startswith("http") else f"{ana_domain_url}{href}"
                    if tam_url in gorulen_urller:
                        continue

                    kart = el
                    if el.name == "a":
                        for _ in range(5):
                            if kart.parent and (
                                "TL" not in kart.get_text(" ", strip=True)
                                and "₺" not in kart.get_text(" ", strip=True)
                            ):
                                kart = kart.parent
                            else:
                                break

                    kart_metni = kart.get_text(" ", strip=True)
                    if "TL" not in kart_metni and "₺" not in kart_metni:
                        continue

                    fiyatlar = self._tr_fiyatlari_coz(kart_metni, zorunlu_birim=True)
                    if not fiyatlar or fiyatlar[0] < self.MIN_URUN_FIYATI:
                        continue

                    guncel_fiyat = fiyatlar[0]
                    eski_fiyat = fiyatlar[-1] if len(fiyatlar) >= 2 and fiyatlar[-1] > guncel_fiyat else 0.0

                    baslik_el = kart.select_one(
                        "h1, h2, h3, h4, [class*='title'], [class*='name'], [data-test-id*='title']"
                    )
                    ham_baslik = (
                        a_tag.get("title")
                        or (baslik_el.get_text(" ", strip=True) if baslik_el else "")
                        or a_tag.get_text(" ", strip=True)
                    )
                    baslik = self._baslik_temizle(ham_baslik)

                    if not self._gecerli_urun_basligi_mi(baslik) or self._kategori_veya_kampanya_linki_mi(
                        tam_url, baslik
                    ):
                        continue

                    gorulen_urller.add(tam_url)
                    adaylar.append({
                        "platform": kaynak_adi.split()[0],
                        "vitrin_kaynagi": kaynak_adi,
                        "urun_adi": baslik,
                        "vitrin_fiyati": guncel_fiyat,
                        "eski_fiyat": eski_fiyat,
                        "url": tam_url,
                    })
                    if len(adaylar) >= max_aday:
                        break
            except Exception as e:
                print(f"[Vitrin Uyarı] {kaynak_adi} taranamadı: {str(e)[:80]}")
            finally:
                browser.close()

        print(f"[Keşif] {kaynak_adi}: {len(adaylar)} aday toplandı (>= {self.MIN_URUN_FIYATI:.0f} TL).")
        return adaylar

    def _aday_degerlendir(
        self, aday: Dict[str, Any], google_analiz: Dict[str, Any], manuel_tetik: bool = False
    ) -> Optional[Dict[str, Any]]:
        if self._kategori_veya_kampanya_linki_mi(aday.get("url", ""), aday.get("urun_adi", "")):
            return None

        aktif_urun = dict(aday)
        daha_ucuz = google_analiz.get("daha_ucuz_teklif")

        if daha_ucuz and daha_ucuz["fiyat"] < aktif_urun["vitrin_fiyati"] * 0.97:
            eski_pazaryeri = aktif_urun["platform"]
            eski_pazaryeri_fiyati = aktif_urun["vitrin_fiyati"]
            aktif_urun["platform"] = f"{daha_ucuz['satici']} (Google Teyitli)"
            aktif_urun["vitrin_fiyati"] = daha_ucuz["fiyat"]
            aktif_urun["url"] = daha_ucuz["url"]
            if aktif_urun.get("eski_fiyat", 0.0) <= eski_pazaryeri_fiyati:
                aktif_urun["eski_fiyat"] = eski_pazaryeri_fiyati
            print(
                f"[Daha Ucuz Site Bulundu] {aktif_urun['urun_adi'][:40]} -> "
                f"{eski_pazaryeri} ({eski_pazaryeri_fiyati} TL) yerine {daha_ucuz['satici']} ({daha_ucuz['fiyat']} TL)"
            )

        urun_anahtari = re.sub(r"\W+", "_", aktif_urun["urun_adi"].lower())[:55]

        if not self.TEST_MODU_DB_ENGEL_DEVRE_DISI and not manuel_tetik:
            if self.db.son_48_saatte_gonderildi_mi(urun_anahtari, aktif_urun["vitrin_fiyati"]):
                return None

        db_analiz = self.db.fiyat_kaydet_ve_analiz_et(
            urun_anahtari=urun_anahtari,
            kaynak=aktif_urun["platform"],
            guncel_fiyat=aktif_urun["vitrin_fiyati"],
        )

        rakip_fiyatlar = list(google_analiz.get("rakip_fiyatlar") or [])
        if aktif_urun.get("eski_fiyat", 0.0) > aktif_urun["vitrin_fiyati"]:
            rakip_fiyatlar.append(aktif_urun["eski_fiyat"])

        if not rakip_fiyatlar:
            return None

        rakip_fiyatlar.sort()
        piyasa_en_ucuz = rakip_fiyatlar[0]
        piyasa_medyan = rakip_fiyatlar[len(rakip_fiyatlar) // 2]

        if piyasa_en_ucuz < aktif_urun["vitrin_fiyati"] * 0.95:
            return None

        tahmini_indirim = ((piyasa_medyan - aktif_urun["vitrin_fiyati"]) / piyasa_medyan) * 100.0
        if tahmini_indirim < self.MIN_VITRIN_INDIRIM_ORANI or tahmini_indirim > 55.0:
            return None

        return {
            **aktif_urun,
            "urun_anahtari": urun_anahtari,
            "piyasa_en_ucuz": round(piyasa_en_ucuz, 2),
            "piyasa_medyan": round(piyasa_medyan, 2),
            "db_90_gun_dip": db_analiz["db_min_fiyat"],
            "tahmini_indirim_yuzdesi": round(tahmini_indirim, 1),
        }

    def gunluk_top30_tarama_calistir(
        self, ilerleme_callback: Optional[Callable[[str], None]] = None, manuel_tetik: bool = False
    ) -> List[Dict[str, Any]]:
        if not self._tarama_kilidi.acquire(blocking=False):
            return []

        try:
            if ilerleme_callback:
                ilerleme_callback("🚀 Otonom Tarama başladı: 7 pazaryeri vitrini taranıyor...")

            kaynak_gruplari: List[List[Dict[str, Any]]] = []
            gorulen_basliklar = set()

            def grup_ekle(liste: List[Dict[str, Any]]) -> None:
                temiz_liste = []
                for a in liste:
                    norm_ad = re.sub(r"\W+", "", a["urun_adi"].lower())[:45]
                    if norm_ad and norm_ad not in gorulen_basliklar:
                        gorulen_basliklar.add(norm_ad)
                        temiz_liste.append(a)
                if temiz_liste:
                    kaynak_gruplari.append(temiz_liste)

            if ilerleme_callback:
                ilerleme_callback("🔎 Taranıyor: Amazon, Hepsiburada & idefix Özel Motorları...")
            grup_ekle(self._amazon_ozel_tara(max_aday=self.MAX_ADAY_KAYNAK_BASINA))
            grup_ekle(self._hepsiburada_ozel_tara(max_aday=self.MAX_ADAY_KAYNAK_BASINA))
            grup_ekle(self._idefix_ozel_tara(max_aday=self.MAX_ADAY_KAYNAK_BASINA))

            for kaynak_adi, domain_parcalari, yol in self.STANDART_VITRINLER:
                if ilerleme_callback:
                    ilerleme_callback(f"🔎 Taranıyor: {kaynak_adi}...")
                grup_ekle(
                    self._vitrin_sayfasi_tara(
                        kaynak_adi, domain_parcalari, yol, max_aday=self.MAX_ADAY_KAYNAK_BASINA
                    )
                )

            vitrin_adaylari: List[Dict[str, Any]] = []
            max_uzunluk = max((len(g) for g in kaynak_gruplari), default=0)
            for idx in range(max_uzunluk):
                for grup in kaynak_gruplari:
                    if idx < len(grup):
                        vitrin_adaylari.append(grup[idx])

            def _on_indirim_skoru(item: Dict[str, Any]) -> float:
                eski = item.get("eski_fiyat", 0.0)
                guncel = item["vitrin_fiyati"]
                if eski > guncel:
                    oran = ((eski - guncel) / eski) * 100.0
                    return oran if 15.0 <= oran <= 55.0 else 0.0
                return 0.0

            vitrin_adaylari.sort(key=_on_indirim_skoru, reverse=True)
            teyit_edilecekler = vitrin_adaylari[: self.GOOGLE_TEYIT_LIMITI]

            if ilerleme_callback:
                ilerleme_callback(
                    f"📊 {len(vitrin_adaylari)} aday toplandı. En güçlü {len(teyit_edilecekler)} ürün Google Alışveriş'te teyit ediliyor..."
                )

            google_teyit_haritasi = self._google_shopping_teyit_ve_ucuz_site_avla(
                teyit_edilecekler, ilerleme_callback=ilerleme_callback
            )

            dogrulanmis_adaylar: List[Dict[str, Any]] = []
            for a in vitrin_adaylari:
                res = self._aday_degerlendir(
                    a,
                    google_teyit_haritasi.get(id(a), {"rakip_fiyatlar": [], "daha_ucuz_teklif": None}),
                    manuel_tetik=manuel_tetik,
                )
                if res:
                    dogrulanmis_adaylar.append(res)

            dogrulanmis_adaylar.sort(key=lambda x: x["tahmini_indirim_yuzdesi"], reverse=True)
            print(f"[Ön Doğrulama] %15+ elemeden geçen gerçek fırsat adayı sayısı: {len(dogrulanmis_adaylar)}")

            if ilerleme_callback:
                ilerleme_callback(
                    f"🧠 {len(dogrulanmis_adaylar)} güçlü aday AI Şeytanın Avukatı denetimine gönderiliyor..."
                )

            onaylanan_firsatlar: List[Dict[str, Any]] = []
            for bas in range(0, min(len(dogrulanmis_adaylar), 60), 10):
                paket = dogrulanmis_adaylar[bas : bas + 10]
                ai_girdi = [
                    {
                        "index": i,
                        "urun_adi": item["urun_adi"],
                        "magaza": item["platform"],
                        "fiyat": item["vitrin_fiyati"],
                        "piyasa_en_ucuz": item["piyasa_en_ucuz"],
                        "piyasa_medyan": item["piyasa_medyan"],
                        "db_90_gun_dip": item["db_90_gun_dip"],
                    }
                    for i, item in enumerate(paket)
                ]
                try:
                    ai_onaylar = self.ai.toplu_indirim_denetle(ai_girdi)
                    for onay in ai_onaylar:
                        if 0 <= onay.aday_index < len(paket):
                            secilen = paket[onay.aday_index]
                            onaylanan_firsatlar.append({
                                "urun_anahtari": secilen["urun_anahtari"],
                                "urun_adi": secilen["urun_adi"],
                                "platform": secilen["platform"],
                                "fiyat": secilen["vitrin_fiyati"],
                                "piyasa_medyan": secilen["piyasa_medyan"],
                                "indirim_yuzdesi": round(onay.gercek_indirim_yuzdesi, 1),
                                "kisa_aciklama": onay.kisa_aciklama,
                                "url": secilen["url"],
                            })
                except Exception as e:
                    print(f"[AI Paket Uyarı] {e}")

            onaylanan_firsatlar.sort(key=lambda x: x["indirim_yuzdesi"], reverse=True)
            top30 = onaylanan_firsatlar[:30]
            if not self.TEST_MODU_DB_ENGEL_DEVRE_DISI:
                self.db.gonderilen_olarak_isaretle(top30)
            return top30
        finally:
            self._tarama_kilidi.release()