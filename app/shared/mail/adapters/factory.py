"""Выбор почтового адаптера по факту настройки — БЕЗ fail-fast.

Остальные внешние интеграции проекта выбираются fail-fast (см.
``Settings._require_billing_providers_in_prod``): вне ``local`` неполная
настройка валит старт. Здесь принято ОБРАТНОЕ решение, и вот почему.

Платёжный шлюз — не единственный вход в систему: приложение без него
поднимается и работает, падение старта лишь не даёт выкатить заведомо
сломанный биллинг. Почта сейчас — другое: ЕСИА выключена
(``AUTH_PROVIDERS=email``), поэтому письмо со ссылкой — ЕДИНСТВЕННЫЙ способ
войти, а SMTP-учётки в проде ещё нет. Fail-fast означал бы, что платформа
вообще не поднимается — недоступны и публичные витрины, и уже вошедшие
сессии. Деградация в лог оставляет систему живой: оператор достаёт ссылку
входа из логов, а починка — это добавить ``MAIL_*`` и перезапуститься, без
единой правки кода.

Чтобы деградация не осталась незамеченной, вне ``local`` при старте пишется
ERROR (:func:`warn_if_mail_unconfigured`).
"""

from __future__ import annotations

import logging

from app.config import MailSettings
from app.shared.mail.adapters.log_sender import LoggingEmailSender
from app.shared.mail.adapters.smtp import SmtpEmailSender
from app.shared.mail.ports.sender import EmailSender

_LOG = logging.getLogger(__name__)


def build_email_sender(settings: MailSettings) -> EmailSender:
    """``MAIL_HOST`` задан → SMTP; не задан → письма в лог."""
    if settings.configured:
        return SmtpEmailSender(settings)
    return LoggingEmailSender()


def warn_if_mail_unconfigured(settings: MailSettings, *, app_env: str) -> None:
    """Вне ``local`` громко предупреждает о ненастроенной почте (ERROR при старте).

    Приложение при этом стартует и работает — см. докстринг модуля.
    """
    if app_env == "local" or settings.configured:
        return
    _LOG.error(
        "Почта не настроена (пустой MAIL_HOST) в окружении '%s': письма со "
        "ссылками входа пишутся в лог, пользователи их НЕ получат. Вход по "
        "email — единственный включённый способ входа; заполните MAIL_HOST и "
        "остальные MAIL_* и перезапустите приложение.",
        app_env,
    )
