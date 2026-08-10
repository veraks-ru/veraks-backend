"""Правка замороженных правил активированного сезона.

Обычно это запрещено: ``UpdateSeason`` отказывает после активации, потому что
условия публичного конкурса не меняются по ходу (ст. 1058 ГК, PRD §7). Запрет
защищает участников, которые уже полагались на объявленные пороги.

Скрипт существует для единственной ситуации, когда защищать некого: сезон
активирован (в том числе автоматически воркером, если ``starts_at`` оказался в
прошлом), но по нему ещё нет ни одного прогноза и нет финализации. Тогда
конкурс фактически не начался, и исправить неудачно замороженные пороги —
честнее, чем оставить сезон, в котором к призам не может пройти никто.

Обе проверки жёсткие: при первом же прогнозе или финализации скрипт
отказывает. Факт правки пишется в append-only аудит — след остаётся.

Запуск::

    python scripts/repair_season_config.py --database-url-file <файл> --preset launch
    python scripts/repair_season_config.py --database-url-file <файл> --preset launch --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.modules.identity.adapters.orm import UserORM
from app.modules.seasons.adapters.orm import SeasonORM
from app.modules.seasons.domain.value_objects import LeagueConfig
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail
from app.shared.audit.domain.entities import AuditActorType

# Пороги под первый сезон платформы без аудитории.
#
# Боевые дефолты (30/4/8.0, min_predictors=5) рассчитаны на живой пул
# участников. На старте они дают сезон, в котором не квалифицируется никто:
# событие вообще не попадает в зачёт, пока по нему не набралось 5 прогнозов.
#
# ``min_predictors=3`` — компромисс: leave-one-out консенсус считается по двум
# оставшимся голосам. Это шумно, но работает; ниже трёх ставить нельзя, иначе
# бенчмарк вырождается в мнение одного человека.
PRESETS: dict[str, LeagueConfig] = {
    "launch": LeagueConfig(
        gradation_map=(0.1, 0.3, 0.5, 0.7, 0.9),
        n_min=12,  # ~27% от 44 заведённых событий
        c_min=3,  # из 10 категорий
        w_min=2.0,  # достижимо примерно на десятке неочевидных событий
        m_per_category=1,
        k_shrink=3.0,  # мягче к малой выборке, чем боевые 6.0
        min_predictors=3,
    ),
    "prod": LeagueConfig.default(),
}


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


def _show(label: str, cfg: dict[str, object] | None) -> None:
    if cfg is None:
        print(f"{label}: не задан")
        return
    print(f"{label}:")
    for key in (
        "n_min",
        "c_min",
        "w_min",
        "m_per_category",
        "k_shrink",
        "min_predictors",
    ):
        print(f"    {key.ljust(16)} {cfg[key]}")
    print(f"    {'gradation_map'.ljust(16)} {cfg['gradation_map']}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-file", required=True)
    parser.add_argument("--season-slug", default="2026-q3")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="launch")
    parser.add_argument(
        "--ends-at",
        help=(
            "новый конец сезона в ISO-8601 UTC (например 2026-10-03T12:00). "
            "Нужен, чтобы успеть зафиксировать исходы до авто-финализации"
        ),
    )
    parser.add_argument("--actor", default="andrey", help="от чьего имени правка")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    url = Path(args.database_url_file).read_text().strip()
    target = PRESETS[args.preset]

    async with _session(url) as session:
        season = (
            await session.execute(
                select(SeasonORM).where(SeasonORM.slug == args.season_slug)
            )
        ).scalar_one_or_none()
        if season is None:
            print(f"ОТКАЗ: сезон «{args.season_slug}» не найден", file=sys.stderr)
            return 2

        # ── Защита участников: правка допустима, только пока их нет ──────────
        predictions = (
            await session.execute(
                text(
                    "SELECT count(*) FROM predictions p "
                    "JOIN events e ON e.id = p.event_id "
                    "WHERE e.season_id = :sid"
                ),
                {"sid": str(season.id)},
            )
        ).scalar_one()
        finalizations = (
            await session.execute(
                text(
                    "SELECT count(*) FROM season_finalizations "
                    "WHERE season_id = :sid"
                ),
                {"sid": str(season.id)},
            )
        ).scalar_one()

        print(f"\nСезон: {season.title} ({season.slug}), статус {season.status}")
        print(f"Прогнозов в сезоне: {predictions}")
        print(f"Финализаций: {finalizations}")

        if predictions:
            print(
                "\nОТКАЗ: по сезону уже есть прогнозы. Условия объявленного "
                "конкурса менять нельзя — участники на них полагались.",
                file=sys.stderr,
            )
            return 3
        if finalizations:
            print(
                "\nОТКАЗ: сезон уже финализирован, результаты зафиксированы.",
                file=sys.stderr,
            )
            return 3

        print()
        _show("Сейчас", season.league_config)
        print()
        _show(f"Станет (пресет «{args.preset}»)", target.to_dict())

        new_ends = None
        if args.ends_at:
            new_ends = datetime.fromisoformat(args.ends_at).replace(tzinfo=UTC)
            print(
                f"\nКонец сезона: {season.ends_at:%d.%m %H:%M UTC}"
                f"  →  {new_ends:%d.%m %H:%M UTC}"
            )

        if not args.apply:
            print("\nСухой прогон — ничего не изменено. Для записи добавьте --apply.")
            return 0

        actor = (
            await session.execute(
                select(UserORM).where(UserORM.username == args.actor)
            )
        ).scalar_one_or_none()
        if actor is None:
            print(f"ОТКАЗ: пользователь @{args.actor} не найден", file=sys.stderr)
            return 2

        before = {
            "league_config": season.league_config,
            "ends_at": season.ends_at.isoformat(),
        }
        season.league_config = target.to_dict()
        if new_ends is not None:
            season.ends_at = new_ends
        season.updated_at = datetime.now(UTC)
        await session.flush()

        # Правка боевых условий обязана оставить след, даже если формально
        # разрешена отсутствием участников.
        await SqlAlchemyAuditTrail(session).record(
            actor_id=actor.id,
            actor_type=AuditActorType.ADMIN,
            action="season.rules_repaired",
            entity_type="season",
            entity_id=season.id,
            before=before,
            after={
                "league_config": season.league_config,
                "ends_at": season.ends_at.isoformat(),
            },
            metadata={
                "reason": "активирован автоматически с боевыми порогами; "
                "прогнозов нет, конкурс фактически не начался",
                "preset": args.preset,
            },
        )
        print("\nГотово. Правила сезона обновлены, запись в аудите оставлена.")
        print(json.dumps(season.league_config, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
