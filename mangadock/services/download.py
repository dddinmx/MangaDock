# -*- coding: utf-8 -*-
"""Source providers and chapter/book download pipeline."""
import base64
import concurrent.futures
import hashlib
import json
import os
import random
import re
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from natsort import natsorted

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import DownloadTask
from mangadock.settings import (
    COMIC_MAPPING_FILE,
    BAOZIMH_ORG_DEFAULT_IMAGE_HOST,
    BAOZIMH_ORG_ENCODED_IMAGE_TRANSLATION,
    BAOZIMH_ORG_IMAGE_HOST,
    COMIC_ROOT,
    CONFIG,
    COVER_ROOT,
    MAX_IMAGE_RESPONSE_BYTES,
    china_tz,
)
from mangadock.services.library import (
    chapter_output_exists,
    is_valid_local_chapter_file,
    get_local_chapter_bases,
    get_local_chapter_match_bases,
    is_existing_local_chapter,
    load_comic_mapping,
    normalize_comic_description,
    save_comic_description,
    save_comic_mapping,
    save_cover_image,
    target_extension,
)
from mangadock.services.tasks import get_task, is_task_cancel_requested, update_task
from mangadock.utils.http import (
    safe_http_get,
    should_verify_upstream_tls,
    write_limited_response_to_file,
)
from mangadock.utils.media import (
    build_pdf_from_image_list,
    default_headers,
    ensure_directory,
    repair_pdf_for_reading,
    sanitize_filename,
)


def safe_print(message, end="\n", flush=False):
    print(message, end=end, flush=flush)

# images_to_cbz_watch is in original between images_to_cbz and images_to_pdf - included in download body if needed


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


def detect_source_provider(url):
    host = urlparse(url).netloc.lower()
    if 'mxs12.cc' in host or 'wzd1.cc' in host:
        return 'mxs'
    if 'baozimh.org' in host:
        return 'baozimh_org'
    if 'baozimhcn.com' in host or 'baozimh.com' in host:
        return 'baozimhcn'
    raise ValueError("暂不支持该站点")


def extract_description_from_html(html_content, soup=None):
    """从详情页 HTML 抽取作品简介（多站点通用）。"""
    if soup is None and html_content:
        soup = BeautifulSoup(html_content, "html.parser")
    if soup is None:
        return ''

    candidates = []

    for prop in ('og:description', 'description', 'twitter:description'):
        if prop.startswith('og:') or prop.startswith('twitter:'):
            node = soup.find('meta', attrs={'property': prop}) or soup.find('meta', property=prop)
        else:
            node = soup.find('meta', attrs={'name': prop})
        if node and node.get('content'):
            candidates.append(node.get('content'))

    for selector in (
        '.comics-detail__desc',
        '.comics-detail__info',
        '.comic-detail__desc',
        '.manga-detail__desc',
        '.book-desc',
        '.intro',
        '#intro',
        '.description',
        '.summary',
        '[class*="detail__desc"]',
        '[class*="comics-detail__desc"]',
        'div.desc',
        'p.desc',
    ):
        try:
            nodes = soup.select(selector)
        except Exception:
            nodes = []
        for node in nodes[:3]:
            text = node.get_text(' ', strip=True)
            if text:
                candidates.append(text)

    # 优先选信息量更足的候选
    best = ''
    for raw in candidates:
        cleaned = normalize_comic_description(raw)
        if len(cleaned) > len(best):
            best = cleaned
    return best


def is_supported_comic_url(url):
    patterns = [
        r'^https://(?:cn\.baozimhcn\.com|(?:www\.)?baozimh\.com)/comic/[^/?#]+/?$',
        r'^https://(?:www\.)?baozimh\.org/manga/[^/?#]+/?$',
        r'^https://(?:www\.)?(?:mxs12|wzd1)\.cc/(?:book/)?[^/?#]+/?$',
    ]
    return any(re.match(pattern, url or '') for pattern in patterns)


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
        f"https://api-get-v3.mgsearcher.com/api/manga/get?mid={mid}&mode=all",
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


