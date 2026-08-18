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

# Корни НУЦ Минцифры: эквайринг ТБанка отдаёт сертификат, выпущенный ими, а в
# certifi и системном хранилище их нет — без этого оплата падает на проверке
# сертификата (см. app/shared/http.py).
COPY certs ./certs

# Остальное (alembic, конфиги, сид, bootstrap-скрипты) — отдельным слоем.
COPY alembic ./alembic
COPY alembic.ini ./
COPY seed.py ./
# scripts/create_app_role.py — бутстрап роли БД приложения orakul_app (T9),
# запускается initContainer'ом в Helm-чарте (infra/helm/veraks).
COPY scripts ./scripts

EXPOSE 8000

# Миграции применяются командой сервиса в docker-compose перед стартом.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
