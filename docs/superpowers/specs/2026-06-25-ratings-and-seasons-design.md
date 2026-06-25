# Дизайн: модуль «Рейтинг и сезоны»

> Дата: 2026-06-25 · Статус: утверждён к реализации
> Компаньон к PRD и «Система скоринга платформы прогнозов».

## 1. Контекст и цель

Реализовать сезонный зачёт поверх уже существующего скоринга: домен **сезонов**
(жизненный цикл, замороженная конфигурация лиги), **порог квалификации** к
призам, **сезонные лидерборды** и надёжный **фоновый пересчёт** (ARQ).

Ключевой факт, определяющий объём работ: домен `scoring` уже реализует
бо́льшую часть «рейтинговой» половины — таблицу `ratings`, сущность `Rating` и
репозиторий, эндпоинты лидербордов, формулы (LOO-консенсус, вес события,
усаженный сезонный рейтинг `season_rating_from_contributions`), калибровку,
межсезонную рекалибровку и use-case `RecomputeRatings`. Константы порогов
(`N_MIN`, `C_MIN`, `W_MIN`, `K_SHRINK`, `MIN_PREDICTORS`) уже определены в
`scoring/domain/constants.py`.

Чего **нет** и что мы добавляем:

- домена **seasons** (таблицы `seasons` нет; `events.season_id` — «голый» UUID
  с `TODO(seasons)`);
- **логики квалификации** (пороги есть как константы, но `qualifies()` и
  гейтинга в `RecomputeRatings` нет);
- любой **фоновой инфраструктуры** (ARQ/Celery отсутствуют; `RecomputeRatings`
  и `ScoreEvent` доступны только ручными admin-эндпоинтами с `TODO`);
- резолва сезона по **slug** (эндпоинт сезонного лидерборда содержит `TODO`).

## 2. Границы модуля и направление зависимостей

Три области кода: новый домен + расширение существующего + общая фон-инфра.

- **Новый домен `app/modules/seasons/`** — жизненный цикл сезона, замороженный
  снапшот конфигурации лиги, **чистая** политика квалификации, финализация с
  неизменяемой записью.
- **Расширение `app/modules/scoring/`** — учёт сезона в пересчёте (вычисление
  флага `qualified`), сезонные лидерборды с резолвом slug, чтение деталей
  квалификации.
- **Новый `app/worker.py` (ARQ)** — фоновый конвейер и планировщик.

**Правило зависимостей: `scoring → seasons`, и никогда наоборот.** Цикла нет.
Это соответствует существующей конвенции, где собственные адаптеры scoring
читают таблицы других доменов напрямую (`SqlAlchemyEventScoringGateway` уже
читает `events`/`predictions`). Поэтому scoring определяет порт
`SeasonConfigGateway`, а его адаптер на стороне scoring читает таблицу
`seasons`. Из `seasons.domain` scoring импортирует только **чистую** функцию
квалификации и value-object `LeagueConfig` (кросс-доменный импорт чистого кода
уже есть в прецеденте — scoring импортирует `identity.UserRole`). Seasons
**никогда** не импортирует scoring.

Следствие для (a): лидерборды остаются в `scoring` (расширяются), не
переезжают в seasons — перенос заставил бы seasons зависеть от рейтингов и
сломал бы ацикличность.

## 3. Домен seasons (`app/modules/seasons/`)

Полный гексагональный срез, зеркалящий scoring.

### domain/
- `entities.py` — `Season` (dataclass, slots) + `SeasonStatus(str, Enum)`:
  `upcoming | active | finished`.
- `value_objects.py` — `LeagueConfig` (`frozen`, `slots`): замороженный снапшот
  правил сезона —
  `gradation_map: tuple[float, ...]` (5 вероятностей, монотонно возрастают),
  `n_min: int`, `c_min: int`, `w_min: float`, `m_per_category: int`,
  `k_shrink: float`, `min_predictors: int`. Сериализуется в/из `jsonb`
  методами `to_dict`/`from_dict` с валидацией (монотонность сетки,
  положительность порогов).
