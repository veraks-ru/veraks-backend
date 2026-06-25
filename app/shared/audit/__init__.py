"""Общая инфраструктура неизменяемого аудита (tamper-evident hash-цепочка).

Вводится доменом resolutions, но спроектирована как кросс-доменная: любой
домен монолита может писать значимые изменения состояния через порт
:class:`~app.shared.audit.ports.audit_trail.AuditTrail`. Журнал ``audit_log``
append-only (схемный триггер блокирует UPDATE/DELETE), записи связаны
хеш-цепочкой ``hash = H(prev_hash ‖ canonical(payload))``.
"""
