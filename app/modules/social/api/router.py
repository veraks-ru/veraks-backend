"""Роутер соцфич: комментарии, подписки, лента."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.modules.identity.api.dependencies import CurrentUser
from app.modules.social.api.dependencies import (
    get_delete_comment,
    get_feed,
    get_follow_user,
    get_list_event_comments,
    get_list_followers,
    get_list_following,
    get_post_comment,
    get_social_stats,
    get_unfollow_user,
)
from app.modules.social.api.schemas import (
    AuthorRef,
    CommentCreateRequest,
    CommentResponse,
    FeedItemResponse,
    SocialStatsResponse,
    UserRefResponse,
)
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

router = APIRouter(tags=["social"])


# ── Комментарии ─────────────────────────────────────────────────────────────


@router.post(
    "/events/{event_id}/comments",
    response_model=CommentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Оставить комментарий к событию",
)
async def post_comment(
    event_id: uuid.UUID,
    payload: CommentCreateRequest,
    current_user: CurrentUser,
    uc: Annotated[PostComment, Depends(get_post_comment)],
) -> CommentResponse:
    comment = await uc.execute(
        event_id=event_id, author_id=current_user.id, body=payload.body
    )
    return CommentResponse(
        id=comment.id,
        event_id=comment.event_id,
        body=comment.body,
        created_at=comment.created_at,
        author=AuthorRef(
            user_id=current_user.id,
            username=current_user.username,
            display_name=current_user.display_name,
        ),
    )


@router.get(
    "/events/{event_id}/comments",
    response_model=list[CommentResponse],
    summary="Комментарии события",
)
async def list_comments(
    event_id: uuid.UUID,
    uc: Annotated[ListEventComments, Depends(get_list_event_comments)],
) -> list[CommentResponse]:
    views = await uc.execute(event_id=event_id)
    return [CommentResponse.from_view(v) for v in views]


@router.delete(
    "/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить комментарий (автор или модератор)",
)
async def delete_comment(
    comment_id: uuid.UUID,
    current_user: CurrentUser,
    uc: Annotated[DeleteComment, Depends(get_delete_comment)],
) -> None:
    await uc.execute(
        comment_id=comment_id,
        actor_id=current_user.id,
        actor_role=current_user.role,
    )


# ── Подписки ────────────────────────────────────────────────────────────────


@router.post(
    "/users/{username}/follow",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Подписаться на предсказателя",
)
async def follow(
    username: str,
    current_user: CurrentUser,
    uc: Annotated[FollowUser, Depends(get_follow_user)],
) -> None:
    await uc.execute(follower_id=current_user.id, username=username)


@router.delete(
    "/users/{username}/follow",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отписаться",
)
async def unfollow(
    username: str,
    current_user: CurrentUser,
    uc: Annotated[UnfollowUser, Depends(get_unfollow_user)],
) -> None:
    await uc.execute(follower_id=current_user.id, username=username)


@router.get(
    "/users/{username}/social",
    response_model=SocialStatsResponse,
    summary="Счётчики подписок пользователя",
)
async def social_stats(
    username: str,
    uc: Annotated[GetSocialStats, Depends(get_social_stats)],
) -> SocialStatsResponse:
    return SocialStatsResponse.from_stats(await uc.execute(username=username))


@router.get(
    "/users/me/following",
    response_model=list[UserRefResponse],
    summary="Кого я читаю",
)
async def my_following(
    current_user: CurrentUser,
    uc: Annotated[ListFollowing, Depends(get_list_following)],
) -> list[UserRefResponse]:
    refs = await uc.execute(user_id=current_user.id)
    return [UserRefResponse.from_ref(r) for r in refs]


@router.get(
    "/users/me/followers",
    response_model=list[UserRefResponse],
    summary="Мои читатели",
)
async def my_followers(
    current_user: CurrentUser,
    uc: Annotated[ListFollowers, Depends(get_list_followers)],
) -> list[UserRefResponse]:
    refs = await uc.execute(user_id=current_user.id)
    return [UserRefResponse.from_ref(r) for r in refs]


# ── Лента ───────────────────────────────────────────────────────────────────


@router.get(
    "/feed",
    response_model=list[FeedItemResponse],
    summary="Персональная лента отслеживаемых предсказателей",
)
async def feed(
    current_user: CurrentUser,
    uc: Annotated[GetFeed, Depends(get_feed)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[FeedItemResponse]:
    items = await uc.execute(user_id=current_user.id, limit=limit)
    return [FeedItemResponse.from_item(it) for it in items]
