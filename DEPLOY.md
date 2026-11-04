# Деплой бота на хостинг

Бот — это long-polling процесс, ему нужен **постоянно работающий воркер** и постоянный диск под SQLite. Ниже самый короткий путь.

## 1. Репозиторий на GitHub

```bash
cd steam-deals-bot
gh auth login                       # если gh не авторизован
gh repo create steam-deals-bot --public --source=. --push
```

Без `gh`: создай пустой репозиторий на github.com, затем

```bash
git remote add origin https://github.com/<логин>/steam-deals-bot.git
git push -u origin main
```

## 2. Railway (проще всего)

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → выбери `steam-deals-bot`.
2. Railway сам найдёт `Dockerfile` и соберёт образ.
3. Вкладка **Variables** → добавь:
   - `BOT_TOKEN` — токен от BotFather;
   - `STEAM_CC` — `ru` или `us` (по желанию);
   - `CHECK_INTERVAL_MINUTES` — например `15`.
4. Вкладка **Volumes** → **Add Volume**, mount path `/app/data`. Без этого подписки и настройки чатов обнулятся при каждом перезапуске.
5. **Deploy** → вкладка **Logs**. Должно появиться:

   ```
   бот запущен, интервал проверки 15 мин
   ```

6. В Telegram: `/start`, `/mode free`, `/priority indie,horror`, `/scan`.

## Альтернативы

| Хостинг | Плюсы | Минусы |
| --- | --- | --- |
| Railway | деплой в 5 кликов, Docker из коробки | бесплатный период ограничен, дальше ~$5/мес |
| Fly.io | `fly launch` подхватывает Dockerfile, дешёвые мелкие машины | нужна карта |
| Oracle Cloud Always Free | VPS бесплатно бессрочно | регистрация с картой и ручная настройка Docker |
| Свой VPS | полный контроль | нужен сервер |

Тарифы у PaaS меняются — сверься с актуальной страницей тарифов перед оплатой.

## 3. Деплой на VPS (любой из них)

```bash
scp -r steam-deals-bot user@server:/opt/
ssh user@server
cd /opt/steam-deals-bot
cp .env.example .env && nano .env          # вписать BOT_TOKEN
docker compose up -d --build
docker compose logs -f
```

## Обновление

```bash
git pull
docker compose up -d --build     # на VPS
```

На Railway деплой запускается сам после пуша в `main`.

## Если в логах `telegram.error.TimedOut`

Хостинг не может достучаться до `api.telegram.org` — смени регион сервера (Railway: Settings → Region, желательно EU/US) или хостинг. Steam API при этом обычно доступен.

## Безопасность

- Токен живёт только в переменных окружения хостинга, в репозиторий он не попадает (`.env` в `.gitignore`).
- Если токен где-то засветился — BotFather `/revoke` и обнови переменную.
