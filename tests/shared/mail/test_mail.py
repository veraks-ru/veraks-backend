"""Выбор почтового адаптера и предупреждение о ненастроенной почте.

Здесь проверяется решение, принятое сознательно и против общего для проекта
паттерна fail-fast: ненастроенный SMTP НЕ валит старт, письма уходят в лог, а
вне ``local`` при старте пишется ERROR. Раз падения нет, единственная защита
от «письма молча не доходят» — это самое сообщение в логах, поэтому оно
покрыто тестом наравне с кодом.
"""

from __future__ import annotations

import logging

import pytest

from app.config import MailSettings
from app.shared.mail.adapters.factory import (
    build_email_sender,
    warn_if_mail_unconfigured,
)
from app.shared.mail.adapters.log_sender import LoggingEmailSender
from app.shared.mail.adapters.smtp import SmtpEmailSender
from app.shared.mail.domain.message import EmailMessage

_LETTER = EmailMessage(
    to="user@example.com",
    subject="Ссылка для входа в Веракс",
    text_body="Ссылка: https://veraks.test/auth/email/callback?token=secret-token",
    html_body="<p>Ссылка</p>",
)


# ── Выбор адаптера ────────────────────────────────────────────────────────


def test_configured_host_selects_smtp() -> None:
    sender = build_email_sender(MailSettings(host="smtp.example.com", port=587))

    assert isinstance(sender, SmtpEmailSender)


def test_empty_host_falls_back_to_log() -> None:
    """Пустой MAIL_HOST — деградация в лог, а не отказ старта."""
    sender = build_email_sender(MailSettings(host=""))

    assert isinstance(sender, LoggingEmailSender)


def test_blank_host_is_treated_as_unconfigured() -> None:
    """Пробелы вместо адреса — тоже «не настроено», а не хост из пробелов."""
    settings = MailSettings(host="   ")

    assert settings.configured is False
    assert isinstance(build_email_sender(settings), LoggingEmailSender)


async def test_log_sender_prints_the_login_link(caplog) -> None:
    """В лог попадает письмо целиком — оператор должен достать из него ссылку.

    Это осознанная утечка одноразового секрета в лог: смысл режима в том,
    чтобы при ненастроенном SMTP человека всё-таки можно было впустить.
    """
    # Уровень WARNING, а не INFO: приложение не настраивает logging, и запись
    # ниже WARNING не попала бы в вывод пода — режим «достать ссылку из логов»
    # молча перестал бы работать (ровно это и случилось на боевом стенде).
    with caplog.at_level(logging.WARNING, logger="app.shared.mail.adapters.log_sender"):
        await LoggingEmailSender().send(_LETTER)

    assert "secret-token" in caplog.text
    assert "user@example.com" in caplog.text


# ── Стартовое предупреждение ──────────────────────────────────────────────

_FACTORY_LOGGER = "app.shared.mail.adapters.factory"


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.message for r in caplog.records if r.levelno >= logging.ERROR]


def test_unconfigured_mail_outside_local_logs_error(caplog) -> None:
    """Главная страховка режима «без fail-fast»: ERROR при старте."""
    with caplog.at_level(logging.ERROR, logger=_FACTORY_LOGGER):
        warn_if_mail_unconfigured(MailSettings(host=""), app_env="prod")

    errors = _errors(caplog)
    assert len(errors) == 1
    # Сообщение должно объяснять и что сломано, и чем это грозит, и как чинить.
    assert "MAIL_HOST" in errors[0]
    assert "в лог" in errors[0]
    assert "prod" in caplog.text


def test_unconfigured_mail_in_local_is_silent(caplog) -> None:
    """Локально письма в логе — штатный режим разработки, ругаться не за что."""
    with caplog.at_level(logging.ERROR, logger=_FACTORY_LOGGER):
        warn_if_mail_unconfigured(MailSettings(host=""), app_env="local")

    assert _errors(caplog) == []


def test_configured_mail_is_silent_outside_local(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger=_FACTORY_LOGGER):
        warn_if_mail_unconfigured(
            MailSettings(host="smtp.example.com"), app_env="prod"
        )

    assert _errors(caplog) == []


def test_app_startup_emits_the_warning(caplog, monkeypatch) -> None:
    """Сквозная проверка: предупреждение реально вызывается при сборке приложения.

    Без неё тесты выше проверяли бы функцию, которую забыли позвать из
    ``create_app`` — а именно этот вызов и есть весь механизм оповещения.
    """
    import app.config as config_module
    from app.config import Settings
    from app.main import create_app

    prod = Settings(
        app_env="prod",
        database_url="postgresql+asyncpg://x/x",
        mail=MailSettings(host=""),
        billing={"checkout_provider": "tbank"},
        tbank={"enabled": True, "terminal_key": "T", "password": "p"},
    )
    # ``create_app`` импортирует get_settings внутри тела — подменяем в модуле.
    monkeypatch.setattr(config_module, "get_settings", lambda: prod)

    with caplog.at_level(logging.ERROR, logger=_FACTORY_LOGGER):
        create_app()

    assert any("MAIL_HOST" in message for message in _errors(caplog))