- `lifecycle.py` — чистые правила переходов:
  `upcoming → active` (снапшотит переданный `LeagueConfig` на момент
  активации), `active → finished`. Любой иной переход →
  `InvalidSeasonTransitionError`. Повторный переход в текущий статус —
  идемпотентный no-op (см. §6).
  **Источник дефолтов конфига (ацикличность).** `LeagueConfig` для активации
  **передаётся в** `TransitionSeason.execute(...)` как входной параметр —
  снапшот формирует вызывающий admin-слой, который знает оба домена (например,
  composition root, импортирующий дефолты из `scoring.domain.constants`).
  Домен и use-case'ы seasons **не импортируют scoring** ни прямо, ни
  транзитивно — иначе ломается направление `scoring → seasons`. В seasons
  допустим лишь внутренний дефолт `LeagueConfig` (нейтральная сетка
  `0.1/0.3/0.5/0.7/0.9` и пороги по умолчанию) как fallback, не зависящий от
  scoring.
- `qualification.py` — чистая
  `evaluate_qualification(n_resolved, category_count, total_weight, cfg) ->
  QualificationResult`. `QualificationResult` (`frozen`) несёт `qualified: bool`
  и побитовый разбор: `volume_ok`, `diversity_ok`, `coverage_ok` + наблюдённые
  значения и пороги (для «почему не квалифицирован»).
- `policies.py` — RBAC: `ensure_can_manage_seasons(role)` (editor/admin —
  создание/правка), `ensure_can_transition(role)` (только admin —
  активация/финализация).
- `errors.py` — `SeasonError` (база) + `SeasonNotFoundError`,
  `SeasonSlugTakenError`, `InvalidSeasonTransitionError`,
  `SeasonPermissionError`, `InvalidSeasonDataError`,
  `SeasonFinalizationBlockedError` (открытые споры, см. §6).

### ports/
- `repositories.py` — `SeasonRepository` (`Protocol`):
  `add`, `get_by_id`, `get_by_slug`, `list(status=None)`, `update`,
  `append_finalization(record, entries)` (родитель + строки-на-участника в
  append-only таблицы, одной транзакцией).
- `gateways.py` — `DisputeGuard` (`Protocol`):
  `has_open_disputes(season_id) -> bool` (см. §6.4, заглушка fail-loud).
- `clock.py` — `Clock` (как в scoring).

### application/
- `dto.py` — входные/выходные DTO (`SeasonView`, `SeasonFinalizationView`).
- `use_cases.py` — по классу на операцию, DI через конструктор:
  `CreateSeason`, `UpdateSeason` (только в `upcoming`),
  `TransitionSeason` (activate/finalize; финализация принимает результат
  пересчёта и пишет неизменяемую запись — см. §6), `ListSeasons`, `GetSeason`.

### adapters/
- `orm.py` — `SeasonORM` (+ `to_domain`/`from_domain`); `slug` — `citext`,
  `UNIQUE(slug)`; `league_config` — `jsonb` (NULL до активации);
  `SeasonFinalizationORM` + `SeasonFinalizationEntryORM` — append-only таблицы
  финализаций (родитель + строка-на-участника, §6.3).
- `dispute_guard.py` — `AlwaysAllowsDisputeGuard` (заглушка fail-loud, §6.4).
- `season_repository.py` — `SqlAlchemySeasonRepository`.
- `clock.py` — `SystemClock`.

### api/
- `router.py`:
  - `GET /seasons` (фильтр по статусу), `GET /seasons/{slug}` — публичные;
  - `POST /admin/seasons` (создать, `upcoming`),
    `PATCH /admin/seasons/{id}` (правка `upcoming`),
    `POST /admin/seasons/{id}/transition` — **ручной** перевод
    activate/finalize (RBAC admin; см. §6 — ручной триггер обязателен).
- `schemas.py` — pydantic-схемы; `from_domain`/`from_*`-классметоды.
- `dependencies.py` — composition root домена.

