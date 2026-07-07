"""План счетов (chart of accounts) — стабильные коды счетов.

Один источник правды для кодов счетов: на эти константы ссылаются и seed
в миграции, и прикладные use-cases при сборке проводок. Префикс кода
(``ops:`` / ``prize:``) дублирует ``ledger_type`` для читаемости — но
истинная принадлежность кассе определяется столбцом ``ledger_type`` счёта.
"""

from __future__ import annotations

import uuid

# ── Операционная касса ────────────────────────────────────────────────────
OPS_CASH_YOOKASSA = "ops:cash:yookassa"
OPS_CASH_TBANK = "ops:cash:tbank"
OPS_REVENUE_SUBSCRIPTIONS = "ops:revenue:subscriptions"
OPS_REVENUE_B2B = "ops:revenue:b2b"
OPS_FEE_PROVIDER = "ops:fee:provider"

# ── Призовая касса ────────────────────────────────────────────────────────
PRIZE_CASH_SPONSOR = "prize:cash:sponsor"
PRIZE_PAYABLE_WINNERS = "prize:payable:winners"
PRIZE_TAX_WITHHELD = "prize:tax:withheld"


def prize_fund_account_code(fund_id: uuid.UUID) -> str:
    """Код счёта-фонда призовой кассы для конкретного фонда."""
    return f"prize:fund:{fund_id}"
