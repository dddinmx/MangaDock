# -*- coding: utf-8 -*-
"""Fanqie integration backed exclusively by the private resource API."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mangadock.services.fanqie_api import FanqieApiError, get_client
from mangadock.services.tasks import get_task, update_task
from mangadock.settings import FANQIE_API_MAX_POLL_SECONDS, FANQIE_API_POLL_INTERVAL, NOVEL_ROOT, china_tz
from mangadock.utils.media import sanitize_filename


FANQIE_TASK_PREFIX = "fanqie://"
BOOK_ID_RE = re.compile(r"\d{8,24}")


class FanqieTaskCancelled(RuntimeError):
    pass


def fanqie_task_url(book_id: str) -> str:
    return f"{FANQIE_TASK_PREFIX}{validate_book_id(book_id)}"


def validate_book_id(value: str) -> str:
    raw = str(value or "").strip()
    if BOOK_ID_RE.fullmatch(raw):
        return raw
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlparse(raw)
        except ValueError:
            parsed = None
        if parsed:
            host = (parsed.hostname or "").lower()
            if host == "fanqienovel.com" or host.endswith(".fanqienovel.com"):
                match = re.search(r"/(?:page|reader)/(\d{8,24})(?:/|$)", parsed.path)
                if match:
                    return match.group(1)
                query = parse_qs(parsed.query)
                for key in ("book_id", "bookId"):
                    candidate = str((query.get(key) or [""])[0])
                    if BOOK_ID_RE.fullmatch(candidate):
                        return candidate
    raise ValueError("请输入有效的番茄小说链接或书籍 ID")


def resolve_novel_target(target: str) -> dict:
    """Resolve any supported Fanqie link on the private API server."""
    data = get_client().resolve_resource(str(target or "").strip(), "novel")
    metadata = data.get("metadata") or {}
    book_id = validate_book_id(str(metadata.get("book_id") or ""))
    return {**metadata, "book_id": book_id}


def book_id_from_task_url(url: str) -> str | None:
    if not str(url or "").startswith(FANQIE_TASK_PREFIX):
        return None
    try:
        return validate_book_id(str(url)[len(FANQIE_TASK_PREFIX):])
    except ValueError:
        return None


def classify_fanqie_target(target: str) -> dict | None:
    """判定番茄目标的作品类型（图片漫画 / 小说）。

    番茄小说与番茄图片漫画共用 ``fanqienovel.com`` 域名，光看 URL 无法分辨，
    因此把类型判定交给中转 API：非图片漫画作品按 ``comic`` 解析会返回
    ``INVALID_TARGET``（"该作品不是图片漫画，请在小说模块下载"），据此回落小说。

    返回 ``{'kind', 'book_id', 'title', 'media_label'}``；非番茄目标返回 None。
    作品不存在/已下架、中转不可用等错误原样抛 ``FanqieApiError``，由调用方提示用户。
    """
    from mangadock.services.fanqie_comics import (
        api_target_for,
        is_fanqie_comic_target,
    )

    text = str(target or "").strip()
    if not is_fanqie_comic_target(text):
        return None

    client = get_client()
    resolved = api_target_for(text)
    try:
        data = client.resolve_resource(resolved, "comic")
    except FanqieApiError as exc:
        if exc.code != "INVALID_TARGET":
            raise
        kind = "novel"
        data = client.resolve_resource(resolved, "novel")
    else:
        kind = "comic"

    metadata = data.get("metadata") or {}
    book_id = validate_book_id(str(metadata.get("book_id") or ""))
    title = str(metadata.get("title") or "").strip()
    if not title:
        title = f"番茄{'漫画' if kind == 'comic' else '小说'} {book_id}"
    return {
        "kind": kind,
        "book_id": book_id,
        "title": title,
        "media_label": "番茄小说" if kind == "novel" else "番茄漫画",
    }


def search_books(query: str, page: int = 1) -> list[dict]:
    query = str(query or "").strip()
    if not query:
        return []
    return get_client().search_novels(query, page)


def fetch_cover(book_id: str) -> tuple[bytes, str]:
    return get_client().fetch_cover(validate_book_id(book_id))


def fetch_book_cover(book_id: str) -> tuple[bytes, str]:
    return fetch_cover(book_id)


def _check_cancelled(task_id: str, api_job_id: str | None = None) -> None:
    task = get_task(task_id)
    if task and task.status == "cancelled":
        if api_job_id:
            try:
                get_client().cancel_job(api_job_id)
            except FanqieApiError:
                pass
        raise FanqieTaskCancelled("用户已取消任务")


def execute_fanqie_task(task_id: str) -> bool:
    task = get_task(task_id)
    if not task:
        return False
    book_id = book_id_from_task_url(task.url)
    if not book_id:
        update_task(task_id, status="error", log="无效的番茄小说任务", end_time=datetime.now(china_tz))
        return False

    temporary: Path | None = None
    api_job_id: str | None = None
    try:
        _check_cancelled(task_id)
        update_task(task_id, status="running", progress_percent=1, log="正在连接番茄资源 API")
        client = get_client()
        api_job = client.create_job(book_id, "novel", "epub")
        api_job_id = str(api_job.get("id") or "")
        if not api_job_id:
            raise FanqieApiError("番茄 API 未返回任务 ID", "INVALID_RESPONSE")
        update_task(task_id, log=f"番茄 API 任务已创建：{api_job_id}")
        poll_deadline = time.monotonic() + FANQIE_API_MAX_POLL_SECONDS

        while api_job.get("status") in {"queued", "running"}:
            _check_cancelled(task_id, api_job_id)
            # 2026-09-20 code review P2：加总超时，避免远端作业卡死时永久占用 worker
            if time.monotonic() > poll_deadline:
                try:
                    client.cancel_job(api_job_id)
                except Exception:
                    pass
                raise FanqieApiError(
                    f"番茄 API 任务超过 {FANQIE_API_MAX_POLL_SECONDS // 60} 分钟未完成，已中止",
                    "TIMEOUT",
                )
            metadata = api_job.get("metadata") or {}
            fields = {
                "progress_percent": max(1, min(99, int(api_job.get("progress") or 0))),
                "completed_chapters": int(api_job.get("completed_items") or 0),
                "total_chapters": int(api_job.get("total_items") or 0),
                "log": str(api_job.get("message") or "番茄 API 正在处理小说"),
            }
            if metadata.get("title"):
                fields["comic_name"] = str(metadata["title"])
            update_task(task_id, **fields)
            time.sleep(FANQIE_API_POLL_INTERVAL)
            api_job = client.get_job(api_job_id)

        if api_job.get("status") == "cancelled":
            raise FanqieTaskCancelled("番茄 API 任务已取消")
        if api_job.get("status") != "completed":
            raise FanqieApiError(
                str(api_job.get("message") or "番茄 API 小说任务失败"),
                "REMOTE_JOB_FAILED",
            )

        metadata = api_job.get("metadata") or {}
        title = str(metadata.get("title") or task.comic_name or f"番茄小说 {book_id}")
        author = str(metadata.get("author") or "未知作者")
        update_task(task_id, comic_name=title, progress_percent=95, log="正在下载并校验 EPUB")

        from mangadock.services.novels import get_novel_by_fanqie_id

        existing = get_novel_by_fanqie_id(book_id)
        if task.is_update and not existing:
            raise RuntimeError("书架中找不到这本番茄小说，无法更新")
        if existing:
            destination = Path(existing["file_path"])
        else:
            destination = Path(NOVEL_ROOT) / f"{sanitize_filename(f'{title}_{author}')}.epub"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.stem}.{task_id}.part.epub")
        client.download_artifact(api_job, temporary)
        _check_cancelled(task_id, api_job_id)
        os.replace(temporary, destination)
        temporary = None
        update_task(
            task_id,
            status="completed",
            progress_percent=100,
            completed_chapters=int(api_job.get("total_items") or 0),
            total_chapters=int(api_job.get("total_items") or 0),
            end_time=datetime.now(china_tz),
            log=f"已入库，可在小说书架阅读：{destination.name}",
        )
        return True
    except FanqieTaskCancelled:
        update_task(task_id, log="任务已取消，临时文件已清理")
        return False
    except Exception as exc:
        update_task(
            task_id,
            status="error",
            end_time=datetime.now(china_tz),
            log=f"番茄小说任务失败：{exc}",
        )
        return False
    finally:
        if temporary:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
