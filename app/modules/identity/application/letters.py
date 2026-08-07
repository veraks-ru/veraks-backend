"""Тексты писем identity (сейчас — одно: ссылка для входа).

Письмо собирается как значение (``EmailMessage``) без обращений к сети и без
внешних ресурсов: ни картинок, ни шрифтов, ни трекинг-пикселя. Причина не в
эстетике — любой внешний ресурс в письме про вход сообщает третьей стороне
факт и время входа конкретного человека, а часть почтовых клиентов ещё и
подставляет адрес получателя в запрос.
"""

from __future__ import annotations

from html import escape

from app.shared.mail.domain.message import EmailMessage

_SUBJECT = "Ссылка для входа в Веракс"


def build_magic_link_letter(*, to: str, link: str, ttl_minutes: int) -> EmailMessage:
    """Письмо со ссылкой входа: тема, текстовая и HTML-версии.

    ``link`` подставляется в HTML экранированным (``&`` в query-string обязан
    стать ``&amp;``, иначе разметка невалидна, а часть клиентов режет ссылку).
    """
    text_body = (
        "Здравствуйте!\n\n"
        "Вы запросили вход в Веракс — биржу репутации предсказателей.\n"
        "Перейдите по ссылке, чтобы войти:\n\n"
        f"{link}\n\n"
        f"Ссылка действует {ttl_minutes} минут и сработает один раз.\n\n"
        "Если вы не запрашивали вход — просто проигнорируйте это письмо: "
        "без перехода по ссылке ничего не произойдёт.\n\n"
        "— Веракс\n"
    )
    safe_link = escape(link, quote=True)
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,'
        'sans-serif;font-size:16px;line-height:1.5;color:#141414">'
        "<p>Здравствуйте!</p>"
        "<p>Вы запросили вход в <strong>Веракс</strong> — биржу репутации "
        "предсказателей.</p>"
        f'<p><a href="{safe_link}" style="display:inline-block;padding:12px 24px;'
        'border-radius:10px;background:#141414;color:#ffffff;'
        'text-decoration:none">Войти в Веракс</a></p>'
        "<p>Если кнопка не работает, скопируйте ссылку в адресную строку:<br>"
        f'<a href="{safe_link}">{safe_link}</a></p>'
        f"<p>Ссылка действует {ttl_minutes} минут и сработает один раз.</p>"
        "<p>Если вы не запрашивали вход — просто проигнорируйте это письмо: "
        "без перехода по ссылке ничего не произойдёт.</p>"
        "<p>— Веракс</p>"
        "</div>"
    )
    return EmailMessage(
        to=to, subject=_SUBJECT, text_body=text_body, html_body=html_body
    )
