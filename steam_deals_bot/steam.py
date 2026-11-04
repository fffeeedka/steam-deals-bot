"""Работа с публичным Steam API: поиск новинок, цены, жанры.

Никакой авторизации — только открытые эндпоинты Steam Store.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "steam-deals-bot/1.0"
SEARCH_URL = "https://store.steampowered.com/search/results/"
DETAILS_URL = "https://store.steampowered.com/api/appdetails"
APPLIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"

_APPID_RE = re.compile(r'data-ds-appid="([\d,]+)"')


@dataclass
class Game:
    appid: int
    name: str
    is_free: bool = False  # постоянно бесплатная (F2P)
    promo: bool = False  # временная раздача 100% — остаётся в библиотеке
    discount: int = 0
    initial: int = 0  # цена до скидки, в копейках валюты региона
    final: int = 0
    price_text: str = ""
    original_text: str = ""
    genres: list[str] = field(default_factory=list)
    release_date: str = ""
    coming_soon: bool = False
    description: str = ""

    @property
    def url(self) -> str:
        return f"https://store.steampowered.com/app/{self.appid}/"

    @property
    def cover(self) -> str:
        """Обложка 460x215 с CDN Steam — Telegram скачает её сам."""
        return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{self.appid}/header.jpg"


def get_json(url: str, params: dict | None = None, attempts: int = 3, timeout: int = 25):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last_error: Exception | None = None

    for attempt in range(attempts):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))

    raise last_error  # type: ignore[misc]


# --- поиск ---------------------------------------------------------------


def search_appids(kind: str, cc: str = "us", pages: int = 2, page_size: int = 100) -> list[int]:
    """appid'ы из поиска Steam.

    kind: "new" — новинки, "freebie" — бесплатные акции, "deals" — скидки.
    """
    params: dict = {
        "query": "",
        "count": page_size,
        "dynamic_data": "",
        "category1": 998,  # только игры
        "infinite": 1,
        "cc": cc,
        "l": "english",
    }
    if kind == "new":
        params["sort_by"] = "Released_DESC"
    elif kind == "freebie":
        params.update({"sort_by": "Price_ASC", "specials": 1, "maxprice": "free"})
    elif kind == "catalog":
        # Все бесплатные игры каталога, а не только акции: free-to-play и «навсегда бесплатно».
        params.update({"sort_by": "Price_ASC", "maxprice": "free"})
    elif kind == "deals":
        params.update({"sort_by": "Discount_DESC", "specials": 1})
    else:
        raise ValueError(f"неизвестный тип поиска: {kind}")

    found: list[int] = []
    for page in range(max(pages, 1)):
        params["start"] = page * page_size
        try:
            payload = get_json(SEARCH_URL, params)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] поиск {kind}, страница {page}: {exc}")
            break

        html = payload.get("results_html") or ""
        matches = _APPID_RE.findall(html)
        for chunk in matches:
            for token in chunk.split(","):
                if token.isdigit() and int(token) not in found:
                    found.append(int(token))
        if len(matches) < page_size:
            break
        time.sleep(1.0)

    return found


def search_by_term(term: str, cc: str = "us", limit: int = 20) -> list[int]:
    """appid'ы по поисковому запросу — для кнопки «Поиск»."""
    try:
        payload = get_json(
            SEARCH_URL,
            {
                "term": term,
                "category1": 998,
                "infinite": 1,
                "count": 100,
                "start": 0,
                "cc": cc,
                "l": "english",
                "dynamic_data": "",
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] поиск «{term}»: {exc}")
        return []

    found: list[int] = []
    for chunk in _APPID_RE.findall(payload.get("results_html") or ""):
        for token in chunk.split(","):
            if token.isdigit() and int(token) not in found:
                found.append(int(token))
    return found[:limit]


def _collect_ids(payload: dict, into: list[int]) -> int:
    matches = _APPID_RE.findall(payload.get("results_html") or "")
    for chunk in matches:
        for token in chunk.split(","):
            if token.isdigit() and int(token) not in into:
                into.append(int(token))
    return len(matches)


def search_by_genre(genre: str, cc: str = "us", pages: int = 3, limit: int = 60) -> list[int]:
    """Все игры жанра из каталога Steam — без фильтра «новинки»."""
    params = {
        "genre": genre,
        "category1": 998,
        "infinite": 1,
        "count": 100,
        "start": 0,
        "cc": cc,
        "l": "english",
        "dynamic_data": "",
    }
    ids: list[int] = []
    for page in range(max(pages, 1)):
        params["start"] = page * 100
        try:
            payload = get_json(SEARCH_URL, params)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] жанр «{genre}», страница {page}: {exc}")
            break
        if _collect_ids(payload, ids) < 100:
            break
        time.sleep(1.0)

    if not ids:  # если Steam не понял параметр жанра — ищем по слову
        ids = search_by_term(genre, cc=cc, limit=limit)
    return ids[:limit]


# --- карточка игры -------------------------------------------------------


def app_details(appid: int, cc: str = "us", lang: str = "english") -> Game | None:
    try:
        payload = get_json(
            DETAILS_URL,
            {"appids": appid, "cc": cc, "l": lang, "filters": "basic,price_overview"},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] appdetails {appid}: {exc}")
        return None

    entry = payload.get(str(appid)) or {}
    if not entry.get("success"):
        return None

    data = entry.get("data") or {}
    if data.get("type") != "game":
        return None

    price = data.get("price_overview") or {}
    release = data.get("release_date") or {}
    initial = price.get("initial") or 0
    final = price.get("final") or 0

    game = Game(
        appid=appid,
        name=(data.get("name") or str(appid)).strip(),
        is_free=bool(data.get("is_free")),
        promo=bool(not data.get("is_free") and initial > 0 and final == 0),
        discount=int(price.get("discount_percent") or 0),
        initial=initial,
        final=final,
        price_text=price.get("final_formatted") or "",
        original_text=price.get("initial_formatted") or "",
        genres=[g.get("description", "") for g in data.get("genres") or [] if g.get("description")],
        release_date=release.get("date") or "",
        coming_soon=bool(release.get("coming_soon")),
        description=data.get("short_description") or "",
    )
    return game


# --- список всех приложений (для поиска новинок) --------------------------


def app_list(cache_path: Path, max_age_days: int = 7) -> list[dict]:
    """Полный список приложений Steam, кэшируется на диск."""
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < max_age_days * 86400:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))["applist"]["apps"]
        except (OSError, KeyError, json.JSONDecodeError):
            pass

    payload = get_json(APPLIST_URL)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    return payload["applist"]["apps"]


def current_max_appid(cache_path: Path) -> int:
    """Наибольший appid в каталоге — стартовая граница для «новинок»."""
    return max((int(app.get("appid", 0)) for app in app_list(cache_path)), default=0)


def apps_above(appid_floor: int, cache_path: Path, limit: int = 150) -> list[int]:
    """appid'ы новее уже известного максимума.

    appid в Steam растут монотонно, поэтому «новые» приложения — это просто
    те, чей номер больше последнего увиденного. Дешевле, чем проверять даты.
    """
    apps = app_list(cache_path)
    fresh = sorted(
        int(app["appid"]) for app in apps if int(app.get("appid", 0)) > appid_floor
    )
    return fresh[:limit]
