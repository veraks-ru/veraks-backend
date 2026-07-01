"""Composition root модуля social."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_session
from app.modules.notifications.adapters.emitter import PushingNotificationEmitter
from app.modules.notifications.adapters.goctopus import GoctopusPusher
from app.modules.notifications.adapters.repository import (
    SqlAlchemyNotificationRepository,
)
from app.modules.social.adapters.clock import SystemClock
from app.modules.social.adapters.event_gateway import (
    SqlAlchemyEventExistsGateway,
)
from app.modules.social.adapters.feed_gateway import SqlAlchemyFeedGateway
from app.modules.social.adapters.repository import (
    SqlAlchemyCommentRepository,
    SqlAlchemyFollowRepository,
)
from app.modules.social.adapters.user_lookup import SqlAlchemyUserLookup
from app.modules.social.application.use_cases import (
    DeleteComment,
    FollowUser,
    GetFeed,
    GetSocialStats,
    ListEventComments,
    ListFollowers,
    ListFollowing,
    PostComment,
    UnfollowUser,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _notifier(session: AsyncSession, settings: Settings) -> PushingNotificationEmitter:
    return PushingNotificationEmitter(
        SqlAlchemyNotificationRepository(session),
        GoctopusPusher(settings.realtime),
    )


def get_post_comment(session: SessionDep, settings: SettingsDep) -> PostComment:
    return PostComment(
        comments=SqlAlchemyCommentRepository(session),
        events=SqlAlchemyEventExistsGateway(session),
        clock=SystemClock(),
        notifier=_notifier(session, settings),
    )


def get_delete_comment(session: SessionDep) -> DeleteComment:
    return DeleteComment(
        comments=SqlAlchemyCommentRepository(session), clock=SystemClock()
    )


def get_list_event_comments(session: SessionDep) -> ListEventComments:
    return ListEventComments(
        comments=SqlAlchemyCommentRepository(session),
        users=SqlAlchemyUserLookup(session),
    )


def get_follow_user(session: SessionDep, settings: SettingsDep) -> FollowUser:
    return FollowUser(
        follows=SqlAlchemyFollowRepository(session),
        users=SqlAlchemyUserLookup(session),
        notifier=_notifier(session, settings),
    )


def get_unfollow_user(session: SessionDep) -> UnfollowUser:
    return UnfollowUser(
        follows=SqlAlchemyFollowRepository(session),
        users=SqlAlchemyUserLookup(session),
    )


def get_social_stats(session: SessionDep) -> GetSocialStats:
    return GetSocialStats(
        follows=SqlAlchemyFollowRepository(session),
        users=SqlAlchemyUserLookup(session),
    )


def get_list_following(session: SessionDep) -> ListFollowing:
    return ListFollowing(
        follows=SqlAlchemyFollowRepository(session),
        users=SqlAlchemyUserLookup(session),
    )


def get_list_followers(session: SessionDep) -> ListFollowers:
    return ListFollowers(
        follows=SqlAlchemyFollowRepository(session),
        users=SqlAlchemyUserLookup(session),
    )


def get_feed(session: SessionDep) -> GetFeed:
    return GetFeed(
        follows=SqlAlchemyFollowRepository(session),
        feed=SqlAlchemyFeedGateway(session),
    )
