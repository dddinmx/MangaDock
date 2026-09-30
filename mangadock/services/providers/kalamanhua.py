# -*- coding: utf-8 -*-
"""卡拉漫画 (kalamanhua.com) provider：源解析 + 单章下载。

站点结构（2026-09-30 实测，苹果 CMS + Cloudflare 图床 image.kalaimg.top）：

1. 作品页 ``https://www.kalamanhua.com/comic/<id>.html``
   - 标题在 ``h1.title``；封面在 ``.stui-content__thumb img[data-original]``；
   - 章节按线路分 ``#playlistN``（I 线 sid=1、J 线 sid=2 …）。各线路话数
     可能不同，取**话数最多**的一条；并列时取 sid 较小的。
   - 章节链接 ``/chapter/<id>-<sid>-<nid>.html``，页面顺序即阅读顺序。
2. 章节页内联 MacCMS ``player_aaaa``：``encrypt: 3`` 的 ``url`` 是一段 hex，
   **player.js 并不解码**（只处理 encrypt 1/2），直接拼进播放器 iframe：
   ``https://image.kalaimg.top/play/min/<hex>``。
3. 主站和图床都有 ``omo_token`` 点击门：HTML 里带 token，设 cookie 后再请求
   才给正文。图床 token 每次都会换；图片直链必须带本次的 ``?cf_verify=``，
   去掉后会 403。
4. 阅读页 ``img.comic-img[data-src]`` 给出 ``/image/index/<hex>``。
   ``Content-Type: image/jpeg``，字节实际是 **WebP**（``RIFF…WEBP``），
   本地文件名写 ``.webp``。封面同域 ``/image/cover/…``，无门可直下。
5. 站点以韩漫为主，按 18+ 源登记（与 MXS 相同门控）。

依赖 download.py 共享管线的函数在函数体内延迟导入，避免循环导入。
"""
import json
import os
import re
import shutil
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from mangadock.services.providers.common import extract_description_from_html
from mangadock.services.tasks import is_task_cancel_requested, update_task
from mangadock.settings import COMIC_ROOT
from mangadock.utils.http import safe_http_get
from mangadock.utils.media import default_headers, ensure_directory, sanitize_filename

KALAMANHUA_SITE_BASE = 'https://www.kalamanhua.com'
KALAMANHUA_SITE_REFERER = 'https://www.kalamanhua.com/'
KALAIMG_BASE = 'https://image.kalaimg.top'
KALAIMG_PLAY_BASE = f'{KALAIMG_BASE}/play/min'

_COMIC_ID_RE = re.compile(
    r'^https?://(?:www\.)?kalamanhua\.com/comic/(\d+)\.html(?:[?#].*)?$',
    re.I,
)
_CHAPTER_HREF_RE = re.compile(r'/chapter/(\d+)-(\d+)-(\d+)\.html', re.I)
_PLAYER_AAAA_RE = re.compile(r'var\s+player_aaaa\s*=\s*(\{.*?\})\s*;', re.S)
_OMO_TOKEN_RE = re.compile(r"(?:const|var|let)\s+token\s*=\s*'([^']+)'")
_HEX_KEY_RE = re.compile(r'^[0-9a-f]{32,}$', re.I)


def is_kalamanhua_url(url):
    return bool(_COMIC_ID_RE.match((url or '').strip()))


def _extract_comic_id(url):
    match = _COMIC_ID_RE.match((url or '').strip())
    if not match:
        raise ValueError('未找到卡拉漫画作品 ID')
    return match.group(1)


def canonical_comic_url(url):
    comic_id = _extract_comic_id(url)
    return f'{KALAMANHUA_SITE_BASE}/comic/{comic_id}.html'


def _extract_omo_token(html_content):
    match = _OMO_TOKEN_RE.search(html_content or '')
    return match.group(1) if match else None


def _is_omo_gate(html_content):
    """点击验证 / 图床跳转页。正文（作品页、章节页、阅读器）都不当成门。"""
    text = html_content or ''
    if 'player_aaaa' in text or 'stui-content' in text or 'comic-container' in text:
        return False
    return bool(_extract_omo_token(text))


def _has_page_content(html_content):
    text = html_content or ''
    return ('player_aaaa' in text) or ('stui-content' in text) or ('comic-container' in text)


