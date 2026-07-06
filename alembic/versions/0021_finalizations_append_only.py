"""Append-only триггеры на журнал финализаций сезонов (M6).

``season_finalizations`` и ``season_finalization_entries`` объявлены неизменяемым
журналом (как ``resolutions``/``ledger_*``), но в 0005 остались без защиты: их
можно было переписать SQL'ом. Здесь вешаем тот же ``block_mutations()`` (из 0008)
BEFORE UPDATE/DELETE — правки только новыми строками.
"""

from __future__ import annotations

from alembic import op

revision: str = "0021_finalizations_append_only"
down_revision: str | None = "0020_resolution_race_guards"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = ("season_finalizations", "season_finalization_entries")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION block_mutations();"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
