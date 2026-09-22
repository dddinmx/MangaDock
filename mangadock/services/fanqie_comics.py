# -*- coding: utf-8 -*-
"""Fanqie comic provider backed exclusively by the private resource API."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from mangadock.services.fanqie_api import FanqieApiError, get_client
from mangadock.services.library import (
    get_local_chapter_match_bases,
    is_existing_local_chapter,
    save_comic_description,
    save_comic_mapping,
)
from mangadock.services.tasks import get_task, update_task
from mangadock.settings import (
    COMIC_ROOT,
    COVER_ROOT,
    FANQIE_API_MAX_ARTIFACT_BYTES,
    FANQIE_API_MAX_POLL_SECONDS,
    FANQIE_API_POLL_INTERVAL,
    MAX_ARCHIVE_ENTRY_BYTES,
    china_tz,
)
from mangadock.utils.cover_image import normalize_cover_bytes
from mangadock.utils.media import sanitize_filename


FANQIE_COMIC_TASK_PREFIX = "fanqie-comic://"
FANQIE_COMIC_HOSTS = ("fanqienovel.com", "changdunovel.com")
BOOK_ID_RE = re.compile(r"\d{8,24}")


def comic_task_url(book_id: str) -> str:
    book_id = str(book_id or "").strip()
    if not BOOK_ID_RE.fullmatch(book_id):
        raise ValueError("无效的番茄漫画作品 ID")
    return f"{FANQIE_COMIC_TASK_PREFIX}{book_id}"


def is_fanqie_comic_target(value: str) -> bool:
    text = str(value or "").strip()
    if text.startswith(FANQIE_COMIC_TASK_PREFIX):
        return bool(BOOK_ID_RE.fullmatch(text[len(FANQIE_COMIC_TASK_PREFIX):]))
    if BOOK_ID_RE.fullmatch(text):
        return True
    try:
        host = (urlparse(text).hostname or "").lower()
    except ValueError:
        return False
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in FANQIE_COMIC_HOSTS)


def api_target_for(target: str) -> str:
    """剥掉内部任务前缀，得到可提交给中转 API 的目标。

    统一入口会把用户粘贴的原始链接/ID 直接交给判定函数，其中可能已带
    ``fanqie-comic://`` 前缀（例如重放历史任务 URL），必须先剥离再解析。
    """
    text = str(target or "").strip()
    if text.startswith(FANQIE_COMIC_TASK_PREFIX):
        return text[len(FANQIE_COMIC_TASK_PREFIX):]
    return text


def load_fanqie_comic_source(target: str, client=None) -> dict:
    if not is_fanqie_comic_target(target):
        raise ValueError("请输入有效的番茄漫画链接或书籍 ID")
    data = (client or get_client()).resolve_resource(api_target_for(target), "comic")
    metadata = data.get("metadata") or {}
    chapters = data.get("chapters") or []
    book_id = str(metadata.get("book_id") or "")
    if not BOOK_ID_RE.fullmatch(book_id):
        raise ValueError("番茄 API 未返回有效作品 ID")
    if not chapters:
        raise ValueError("番茄漫画目录为空")
    normalized_chapters = []
    for index, item in enumerate(chapters, 1):
        if not isinstance(item, dict) or not str(item.get("id") or ""):
            continue
        title = str(item.get("title") or f"第{index}话")
        order = int(item.get("order") or index)
        normalized_chapters.append({
            "id": str(item["id"]),
            "item_id": str(item["id"]),
            "order": order,
            "title": title,
            "filename_base": sanitize_filename(
                str(item.get("filename_base") or f"{order:04d}_{title}")
            ),
        })
    if not normalized_chapters:
        raise ValueError("番茄漫画目录为空")
    return {
        "provider": "fanqie",
        "book_id": book_id,
        "title": sanitize_filename(str(metadata.get("title") or book_id)),
        "author": str(metadata.get("author") or "未知作者"),
        "description": str(metadata.get("description") or ""),
        "declared_comic": bool(metadata.get("declared_comic")),
        "chapters": normalized_chapters,
        "source_url": comic_task_url(book_id),
    }


def _save_cover(comic_name: str, book_id: str, content: bytes | None = None) -> None:
    if content is None:
        content, _content_type = get_client().fetch_cover(book_id)
    if not content or len(content) > 8 * 1024 * 1024:
        raise ValueError("番茄漫画封面无效")
    os.makedirs(COVER_ROOT, exist_ok=True)
    cover_path = Path(COVER_ROOT) / f"{comic_name}.jpg"
    # 统一经封面转码写盘：源站返回 AVIF 等格式时 Pillow 直接 open 会失败，
    # 这里交给公共转换链处理（Pillow → 可选插件 → sips/ImageMagick/ffmpeg）。
    if not normalize_cover_bytes(content, str(cover_path)):
        raise ValueError("番茄漫画封面格式无法转码")
    # 超分缓存走后台线程（AI 引擎单张 7~11s），不阻塞入库/下载 worker
    try:
        from mangadock.utils.cover_enhance import request_hero_cover
        request_hero_cover(comic_name)
    except Exception:
        pass


def _persist_comic_metadata(task_id: str, folder: str, source: dict, cover: bytes | None = None) -> None:
    save_comic_mapping(folder, source["source_url"])
    if source.get("description"):
        save_comic_description(folder, source["description"])
    try:
        _save_cover(folder, source["book_id"], content=cover)
        update_task(task_id, log="封面已保存")
    except Exception as exc:
        update_task(task_id, log=f"封面下载失败，正文已正常入库：{exc}")


def _safe_archive_name(name: str) -> bool:
    path = Path(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name


def _import_comic_archive(
    archive_path: Path,
    source: dict,
    comic_dir: Path,
    output_format: str,
    expected_ids: set[str],
    task_id: str,
) -> tuple[int, bytes | None]:
    staging = comic_dir.parent / f".{comic_dir.name}.{task_id}.import"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            for info in infos:
                if not _safe_archive_name(info.filename):
                    raise ValueError("番茄漫画压缩包包含不安全路径")
                if info.file_size > MAX_ARCHIVE_ENTRY_BYTES:
                    raise ValueError("番茄漫画章节文件超过大小限制")
                total_uncompressed += info.file_size
                if total_uncompressed > FANQIE_API_MAX_ARTIFACT_BYTES:
                    raise ValueError("番茄漫画压缩包解压后超过大小限制")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("番茄漫画压缩包不能包含符号链接")

            try:
                manifest_info = archive.getinfo("manifest.json")
            except KeyError as exc:
                raise ValueError("番茄漫画压缩包缺少清单") from exc
            if manifest_info.file_size > 2 * 1024 * 1024:
                raise ValueError("番茄漫画清单过大")
            manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
            metadata = manifest.get("metadata") or {}
            if str(metadata.get("book_id") or "") != source["book_id"]:
                raise ValueError("番茄漫画压缩包作品 ID 不匹配")
            if str(manifest.get("format") or "") != output_format:
                raise ValueError("番茄漫画压缩包格式不匹配")

            manifest_chapters = manifest.get("chapters") or []
            returned_ids = {str(item.get("id") or "") for item in manifest_chapters if isinstance(item, dict)}
            if expected_ids and returned_ids != expected_ids:
                raise ValueError("番茄漫画压缩包章节集合不匹配")
            if not manifest_chapters:
                raise ValueError("番茄漫画压缩包没有章节")

            staged_files = []
            for item in manifest_chapters:
                if not isinstance(item, dict):
                    raise ValueError("番茄漫画章节清单格式无效")
                member_name = str(item.get("file") or "")
                expected_suffix = f".{output_format}"
                if not member_name.startswith("chapters/") or not member_name.endswith(expected_suffix):
                    raise ValueError("番茄漫画章节文件名无效")
                member_path = Path(member_name)
                if len(member_path.parts) != 2 or not _safe_archive_name(member_name):
                    raise ValueError("番茄漫画章节路径无效")
                expected_base = sanitize_filename(str(item.get("filename_base") or ""))
                if member_path.stem != expected_base:
                    raise ValueError("番茄漫画章节文件与清单不匹配")
                try:
                    info = archive.getinfo(member_name)
                except KeyError as exc:
                    raise ValueError("番茄漫画章节文件缺失") from exc
                destination = staging / member_path.name
                with archive.open(info) as source_file, destination.open("wb") as output_file:
                    copied = 0
                    while True:
                        chunk = source_file.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > MAX_ARCHIVE_ENTRY_BYTES:
                            raise ValueError("番茄漫画章节文件超过大小限制")
                        output_file.write(chunk)
                if copied != info.file_size or copied <= 0:
                    raise ValueError("番茄漫画章节文件不完整")
                staged_files.append(destination)

            cover = None
            cover_name = str(manifest.get("cover") or "")
            if cover_name:
                if not re.fullmatch(r"cover\.(?:jpe?g|png|webp|gif)", cover_name, re.I):
                    raise ValueError("番茄漫画封面文件名无效")
                cover_info = archive.getinfo(cover_name)
                if cover_info.file_size > 8 * 1024 * 1024:
                    raise ValueError("番茄漫画封面过大")
                cover = archive.read(cover_info)

        comic_dir.mkdir(parents=True, exist_ok=True)
        for staged_file in staged_files:
            os.replace(staged_file, comic_dir / staged_file.name)
        return len(staged_files), cover
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def execute_fanqie_comic_task(task_id: str) -> bool:
    task = get_task(task_id)
    if not task:
        return False
    api_job_id = None
    archive_path = None
    try:
        update_task(task_id, status="running", progress_percent=1, log="正在通过番茄资源 API 读取漫画信息")
        client = get_client()
        source = load_fanqie_comic_source(task.url, client=client)
        folder = task.comic_name if task.is_update and task.comic_name else source["title"]
        folder = sanitize_filename(folder or source["book_id"])
        comic_dir = Path(COMIC_ROOT) / folder

        update_task(task_id, comic_name=folder, url=source["source_url"], log=f"准备下载：《{source['title']}》")
        existing_match_bases = get_local_chapter_match_bases(folder)
        pending = [
            chapter for chapter in source["chapters"]
            if not is_existing_local_chapter(chapter, existing_match_bases)
        ]
        update_task(
            task_id,
            total_chapters=len(pending),
            completed_chapters=0,
            progress_percent=0,
            log=f"远端共 {len(source['chapters'])} 章，待下载 {len(pending)} 章",
        )
        if not pending:
            _persist_comic_metadata(task_id, folder, source)
            update_task(
                task_id,
                status="completed",
                progress_percent=100,
                end_time=datetime.now(china_tz),
                log="当前漫画已是最新版本",
            )
            return True

        output_format = "pdf" if int(task.comic_format or 2) == 1 else "cbz"
        api_job = client.create_job(
            source["book_id"],
            "comic",
            output_format,
            chapter_ids=[chapter["id"] for chapter in pending],
        )
        api_job_id = str(api_job.get("id") or "")
        if not api_job_id:
            raise FanqieApiError("番茄 API 未返回任务 ID", "INVALID_RESPONSE")
        # 2026-09-20 code review P2：轮询总超时，避免远端作业卡死时永久占用 worker
        poll_deadline = time.monotonic() + FANQIE_API_MAX_POLL_SECONDS

        while api_job.get("status") in {"queued", "running"}:
            current = get_task(task_id)
            if current and current.status == "cancelled":
                try:
                    client.cancel_job(api_job_id)
                except FanqieApiError:
                    pass
                update_task(task_id, log="任务已取消")
                return False
            if time.monotonic() > poll_deadline:
                try:
                    client.cancel_job(api_job_id)
                except FanqieApiError:
                    pass
                update_task(
                    task_id,
                    log=f"番茄 API 任务超过 {FANQIE_API_MAX_POLL_SECONDS // 60} 分钟未完成，已中止",
                )
                return False
            update_task(
                task_id,
                progress_percent=max(1, min(94, int(api_job.get("progress") or 0))),
                completed_chapters=int(api_job.get("completed_items") or 0),
                total_chapters=int(api_job.get("total_items") or len(pending)),
                log=str(api_job.get("message") or "番茄 API 正在处理漫画"),
            )
            time.sleep(FANQIE_API_POLL_INTERVAL)
            api_job = client.get_job(api_job_id)

        if api_job.get("status") == "cancelled":
            update_task(task_id, log="任务已取消")
            return False
        if api_job.get("status") != "completed":
            raise FanqieApiError(
                str(api_job.get("message") or "番茄 API 漫画任务失败"),
                "REMOTE_JOB_FAILED",
            )

        comic_dir.parent.mkdir(parents=True, exist_ok=True)
        archive_path = comic_dir.parent / f".{folder}.{task_id}.part.zip"
        update_task(task_id, progress_percent=95, log="正在下载并校验漫画章节包")
        client.download_artifact(api_job, archive_path)
        imported, cover = _import_comic_archive(
            archive_path,
            source,
            comic_dir,
            output_format,
            {chapter["id"] for chapter in pending},
            task_id,
        )
        _persist_comic_metadata(task_id, folder, source, cover=cover)
        update_task(
            task_id,
            status="completed",
            completed_chapters=imported,
            total_chapters=len(pending),
            progress_percent=100,
            end_time=datetime.now(china_tz),
            log="番茄漫画已入库，可直接开始阅读",
        )
        return True
    except Exception as exc:
        current = get_task(task_id)
        if current and current.status == "cancelled":
            update_task(task_id, log="任务已取消")
            return False
        update_task(
            task_id,
            status="error",
            end_time=datetime.now(china_tz),
            log=f"番茄漫画任务失败：{exc}",
        )
        return False
    finally:
        if archive_path:
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                pass