def _absolute_url(src, base):
    src = (src or '').strip()
    if not src:
        return ''
    if src.startswith('//'):
        return f'https:{src}'
    if src.startswith('http://') or src.startswith('https://'):
        return src
    return urljoin(base if base.endswith('/') else base + '/', src.lstrip('/'))


def _with_cf_verify(url, token):
    if not url or not token or 'cf_verify=' in url:
        return url
    joiner = '&' if '?' in url else '?'
    return f'{url}{joiner}cf_verify={token}'


def _fetch_html(url, referer=None):
    """过 omo_token 门后返回 (html, token)。token 可能为空（无门的页面）。

    主站点击验证页实测会回 HTTP 403（正文仍是带 token 的「安全验证」HTML），
    所以不能按状态码直接 raise，要先认门再带 cookie 重试。
    """
    headers = default_headers(referer=referer)
    response = safe_http_get(url, headers=headers, timeout=30)
    response.encoding = response.encoding or 'utf-8'
    html_content = response.text
    token = _extract_omo_token(html_content)
    if not (token and _is_omo_gate(html_content)):
        response.raise_for_status()
        return html_content, token

    gated_headers = dict(headers)
    gated_headers['Cookie'] = f'omo_token={token}'
    response = safe_http_get(url, headers=gated_headers, timeout=30)
    response.encoding = response.encoding or 'utf-8'
    html_content = response.text
    if _has_page_content(html_content):
        return html_content, token

    joiner = '&' if '?' in url else '?'
    response = safe_http_get(
        f'{url}{joiner}cf_verify={token}',
        headers=gated_headers,
        timeout=30,
    )
    response.encoding = response.encoding or 'utf-8'
    html_content = response.text
    if _has_page_content(html_content):
        return html_content, token
    response.raise_for_status()
    raise ValueError('卡拉漫画访问验证失败')


def _parse_playlists(html_content, soup, comic_id):
    """从 #playlistN 取出章节，返回话数最多的一条（并列取 sid 较小）。"""
    grouped = {}
    containers = soup.select('[id^=playlist]')
    if not containers:
        containers = [soup]

    for container in containers:
        for anchor in container.find_all('a', href=True):
            match = _CHAPTER_HREF_RE.search(anchor['href'])
            if not match:
                continue
            found_id, sid, nid = match.group(1), int(match.group(2)), int(match.group(3))
            if found_id != comic_id:
                continue
            grouped.setdefault(sid, [])
            grouped[sid].append((nid, anchor.get_text(' ', strip=True)))

    if not grouped:
        for href, text in re.findall(
            r'href="(/chapter/\d+-\d+-\d+\.html)"[^>]*>([^<]*)</a>',
            html_content or '',
        ):
            match = _CHAPTER_HREF_RE.search(href)
            if not match:
                continue
            found_id, sid, nid = match.group(1), int(match.group(2)), int(match.group(3))
            if found_id != comic_id:
                continue
            grouped.setdefault(sid, [])
            grouped[sid].append((nid, text.strip()))

    best_sid = None
    best_pairs = []
    for sid, pairs in grouped.items():
        seen = set()
        unique = []
        for nid, title in pairs:
            if nid in seen:
                continue
            seen.add(nid)
            unique.append((nid, title))
        unique.sort(key=lambda item: item[0])
        if best_sid is None or len(unique) > len(best_pairs) or (
            len(unique) == len(best_pairs) and sid < best_sid
        ):
            best_sid = sid
            best_pairs = unique

    chapters = []
    for index, (nid, text) in enumerate(best_pairs, start=1):
        label = text or f'第{index}话'
        chapters.append({
            'order': index,
            'title': label,
            'filename_base': f'{index:04d}_{sanitize_filename(label)}',
            'chapter_url': f'{KALAMANHUA_SITE_BASE}/chapter/{comic_id}-{best_sid}-{nid}.html',
            'comic_id': comic_id,
            'sid': best_sid,
            'nid': nid,
        })
    return chapters


