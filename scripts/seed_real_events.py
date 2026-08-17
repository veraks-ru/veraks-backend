"""Наполнение боевой базы реальными событиями по новостной повестке.

В отличие от ``seed.py`` (демо-данные, начинается с TRUNCATE) — этот скрипт
только добавляет события и ничего не удаляет. Формулировки собраны под
конкретную повестку августа–сентября 2026 и одноразовы: файл остаётся в репо
как след того, что именно и на каком основании было заведено.

Идёт через доменные use-cases ``CreateEvent`` + ``PublishEvent``, а не через
INSERT: так работает валидация окна (``opens_at < closes_at <= resolves_at``),
проверка запрещённых категорий (PRD §7.5) и запись в append-only аудит.

Требования к каждому событию (иначе оно бесполезно для скоринга):
  * бинарность — исход строго ДА/НЕТ, без «частично»;
  * проверяемость — назван источник, по которому исход читается однозначно;
  * разрешение внутри окна сезона, иначе прогноз не попадёт в зачёт;
  * неочевидность — у события, где все ответят одинаково, вес ``w_e ≈ 0``
    и оно не даёт участникам ничего.

Запуск (через port-forward к прод-БД)::

    python scripts/seed_real_events.py --database-url-file <файл> --dry-run
    python scripts/seed_real_events.py --database-url-file <файл> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.modules.events.adapters.clock import SystemClock
from app.modules.events.adapters.orm import EventORM
from app.modules.events.adapters.repository import (
    SqlAlchemyCategoryRepository,
    SqlAlchemyEventRepository,
)
from app.modules.events.application.dto import (
    Actor,
    NewEventInput,
)
from app.modules.events.application.use_cases import (
    CancelEvent,
    CreateEvent,
    PublishEvent,
)
from app.modules.identity.adapters.orm import UserORM
from app.modules.identity.domain.entities import UserRole
from app.modules.seasons.adapters.clock import SystemClock as SeasonSystemClock
from app.modules.seasons.adapters.orm import SeasonORM
from app.modules.seasons.adapters.season_repository import SqlAlchemySeasonRepository
from app.modules.seasons.application.use_cases import UpdateSeason
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail


def d(day: int, hour: int = 12, month: int = 9) -> datetime:
    """Дата 2026 года в UTC — короткая запись для таблицы событий ниже."""
    return datetime(2026, month, day, hour, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Draft:
    """Заготовка события: то, что попадёт в ``NewEventInput``."""

    category: str  # slug
    title: str
    description: str
    closes: datetime
    resolves: datetime
    source: str
    criteria: str
    # Сезон события: по умолчанию тот, что передан аргументом. Событие,
    # разрешающееся за окном своего сезона, в зачёт не попадёт — сезон
    # финализируется в ends_at, — поэтому для таких указывается другой slug.
    season_slug: str | None = None


AUG = 8

# ── Повестка ────────────────────────────────────────────────────────────────
# Факты на 10.08.2026, от которых отталкиваются формулировки, вынесены в
# описания событий: участник должен видеть точку отсчёта, а не угадывать её.

DRAFTS: list[Draft] = [
    # ── Финансы ─────────────────────────────────────────────────────────────
    Draft(
        "finance",
        "Банк России снизит ключевую ставку на заседании 11 сентября?",
        "После снижения 19 июня ключевая ставка составляет 14,00% годовых. "
        "Совет директоров соберётся 11 сентября 2026 года. Засчитывается ДА, "
        "если объявленная ставка окажется ниже 14,00%.",
        d(11, 9), d(11, 16),
        "Пресс-релиз Банка России (cbr.ru/press/keypr)",
        "ДА — если новая ключевая ставка ниже 14,00% годовых. НЕТ — если она "
        "сохранена или повышена.",
    ),
    Draft(
        "finance",
        "Ставка ЦБ после сентябрьского заседания опустится до 13,50% или ниже?",
        "Речь о шаге снижения. С 14,00% это означает −50 базисных пунктов или "
        "больше за одно заседание. Часть аналитиков ждёт более быстрого цикла "
        "смягчения, часть — осторожного шага в 25 б.п.",
        d(11, 9), d(11, 16),
        "Пресс-релиз Банка России (cbr.ru/press/keypr)",
        "ДА — если объявленная ставка ≤ 13,50% годовых.",
    ),
    Draft(
        "finance",
        "ФРС сохранит ставку в диапазоне 3,50–3,75% на заседании 16 сентября?",
        "Ставка не менялась с декабря 2025 года. На июльском заседании трое "
        "членов FOMC голосовали против сохранения и выступали за повышение. "
        "Сентябрьское заседание сопровождается обновлением прогнозов (dot plot).",
        d(16, 17), d(16, 21),
        "Заявление FOMC (federalreserve.gov)",
        "ДА — если целевой диапазон остался 3,50–3,75%. НЕТ — при любом "
        "изменении диапазона.",
    ),
    Draft(
        "finance",
        "Годовая инфляция в США за август окажется ниже 3,5%?",
        "Индекс потребительских цен в июне составил 3,5% в годовом выражении "
        "против 4,2% в мае — всплеск цен на энергоносители развернулся. "
        "Данные за август публикуются в середине сентября.",
        d(10, 12), d(18, 20),
        "Bureau of Labor Statistics, релиз CPI (bls.gov)",
        "ДА — если годовой рост CPI за август строго ниже 3,5%.",
    ),
    # ── Криптовалюта ────────────────────────────────────────────────────────
    Draft(
        "crypto",
        "Биткоин поднимется выше $70 000 хотя бы раз до 30 сентября?",
        "7 августа биткоин торговался около $65 100, войдя в месяц на уровне "
        "$64 040. Актив остаётся в нисходящем канале, для разворота нужен "
        "пробой линии сопротивления.",
        d(29, 20), d(30, 22),
        "Дневные максимумы BTC/USD по данным CoinGecko",
        "ДА — если внутридневной максимум BTC/USD хотя бы раз превысил $70 000 "
        "до 30 сентября включительно.",
    ),
    Draft(
        "crypto",
        "Эфириум закроет 30 сентября выше $2 000?",
        "Эфириум держится около $1 929 и технически выглядит сильнее биткоина: "
        "торгуется выше ключевой поддержки, +2% за неделю.",
        d(29, 20), d(30, 23),
        "Цена закрытия ETH/USD 30 сентября 2026 по CoinGecko (UTC)",
        "ДА — если цена закрытия выше $2 000.",
    ),
    Draft(
        "crypto",
        "Биткоин опустится ниже $55 000 хотя бы раз до 30 сентября?",
        "Обратная сторона того же нисходящего канала: пробой вниз. От текущих "
        "уровней это падение примерно на 15%.",
        d(29, 20), d(30, 22),
        "Дневные минимумы BTC/USD по данным CoinGecko",
        "ДА — если внутридневной минимум BTC/USD хотя бы раз опускался ниже "
        "$55 000 до 30 сентября включительно.",
    ),
    Draft(
        "crypto",
        "Эфириум обгонит биткоин по доходности за сентябрь?",
        "Сравниваются процентные изменения с 31 августа по 30 сентября. "
        "Аналитики отмечают относительную силу ETH, но исторически сентябрь "
        "слабый месяц для всего рынка.",
        d(29, 20), d(30, 23),
        "Цены закрытия BTC/USD и ETH/USD 31 августа и 30 сентября по CoinGecko",
        "ДА — если процентное изменение ETH за сентябрь строго выше, чем у BTC.",
    ),
    # ── Выборы ──────────────────────────────────────────────────────────────
    Draft(
        "election",
        "Явка на выборах в Госдуму превысит 50%?",
        "Голосование проходит 18–20 сентября 2026 года, три дня, с "
        "возможностью дистанционного электронного голосования. Явка на "
        "выборах в Думу 2021 года составила около 51,7%.",
        d(18, 4), d(23, 20),
        "Итоговые данные ЦИК России (cikrf.ru)",
        "ДА — если официальная явка по федеральному округу строго выше 50,0%.",
    ),
    Draft(
        "election",
        "«Единая Россия» получит 300 и более мандатов в новой Госдуме?",
        "300 мандатов из 450 — конституционное большинство, позволяющее "
        "принимать поправки к Конституции. В созыве 2021 года партия получила "
        "324 мандата.",
        d(18, 4), d(25, 20),
        "Итоговое распределение мандатов по данным ЦИК России",
        "ДА — если суммарно (списки + одномандатные округа) партия получила "
        "не менее 300 мандатов.",
    ),
    Draft(
        "election",
        "В Госдуму пройдут более пяти партий?",
        "В действующем созыве представлены пять партий. Барьер для прохождения "
        "по спискам — 5% голосов.",
        d(18, 4), d(25, 20),
        "Итоговые данные ЦИК России о партиях, преодолевших барьер",
        "ДА — если 5%-й барьер преодолели шесть и более партий.",
    ),
    Draft(
        "election",
        "Все действующие главы 11 регионов сохранят посты по итогам выборов?",
        "Одновременно с думскими проходят выборы глав 11 субъектов Федерации "
        "и депутатов заксобраний в 39 регионах.",
        d(18, 4), d(25, 20),
        "Итоговые данные избирательных комиссий субъектов",
        "ДА — если во всех 11 регионах победили действующие главы (включая "
        "врио). НЕТ — если хотя бы в одном победил другой кандидат.",
    ),
    Draft(
        "election",
        "Доля проголосовавших через ДЭГ превысит 15% от общего числа голосов?",
        "Дистанционное электронное голосование доступно в ряде регионов "
        "наряду с участками; голосование идёт с 8:00 до 20:00.",
        d(18, 4), d(25, 20),
        "Данные ЦИК России о числе проголосовавших через ДЭГ",
        "ДА — если доля голосов, поданных через ДЭГ, строго выше 15% от "
        "общего числа проголосовавших.",
    ),
    # ── Политика ────────────────────────────────────────────────────────────
    Draft(
        "politics",
        "Саммит будущего 22–23 сентября завершится принятием итогового документа?",
        "Саммит предваряет общие прения 81-й сессии Генассамблеи ООН. "
        "Подобные встречи не всегда заканчиваются согласованным текстом: "
        "документ требует консенсуса государств-членов.",
        d(22, 8), d(24, 20),
        "Официальные документы и пресс-релизы ООН (un.org)",
        "ДА — если по итогам саммита принят итоговый документ (декларация или "
        "резолюция).",
    ),
    Draft(
        "politics",
        "Лидеры всех пяти постоянных членов Совбеза ООН выступят на прениях лично?",
        "Общие прения 81-й сессии идут 22–26 и 28 сентября. Главы государств "
        "нередко направляют вместо себя министров иностранных дел или "
        "постоянных представителей.",
        d(22, 8), d(29, 20),
        "Официальный список выступавших на общих прениях (gadebate.un.org)",
        "ДА — если от каждой из пяти стран (Великобритания, Китай, Россия, "
        "США, Франция) выступил глава государства или правительства лично.",
    ),
    Draft(
        "politics",
        "Переговоры по Ормузскому проливу приведут к объявленному соглашению до 30 сентября?",
        "Ход переговоров в начале августа оставался одним из факторов, за "
        "которыми следят финансовые рынки.",
        d(29, 12), d(30, 20),
        "Официальные заявления сторон переговоров, сообщения Reuters/AP",
        "ДА — если стороны публично объявили о достигнутом соглашении до "
        "30 сентября включительно.",
    ),
    Draft(
        "politics",
        "Евросоюз утвердит новый пакет санкций до 30 сентября?",
        "Речь о формально утверждённом Советом ЕС пакете, а не о предложении "
        "Еврокомиссии или обсуждении на уровне послов.",
        d(29, 12), d(30, 20),
        "Официальный журнал ЕС (eur-lex.europa.eu), решения Совета ЕС",
        "ДА — если новый пакет ограничительных мер официально принят Советом "
        "ЕС и опубликован до 30 сентября включительно.",
    ),
    # ── Спорт ───────────────────────────────────────────────────────────────
    Draft(
        "sport",
        "Янник Синнер выиграет US Open 2026?",
        "Турнир идёт с 23 августа по 13 сентября. Синнер — первая ракетка "
        "мира и действующий чемпион Уимблдона, у букмекеров основной фаворит.",
        d(6, 12), d(14, 12),
        "Официальный сайт турнира (usopen.org)",
        "ДА — если Синнер выиграл одиночный разряд среди мужчин.",
    ),
    Draft(
        "sport",
        "Арина Соболенко возьмёт третий подряд титул US Open?",
        "Соболенко — первая ракетка мира, за последний год на харде 39 побед "
        "при 4 поражениях. Основные соперницы: Швёнтек, Гауфф, Рыбакина, "
        "Андреева.",
        d(6, 12), d(14, 12),
        "Официальный сайт турнира (usopen.org)",
        "ДА — если Соболенко выиграла одиночный разряд среди женщин.",
    ),
    Draft(
        "sport",
        "Гран-при Италии в Монце выиграет пилот, стартовавший с поула?",
        "Гонка проходит 6 сентября. Монца — трасса с длинными прямыми и "
        "сильным эффектом слипстрима, где преимущество поула часто теряется.",
        d(6, 11), d(7, 12),
        "Официальные результаты FIA (formula1.com)",
        "ДА — если победитель гонки стартовал с первой позиции.",
    ),
    Draft(
        "sport",
        "Сборная США выиграет чемпионат мира по баскетболу среди женщин?",
        "Турнир проходит 4–13 сентября в Германии. Американки — многолетние "
        "фавориты женского баскетбола.",
        d(6, 12), d(14, 12),
        "Официальный сайт FIBA (fiba.basketball)",
        "ДА — если сборная США выиграла золото.",
    ),
    Draft(
        "sport",
        "В первом туре Лиги чемпионов будет забито 50 и более голов?",
        "Первый тур основного этапа проходит 8–10 сентября: 18 матчей. "
        "50 голов — это в среднем 2,8 за матч.",
        d(8, 15), d(11, 12),
        "Официальный сайт УЕФА (uefa.com), протоколы матчей",
        "ДА — если суммарно во всех матчах первого тура забито ≥ 50 голов "
        "(основное время, включая добавленное).",
    ),
    # ── Технологии ──────────────────────────────────────────────────────────
    Draft(
        "tech",
        "Apple представит складной iPhone на сентябрьской презентации?",
        "По слухам, первый складной iPhone книжного типа готов к выходу: "
        "около 7,6 дюйма в раскрытом виде и 5,3 в сложенном. Компания не "
        "подтверждала ни устройство, ни сроки.",
        d(8, 12), d(20, 20),
        "Официальная трансляция и пресс-релизы Apple (apple.com/newsroom)",
        "ДА — если складной iPhone официально анонсирован на сентябрьском "
        "мероприятии Apple.",
    ),
    Draft(
        "tech",
        "Стартовая цена складного iPhone составит $2 000 или выше?",
        "Вопрос имеет смысл, только если устройство вообще представят. Если "
        "анонса не будет, событие разрешается как НЕТ.",
        d(8, 12), d(20, 20),
        "Официальная страница цен Apple США (apple.com)",
        "ДА — если объявленная стартовая цена в США ≥ $1 999. НЕТ — если ниже "
        "или если устройство не представлено.",
    ),
    Draft(
        "tech",
        "Apple публично назовёт Google Gemini в основе обновлённой Siri?",
        "По сообщениям, Apple использует модель Gemini, адаптированную Google "
        "под Apple, и запускает её на собственных серверах Private Cloud "
        "Compute. Компании нередко не раскрывают такие партнёрства публично.",
        d(8, 12), d(20, 20),
        "Официальные материалы Apple: презентация, пресс-релизы, страницы "
        "продуктов",
        "ДА — если Apple в официальных материалах прямо указала Google Gemini "
        "как одну из моделей, на которых работает Siri.",
    ),
    Draft(
        "tech",
        "Apple представит AirPods со встроенной камерой?",
        "Среди ожидаемых новинок сентября упоминались новые AirPods, возможно "
        "с камерами — одна из самых спорных позиций в списке слухов.",
        d(8, 12), d(20, 20),
        "Официальные пресс-релизы Apple (apple.com/newsroom)",
        "ДА — если анонсированы AirPods, в спецификации которых заявлена "
        "камера.",
    ),
    Draft(
        "tech",
        "iPhone 18 Pro выйдет с собственным модемом Apple C1?",
        "Переход с модемов Qualcomm на собственный C1 — один из ключевых "
        "ожидаемых шагов. Ранее Apple уже переносила подобные переходы.",
        d(8, 12), d(25, 20),
        "Официальные технические характеристики Apple и разборы iFixit",
        "ДА — если в iPhone 18 Pro установлен модем Apple собственной "
        "разработки.",
    ),
    # ── Игры ────────────────────────────────────────────────────────────────
    Draft(
        "games",
        "Valheim 1.0 выйдет 9 сентября без переноса?",
        "Выход версии 1.0 после нескольких лет раннего доступа заявлен на "
        "9 сентября 2026 года — самый ожидаемый релиз месяца.",
        d(8, 20), d(11, 20),
        "Страница Valheim в Steam, официальные каналы Iron Gate",
        "ДА — если версия 1.0 стала доступна игрокам 9 сентября 2026 года.",
    ),
    Draft(
        "games",
        "The Blood of Dawnwalker выйдет 3 сентября без переноса?",
        "Тёмное фэнтези от Rebel Wolves — студии выходцев из CD Projekt RED. "
        "Дебютный проект студии.",
        d(2, 20), d(5, 20),
        "Страницы игры в Steam и магазинах консолей",
        "ДА — если игра стала доступна для покупки 3 сентября 2026 года.",
    ),
    Draft(
        "games",
        "The Blood of Dawnwalker получит на Metacritic 80 баллов или выше?",
        "Оценка на платформе PC по агрегированным рецензиям прессы. Дебютные "
        "проекты новых студий редко берут высокую планку сразу.",
        d(2, 20), d(20, 20),
        "Metacritic, страница игры (версия для PC)",
        "ДА — если Metascore ≥ 80 по состоянию на 20 сентября 2026 года.",
    ),
    Draft(
        "games",
        "Пиковый онлайн Valheim в Steam превысит 100 000 игроков после выхода 1.0?",
        "Рекорд игры времён раннего доступа измерялся сотнями тысяч "
        "одновременных игроков, но с тех пор прошло несколько лет.",
        d(8, 20), d(20, 20),
        "SteamDB, показатель peak concurrent players",
        "ДА — если суточный пик превысил 100 000 игроков хотя бы раз с 9 по "
        "20 сентября.",
    ),
    # ── Погода ──────────────────────────────────────────────────────────────
    Draft(
        "weather",
        "В Атлантике сформируется хотя бы один ураган до 30 сентября?",
        "На 6 августа сезон дал два тропических шторма (Arthur и Bertha) и ни "
        "одного урагана. NOAA ждёт 2–6 ураганов за сезон, но прогнозирует "
        "активность ниже нормы: вероятность сильного Эль-Ниньо в августе–"
        "октябре оценивается в 90%.",
        d(29, 12), d(30, 22),
        "National Hurricane Center (nhc.noaa.gov), официальные сводки",
        "ДА — если хотя бы одна система в Атлантическом бассейне достигла "
        "статуса урагана (ветер ≥ 74 миль/ч) до 30 сентября включительно.",
    ),
    Draft(
        "weather",
        "Число названных штормов в Атлантике достигнет семи к 30 сентября?",
        "Сейчас их два. Прогноз NOAA на весь сезон — 7–13 названных штормов, "
        "причём пик приходится на август–октябрь.",
        d(29, 12), d(30, 22),
        "National Hurricane Center, список названных штормов сезона 2026",
        "ДА — если к 30 сентября включительно образовалось семь и более "
        "названных штормов.",
    ),
    Draft(
        "weather",
        "Сформируется ли в Атлантике ураган категории 3 или выше до 30 сентября?",
        "NOAA оценивает вероятное число мощных ураганов за весь сезон в 0–2. "
        "Сильный сдвиг ветра при Эль-Ниньо разрушает системы до того, как они "
        "успевают набрать силу.",
        d(29, 12), d(30, 22),
        "National Hurricane Center, классификация по шкале Саффира-Симпсона",
        "ДА — если хотя бы одна система достигла категории 3 (ветер ≥ 111 "
        "миль/ч) до 30 сентября включительно.",
    ),
    Draft(
        "weather",
        "Хотя бы один ураган выйдет на побережье США до 30 сентября?",
        "Выход на сушу (landfall) в статусе урагана — отдельное событие: "
        "система может достичь силы урагана и не дойти до берега.",
        d(29, 12), d(30, 22),
        "National Hurricane Center, официальные отчёты о выходе на сушу",
        "ДА — если центр системы в статусе урагана пересёк береговую линию "
        "континентальных США до 30 сентября включительно.",
    ),
    # ── Культура ────────────────────────────────────────────────────────────
    Draft(
        "culture",
        "«Эмми» за лучший драматический сериал получит проект стримингового сервиса?",
        "78-я церемония пройдёт 14 сентября в Peacock Theater. Номинации "
        "объявлены 8 июля. Стриминги давно теснят эфирные и кабельные каналы, "
        "но исключения случаются.",
        d(14, 20), d(15, 20),
        "Официальные результаты Television Academy (emmys.com)",
        "ДА — если победитель в категории Outstanding Drama Series выпущен "
        "стриминговым сервисом.",
    ),
    Draft(
        "culture",
        "«The Traitors» победит в категории лучшего реалити-соревнования на «Эмми»?",
        "В номинации также представлены Survivor и RuPaul's Drag Race — "
        "многолетние участники этой категории.",
        d(14, 20), d(15, 20),
        "Официальные результаты Television Academy (emmys.com)",
        "ДА — если победил The Traitors.",
    ),
    Draft(
        "culture",
        "«Золотого льва» Венецианского фестиваля получит фильм режиссёра-женщины?",
        "83-й фестиваль идёт 2–12 сентября на Лидо, международное жюри "
        "возглавляет Мэгги Джилленхол. За всю историю фестиваля главный приз "
        "доставался женщинам-режиссёрам считаное число раз.",
        d(12, 12), d(13, 20),
        "Официальный сайт La Biennale di Venezia (labiennale.org)",
        "ДА — если «Золотой лев» за лучший фильм присуждён картине, "
        "снятой режиссёром-женщиной (в том числе в соавторстве).",
    ),
    Draft(
        "culture",
        "Элизабет Страут попадёт в шортлист Букеровской премии?",
        "Шортлист объявят 22 сентября в Southbank Centre в Лондоне. В лонг-"
        "листе 2026 года — Дуглас Стюарт, Марлон Джеймс, Элизабет Страут и "
        "несколько дебютантов.",
        d(22, 15), d(23, 20),
        "Официальный сайт The Booker Prizes (thebookerprizes.com)",
        "ДА — если роман Элизабет Страут вошёл в шортлист из шести книг.",
    ),
    Draft(
        "culture",
        "Джордж Клуни лично получит «Золотого льва» за карьеру в Венеции?",
        "Награда за вклад в кинематограф объявлена заранее; вопрос в том, "
        "приедет ли актёр на церемонию — лауреаты иногда получают приз "
        "заочно.",
        d(11, 12), d(13, 20),
        "Официальные материалы и трансляции La Biennale di Venezia",
        "ДА — если Клуни присутствовал на церемонии лично.",
    ),
    # ── Искусство ───────────────────────────────────────────────────────────
    Draft(
        "art",
        "Выставка Тёрнеровской премии откроется в MIMA 26 сентября без переноса?",
        "Впервые выставка премии проходит в университетском пространстве — "
        "Middlesbrough Institute of Modern Art. В шортлисте 2026 года: Simeon "
        "Barclay, Kira Freije, Marguerite Humeau и Tanoa Sasraku.",
        d(25, 12), d(28, 20),
        "Официальные страницы Tate и MIMA",
        "ДА — если выставка открылась для публики 26 сентября 2026 года.",
    ),
    Draft(
        "art",
        "На Frieze Seoul объявят публично о продаже дороже $5 млн?",
        "Пятая ярмарка Frieze Seoul проходит 2–5 сентября в COEX: более 125 "
        "галерей из 30 стран, свыше 70% участников — из Азиатско-"
        "Тихоокеанского региона. Галереи не всегда раскрывают суммы сделок.",
        d(4, 12), d(10, 20),
        "Отчёты о продажах Artnet News, The Art Newspaper, Artsy",
        "ДА — если публично сообщено хотя бы об одной сделке на ярмарке "
        "стоимостью $5 млн и выше.",
    ),
    Draft(
        "art",
        "До 30 сентября будет объявлен новый аукционный рекорд для ныне живущего художника?",
        "Первое полугодие 2026 года арт-рынок закрыл ростом: продажи Christie's, "
        "Sotheby's и Phillips выросли на 70% год к году, до $6,8 млрд с учётом "
        "комиссий. Осенний сезон торгов только начинается.",
        d(29, 12), d(30, 20),
        "Официальные результаты торгов Christie's, Sotheby's, Phillips",
        "ДА — если установлен и объявлен новый мировой аукционный рекорд для "
        "работы ныне живущего художника.",
    ),
    Draft(
        "art",
        "Крупный западный музей объявит о возврате артефактов стране происхождения до 30 сентября?",
        "Реституция музейных коллекций — устойчивый тренд последних лет, но "
        "конкретные решения принимаются нерегулярно и часто затягиваются.",
        d(29, 12), d(30, 20),
        "Официальные заявления музеев, сообщения The Art Newspaper",
        "ДА — если музей из списка крупнейших (Британский музей, Лувр, "
        "Метрополитен, Рейксмузеум, Гумбольдт-форум и др.) официально объявил "
        "о возврате предметов коллекции.",
    ),
    # ── Вне сезона: концерт позже финализации Q3 ────────────────────────────
    Draft(
        "culture",
        "Состоится ли концерт Канье Уэста в Петербурге?",
        "17 августа анонсированы два концерта на «Газпром Арене» — 10 и 11 "
        "октября 2026 года. Билеты продаются на yerussia2026.ru: от 9 000 ₽, "
        "танцпол 25 000 ₽, VIP-ложи до 160 000 ₽. Вместе с артистом в город "
        "должен приехать его музыкальный куратор Андре Траутман. Исход не "
        "предрешён: у артиста долгая история отмен и переносов концертов.",
        d(10, 12, month=10), d(12, 12, month=10),
        "Официальные сообщения организатора и «Газпром Арены», подтверждение "
        "в федеральных СМИ (ТАСС, «Интерфакс», «Фонтанка»)",
        "ДА — если хотя бы один из двух заявленных концертов (10 или 11 "
        "октября) фактически состоялся. НЕТ — если оба не состоялись в "
        "заявленные даты: отменены или перенесены.",
        season_slug="2026-q4",
    ),
]


# Запас между разрешением последнего события и концом сезона. Воркер
# финализирует сезон ровно в ``ends_at``, а исход надо успеть зафиксировать
# руками и прогнать скоринг — иначе прогнозы по такому событию не попадут в
# сезонный зачёт вообще.
FINALIZE_MARGIN_HOURS = 24


async def fix_window(
    session: AsyncSession, *, actor: Actor, season: SeasonORM, apply: bool
) -> int:
    """Отодвигает конец сезона за последнее разрешающееся событие.

    Двигать окно самих событий нельзя: домен запрещает правку после
    публикации (``EventEditNotAllowedError``), и это верно — условия
    зафиксированы для всех, кто уже дал прогноз. Поэтому сдвигается граница
    сезона, пока он ещё ``upcoming``.

    Запас нужен не только на сам исход: администратор должен успеть
    зафиксировать его вручную и прогнать скоринг до авто-финализации.
    """
    last = (
        await session.execute(select(func.max(EventORM.resolves_at)))
    ).scalar_one_or_none()
    if last is None:
        print("Событий нет — двигать нечего.")
        return 0

    need = last + timedelta(hours=FINALIZE_MARGIN_HOURS)
    print(f"\nПоследнее событие разрешается: {last:%d.%m %H:%M UTC}")
    print(f"Сезон завершается:               {season.ends_at:%d.%m %H:%M UTC}")

    if season.ends_at >= need:
        print("Запаса достаточно — правка не нужна.")
        return 0

    print(f"Нужно отодвинуть до:             {need:%d.%m %H:%M UTC} "
          f"(+{FINALIZE_MARGIN_HOURS} ч на фиксацию исхода и скоринг)")

    if not apply:
        print("\nСухой прогон — ничего не изменено. Для правки добавьте --apply.")
        return 0

    updated = await UpdateSeason(
        repo=SqlAlchemySeasonRepository(session), clock=SeasonSystemClock()
    ).execute(season_id=season.id, actor_role=actor.role, ends_at=need)
    print(f"\nСезон «{updated.title}» теперь завершается {updated.ends_at:%d.%m %H:%M UTC}")
    return 0


async def requeue_late(
    session: AsyncSession, *, actor: Actor, season: SeasonORM, apply: bool
) -> int:
    """Пересоздаёт события, разрешающиеся после конца сезона.

    Почему не правкой: домен запрещает менять окно опубликованного события
    (``EventEditNotAllowedError``) — условия зафиксированы для всех, кто уже
    дал прогноз. Двигать конец сезона тоже нельзя: он уже активирован, а
    ``LeagueConfig`` и границы публичного конкурса после активации неизменны
    (ст. 1058 ГК).

    Остаётся легитимный путь: отменить событие и завести заново с окном внутри
    сезона. Безопасно, только пока по нему нет прогнозов, — это проверяется.
    Даты в текстах переписываются вместе с окном, иначе критерий разрешения
    начнёт противоречить сроку.
    """
    margin = timedelta(hours=FINALIZE_MARGIN_HOURS)
    deadline = season.ends_at - margin
    new_closes = datetime(2026, 9, 28, 18, tzinfo=UTC)
    new_resolves = datetime(2026, 9, 29, 12, tzinfo=UTC)

    rows = (
        await session.execute(
            select(EventORM)
            .where(EventORM.resolves_at > deadline)
            .order_by(EventORM.title)
        )
    ).scalars().all()

    print(f"\nСезон завершается {season.ends_at:%d.%m %H:%M UTC}; "
          f"с запасом {FINALIZE_MARGIN_HOURS} ч дедлайн разрешения — "
          f"{deadline:%d.%m %H:%M UTC}")
    print(f"Событий за дедлайном: {len(rows)}")
    if not rows:
        return 0

    with_predictions = {
        row[0]
        for row in (
            await session.execute(
                text(
                    "SELECT DISTINCT event_id FROM predictions "
                    "WHERE event_id = ANY(:ids)"
                ),
                {"ids": [r.id for r in rows]},
            )
        ).all()
    }
    blocked = [r for r in rows if r.id in with_predictions]
    movable = [r for r in rows if r.id not in with_predictions]

    for r in movable:
        print(f"  · {r.title[:68]}")
    for r in blocked:
        print(f"  ! пропуск (есть прогнозы): {r.title[:50]}")

    if not apply:
        print("\nСухой прогон — ничего не изменено. Для правки добавьте --apply.")
        return 0

    def swap(value: str) -> str:
        """Срок в тексте должен совпасть с новым окном, иначе критерий врёт."""
        return value.replace("30 сентября", "28 сентября")

    cancel = CancelEvent(
        events=SqlAlchemyEventRepository(session),
        clock=SystemClock(),
        audit=SqlAlchemyAuditTrail(session),
    )
    create = CreateEvent(
        events=SqlAlchemyEventRepository(session),
        categories=SqlAlchemyCategoryRepository(session),
        clock=SystemClock(),
        audit=SqlAlchemyAuditTrail(session),
    )
    publish = PublishEvent(
        events=SqlAlchemyEventRepository(session),
        clock=SystemClock(),
        audit=SqlAlchemyAuditTrail(session),
    )

    now = datetime.now(UTC)
    for row in movable:
        await cancel.execute(actor=actor, event_id=row.id)
        fresh = await create.execute(
            actor=actor,
            data=NewEventInput(
                title=swap(row.title),
                description=swap(row.description),
                category_id=row.category_id,
                season_id=row.season_id,
                opens_at=now,
                closes_at=new_closes,
                resolves_at=new_resolves,
                resolution_source=row.resolution_source,
                resolution_criteria=swap(row.resolution_criteria),
            ),
        )
        await publish.execute(actor=actor, event_id=fresh.id)
        print(f"  ✓ {swap(row.title)[:68]}")

    print(f"\nПересоздано событий: {len(movable)}"
          + (f", пропущено с прогнозами: {len(blocked)}" if blocked else ""))
    return 0


@asynccontextmanager
async def _session(database_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-file", required=True)
    parser.add_argument("--season-slug", default="2026-q3")
    parser.add_argument(
        "--author",
        default="andrey",
        help="username автора событий (нужна роль editor/admin)",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="сразу опубликовать (draft → open); иначе останутся черновиками",
    )
    parser.add_argument(
        "--requeue-late",
        action="store_true",
        help="пересоздать события, разрешающиеся после конца сезона",
    )
    parser.add_argument(
        "--fix-window",
        action="store_true",
        help=(
            "перенести события, разрешающиеся после конца сезона, внутрь окна "
            "(правит окно и тексты уже созданных событий)"
        ),
    )
    parser.add_argument(
        "--recreate",
        metavar="ПОДСТРОКА",
        help=(
            "отменить события с этой подстрокой в заголовке и завести заново "
            "из DRAFTS. Нужно, когда меняется сезон или окно: после публикации "
            "домен их править запрещает"
        ),
    )
    parser.add_argument("--apply", action="store_true", help="записать в БД")
    args = parser.parse_args()

    url = Path(args.database_url_file).read_text().strip()

    async with _session(url) as session:
        categories = SqlAlchemyCategoryRepository(session)
        by_slug = {c.slug: c for c in await categories.list_all()}
        missing = sorted({dr.category for dr in DRAFTS} - by_slug.keys())
        if missing:
            print(f"ОТКАЗ: нет категорий: {', '.join(missing)}", file=sys.stderr)
            return 2

        season = (
            await session.execute(
                select(SeasonORM).where(SeasonORM.slug == args.season_slug)
            )
        ).scalar_one_or_none()
        if season is None:
            print(f"ОТКАЗ: сезон «{args.season_slug}» не найден", file=sys.stderr)
            return 2

        author = (
            await session.execute(
                select(UserORM).where(UserORM.username == args.author)
            )
        ).scalar_one_or_none()
        if author is None:
            print(f"ОТКАЗ: пользователь @{args.author} не найден", file=sys.stderr)
            return 2
        actor = Actor(user_id=author.id, role=UserRole(author.role))

        if args.fix_window:
            return await fix_window(session, actor=actor, season=season, apply=args.apply)
        if args.requeue_late:
            return await requeue_late(
                session, actor=actor, season=season, apply=args.apply
            )

        # Уже заведённые заголовки — чтобы повторный запуск не задваивал.
        existing = {
            row[0]
            for row in (await session.execute(text("SELECT title FROM events"))).all()
        }

        if args.recreate:
            doomed = (
                await session.execute(
                    select(EventORM).where(EventORM.title.contains(args.recreate))
                )
            ).scalars().all()
            with_predictions = {
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT DISTINCT event_id FROM predictions "
                            "WHERE event_id = ANY(:ids)"
                        ),
                        {"ids": [e.id for e in doomed]},
                    )
                ).all()
            }
            print(f"\nК пересозданию по «{args.recreate}»: {len(doomed)}")
            for e in doomed:
                mark = " ! есть прогнозы — пропуск" if e.id in with_predictions else ""
                print(f"  · [{e.status.value}] {e.title[:60]}{mark}")
            movable = [e for e in doomed if e.id not in with_predictions]
            if args.apply:
                cancel = CancelEvent(
                    events=SqlAlchemyEventRepository(session),
                    clock=SystemClock(),
                    audit=SqlAlchemyAuditTrail(session),
                )
                for e in movable:
                    if e.status.value not in {"cancelled", "annulled"}:
                        await cancel.execute(actor=actor, event_id=e.id)
                # Освобождаем заголовки, чтобы они не считались дублями ниже.
                existing -= {e.title for e in movable}

        planned = [dr for dr in DRAFTS if dr.title not in existing]
        skipped = len(DRAFTS) - len(planned)

        per_cat: dict[str, int] = {}
        for dr in planned:
            per_cat[dr.category] = per_cat.get(dr.category, 0) + 1

        print(f"\nСезон: {season.title} ({season.slug})")
        print(f"Автор: @{author.username} ({author.role})")
        print(f"К созданию: {len(planned)}" + (f", пропущено дублей: {skipped}" if skipped else ""))
        for slug in sorted(per_cat):
            print(f"  {by_slug[slug].title.ljust(14)} {per_cat[slug]}")

        if not args.apply:
            print("\nСухой прогон — ничего не записано. Для записи добавьте --apply.")
            return 0

        # Карта slug → id: события могут ссылаться на разные сезоны, если их
        # исход приходится за окном основного.
        needed = {dr.season_slug for dr in planned if dr.season_slug} | {
            args.season_slug
        }
        season_ids: dict[str, uuid.UUID] = {}
        for slug in sorted(needed):
            row = (
                await session.execute(
                    select(SeasonORM).where(SeasonORM.slug == slug)
                )
            ).scalar_one_or_none()
            if row is None:
                print(f"ОТКАЗ: сезон «{slug}» не найден", file=sys.stderr)
                return 2
            season_ids[slug] = row.id

        create = CreateEvent(
            events=SqlAlchemyEventRepository(session),
            categories=categories,
            clock=SystemClock(),
            audit=SqlAlchemyAuditTrail(session),
        )
        publish = PublishEvent(
            events=SqlAlchemyEventRepository(session),
            clock=SystemClock(),
            audit=SqlAlchemyAuditTrail(session),
        )

        now = datetime.now(UTC)
        created = 0
        for dr in planned:
            event = await create.execute(
                actor=actor,
                data=NewEventInput(
                    title=dr.title,
                    description=dr.description,
                    category_id=by_slug[dr.category].id,
                    season_id=season_ids[dr.season_slug or args.season_slug],
                    # Приём открыт с момента заведения: события уже в повестке.
                    opens_at=now,
                    closes_at=dr.closes,
                    resolves_at=dr.resolves,
                    resolution_source=dr.source,
                    resolution_criteria=dr.criteria,
                ),
            )
            if args.publish:
                await publish.execute(actor=actor, event_id=event.id)
            created += 1
            print(f"  ✓ [{dr.category}] {dr.title[:64]}")

        print(f"\nСоздано событий: {created}" + (" (опубликованы)" if args.publish else " (черновики)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
