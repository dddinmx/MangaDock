# -*- coding: utf-8 -*-
"""包子漫画 baozimh.org provider：源解析 + 单章下载。

依赖 download.py 共享管线（download_chapter_images 等）的函数
在函数体内延迟导入，避免循环导入。
"""
import base64
import json
import os
import re
import shutil

from bs4 import BeautifulSoup

from mangadock.settings import (
    BAOZIMH_ORG_API_BASE_URL,
    BAOZIMH_ORG_DEFAULT_IMAGE_HOST,
    BAOZIMH_ORG_ENCODED_IMAGE_TRANSLATION,
    BAOZIMH_ORG_IMAGE_HOST,
    COMIC_ROOT,
)
from mangadock.services.library import normalize_comic_description
from mangadock.services.providers.common import extract_description_from_html, safe_print
from mangadock.services.tasks import is_task_cancel_requested, update_task
from mangadock.utils.http import safe_http_get
from mangadock.utils.media import default_headers, ensure_directory, sanitize_filename


def resolve_baozimh_org_image_host(line):
    try:
        image_line = int(line)
    except (TypeError, ValueError):
        image_line = None
    return BAOZIMH_ORG_IMAGE_HOST if image_line == 2 else BAOZIMH_ORG_DEFAULT_IMAGE_HOST


def build_baozimh_org_image_url(image_path, image_host=BAOZIMH_ORG_IMAGE_HOST):
    if not image_path:
        return None
    if image_path.startswith('http://') or image_path.startswith('https://'):
        return image_path
    return f"{image_host}{image_path}"


def decode_baozimh_org_image_payload(encoded_images):
    prefix = 'J7r'
    inner_marker = 'kD'
    split_marker = 'W4s'
    suffix = 'nQ'
    chunk_size = 7

    if (
        not isinstance(encoded_images, str)
        or not encoded_images.startswith(prefix)
        or not encoded_images.endswith(suffix)
    ):
        raise ValueError("章节图片数据格式无效")

    body = encoded_images[len(prefix):-len(suffix)]
    payload_length = len(body) - len(inner_marker) - len(split_marker)
    if payload_length <= 0:
        raise ValueError("章节图片数据格式无效")

    trailing_length = payload_length // 3
    leading_length = (payload_length - trailing_length) // 2
    middle_length = payload_length - trailing_length - leading_length
    leading = body[:leading_length]
    inner = body[leading_length:leading_length + len(inner_marker)]
    middle_start = leading_length + len(inner_marker)
    middle = body[middle_start:middle_start + middle_length]
    split_start = middle_start + middle_length
    split = body[split_start:split_start + len(split_marker)]
    trailing = body[split_start + len(split_marker):]

    if inner != inner_marker or split != split_marker or len(trailing) != trailing_length:
        raise ValueError("章节图片数据格式无效")

    mixed_payload = trailing + leading + middle
    restored_chunks = []
    for chunk_index, start in enumerate(range(0, len(mixed_payload), chunk_size)):
        chunk = mixed_payload[start:start + chunk_size]
        restored_chunks.append(chunk[::-1] if chunk_index % 2 else chunk)

    translated_payload = ''.join(restored_chunks).translate(BAOZIMH_ORG_ENCODED_IMAGE_TRANSLATION)
    padding = '=' * ((4 - len(translated_payload) % 4) % 4)
    try:
        decoded_payload = base64.urlsafe_b64decode(
            f"{translated_payload}{padding}".encode('ascii')
        ).decode('utf-8')
        images = json.loads(decoded_payload)
    except Exception as exc:
        raise ValueError("章节图片数据解析失败") from exc

    if not isinstance(images, list):
        raise ValueError("章节图片数据格式无效")
    return images