## 4. Интеграция в scoring

- **Схема**: добавить `qualified BOOLEAN NULL` в `ratings` (NULL для
  global/category, bool для season) — миграция `0006`. Расширить сущность
  `Rating` и ORM-маппинг (`to_domain`/`from_domain`).
- **Порт**: новый `SeasonConfigGateway` в `scoring.ports`:
  `resolve_slug(slug) -> uuid|None`, `get_config(season_id) -> LeagueConfig|None`.
  Адаптер на стороне scoring (`SqlAlchemySeasonConfigGateway`) читает таблицу
  `seasons` напрямую (та же БД).
- **`RecomputeRatings`**: для season-scope аккумулятор
  `(season_id, user_id)` дополнительно собирает **множество категорий** и
  **Σw**. После агрегации use-case подгружает `LeagueConfig` сезона через
  `SeasonConfigGateway` и вызывает чистую `evaluate_qualification(...)` из
  `seasons.domain`, проставляя `qualified` в строку сезонного рейтинга.
  Global/category-scope не меняются (`qualified=None`). Полностью идемпотентен.

  **Два разных случая «конфиг недоступен» (не путать).** Решение о квалификации
  зависит от статуса сезона, который `SeasonConfigGateway` отдаёт вместе с
  конфигом:
  - сезон отсутствует или в статусе `upcoming` (ещё не активирован, конфига
    нет по определению) — **нормальный** пропуск сезонного scope с info-логом;
  - сезон в статусе `active`, но конфиг загрузить не удалось — это **баг
    инварианта**, а не норма: активный сезон, не умеющий считать квалификацию,
    молча перестаёт обновлять рейтинги до жалобы пользователя. Поэтому —
    **error-лог + алерт**, не тихий пропуск (стыкуется с автоматической
    проверкой инвариантов из анти-фрод части). Конкретная реакция: поднять
    доменную ошибку/залогировать на уровне error и не «проглотить».
- **Сезонный лидерборд**: `GET /leaderboards/seasons/{slug}` резолвит slug→id
  через `SeasonConfigGateway` и получает query-параметр `qualified_only: bool`
  (по умолчанию `false`).
- **Новое чтение**: `GET /users/{user_id}/seasons/{slug}/qualification` →
  разбор квалификации (какие пороги пройдены/нет) для UX профиля. Живёт в
  scoring (владеет данными рейтинга и сезонной статистикой пользователя).

## 5. Фоновый воркер (ARQ)

- Зависимость `arq`; `app/worker.py` с `WorkerSettings`; в `app/db/session.py`
  — фабрика сессий вне request-scope (`worker_session`/контекст-менеджер
  `session_scope`) с явным `commit`/`rollback`.
- **Задачи**: `score_event(event_id)` (пер-прогнозный Brier при разрешении),
  `recompute_ratings(season_id=None)`, `season_roll()`.
- **Cron**: ночной полный `recompute_ratings`; периодический `season_roll`,
  который активирует `upcoming`-сезоны после `starts_at` и финализирует
  `active`-сезоны после `ends_at`.
- Домен остаётся свободным от arq: тонкий порт `TaskScheduler` + arq-адаптер
  для постановки `score_event`. Реальный триггер из домена resolutions помечен
  `TODO(scoring-infra)` (домен ещё не построен). Существующие admin-эндпоинты
  остаются ручными триггерами.

## 6. Надёжность воркера (обязательный раздел)

ARQ выбран, чтобы «сделать правильно», поэтому воркер даёт гарантии, а не
просто набор задач.

### 6.1 Идемпотентность и защита от двойного запуска
`season_roll` и `recompute_ratings` безопасны при ретраях и наложении cron.
- Переход `active → finished` **атомарен** и идемпотентен: финализация уже
  `finished`-сезона — **no-op**, а не повторный пересчёт с возможным иным
  результатом. Реализация: финализация выполняется в одной транзакции с
  `SELECT ... FOR UPDATE` строки сезона; внутри транзакции проверяется текущий
  статус — если уже `finished`, выходим без действий. Это защищает от двух
  параллельных `season_roll`.
