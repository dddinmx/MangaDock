# -*- coding: utf-8 -*-
"""嬉皮漫畫 (hipmh.com) provider：源解析 + 单章下载。

站点结构（2026-09-21 实测，Astro 前端 + S3 静态图床）：

1. 作品页 ``https://m.hipmh.com/works/<slug>-<manga_id>-<oid>`` 的
   ``#chapters-config`` 节点带 ``data-mid`` / ``data-manga-id`` / ``data-title`` / ``data-cover``；
2. 章节列表：``GET {HIPMH_API_BASE_URL}/v1/manga/chapters?mid=&page=&per_page=&order=``；
3. 阅读页 ``https://reader.hipmh.top/chapter/<作品页 hid>`` 里给出
   ``data-api-hid``（**与作品页 hid 不同**，须先取阅读页）、图床基址（line1/line2 及各自 s1 变体）；
4. 章节图片：``GET {HIPMH_API_BASE_URL}/v2/chapter?hid=<api_hid>``，``data.images`` 为混淆串，
   解码步骤：按 marker 三段重排 → 每 7 字符块隔块反转 → 双字母表翻译 → base64url → JSON；
5. 解码后的数组混入 **1 张诱饵图**，需按 ``order_id`` / ``sid`` 计算下标剔除（与前端逻辑一致）。

依赖 download.py 共享管线（download_chapter_images 等）的函数在函数体内延迟导入，
避免循环导入。
"""
import base64
import json
import os
import re
import shutil
from urllib.parse import quote

from mangadock.settings import (
    COMIC_ROOT,
    HIPMH_API_BASE_URL,
    HIPMH_DEFAULT_IMAGE_HOST,
    HIPMH_ENCODED_IMAGE_TRANSLATION,
    HIPMH_IMAGE_PAYLOAD_INNER_MARKER,
    HIPMH_IMAGE_PAYLOAD_PREFIX,
    HIPMH_IMAGE_PAYLOAD_SPLIT_MARKER,
    HIPMH_IMAGE_PAYLOAD_SUFFIX,
    HIPMH_READER_BASE_URL,
    HIPMH_READER_REFERER,
    HIPMH_SITE_REFERER,
)
from mangadock.services.providers.common import extract_description_from_html
from mangadock.services.tasks import is_task_cancel_requested, update_task
from mangadock.utils import safe_int
from mangadock.utils.http import safe_http_get
from mangadock.utils.media import default_headers, ensure_directory, sanitize_filename

# 前端剔除诱饵图用的两个 32 位常量（与站点 JS 保持一致）。
HIPMH_DECOY_CONST_A = (40503 << 16 | 31153) & 0xFFFFFFFF
HIPMH_DECOY_CONST_B = (34283 << 16 | 51819) & 0xFFFFFFFF

HIPMH_IMAGE_CHUNK_SIZE = 7
HIPMH_CHAPTER_PAGE_SIZE = 100


def _normalize_u32(value):
    """对齐站点 JS 的 q()：非有限/负数/超 32 位一律视为缺失。"""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or number > 0xFFFFFFFF:
        return None
    return number


