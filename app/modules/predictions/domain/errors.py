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


class PredictionSubscriptionRequiredError(PredictionError):
    """Постановка прогноза без активной подписки.

    Смотреть площадку можно бесплатно; голосовать — только с активной подпиской.
    """


class EventTopPredictionsUnavailableError(PredictionError):
    """Доска лучших прогнозов запрошена не для разрешённого события.

    Доступна только в статусе ``resolved``: до разрешения Brier ещё не
    посчитан, а после аннулирования (ст. 1058 ГК РФ) событие целиком
    исключается из публичных витрин точности.
    """