- `score_event` уже идемпотентен по событию (Brier пишется один раз); поведение
  сохраняется.

### 6.2 Атомарный пересчёт на сезон
Если воркер падает посреди пересчёта, состояние рейтингов сезона — целиком
старое или целиком новое, не «наполовину». Пер-сезонный пересчёт оборачивается
в одну транзакцию: вычисление в памяти (чистые формулы) → один батч
`upsert_many` → `commit`. Падение до commit → rollback, старое состояние цело.

### 6.3 Неизменяемая запись при финализации
Переход `active → finished` (с финальным пересчётом) — момент определения
победителей и призов. Он пишет неизменяемую запись.

**Форма хранения снапшота (решено: строка-на-участника, не один jsonb-блоб).**
Полный ранжированный снапшот всех квалифицированных участников в одной jsonb-
ячейке тяжелеет при десятках тысяч участников. Поэтому финализация пишет в
**две** append-only таблицы:
- `season_finalizations` (родитель, одна строка на финализацию): `id`,
  `season_id`, `finalized_at` (источник времени — сервер),
  `league_config` (применённый снапшот, jsonb — он мал и фиксирован),
  `qualified_count`, `total_participants`;
- `season_finalization_entries` (одна строка на квалифицированного участника):
  `finalization_id` FK, `user_id`, `rank`, `skill_score`, `mean_brier`,
  `calibration_error`, `n_resolved`. Индекс `(finalization_id, rank)`.

Обе таблицы append-only по тому же принципу, что `resolutions`/`audit_log`: у
роли приложения нет `UPDATE`/`DELETE`. Это защищает результат перед
оспаривающим участником и не упирается в размер одной ячейки. Запись
родителя и всех entries — в той же транзакции, что финальный пересчёт и флаг
`finished` (§6.2). Интеграция с глобальным `audit_log` (hash-цепочка) —
`TODO(audit)`; до него таблицы самодостаточны.

### 6.4 Запрет финализации при открытых спорах
Сезон не финализируется, пока есть открытые споры по его событиям (финализация
на нефинальных исходах = расчёт призов по исходам, которые ещё могут
измениться). Домен resolutions/disputes ещё не существует, поэтому проверка
оформляется как порт `DisputeGuard.has_open_disputes(season_id) -> bool`.
`season_roll` и ручная финализация **уже сейчас** спроектированы вокруг неё:
при `True` поднимается `SeasonFinalizationBlockedError`, финализация не
выполняется.

**Заглушка fail-loud, не fail-silent.** Заглушка, молча возвращающая `False`,
означает, что спроектированная защита от споров фактически выключена — и
невидимо. Поэтому адаптер-заглушка называется явно — `AlwaysAllowsDisputeGuard`
— и **пишет warning-лог при каждом вызове** («dispute guard is a no-op stub —
real resolutions check not wired»). В `TODO(resolutions)` явно зафиксировано:
**автоматическую таймерную финализацию (`season_roll`) запрещено включать в
проде с реальными призовыми деньгами, пока эта заглушка не заменена реальной
проверкой споров.** Иначе через три месяца кто-то включит авто-финализацию,
забыв, что guard — no-op, и сезон закроется поверх открытых споров. Защита
обязана падать громко.

### 6.5 Ручной admin-override переходов
Ручные admin-триггеры для **activate и finalize** сохраняются рядом с
автоматическим таймерным `season_roll` (не только для recompute). Это позволяет
придержать финализацию, если события сезона ещё оспариваются, либо запустить её
вручную после разрешения споров.

## 7. Обработка ошибок, тесты, миграции

### Ошибки
`SeasonError` регистрируется в `app/main.py` отдельным `@app.exception_handler`
по образцу других доменов. Маппинг статусов: `SeasonNotFoundError` → 404;
`SeasonSlugTakenError` → 409; `InvalidSeasonTransitionError` → 409;
`SeasonFinalizationBlockedError` → 409; `SeasonPermissionError` → 403;
`InvalidSeasonDataError` → 400.

