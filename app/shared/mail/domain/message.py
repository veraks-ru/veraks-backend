"""Письмо как иммутабельное значение (без транспорта и без I/O)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """Одно письмо одному получателю.

    Всегда две версии тела: ``text_body`` — обязательная (почтовые клиенты,
    отключившие HTML, и антиспам-фильтры, которые режут письма без текстовой
    части), ``html_body`` — оформленная. Внешних картинок и трекеров в теле
    быть не должно (см. ``identity.application.letters``): пиксель-трекер в
    письме про вход — это утечка факта и времени входа третьей стороне.
    """

    to: str
    subject: str
    text_body: str
    html_body: str
