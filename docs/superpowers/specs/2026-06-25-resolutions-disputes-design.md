# Модуль «Разрешение событий и споры» (resolutions) — дизайн

Дата: 2026-06-25
Статус: на согласовании

## 1. Назначение и границы

Модуль `resolutions` отвечает за подведение исхода события, окно оспаривания,
арбитраж и **неизменяемый аудит**. Он:

- фиксирует исход (`resolutions` — append-only журнал решений);
- ведёт окно оспаривания и споры (`disputes` — изменяемый жизненный цикл);
- пересматривает решения (overturn = «аннулирование» прежнего решения новой
  строкой через `supersedes_id`);
- по истечении окна без открытых споров ставит фоновую задачу скоринга;
- даёт сезонам гарантию «нет открытых споров» (`DisputeGuard`);
- пишет tamper-evident `audit_log` (хеш-цепочка) — **общая инфраструктура**.

**Вне границ модуля** (намеренно, во избежание дублирования):

- Отмена события (`cancelled`) принадлежит `events` (`POST /events/{id}/cancel`).
  Конечный автомат событий не разрешает отмену уже разрешённого события — это
  инвариант `events`, не трогаем.
- Сам пер-прогнозный Brier и пересчёт рейтингов — домен `scoring`; мы лишь
  ставим задачу `score_event`.

## 2. Решения по интеграции (согласованы)

| Вопрос | Решение |
|---|---|
| Аудит-лог | Общая append-only инфраструктура `app/shared/audit/` (порт `AuditTrail` + адаптер с хеш-цепочкой). Вводится этим модулем, переиспользуется впредь. |
| Смена статуса события | Через порт `EventResolutionGateway` (адаптер поверх `EventRepository`); источник истины по статусу остаётся в `events`. |
| Триггер скоринга | Периодическая arq-задача `close_dispute_windows`: находит разрешённые события с истёкшим окном без открытых споров → ставит `score_event`. |
| `DisputeGuard` для seasons | Реализуем реальный guard (поверх `disputes`); `seasons_auto_finalize` остаётся `False` (решение эксплуатации). |

## 3. Конечные автоматы

### 3.1 Событие (владелец — `events`, мы только драйвим через gateway)

```
CLOSED ──proposeResolution──▶ RESOLVING ──finalize(в той же UC)──▶ RESOLVED
RESOLVED ──raiseDispute──▶ DISPUTED ──decideDispute(последний спор закрыт)──▶ RESOLVED
```

MVP: фиксация исхода — **одношаговая**. Use-case `FixResolution` проводит
событие `CLOSED → RESOLVING → RESOLVED` в одной транзакции (через gateway),
вставляет **одну** строку `resolutions(status=final)` и открывает окно. Статус
`proposed` оставлен в enum как зарезервированный под будущий двухшаговый
maker-checker; в MVP строки `proposed` не пишутся (документировано).

### 3.2 Решение (`resolutions`) — append-only

- Строка неизменяема (DB-триггер блокирует UPDATE/DELETE).
- «Текущее» решение = последняя строка `status=final` по `resolved_at`, на
  которую никто не ссылается через `supersedes_id`.
- Overturn (аннулирование): вставляется новая `final`-строка с
  `supersedes_id = <id текущего final>`; прежняя строка остаётся в журнале.
  Значение enum `overturned` сохранено для полноты схемы; «отменённость»
  восстанавливается по цепочке `supersedes_id` (без UPDATE).

### 3.3 Спор (`disputes`) — изменяемый

```
open ──takeUnderReview(опц.)──▶ under_review ──decide──▶ accepted | rejected
```

- `rejected`: если это был последний открытый спор события → `DISPUTED → RESOLVED`
  (исход не меняется; окно не сбрасывается — если оно уже истекло, worker
  заберёт событие на скоринг на ближайшем тике).
- `accepted` (overturn): арбитр передаёт новый исход → новая `final`-резолюция
  (supersedes), `event.outcome` обновляется, окно открывается заново
  (`dispute_window_ends_at = now + window`), `DISPUTED → RESOLVED` при отсутствии
  других открытых споров.
- Нельзя решать собственный спор: `decided_by != raised_by` (разделение
  обязанностей).

## 4. Структура файлов

