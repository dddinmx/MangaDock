# -*- coding: utf-8 -*-
"""瓜子漫画 (guazimanhua.com) provider：源解析 + 单章下载。

站点结构（2026-09-22 实测，PHP 服务端渲染 + Cloudflare 图床 img.guazicdn.com）：

1. 作品页 ``https://www.guazimanhua.com/comic.php?id=<作品id>``
   - ``<script type="application/ld+json">`` 的 ``@graph`` 里 ``ComicStory``
     给出 name / image（封面）/ author / genre，用来取标题与封面；
   - 同一个 JSON-LD 的 ``ItemList`` 也带章节目录，但**超过 50 章会被截断**
     （实测 83 / 104 / 115 章的作品都只给 50 条），**不可作为章节来源**；
   - DOM ``<div class="all-chapter-grid" data-chapter-list>`` 内的
     ``<a href="/chapter.php?id=...">第N话</a>`` 才是**完整**目录
     （条数与页面上的「共 N 话」一致），且按**倒序**排列（最新在最前），
     反转即阅读顺序。章节命名不统一（``第1话`` / ``预告`` / ``偷天换日令``），
     因此一律沿用站点给的文字，顺序用 DOM 位置推导，不解析标题里的数字。
2. 章节页 ``https://www.guazimanhua.com/chapter.php?id=<章节id>``
   - 图片全部内联在 DOM：``<img id="page-N" class="reading-image" src="...">``，
     ``data-page`` 从 1 连续到总页数；这是**唯一可靠**的图片源 ——
     同一个页面的 JSON-LD 图片 ItemList **只给前 20 张**，不能用。
3. 图片托管在 ``img.guazicdn.com``：**无防盗链**（带不带 Referer 都是 200），
   但 **URL 后缀写的是 ``.webp``、实际字节是 JPEG**（``content-type: image/jpeg``，
   实测 12/12 张 magic 均为 ``ffd8ffe0``），所以本地文件名统一写 ``.jpg``，
   免得读图端按扩展名误判。站点自己的 Web 阅读器也是靠嗅探内容显示的。
4. **无会员墙**：实测最新话（第 46/46 话）未登录即可取齐全部图片。

依赖 download.py 共享管线的函数在函数体内延迟导入，避免循环导入。
"""
import json
import os
import re
import shutil

from bs4 import BeautifulSoup

from mangadock.services.providers.common import extract_description_from_html
from mangadock.services.tasks import is_task_cancel_requested, update_task
from mangadock.settings import COMIC_ROOT
from mangadock.utils import safe_int
from mangadock.utils.http import safe_http_get
from mangadock.utils.media import default_headers, ensure_directory, sanitize_filename

GUAZIMANHUA_SITE_BASE = 'https://www.guazimanhua.com'
GUAZIMANHUA_SITE_REFERER = 'https://www.guazimanhua.com/'

_ID_PARAM_RE = re.compile(r'[?&]id=(\d+)')
_CHAPTER_ID_RE = re.compile(r'/chapter\.php\?id=(\d+)')
_LD_JSON_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_READING_IMAGE_RE = re.compile(
    r'<img[^>]+class="[^"]*reading-image[^"]*"[^>]+src="(https?://[^"]+)"'
)


def is_guazimanhua_url(url):
    """整 URL 判定（供注册表 pattern 之外的场合复用）。"""
    match = re.match(
        r'^https?://(?:www\.)?guazimanhua\.com/comic\.php\?(?:[^#\s]*&)?id=\d+',
        (url or '').strip()
    )
    return bool(match)


def _extract_comic_id(url):
    match = _ID_PARAM_RE.search(url or '')
    if not match:
        raise ValueError('未找到瓜子漫画作品 ID')
    return match.group(1)


def _fetch_html(url, referer=None):
    response = safe_http_get(url, headers=default_headers(referer=referer), timeout=30)
    response.raise_for_status()
    response.encoding = 'utf-8'
    return response.text


def _parse_ld_json_nodes(html_content):
    """取出所有 ld+json 块的 ``@graph`` 节点（站点把结构化数据全塞在这一处）。"""
    nodes = []
    for raw in _LD_JSON_RE.findall(html_content or ''):
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        graph = payload.get('@graph') if isinstance(payload, dict) else None
        if isinstance(graph, list):
            nodes.extend(node for node in graph if isinstance(node, dict))
    return nodes


def _first_ld_node(nodes, node_type):
    for node in nodes:
        if node.get('@type') == node_type:
            return node
    return None


