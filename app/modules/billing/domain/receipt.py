"""Сборка объекта Receipt для чека 54-ФЗ (ТБанк).

Формируется вместе с Init (чек прихода) и Cancel (чек возврата). Реально
фискализируется, когда к терминалу привязана онлайн-касса; на тестовом
терминале проходит тесты «Формирование чека». ИП на УСН «доходы» — без НДС
(``Tax="none"``), предмет расчёта — услуга (``PaymentObject="service"``).
"""

from __future__ import annotations


def build_receipt(
    *,
    description: str,
    amount_kopecks: int,
    taxation: str,
    email: str | None,
    phone: str | None,
) -> dict[str, object]:
    """Объект Receipt из одной позиции-услуги (подписка)."""
    receipt: dict[str, object] = {
        "Taxation": taxation,
        "Items": [
            {
                "Name": description[:128],
                "Price": amount_kopecks,
                "Quantity": 1,
                "Amount": amount_kopecks,
                "Tax": "none",
                "PaymentMethod": "full_payment",
                "PaymentObject": "service",
            }
        ],
    }
    if email:
        receipt["Email"] = email
    if phone:
        receipt["Phone"] = phone
    return receipt
