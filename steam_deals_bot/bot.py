"""Telegram-бот: находит бесплатные новинки Steam и рассылает их по приоритетам."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes

from . import steam, view
from .storage import Chat, Store

logger = logging.getLogger(__name__)


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


# --- сбор находок (блокирующе, выполняется в отдельном потоке) -------------


def _looks_interesting(game: steam.Game) -> bool:
    if game.coming_soon:
        return False
    return game.promo or game.is_free or game.discount >= 40


def collect_new(config: Config, store: Store) -> list[steam.Game]:
    """Новые бесплатные/интересные игры: свежие appid + текущие раздачи."""
    found: dict[int, steam.Game] = {}

    floor = int(store.meta_get("max_appid", "0") or 0)
    fresh: list[int] = []

    try:
        if floor == 0:
            # Первый запуск: фиксируем текущий максимум каталога и не заваливаем
            # чат историей всех приложений Steam.
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


async def scan_and_send(app: Application, force: bool = False) -> int:
    config: Config = app.bot_data["config"]
    store: Store = app.bot_data["store"]

    games = await asyncio.to_thread(collect_new, config, store)
    if not games:
        logger.info("новых игр не найдено")
        return 0

    chats = store.all_chats()
    sent_total = 0

    for chat in chats:
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
                await app.bot.send_message(
                    chat_id=chat.chat_id,
                    text=view.card(game, chat),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
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
            await asyncio.sleep(3)  # лимит Telegram: не больше ~20 сообщений в минуту

        store.meta_set(f"last_scan_{chat.chat_id}", str(int(time.time())))

    logger.info("отправлено сообщений: %s", sent_total)
    return sent_total


# --- хендлеры ------------------------------------------------------------


def _chat(update: Update, store: Store) -> Chat:
    chat = store.add_chat(update.effective_chat.id)
    return chat


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    _chat(update, store)
    await update.message.reply_text(
        "Привет! Я слежу за Steam и присылаю бесплатные новинки и раздачи.\n\n"
        "Я ничего не покупаю и не захожу в аккаунт — только присылаю ссылку, "
        "забрать игру в библиотеку нужно самому.\n\n"
        "<b>Команды</b>\n"
        "/settings — текущие настройки\n"
        "/mode free|new|deals — что присылать\n"
        "/discount 60 — минимальная скидка для режима deals\n"
        "/priority indie,horror — что показать первым\n"
        "/block early access — что скрыть\n"
        "/latest — последние находки\n"
        "/scan — проверить прямо сейчас\n"
        "/stop — отключить рассылку",
        parse_mode=ParseMode.HTML,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    await update.message.reply_text(view.settings_text(chat), parse_mode=ParseMode.HTML)


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    args = context.args or []

    if not args or args[0].lower() not in view.MODE_LABELS:
        options = "\n".join(f"• <b>{key}</b> — {label}" for key, label in view.MODE_LABELS.items())
        await update.message.reply_text(
            f"Текущий режим: <b>{chat.settings.get('mode')}</b>\n\n{options}\n\n"
            "Пример: <code>/mode deals</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    store.update_setting(chat.chat_id, "mode", args[0].lower())
    await update.message.reply_text(
        f"Готово: {view.MODE_LABELS[args[0].lower()]}", parse_mode=ParseMode.HTML
    )


async def cmd_discount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    args = context.args or []

    if not args or not args[0].isdigit():
        await update.message.reply_text(
            f"Сейчас: <b>{chat.settings.get('min_discount')}%</b>\n"
            "Пример: <code>/discount 60</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    value = max(1, min(int(args[0]), 99))
    store.update_setting(chat.chat_id, "min_discount", value)
    await update.message.reply_text(f"Минимальная скидка: {value}%")


def _parse_genres(raw_args: list[str]) -> list[str]:
    text = " ".join(raw_args).replace(";", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


async def cmd_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    genres = _parse_genres(context.args or [])

    if not genres:
        current = ", ".join(chat.settings.get("priority_genres") or []) or "—"
        await update.message.reply_text(
            f"⭐ Приоритетные жанры: {html.escape(current)}\n\n"
            f"Доступные жанры: {', '.join(view.KNOWN_GENRES)}\n\n"
            "Пример: <code>/priority indie,horror</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    store.update_setting(chat.chat_id, "priority_genres", genres)
    await update.message.reply_text(
        "Приоритет задан: " + ", ".join(genres) + "\nТакие игры будут выше в выдаче и со звёздочкой ⭐"
    )


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    genres = _parse_genres(context.args or [])

    if not genres:
        current = ", ".join(chat.settings.get("blocked_genres") or []) or "—"
        await update.message.reply_text(
            f"🚫 Скрытые жанры: {html.escape(current)}\n"
            "Пример: <code>/block early access</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    store.update_setting(chat.chat_id, "blocked_genres", genres)
    await update.message.reply_text("Скрыто: " + ", ".join(genres))


async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    raw = store.meta_get("last_found", "[]")
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        items = []

    if not items:
        await update.message.reply_text(
            "Пока пусто — бот ещё не нашёл ничего нового. Первый прогон может занять пару минут."
        )
        return

    lines = [f"• <a href=\"{html.escape(item['url'])}\">{html.escape(item['name'])}</a>" for item in items[:10]]
    await update.message.reply_text(
        "Последние находки:\n" + "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    last = float(store.meta_get("last_manual_scan", "0") or 0)

    if time.time() - last < 300:
        wait = int(300 - (time.time() - last))
        await update.message.reply_text(f"Слишком часто. Попробуй через {wait} сек.")
        return

    store.meta_set("last_manual_scan", str(int(time.time())))
    message = await update.message.reply_text("Проверяю Steam, это займёт до минуты…")
    sent = await scan_and_send(context.application, force=True)
    await message.edit_text(f"Готово. Отправлено новых игр: {sent}" if sent else "Нового ничего нет.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: Store = context.application.bot_data["store"]
    chat = _chat(update, store)
    store.update_setting(chat.chat_id, "enabled", False)
    await update.message.reply_text("Рассылка выключена. Вернуть — /start")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await scan_and_send(context.application)
    except Exception:  # noqa: BLE001
        logger.exception("ошибка в плановом прогоне")


# --- запуск --------------------------------------------------------------


async def _health_server(port: int) -> None:
    """Минимальный HTTP-ответ на /, чтобы панели, ждущие веб-сервис, не рестартовали бота."""

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


async def _post_init(app: Application) -> None:
    app.bot_data["store"] = Store(app.bot_data["config"].db_path)
    logger.info("бот запущен, интервал проверки %s мин", app.bot_data["config"].interval_minutes)

    port = os.getenv("PORT", "").strip()
    if port.isdigit():
        app.create_task(_health_server(int(port)), name="health-server")


async def _post_shutdown(app: Application) -> None:
    store = app.bot_data.get("store")
    if store is not None:
        store.close()


def build_app(config: Config) -> Application:
    app = Application.builder().token(config.token).post_init(_post_init).post_shutdown(_post_shutdown).build()
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

    app.job_queue.run_repeating(
        scheduled_scan,
        interval=config.interval_minutes * 60,
        first=30,
        name="steam-scan",
    )
    return app


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    config = Config.from_env()
    app = build_app(config)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
