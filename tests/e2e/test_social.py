"""E2E соцфич против реального Postgres: комментарии, подписки, лента."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.entities import UserRole
from app.modules.predictions.adapters.repository import (
    SqlAlchemyPredictionRepository,
)
from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction
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
    PostComment,
    UnfollowUser,
)
from app.modules.social.domain.errors import (
    CommentForbiddenError,
    SelfFollowError,
)
from tests.e2e.helpers import (
    OPENS_AT,
    add_active_season,
    add_category,
    add_open_event,
    add_user,
)

pytestmark = pytest.mark.asyncio


def _post_uc(session):  # noqa: ANN001
    return PostComment(
        comments=SqlAlchemyCommentRepository(session),
        events=SqlAlchemyEventExistsGateway(session),
        clock=SystemClock(),
    )


def _list_uc(session):  # noqa: ANN001
    return ListEventComments(
        comments=SqlAlchemyCommentRepository(session),
        users=SqlAlchemyUserLookup(session),
    )


async def _open_event(session, admin_id):  # noqa: ANN001
    category = await add_category(session)
    season = await add_active_season(session)
    await session.flush()
    return await add_open_event(
        session, category_id=category.id, created_by=admin_id, season_id=season.id
    )


async def test_comment_post_list_and_delete(session: AsyncSession) -> None:
    admin = await add_user(session, username="mod1", role=UserRole.ADMIN)
    author = await add_user(session, username="author1")
    event = await _open_event(session, admin.id)

    comment = await _post_uc(session).execute(
        event_id=event.id, author_id=author.id, body="  Интересное событие  "
    )
    assert comment.body == "Интересное событие"  # trimmed

    views = await _list_uc(session).execute(event_id=event.id)
    assert len(views) == 1
    assert views[0].author is not None
    assert views[0].author.username == "author1"

    # Автор удаляет свой комментарий → исчезает из выдачи.
    await DeleteComment(
        comments=SqlAlchemyCommentRepository(session), clock=SystemClock()
    ).execute(comment_id=comment.id, actor_id=author.id, actor_role=UserRole.USER)
    assert await _list_uc(session).execute(event_id=event.id) == []
    await session.commit()


async def test_comment_delete_forbidden_for_stranger(
    session: AsyncSession,
) -> None:
    admin = await add_user(session, username="mod2", role=UserRole.ADMIN)
    author = await add_user(session, username="author2")
    stranger = await add_user(session, username="stranger2")
    event = await _open_event(session, admin.id)
    comment = await _post_uc(session).execute(
        event_id=event.id, author_id=author.id, body="моё"
    )

    # Посторонний не может удалить.
    with pytest.raises(CommentForbiddenError):
        await DeleteComment(
            comments=SqlAlchemyCommentRepository(session), clock=SystemClock()
        ).execute(
            comment_id=comment.id, actor_id=stranger.id, actor_role=UserRole.USER
        )
    # Модератор — может.
    await DeleteComment(
        comments=SqlAlchemyCommentRepository(session), clock=SystemClock()
    ).execute(comment_id=comment.id, actor_id=admin.id, actor_role=UserRole.ADMIN)
    assert await _list_uc(session).execute(event_id=event.id) == []
    await session.commit()


async def test_follow_unfollow_and_stats(session: AsyncSession) -> None:
    a = await add_user(session, username="alice")
    await add_user(session, username="bob")
    await session.flush()

    follows = SqlAlchemyFollowRepository(session)
    users = SqlAlchemyUserLookup(session)

    # Нельзя подписаться на себя.
    with pytest.raises(SelfFollowError):
        await FollowUser(follows=follows, users=users).execute(
            follower_id=a.id, username="alice"
        )

    await FollowUser(follows=follows, users=users).execute(
        follower_id=a.id, username="bob"
    )
    # Идемпотентно.
    await FollowUser(follows=follows, users=users).execute(
        follower_id=a.id, username="bob"
    )

    stats = await GetSocialStats(follows=follows, users=users).execute(
        username="bob", viewer_id=a.id
    )
    assert stats.followers == 1
    assert stats.is_following is True

    removed = await UnfollowUser(follows=follows, users=users).execute(
        follower_id=a.id, username="bob"
    )
    assert removed is True
    stats2 = await GetSocialStats(follows=follows, users=users).execute(
        username="bob", viewer_id=a.id
    )
    assert stats2.followers == 0
    assert stats2.is_following is False
    await session.commit()


async def test_feed_shows_followee_activity(session: AsyncSession) -> None:
    admin = await add_user(session, username="mod3", role=UserRole.ADMIN)
    a = await add_user(session, username="alice3")
    b = await add_user(session, username="bob3")
    event = await _open_event(session, admin.id)

    follows = SqlAlchemyFollowRepository(session)
    users = SqlAlchemyUserLookup(session)
    await FollowUser(follows=follows, users=users).execute(
        follower_id=a.id, username="bob3"
    )

    # B комментирует событие.
    await _post_uc(session).execute(
        event_id=event.id, author_id=b.id, body="ставлю на ДА"
    )
    # B имеет засчитанный прогноз (проставим brier напрямую).
    pred = await SqlAlchemyPredictionRepository(session).add(
        Prediction.place(
            user_id=b.id,
            event_id=event.id,
            grade=ConfidenceGrade.PROBABLY_YES,
            now=OPENS_AT + timedelta(days=1),
        )
    )
    await session.execute(
        text(
            "UPDATE predictions SET brier_score = 0.09000, scored_at = now() "
            "WHERE id = :pid"
        ),
        {"pid": str(pred.id)},
    )
    await session.flush()

    feed = await GetFeed(
        follows=follows, feed=SqlAlchemyFeedGateway(session)
    ).execute(user_id=a.id, limit=50)

    kinds = {it.kind for it in feed}
    assert kinds == {"comment", "score"}
    assert all(it.actor_username == "bob3" for it in feed)
    await session.commit()


async def test_feed_pagination(session: AsyncSession) -> None:
    admin = await add_user(session, username="mod_pg", role=UserRole.ADMIN)
    a = await add_user(session, username="alice_pg")
    b = await add_user(session, username="bob_pg")
    event = await _open_event(session, admin.id)
    follows = SqlAlchemyFollowRepository(session)
    users = SqlAlchemyUserLookup(session)
    await FollowUser(follows=follows, users=users).execute(
        follower_id=a.id, username="bob_pg"
    )
    for i in range(5):
        await _post_uc(session).execute(
            event_id=event.id, author_id=b.id, body=f"коммент {i}"
        )
    await session.flush()

    feed = GetFeed(follows=follows, feed=SqlAlchemyFeedGateway(session))
    page1 = await feed.execute(user_id=a.id, limit=2, offset=0)
    page2 = await feed.execute(user_id=a.id, limit=2, offset=2)
    page3 = await feed.execute(user_id=a.id, limit=2, offset=4)
    assert len(page1) == 2 and len(page2) == 2 and len(page3) == 1
    bodies = {it.body for it in page1 + page2 + page3}
    assert len(bodies) == 5  # без дублей между страницами
    await session.commit()


async def test_notifications_fire_on_follow_and_comment(
    session: AsyncSession,
) -> None:
    from app.modules.notifications.adapters.emitter import DbNotificationEmitter
    from app.modules.notifications.adapters.repository import (
        SqlAlchemyNotificationRepository,
    )

    admin = await add_user(session, username="mod_nt", role=UserRole.ADMIN)
    author = await add_user(session, username="author_nt")
    fan = await add_user(session, username="fan_nt")
    event = await _open_event(session, admin.id)
    notifier = DbNotificationEmitter(SqlAlchemyNotificationRepository(session))

    # Комментарий постороннего → уведомление автору события (admin).
    await PostComment(
        comments=SqlAlchemyCommentRepository(session),
        events=SqlAlchemyEventExistsGateway(session),
        clock=SystemClock(),
        notifier=notifier,
    ).execute(event_id=event.id, author_id=author.id, body="ну как вам?")

    # Подписка → уведомление тому, на кого подписались.
    await FollowUser(
        follows=SqlAlchemyFollowRepository(session),
        users=SqlAlchemyUserLookup(session),
        notifier=notifier,
    ).execute(follower_id=fan.id, username="author_nt")
    await session.flush()

    repo = SqlAlchemyNotificationRepository(session)
    admin_notes = await repo.list_for_user(admin.id)
    author_notes = await repo.list_for_user(author.id)
    assert any(n.kind == "comment.created" for n in admin_notes)
    assert any(n.kind == "follow.created" for n in author_notes)
    await session.commit()