def load_mxs_source(url):
    response = safe_http_get(url, headers=default_headers(), timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    title_tag = soup.find("h1") or soup.find("title")
    title = sanitize_filename(title_tag.get_text(strip=True) if title_tag else "未知漫画")

    path_parts = [part for part in urlparse(url).path.split('/') if part]
    if 'book' in path_parts:
        book_index = path_parts.index('book')
        cover_id = path_parts[book_index + 1] if book_index + 1 < len(path_parts) else path_parts[-1]
    else:
        cover_id = path_parts[-1]

    links = soup.select('ul#detail-list-select li a')
    chapters = []
    for index, link in enumerate(links, start=1):
        href = link.get('href')
        if not href:
            continue
        chapters.append({
            'order': index,
            'title': link.get_text(strip=True) or f"第{index}章",
            'filename_base': f"{index:02d}",
            'chapter_url': urljoin(url, href)
        })

    return {
        'provider': 'mxs',
        'title': title,
        'cover_url': f"https://www.wzd1.cc/static/upload/book/{cover_id}/cover.jpg",
        'cover_verify': True,
        'description': extract_description_from_html(response.text, soup=soup),
        'chapters': chapters
    }


def load_baozimhcn_source(url):
    response = safe_http_get(url, headers=default_headers(), timeout=30)
    response.raise_for_status()
    response.encoding = 'utf-8'
    html_content = response.text

    soup = BeautifulSoup(html_content, "html.parser")
    title_tag = soup.find("h1", class_="comics-detail__title") or soup.find("title")
    title = sanitize_filename(title_tag.get_text(strip=True) if title_tag else "未知漫画")

    cover_match = re.search(
        r'<meta data-n-head="ssr" data-hid="og:image" name="og:image" content="(https?://[^"]+)"',
        html_content
    )

    chapter_max = 0
    chapter_slot = re.search(r'chapter_slot=(\d+)', html_content)
    if chapter_slot:
        chapter_max = int(chapter_slot.group(1))
    if chapter_max == 0:
        chapter_match = re.search(r'共(\d+)话', html_content)
        if chapter_match:
            chapter_max = int(chapter_match.group(1))
    if chapter_max == 0:
        chapter_links = re.findall(r'/comic/chapter/[^/]+/0_(\d+)\.html', html_content)
        if chapter_links:
            chapter_max = max(map(int, chapter_links)) + 1
    if chapter_max <= 0:
        raise ValueError("未获取到有效章节数")

    base_chapter_url = url.rstrip('/').replace("/comic/", "/comic/chapter/") + "/0_{}.html"
    chapters = [
        {
            'order': index + 1,
            'title': f"第{index + 1}章",
            'filename_base': f"{index + 1:02d}",
            'chapter_url': base_chapter_url.format(index)
        }
        for index in range(chapter_max)
    ]

    return {
        'provider': 'baozimhcn',
        'title': title,
        'cover_url': cover_match.group(1) if cover_match else None,
        'cover_verify': True,
        'description': extract_description_from_html(html_content, soup=soup),
        'chapters': chapters
    }


def load_comic_source(url):
    provider = detect_source_provider(url)
    if provider == 'mxs':
        return load_mxs_source(url)
    if provider == 'baozimh_org':
        return load_baozimh_org_source(url)
    return load_baozimhcn_source(url)


def persist_source_description(comic_name, source):
    """把 source['description'] 落到库里（下载/更新共用）。"""
    description = ''
    if isinstance(source, dict):
        description = source.get('description') or ''
    if not description:
        return False
    return save_comic_description(comic_name, description)


def refresh_comic_description(comic_name, source_url=None):
    """按 comic.json 源站 URL 重新抓取简介（用于旧书补全）。"""
    normalized_name = (comic_name or '').strip()
    if not normalized_name:
        return ''
    url = (source_url or '').strip() or load_comic_mapping().get(normalized_name)
    if not url:
        return ''
    try:
        source = load_comic_source(url)
        persist_source_description(normalized_name, source)
        return normalize_comic_description(source.get('description'))
    except Exception:
        return ''

def is_mxs_url(url):
    """判断是否为 mxs12.cc 网站的 URL"""
    return 'mxs12.cc' in url or 'wzd1.cc' in url


def title_mxs(url):
    """获取 mxs12.cc 漫画标题和总章节数"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        response = safe_http_get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html_content = response.text

        if not html_content.strip():
            raise Exception("获取到空的网页内容")

        soup = BeautifulSoup(html_content, "html.parser")
        title_tag = soup.find("h1")

        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else "未知漫画"

        mxs_description = extract_description_from_html(html_content, soup=soup)

        # 获取章节列表
        links = soup.select('ul#detail-list-select li a')
        chapter_max = len(links)

        # 获取封面图片 - 处理 /book/ 路径
        path_parts = url.strip('/').split('/')
        # 查找 book 后的 ID，或者直接取最后一段
        if 'book' in path_parts:
            book_index = path_parts.index('book')
            if book_index + 1 < len(path_parts):
                cid = path_parts[book_index + 1]
            else:
                cid = path_parts[-1]
        else:
            cid = path_parts[-1]
        cover_url = f"https://www.wzd1.cc/static/upload/book/{cid}/cover.jpg"
        try:
            response = safe_http_get(cover_url, timeout=10, max_bytes=MAX_IMAGE_RESPONSE_BYTES)
            if response.status_code == 200:
                if not os.path.exists(COVER_ROOT):
                    os.makedirs(COVER_ROOT)
                with open(os.path.join(COVER_ROOT, f"{title}.jpg"), 'wb') as f:
                    f.write(response.content)
                print(f"封面已保存到: {os.path.join(COVER_ROOT, f'{title}.jpg')}")
                try:
                    from mangadock.utils.cover_enhance import refresh_hero_cover
                    refresh_hero_cover(title)
                except Exception as enhance_exc:
                    print(f"封面超分缓存失败: {enhance_exc}")
        except Exception as e:
            print(f"下载封面时出错: {e}")

        safe_print(f"提取到的章节数={chapter_max}")
        if mxs_description:
            save_comic_description(str(title), mxs_description)

        return str(title), chapter_max, html_content

    except Exception as e:
        error_msg = f"获取漫画信息失败: {str(e)}"
        safe_print(error_msg)
        return "未知漫画", 0, ""


def title(url):
    """获取漫画标题和总章节数（自动识别网站）"""
    if is_mxs_url(url):
        return title_mxs(url)

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        response = safe_http_get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html_content = response.text

        if not html_content.strip():
            raise Exception("获取到空的网页内容")

        soup = BeautifulSoup(html_content, "html.parser")
        title_tag = soup.find("h1", class_="comics-detail__title")

        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else "未知漫画"

        pattern = r'<meta data-n-head="ssr" data-hid="og:image" name="og:image" content="(https?://[^"]+)"'
        match = re.search(pattern, html_content)
        if match:
            image_url = match.group(1)
            print(f"找到图片URL: {image_url}")
            try:
                response = safe_http_get(image_url, max_bytes=MAX_IMAGE_RESPONSE_BYTES)
                response.raise_for_status()
                # 保存图片到本地
                if not os.path.exists(COVER_ROOT):
                    os.makedirs(COVER_ROOT)
                with open(os.path.join(COVER_ROOT, f"{title}.jpg"), 'wb') as f:
                    f.write(response.content)
                print(f"图片已保存到: {os.path.join(COVER_ROOT, f'{title}.jpg')}")
                try:
                    from mangadock.utils.cover_enhance import refresh_hero_cover
                    refresh_hero_cover(title)
                except Exception as enhance_exc:
                    print(f"封面超分缓存失败: {enhance_exc}")
            except Exception as e:
                print(f"下载或保存图片时出错: {e}")

        chapter_max = 0
        chapter_slot = re.search(r'chapter_slot=(\d+)', html_content)
        if chapter_slot:
            chapter_max = int(chapter_slot.group(1))
        if chapter_max == 0:
            chapter_match = re.search(r'共(\d+)话', html_content)
            if chapter_match:
                chapter_max = int(chapter_match.group(1))
        if chapter_max == 0:
            chapter_links = re.findall(r'/comic/chapter/[^/]+/0_(\d+)\.html', html_content)
            if chapter_links:
                chapter_max = max(map(int, chapter_links)) + 1  # 加1因为章节通常从0开始

        safe_print(f"提取到的章节数={chapter_max}")
        safe_print(f"HTML片段包含chapter_slot? {('chapter_slot' in html_content)}")
        cn_description = extract_description_from_html(html_content, soup=soup)
        if cn_description:
            save_comic_description(str(title), cn_description)

        return str(title), chapter_max, html_content

    except Exception as e:
        error_msg = f"获取漫画信息失败: {str(e)}"
        safe_print(error_msg)
        return "未知漫画", 0, ""

def images_to_cbz(folder_path):
    """将图片转换为CBZ格式"""
    try:
        images = []
        for fname in os.listdir(folder_path):
            if fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                images.append(os.path.join(folder_path, fname))
        
        # 排序
        images = natsorted(images)
        if not images:
            return False, f"文件夹 {folder_path} 中没有图片"
        
        cbz_name = os.path.join(os.path.dirname(folder_path), f"{os.path.basename(folder_path)}.cbz")
        with zipfile.ZipFile(cbz_name, 'w') as cbz_file:
            for image_path in images:
                cbz_file.write(
                    image_path,
                    arcname=os.path.basename(image_path),
                    compress_type=zipfile.ZIP_STORED
                )
        return True, f"成功生成CBZ：{cbz_name}"
    except Exception as e:
        return False, f"CBZ生成失败：{str(e)}"
    
def images_to_cbz_watch(folder_path):
    try:
        single_image_path = os.path.join(COVER_ROOT, "bzmh.png")
        if not os.path.exists(single_image_path):
            return False, f"错误：图片文件不存在，请检查路径 -> {single_image_path}"
        cbz_name = os.path.join(folder_path, "00.cbz")
        with zipfile.ZipFile(cbz_name, 'w') as cbz_file:
            cbz_file.write(
                single_image_path,
                arcname=os.path.basename(single_image_path),
                compress_type=zipfile.ZIP_STORED
            )
        return True, f"成功生成 CBZ：{cbz_name}（包含图片：{os.path.basename(single_image_path)}）"
    except Exception as e:
        return False, f"CBZ 生成失败：{str(e)}"

def images_to_pdf(folder_path):
    """将图片转换为PDF格式，页尺寸跟随图片内容。"""
    try:
        images = []
        for fname in os.listdir(folder_path):
            if fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                images.append(os.path.join(folder_path, fname))

        images = natsorted(images)
        if not images:
            return False, f"文件夹 {folder_path} 中没有图片"

        pdf_name = os.path.join(os.path.dirname(folder_path), f"{os.path.basename(folder_path)}.pdf")

        return build_pdf_from_image_list(images, pdf_name)
    except Exception as e:
        return False, f"PDF生成失败：{str(e)}"

def download_image_mxs(session, img_url, save_path, retries=3, cancel_checker=None):
    """下载单张图片（mxs12.cc 专用）"""
    for attempt in range(retries):
        if cancel_checker and cancel_checker():
            return False
        try:
            safe_print(f"正在下载: {os.path.basename(save_path)}")
            with safe_http_get(
                img_url,
                stream=True,
                timeout=30,
                max_bytes=MAX_IMAGE_RESPONSE_BYTES,
                session_obj=session
            ) as response:
                if response.status_code == 200:
                    if cancel_checker and cancel_checker():
                        return False
                    write_limited_response_to_file(response, save_path, MAX_IMAGE_RESPONSE_BYTES)
                    return True
                else:
                    safe_print(f"图片请求失败，状态码: {response.status_code}，第{attempt+1}次重试。")
        except Exception as e:
            safe_print(f"图片请求失败，错误: {str(e)}，第{attempt+1}次重试。")
        if attempt < retries - 1:
            if cancel_checker and cancel_checker():
                return False
            time.sleep(2)
    safe_print(f"图片多次尝试失败: {os.path.basename(save_path)}")
    return False


def download_images_concurrently_mxs(session, img_urls, save_dir, max_workers=2, cancel_checker=None):
    """并发下载图片（mxs12.cc 专用）"""
    os.makedirs(save_dir, exist_ok=True)
    safe_print(f"开始下载 {len(img_urls)} 张图片到: {save_dir}")

    success_count = 0
    failed_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        job_iter = iter(enumerate(img_urls, start=1))

        def submit_next():
            try:
                img_idx, img_url = next(job_iter)
            except StopIteration:
                return False
            img_name = f"{img_idx:02d}.jpg"
            img_path = os.path.join(save_dir, img_name)
            future = executor.submit(
                download_image_mxs,
                session,
                img_url,
                img_path,
                3,
                cancel_checker
            )
            future_map[future] = img_url
            return True

        for _ in range(max_workers):
            if not submit_next():
                break

        while future_map:
            if cancel_checker and cancel_checker():
                executor.shutdown(wait=False, cancel_futures=True)
                return success_count, True

            done, _ = concurrent.futures.wait(
                future_map.keys(),
                timeout=0.5,
                return_when=concurrent.futures.FIRST_COMPLETED
            )
            if not done:
                continue

            for future in done:
                future_map.pop(future, None)
                if future.result():
                    success_count += 1
                else:
                    failed_count += 1
                submit_next()

    safe_print(f"图片下载完成: 成功 {success_count} 张，失败 {failed_count} 张")
    return success_count, False


def crawl_chapter_mxs(chapter_url, folder, chapter, comic_format, task_id):
    """下载单个章节（mxs12.cc 专用）"""
    save_dir = os.path.join(COMIC_ROOT, folder, f"{chapter:02d}")
    os.makedirs(save_dir, exist_ok=True)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0'
    }

    try:
        with requests.Session() as session:
            # 配置会话参数
            session.headers.update(headers)
            session.timeout = 30  # 增加超时时间

            for attempt in range(3):
                if is_task_cancel_requested(task_id):
                    shutil.rmtree(save_dir, ignore_errors=True)
                    return False, "任务已取消"
                try:
                    safe_print(f"正在访问章节 {chapter}: {chapter_url}")
                    response = safe_http_get(chapter_url, timeout=30, session_obj=session)
                    response.raise_for_status()

                    safe_print(f"章节 {chapter} 页面获取成功，状态码: {response.status_code}")
                    safe_print(f"页面大小: {len(response.text)} 字节")

                    soup = BeautifulSoup(response.text, 'html.parser')
                    img_tags = soup.find_all('img', class_='lazy')
                    img_urls = [img['data-original'] for img in img_tags if img.has_attr('data-original')]

                    safe_print(f"章节 {chapter} 找到 {len(img_urls)} 张图片")

                    if img_urls:
                        success_count, cancelled = download_images_concurrently_mxs(
                            session,
                            img_urls,
                            save_dir,
                            cancel_checker=lambda: is_task_cancel_requested(task_id)
                        )
                        if cancelled:
                            shutil.rmtree(save_dir, ignore_errors=True)
                            return False, "任务已取消"
                        safe_print(f"章节 {chapter} 图片下载成功: {success_count} 张")

                        if success_count > 0:
                            if is_task_cancel_requested(task_id):
                                shutil.rmtree(save_dir, ignore_errors=True)
                                return False, "任务已取消"
                            # 章节下载完成后压缩成 CBZ
                            cbz_path = os.path.join(COMIC_ROOT, folder, f"{chapter:02d}.cbz")
                            success, msg = images_to_cbz(save_dir)
                            if success:
                                safe_print(f"已压缩为: {os.path.basename(cbz_path)}")
                            else:
                                safe_print(f"压缩失败: {msg}")

                            # 删除原文件夹
                            shutil.rmtree(save_dir)

                            return True, f"章节 {chapter} 下载完成（成功 {success_count} 张）"
                        else:
                            safe_print(f"章节 {chapter} 图片下载全部失败")
                            shutil.rmtree(save_dir)
                    else:
                        safe_print(f"章节 {chapter} 未找到图片")

                except Exception as e:
                    safe_print(f"章节 {chapter} 第 {attempt + 1} 次尝试失败: {str(e)}")
                    if attempt < 2:
                        if is_task_cancel_requested(task_id):
                            shutil.rmtree(save_dir, ignore_errors=True)
                            return False, "任务已取消"
                        time.sleep(3)  # 增加重试间隔
                    else:
                        safe_print(f"章节 {chapter} 三次尝试都失败，放弃")

        return False, f"章节 {chapter} 下载失败"

    except Exception as e:
        safe_print(f"章节 {chapter} 下载异常: {str(e)}")
        return False, f"章节 {chapter} 下载失败: {str(e)}"


def download_image(session, base_url, save_dir, n, task_id, retries=CONFIG['retry_times']):
    """下载单张图片"""
    img_url = base_url.format(n)
    file_path = os.path.join(save_dir, f"{n}.jpg")

    for attempt in range(retries):
        if is_task_cancel_requested(task_id):
            return False, n, True
        time.sleep(random.uniform(*CONFIG['delay_range']))
        try:
            with safe_http_get(
                img_url,
                stream=True,
                timeout=15,
                max_bytes=MAX_IMAGE_RESPONSE_BYTES,
                session_obj=session
            ) as response:
                if response.status_code == 200:
                    if is_task_cancel_requested(task_id):
                        return False, n, True
                    write_limited_response_to_file(response, file_path, MAX_IMAGE_RESPONSE_BYTES)
                    return True, n, False
                else:
                    if response.status_code == 404:
                        return False, n, True
                    else:
                        safe_print(f"图片{n} 下载失败，第{attempt+1}次重试。")
        except Exception as e:
            safe_print(f"图片{n} 下载失败，第{attempt+1}次重试。")

        if attempt < retries - 1:
            if is_task_cancel_requested(task_id):
                return False, n, True
            time.sleep(2 ** attempt)

    return False, n, False

def crawl_chapter(chapter_url, folder, chapter, comic_format, task_id):
    """下载单��章��"""
    save_dir = os.path.join(COMIC_ROOT, folder, f"{chapter:02d}")
    os.makedirs(save_dir, exist_ok=True)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        with requests.Session() as session:
            for attempt in range(3):
                if is_task_cancel_requested(task_id):
                    shutil.rmtree(save_dir, ignore_errors=True)
                    return False, "任务已取消"
                try:
                    response = safe_http_get(chapter_url, headers=headers, timeout=10, session_obj=session)
                    # 如果成功获取200响应，直接返回成功
                    if response.status_code == 200:
                        break
                    # 非200状态码，记录并继续重试
                    safe_print(f"章节页访问失败，状态码: {response.status_code}，第{attempt+1}次尝试")
                except Exception as e:
                    safe_print(f"章节页访问异常: {str(e)}，第{attempt+1}次尝试")
            else:
                # 当循环完成且未通过break退出时，说明3次尝试都失败
                return False, f"章节页经过3次尝试后仍访问失败"

            match = re.search(r'(https?://[^/]+/scomic/[^/]+/\d+/[^/]+/1\.jpg)', response.text)
            if not match:
                return False, "未找到图片地址"
            
            base_url = match.group(1).replace("1.jpg", "{}.jpg")
            update_task(task_id, log=f"开始下载章节：{chapter}")

            max_workers = CONFIG['max_workers']
            success_count = 0
            n = 1
            stop_flag = False
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                while not stop_flag:
                    # 检查任务是否已取消
                    if is_task_cancel_requested(task_id):
                        executor.shutdown(wait=False)
                        return False, "任务已取消"
                        
                    futures = []
                    for _ in range(max_workers * 2):
                        futures.append(executor.submit(
                            download_image, session, base_url, save_dir, n, task_id
                        ))
                        n += 1
                    
                    for future in as_completed(futures):
                        if is_task_cancel_requested(task_id):
                            executor.shutdown(wait=False)
                            return False, "任务已取消"
                            
                        success, num, stop_download = future.result()
                        if success:
                            success_count += 1
                        else:
                            if stop_download:
                                stop_flag = True
                                executor.shutdown(wait=False)
                                break

                    time.sleep(random.uniform(*CONFIG['delay_range']))

            if is_task_cancel_requested(task_id):
                executor.shutdown(wait=False)
                return False, "任务已取消"

            if comic_format == 1:
                update_task(task_id, log=f"章节 {chapter} 下载完成，开始生成PDF...")
                success, msg = images_to_pdf(save_dir)
            else:
                update_task(task_id, log=f"章节 {chapter} 下载完成，开始生成CBZ...")
                success, msg = images_to_cbz(save_dir)
            
            update_task(task_id, log=msg)
            
            if os.path.isdir(save_dir):
                try:
                    shutil.rmtree(save_dir)
                except Exception as e:
                    update_task(task_id, log=f"删除临时目录时发生错误: {e}")
            
            if success:
                return True, f"章节 {chapter} 处理完成"
            else:
                return False, f"章节 {chapter} 处理失败: {msg}"
                
    except Exception as e:
        error_msg = f"章节处理异常：{str(e)}"
        update_task(task_id, log=error_msg)
        return False, error_msg


def extract_image_extension(image_url, default_ext=".jpg"):
    ext = os.path.splitext(urlparse(image_url).path)[1].lower()
    return ext if ext in {'.jpg', '.jpeg', '.png', '.webp', '.gif'} else default_ext


def download_binary_image(image_url, save_path, referer=None, verify=True, retries=None, cancel_checker=None):
    retries = retries or CONFIG['retry_times']
    headers = default_headers(referer=referer)

    for attempt in range(retries):
        if cancel_checker and cancel_checker():
            return False, "cancelled"
        try:
            response = safe_http_get(
                image_url,
                headers=headers,
                stream=True,
                timeout=30,
                verify=verify,
                max_bytes=MAX_IMAGE_RESPONSE_BYTES
            )
            if response.status_code == 200:
                if cancel_checker and cancel_checker():
                    return False, "cancelled"
                write_limited_response_to_file(response, save_path, MAX_IMAGE_RESPONSE_BYTES)
                return True, save_path
            safe_print(f"图片下载失败，状态码: {response.status_code}，第{attempt + 1}次重试")
        except Exception as exc:
            safe_print(f"图片下载异常: {exc}，第{attempt + 1}次重试")

        if attempt < retries - 1:
            if cancel_checker and cancel_checker():
                return False, "cancelled"
            time.sleep(2)

    return False, image_url


def download_chapter_images(image_jobs, save_dir, referer=None, verify=True, max_workers=None, cancel_checker=None):
    ensure_directory(save_dir)
    success_count = 0
    failed_items = []
    max_workers = max_workers or CONFIG['max_workers']

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        job_iter = iter(image_jobs)

        def submit_next():
            try:
                job = next(job_iter)
            except StopIteration:
                return False
            future = executor.submit(
                download_binary_image,
                job['url'],
                os.path.join(save_dir, job['filename']),
                referer,
                verify,
                None,
                cancel_checker
            )
            future_map[future] = job
            return True

        for _ in range(max_workers):
            if not submit_next():
                break

        while future_map:
            if cancel_checker and cancel_checker():
                executor.shutdown(wait=False, cancel_futures=True)
                return success_count, failed_items, True

            done, _ = concurrent.futures.wait(
                future_map.keys(),
                timeout=0.5,
                return_when=concurrent.futures.FIRST_COMPLETED
            )
            if not done:
                continue

            for future in done:
                job = future_map.pop(future)
                success, result = future.result()
                if success:
                    success_count += 1
                elif result != "cancelled":
                    failed_items.append(job['url'])
                submit_next()

    return success_count, failed_items, False


def finalize_downloaded_chapter(save_dir, comic_format):
    if comic_format == 1:
        return images_to_pdf(save_dir)
    return images_to_cbz(save_dir)


def download_mxs_chapter(chapter, folder, comic_format, task_id):
    save_dir = os.path.join(COMIC_ROOT, folder, chapter['filename_base'])
    ensure_directory(save_dir)

    try:
        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        response = safe_http_get(chapter['chapter_url'], headers=default_headers(), timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        image_urls = [
            urljoin(chapter['chapter_url'], img['data-original'])
            for img in soup.find_all('img', class_='lazy')
            if img.has_attr('data-original')
        ]
        if not image_urls:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 未找到图片"

        image_jobs = [
            {
                'url': image_url,
                'filename': f"{index:03d}{extract_image_extension(image_url)}"
            }
            for index, image_url in enumerate(image_urls, start=1)
        ]
        success_count, failed_items, cancelled = download_chapter_images(
            image_jobs,
            save_dir,
            referer=chapter['chapter_url'],
            verify=True,
            cancel_checker=lambda: is_task_cancel_requested(task_id)
        )
        if cancelled:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        if success_count == 0:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 下载失败"

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


def load_baozimh_org_chapter_images(mid, chapter_id):
    response = safe_http_get(
        f"https://api-get-v3.mgsearcher.com/api/v2/chapter/getinfo?m={mid}&c={chapter_id}",
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
        if success_count == 0:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 下载失败"

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


def download_provider_chapter(source, chapter, folder, comic_format, task_id):
    if source['provider'] == 'mxs':
        return download_mxs_chapter(chapter, folder, comic_format, task_id)
    if source['provider'] == 'baozimh_org':
        return download_baozimh_org_chapter(source, chapter, folder, comic_format, task_id)
    return crawl_chapter(chapter['chapter_url'], folder, chapter['order'], comic_format, task_id)

def download_complete_book_mxs(url, comic_format, task_id):
    """下载整本漫画（mxs12.cc 专用）"""
    try:
        update_task(task_id, status='running')

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        folder, chapter_max, html_content = title(url)
        update_task(task_id, comic_name=folder)
        update_task(task_id, log=f"开始下载漫画: {folder}")
        update_task(task_id, log=f"总章节数: {chapter_max}")

        if chapter_max <= 0:
            update_task(task_id, log="错误：未获取到有效的章节数，无法开始下载")
            update_task(task_id, log=f"请检查URL是否正确: {url}")
            update_task(task_id, status='error')
            return

        update_task(task_id, total_chapters=chapter_max)

        # 解析章节链接
        soup = BeautifulSoup(html_content, 'html.parser')
        links = soup.select('ul#detail-list-select li a')
        base_url = "https://mxs12.cc"
        chapter_urls = [base_url + a['href'] for a in links]

        json_file_path = COMIC_MAPPING_FILE
        try:
            if os.path.exists(json_file_path):
                if os.path.getsize(json_file_path) > 0:
                    with open(json_file_path, "r", encoding="utf-8") as json_file:
                        existing_data = json.load(json_file)
                else:
                    existing_data = {}  # 空文件时初始化空字典
            else:
                existing_data = {}

            existing_data[folder] = url
            with open(json_file_path, "w", encoding="utf-8") as json_file:
                json.dump(existing_data, json_file, ensure_ascii=False, indent=4)
        except json.JSONDecodeError:
            error_msg = f"JSON文件格式错误，已创建新文件: {json_file_path}"
            update_task(task_id, log=error_msg)
            with open(json_file_path, "w", encoding="utf-8") as json_file:
                json.dump({folder: url}, json_file, ensure_ascii=False, indent=4)
        except Exception as e:
            error_msg = f"处理JSON文件失败: {str(e)}"
            update_task(task_id, log=error_msg)

        comic_path = os.path.join(COMIC_ROOT, folder)
        if not os.path.exists(comic_path):
            os.makedirs(comic_path)

        for idx, chapter_url in enumerate(chapter_urls, start=1):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            update_task(task_id, log=f"开始处理第 {idx} 章")
            success, msg = crawl_chapter_mxs(chapter_url, folder, idx, comic_format, task_id)
            update_task(task_id, log=msg)

            if task:
                new_completed = task.completed_chapters + 1
                progress = int((new_completed / chapter_max) * 100)
                update_task(task_id, completed_chapters=new_completed, progress_percent=progress)
            time.sleep(2)  # 减小延迟，避免被封禁

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有章节处理完成")

    except Exception as e:
        error_msg = f"下载过程出错: {str(e)}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))


def download_complete_book(url, comic_format, task_id):
    """下载整本漫画（统一站点任务流）"""
    try:
        update_task(task_id, status='running')

        source = load_comic_source(url)
        folder = source['title']
        chapters = source['chapters']

        update_task(task_id, comic_name=folder)
        update_task(task_id, log=f"开始下载漫画: {folder}")
        update_task(task_id, log=f"站点类型: {source['provider']}")
        update_task(task_id, total_chapters=len(chapters))

        if not chapters:
            raise ValueError("未获取到任何章节")

        ensure_directory(os.path.join(COMIC_ROOT, folder))
        save_comic_mapping(folder, url)
        save_cover_image(
            folder,
            source.get('cover_url'),
            referer=url,
            verify=source.get('cover_verify', True)
        )
        if persist_source_description(folder, source):
            update_task(task_id, log="已保存作品简介")
        elif source.get('description'):
            update_task(task_id, log="作品简介保存失败，可稍后在详情页重试补全")

        for index, chapter in enumerate(chapters, start=1):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            update_task(task_id, log=f"开始处理章节: {chapter['title']}")
            success, message = download_provider_chapter(source, chapter, folder, comic_format, task_id)
            update_task(task_id, log=message)
            if not success:
                update_task(task_id, status='error', end_time=datetime.now(china_tz))
                return

            progress = int((index / len(chapters)) * 100)
            update_task(task_id, completed_chapters=index, progress_percent=progress)

            if source['provider'] == 'baozimhcn':
                time.sleep(2)

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有章节处理完成")
    except Exception as exc:
        error_msg = f"下载过程出错: {exc}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))

def update_comic(comic_name, comic_format, task_id):
    try:
        update_task(task_id, status='running')
        update_task(task_id, comic_name=comic_name)
        update_task(task_id, log=f"开始更新漫画: {comic_name}")

        comic_path = os.path.join(COMIC_ROOT, comic_name)
        if not os.path.isdir(comic_path):
            raise ValueError(f"漫画目录不存在: {comic_path}")

        json_file_path = COMIC_MAPPING_FILE
        if not os.path.exists(json_file_path) or os.path.getsize(json_file_path) == 0:
            raise ValueError("comic.json文件不存在或为空")

        with open(json_file_path, "r", encoding="utf-8") as json_file:
            comic_mapping = json.load(json_file)

        update_url = comic_mapping.get(comic_name)
        if not update_url:
            raise ValueError(f"漫画 {comic_name} 的URL信息不存在于JSON文件中")

        update_task(task_id, url=update_url)
        source = load_comic_source(update_url)
        save_cover_image(
            comic_name,
            source.get('cover_url'),
            referer=update_url,
            verify=source.get('cover_verify', True)
        )
        if persist_source_description(comic_name, source):
            update_task(task_id, log="已同步作品简介")

        existing_outputs = {
            os.path.splitext(filename)[0]
            for filename in os.listdir(comic_path)
            if is_valid_local_chapter_file(filename, (f".{target_extension(comic_format)}",))
        }
        existing_match_bases = get_local_chapter_match_bases(comic_name)
        chapters_to_download = [
            chapter for chapter in source['chapters']
            if not is_existing_local_chapter(chapter, existing_match_bases)
        ]

        update_task(task_id, log=f"远端章节总数: {len(source['chapters'])}")
        update_task(task_id, log=f"目标格式已存在章节数: {len(existing_outputs)}")

        if not chapters_to_download:
            update_task(task_id, log="未找到更新，当前已是最新版本")
            update_task(task_id, status='completed', end_time=datetime.now(china_tz))
            return

        update_task(task_id, total_chapters=len(chapters_to_download))
        update_task(task_id, log=f"找到 {len(chapters_to_download)} 个新章节，开始下载")

        for index, chapter in enumerate(chapters_to_download, start=1):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            update_task(task_id, log=f"开始处理章节: {chapter['title']}")
            success, message = download_provider_chapter(source, chapter, comic_name, comic_format, task_id)
            update_task(task_id, log=message)
            if not success:
                update_task(task_id, status='error', end_time=datetime.now(china_tz))
                return

            progress = int((index / len(chapters_to_download)) * 100)
            update_task(task_id, completed_chapters=index, progress_percent=progress)

            if source['provider'] == 'baozimhcn':
                time.sleep(2)

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有更新章节处理完成")

    except Exception as e:
        error_msg = f"更新过程出错: {str(e)}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))
