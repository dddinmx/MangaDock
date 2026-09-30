# -*- coding: utf-8 -*-
"""Single enqueue path for comic/novel downloads.

Web forms and /api/v1 adapters translate EnqueueResult into redirects or JSON.
HTTP status and copy stay in the adapters when the two surfaces already diverge
(group ACL is 403 on the form, 404 on the JSON API).
"""
from dataclasses import dataclass

from mangadock.services.adult_content import is_adult_content_enabled_for
from mangadock.services.download import (
    is_adult_content_blocked,
    is_supported_comic_url,
    normalize_target_input,
)
from mangadock.services.fanqie import classify_fanqie_target
from mangadock.services.fanqie_api import FanqieApiError
from mangadock.services.groups import can_user_access_group, get_comic_group_map, normalize_group_name
from mangadock.services.library import load_comic_mapping
from mangadock.services.tasks import normalize_comic_url_identity
from mangadock.services.workers import start_download_task, start_novel_task
from mangadock.settings import ADULT_CONTENT_DISABLED_MESSAGE


@dataclass(frozen=True)
class EnqueueResult:
    ok: bool
    code: str
    message: str
    task_id: str | None = None
    media_kind: str | None = None
    reused: bool = False
    fanqie_target: dict | None = None
    http_status: int = 400


def _unsupported_message(user):
    extra = '、MXS、卡拉漫画' if is_adult_content_enabled_for(user) else ''
    return (
        '请输入有效的漫画或小说链接/ID（支持包子漫画、漫画柜、嬉皮漫畫、'
        '番茄图片漫画、番茄小说' + extra + '）'
    )


def _mapped_names(url, mapping):
    normalized = normalize_comic_url_identity(url)
    return [
        comic_name
        for comic_name, mapped_url in mapping.items()
        if normalize_comic_url_identity(mapped_url) == normalized
    ]


def _blocked_by_group(user, names, group_map):
    return any(
        not can_user_access_group(
            normalize_group_name(group_map.get(comic_name)) or '默认分组',
            user,
        )
        for comic_name in names
    )


def enqueue_user_download(user, raw_url, comic_format=2):
    """Validate the pasted target and queue a comic or Fanqie novel download."""
    comic_url = normalize_target_input(raw_url)
    if not is_supported_comic_url(comic_url):
        return EnqueueResult(False, 'UNSUPPORTED_URL', _unsupported_message(user))

    mapping = load_comic_mapping()
    group_map = get_comic_group_map()
    if _blocked_by_group(user, _mapped_names(comic_url, mapping), group_map):
        return EnqueueResult(False, 'FORBIDDEN', '当前账号无权访问该漫画', http_status=403)

    if is_adult_content_blocked(comic_url, allow_adult=is_adult_content_enabled_for(user)):
        return EnqueueResult(
            False, 'ADULT_CONTENT_DISABLED', ADULT_CONTENT_DISABLED_MESSAGE, http_status=403,
        )

    try:
        fanqie_target = classify_fanqie_target(comic_url)
    except FanqieApiError as exc:
        return EnqueueResult(
            False, 'FANQIE_TARGET_INVALID', str(exc) or '番茄作品解析失败',
        )
    except ValueError as exc:
        return EnqueueResult(False, 'UNSUPPORTED_URL', str(exc) or _unsupported_message(user))

    if fanqie_target and fanqie_target['kind'] == 'comic':
        identity = f"fanqie-comic://{fanqie_target['book_id']}"
        if _blocked_by_group(user, _mapped_names(identity, mapping), group_map):
            return EnqueueResult(False, 'FORBIDDEN', '当前账号无权访问该漫画', http_status=403)

    if fanqie_target and fanqie_target['kind'] == 'novel':
        task_id, reused = start_novel_task(
            fanqie_target['book_id'],
            title=fanqie_target['title'],
            created_by_user_id=user.id if user else None,
        )
        if not task_id:
            return EnqueueResult(
                True,
                'NOVEL_IN_PROGRESS',
                '这本小说已在下载队列中，完成后会出现在小说书架',
                media_kind='novel',
                reused=True,
                fanqie_target=fanqie_target,
                http_status=200,
            )
        return EnqueueResult(
            True,
            'NOVEL_QUEUED',
            '',
            task_id=task_id,
            media_kind='novel',
            reused=reused,
            fanqie_target=fanqie_target,
            http_status=200 if reused else 201,
        )

    try:
        task_id = start_download_task(
            comic_url,
            comic_format,
            allow_adult=bool(user and getattr(user, 'can_view_adult', False)),
            created_by_user_id=user.id if user else None,
        )
    except PermissionError:
        return EnqueueResult(False, 'FORBIDDEN', '当前账号无权访问该漫画', http_status=403)

    return EnqueueResult(
        True, 'COMIC_QUEUED', '', task_id=task_id, media_kind='comic', http_status=201,
    )
