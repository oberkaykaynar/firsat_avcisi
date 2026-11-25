import asyncio
import html
import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ai_engine import AIEngine
from database import DatabaseManager
from scraper import OtonomFirsatMotoru

load_dotenv()


class FirsatBotu:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.db = DatabaseManager()
        self.ai = AIEngine(self.db)
        self.motor = OtonomFirsatMotoru(self.db, self.ai)

    async def start_komutu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await update.message.reply_text(
            "🤖 <b>Otonom Fırsat İstihbarat Motoru Aktif</b>\n\n"
            "• Manuel ürün arama kapalıdır.\n"
            "• Günde yalnızca 1 kez, saat <b>10:00</b>'da tüm büyük pazaryerleri taranır (≥ 150 TL).\n"
            "• Sahte indirimler elendikten sonra en iyi <b>30 gerçek indirim</b> sırayla gönderilir.\n"
            "• Anlık test için /tara komutunu kullanabilirsin.",
            parse_mode=ParseMode.HTML,
        )

    async def manuel_mesaj_uyari(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await update.message.reply_text(
            "ℹ️ Manuel arama kapalıdır. Sistem her gün saat <b>10:00</b>'da otonom tarama yapıp "
            "en iyi 30 gerçek indirimi gönderecektir. (Hemen test etmek için: /tara)",
            parse_mode=ParseMode.HTML,
        )

    async def top30_raporu_gonder(self, bot_app, hedef_chat_id: str, firsatlar: List[Dict[str, Any]]) -> None:
        if not firsatlar:
            await bot_app.bot.send_message(
                chat_id=hedef_chat_id,
                text="📉 Bugünkü taramada tüm doğrulamalardan geçen gerçek bir indirim bulunamadı.",
            )
            return

        await bot_app.bot.send_message(
            chat_id=hedef_chat_id,
            text=(
                f"🏆 <b>GÜNÜN EN İYİ {len(firsatlar)} GERÇEK İNDİRİMİ</b>\n"
                f"<i>150 TL üzeri • Sahte indirimler elendi • En yüksek indirimden sıralandı</i>"
            ),
            parse_mode=ParseMode.HTML,
        )

        for baslangic in range(0, len(firsatlar), 10):
            blok = firsatlar[baslangic : baslangic + 10]
            satirlar: List[str] = []
            for idx, item in enumerate(blok, start=baslangic + 1):
                ad = html.escape(item["urun_adi"])
                magaza = html.escape(item["platform"])
                aciklama = html.escape(item["kisa_aciklama"])
                link = html.escape(item["url"], quote=True)
                satirlar.append(
                    f"<b>{idx}. {ad}</b>\n"
                    f"🏪 {magaza} | 💰 <b>{item['fiyat']:.2f} TL</b> "
                    f"(Piyasa: ~{item['piyasa_medyan']:.2f} TL | 🔥 <b>%{item['indirim_yuzdesi']} İndirim</b>)\n"
                    f"📝 {aciklama}\n"
                    f"🔗 <a href=\"{link}\">Ürüne Git</a>"
                )
            await bot_app.bot.send_message(
                chat_id=hedef_chat_id,
                text="\n\n".join(satirlar),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    async def tara_komutu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        durum_msg = await update.message.reply_text("⏳ Otonom Top 30 taraması başlatılıyor...")
        loop = asyncio.get_running_loop()

        def ilerleme(metin: str) -> None:
            asyncio.run_coroutine_threadsafe(durum_msg.edit_text(metin), loop)

        firsatlar = await asyncio.to_thread(self.motor.gunluk_top30_tarama_calistir, ilerleme)
        await durum_msg.delete()
        await self.top30_raporu_gonder(context.application, str(update.effective_chat.id), firsatlar)