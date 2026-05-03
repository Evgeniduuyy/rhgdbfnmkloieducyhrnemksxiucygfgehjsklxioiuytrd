"""
Автозапуск и автоперезапуск Minesbot3.
Бот перезапускается при любом выходе или ошибке.
При каждом перезапуске администратор получает уведомление в Telegram.
"""

import subprocess
import sys
import time
import logging
import urllib.request
import urllib.parse
import json
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("restart.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("watchdog")

BOT_SCRIPT          = "Minesbot3__1777789514092.py"
BOT_TOKEN           = "8680465230:AAFB-jpZf4xYMOTi4uMUGAI18_tdebqh9CY"
ADMIN_ID            = 853173723
RESTART_DELAY       = 5
MAX_RESTART_DELAY   = 60
FAST_CRASH_THRESHOLD = 10


def send_telegram(text: str) -> None:
    """Отправляет сообщение администратору через Telegram Bot API."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": ADMIN_ID,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        log.warning("Не удалось отправить уведомление в Telegram: %s", e)


def run():
    restart_count = 0
    delay = RESTART_DELAY

    send_telegram("✅ <b>Watchdog запущен.</b>\nБот запускается...")

    while True:
        start_time = time.time()
        log.info("Запуск бота (попытка #%d)...", restart_count + 1)

        try:
            process = subprocess.run(
                [sys.executable, BOT_SCRIPT],
                check=False,
            )
            exit_code = process.returncode
        except Exception as e:
            log.error("Не удалось запустить процесс: %s", e)
            exit_code = -1

        elapsed = time.time() - start_time
        restart_count += 1

        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

        if exit_code == 0:
            log.warning(
                "Бот завершился штатно (код 0) за %.1f сек. Перезапуск через %d сек...",
                elapsed, delay,
            )
            msg = (
                f"⚠️ <b>Бот остановился</b>\n"
                f"🕐 Время: {now}\n"
                f"📋 Код выхода: 0 (штатное завершение)\n"
                f"⏱ Работал: {elapsed:.1f} сек\n"
                f"🔄 Перезапуск #{restart_count} через {delay} сек..."
            )
        else:
            log.error(
                "Бот упал с кодом %d за %.1f сек. Перезапуск через %d сек...",
                exit_code, elapsed, delay,
            )
            msg = (
                f"🔴 <b>Бот упал с ошибкой!</b>\n"
                f"🕐 Время: {now}\n"
                f"📋 Код выхода: {exit_code}\n"
                f"⏱ Работал: {elapsed:.1f} сек\n"
                f"🔄 Перезапуск #{restart_count} через {delay} сек..."
            )

        if elapsed < FAST_CRASH_THRESHOLD:
            delay = min(delay * 2, MAX_RESTART_DELAY)
            msg += f"\n⚡ Быстрый краш — задержка увеличена до {delay} сек."
            log.warning("Быстрый краш — увеличиваю задержку до %d сек.", delay)
        else:
            delay = RESTART_DELAY

        send_telegram(msg)

        log.info("Ожидание %d сек перед перезапуском...", delay)
        time.sleep(delay)

        send_telegram(f"🔄 <b>Бот перезапускается...</b> (попытка #{restart_count + 1})")


if __name__ == "__main__":
    log.info("=== Watchdog запущен ===")
    try:
        run()
    except KeyboardInterrupt:
        log.info("Watchdog остановлен вручную (Ctrl+C).")
        send_telegram("🛑 <b>Watchdog остановлен вручную.</b>\nБот больше не перезапускается.")