def _parse_chapter_list(html_content, soup):
    """解析完整章节目录，返回**按阅读顺序（旧 → 新）**排列的章节列表。

    站点目录是**倒序**（最新在最前），所以最后整体反转一次。
    """
    container = soup.select_one('[data-chapter-list]')
    raw_pairs = []
    if container is not None:
        for anchor in container.find_all('a', href=True):
            match = _CHAPTER_ID_RE.search(anchor['href'])
            if not match:
                continue
            raw_pairs.append((match.group(1), anchor.get_text(' ', strip=True)))

    if not raw_pairs:
        # 兜底：容器结构变化时退回全页正则（下面同样会去重）。
        for href, text in re.findall(
            r'href="(/chapter\.php\?id=\d+)"[^>]*>([^<]*)</a>', html_content or ''
        ):
            match = _CHAPTER_ID_RE.search(href)
            if match:
                raw_pairs.append((match.group(1), text.strip()))

    # 去重（同一章节在页面里可能出现多次），保持首次出现的位置
    seen = set()
    unique_pairs = []
    for chapter_id, text in raw_pairs:
        if chapter_id in seen:
            continue
        seen.add(chapter_id)
        unique_pairs.append((chapter_id, text))

    unique_pairs.reverse()  # 倒序 → 阅读顺序

    chapters = []
    for index, (chapter_id, text) in enumerate(unique_pairs, start=1):
        label = text or f'第{index}话'
        chapters.append({
            'order': index,
            'title': label,
            'filename_base': f'{index:04d}_{sanitize_filename(label)}',
            'chapter_url': f'{GUAZIMANHUA_SITE_BASE}/chapter.php?id={chapter_id}',
            'chapter_id': chapter_id,
        })
    return chapters


def load_guazimanhua_source(url):
    comic_id = _extract_comic_id(url)
    canonical_url = f'{GUAZIMANHUA_SITE_BASE}/comic.php?id={comic_id}'
    html_content = _fetch_html(canonical_url, referer=GUAZIMANHUA_SITE_REFERER)
    soup = BeautifulSoup(html_content, 'html.parser')

    story = _first_ld_node(_parse_ld_json_nodes(html_content), 'ComicStory') or {}
    heading = soup.find('h1')
    title_source = (
        story.get('name')
        or (heading.get_text(' ', strip=True) if heading else '')
        or (soup.title.get_text(strip=True) if soup.title else '')
        or '未知漫画'
    )

    chapters = _parse_chapter_list(html_content, soup)
    if not chapters:
        raise ValueError('未获取到有效章节列表')

    return {
        'provider': 'guazimanhua',
        'title': sanitize_filename(title_source),
        'cover_url': story.get('image'),
        'cover_verify': True,
        'description': extract_description_from_html(html_content, soup=soup),
        'chapters': chapters,
        'comic_id': comic_id,
    }


def load_guazimanhua_chapter_images(chapter_url):
    """取某章的全部图片直链（按 ``data-page`` 排序，保证页序正确）。"""
    html_content = _fetch_html(chapter_url, referer=GUAZIMANHUA_SITE_REFERER)
    soup = BeautifulSoup(html_content, 'html.parser')

    items = []
    for fallback_order, node in enumerate(soup.select('img.reading-image'), start=1):
        src = (node.get('src') or node.get('data-src') or '').strip()
        if src.startswith('//'):
            src = f'https:{src}'
        if not src.startswith('http'):
            continue
        page_number = safe_int(node.get('data-page'), default=fallback_order, minimum=1)
        items.append((page_number, src))

    if not items:
        # 兜底：class 选择器失效时用正则（此时只能依赖文档顺序）。
        for fallback_order, src in enumerate(_READING_IMAGE_RE.findall(html_content), start=1):
            items.append((fallback_order, src))

    items.sort(key=lambda item: item[0])
    return [src for _, src in items]


def download_guazimanhua_chapter(source, chapter, folder, comic_format, task_id):
    save_dir = os.path.join(COMIC_ROOT, folder, chapter['filename_base'])

    from mangadock.services.download import (
        download_chapter_images,
        finalize_downloaded_chapter,
        incomplete_chapter_reason,
        note_chapter_finalize,
    )

    ensure_directory(save_dir)

    try:
        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"

        image_urls = load_guazimanhua_chapter_images(chapter['chapter_url'])
        image_jobs = [
            # 源端 URL 后缀是 .webp，字节实际是 JPEG（见模块 docstring），统一落 .jpg
            {'url': image_url, 'filename': f"{image_order:03d}.jpg"}
            for image_order, image_url in enumerate(image_urls, start=1)
            if image_url
        ]

        if not image_jobs:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 未找到图片"

        success_count, failed_items, cancelled = download_chapter_images(
            image_jobs,
            save_dir,
            referer=GUAZIMANHUA_SITE_REFERER,
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

        # 2026-09-24 P1：容忍缺页落盘的章节写 .incomplete 标记，更新时补回
        note_chapter_finalize(folder, chapter['filename_base'], len(failed_items))
        if failed_items:
            update_task(task_id, log=f"章节 {chapter['title']} 有 {len(failed_items)} 张图片下载失败")
        return True, f"章节 {chapter['title']} 处理完成"
    except Exception as exc:
        shutil.rmtree(save_dir, ignore_errors=True)
        return False, f"章节 {chapter['title']} 下载失败: {exc}"
