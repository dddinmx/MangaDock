# -*- coding: utf-8 -*-
"""Local comic library: scan roots, identity, chapters, cache."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime

from flask import url_for
from natsort import natsorted
from PIL import Image

from mangadock.core import API_PAGE_CACHE_ROOT, app
from mangadock.extensions import (
    comic_directory_scan_cache,
    comic_root_listing_cache,
    comics_cache,
    db,
)
from mangadock.models import ComicIdentity, ComicScanPath, DownloadTask
from mangadock.settings import (
    CONFIG,
    COMIC_MAPPING_FILE,
    COMIC_ROOT,
    COVER_ROOT,
    MAX_ARCHIVE_ENTRY_BYTES,
    MAX_ARCHIVE_PAGES,
    MAX_ARCHIVE_TOTAL_BYTES,
    MAX_IMAGE_RESPONSE_BYTES,
    MAX_PDF_PAGES,
    PDF_TOOL_TIMEOUT_SECONDS,
    china_tz,
)
from mangadock.utils.files import resolve_file_under_directory
from mangadock.utils.http import safe_http_get, should_verify_upstream_tls, write_limited_response_to_file
from mangadock.utils.media import default_headers, ensure_directory, repair_pdf_for_reading, sanitize_filename

comic_scan_cache_lock = threading.Lock()
comics_cache_lock = threading.Lock()

def safe_print(message, end="\n", flush=False):
    print(message, end=end, flush=flush)

def normalize_scan_path(scan_path):
    cleaned_path = (scan_path or '').strip().strip('"').strip("'")
    if not cleaned_path:
        return None
    expanded_path = os.path.expandvars(os.path.expanduser(cleaned_path))
    return os.path.abspath(os.path.normpath(expanded_path))


def get_comic_scan_roots(existing_only=False):
    roots = []
    seen_roots = set()

    default_root = normalize_scan_path(COMIC_ROOT)
    if default_root:
        seen_roots.add(default_root)
        if not existing_only or os.path.isdir(default_root):
            roots.append(default_root)

    with app.app_context():
        custom_paths = ComicScanPath.query.filter_by(enabled=True).order_by(ComicScanPath.created_at.asc()).all()

    for scan_path in custom_paths:
        normalized_path = normalize_scan_path(scan_path.path)
        if not normalized_path or normalized_path in seen_roots:
            continue
        seen_roots.add(normalized_path)
        if existing_only and not os.path.isdir(normalized_path):
            continue
        roots.append(normalized_path)

    return roots


def get_scan_path_entries():
    entries = []
    default_root = normalize_scan_path(COMIC_ROOT)
    if default_root:
        entries.append({
            'id': None,
            'path': default_root,
            'enabled': True,
            'is_default': True,
            'exists': os.path.isdir(default_root),
        })

    with app.app_context():
        custom_paths = ComicScanPath.query.order_by(ComicScanPath.created_at.asc()).all()

    for scan_path in custom_paths:
        normalized_path = normalize_scan_path(scan_path.path)
        entries.append({
            'id': scan_path.id,
            'path': normalized_path or scan_path.path,
            'enabled': bool(scan_path.enabled),
            'is_default': False,
            'exists': bool(normalized_path and os.path.isdir(normalized_path)),
        })

    return entries


def is_safe_comic_name(comic_name):
    normalized_name = (comic_name or '').strip()
    return bool(
        normalized_name
        and normalized_name not in {'.', '..'}
        and '/' not in normalized_name
        and '\\' not in normalized_name
    )


def get_comic_directory(comic_name):
    if not is_safe_comic_name(comic_name):
        return None

    normalized_name = comic_name.strip()
    for root in get_comic_scan_roots(existing_only=True):
        comic_path = os.path.join(root, normalized_name)
        if os.path.isdir(comic_path):
            return comic_path
    return None


def iter_local_comic_directories():
    seen_comics = set()
    for root in get_comic_scan_roots(existing_only=True):
        for comic_name, comic_path in list_cached_root_directories(root):
            if comic_name in seen_comics:
                continue
            seen_comics.add(comic_name)
            yield comic_name, comic_path


def generate_comic_id():
    return f"c_{uuid.uuid4().hex[:12]}"


def sync_comic_identity_records(comic_names=None):
    candidate_names = []
    if comic_names is None:
        candidate_names.extend(comic_name for comic_name, _comic_path in iter_local_comic_directories())
        with app.app_context():
            candidate_names.extend(
                comic_name
                for (comic_name,) in db.session.query(DownloadTask.comic_name)
                .filter(DownloadTask.comic_name.isnot(None))
                .distinct()
                .all()
                if comic_name
            )
    else:
        candidate_names.extend(comic_names)

    normalized_names = []
    seen_names = set()
    for comic_name in candidate_names:
        normalized_name = (comic_name or '').strip()
        if not is_safe_comic_name(normalized_name) or normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)
        normalized_names.append(normalized_name)

    if not normalized_names:
        return {}

    with app.app_context():
        existing_records = {
            identity.comic_name: identity
            for identity in ComicIdentity.query.filter(ComicIdentity.comic_name.in_(normalized_names)).all()
        }
        has_changes = False
        now = datetime.now(china_tz)

        for comic_name in normalized_names:
            identity = existing_records.get(comic_name)
            if not identity:
                identity = ComicIdentity(
                    comic_id=generate_comic_id(),
                    comic_name=comic_name,
                    created_at=now,
                    updated_at=now
                )
                db.session.add(identity)
                existing_records[comic_name] = identity
                has_changes = True
            elif not identity.comic_id:
                identity.comic_id = generate_comic_id()
                identity.updated_at = now
                has_changes = True

        if has_changes:
            try:
                db.session.commit()
            except Exception:
                # Concurrent cache refresh / workers may insert the same identity first.
                db.session.rollback()
                existing_records = {
                    identity.comic_name: identity
                    for identity in ComicIdentity.query.filter(
                        ComicIdentity.comic_name.in_(normalized_names)
                    ).all()
                }
                for comic_name in normalized_names:
                    if comic_name in existing_records:
                        continue
                    identity = ComicIdentity(
                        comic_id=generate_comic_id(),
                        comic_name=comic_name,
                        created_at=now,
                        updated_at=now
                    )
                    db.session.add(identity)
                    existing_records[comic_name] = identity
                db.session.commit()

        return existing_records


def ensure_comic_identity(comic_name):
    normalized_name = (comic_name or '').strip()
    if not is_safe_comic_name(normalized_name):
        return None
    identity_map = sync_comic_identity_records([normalized_name])
    return identity_map.get(normalized_name)


def normalize_comic_description(text, max_length=2000):
    """清理站点简介文本：折叠空白、去掉常见噪声前缀。"""
    if not text:
        return ''
    cleaned = re.sub(r'\s+', ' ', str(text)).strip()
    if not cleaned:
        return ''
    # 部分站点 meta 会把最新章节标题拼在简介前面
    cleaned = re.sub(
        r'^(公告|最新|更新|连载)[^\s，。！？]{0,24}[：:\s]+',
        '',
        cleaned,
        count=1,
    ).strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 1].rstrip() + '…'
    return cleaned


def get_comic_description(comic_name):
    normalized_name = (comic_name or '').strip()
    if not is_safe_comic_name(normalized_name):
        return ''
    with app.app_context():
        identity = ComicIdentity.query.filter_by(comic_name=normalized_name).first()
        if not identity or not identity.description:
            return ''
        return normalize_comic_description(identity.description)


def get_comic_descriptions(comic_names):
    """批量取简介，返回 {comic_name: description}。"""
    names = []
    seen = set()
    for name in comic_names or []:
        normalized = (name or '').strip()
        if not is_safe_comic_name(normalized) or normalized in seen:
            continue
        seen.add(normalized)
        names.append(normalized)
    if not names:
        return {}
    with app.app_context():
        rows = ComicIdentity.query.filter(ComicIdentity.comic_name.in_(names)).all()
        result = {}
        for row in rows:
            desc = normalize_comic_description(row.description)
            if desc:
                result[row.comic_name] = desc
        return result


def save_comic_description(comic_name, description):
    """把爬取到的简介写入 ComicIdentity（空值不覆盖已有）。"""
    normalized_name = (comic_name or '').strip()
    cleaned = normalize_comic_description(description)
    if not is_safe_comic_name(normalized_name) or not cleaned:
        return False
    with app.app_context():
        # 先确保身份存在；再重新 query 拿到 session 附着对象（sync 返回值可能 detached）
        ensure_comic_identity(normalized_name)
        identity = ComicIdentity.query.filter_by(comic_name=normalized_name).first()
        if not identity:
            return False
        existing = normalize_comic_description(identity.description)
        if existing == cleaned:
            return True
        identity.description = cleaned
        try:
            db.session.commit()
            return True
        except Exception:
            db.session.rollback()
            return False


def get_comic_identity_by_id(comic_id):
    normalized_id = (comic_id or '').strip()
    if not normalized_id:
        return None
    with app.app_context():
        return ComicIdentity.query.filter_by(comic_id=normalized_id).first()


def resolve_comic_name(comic_reference):
    normalized_reference = (comic_reference or '').strip()
    if not normalized_reference:
        return None

    identity = get_comic_identity_by_id(normalized_reference)
    if identity:
        return identity.comic_name

    if is_safe_comic_name(normalized_reference):
        with app.app_context():
            existing_identity = ComicIdentity.query.filter_by(comic_name=normalized_reference).first()
            if existing_identity:
                return existing_identity.comic_name
        return normalized_reference

    return None


def build_chapter_id(filename):
    normalized_name = (filename or '').strip()
    if not normalized_name:
        return None
    digest = hashlib.sha1(normalized_name.encode('utf-8')).hexdigest()
    return f"ch_{digest[:12]}"


SUPPORTED_PAGE_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
API_PAGE_CACHE_ROOT = os.path.join(app.instance_path, 'api_page_cache')


def ensure_api_page_cache_dir(comic_id, chapter_id):
    safe_comic_id = re.sub(r'[^A-Za-z0-9_.-]', '_', (comic_id or '').strip()) or 'comic'
    safe_chapter_id = re.sub(r'[^A-Za-z0-9_.-]', '_', (chapter_id or '').strip()) or 'chapter'
    cache_dir = os.path.join(API_PAGE_CACHE_ROOT, safe_comic_id, safe_chapter_id)
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def get_chapter_file_path(comic_name, chapter):
    comic_dir = get_comic_directory(comic_name)
    filename = (chapter or {}).get('filename')
    if not comic_dir or not filename:
        return None
    file_path = resolve_file_under_directory(comic_dir, filename)
    if not file_path:
        return None
    return file_path if os.path.exists(file_path) else None


def list_archive_page_entries(file_path):
    with zipfile.ZipFile(file_path) as archive:
        entries = []
        total_uncompressed_size = 0
        for entry in archive.infolist():
            name = entry.filename
            if (
                name
                and not name.endswith('/')
                and not os.path.basename(name).startswith('.')
                and os.path.splitext(name)[1].lower() in SUPPORTED_PAGE_IMAGE_EXTENSIONS
            ):
                if entry.file_size > MAX_ARCHIVE_ENTRY_BYTES:
                    raise ValueError('CBZ 单页超过大小限制')
                total_uncompressed_size += entry.file_size
                if total_uncompressed_size > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ValueError('CBZ 解压后总大小超过限制')
                entries.append(name)
        if len(entries) > MAX_ARCHIVE_PAGES:
            raise ValueError('CBZ 页数超过限制')
    return natsorted(entries)


def get_pdf_page_count(pdf_path):
    try:
        result = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=PDF_TOOL_TIMEOUT_SECONDS
        )
    except Exception:
        return None

    match = re.search(r'^Pages:\s+(\d+)', result.stdout, re.MULTILINE)
    page_count = int(match.group(1)) if match else None
    if page_count and page_count > MAX_PDF_PAGES:
        raise ValueError('PDF 页数超过限制')
    return page_count


def get_pdf_page_renderer():
    for command in ("pdftoppm", "pdftocairo"):
        if shutil.which(command):
            return command
    return None


def get_chapter_page_count(file_path):
    extension = os.path.splitext(file_path)[1].lower()
    if extension == '.cbz':
        try:
            return len(list_archive_page_entries(file_path))
        except Exception:
            return None
    if extension == '.pdf':
        return get_pdf_page_count(repair_pdf_for_reading(file_path))
    return None


def build_api_page_list(comic_id, chapter_id, file_path):
    total_pages = get_chapter_page_count(file_path)
    if total_pages is None:
        raise ValueError('无法读取章节页数')

    pages = [
        {
            'index': page_index,
            'image_url': url_for(
                'api_comic_chapter_page_image',
                comic_id=comic_id,
                chapter_id=chapter_id,
                page_index=page_index
            ),
        }
        for page_index in range(total_pages)
    ]
    return pages, total_pages


def extract_cbz_page_to_cache(file_path, page_index, cache_dir):
    entries = list_archive_page_entries(file_path)
    if page_index < 0 or page_index >= len(entries):
        return None

    entry_name = entries[page_index]
    extension = os.path.splitext(entry_name)[1].lower() or '.jpg'
    cache_path = os.path.join(cache_dir, f"{page_index:04d}{extension}")
    if os.path.exists(cache_path):
        return cache_path

    with zipfile.ZipFile(file_path) as archive:
        with archive.open(entry_name) as source_handle, open(cache_path, 'wb') as target_handle:
            shutil.copyfileobj(source_handle, target_handle)

    return cache_path


def render_pdf_page_to_cache(file_path, page_index, cache_dir):
    renderer = get_pdf_page_renderer()
    if not renderer:
        raise RuntimeError('服务器缺少 PDF 页面渲染工具（pdftoppm/pdftocairo）')

    readable_pdf = repair_pdf_for_reading(file_path)
    total_pages = get_pdf_page_count(readable_pdf)
    if total_pages is None:
        raise RuntimeError('无法读取 PDF 页数')
    if page_index < 0 or page_index >= total_pages:
        return None

    output_prefix = os.path.join(cache_dir, f"{page_index:04d}")
    cache_path = f"{output_prefix}.png"
    if os.path.exists(cache_path):
        return cache_path

    page_number = page_index + 1
    command = [
        renderer,
        '-f', str(page_number),
        '-l', str(page_number),
        '-png',
        '-singlefile',
        readable_pdf,
        output_prefix,
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=PDF_TOOL_TIMEOUT_SECONDS)

    return cache_path if os.path.exists(cache_path) else None


def resolve_chapter_page_image(file_path, comic_id, chapter_id, page_index):
    cache_dir = ensure_api_page_cache_dir(comic_id, chapter_id)
    extension = os.path.splitext(file_path)[1].lower()
    if extension == '.cbz':
        return extract_cbz_page_to_cache(file_path, page_index, cache_dir)
    if extension == '.pdf':
        return render_pdf_page_to_cache(file_path, page_index, cache_dir)
    return None


def detect_local_comic_format(comic_path):
    comic_name = os.path.basename(os.path.normpath(comic_path))
    return get_cached_local_comic_scan(comic_name, comic_path).get('comic_format')


def resolve_comic_file_request(filename):
    normalized_request_path = os.path.normpath((filename or '').replace('\\', '/')).lstrip('/')
    if not normalized_request_path or normalized_request_path.startswith('..'):
        return None

    path_parts = [part for part in normalized_request_path.split('/') if part]
    if len(path_parts) < 2:
        return None

    comic_name = path_parts[0]
    relative_filename = os.path.join(*path_parts[1:])
    comic_dir = get_comic_directory(comic_name)
    if not comic_dir:
        return None

    absolute_comic_dir = os.path.realpath(os.path.abspath(comic_dir))
    absolute_file_path = resolve_file_under_directory(absolute_comic_dir, relative_filename)

    if not absolute_file_path or not os.path.isfile(absolute_file_path):
        return None

    return {
        'comic_name': comic_name,
        'comic_dir': absolute_comic_dir,
        'relative_filename': relative_filename,
        'file_path': absolute_file_path,
    }

def get_directory_signature(path):
    try:
        stat_result = os.stat(path)
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)


def list_cached_root_directories(root):
    normalized_root = normalize_scan_path(root)
    if not normalized_root or not os.path.isdir(normalized_root):
        return []

    signature = get_directory_signature(normalized_root)
    if signature is None:
        return []

    with comic_scan_cache_lock:
        cached_entry = comic_root_listing_cache.get(normalized_root)
        if cached_entry and cached_entry.get('signature') == signature:
            return list(cached_entry.get('directories', ()))

    directories = []
    try:
        with os.scandir(normalized_root) as root_entries:
            for entry in root_entries:
                if not entry.is_dir():
                    continue
                directories.append((entry.name, entry.path))
    except OSError:
        return []

    directories.sort(key=lambda item: item[0])

    with comic_scan_cache_lock:
        comic_root_listing_cache[normalized_root] = {
            'signature': signature,
            'directories': tuple(directories),
        }

    return directories


def get_cached_local_comic_scan(comic_name, comic_path=None):
    resolved_path = comic_path or get_comic_directory(comic_name)
    if not resolved_path or not os.path.isdir(resolved_path):
        return {
            'available_chapters': 0,
            'comic_format': None,
        }

    normalized_path = os.path.abspath(resolved_path)
    signature = get_directory_signature(normalized_path)
    if signature is None:
        return {
            'available_chapters': 0,
            'comic_format': None,
        }

    with comic_scan_cache_lock:
        cached_entry = comic_directory_scan_cache.get(normalized_path)
        if cached_entry and cached_entry.get('signature') == signature:
            return dict(cached_entry['data'])

    pdf_count = 0
    cbz_count = 0
    chapter_count = 0
    try:
        with os.scandir(normalized_path) as chapter_entries:
            for entry in chapter_entries:
                if not entry.is_file():
                    continue
                filename = entry.name
                if is_valid_local_chapter_file(filename, ('.pdf',)):
                    pdf_count += 1
                    chapter_count += 1
                elif is_valid_local_chapter_file(filename, ('.cbz',)):
                    cbz_count += 1
                    chapter_count += 1
    except OSError:
        return {
            'available_chapters': 0,
            'comic_format': None,
        }

    if pdf_count and cbz_count:
        comic_format = 1 if pdf_count > cbz_count else 2
    elif pdf_count:
        comic_format = 1
    elif cbz_count:
        comic_format = 2
    else:
        comic_format = None

    scan_data = {
        'available_chapters': chapter_count,
        'comic_format': comic_format,
    }

    with comic_scan_cache_lock:
        comic_directory_scan_cache[normalized_path] = {
            'signature': signature,
            'data': dict(scan_data),
        }

    return scan_data


def load_comic_mapping():
    json_file_path = COMIC_MAPPING_FILE
    if not os.path.exists(json_file_path) or os.path.getsize(json_file_path) <= 0:
        return {}
    try:
        with open(json_file_path, "r", encoding="utf-8") as json_file:
            comic_data = json.load(json_file)
            return comic_data if isinstance(comic_data, dict) else {}
    except Exception:
        return {}


def get_local_chapter_bases(comic_name):
    comic_path = get_comic_directory(comic_name)
    if not comic_path or not os.path.isdir(comic_path):
        return set()

    chapter_bases = set()
    try:
        with os.scandir(comic_path) as chapter_entries:
            for entry in chapter_entries:
                if not entry.is_file():
                    continue
                if is_valid_local_chapter_file(entry.name):
                    chapter_bases.add(os.path.splitext(entry.name)[0])
    except OSError:
        return set()
    return chapter_bases


def get_local_chapter_match_bases(comic_name):
    match_bases = set()
    for chapter in list_local_chapters(comic_name):
        filename = chapter.get('filename') or ''
        filename_stem = os.path.splitext(filename)[0] if filename else ''
        if filename_stem:
            match_bases.add(filename_stem)

        title = sanitize_filename(chapter.get('title') or '')
        if not title:
            continue

        match_bases.add(title)
        order = chapter.get('order')
        if isinstance(order, int) and order > 0:
            match_bases.add(f"{order:04d}_{title}")

    return match_bases


def is_existing_local_chapter(chapter, local_match_bases):
    title = sanitize_filename(chapter.get('title') or '')
    chapter_keys = set()

    filename_base = chapter.get('filename_base')
    if filename_base:
        chapter_keys.add(filename_base)

    if title:
        chapter_keys.add(title)

    order = chapter.get('order')
    if isinstance(order, int) and order > 0 and title:
        chapter_keys.add(f"{order:04d}_{title}")

    return any(key in local_match_bases for key in chapter_keys)


def build_available_comics_snapshot():
    with app.app_context():
        # Membership table is the source of truth; DownloadTask.group can lag behind.
        from mangadock.services.groups import get_comic_group_map

        comic_group_map = get_comic_group_map()
        query = DownloadTask.query.filter(
            DownloadTask.status.in_(['completed', 'running', 'error', 'cancelled'])
        )
        all_tasks = query.all()
        comics = {}

        for task in all_tasks:
            if task.comic_name not in comics:
                comic_path = get_comic_directory(task.comic_name)
                has_content = False
                available_chapters = 0
                detected_format = None

                if comic_path and os.path.exists(comic_path):
                    try:
                        scan_data = get_cached_local_comic_scan(task.comic_name, comic_path)
                        available_chapters = scan_data.get('available_chapters', 0)
                        detected_format = scan_data.get('comic_format')
                        if available_chapters > 0:
                            has_content = True
                    except Exception:
                        continue

                if has_content:
                    comics[task.comic_name] = {
                        'id': task.id,
                        'comic_name': task.comic_name,
                        'comic_format': detected_format or task.comic_format,
                        'status': task.status,
                        'total_chapters': task.total_chapters,
                        'completed_chapters': task.completed_chapters,
                        'available_chapters': available_chapters,
                        'created_at': task.created_at,
                        'group': comic_group_map.get(task.comic_name) or task.group or '默认分组'
                    }

        for comic_name, _comic_path in iter_local_comic_directories():
            if comic_name not in comics:
                try:
                    scan_data = get_cached_local_comic_scan(comic_name, _comic_path)
                    available_chapters = scan_data.get('available_chapters', 0)
                    comic_format = scan_data.get('comic_format')
                    if available_chapters and comic_format:
                        comics[comic_name] = {
                            'id': comic_name,
                            'comic_name': comic_name,
                            'comic_format': comic_format,
                            'status': 'completed',
                            'total_chapters': available_chapters,
                            'completed_chapters': available_chapters,
                            'available_chapters': available_chapters,
                            'created_at': None,
                            'group': comic_group_map.get(comic_name) or '默认分组'
                        }
                except Exception:
                    continue

        sync_comic_identity_records(comics.keys())
        return list(comics.values())


def refresh_comics_cache(force=False, async_refresh=False):
    current_time = time.time()

    with comics_cache_lock:
        cache_is_fresh = (
            comics_cache['data'] is not None and
            not force and
            (current_time - comics_cache['timestamp'] < comics_cache['expiration'])
        )
        if cache_is_fresh:
            return comics_cache['data']

        if async_refresh:
            if comics_cache['refreshing']:
                return comics_cache['data']
            comics_cache['refreshing'] = True

    def _refresh():
        try:
            refreshed_data = build_available_comics_snapshot()
            with comics_cache_lock:
                comics_cache['data'] = refreshed_data
                comics_cache['timestamp'] = time.time()
        finally:
            with comics_cache_lock:
                comics_cache['refreshing'] = False

    if async_refresh:
        refresh_thread = threading.Thread(target=_refresh, daemon=True)
        refresh_thread.start()
        return comics_cache['data']

    _refresh()
    return comics_cache['data']


def get_available_comics():
    """获取可阅读的漫画列表（包括未完成的和已删除任务但文件仍存在的）"""
    current_time = time.time()

    with comics_cache_lock:
        cached_data = comics_cache['data']
        cache_timestamp = comics_cache['timestamp']
        cache_expiration = comics_cache['expiration']

    if cached_data and (current_time - cache_timestamp < cache_expiration):
        return cached_data

    if cached_data:
        refresh_comics_cache(async_refresh=True)
        return cached_data

    return refresh_comics_cache(force=True) or []


def invalidate_comics_cache():
    with comics_cache_lock:
        comics_cache['data'] = None
        comics_cache['timestamp'] = 0
        comics_cache['refreshing'] = False


def schedule_comics_cache_refresh():
    refresh_comics_cache(force=True, async_refresh=True)



def save_comic_mapping(comic_name, url):
    json_file_path = COMIC_MAPPING_FILE
    try:
        if os.path.exists(json_file_path) and os.path.getsize(json_file_path) > 0:
            with open(json_file_path, "r", encoding="utf-8") as json_file:
                existing_data = json.load(json_file)
        else:
            existing_data = {}
    except Exception:
        existing_data = {}

    existing_data[comic_name] = url
    with open(json_file_path, "w", encoding="utf-8") as json_file:
        json.dump(existing_data, json_file, ensure_ascii=False, indent=4)


def save_cover_image(comic_name, cover_url, referer=None, verify=True):
    if not cover_url:
        return

    ensure_directory(COVER_ROOT)
    cover_path = os.path.join(COVER_ROOT, f"{comic_name}.jpg")

    try:
        response = safe_http_get(
            cover_url,
            headers=default_headers(referer=referer),
            timeout=CONFIG['request_timeout'],
            verify=verify,
            max_bytes=MAX_IMAGE_RESPONSE_BYTES
        )
        response.raise_for_status()
        with open(cover_path, "wb") as cover_file:
            cover_file.write(response.content)
        try:
            from mangadock.utils.cover_enhance import refresh_hero_cover
            refresh_hero_cover(comic_name)
        except Exception as enhance_exc:
            safe_print(f"封面超分缓存失败: {enhance_exc}")
    except Exception as exc:
        safe_print(f"下载封面失败: {exc}")


def chapter_sort_key(filename):
    stem = os.path.splitext(filename)[0]
    prefix_match = re.match(r'^(\d+)[_-](.+)$', stem)
    if prefix_match:
        return (0, int(prefix_match.group(1)), prefix_match.group(2))
    if stem.isdigit():
        return (0, int(stem), "")

    number_match = re.search(r'(\d+)', stem)
    if number_match:
        return (1, int(number_match.group(1)), stem)

    return (2, stem)


def chapter_display_title(filename):
    stem = os.path.splitext(filename)[0]
    prefix_match = re.match(r'^(\d+)[_-](.+)$', stem)
    if prefix_match:
        return prefix_match.group(2)
    if stem.isdigit():
        return f"第{int(stem)}章"
    return stem


def is_valid_local_chapter_file(filename, extensions=('.pdf', '.cbz')):
    stem, ext = os.path.splitext(filename)
    return (
        not filename.startswith('._')
        and ext.lower() in extensions
        and stem != '00'
    )


def list_local_chapters(comic_name):
    comic_path = get_comic_directory(comic_name)
    if not comic_path or not os.path.exists(comic_path):
        return []

    chapter_files = [
        filename for filename in os.listdir(comic_path)
        if is_valid_local_chapter_file(filename)
    ]
    chapter_files.sort(key=chapter_sort_key)

    chapters = []
    for index, filename in enumerate(chapter_files):
        stem = os.path.splitext(filename)[0]
        order_match = re.match(r'^(\d+)', stem)
        chapters.append({
            'number': index + 1,
            'order': int(order_match.group(1)) if order_match else index + 1,
            'title': chapter_display_title(filename),
            'filename': filename,
            'format': os.path.splitext(filename)[1][1:].lower()
        })
    return chapters


def target_extension(comic_format):
    return 'pdf' if comic_format == 1 else 'cbz'


def chapter_output_exists(comic_name, filename_base, comic_format):
    return os.path.exists(
        os.path.join(COMIC_ROOT, comic_name, f"{filename_base}.{target_extension(comic_format)}")
    )

