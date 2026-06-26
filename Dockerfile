# Бэкенд: модульный монолит FastAPI. Образ для локального кластера.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Сначала метаданные пакета — для кэширования слоя зависимостей.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --upgrade pip && pip install -e .

# Остальное (alembic, конфиги, сид) — отдельным слоем.
COPY alembic ./alembic
COPY alembic.ini ./
COPY seed.py ./

EXPOSE 8000

# Миграции применяются командой сервиса в docker-compose перед стартом.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