def load_baozimh_org_source(url):
    response = safe_http_get(url, headers=default_headers(), timeout=30, verify=False)
    response.raise_for_status()
    response.encoding = 'utf-8'

    soup = BeautifulSoup(response.text, "html.parser")
    history_node = soup.find(id='MangaHistoryStorage')
    page_title = sanitize_filename(
        history_node.get('data-title') if history_node else ''
    )
    page_cover = history_node.get('data-cover') if history_node else None

    mid_match = re.search(r'data-mid="([0-9]+)"', response.text)
    if not mid_match:
        raise ValueError("未找到 baozimh.org 漫画 ID")

    mid = int(mid_match.group(1))
    page_description = extract_description_from_html(response.text, soup=soup)

    if not soup.find(id='mangachapters'):
        return {
            'provider': 'baozimh_org',
            'title': page_title or "未知漫画",
            'cover_url': page_cover,
            'cover_verify': False,
            'description': page_description,
            'chapters': [],
            'mid': mid
        }

    manga_response = safe_http_get(
        f"{BAOZIMH_ORG_API_BASE_URL}/api/manga/get?mid={mid}&mode=all",
        headers=default_headers(),
        timeout=30,
        verify=False
    )
    manga_response.raise_for_status()
    manga_info = manga_response.json()
    if manga_info.get('code') != 200:
        raise ValueError("获取 baozimh.org 漫画信息失败")

    manga_data = manga_info.get('data', {})
    remote_chapters = manga_data.get('chapters', [])
    remote_chapters.sort(key=lambda item: int(item['attributes'].get('order', 0)))

    chapters = []
    for index, chapter in enumerate(remote_chapters, start=1):
        order = int(chapter['attributes'].get('order', index))
        title = chapter['attributes'].get('title') or f"第{order}话"
        chapters.append({
            'order': order,
            'title': title,
            'filename_base': f"{order:04d}_{sanitize_filename(title)}",
            'chapter_id': chapter['id']
        })

    description = normalize_comic_description(manga_data.get('desc') or manga_data.get('description'))
    if not description:
        description = page_description

    return {
        'provider': 'baozimh_org',
        'title': sanitize_filename(manga_data.get('title') or "未知漫画"),
        'cover_url': manga_data.get('cover'),
        'cover_verify': False,
        'description': description,
        'chapters': chapters,
        'mid': mid
    }


def load_baozimh_org_chapter_images(mid, chapter_id):
    response = safe_http_get(
        f"{BAOZIMH_ORG_API_BASE_URL}/api/v2/chapter/getinfo?m={mid}&c={chapter_id}",
        headers=default_headers(referer='https://baozimh.org/'),
        timeout=30,
        verify=False
    )
    response.raise_for_status()
    chapter_info = response.json()
    if chapter_info.get('code') != 200:
        raise ValueError("获取章节图片信息失败")
    chapter_data = chapter_info.get('data', {})
    image_data = chapter_data.get('info', {}).get('images', {})
    raw_images = image_data.get('images', [])
    if isinstance(raw_images, str):
        raw_images = decode_baozimh_org_image_payload(raw_images)
    return raw_images, image_data.get('line')


def download_baozimh_org_chapter(source, chapter, folder, comic_format, task_id):
    save_dir = os.path.join(COMIC_ROOT, folder, chapter['filename_base'])

    from mangadock.services.download import (
        download_chapter_images,
        extract_image_extension,
        finalize_downloaded_chapter,
        incomplete_chapter_reason,
    )

    ensure_directory(save_dir)

    try:
        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        raw_images, image_line = load_baozimh_org_chapter_images(source['mid'], chapter['chapter_id'])
        image_host = resolve_baozimh_org_image_host(image_line)
        image_jobs = []
        for fallback_order, image in enumerate(raw_images, start=1):
            if not isinstance(image, dict):
                continue
            image_url = build_baozimh_org_image_url(image.get('url'), image_host)
            if not image_url:
                continue
            image_order = int(image.get('order') or fallback_order)
            image_jobs.append({
                'url': image_url,
                'filename': f"{image_order:03d}{extract_image_extension(image_url, '.webp')}"
            })

        if not image_jobs:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 未找到图片"

        success_count, failed_items, cancelled = download_chapter_images(
            image_jobs,
            save_dir,
            referer='https://baozimh.org/',
            verify=False,
            max_workers=5,
            cancel_checker=lambda: is_task_cancel_requested(task_id)
        )
        if cancelled:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        # 2026-09-20 code review P2：不能只看 success_count == 0，
        # 部分成功会 finalize 出缺页 CBZ/PDF 且更新检查认为已是最新。
        incomplete_reason = incomplete_chapter_reason(success_count, len(image_jobs))
        if incomplete_reason:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 下载失败：{incomplete_reason}"

        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        success, message = finalize_downloaded_chapter(save_dir, comic_format)
        shutil.rmtree(save_dir, ignore_errors=True)
        if not success:
            return False, message

        if failed_items:
            update_task(task_id, log=f"章节 {chapter['title']} 有 {len(failed_items)} 张图片下载失败")
        return True, f"章节 {chapter['title']} 处理完成"
    except Exception as exc:
        shutil.rmtree(save_dir, ignore_errors=True)
        return False, f"章节 {chapter['title']} 下载失败: {exc}"
