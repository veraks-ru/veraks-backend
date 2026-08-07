"""SMTP-адаптер отправки писем (aiosmtplib)."""

from __future__ import annotations

from email.message import EmailMessage as MimeMessage

import aiosmtplib

from app.config import MailSettings
from app.shared.mail.domain.message import EmailMessage


class SmtpEmailSender:
    """Отправка письма по SMTP: соединение на каждое письмо.

    Пул соединений не держим намеренно: писем мало (ссылка входа —
    единственный сценарий), а живое SMTP-соединение между запросами пришлось
    бы переподключать после таймаутов сервера. ``username``/``password``
    опциональны — локальный mailpit принимает почту без аутентификации, и
    пустые креды не должны превращаться в попытку AUTH с пустым логином.

    ``use_tls`` — сразу TLS-сокет (порт 465), ``use_starttls`` — апгрейд
    открытого соединения (порт 587). Взаимоисключающи: при ``use_tls``
    STARTTLS не запрашивается.
    """

    def __init__(self, settings: MailSettings, *, timeout_seconds: float = 10.0) -> None:
        self._settings = settings
        self._timeout = timeout_seconds

    async def send(self, message: EmailMessage) -> None:
        """Собирает multipart/alternative и отправляет его SMTP-серверу."""
        mime = MimeMessage()
        mime["From"] = self._settings.sender_header
        mime["To"] = message.to
        mime["Subject"] = message.subject
        # Ссылка входа — одноразовый секрет: просим не индексировать и не
        # предзагружать письмо автоматическими сканерами ссылок.
        mime["X-Auto-Response-Suppress"] = "All"
        mime.set_content(message.text_body)
        mime.add_alternative(message.html_body, subtype="html")

        use_tls = self._settings.use_tls
        await aiosmtplib.send(
            mime,
            hostname=self._settings.host,
            port=self._settings.port,
            username=self._settings.username or None,
            password=self._settings.password or None,
            use_tls=use_tls,
            start_tls=self._settings.use_starttls if not use_tls else False,
            # Дефолт aiosmtplib — 60 секунд. Это не «на всякий случай»
            # большое число: неотвечающий SMTP держал бы фоновую задачу
            # (а раньше — и HTTP-запрос) целую минуту. Письмо со ссылкой
            # входа либо уходит быстро, либо не уходит вовсе.
            timeout=self._timeout,
        )