def decode_hipmh_image_payload(encoded_images):
    """解码章节图片混淆串，返回图片相对路径列表。"""
    if (
        not isinstance(encoded_images, str)
        or not encoded_images.startswith(HIPMH_IMAGE_PAYLOAD_PREFIX)
        or not encoded_images.endswith(HIPMH_IMAGE_PAYLOAD_SUFFIX)
    ):
        raise ValueError("章节图片数据格式无效")

    body = encoded_images[len(HIPMH_IMAGE_PAYLOAD_PREFIX):-len(HIPMH_IMAGE_PAYLOAD_SUFFIX)]
    payload_length = len(body) - len(HIPMH_IMAGE_PAYLOAD_INNER_MARKER) - len(HIPMH_IMAGE_PAYLOAD_SPLIT_MARKER)
    if payload_length <= 0:
        raise ValueError("章节图片数据格式无效")

    trailing_length = payload_length // 3
    leading_length = (payload_length - trailing_length) // 2
    middle_length = payload_length - trailing_length - leading_length

    leading = body[:leading_length]
    inner = body[leading_length:leading_length + len(HIPMH_IMAGE_PAYLOAD_INNER_MARKER)]
    middle_start = leading_length + len(HIPMH_IMAGE_PAYLOAD_INNER_MARKER)
    middle = body[middle_start:middle_start + middle_length]
    split_start = middle_start + middle_length
    split = body[split_start:split_start + len(HIPMH_IMAGE_PAYLOAD_SPLIT_MARKER)]
    trailing = body[split_start + len(HIPMH_IMAGE_PAYLOAD_SPLIT_MARKER):]

    if (
        inner != HIPMH_IMAGE_PAYLOAD_INNER_MARKER
        or split != HIPMH_IMAGE_PAYLOAD_SPLIT_MARKER
        or len(trailing) != trailing_length
    ):
        raise ValueError("章节图片数据格式无效")

    mixed_payload = trailing + leading + middle
    restored_chunks = []
    for chunk_index, start in enumerate(range(0, len(mixed_payload), HIPMH_IMAGE_CHUNK_SIZE)):
        chunk = mixed_payload[start:start + HIPMH_IMAGE_CHUNK_SIZE]
        restored_chunks.append(chunk[::-1] if chunk_index % 2 else chunk)

    translated_payload = ''.join(restored_chunks).translate(HIPMH_ENCODED_IMAGE_TRANSLATION)
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
    return [item for item in images if isinstance(item, str)]


def drop_hipmh_decoy_image(images, order_id, sid):
    """按 order_id / sid 计算下标并剔除站点混入的诱饵图（与前端 H() 一致）。"""
    total = len(images)
    if total <= 0:
        return images
    order = _normalize_u32(order_id)
    session_id = _normalize_u32(sid)
    if order is None or session_id is None:
        return images
    try:
        index = (session_id * HIPMH_DECOY_CONST_A ^ total * HIPMH_DECOY_CONST_B) % total
        index = order ^ index
    except (TypeError, ValueError, OverflowError):
        return images
    if index < 0 or index >= total:
        return images
    return images[:index] + images[index + 1:]


def build_hipmh_image_url(image_path, image_base=HIPMH_DEFAULT_IMAGE_HOST):
    if not image_path:
        return None
    if image_path.startswith('http://') or image_path.startswith('https://'):
        return image_path
    return f"{image_base}{image_path}"


def _find_meta_attr(html_content, attr_name):
    match = re.search(attr_name + r'="([^"]*)"', html_content or '')
    return match.group(1) if match else None