```
app/shared/audit/
  __init__.py
  domain/__init__.py
  domain/entities.py        # AuditEntry, AuditActorType
  domain/hashing.py         # canonical(payload), chain_hash(prev, payload)
  ports/__init__.py
  ports/audit_trail.py      # AuditTrail (Protocol)
  adapters/__init__.py
  adapters/orm.py           # AuditLogORM (bigserial)
  adapters/trail.py         # SqlAlchemyAuditTrail (advisory-lock + хеш-цепочка)

app/modules/resolutions/
  __init__.py
  domain/
    __init__.py
    entities.py             # Resolution, Dispute, ResolutionStatus, DisputeStatus
    errors.py               # ResolutionError + подклассы
    policies.py             # RBAC + правила переходов споров
  ports/
    __init__.py
    repositories.py         # ResolutionRepository, DisputeRepository, ScoringDispatchRepository
    gateways.py             # EventResolutionGateway, ParticipationGateway
    tasks.py                # TaskScheduler (enqueue_score_event)
    clock.py                # Clock
  application/
    __init__.py
    dto.py                  # ResolutionView, DisputeView, FixOutcomeCommand, ...
    use_cases.py            # FixResolution, GetResolution, RaiseDispute,
                            #   DecideDispute, ListDisputes, CloseDisputeWindows
  adapters/
    __init__.py
    orm.py                  # ResolutionORM, DisputeORM, ScoringDispatchORM
    repositories.py         # SqlAlchemy* реализации
    event_gateway.py        # SqlAlchemyEventResolutionGateway (поверх EventRepository)
    participation_gateway.py# SqlAlchemyParticipationGateway (read predictions)
    dispute_guard.py        # ResolutionDisputeGuard (реализует seasons.DisputeGuard)
    clock.py                # SystemClock
  api/
    __init__.py
    schemas.py              # pydantic-схемы
    router.py               # /events/{id}/resolution, /events/{id}/disputes, /disputes/{id}/decision
    dependencies.py         # composition root

alembic/versions/
  0008_create_audit_log.py             # audit_log + enum actor_type + block_mutations() trigger
  0009_create_resolutions_disputes.py  # resolutions(+trigger), disputes, scoring_dispatches

tests/resolutions/
  fakes.py
  unit/test_use_cases.py
  unit/test_policies.py
  unit/test_audit_hashing.py
  integration/test_resolution_endpoints.py
  integration/test_dispute_endpoints.py
```

## 5. Порты (контракты)

```python
# resolutions/ports/gateways.py
class EventResolutionGateway(Protocol):
    async def get_lifecycle(self, event_id) -> EventLifecycle | None  # status, outcome, window, season_id
    async def mark_resolving(self, event_id, *, now) -> None
    async def mark_resolved(self, event_id, *, outcome, dispute_window_ends_at, now) -> None
    async def mark_disputed(self, event_id, *, now) -> None
    async def back_to_resolved(self, event_id, *, outcome, dispute_window_ends_at, now) -> None
    async def find_resolved_past_window(self, *, now) -> list[uuid.UUID]

class ParticipationGateway(Protocol):
    async def has_prediction(self, *, user_id, event_id) -> bool

# resolutions/ports/repositories.py
class ResolutionRepository(Protocol):
    async def add(self, resolution) -> Resolution            # INSERT-only
    async def current_final(self, event_id) -> Resolution | None
    async def list_for_event(self, event_id) -> list[Resolution]

class DisputeRepository(Protocol):
    async def add(self, dispute) -> Dispute
    async def get_by_id(self, dispute_id) -> Dispute | None
    async def update(self, dispute) -> Dispute               # статус мутируем
    async def list_for_event(self, event_id) -> list[Dispute]
    async def has_open_for_event(self, event_id) -> bool
    async def has_open_in_season(self, season_id) -> bool    # для DisputeGuard (join events)

class ScoringDispatchRepository(Protocol):
    async def exists(self, resolution_id) -> bool
    async def add(self, *, resolution_id, event_id, now) -> bool  # idempotent insert-once

# resolutions/ports/tasks.py
class TaskScheduler(Protocol):
    async def enqueue_score_event(self, event_id) -> None

# app/shared/audit/ports/audit_trail.py
class AuditTrail(Protocol):
    async def record(self, *, actor_id, actor_type, action,
                     entity_type, entity_id, before, after, metadata) -> AuditEntry
```

## 6. Use-cases

