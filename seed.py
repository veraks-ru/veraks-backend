"""Сид демо-данных для локальной разработки.

Наполняет БД категориями, сезоном, событиями (открытые/разрешённые),
участниками и прогнозами, затем прогоняет скоринг и пересчёт рейтингов —
чтобы лидерборды, профили и калибровка были непустыми.

Запуск (из каталога backend, при поднятых Postgres/Redis):
    .venv/bin/python seed.py

Идемпотентность: перед наполнением чистит доменные таблицы.
НЕ для прода.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text

from app.config import get_settings
from app.db.session import session_scope
from app.modules.identity.adapters.orm import UserORM
from app.modules.identity.adapters.security import HmacSnilsHasher
from app.modules.identity.domain.entities import UserRole, UserStatus
from app.modules.identity.domain.value_objects import Snils
from app.modules.events.adapters.orm import CategoryORM, EventORM
from app.modules.events.domain.entities import EventStatus
from app.modules.predictions.adapters.orm import PredictionORM
from app.modules.predictions.domain.entities import ConfidenceGrade
from app.modules.seasons.adapters.orm import SeasonORM
from app.modules.seasons.domain.entities import SeasonStatus
from app.modules.resolutions.adapters.orm import ResolutionORM
from app.modules.resolutions.domain.entities import ResolutionStatus
from app.modules.scoring.adapters.orm import RatingORM
from app.modules.scoring.adapters.clock import SystemClock
from app.modules.scoring.adapters.rating_repository import SqlAlchemyRatingRepository
from app.modules.scoring.adapters.scoring_gateway import (
    SqlAlchemyEventScoringGateway,
    SqlAlchemyPredictionScoreWriter,
)
from app.modules.scoring.adapters.season_config_gateway import (
    SqlAlchemySeasonConfigGateway,
)
from app.modules.scoring.application.use_cases import RecomputeRatings, ScoreEvent

rng = random.Random(20260626)
now = datetime.now(timezone.utc)
DAY = timedelta(days=1)

GRADES = [
    ConfidenceGrade.DEFINITELY_NO,
    ConfidenceGrade.PROBABLY_NO,
    ConfidenceGrade.FIFTY_FIFTY,
    ConfidenceGrade.PROBABLY_YES,
    ConfidenceGrade.DEFINITELY_YES,
]
PROB = {0: "0.10", 1: "0.30", 2: "0.50", 3: "0.70", 4: "0.90"}

CATEGORIES = [
    ("politics", "Политика"),
    ("economy", "Экономика"),
    ("tech", "Технологии"),
    ("sport", "Спорт"),
    ("science", "Наука"),
    ("society", "Общество"),
]

# (username, display_name, skill 0..1, snils_number). Первые три совпадают с
# «гражданами» мок-ЕСИА → вход под ними попадает в этот аккаунт.
USERS = [
    ("kalibr", "Артём Калибров", 0.80, 1001501),
    ("mediana", "Мария Медиана", 0.86, 1001502),
    ("baseline", "Борис Базлайнов", 0.78, 1001503),
    ("statistik", "Статистик", 0.74, 1001101),
    ("vera_d", "Вера Д.", 0.70, 1001102),
    ("prognoz", "Прогноз", 0.66, 1001103),
    ("panteley", "Пантелей", 0.62, 1001104),
    ("kassandra", "Кассандра", 0.58, 1001105),
    ("marina_p", "Марина П.", 0.52, 1001106),
    ("pari_net", "Пари-нет", 0.42, 1001107),
]

# Разрешённые события: (category, title, outcome).
RESOLVED = [
    ("economy", "Ключевая ставка ЦБ была снижена на заседании", False),
    ("science", "Результат по термоядерному синтезу повторили независимо", False),
    ("society", "Новая линия метро открыта в этом полугодии", True),
    ("sport", "Сборная вышла в четвертьфинал турнира", True),
]

# Открытые события: (category, title).
OPEN = [
    ("economy", "Ключевая ставка ЦБ будет снижена на ближайшем заседании"),
    ("tech", "Starship выполнит полный орбитальный полёт до конца квартала"),
    ("economy", "Годовая инфляция опустится ниже 5% по итогам месяца"),
    ("tech", "Открытая модель обгонит закрытого лидера в публичном бенчмарке"),
    ("politics", "По итогам саммита будет принято совместное заявление"),
    ("sport", "Действующий чемпион защитит титул в этом сезоне"),
]


def grade_for(outcome: bool, skill: float) -> int:
    """Градация для разрешённого события: навык → чаще на правильной стороне."""
    if rng.random() < 0.1:
        return 2  # иногда честное «50 на 50»
    correct = rng.random() < (0.5 + skill * 0.45)
    strong = rng.random() < skill
    if outcome:
        return (4 if strong else 3) if correct else (0 if strong else 1)
    return (0 if strong else 1) if correct else (4 if strong else 3)


async def reset(session) -> None:
    # TRUNCATE (а не DELETE): resolutions/ledger/audit — append-only, на DELETE
    # стоит блокирующий триггер. TRUNCATE его не задевает; CASCADE подчищает
    # зависимые таблицы (подписки/платежи/выплаты/диспуты и т.п.).
    await session.execute(
        text(
            "TRUNCATE TABLE users, categories, seasons, events, predictions, "
            "resolutions, disputes, ratings RESTART IDENTITY CASCADE"
        )
    )


async def build() -> tuple[list[uuid.UUID], uuid.UUID]:
    settings = get_settings()
    hasher = HmacSnilsHasher(settings.security.snils_hmac_key)
    resolved_ids: list[uuid.UUID] = []

    async with session_scope() as session:
        await reset(session)

        # ── Пользователи ──
        users: list[UserORM] = []
        editor_id: uuid.UUID | None = None
        for i, (username, display, _skill, snils_num) in enumerate(USERS):
            digits = f"{snils_num:09d}00"
            u = UserORM(
                id=uuid.uuid4(),
                esia_oid=f"oid-{username}",
                snils_hash=hasher.hash(Snils.parse(digits)),
                username=username,
                display_name=display,
                real_name_enc=None,
                role=UserRole.EDITOR if i == 0 else UserRole.USER,
                status=UserStatus.ACTIVE,
                created_at=now - 220 * DAY,
            )
            users.append(u)
            session.add(u)
        editor_id = users[0].id
        skills = {users[i].id: USERS[i][2] for i in range(len(USERS))}
        await session.flush()  # пользователи на месте до вставки событий (FK)

        # ── Категории ──
        cats: dict[str, uuid.UUID] = {}
        for slug, title in CATEGORIES:
            c = CategoryORM(id=uuid.uuid4(), slug=slug, title=title, description="", parent_id=None)
            cats[slug] = c.id
            session.add(c)
        await session.flush()

        # ── Сезон (для UI; события к нему не привязываем) ──
        season = SeasonORM(
            id=uuid.uuid4(),
            slug="2026-q2",
            title="Сезон 2026 · II квартал",
            starts_at=now - 30 * DAY,
            ends_at=now + 60 * DAY,
            status=SeasonStatus.ACTIVE,
            league_config=None,
            created_at=now - 30 * DAY,
            updated_at=now,
        )
        session.add(season)

        # ── Разрешённые события + прогнозы (locked) ──
        for slug, title, outcome in RESOLVED:
            ev = EventORM(
                id=uuid.uuid4(),
                title=title,
                description="Демо-событие для локальной разработки.",
                category_id=cats[slug],
                created_by=editor_id,
                season_id=None,
                status=EventStatus.RESOLVED,
                opens_at=now - 40 * DAY,
                closes_at=now - 12 * DAY,
                resolves_at=now - 6 * DAY,
                resolution_source="Официальный источник (демо)",
                resolution_criteria="Засчитывается ДА при подтверждении по источнику.",
                outcome=outcome,
                resolved_at=now - 6 * DAY,
                dispute_window_ends_at=now - 3 * DAY,  # окно закрыто → скоринг разрешён
                created_at=now - 40 * DAY,
                updated_at=now - 6 * DAY,
            )
            session.add(ev)
            await session.flush()
            resolved_ids.append(ev.id)
            session.add(
                ResolutionORM(
                    id=uuid.uuid4(),
                    event_id=ev.id,
                    outcome=outcome,
                    status=ResolutionStatus.FINAL,
                    resolved_by=editor_id,
                    source_reference="https://example.org/proof",
                    supersedes_id=None,
                    notes="",
                    resolved_at=now - 6 * DAY,
                )
            )
            for u in users:  # все участники → ≥ MIN_PREDICTORS
                gi = grade_for(outcome, skills[u.id])
                session.add(
                    PredictionORM(
                        id=uuid.uuid4(),
                        user_id=u.id,
                        event_id=ev.id,
                        confidence_grade=GRADES[gi],
                        probability=Decimal(PROB[gi]),
                        is_locked=True,
                        brier_score=None,
                        scored_at=None,
                        created_at=now - 30 * DAY,
                        updated_at=now - 12 * DAY,
                    )
                )

        # ── Открытые события + прогнозы (unlocked) ──
        for idx, (slug, title) in enumerate(OPEN):
            closes = now + timedelta(hours=8) if idx == 0 else now + (idx + 1) * DAY
            ev = EventORM(
                id=uuid.uuid4(),
                title=title,
                description="Демо-событие для локальной разработки.",
                category_id=cats[slug],
                created_by=editor_id,
                season_id=None,
                status=EventStatus.OPEN,
                opens_at=now - 5 * DAY,
                closes_at=closes,
                resolves_at=closes + 5 * DAY,
                resolution_source="Официальный источник (демо)",
                resolution_criteria="Засчитывается ДА при подтверждении по источнику.",
                outcome=None,
                resolved_at=None,
                dispute_window_ends_at=None,
                created_at=now - 5 * DAY,
                updated_at=now - 5 * DAY,
            )
            session.add(ev)
            await session.flush()
            # часть участников делает прогноз (включая kalibr на части событий)
            voters = [u for u in users if rng.random() < 0.75]
            if users[0] not in voters and idx % 2 == 0:
                voters.append(users[0])  # kalibr прогнозирует на части открытых
            for u in voters:
                gi = rng.choice([0, 1, 1, 2, 2, 3, 3, 3, 4])
                session.add(
                    PredictionORM(
                        id=uuid.uuid4(),
                        user_id=u.id,
                        event_id=ev.id,
                        confidence_grade=GRADES[gi],
                        probability=Decimal(PROB[gi]),
                        is_locked=False,
                        brier_score=None,
                        scored_at=None,
                        created_at=now - 2 * DAY,
                        updated_at=now - 1 * DAY,
                    )
                )

    print(f"✓ Засеяно: {len(USERS)} участников, {len(CATEGORIES)} категорий, "
          f"{len(RESOLVED)} разрешённых, {len(OPEN)} открытых событий")
    return resolved_ids, season.id


async def score(resolved_ids: list[uuid.UUID]) -> None:
    for eid in resolved_ids:
        async with session_scope() as session:
            clock = SystemClock()
            uc = ScoreEvent(
                gateway=SqlAlchemyEventScoringGateway(session, clock),
                writer=SqlAlchemyPredictionScoreWriter(session),
                clock=clock,
            )
            scored = await uc.execute(event_id=eid)
        print(f"  · score_event {eid}: {scored} прогнозов оценено")

    async with session_scope() as session:
        clock = SystemClock()
        uc = RecomputeRatings(
            gateway=SqlAlchemyEventScoringGateway(session, clock),
            ratings=SqlAlchemyRatingRepository(session),
            clock=clock,
            season_config=SqlAlchemySeasonConfigGateway(session),
        )
        upserted = await uc.execute(season_id=None)
    print(f"✓ Рейтинги пересчитаны: {upserted} строк")


async def main() -> None:
    resolved_ids, _season_id = await build()
    await score(resolved_ids)
    async with session_scope() as session:
        n = len((await session.execute(select(RatingORM.id))).all())
    print(f"✓ Готово. Строк рейтинга в БД: {n}")


if __name__ == "__main__":
    asyncio.run(main())
