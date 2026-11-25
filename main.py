import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot import FirsatBotu

TR_TZ = ZoneInfo("Europe/Istanbul")


async def tek_seferlik_tarama_ve_gonder(firsat_botu: FirsatBotu) -> None:
    if not firsat_botu.chat_id:
        print("[Uyarı] TELEGRAM_CHAT_ID tanımlı değil, mesaj gönderilemiyor.")
        return

    app = Application.builder().token(firsat_botu.token).build()
    await app.initialize()
    try:
        await app.bot.send_message(
            chat_id=firsat_botu.chat_id,
            text="🕙 <b>Otonom Tarama Başladı!</b>\nTüm pazaryerleri taranıyor, gerçek indirimler doğrulanıyor...",
            parse_mode="HTML",
        )
        firsatlar = await asyncio.to_thread(firsat_botu.motor.gunluk_top30_tarama_calistir, None)
        await firsat_botu.top30_raporu_gonder(app, firsat_botu.chat_id, firsatlar)
    finally:
        await app.shutdown()


def main() -> None:
    print(f"Otonom Fırsat İstihbarat Motoru başlatılıyor... ({datetime.now(TR_TZ):%d.%m.%Y %H:%M})")
    firsat_botu = FirsatBotu()

    # GitHub Actions (CI_MODE=true) veya TEK_SEFER=true ise tarayıp hemen çıkar
    if os.getenv("CI_MODE") == "true" or os.getenv("TEK_SEFER") == "true":
        asyncio.run(tek_seferlik_tarama_ve_gonder(firsat_botu))
        print("Tek seferlik tarama tamamlandı, çıkılıyor.")
        return

    # Yerel bilgisayarda sürekli çalıştırmak istersen polling + günlük 10:00 zamanlayıcı aktif kalır
    from datetime import time as dt_time

    async def saat_10_otonom_gorev(context) -> None:
        bot_instance: FirsatBotu = context.job.data
        if not bot_instance.chat_id:
            return
        await context.bot.send_message(
            chat_id=bot_instance.chat_id,
            text="🕙 <b>Saat 10:00 Otonom Tarama Başladı!</b>\nTüm pazaryerleri taranıyor, gerçek indirimler doğrulanıyor...",
            parse_mode="HTML",
        )
        firsatlar = await asyncio.to_thread(bot_instance.motor.gunluk_top30_tarama_calistir, None)
        await bot_instance.top30_raporu_gonder(context.application, bot_instance.chat_id, firsatlar)

    app = Application.builder().token(firsat_botu.token).build()
    app.add_handler(CommandHandler("start", firsat_botu.start_komutu))
    app.add_handler(CommandHandler("tara", firsat_botu.tara_komutu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, firsat_botu.manuel_mesaj_uyari))

    app.job_queue.run_daily(
        saat_10_otonom_gorev,
        time=dt_time(hour=10, minute=0, tzinfo=TR_TZ),
        data=firsat_botu,
        name="gunluk_10_top30_tarama",
    )

    print(f"[Zamanlayıcı] Günde 1 kez saat 10:00 (TR) taraması aktif. Şu an: {datetime.now(TR_TZ):%d.%m.%Y %H:%M}")
    app.run_polling()


if __name__ == "__main__":
    main()