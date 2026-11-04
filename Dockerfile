FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY steam_deals_bot ./steam_deals_bot

# База и кэш каталога живут вне контейнера, чтобы настройки не терялись.
VOLUME ["/app/data"]

CMD ["python", "-m", "steam_deals_bot"]
