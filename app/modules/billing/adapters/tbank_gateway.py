"""Адаптер эквайринга ТБанк: Init (создание платежа) и Cancel (возврат).

Hosted-форма банка (nonPCI): backend вызывает Init → получает PaymentURL → фронт
редиректит пользователя на неё, форму отрисовывает банк (карточные данные к нам
не попадают, PCI DSS не нужен). Приём оплаты подтверждается вебхуком (см.
api/router.py). Подпись Token — domain/tbank_signing.py.
"""

from __future__ import annotations

import uuid

import httpx

from app.config import TBankSettings
from app.modules.billing.domain.errors import PaymentGatewayError
from app.modules.billing.domain.receipt import build_receipt
from app.modules.billing.domain.tbank_signing import make_token
from app.modules.billing.ports.gateways import CheckoutIntent, RefundResult


class TBankGateway:
    """Реализует SubscriptionCheckoutGateway и PaymentRefundGateway для ТБанк."""

    def __init__(
        self,
        settings: TBankSettings,
        client: httpx.AsyncClient,
        *,
        notification_url: str,
        success_url: str,
        fail_url: str,
    ) -> None:
        self._s = settings
        self._client = client
        self._notification_url = notification_url
        self._success_url = success_url
        self._fail_url = fail_url

    async def _post(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        """Подписать, отправить и разобрать ответ метода API ТБанк."""
        signed = {**payload, "Token": make_token(payload, self._s.password)}
        try:
            resp = await self._client.post(
                f"{self._s.api_base_url}/{method}", json=signed
            )
            resp.raise_for_status()
            data: dict[str, object] = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentGatewayError(
                f"ТБанк {method}: сетевая ошибка: {exc}"
            ) from exc
        if not data.get("Success", False):
            raise PaymentGatewayError(
                f"ТБанк {method}: {data.get('ErrorCode')} {data.get('Message')}"
            )
        return data

    async def create_checkout(
        self,
        *,
        subscription_id: uuid.UUID,
        amount_kopecks: int,
        description: str,
        customer_key: str | None = None,
    ) -> CheckoutIntent:
        """Init: создать платёж и вернуть URL платёжной формы банка.

        ``customer_key`` включает рекуррент: пара ``Recurrent=Y`` +
        ``CustomerKey`` делает этот платёж родительским, и в уведомлении о нём
        банк присылает ``RebillId`` — токен для последующих списаний.
        Карточные данные при этом по-прежнему остаются у банка.
        """
        payload: dict[str, object] = {
            "TerminalKey": self._s.terminal_key,
            "Amount": amount_kopecks,
            "OrderId": str(subscription_id),
            "Description": description[:140],
            "PayType": "O",
            "NotificationURL": self._notification_url,
            "SuccessURL": self._success_url,
            "FailURL": self._fail_url,
        }
        if customer_key:
            payload["Recurrent"] = "Y"
            payload["CustomerKey"] = customer_key
        # Чек 54-ФЗ прихода (Тест 7) — если задан e-mail для чека.
        if self._s.receipt_email:
            payload["Receipt"] = build_receipt(
                description=description,
                amount_kopecks=amount_kopecks,
                taxation=self._s.taxation,
                email=self._s.receipt_email,
                phone=None,
            )
        data = await self._post("Init", payload)
        return CheckoutIntent(
            confirmation_url=str(data["PaymentURL"]),
            provider_subscription_id=str(data["PaymentId"]),
        )

    async def charge_recurrent(
        self,
        *,
        subscription_id: uuid.UUID,
        amount_kopecks: int,
        description: str,
        rebill_id: str,
        customer_key: str,
    ) -> str:
        """Списать по сохранённому токену: Init + Charge. Возвращает PaymentId.

        Двухшагово по требованию API: сначала обычный Init (без ``Recurrent`` —
        родительским он уже не является), затем Charge с ``RebillId``. Чек
        нужен и здесь: списание — такой же приход по 54-ФЗ.

        ``OrderId`` уникален для каждой попытки, иначе банк отклонит повтор
        как дубль заказа.
        """
        order_id = f"{subscription_id}:{uuid.uuid4().hex[:8]}"
        payload: dict[str, object] = {
            "TerminalKey": self._s.terminal_key,
            "Amount": amount_kopecks,
            "OrderId": order_id,
            "Description": description[:140],
            "CustomerKey": customer_key,
            "NotificationURL": self._notification_url,
        }
        if self._s.receipt_email:
            payload["Receipt"] = build_receipt(
                description=description,
                amount_kopecks=amount_kopecks,
                taxation=self._s.taxation,
                email=self._s.receipt_email,
                phone=None,
            )
        init = await self._post("Init", payload)
        payment_id = str(init["PaymentId"])

        await self._post(
            "Charge",
            {
                "TerminalKey": self._s.terminal_key,
                "PaymentId": payment_id,
                "RebillId": rebill_id,
            },
        )
        return payment_id

    async def cancel_payment(
        self,
        *,
        provider_payment_id: str,
        amount_kopecks: int,
        receipt: dict[str, object] | None,
    ) -> RefundResult:
        """Cancel: полный возврат подтверждённого платежа (+чек возврата)."""
        payload: dict[str, object] = {
            "TerminalKey": self._s.terminal_key,
            "PaymentId": provider_payment_id,
            "Amount": amount_kopecks,
        }
        if receipt is not None:
            payload["Receipt"] = receipt
        data = await self._post("Cancel", payload)
        return RefundResult(
            provider_payment_id=str(data["PaymentId"]),
            status=str(data.get("Status", "REFUNDED")),
        )
