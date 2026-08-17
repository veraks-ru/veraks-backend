"""Заведение сезонов вперёд с заранее выбранными порогами.

Сезоны активируются по таймеру: воркер поднимает ``upcoming``, у которого
наступил ``starts_at``. Поэтому пороги задаются здесь, в
``planned_league_config``, — иначе каждый сезон заморозит боевые дефолты, и
переиграть их будет уже нельзя (ст. 1058 ГК).

Границы кварталов считаются в часовом поясе владельца платформы (UTC+10):
сезон начинается в 00:00 первого дня квартала по местному времени и
заканчивается в 23:59 последнего. Между концом одного и началом следующего —
минута зазора, чтобы не было двух активных сезонов одновременно: активным
считается последний по ``starts_at``, и пересечение делало бы «текущий сезон»
неоднозначным.

Идемпотентно: сезон с занятым slug пропускается.

Запуск::

    python scripts/seed_seasons.py --database-url-file <файл>
    python scripts/seed_seasons.py --database-url-file <файл> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.modules.identity.adapters.orm import UserORM
from app.modules.identity.domain.entities import UserRole
from app.modules.seasons.adapters.clock import SystemClock
from app.modules.seasons.adapters.season_repository import SqlAlchemySeasonRepository
from app.modules.seasons.application.use_cases import CreateSeason
from app.modules.seasons.domain.value_objects import LeagueConfig
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail

# Пороги первых сезонов платформы. Боевые дефолты (30/4/8.0, min_predictors=5)
# рассчитаны на живой пул участников: пока аудитории нет, событие не попадёт в
# зачёт, пока по нему не наберётся пять прогнозов. Три — минимум, при котором
# leave-one-out консенсус ещё осмыслен (считается по двум оставшимся голосам).
#
# Значения можно свободно править до активации — обычной правкой сезона.
LAUNCH = LeagueConfig(
    gradation_map=(0.1, 0.3, 0.5, 0.7, 0.9),
    n_min=12,
    c_min=3,
    w_min=2.0,
    m_per_category=1,
    k_shrink=3.0,
    min_predictors=3,
)

ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}

# Смещение часового пояса владельца: границы кварталов задаются по местному
# времени, а хранятся в UTC.
TZ_OFFSET_HOURS = 10


def quarter_bounds(year: int, quarter: int) -> tuple[datetime, datetime]:
    """Начало и конец квартала в UTC (00:00 и 23:59 по местному времени)."""
    first_month = 3 * (quarter - 1) + 1
    start_local = datetime(year, first_month, 1, tzinfo=UTC)
    if quarter == 4:
        end_local = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end_local = datetime(year, first_month + 3, 1, tzinfo=UTC)
    # Переводим «местные» отметки в UTC и отступаем минуту от полуночи.
    from datetime import timedelta

    shift = timedelta(hours=TZ_OFFSET_HOURS)
    return start_local - shift, end_local - shift - timedelta(minutes=1)


@asynccontextmanager
async def _session(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-file", required=True)
    parser.add_argument("--actor", default="andrey")
    parser.add_argument(
        "--not-before",
        help=(
            "ISO-8601 UTC: не начинать первый сезон раньше этой отметки. Нужно, "
            "чтобы новый сезон не пересёкся с уже активным"
        ),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    url = Path(args.database_url_file).read_text().strip()
    floor = (
        datetime.fromisoformat(args.not_before).replace(tzinfo=UTC)
        if args.not_before
        else None
    )

    planned = [(2026, 4), (2027, 1), (2027, 2), (2027, 3), (2027, 4)]

    async with _session(url) as session:
        repo = SqlAlchemySeasonRepository(session)
        actor = (
            await session.execute(
                select(UserORM).where(UserORM.username == args.actor)
            )
        ).scalar_one_or_none()
        if actor is None:
            print(f"ОТКАЗ: пользователь @{args.actor} не найден", file=sys.stderr)
            return 2

        create = CreateSeason(
            repo=repo, clock=SystemClock(), audit=SqlAlchemyAuditTrail(session)
        )

        print(f"\nПороги для всех: {LAUNCH.to_dict()}\n")
        created = 0
        for year, quarter in planned:
            slug = f"{year}-q{quarter}"
            starts, ends = quarter_bounds(year, quarter)
            if floor is not None and starts < floor:
                # Первый квартал списка может пересечься с идущим сезоном.
                starts = floor
            title = f"Сезон {year} · {ROMAN[quarter]} квартал"

            if await repo.get_by_slug(slug) is not None:
                print(f"  · {slug.ljust(8)} уже есть — пропуск")
                continue

            if not args.apply:
                print(
                    f"  + {slug.ljust(8)} {starts:%d.%m.%Y %H:%M} → "
                    f"{ends:%d.%m.%Y %H:%M} UTC   {title}"
                )
                continue

            season = await create.execute(
                slug=slug,
                title=title,
                starts_at=starts,
                ends_at=ends,
                actor_id=actor.id,
                actor_role=UserRole(actor.role),
                planned_league_config=LAUNCH,
            )
            created += 1
            print(
                f"  ✓ {season.slug.ljust(8)} {starts:%d.%m.%Y %H:%M} → "
                f"{ends:%d.%m.%Y %H:%M} UTC   {title}"
            )

        if not args.apply:
            print("\nСухой прогон — ничего не записано. Для записи добавьте --apply.")
        else:
            print(f"\nСоздано сезонов: {created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
