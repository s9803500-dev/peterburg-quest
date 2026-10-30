"""
Точка входа для Render.

Render для Python-сервиса по умолчанию запускает `gunicorn app:app`.
Этот файл делает так, что при таком запуске:
  * Flask отдаёт порт и /health,
  * бот одновременно работает в фоновом потоке.

Если в панели Render указана команда `python bot.py`, этот файл просто
не используется — там всё делает bot.py. Работают оба варианта.
"""

import logging
import threading

from bot import app, keep_alive, run_bot, run_web

log = logging.getLogger("quest")


def _start_bot():
    try:
        run_bot()
    except Exception:  # noqa: BLE001
        log.exception("Бот упал при запуске")


# gunicorn импортирует модуль: поднимаем бота в фоне.
# Важно: у сервиса должен быть ОДИН воркер, иначе Telegram вернёт 409 Conflict.
threading.Thread(target=_start_bot, daemon=True).start()
threading.Thread(target=keep_alive, daemon=True).start()

# `app` — это WSGI-приложение, которое ищет gunicorn (app:app).
__all__ = ["app"]


if __name__ == "__main__":
    # запуск вида `python app.py` тоже должен работать
    threading.Thread(target=run_web, daemon=True).start()
    run_bot()
