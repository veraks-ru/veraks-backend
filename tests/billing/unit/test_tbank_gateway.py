"""Адаптер ТБанк: Init формирует тело+Token и возвращает PaymentURL; Cancel; чек."""

import json
import uuid

import httpx
import pytest

from app.config import TBankSettings
from app.modules.billing.adapters.tbank_gateway import TBankGateway
from app.modules.billing.domain.errors import PaymentGatewayError
from app.modules.billing.domain.receipt import build_receipt
from app.modules.billing.domain.tbank_signing import make_token


def _settings() -> TBankSettings:
    return TBankSettings(
        enabled=True, terminal_key="TDEMO", password="p",
        api_base_url="https://pay.test/v2",
    )


def _gateway(handler) -> TBankGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TBankGateway(
        _settings(), client,
        notification_url="https://api.veraks.ru/webhooks/payments/tbank",
        success_url="https://veraks.ru/account",
        fail_url="https://veraks.ru/account",
    )


async def test_init_builds_request_and_returns_payment_url():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(json.loads(req.content))
        assert req.url.path.endswith("/Init")
        return httpx.Response(200, json={
            "Success": True, "Status": "NEW",
            "PaymentId": "900", "PaymentURL": "https://pay.test/form/900",
        })

    sub_id = uuid.uuid4()
    intent = await _gateway(handler).create_checkout(
        subscription_id=sub_id, amount_kopecks=99000, description="Подписка monthly"
    )
    assert intent.confirmation_url == "https://pay.test/form/900"
    assert intent.provider_subscription_id == "900"
    assert captured["Amount"] == 99000
    assert captured["OrderId"] == str(sub_id)
    assert captured["TerminalKey"] == "TDEMO"
    assert captured["NotificationURL"] == "https://api.veraks.ru/webhooks/payments/tbank"
    # Token корректен (make_token игнорирует само поле Token)
    assert captured["Token"] == make_token(captured, "p")


async def test_init_includes_receipt_when_email_configured():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(json.loads(req.content))
        return httpx.Response(200, json={
            "Success": True, "Status": "NEW",
            "PaymentId": "901", "PaymentURL": "https://pay.test/form/901",
        })

    settings = TBankSettings(
        enabled=True, terminal_key="TDEMO", password="p",
        api_base_url="https://pay.test/v2", receipt_email="chek@example.com",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gw = TBankGateway(settings, client, notification_url="n",
                      success_url="s", fail_url="f")
    await gw.create_checkout(
        subscription_id=uuid.uuid4(), amount_kopecks=99000, description="Подписка"
    )
    assert captured["Receipt"]["Email"] == "chek@example.com"
    # Token не учитывает вложенный Receipt (исключён из подписи)
    assert captured["Token"] == make_token(captured, "p")


async def test_init_no_receipt_without_email():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(json.loads(req.content))
        return httpx.Response(200, json={
            "Success": True, "Status": "NEW", "PaymentId": "902",
            "PaymentURL": "https://pay.test/form/902",
        })

    await _gateway(handler).create_checkout(
        subscription_id=uuid.uuid4(), amount_kopecks=1, description="x"
    )
    assert "Receipt" not in captured


async def test_init_failure_raises_gateway_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "Success": False, "ErrorCode": "1", "Message": "Отказ",
        })

    with pytest.raises(PaymentGatewayError):
        await _gateway(handler).create_checkout(
            subscription_id=uuid.uuid4(), amount_kopecks=1, description="x"
        )


async def test_cancel_sends_payment_id_amount_and_token():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(json.loads(req.content))
        assert req.url.path.endswith("/Cancel")
        return httpx.Response(200, json={
            "Success": True, "Status": "REFUNDED", "PaymentId": "900",
        })

    res = await _gateway(handler).cancel_payment(
        provider_payment_id="900", amount_kopecks=99000, receipt=None
    )
    assert res.status == "REFUNDED"
    assert res.provider_payment_id == "900"
    assert captured["PaymentId"] == "900"
    assert captured["Amount"] == 99000
    assert captured["Token"] == make_token(captured, "p")


async def test_init_network_error_raises_gateway_error():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    with pytest.raises(PaymentGatewayError):
        await _gateway(handler).create_checkout(
            subscription_id=uuid.uuid4(), amount_kopecks=1, description="x"
        )


def test_build_receipt_single_service_item():
    receipt = build_receipt(
        description="Подписка monthly", amount_kopecks=99000,
        taxation="usn_income", email="u@example.com", phone=None,
    )
    assert receipt["Taxation"] == "usn_income"
    assert receipt["Email"] == "u@example.com"
    assert "Phone" not in receipt
    items = receipt["Items"]
    assert isinstance(items, list) and len(items) == 1
    assert items[0]["Amount"] == 99000
    assert items[0]["Tax"] == "none"
    assert items[0]["PaymentObject"] == "service"
