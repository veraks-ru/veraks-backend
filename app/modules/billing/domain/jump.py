"""Доменные правила интеграции Jump.Finance (выплаты, касса PRIZE).

Jump не присылает вебхуки — статусы опрашиваются. Здесь чистый маппинг кодов
статусов Jump в исход выплаты и конвертация копеек в рубли для API Jump
(суммы у Jump — в рублях с дробной частью; float запрещён инвариантом).
"""

from __future__ import annotations

from decimal import Decimal

# Коды статусов выплат Jump (GET /payments/{id}).
JUMP_STATUS_PAID = 1
JUMP_STATUS_FAILED = frozenset({2, 5, 6})  # Отклонён / Ошибка выплаты / Удалено
# 3 «В обработке», 4 «Ожидает оплаты», 7 «Ожидает подтверждения» (штатный
# режим тестирования на бою), 8 «Ждёт подписания акта» — ждать.


def map_jump_status(status_id: int, *, is_final: bool) -> bool | None:
    """Исход выплаты по статусу Jump: True=оплачена, False=неуспех, None=ждать.

    Неизвестный, но финальный (``is_final``) код трактуем как неуспех —
    выплата не должна зависнуть в processing навсегда.
    """
    if status_id == JUMP_STATUS_PAID:
        return True
    if status_id in JUMP_STATUS_FAILED:
        return False
    if is_final:
        return False
    return None


def rubles_str(amount_kopecks: int) -> str:
    """Копейки → строка в рублях для API Jump: 123456 → ``"1234.56"``."""
    rub = Decimal(amount_kopecks) / Decimal(100)
    return str(rub.quantize(Decimal("0.01")))
