"""Адаптер Jump.Finance: smart-выплата по СБП, опрос статуса, ошибки.

Формат запросов/ответов — по официальному OpenAPI-спеку Jump
(https://apidoc.jump.finance/openapi-v1): СБП — ``type_id=10``,
идемпотентность — ``customer_payment_id`` (uuid нашей выплаты),
статус — ``GET /payments/{id}`` c флагом ``is_final``.
"""

import json
import uuid

import httpx
import pytest

from app.config import JumpSettings
from app.modules.billing.adapters.jump_gateway import JumpGateway
from app.modules.billing.domain.errors import PaymentGatewayError
from app.modules.billing.ports.gateways import PayoutRecipient

_BASE = "https://jump.test/services/openapi"


def _settings(**overrides: object) -> JumpSettings:
    params: dict[str, object] = {
        "enabled": True,
        "api_key": "jump-client-key",
        "api_base_url": _BASE,
        "agent_id": 77,
        "bank_account_id": 5,
    }
    params.update(overrides)
    return JumpSettings(**params)  # type: ignore[arg-type]


def _gateway(handler, **overrides: object) -> JumpGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JumpGateway(_settings(**overrides), client)


def _recipient() -> PayoutRecipient:
    return PayoutRecipient(
        phone="+79001234567",
        last_name="Иванов",
        first_name="Пётр",
        middle_name="Сергеевич",
        sbp_bank_id="100000000004",
    )


def _payment_response(payment_id: int = 15731787) -> dict:
    return {
        "item": {
            "id": payment_id,
            "amount": 1234.56,
            "status": {"id": 3, "title": "в обработке"},
            "is_final": False,
        }
    }


async def test_send_payout_posts_smart_payment_with_sbp_requisite():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=_payment_response())

    payout_id = uuid.uuid4()
    instruction = await _gateway(handler).send_payout(
        payout_id=payout_id,
        user_id=uuid.uuid4(),
        amount_kopecks=123_456,
        recipient=_recipient(),
    )

    assert instruction.provider == "jump"
    assert instruction.provider_payout_id == "15731787"
    assert captured["path"].endswith("/payments/smart")
    assert captured["headers"]["Client-Key"] == "jump-client-key"

    body = captured["body"]
    # Идемпотентность на стороне Jump — uuid нашей выплаты (≤36 символов).
    assert body["customer_payment_id"] == str(payout_id)
    assert body["phone"] == "+79001234567"
    assert body["last_name"] == "Иванов"
    assert body["first_name"] == "Пётр"
    assert body["middle_name"] == "Сергеевич"
    assert body["amount"] == 1234.56
    assert body["agent_id"] == 77
    assert body["bank_account_id"] == 5
    assert body["requisite"] == {
        "type_id": 10,
        "account_number": "+79001234567",
        "sbp_bank_id": 100000000004,
    }
    # Данные для проверки получателя на стороне банка СБП.
    assert body["sbp_validation_data"] == {
        "last_name": "Иванов",
        "first_name": "Пётр",
        "middle_name": "Сергеевич",
    }


async def test_send_payout_omits_optional_fields_when_absent():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json=_payment_response())

    recipient = PayoutRecipient(
        phone="+79001234567",
        last_name="Иванов",
        first_name="Пётр",
        middle_name=None,
        sbp_bank_id="100000000004",
    )
    await _gateway(handler, bank_account_id=None).send_payout(
        payout_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        amount_kopecks=100,
        recipient=recipient,
    )
    body = captured["body"]
    assert "middle_name" not in body
    assert "bank_account_id" not in body
    assert "middle_name" not in body["sbp_validation_data"]
    assert body["amount"] == 1.0


async def test_send_payout_error_response_raises_gateway_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={
            "error": {
                "title": "Ошибка валидации",
                "detail": "Неверный банк СБП",
                "fields": None,
                "code": 422,
            }
        })

    with pytest.raises(PaymentGatewayError, match="Неверный банк СБП"):
        await _gateway(handler).send_payout(
            payout_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            amount_kopecks=100,
            recipient=_recipient(),
        )


async def test_send_payout_error_includes_field_details():
    # Jump кладёт конкретику валидации в error.fields — без неё «Ошибка в
    # переданных данных» недиагностируема.
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={
            "error": {
                "title": "Ошибка",
                "detail": "Ошибка в переданных данных.",
                "fields": [
                    {"field": "requisite.sbp_bank_id", "messages": ["Банк не найден"]},
                    {"field": "phone", "messages": ["Неверный формат"]},
                ],
                "code": 422,
            }
        })

    with pytest.raises(
        PaymentGatewayError,
        match=r"requisite\.sbp_bank_id: Банк не найден.*phone: Неверный формат",
    ):
        await _gateway(handler).send_payout(
            payout_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            amount_kopecks=100,
            recipient=_recipient(),
        )


async def test_send_payout_network_error_raises_gateway_error():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    with pytest.raises(PaymentGatewayError):
        await _gateway(handler).send_payout(
            payout_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            amount_kopecks=100,
            recipient=_recipient(),
        )


async def test_send_payout_malformed_response_raises_gateway_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(PaymentGatewayError):
        await _gateway(handler).send_payout(
            payout_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            amount_kopecks=100,
            recipient=_recipient(),
        )


async def test_get_payout_status_returns_status_view():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/payments/15731787")
        assert req.headers["Client-Key"] == "jump-client-key"
        return httpx.Response(200, json={
            "item": {
                "id": 15731787,
                "status": {"id": 1, "title": "оплачен"},
                "is_final": True,
            }
        })

    view = await _gateway(handler).get_payout_status(provider_payout_id="15731787")
    assert view.status_id == 1
    assert view.is_final is True