| Use-case | Роль | Действие |
|---|---|---|
| `FixResolution` | editor/arbiter | событие CLOSED→RESOLVING→RESOLVED, вставка `final`, окно, аудит |
| `GetResolution` | публично | текущее решение события |
| `RaiseDispute` | участник (есть прогноз) | RESOLVED→DISPUTED, спор `open`, аудит; только внутри окна |
| `DecideDispute` | arbiter (≠ raiser) | accept(overturn)/reject; переходы события; аудит |
| `ListDisputes` | публично | споры события |
| `CloseDisputeWindows` | worker | находит RESOLVED+окно истекло+нет открытых споров+не диспатчено → `score_event` |

## 7. Хранилище

- `resolutions`: id, event_id, outcome bool, status enum(proposed,final,overturned),
  resolved_by, source_reference, supersedes_id (self-FK NULL), notes, resolved_at.
  Индексы: btree(event_id), btree(event_id, status). **Append-only trigger.**
- `disputes`: id, event_id, resolution_id, raised_by, reason, evidence,
  status enum(open,under_review,accepted,rejected), decided_by NULL,
  decision_notes, created_at, decided_at NULL. Индексы: btree(event_id),
  btree(status), btree(raised_by), partial btree(event_id) WHERE status IN (open,under_review).
- `resolution_scoring_dispatches`: resolution_id PK, event_id, dispatched_at.
  Маркер «скоринг по этой резолюции поставлен» — ограничивает скан worker'а и
  даёт идемпотентность. INSERT-once (ON CONFLICT DO NOTHING).
- `audit_log`: bigserial id, occurred_at, actor_id NULL, actor_type enum,
  action text, entity_type text, entity_id uuid, before jsonb NULL, after jsonb
  NULL, metadata jsonb, prev_hash text NULL, hash text. **Append-only trigger.**

### Append-only на уровне схемы

Функция `block_mutations()` (RAISE EXCEPTION) + триггеры `BEFORE UPDATE OR DELETE`
на `resolutions` и `audit_log` — схемная гарантия неизменяемости (в духе
триггера раздельных касс из задания). Дополняет «у роли приложения нет
UPDATE/DELETE».

### Хеш-цепочка аудита

`hash = sha256(prev_hash ‖ canonical_json(payload))`. Адаптер сериализует
запись хеш-цепочки через `pg_advisory_xact_lock(<const>)`: берёт advisory-lock,
читает последний `hash`, считает новый, вставляет — так цепочка консистентна при
конкуренции.

## 8. Конфигурация

`ResolutionsSettings(env_prefix="RESOLUTIONS_")`: `dispute_window_hours: int = 72`.
Добавляется в корневой `Settings`. Окно прокидывается в `FixResolution`/
`DecideDispute` (overturn).

## 9. Воркер и main.py

- `app/worker.py`: новая задача `close_dispute_windows` + cron (каждые 5 мин),
  собирает UC из адаптеров через `session_scope`; в композите подменяется
  `AlwaysAllowsDisputeGuard` на `ResolutionDisputeGuard` в `season_roll`.
- `app/main.py`: регистрация `resolutions_router` и `@app.exception_handler(ResolutionError)`;
  пополнение `_ERROR_STATUS` (NotFound→404, Permission→403, переходы/окно→409,
  валидация→400).

## 10. Тестирование

- Unit: use-cases на фейках портов (in-memory repos/gateways/scheduler/audit),
  политики (RBAC, переходы), чистая хеш-цепочка аудита (детерминизм, связность).
- Integration: эндпоинты через `app.dependency_overrides` (фейковые I/O-порты,
  реальные настройки), сценарии: фиксация → спор → решение(reject/accept-overturn)
  → закрытие окна ставит `score_event`; запрет решать свой спор; запрет спора вне
  окна; гость не фиксирует исход.
- e2e БД-инварианты (триггеры append-only, UNIQUE) — помечаются TODO (как в
  остальном проекте).

## 11. TODO-точки интеграции

- `TODO(resolutions-scoring)`: `score_event` идемпотентен по событию; overturn
  переписывает оценки (ответственность `scoring`).
- `TODO(resolutions-predictions)`: `ParticipationGateway` читает `predictions`
  напрямую (read-only) — допустимо до выделения публичного порта у `predictions`.
- `TODO(audit-anchor)`: периодическая публикация последнего `hash` как внешнего
  якоря — вне MVP.
- `TODO(resolutions-infra)`: роль приложения должна иметь только INSERT на
  `audit_log`/`resolutions` (управление ролями БД — вне alembic).
```
