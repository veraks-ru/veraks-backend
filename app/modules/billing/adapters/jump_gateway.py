"""Адаптер выплат Jump.Finance: smart-выплата по СБП и опрос статуса.

Реализует порты ``PayoutGateway`` и ``PayoutStatusProbe``. Вебхуков у Jump
нет — терминальный статус выплаты добирается опросом ``GET /payments/{id}``
(не чаще раза в минуту, до ``is_final``). Идемпотентность создания —
``customer_payment_id`` = uuid нашей выплаты: повтор с тем же id не создаёт
второй перевод. Формат — официальный OpenAPI-спек Jump (openapi-v1).
"""

from __future__ import annotations

import uuid

import httpx

from app.config import JumpSettings
from app.modules.billing.domain.errors import PaymentGatewayError
from app.modules.billing.domain.jump import rubles_str
from app.modules.billing.ports.gateways import (
    PayoutInstruction,
    PayoutRecipient,
    PayoutStatusView,
)

# Тип реквизита Jump «СБП по номеру телефона» (схема Requisites спека).
_SBP_REQUISITE_TYPE_ID = 10


class JumpGateway:
    """Реализует PayoutGateway и PayoutStatusProbe для Jump.Finance."""

    def __init__(self, settings: JumpSettings, client: httpx.AsyncClient) -> None:
        self._s = settings
        self._client = client

    async def _request(
        self, method: str, path: str, json: dict[str, object] | None = None
    ) -> dict[str, object]:
        """Выполнить запрос к OpenAPI Jump и разобрать ответ/ошибку."""
        try:
            resp = await self._client.request(
                method,
                f"{self._s.api_base_url}{path}",
                json=json,
                headers={
                    "Client-Key": self._s.api_key,
                    "Accept": "application/json",
                },
            )
            data: dict[str, object] = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentGatewayError(
                f"Jump {method} {path}: сетевая ошибка: {exc}"
            ) from exc
        if resp.is_error:
            error = data.get("error")
            detail = error if isinstance(error, dict) else {}
            raise PaymentGatewayError(
                f"Jump {method} {path}: HTTP {resp.status_code}: "
                f"{detail.get('title', '')} {detail.get('detail', '')}".strip()
            )
        return data

    @staticmethod
    def _item(data: dict[str, object], context: str) -> dict[str, object]:
        item = data.get("item")
        if not isinstance(item, dict) or "id" not in item:
            raise PaymentGatewayError(f"Jump {context}: неожиданный ответ: {data!r}")
        return item

    async def send_payout(
        self,
        *,
        payout_id: uuid.UUID,
        user_id: uuid.UUID,
        amount_kopecks: int,
        recipient: PayoutRecipient,
    ) -> PayoutInstruction:
        """POST /payments/smart: выплата по СБП с созданием исполнителя.

        Jump матчит исполнителя по телефону, отдельный маппинг
        user → contractor_id не нужен. Сумма — рубли: JSON-число формируется
        из точной строки :func:`rubles_str` (float-арифметики нет; shortest
        repr сериализует те же два знака).
        """
        # ФИО дублируется в sbp_validation_data — банк получателя сверяет имя.
        validation: dict[str, object] = {
            "last_name": recipient.last_name,
            "first_name": recipient.first_name,
        }
        body: dict[str, object] = {
            "customer_payment_id": str(payout_id),
            "phone": recipient.phone,
            "last_name": recipient.last_name,
            "first_name": recipient.first_name,
            "amount": float(rubles_str(amount_kopecks)),
            "requisite": {
                "type_id": _SBP_REQUISITE_TYPE_ID,
                "account_number": recipient.phone,
                "sbp_bank_id": int(recipient.sbp_bank_id),
            },
            "sbp_validation_data": validation,
        }
        if recipient.middle_name:
            body["middle_name"] = recipient.middle_name
            validation["middle_name"] = recipient.middle_name
        if self._s.agent_id is not None:
            body["agent_id"] = self._s.agent_id
        if self._s.bank_account_id is not None:
            body["bank_account_id"] = self._s.bank_account_id

        data = await self._request("POST", "/payments/smart", json=body)
        item = self._item(data, "payments/smart")
        return PayoutInstruction(
            provider="jump", provider_payout_id=str(item["id"])
        )

    async def get_payout_status(
        self, *, provider_payout_id: str
    ) -> PayoutStatusView:
        """GET /payments/{id}: код статуса и флаг финальности."""
        data = await self._request("GET", f"/payments/{provider_payout_id}")
        item = self._item(data, f"payments/{provider_payout_id}")
        status = item.get("status")
        status_id = status.get("id") if isinstance(status, dict) else None
        if not isinstance(status_id, int):
            raise PaymentGatewayError(
                f"Jump payments/{provider_payout_id}: нет кода статуса: {item!r}"
            )
        return PayoutStatusView(
            status_id=status_id, is_final=bool(item.get("is_final", False))
        )
