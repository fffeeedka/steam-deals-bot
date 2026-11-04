"""Telegram-бот: находит бесплатные новинки и скидки Steam, присылает с обложкой."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from . import steam, view
from .storage import Chat, Store

logger = logging.getLogger(__name__)

WELCOME = "🎁 <b>Новинки и скидки Steam</b>"


@dataclass
class Config:
    token: str
    cc: str = "us"
    db_path: Path = Path("data/bot.db")
    applist_cache: Path = Path("data/applist.json")
    interval_minutes: int = 15
    max_per_run: int = 5
    new_lookup_limit: int = 120

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("Не задан BOT_TOKEN. Получи токен у @BotFather.")
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        return cls(
            token=token,
            cc=os.getenv("STEAM_CC", "us").strip() or "us",
            db_path=data_dir / "bot.db",
            applist_cache=data_dir / "applist.json",
            interval_minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "15")),
            max_per_run=int(os.getenv("MAX_PER_RUN", "5")),
            new_lookup_limit=int(os.getenv("NEW_LOOKUP_LIMIT", "120")),
        )


# --- клавиатуры ----------------------------------------------------------


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings"),
                InlineKeyboardButton("⭐ Жанры", callback_data="menu:priority"),
            ],
            [
                InlineKeyboardButton("🔄 Проверить", callback_data="menu:scan"),
                InlineKeyboardButton("📋 Последние", callback_data="menu:latest"),
            ],
        ]
    )


def settings_menu(chat: Chat) -> InlineKeyboardMarkup:
    mode = chat.settings.get("mode", "free")
    rows = []
    for key, label in (
        ("free", "🎁 Бесплатное"),
        ("new", "🆕 Новинки"),
        ("deals", "💸 Скидки"),
    ):
        prefix = "✅ " if key == mode else ""
        rows.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"mode:{key}")])

    discount = int(chat.settings.get("min_discount", 50))
    rows.append(
        [
            InlineKeyboardButton("−10%", callback_data="disc:down"),
            InlineKeyboardButton(f"от {discount}%", callback_data="menu:noop"),
            InlineKeyboardButton("+10%", callback_data="disc:up"),
        ]
    )
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def priority_menu(chat: Chat) -> InlineKeyboardMarkup:
    chosen = {genre.lower() for genre in chat.settings.get("priority_genres", [])}
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for index, genre in enumerate(view.KNOWN_GENRES):
        mark = "✅ " if genre.lower() in chosen else ""
        row.append(InlineKeyboardButton(mark + genre, callback_data=f"pri:{index}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


# --- сбор находок --------------------------------------------------------


def _looks_interesting(game: steam.Game) -> bool:
    if game.coming_soon:
        return False
    return game.promo or game.is_free or game.discount >= 40


def collect_new(config: Config, store: Store) -> list[steam.Game]:
    """Свежие appid + текущие бесплатные акции."""
    found: dict[int, steam.Game] = {}

    floor = int(store.meta_get("max_appid", "0") or 0)
    fresh: list[int] = []

    try:
        if floor == 0:
            floor = steam.current_max_appid(config.applist_cache)
            store.meta_set("max_appid", str(floor))
            logger.info("стартовая граница appid: %s", floor)
        else:
            fresh = steam.apps_above(floor, config.applist_cache, limit=config.new_lookup_limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("не удалось получить список приложений: %s", exc)

    highest = floor
    for appid in fresh:
        highest = max(highest, appid)
        game = steam.app_details(appid, cc=config.cc)
        if game is not None and _looks_interesting(game):
            found[game.appid] = game
        time.sleep(1.0)

    for appid in steam.search_appids("freebie", cc=config.cc, pages=2):
        if appid in found:
            continue
        game = steam.app_details(appid, cc=config.cc)
        if game is not None and (game.promo or game.is_free):
            found[game.appid] = game
        time.sleep(1.0)

    if highest > floor:
        store.meta_set("max_appid", str(highest))

    store.meta_set(
        "last_found",
        json.dumps(
            [{"appid": g.appid, "name": g.name, "url": g.url} for g in found.values()][:10],
            ensure_ascii=False,
        ),
    )
    return list(found.values())


async def _send_game(app: Application, chat_id: int, game: steam.Game, chat: Chat) -> None:
    caption = view.card(game, chat)
    try:
        await app.bot.send_photo(
            chat_id=chat_id,
            photo=game.cover,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:  # noqa: BLE001 - обложка может не отдаться
        logger.info("обложка не ушла (%s), шлю текстом", exc)
        await app.bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML)


async def scan_and_send(app: Application, force: bool = False) -> int:
    config: Config = app.bot_data["config"]
    store: Store = app.bot_data["store"]

    games = await asyncio.to_thread(collect_new, config, store)
    if not games:
        logger.info("новых игр не найдено")
        return 0

    sent_total = 0
    for chat in store.all_chats():
        if not chat.settings.get("enabled", True):
            continue
        if not force and time.time() - float(store.meta_get(f"last_scan_{chat.chat_id}", "0") or 0) < 60:
            continue

        fresh = [
            game
            for game in games
            if view.passes(game, chat) and not store.was_sent(chat.chat_id, game.appid)
        ]
        fresh.sort(key=lambda game: view.score(game, chat), reverse=True)

        for game in fresh[: config.max_per_run]:
            try:
                await _send_game(app, chat.chat_id, game, chat)
            except Forbidden:
                logger.info("чат %s заблокировал бота — убираю", chat.chat_id)
                store.remove_chat(chat.chat_id)
                break
            except RetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except Exception as exc:  # noqa: BLE001
                logger.warning("не смог отправить в %s: %s", chat.chat_id, exc)
                continue

            store.mark_sent(chat.chat_id, game.appid)
            sent_total += 1
            await asyncio.sleep(3)

        store.meta_set(f"last_scan_{chat.chat_id}", str(int(time.time())))

    logger.info("отправлено сообщений: %s", sent_total)
    return sent_total


# --- команды -------------------------------------------------------------


def _chat(update: Update, store: Store) -> Chat:
    return store.add_chat(update.effective_chat.id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    _chat(update, store)
    await update.message.reply_text(
        WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu()
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    await update.message.reply_text(
        view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=settings_menu(chat)
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    args = context.args or []

    if not args or args[0].lower() not in view.MODE_LABELS:
        await update.message.reply_text(
            view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=settings_menu(chat)
        )
        return

    store.update_setting(chat.chat_id, "mode", args[0].lower())
    chat = store.get_chat(chat.chat_id) or chat
    await update.message.reply_text(
        view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=settings_menu(chat)
    )


async def cmd_discount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    args = context.args or []

    if args and args[0].isdigit():
        value = max(1, min(int(args[0]), 99))
        store.update_setting(chat.chat_id, "min_discount", value)
        chat = store.get_chat(chat.chat_id) or chat

    await update.message.reply_text(
        view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=settings_menu(chat)
    )


async def cmd_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    raw = " ".join(context.args or []).replace(";", ",")
    genres = [part.strip() for part in raw.split(",") if part.strip()]

    if genres:
        store.update_setting(chat.chat_id, "priority_genres", genres)
        chat = store.get_chat(chat.chat_id) or chat

    await update.message.reply_text(
        view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=priority_menu(chat)
    )


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    raw = " ".join(context.args or []).replace(";", ",")
    genres = [part.strip() for part in raw.split(",") if part.strip()]

    if genres:
        store.update_setting(chat.chat_id, "blocked_genres", genres)
        chat = store.get_chat(chat.chat_id) or chat

    await update.message.reply_text(
        view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=settings_menu(chat)
    )


async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    await update.message.reply_text(
        _latest_text(store), parse_mode=ParseMode.HTML, reply_markup=main_menu()
    )


def _latest_text(store: Store) -> str:
    try:
        items = json.loads(store.meta_get("last_found", "[]"))
    except json.JSONDecodeError:
        items = []
    if not items:
        return "Пока пусто — новых находок ещё не было. Первый прогон может занять пару минут."
    lines = [
        f'• <a href="{html.escape(item["url"])}">{html.escape(item["name"])}</a>'
        for item in items[:10]
    ]
    return "Последние находки:\n" + "\n".join(lines)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    last = float(store.meta_get("last_manual_scan", "0") or 0)

    if time.time() - last < 300:
        await update.message.reply_text(
            f"Слишком часто. Попробуй через {int(300 - (time.time() - last))} сек."
        )
        return

    store.meta_set("last_manual_scan", str(int(time.time())))
    message = await update.message.reply_text("Проверяю Steam, это займёт до минуты…")
    sent = await scan_and_send(context.application, force=True)
    await message.edit_text(
        f"Готово. Отправлено новых игр: {sent}" if sent else "Нового ничего нет."
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    store.update_setting(chat.chat_id, "enabled", False)
    await update.message.reply_text("Рассылка выключена. Вернуть — /start")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


# --- кнопки --------------------------------------------------------------


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    store: Store = context.application.bot_data["store"]
    chat = store.add_chat(query.message.chat_id)
    data = query.data or ""

    if data == "menu:noop":
        return

    if data.startswith("mode:"):
        store.update_setting(chat.chat_id, "mode", data.split(":", 1)[1])
        chat = store.get_chat(chat.chat_id) or chat
        await query.edit_message_text(
            view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=settings_menu(chat)
        )
        return

    if data.startswith("disc:"):
        step = 10 if data.endswith("up") else -10
        value = max(10, min(int(chat.settings.get("min_discount", 50)) + step, 90))
        store.update_setting(chat.chat_id, "min_discount", value)
        chat = store.get_chat(chat.chat_id) or chat
        await query.edit_message_text(
            view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=settings_menu(chat)
        )
        return

    if data.startswith("pri:"):
        try:
            genre = view.KNOWN_GENRES[int(data.split(":", 1)[1])]
        except (ValueError, IndexError):
            return
        current = list(chat.settings.get("priority_genres", []))
        if any(genre.lower() == item.lower() for item in current):
            current = [item for item in current if item.lower() != genre.lower()]
        else:
            current.append(genre)
        store.update_setting(chat.chat_id, "priority_genres", current)
        chat = store.get_chat(chat.chat_id) or chat
        await query.edit_message_text(
            view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=priority_menu(chat)
        )
        return

    if data == "menu:scan":
        last = float(store.meta_get("last_manual_scan", "0") or 0)
        if time.time() - last < 120:
            await query.edit_message_text(
                f"Проверял недавно. Попробуй через {int(120 - (time.time() - last))} сек.",
                reply_markup=main_menu(),
            )
            return
        store.meta_set("last_manual_scan", str(int(time.time())))
        await query.edit_message_text("Проверяю Steam, это займёт до минуты…")
        sent = await scan_and_send(context.application, force=True)
        await query.edit_message_text(
            f"Готово, отправил {sent} игр." if sent else "Нового ничего нет.",
            reply_markup=main_menu(),
        )
        return

    if data == "menu:latest":
        await query.edit_message_text(
            _latest_text(store), parse_mode=ParseMode.HTML, reply_markup=main_menu()
        )
        return

    if data == "menu:settings":
        await query.edit_message_text(
            view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=settings_menu(chat)
        )
        return

    if data == "menu:priority":
        await query.edit_message_text(
            view.settings_text(chat), parse_mode=ParseMode.HTML, reply_markup=priority_menu(chat)
        )
        return

    # menu:main и всё неизвестное
    await query.edit_message_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=main_menu())


# --- планировщик и запуск ------------------------------------------------


async def _health_server(port: int) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readline(), timeout=5)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 2\r\nConnection: close\r\n\r\nok"
            )
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    logger.info("health-check слушает порт %s", port)
    async with server:
        await server.serve_forever()


async def _scan_loop(app: Application) -> None:
    """Планировщик на asyncio — без APScheduler и лишних зависимостей."""
    interval = app.bot_data["config"].interval_minutes * 60
    await asyncio.sleep(30)
    while True:
        try:
            await scan_and_send(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("ошибка в плановом прогоне")
        await asyncio.sleep(interval)


async def _post_init(app: Application) -> None:
    app.bot_data["store"] = Store(app.bot_data["config"].db_path)
    logger.info("бот запущен, интервал проверки %s мин", app.bot_data["config"].interval_minutes)

    port = os.getenv("PORT", "").strip()
    if port.isdigit():
        app.create_task(_health_server(int(port)), name="health-server")

    app.create_task(_scan_loop(app), name="scan-loop")


async def _post_shutdown(app: Application) -> None:
    store = app.bot_data.get("store")
    if store is not None:
        store.close()


def build_app(config: Config) -> Application:
    app = (
        Application.builder()
        .token(config.token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["config"] = config

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("discount", cmd_discount))
    app.add_handler(CommandHandler("priority", cmd_priority))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))

    return app


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    app = build_app(Config.from_env())
    app.run_polling(allowed_updates=Update.ALL_TYPES)
