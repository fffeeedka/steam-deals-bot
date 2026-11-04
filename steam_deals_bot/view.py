"""Фильтры, приоритеты и текст карточки."""

from __future__ import annotations

import html

from .steam import Game
from .storage import Chat

KNOWN_GENRES = [
    "Action",
    "Adventure",
    "Casual",
    "Indie",
    "RPG",
    "Simulation",
    "Strategy",
    "Racing",
    "Sports",
    "Massively Multiplayer",
    "Early Access",
    "Free to Play",
    "Horror",
    "Puzzle",
    "Platformer",
    "Roguelike",
    "Survival",
    "Open World",
]

MODE_LABELS = {
    "free": "только бесплатное (F2P и раздачи)",
    "new": "все новинки",
    "deals": "скидки от указанного процента",
}


def _genres_lower(game: Game) -> set[str]:
    return {genre.lower() for genre in game.genres}


def is_blocked(game: Game, chat: Chat) -> bool:
    genres = _genres_lower(game)
    return any(blocked in genres for blocked in chat.blocked)


def priority_hit(game: Game, chat: Chat) -> bool:
    genres = _genres_lower(game)
    return any(priority in genres for priority in chat.priority)


def passes(game: Game, chat: Chat) -> bool:
    if game.coming_soon:
        return False
    if is_blocked(game, chat):
        return False

    mode = chat.settings.get("mode", "free")
    if mode == "free":
        return game.promo or game.is_free
    if mode == "new":
        return True
    if mode == "deals":
        return game.discount >= int(chat.settings.get("min_discount", 50))
    return False


def score(game: Game, chat: Chat) -> int:
    """Чем выше — тем раньше уйдёт в чат."""
    result = 0
    if game.promo:
        result += 50  # главное: бесплатно навсегда
    elif game.is_free:
        result += 35
    if priority_hit(game, chat):
        result += 25
    result += min(game.discount // 5, 20)
    return result


def _prices(game: Game) -> str:
    if game.promo:
        return "бесплатно навсегда"
    if game.is_free:
        return "free-to-play"
    if game.discount:
        was = f", было {game.original_text}" if game.original_text else ""
        return f"−{game.discount}%{was}"
    return game.price_text or "цена не указана"


def card(game: Game, chat: Chat) -> str:
    stars = "⭐ " if priority_hit(game, chat) else ""
    lines = [f"{stars}<b>{html.escape(game.name)}</b>", f"💰 {html.escape(_prices(game))}"]

    if game.genres:
        lines.append("🎮 " + html.escape(", ".join(game.genres[:4])))
    if game.release_date:
        lines.append("📅 " + html.escape(game.release_date))
    if game.description:
        text = game.description.strip()
        if len(text) > 220:
            text = text[:217].rstrip() + "…"
        lines.append(f"<i>{html.escape(text)}</i>")

    lines.append(f'<a href="{game.url}">Открыть в Steam</a>')
    return "\n".join(lines)


def settings_text(chat: Chat) -> str:
    settings = chat.settings
    priority = ", ".join(settings.get("priority_genres") or []) or "—"
    blocked = ", ".join(settings.get("blocked_genres") or []) or "—"
    mode = settings.get("mode", "free")
    return (
        "<b>Настройки</b>\n\n"
        f"Режим: <b>{mode}</b> — {MODE_LABELS.get(mode, '')}\n"
        f"Минимальная скидка: <b>{settings.get('min_discount', 50)}%</b>\n"
        f"⭐ Приоритетные жанры: {html.escape(priority)}\n"
        f"🚫 Скрытые жанры: {html.escape(blocked)}\n"
        f"🆓 Фри-каталог: {'включён' if settings.get('catalog', True) else 'выключен'}\n"
        f"Рассылка: {'включена' if settings.get('enabled') else 'выключена'}"
    )