def load_kalamanhua_source(url):
    comic_id = _extract_comic_id(url)
    canonical_url = f'{KALAMANHUA_SITE_BASE}/comic/{comic_id}.html'
    html_content, _token = _fetch_html(canonical_url, referer=KALAMANHUA_SITE_REFERER)
    soup = BeautifulSoup(html_content, 'html.parser')

    heading = soup.find('h1')
    title_source = (
        (heading.get_text(' ', strip=True) if heading else '')
        or (soup.title.get_text(strip=True) if soup.title else '')
        or '未知漫画'
    )
    title_source = re.split(r'\s*[_\-|｜]\s*', title_source, maxsplit=1)[0]
    title_source = title_source.replace('漫画免费在线免费下拉观看', '').strip() or title_source

    cover_url = ''
    thumb = soup.select_one('.stui-content__thumb img[data-original], .stui-content__thumb img[data-src]')
    if thumb is not None:
        cover_url = _absolute_url(
            thumb.get('data-original') or thumb.get('data-src') or thumb.get('src'),
            KALAMANHUA_SITE_BASE,
        )
    if cover_url and 'load.gif' in cover_url:
        cover_url = ''

    description = ''
    detail = soup.select_one('.detail-content')
    if detail is not None:
        description = detail.get_text(' ', strip=True)
    if not description:
        description = extract_description_from_html(html_content, soup=soup)

    chapters = _parse_playlists(html_content, soup, comic_id)
    if not chapters:
        raise ValueError('未获取到有效章节列表')

    return {
        'provider': 'kalamanhua',
        'title': sanitize_filename(title_source),
        'cover_url': cover_url or None,
        'cover_verify': True,
        'description': description,
        'chapters': chapters,
        'comic_id': comic_id,
    }


def _play_min_url(player_url):
    value = (player_url or '').strip()
    if not value:
        return ''
    if value.startswith('http://') or value.startswith('https://'):
        return value
    if value.startswith('/play/'):
        return _absolute_url(value, KALAIMG_BASE)
    if _HEX_KEY_RE.match(value):
        return f'{KALAIMG_PLAY_BASE}/{value}'
    return _absolute_url(value, KALAIMG_PLAY_BASE)


def _parse_player_aaaa(html_content):
    match = _PLAYER_AAAA_RE.search(html_content or '')
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def load_kalamanhua_chapter_images(chapter_url):
    """取某章全部图片直链（含本次图床 cf_verify）。"""
    html_content, _token = _fetch_html(chapter_url, referer=KALAMANHUA_SITE_REFERER)
    player = _parse_player_aaaa(html_content)
    if not player or not player.get('url'):
        raise ValueError('章节页未找到播放地址')

    play_url = _play_min_url(player.get('url'))
    if not play_url:
        raise ValueError('章节播放地址无效')

    reader_html, reader_token = _fetch_html(play_url, referer=chapter_url)
    soup = BeautifulSoup(reader_html, 'html.parser')
    image_urls = []
    for node in soup.select('img.comic-img'):
        src = (node.get('data-src') or node.get('src') or '').strip()
        if not src or src.startswith('data:'):
            continue
        image_url = _absolute_url(src, KALAIMG_BASE)
        parsed = urlsplit(image_url)
        if parsed.path.startswith('/image/index/'):
            image_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))
        image_url = _with_cf_verify(image_url, reader_token)
        if image_url.startswith('https://'):
            image_urls.append(image_url)

    if not image_urls:
        raise ValueError('章节阅读页未找到图片')
    return image_urls


def download_kalamanhua_chapter(source, chapter, folder, comic_format, task_id):
    save_dir = os.path.join(COMIC_ROOT, folder, chapter['filename_base'])

    from mangadock.services.download import (
        download_chapter_images,
        extract_image_extension,
        finalize_downloaded_chapter,
        incomplete_chapter_reason,
        note_chapter_finalize,
    )

    ensure_directory(save_dir)

    try:
        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"

        image_urls = load_kalamanhua_chapter_images(chapter['chapter_url'])
        image_jobs = [
            {
                'url': image_url,
                'filename': f"{image_order:03d}{extract_image_extension(image_url, '.webp')}",
            }
            for image_order, image_url in enumerate(image_urls, start=1)
            if image_url
        ]

        if not image_jobs:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 未找到图片"

        success_count, failed_items, cancelled = download_chapter_images(
            image_jobs,
            save_dir,
            referer=KALAIMG_BASE + '/',
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

        note_chapter_finalize(folder, chapter['filename_base'], len(failed_items))
        if failed_items:
            update_task(task_id, log=f"章节 {chapter['title']} 有 {len(failed_items)} 张图片下载失败")
        return True, f"章节 {chapter['title']} 处理完成"
    except Exception as exc:
        shutil.rmtree(save_dir, ignore_errors=True)
        return False, f"章节 {chapter['title']} 下载失败: {exc}"
