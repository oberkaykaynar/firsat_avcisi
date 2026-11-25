import os
import re
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

PROFIL_KLASORU = os.path.join(os.getcwd(), "test_chrome_profili")


def fiyat_coz(metin: str):
    m = re.search(r"(?:₺|TRY)\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)", metin)
    if m:
        val = float(m.group(1).replace(".", "").replace(",", "."))
        if 150.0 <= val <= 300000.0:
            return val
    return None


def indirim_filtresi_uygula(page):
    """Sol panelde yalnızca gerçek 'İndirimde' filtresi varsa tıklar; sorgu metnine dokunmaz."""
    try:
        # Sol filtre paneli genellikle <nav> veya sol sütun içindedir
        filtre = page.locator("a:has-text('İndirimde'), div[role='checkbox']:has-text('İndirimde')").first
        if filtre.is_visible(timeout=2000):
            filtre.click()
            print("   ✓ Sol menüden 'İndirimde' filtresi tıklandı.")
            page.wait_for_timeout(2500)
        else:
            print("   • 'İndirimde' yan filtresi bu sorguda görünmedi, kart etiketlerinden ayrıştırılacak.")
    except Exception as e:
        print(f"   • Filtre atlandı: {e}")


def kartlari_ayikla(html: str, min_fiyat: float = 150.0):
    soup = BeautifulSoup(html, "html.parser")
    kartlar = soup.select("div.UC8ZCe, div.MUWJ8c")
    yasak_etiketler = {"DÜŞÜK FİYAT", "FİYAT DÜŞÜŞÜ", "İNDİRİM", "FIRSAT", "YENİ", "SPONSORLU"}

    urunler = []
    gorulen = set()

    for kart in kartlar:
        parcalar = [p.strip() for p in kart.stripped_strings if p.strip()]
        temiz_parcalar = [
            p
            for p in parcalar
            if p.upper() not in yasak_etiketler and not re.match(r"^%\d+\s*İNDİRİM$", p, re.IGNORECASE)
        ]
        if len(temiz_parcalar) < 3:
            continue

        urun_adi = re.sub(r"^\d{1,3}(?:\.\d{3})*(?:,\d{2})?\s*(?:TL|₺)\s*[-–]?\s*", "", temiz_parcalar[0]).strip()
        if len(urun_adi) < 12 or urun_adi.lower() in gorulen:
            continue

        guncel_fiyat = None
        eski_fiyat = 0.0
        satici = "Google Alışveriş"
        etiket_var = any(p.upper() in ("DÜŞÜK FİYAT", "FİYAT DÜŞÜŞÜ") for p in parcalar)

        for p in temiz_parcalar[1:]:
            if p.startswith("₺") and guncel_fiyat is None:
                guncel_fiyat = fiyat_coz(p)
            elif "genellikle" in p.lower() and "₺" in p:
                eski_fiyat = fiyat_coz(p) or 0.0
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

        if guncel_fiyat and guncel_fiyat >= min_fiyat:
            gorulen.add(urun_adi.lower())
            indirim_orani = (
                round(((eski_fiyat - guncel_fiyat) / eski_fiyat) * 100, 1)
                if eski_fiyat > guncel_fiyat
                else 0.0
            )
            urunler.append({
                "urun_adi": urun_adi,
                "fiyat": guncel_fiyat,
                "eski_fiyat": eski_fiyat,
                "indirim_orani": indirim_orani,
                "etiket": etiket_var,
                "satici": satici,
            })

    # Önce gerçek indirim oranı hesaplananları, sonra 'DÜŞÜK FİYAT' etiketlileri en üste al
    urunler.sort(key=lambda x: (x["indirim_orani"], x["etiket"]), reverse=True)
    return urunler


def google_shopping_test(sorgular=None):
    if sorgular is None:
        sorgular = ["bluetooth kulaklık", "kahve makinesi", "robot süpürge"]

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFIL_KLASORU,
            channel="chrome",
            headless=False,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1366, "height": 850},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            tum_urunler = []
            for idx, sorgu in enumerate(sorgular):
                print(f"\n[{idx + 1}/{len(sorgular)}] Sorgu açılıyor: '{sorgu}'...")
                hedef = f"https://www.google.com.tr/search?q={quote_plus(sorgu)}&udm=28&hl=tr&gl=tr"
                page.goto(hedef, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1800)

                if idx == 0:
                    for btn_text in ("Tümünü kabul et", "Kabul et"):
                        btn = page.locator(f"button:has-text('{btn_text}')").first
                        if btn.is_visible(timeout=900):
                            btn.click()
                            page.wait_for_timeout(800)
                            break

                if "sorry/index" in page.url:
                    print("⚠️ CAPTCHA çıktı, çözülmesi bekleniyor...")
                    page.wait_for_url("**/search**", timeout=60000)

                indirim_filtresi_uygula(page)

                for _ in range(3):
                    page.mouse.wheel(0, 1200)
                    page.wait_for_timeout(600)

                bulunan = kartlari_ayikla(page.content(), min_fiyat=150.0)
                print(f"   -> '{sorgu}' için {len(bulunan)} ürün bulundu (>= 150 TL).")
                tum_urunler.extend(bulunan)

            # Tüm sorgulardan gelenleri indirim oranına göre sırala
            tum_urunler.sort(key=lambda x: (x["indirim_orani"], x["etiket"]), reverse=True)
            print(f"\n=== TOPLAM {len(tum_urunler)} ÜRÜN (EN İYİ FIRSATLAR ÜSTTE) ===\n")

            for i, u in enumerate(tum_urunler[:30], 1):
                indirim_bilgi = (
                    f"Eski: {u['eski_fiyat']} TL (%{u['indirim_orani']} indirim)"
                    if u["eski_fiyat"] > 0
                    else ("DÜŞÜK FİYAT Etiketli" if u["etiket"] else "Standart Fiyat")
                )
                print(f"{i:2d}. {u['urun_adi'][:55]} | {u['fiyat']} TL | {indirim_bilgi} | Satıcı: {u['satici']}")

        finally:
            context.close()


if __name__ == "__main__":
    google_shopping_test(["bluetooth kulaklık", "kahve makinesi", "robot süpürge"])