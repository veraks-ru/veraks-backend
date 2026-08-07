"""Адаптер «почта в лог» — режим ненастроенного SMTP."""

from __future__ import annotations

import logging

from app.shared.mail.domain.message import EmailMessage

_LOG = logging.getLogger(__name__)


class LoggingEmailSender:
    """Пишет письмо целиком в лог вместо отправки.

    Целиком — включая текстовое тело со ссылкой входа: смысл режима в том,
    чтобы при ненастроенном SMTP оператор мог достать ссылку из логов и
    впустить человека вручную (а локально — просто войти без почтового
    сервера). Это осознанная утечка одноразового секрета в лог, поэтому
    режим включается ровно тогда, когда ``MAIL_HOST`` не задан, а вне
    ``local`` при старте пишется ERROR (см. ``factory.warn_if_mail_unconfigured``).
    """

    async def send(self, message: EmailMessage) -> None:
        """Логирует письмо (INFO) — доставки не происходит."""
        _LOG.info(
            "Почта не настроена — письмо не отправлено, печатаем в лог.\n"
            "Кому: %s\nТема: %s\n%s",
            message.to,
            message.subject,
            message.text_body,
        )
