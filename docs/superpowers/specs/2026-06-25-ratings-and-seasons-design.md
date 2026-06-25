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
  `upcoming → active` (снапшотит `LeagueConfig` из дефолтов scoring на момент
  активации), `active → finished`. Любой иной переход →
  `InvalidSeasonTransitionError`. Повторный переход в текущий статус —
  идемпотентный no-op (см. §6).
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
  `append_finalization(record)` (запись в append-only таблицу финализаций).
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
  `SeasonFinalizationORM` — append-only таблица финализаций.
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
  Если конфиг сезона недоступен (сезон ещё не активирован) — сезонный scope
  пропускается с логом, не падая.
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
победителей и призов. Он пишет неизменяемую запись в append-only таблицу
`season_finalizations`:
- какой `LeagueConfig` применён (полный снапшот jsonb),
- когда (`finalized_at`, источник времени — сервер),
- финальный снапшот рейтингов сезона (ранжированные `qualified`-участники с
  метриками) в jsonb.

Таблица append-only по тому же принципу, что `resolutions`/`audit_log`: у роли
приложения нет `UPDATE`/`DELETE`. Это нужно, чтобы защитить результат перед
оспаривающим участником. Интеграция с глобальным `audit_log` (hash-цепочка) —
`TODO(audit)` (домен аудита ещё не построен); до него `season_finalizations`
самодостаточна.

### 6.4 Запрет финализации при открытых спорах
Сезон не финализируется, пока есть открытые споры по его событиям (финализация
на нефинальных исходах = расчёт призов по исходам, которые ещё могут
измениться). Домен resolutions/disputes ещё не существует, поэтому проверка
оформляется как порт `DisputeGuard.has_open_disputes(season_id) -> bool` с
заглушкой-адаптером, всегда возвращающей `False`, и помечается
`TODO(resolutions)`. `season_roll` и ручная финализация **уже сейчас**
спроектированы вокруг этой проверки: при `True` поднимается
`SeasonFinalizationBlockedError`, финализация не выполняется. Так доработка не
потребует переписывания.

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
  индексы по статусу), append-only таблица `season_finalizations`. **Не**
  трогает `events`.
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
- Реальная проверка открытых споров — `TODO(resolutions)` (порт уже заложен).
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
