# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

Бэкенд платформы прогнозов «Биржа репутации предсказателей» — **модульный
монолит на FastAPI** с гексагональной нарезкой по доменам, единой
PostgreSQL и фоновым воркером (ARQ). Архитектура и модель данных —
`../ARCHITECTURE.md`, формулы скоринга — `../scoring_system_design.md`,
дизайн-спеки фич — `docs/superpowers/specs/`, обзор монорепо — `../CLAUDE.md`.

## Команды

```bash
# окружение
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # затем заполнить секреты (ключи >= 32 байт)

# тесты
pytest                          # весь набор (e2e без спец-БД скипаются)
pytest tests/identity/unit      # только юнит (бизнес-логика)
pytest -k snils                 # один тест по подстроке
pytest path::test_name          # один конкретный тест
# e2e против реального Postgres — только выделенная БД с «e2e» в имени:
DATABASE_URL=postgresql+asyncpg://orakul:orakul@localhost:5432/orakul_e2e pytest tests/e2e

# качество
mypy app                        # строгая типизация (strict)
ruff check app tests            # линт

# миграции (нужен запущенный Postgres, DATABASE_URL из .env)
alembic upgrade head
alembic revision -m "msg" --autogenerate

# запуск
python seed.py                  # демо-данные (kalibr/mediana/baseline) + скоринг
uvicorn app.main:app --reload
arq app.worker.WorkerSettings   # фоновый воркер: скоринг, roll сезонов
```

## Архитектура (важно для продуктивности)

**Гексагональная нарезка внутри каждого домена** (`app/modules/<domain>/`).
Слои и направление зависимостей — строго внутрь:

- `domain/` — чистые сущности, value-objects, политики. **Без I/O**, без
  FastAPI/SQLAlchemy/pydantic. Здесь живёт бизнес-логика и её инварианты.
- `ports/` — абстрактные `Protocol`-интерфейсы (репозитории, внешние шлюзы,
  крипто). Прикладной слой зависит от них, а не от реализаций.
- `application/` — use-cases (по одному классу на операцию) + DTO. Оркеструют
  порты, получают зависимости через конструктор.
- `adapters/` — реализации портов: SQLAlchemy-репозитории, HTTP-клиенты,
  JWT/крипто, Redis-хранилища. ORM-модели маппятся на доменные сущности
  явными `to_domain`/`from_domain`, а не наследованием.
- `api/` — тонкий FastAPI-слой: pydantic-схемы, роутер, и `dependencies.py`
  как **composition root** домена (единственное место, где порты связываются
  с конкретными адаптерами через DI).

**Доменные ошибки** наследуются от базового `*Error` домена и маппятся в HTTP
централизованно в `app/main.py` (`@app.exception_handler`), а не в роутерах.

**Тестирование портов фейками.** Юнит-тесты гоняют use-cases с in-memory
фейками портов (`tests/<domain>/fakes.py`); интеграционные тесты поднимают
приложение и подменяют I/O-порты через `app.dependency_overrides`, оставляя
крипто и настройки реальными. БД-зависимые проверки (UNIQUE, enum, append-only
триггеры) — e2e в `tests/e2e` против реального Postgres: настоящие миграции,
обязательна выделенная БД с «e2e» в имени (иначе тесты скипаются).

**Конфигурация** — `app/config.py`: вложенные `BaseSettings` по группам
(`SecuritySettings`, `EsiaSettings`), читаются из env с префиксами
(`SECURITY_`, `ESIA_`). `get_settings()` закэширован.

## Конвенции модели данных

- PK — `uuid` (логи — `bigserial`); деньги — `amount_kopecks bigint` (никогда
  не float); время — `timestamptz`, источник времени — сервер.
- Перечисления — нативные Postgres enum со значениями в нижнем регистре
  (`values_callable` в ORM, явные типы в миграции).
- **Append-only и неизменяемость**: `audit_log`, `resolutions`,
  `ledger_*` правятся только новыми строками; у роли приложения нет
  UPDATE/DELETE на них. Это инвариант — не обходить.
- **Две кассы** (`OPERATIONS`/`PRIZE`) разделены на уровне схемы триггером;
  проводка целиком в одной кассе. Не вводить транзакции, пересекающие кассы.

## Текущее состояние

Реализованы все домены MVP (`app/modules/`): **identity** (ЕСИА OIDC, сессии
JWT access + ротируемый refresh, «один человек = один аккаунт» по
`UNIQUE(snils_hash)`), **events**, **predictions**, **scoring** (Brier,
рейтинги, калибровка), **seasons**, **resolutions** (разрешение + споры),
**billing** (две кассы, подписки, выплаты maker-checker), **notifications**
(+ реал-тайм через goctopus), **social**, **leagues**, **b2b** (API-ключи,
сигнал). Фон — `app/worker.py` (композит-рут воркера, единственное место,
которому можно знать несколько доменов). Демо-данные — `seed.py`. Интеграция
с реальной ЕСИА идёт через сертифицированный шлюз — точки стыка помечены
`TODO(identity-infra)`.