### Тесты
- **Юнит**: переходы жизненного цикла (валидные/невалидные, идемпотентный
  no-op); политика квалификации по комбинациям порогов (объём/разнообразие/
  охват, граничные значения); снапшот и валидация `LeagueConfig`
  (монотонность сетки); расчёт флага `qualified` в `RecomputeRatings`;
  атомарность/идемпотентность финализации (на фейках). In-memory фейки в
  `tests/seasons/fakes.py`.
- **Интеграция**: эндпоинты seasons (CRUD, переходы, RBAC) и сезонный
  лидерборд с `qualified_only` через `app.dependency_overrides`. БД-зависимые
  проверки (citext `UNIQUE(slug)`, `jsonb`, append-only грант) — отдельным e2e
  (`TODO`), как в существующей конвенции.
- Воркер тестируется через прямой вызов задач с фейковой фабрикой сессий и
  фейковыми use-case'ами; HTTP-обёртки тонкие.

### Миграции
- **`0005_create_seasons`** — *только* домен seasons: enum `season_status`,
  таблица `seasons` (`slug` citext UNIQUE, `league_config` jsonb NULL,
  индексы по статусу), append-only таблицы `season_finalizations` и
  `season_finalization_entries` (индекс `(finalization_id, rank)`). **Не**
  трогает `events`. (Append-only грант для роли приложения — отдельным
  инфра-шагом/`TODO`, как у `resolutions`/`ledger`.)
- **`0006_add_ratings_qualified`** — `ALTER TABLE ratings ADD COLUMN qualified
  boolean NULL`.
- **`0007_link_events_season_fk`** (отдельная, позже) — добавляет отложенный
  FK `events.season_id → seasons.id`. Выносится отдельно сознательно: до сих
  пор сезонов не было, поэтому существующие `events.season_id` могут быть
  «голыми» UUID, не указывающими ни на один реальный сезон. Перед навешиванием
  FK миграция **верифицирует/бэкфиллит** значения (orphan → `NULL`), иначе
  навешивание FK упадёт целиком и потянет за собой создание сезонов.
  Кросс-доменное изменение FK не смешивается с миграцией создания таблицы.

## 8. Сознательно вне объёма (помечено TODO)
- Привязка призового фонда/выплат (`/seasons/{slug}/prize-fund`) —
  `TODO(prize)`.
- Триггер `resolutions → score_event` (постановка задачи при финализации
  разрешения) — `TODO(scoring-infra)`.
- Реальная проверка открытых споров — `TODO(resolutions)` (порт заложен,
  заглушка `AlwaysAllowsDisputeGuard` fail-loud; авто-финализацию запрещено
  включать в проде до замены заглушки — см. §6.4).
- Интеграция финализации с глобальным `audit_log` (hash-цепочка) —
  `TODO(audit)`.
- Горячие топы в Redis sorted set — `TODO`.
- Энтропийная мера разнообразия категорий (пост-MVP альтернатива `C_MIN`).

## 9. Сводка ключевых решений
| Решение | Выбор | Почему |
|---|---|---|
| Структура | новый домен seasons + расширение scoring | переиспользуем математику scoring, не дублируем |
| Зависимости | `scoring → seasons`, ацикл | seasons не знает о рейтингах |
| Конфиг сезона | замороженный `LeagueConfig` снапшот при активации | правила публикуются заранее, нет ретро-изменений |
| Квалификация | флаг `qualified` на строке сезонного рейтинга + разбор | лидерборд фильтрует, будущий prize читает флаг |
| Лидерборды | остаются в scoring (расширены) | перенос сломал бы ацикличность |
| Фон | реальный ARQ + cron + гарантии надёжности (§6) | финализацию нужно уметь защитить |
| FK events.season_id | отдельная поздняя миграция с бэкфиллом | защита от падения на orphan-UUID |
