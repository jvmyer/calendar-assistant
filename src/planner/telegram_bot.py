"""Telegram-Anschluss: Meldungen senden (`TelegramNotifier`) und Eingaben empfangen (Long Polling).

Sicherheit:
- Nur `settings.telegram_user_id` darf den Bot bedienen; alle anderen werden still ignoriert.
- Der Bot-Token steckt in jeder API-URL. Deshalb loggen wir nie URLs oder Bibliotheksfehlertexte,
  sondern nur Fehlerklassen, und stellen die HTTP-Logger auf WARNING.
- Kein offener Port: Long Polling, nur ausgehende Verbindungen.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import closing

import httpx

from .bot_logic import Buttons, handle_callback, handle_message, proposal_buttons
from .config import Settings
from .db import connect

log = logging.getLogger("planner.telegram")
API = "https://api.telegram.org"
MAX_LEN = 4000   # Telegram-Limit: 4096 Zeichen

COMMANDS = [
    ("heute", "Termine, Blocker und Fälliges von heute"),
    ("aufgaben", "Offene Aufgaben"),
    ("vorschlaege", "Offene Vorschläge"),
    ("ilias", "Neueste ILIAS-Neuigkeiten"),
    ("log", "Letzte Änderungen"),
    ("hilfe", "Hilfe"),
]


def quiet_http_loggers() -> None:
    """httpx/httpcore loggen auf INFO die URL – und damit den Token. Das unterbinden wir."""
    for name in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _keyboard(buttons: Buttons) -> dict | None:
    if not buttons:
        return None
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}


class TelegramNotifier:
    """Sendet Meldungen an den Nutzer. Fehler stoppen den Dienst nie (nur Warnung ohne URL)."""

    def __init__(self, token: str, chat_id: int, *, post=None):
        self._token = token
        self._chat_id = chat_id
        self._post = post or httpx.post

    def _call(self, method: str, payload: dict) -> bool:
        try:
            resp = self._post(f"{API}/bot{self._token}/{method}", json=payload, timeout=15)
        except Exception as exc:  # noqa: BLE001 – bewusst breit; nie str(exc): kann URL samt Token enthalten
            log.warning("Telegram %s fehlgeschlagen: %s", method, type(exc).__name__)
            return False
        if resp.status_code != 200:
            log.warning("Telegram %s: HTTP %s", method, resp.status_code)
            return False
        return True

    def send(self, text: str, buttons: Buttons | None = None) -> bool:
        payload: dict = {"chat_id": self._chat_id, "text": text[:MAX_LEN], "disable_web_page_preview": True}
        markup = _keyboard(buttons or [])
        if markup:
            payload["reply_markup"] = markup
        return self._call("sendMessage", payload)

    def send_proposal(self, proposal_id: int, text: str) -> bool:
        return self.send(text, proposal_buttons(proposal_id))


def start_bot(settings: Settings, wake: threading.Event, stop: threading.Event) -> threading.Thread:
    """Startet den Empfang in einem eigenen Thread (eigener asyncio-Loop)."""
    quiet_http_loggers()

    def runner() -> None:
        try:
            asyncio.run(_run(settings, wake, stop))
        except Exception as exc:  # noqa: BLE001 – Thread darf den Dienst nicht beenden; kein Text (Token)
            log.error("Telegram-Bot beendet: %s", type(exc).__name__)

    thread = threading.Thread(target=runner, name="telegram-bot", daemon=True)
    thread.start()
    return thread


async def _run(settings: Settings, wake: threading.Event, stop: threading.Event) -> None:
    from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    allowed = settings.telegram_user_id

    def markup(buttons: Buttons):
        if not buttons:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in buttons])

    def is_allowed(update: Update) -> bool:
        user = update.effective_user
        if user is None or user.id != allowed:
            log.warning("Nachricht von fremdem Absender ignoriert")
            return False
        return True

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_allowed(update) or update.message is None or update.message.text is None:
            return
        with closing(connect(settings.db_path)) as conn:
            reply = handle_message(conn, update.message.text, message_id=update.message.message_id)
        await update.message.reply_text(reply.text, reply_markup=markup(reply.buttons))
        if reply.changed:
            wake.set()

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if not is_allowed(update):
            return
        with closing(connect(settings.db_path)) as conn:
            reply = handle_callback(conn, query.data or "")
        await query.edit_message_text(reply.text, reply_markup=markup(reply.buttons))
        if reply.changed:
            wake.set()

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Bot-Fehler: %s", type(context.error).__name__)   # kein Text: könnte die URL enthalten

    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(MessageHandler(filters.TEXT, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    async with app:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        await app.start()
        await app.updater.start_polling(allowed_updates=["message", "callback_query"])
        log.info("Telegram-Bot läuft (Long Polling)")
        while not stop.is_set():
            await asyncio.sleep(1)
        await app.updater.stop()
        await app.stop()

