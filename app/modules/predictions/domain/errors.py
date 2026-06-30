"""Доменные исключения predictions.

Все ошибки наследуются от :class:`PredictionError`, что позволяет API-слою
единообразно маппить их в HTTP-ответы (см. ``app/main.py``), не завязываясь
на конкретику.
"""

from __future__ import annotations


class PredictionError(Exception):
    """Базовая ошибка домена predictions."""


class PredictionsClosedError(PredictionError):
    """Приём прогнозов по событию закрыт.

    Событие не в статусе ``open`` либо серверное время вышло за ``closes_at``
    (дедлайн прошёл). Ставить и править прогнозы нельзя.
    """


class PredictionLockedError(PredictionError):
    """Попытка изменить уже заблокированный прогноз (после ``closes_at``).

    Инвариант честности: после блокировки прогноз неизменяем.
    """


class PredictionNotFoundError(PredictionError):
    """Запрошенный прогноз не найден."""


class PredictionTargetEventNotFoundError(PredictionError):
    """Событие, по которому ставится прогноз, не существует."""


class ProfileUserNotFoundError(PredictionError):
    """Пользователь с таким хэндлом не найден (публичный трек-рекорд)."""


class PredictionSummaryHiddenError(PredictionError):
    """Агрегат прогнозов запрошен в недопустимый момент (зарезервировано)."""


class PredictionSubscriptionRequiredError(PredictionError):
    """Постановка прогноза без активной подписки.

    Смотреть площадку можно бесплатно; голосовать — только с активной подпиской.
    """