def load_hipmh_chapter_list(mid):
    """按 mid 分页拉取全部章节。"""
    chapters = []
    page = 1
    while True:
        list_url = (
            f"{HIPMH_API_BASE_URL}/v1/manga/chapters"
            f"?mid={quote(str(mid))}&page={page}&per_page={HIPMH_CHAPTER_PAGE_SIZE}&order=asc"
        )
        response = safe_http_get(
            list_url,
            headers=default_headers(referer=HIPMH_SITE_REFERER),
            timeout=30
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('code') != 200:
            raise ValueError("获取嬉皮漫畫章节列表失败")

        data = payload.get('data') or {}
        items = data.get('items') or []
        for index, item in enumerate(items, start=1):
            chapter_hid = item.get('hid')
            if not chapter_hid:
                continue
            order = safe_int(item.get('chapter_number'), default=len(chapters) + 1, minimum=1)
            chapter_title = item.get('title') or f"第{order}話"
            chapters.append({
                'order': order,
                'title': chapter_title,
                'filename_base': f"{order:04d}_{sanitize_filename(chapter_title)}",
                'chapter_hid': chapter_hid
            })

        total_pages = safe_int(data.get('total_pages'), default=page, minimum=1)
        if not items or page >= total_pages:
            break
        page += 1

    return chapters


def load_hipmh_source(url):
    response = safe_http_get(url, headers=default_headers(referer=HIPMH_SITE_REFERER), timeout=30)
    response.raise_for_status()
    response.encoding = 'utf-8'
    html_content = response.text

    config_node = re.search(r'id="chapters-config"([^>]*)>', html_content)
    config_html = config_node.group(1) if config_node else html_content

    mid = _find_meta_attr(config_html, 'data-mid')
    if not mid:
        raise ValueError("未找到嬉皮漫畫作品 ID")

    page_title = sanitize_filename(_find_meta_attr(config_html, 'data-title') or "未知漫画")
    page_cover = _find_meta_attr(config_html, 'data-cover')
    description = extract_description_from_html(html_content)

    chapters = load_hipmh_chapter_list(mid)
    if not chapters:
        return {
            'provider': 'hipmh',
            'title': page_title,
            'cover_url': page_cover,
            'cover_verify': True,
            'description': description,
            'chapters': [],
            'mid': mid
        }

    return {
        'provider': 'hipmh',
        'title': page_title,
        'cover_url': page_cover,
        'cover_verify': True,
        'description': description,
        'chapters': chapters,
        'mid': mid
    }


def load_hipmh_chapter_images(chapter_hid):
    """取某章的图片直链列表（含线路判定与诱饵图剔除）。"""
    reader_url = f"{HIPMH_READER_BASE_URL}/chapter/{quote(str(chapter_hid))}"
    reader_response = safe_http_get(
        reader_url,
        headers=default_headers(referer=HIPMH_SITE_REFERER),
        timeout=30
    )
    reader_response.raise_for_status()
    reader_response.encoding = 'utf-8'
    reader_html = reader_response.text

    api_hid = _find_meta_attr(reader_html, 'data-api-hid')
    if not api_hid:
        raise ValueError("未获取到嬉皮漫畫章节 API 标识")

    api_base = (
        _find_meta_attr(reader_html, 'data-api-base-url-line1')
        or HIPMH_API_BASE_URL
    ).rstrip('/')
    if api_base.endswith('/v1'):
        api_base = api_base[:-len('/v1')]

    image_base_line1 = _find_meta_attr(reader_html, 'data-chapter-img-base-line1') or HIPMH_DEFAULT_IMAGE_HOST
    image_base_line1_secure = _find_meta_attr(reader_html, 'data-chapter-img-base-line1s') or image_base_line1

    chapter_response = safe_http_get(
        f"{api_base}/v2/chapter?hid={quote(api_hid)}",
        headers=default_headers(referer=HIPMH_READER_REFERER),
        timeout=30
    )
    chapter_response.raise_for_status()
    chapter_payload = chapter_response.json()
    if chapter_payload.get('code') != 200:
        raise ValueError("获取嬉皮漫畫章节图片失败")

    chapter_data = chapter_payload.get('data') or {}
    raw_images = chapter_data.get('images')
    if isinstance(raw_images, str):
        images = decode_hipmh_image_payload(raw_images)
    elif isinstance(raw_images, list):
        images = [item for item in raw_images if isinstance(item, str)]
    else:
        raise ValueError("章节图片数据格式无效")

    # 线路：站点前端默认固定走 line1 图床，只有 API 返回 line=9 时才切到该线路的 s1（安全）变体。
    line_number = safe_int(chapter_data.get('line'), default=1, minimum=0)
    image_base = image_base_line1_secure if line_number == 9 else image_base_line1
    images = drop_hipmh_decoy_image(images, chapter_data.get('order_id'), chapter_data.get('sid'))
    return [build_hipmh_image_url(image, image_base) for image in images]


def download_hipmh_chapter(source, chapter, folder, comic_format, task_id):
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

        image_urls = load_hipmh_chapter_images(chapter['chapter_hid'])
        image_jobs = []
        for image_order, image_url in enumerate(image_urls, start=1):
            if not image_url:
                continue
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
            referer=HIPMH_READER_REFERER,
            max_workers=5,
            cancel_checker=lambda: is_task_cancel_requested(task_id)
        )
        if cancelled:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"

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
