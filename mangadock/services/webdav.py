# -*- coding: utf-8 -*-
"""只读 WebDAV 书库。

同步时只保存漫画和章节索引，以及小封面。网页 CBZ 阅读支持按需分段
缓存；PDF 和完整文件下载沿用整章缓存，分组、进度和 AniList 走原有逻辑。
"""
import base64
import fcntl
import glob
import hashlib
import ipaddress
import json
import os
import shutil
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import request, send_file

from mangadock.core import app
from mangadock.settings import COVER_ROOT, china_tz
from mangadock.utils.cover_image import normalize_cover_bytes
from mangadock.utils.media import ensure_directory

INDEX_FILENAME = '.webdav-index.json'
CACHE_META_SUFFIX = '.webdav-meta'
CONFIG_FILENAME = 'webdav.json'
COVER_NAMES = ('cover.jpg', 'cover.jpeg', 'cover.png', 'cover.webp', 'folder.jpg')
CHAPTER_EXTENSIONS = ('.cbz', '.pdf')
MAX_LISTING_BYTES = 8 * 1024 * 1024
MAX_COVER_BYTES = 20 * 1024 * 1024
MAX_CHAPTER_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_CACHE_GB = 20

_download_guard = threading.Lock()
_download_locks = {}


class WebDavError(ValueError):
    pass


class WebDavSyncCancelled(WebDavError):
    pass


def cache_root():
    path = os.path.join(app.instance_path, 'webdav_comics')
    return os.path.abspath(path)


def is_cache_path(path):
    if not path:
        return False
    root = os.path.realpath(cache_root())
    candidate = os.path.realpath(path)
    return candidate == root or candidate.startswith(root + os.sep)


def indexed_chapter_filenames(comic_path):
    """有 WebDAV 索引时返回完整章节名；普通本地目录返回 None。"""
    if not comic_path:
        return None
    index_path = os.path.join(comic_path, INDEX_FILENAME)
    if not os.path.isfile(index_path):
        return None
    try:
        with open(index_path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    if not _stored_source_visible(payload):
        return []
    chapters = payload.get('chapters')
    if not isinstance(chapters, list):
        return []
    filenames = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        filename = os.path.basename(str(chapter.get('filename') or ''))
        if filename and filename not in filenames:
            filenames.append(filename)
    return filenames


def index_has_chapter(comic_path, filename):
    name = os.path.basename(str(filename or '').replace('\\', '/'))
    return bool(name) and name in set(indexed_chapter_filenames(comic_path) or [])


def note_chapter_used(file_path):
    """记录最后访问时间，供缓存淘汰使用。不改章节文件自身的时间。"""
    if not file_path or not is_cache_path(file_path) or not os.path.isfile(file_path):
        return
    meta_path = file_path + CACHE_META_SUFFIX
    try:
        with open(meta_path, encoding='utf-8') as handle:
            meta = json.load(handle)
        if not isinstance(meta, dict):
            return
        meta['last_used'] = time.time_ns()
        _atomic_json(meta_path, meta)
    except (OSError, ValueError):
        pass


def open_chapter_file(file_path):
    """缓存章节先加锁再打开。返回后锁已释放，打开的句柄仍可读取。"""
    if file_path and is_cache_path(file_path):
        with _chapter_file_lock(file_path):
            return open(file_path, 'rb')
    return open(file_path, 'rb')


def chapter_file_response(handle, filename, mimetype):
    """Serve the opened inode, including byte ranges and conditional requests."""
    try:
        stat = os.fstat(handle.fileno())
        etag = f'{stat.st_ino:x}-{stat.st_size:x}-{stat.st_mtime_ns:x}'
        response = send_file(
            handle, mimetype=mimetype, download_name=filename,
            conditional=False, etag=etag, last_modified=stat.st_mtime, max_age=0,
        )
        response.content_length = stat.st_size
        response.make_conditional(request.environ, accept_ranges=True, complete_length=stat.st_size)
        response.call_on_close(handle.close)
        return response
    except Exception:
        handle.close()
        raise


def settings_view():
    config = _read_config()
    cache = cache_root()
    cleanup_pending = False
    if os.path.isdir(cache):
        with os.scandir(cache) as entries:
            cleanup_pending = any(entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.')
                                  for entry in entries)
    return {
        'configured': bool(config.get('url')),
        'cleanup_pending': cleanup_pending,
        'url': config.get('url') or '',
        'username': config.get('username') or '',
        'password_set': bool(config.get('password')),
        'root': config.get('root') or '',
        'cache_gb': config.get('cache_gb') or DEFAULT_CACHE_GB,
        'last_sync_at': config.get('last_sync_at') or '',
        'last_error': config.get('last_error') or '',
        'comic_count': int(config.get('comic_count') or 0),
        'skipped_count': int(config.get('skipped_count') or 0),
    }


@contextmanager
def _config_lock():
    with _open_lock_file(_config_path()) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sync_config(snapshot):
    current = _read_config()
    keys = ('url', 'root', 'username', 'password', 'cache_gb', 'revision')
    if any(current.get(key) != snapshot.get(key) for key in keys):
        raise WebDavError('WebDAV 连接已修改或断开，本次同步已停止，请重新同步')
    return current


def save_settings(url, username, password, root, cache_gb, keep_password=False):
    current = _read_config()
    normalized_url = _validate_base_url(url)
    root_path = _validate_root(root)
    limit = _validate_cache_gb(cache_gb)
    next_password = current.get('password') if keep_password else (password or '')
    if not next_password and not current.get('password'):
        raise WebDavError('请填写 WebDAV 密码')
    candidate = {
        'url': normalized_url,
        'username': (username or '').strip(),
        'password': next_password,
        'root': root_path,
        'cache_gb': limit,
    }
    _propfind(candidate, _library_url(candidate))
    with _config_lock():
        candidate['revision'] = time.time_ns()
        source_changed = bool(current.get('url')) and _source_id(current) != _source_id(candidate)
        if source_changed:
            _invalidate_other_source_caches(_source_id(candidate))
            candidate['last_sync_at'] = ''
            candidate['last_error'] = ''
            candidate['comic_count'] = 0
            candidate['skipped_count'] = 0
        else:
            candidate['last_sync_at'] = current.get('last_sync_at') or ''
            candidate['last_error'] = ''
            candidate['comic_count'] = current.get('comic_count') or 0
            candidate['skipped_count'] = current.get('skipped_count') or 0
        _write_config(candidate)
    evict_cached_chapters(cache_root(), _cache_limit_bytes(candidate), None)


def disconnect():
    from mangadock.extensions import db
    from mangadock.models import ComicIdentity, ReadingProgress, ReadingSessionState
    from mangadock.services.comic_delete import _remove_tree
    from mangadock.services.home_banner import HOME_BANNER_DIR
    from mangadock.services.library import get_comic_scan_roots

    with _config_lock():
        path = _config_path()
        try:
            root = cache_root()
            local_roots = [scan_root for scan_root in get_comic_scan_roots(existing_only=False)
                           if os.path.realpath(scan_root) != os.path.realpath(root)]
            local_roots_available = all(os.path.isdir(scan_root) for scan_root in local_roots)
            entries = ([entry for entry in os.scandir(root)
                        if entry.is_dir(follow_symlinks=False) and entry.name and not entry.name.startswith('.')]
                       if os.path.isdir(root) else [])
            names = [entry.name for entry in entries
                     if not any(os.path.isdir(os.path.join(scan_root, entry.name)) for scan_root in local_roots)]
            if names and local_roots_available:
                ReadingProgress.query.filter(ReadingProgress.comic_name.in_(names)).delete(synchronize_session=False)
                ReadingSessionState.query.filter(ReadingSessionState.comic_name.in_(names)).delete(synchronize_session=False)
                db.session.commit()
            if names:
                identities = ComicIdentity.query.filter(ComicIdentity.comic_name.in_(names)).all()
                for identity in identities:
                    comic_id = identity.comic_id or ''
                    if comic_id and comic_id not in ('.', '..') and os.path.basename(comic_id) == comic_id:
                        page_cache = os.path.join(app.instance_path, 'api_page_cache', comic_id)
                        if os.path.islink(page_cache):
                            os.remove(page_cache)
                        elif os.path.isdir(page_cache):
                            _remove_tree(page_cache)
            if names and local_roots_available:
                for name in names:
                    covers = (os.path.join(COVER_ROOT, name + '.jpg'),
                              os.path.join(COVER_ROOT, 'hero', name + '.jpg'))
                    banner_key = hashlib.sha256(name.encode('utf-8')).hexdigest()
                    banners = glob.glob(os.path.join(HOME_BANNER_DIR, banner_key + '.*'))
                    banners += glob.glob(os.path.join(HOME_BANNER_DIR, 'upscaled', banner_key + '-*.jpg'))
                    for image_path in (*covers, *banners):
                        if os.path.isfile(image_path) or os.path.islink(image_path):
                            os.remove(image_path)
            for entry in entries:
                _prune_comic_cache(entry.path, blocking=True)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        except Exception:
            db.session.rollback()
            raise
        finally:
            _publish_library_change()


def queue_library_sync():
    from mangadock.services.updates import queue_background_command
    with _config_lock():
        _require_config()
        return queue_background_command('sync_webdav')


def _publish_library_change():
    _atomic_json(os.path.join(app.instance_path, 'comic-deletion.version'), time.time_ns())


def sync_library(progress=None):
    with _open_lock_file(_config_path() + '.sync') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WebDavError('WebDAV 正在同步，请等待当前同步完成') from exc
        try:
            return _sync_library_locked(progress) if progress else _sync_library_locked()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sync_library_locked(progress=None):
    config = _require_config()
    library_url = _library_url(config)
    root_listing = _propfind(config, library_url)
    root_items = root_listing['items']
    library_path = _path_of(library_url)
    from mangadock.services.library import (
        get_comic_scan_roots,
        invalidate_comics_cache,
        is_safe_comic_name,
        list_cached_root_directories,
        refresh_comics_cache,
    )

    local_names = set()
    cache = cache_root()
    for scan_root in get_comic_scan_roots(existing_only=True):
        if os.path.abspath(scan_root) == cache:
            continue
        for comic_name, _comic_path in list_cached_root_directories(scan_root):
            local_names.add(comic_name)

    ensure_directory(cache)
    kept = set()
    added = 0
    skipped = 0
    failures = []
    root_children = _direct_children(root_items, library_path)
    candidates = [item for item in root_children if item['is_dir'] and is_safe_comic_name(item['name'])]
    def report(done, current=''):
        if progress:
            progress({'done': done, 'total': len(candidates), 'current': current,
                      'added': added, 'skipped': skipped, 'failed': len(failures)})
    report(0)
    for position, item in enumerate(candidates):
        report(position, item['name'])
        if not item['is_dir']:
            continue
        comic_name = item['name']
        if not is_safe_comic_name(comic_name):
            continue
        if comic_name in local_names:
            skipped += 1
            continue
        comic_dir = os.path.join(cache, comic_name)
        try:
            child_listing = _propfind(config, _url_for(config, item['path']))
        except WebDavError as exc:
            failures.append(comic_name + '：' + str(exc))
            if os.path.isdir(comic_dir):
                kept.add(comic_name)
            continue
        if not _listing_is_usable(child_listing, item['path']):
            failures.append(comic_name + '：目录列表不完整')
            if os.path.isdir(comic_dir):
                kept.add(comic_name)
            continue
        chapters, cover = _chapters_and_cover(child_listing['items'], item['path'])
        if not chapters and not os.path.isdir(comic_dir):
            continue
        with _config_lock():
            _sync_config(config)
            ensure_directory(comic_dir)
            _write_index(comic_dir, chapters, _source_id(config))
            kept.add(comic_name)
            added += 1
            _publish_library_change()
        report(position + 1)
        with _config_lock():
            _sync_config(config)
            try:
                _save_cover(config, comic_name, cover)
            except WebDavError as exc:
                failures.append(comic_name + '：封面读取失败：' + str(exc))
                app.logger.warning('WebDAV 封面读取失败 %s: %s', comic_name, exc)
        try:
            from mangadock.services.webdav_metadata import queue_metadata
            queue_metadata(comic_name)
        except Exception as exc:
            app.logger.warning('WebDAV 资料补全排队失败 %s: %s', comic_name, exc)

    report(len(candidates))
    with _config_lock():
        _sync_config(config)
        pruned = False
        if _can_prune_missing_comics(root_listing, library_path):
            pruned = True
            for entry in os.scandir(cache):
                if entry.is_dir() and entry.name not in kept and is_safe_comic_name(entry.name):
                    _prune_comic_cache(entry.path)

    _publish_library_change()
    report(len(candidates))
    message = f'WebDAV 同步完成：写入 {added} 部，跳过本地已有 {skipped} 部；封面与简介继续在后台补全'
    if not pruned:
        message += '；目录列表不完整或为空，未清理已有缓存'
    if failures:
        message += '；' + str(len(failures)) + ' 部读取失败'
    with _config_lock():
        current = _sync_config(config)
        current['last_sync_at'] = datetime.now(china_tz).strftime('%Y-%m-%d %H:%M')
        current['last_error'] = '；'.join(failures[:3])
        current['comic_count'] = added
        current['skipped_count'] = skipped
        _write_config(current)
    invalidate_comics_cache()
    refresh_comics_cache(force=True)
    return message


@contextmanager
def chapter_access(file_path):
    """Materialize and read a cache chapter under one uninterrupted lock."""
    if not file_path or not is_cache_path(file_path):
        yield file_path if file_path and os.path.isfile(file_path) else None
        return
    with _chapter_file_lock(file_path):
        cached = _ensure_chapter_cached_locked(os.path.dirname(file_path), os.path.basename(file_path))
        try:
            yield cached
        finally:
            config = _read_config()
    if config.get('url'):
        _maybe_evict_cached_chapters(config, file_path)


def ensure_chapter_cached(comic_dir, relative_filename):
    if not comic_dir or not relative_filename or not is_cache_path(comic_dir):
        return None
    filename = os.path.basename(relative_filename.replace('\\', '/'))
    with _chapter_file_lock(os.path.join(comic_dir, filename)):
        return _ensure_chapter_cached_locked(comic_dir, relative_filename)


def _ensure_chapter_cached_locked(comic_dir, relative_filename):
    if not comic_dir or not relative_filename or not is_cache_path(comic_dir):
        return None
    filename = os.path.basename(relative_filename.replace('\\', '/'))
    if not filename or filename != os.path.basename(filename):
        return None
    payload = _read_index_payload(comic_dir)
    chapter = next((item for item in payload['chapters'] if item.get('filename') == filename), None)
    if not chapter:
        return None
    destination = os.path.join(comic_dir, filename)
    config = _read_config()
    source = _source_id(config) if config.get('url') else ''
    if source and payload.get('source') and payload['source'] != source:
        _invalidate_comic_cache(comic_dir)
        return None
    if not config.get('url') or not config.get('password'):
        if _cache_matches(destination, chapter, payload.get('source') or source):
            note_chapter_used(destination)
            return destination
        _remove_cache_file(destination)
        return None

    if _cache_matches(destination, chapter, source):
        note_chapter_used(destination)
        return destination
    _remove_cache_file(destination)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix='.chapter-', suffix='.part', dir=comic_dir,
        )
        os.close(descriptor)
        _download(config, chapter, temporary)
        if not _valid_chapter_file(temporary, filename):
            raise WebDavError('章节内容无效，未写入缓存')
        os.replace(temporary, destination)
        temporary = None
        _write_cache_meta(destination, chapter, source)
    except WebDavError as exc:
        app.logger.warning('WebDAV 章节下载失败 %s: %s', filename, exc)
        return None
    finally:
        if temporary:
            try:
                os.remove(temporary)
            except OSError:
                pass
    evict_cached_chapters(cache_root(), _cache_limit_bytes(config), destination)
    note_chapter_used(destination)
    return destination


def discard_cached_chapter(file_path):
    if not file_path or not is_cache_path(file_path):
        return
    if os.path.splitext(file_path)[1].lower() not in CHAPTER_EXTENSIONS:
        return
    with _chapter_file_lock(file_path):
        _remove_cache_file(file_path)


def _maybe_evict_cached_chapters(config, protect_path):
    """Coalesce read-triggered scans across workers, at most once per 30 seconds."""
    with _open_lock_file(_config_path() + '.eviction') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            handle.seek(0)
            try:
                last_scan = float(handle.read() or '0')
            except ValueError:
                last_scan = 0
            now = time.time()
            if 0 <= now - last_scan < 30:
                return
            evict_cached_chapters(cache_root(), _cache_limit_bytes(config), protect_path)
            handle.seek(0)
            handle.truncate()
            handle.write(str(now))
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _chapter_derivatives(path):
    paths = [path + '.webdav-ranges'] if path.lower().endswith('.cbz') else []
    if path.lower().endswith('.pdf'):
        repaired_root = os.path.join(os.path.dirname(path), '.repaired')
        filename = os.path.basename(path)
        legacy = os.path.join(repaired_root, filename)
        if os.path.isfile(legacy):
            paths.append(legacy)
        paths.extend(glob.glob(os.path.join(glob.escape(repaired_root), '*', glob.escape(filename))))
    from mangadock.services.library import get_comic_identity, get_comic_directory, build_chapter_id
    comic_name = os.path.basename(os.path.dirname(path))
    comic_dir = get_comic_directory(comic_name)
    # A local comic with the same name owns its own API cache.
    if comic_dir and os.path.realpath(comic_dir) == os.path.realpath(os.path.dirname(path)):
        identity = get_comic_identity(comic_name)
        if identity:
            chapter_dir = os.path.join(app.instance_path, 'api_page_cache', identity.comic_id,
                                       build_chapter_id(os.path.basename(path)))
            if os.path.isdir(chapter_dir):
                paths.append(chapter_dir)
    return paths


def _cache_bytes(paths):
    total = 0
    for path in paths:
        if os.path.isdir(path):
            for directory, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(directory, name))
                    except FileNotFoundError:
                        pass
        else:
            try:
                total += os.path.getsize(path)
            except FileNotFoundError:
                pass
    return total


def evict_cached_chapters(root, limit_bytes, protect_path):
    if limit_bytes <= 0 or not os.path.isdir(root):
        return []
    files = []
    total = 0
    for entry in os.scandir(root):
        if not entry.is_dir():
            continue
        names = {chapter.name for chapter in os.scandir(entry.path)
                 if chapter.is_file() and os.path.splitext(chapter.name)[1].lower() in CHAPTER_EXTENSIONS}
        names.update(os.path.basename(chapter['filename']) for chapter in _read_index(entry.path))
        for name in names:
            path = os.path.join(entry.path, name)
            size = _cache_bytes([path, path + CACHE_META_SUFFIX] + _chapter_derivatives(path))
            if size:
                files.append((_chapter_last_used(path), size, path))
                total += size
    files.sort()
    removed = []
    protected = os.path.realpath(protect_path) if protect_path else None
    for last_used, _size, path in files:
        if total <= limit_bytes:
            break
        if protected and os.path.realpath(path) == protected:
            continue
        with _try_chapter_file_lock(path) as acquired:
            if not acquired or _chapter_last_used(path) != last_used:
                continue
            size = _cache_bytes([path, path + CACHE_META_SUFFIX] + _chapter_derivatives(path))
            _remove_cache_file(path)
        total -= size
        removed.append(path)
    return removed


def parse_propfind(payload):
    return parse_listing(payload)['items']


def parse_listing(payload):
    if not payload:
        return {'items': [], 'complete': False}
    if len(payload) > MAX_LISTING_BYTES:
        raise WebDavError('目录列表过大')
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise WebDavError('WebDAV 目录列表无法解析') from exc
    items = []
    complete = True
    for response in root.findall('{DAV:}response'):
        href = (response.findtext('{DAV:}href') or '').strip()
        prop = ElementTree.Element('{DAV:}prop')
        for propstat in response.findall('{DAV:}propstat'):
            status = propstat.findtext('{DAV:}status') or ''
            if status and ' 200 ' not in f' {status} ':
                continue
            candidate = propstat.find('{DAV:}prop')
            if candidate is not None:
                prop.extend(list(candidate))
        if not len(prop):
            complete = False
            continue
        resource = prop.find('{DAV:}resourcetype')
        if resource is None or not href:
            complete = False
        is_dir = resource is not None and resource.find('{DAV:}collection') is not None
        length_text = prop.findtext('{DAV:}getcontentlength') or '0'
        try:
            size = max(0, int(length_text))
        except ValueError:
            size = 0
        items.append({
            'href': href,
            'name': (prop.findtext('{DAV:}displayname') or '').strip(),
            'is_dir': is_dir,
            'size': size,
            'etag': (prop.findtext('{DAV:}getetag') or '').strip(),
            'modified': (prop.findtext('{DAV:}getlastmodified') or '').strip(),
        })
    if not items:
        complete = False
    return {'items': items, 'complete': complete}


def _chapters_and_cover(items, parent_path):
    chapters = []
    cover = None
    for item in _direct_children(items, parent_path):
        name = item['name']
        if item['is_dir'] or not name or name.startswith('.'):
            continue
        extension = os.path.splitext(name)[1].lower()
        if extension in CHAPTER_EXTENSIONS and not name.startswith('._') and os.path.splitext(name)[0] != '00':
            chapters.append({
                'filename': name,
                'path': item['path'].rstrip('/'),
                'size': item['size'],
                'etag': item.get('etag') or '',
                'modified': item.get('modified') or '',
            })
        elif name.lower() in COVER_NAMES and cover is None:
            cover = item
    return chapters, cover


def _direct_children(items, parent_path):
    parent = _normalize_path(parent_path).rstrip('/')
    children = []
    for item in items:
        path = _href_path(item.get('href') or '')
        if not path:
            continue
        normalized = path.rstrip('/')
        if normalized == parent:
            continue
        if not normalized.startswith(parent + '/'):
            continue
        name = normalized[len(parent) + 1:]
        if not name or '/' in name or name in {'.', '..'}:
            continue
        children.append({
            'name': item.get('name') or name,
            'path': normalized + ('/' if item.get('is_dir') else ''),
            'is_dir': bool(item.get('is_dir')),
            'size': item.get('size') or 0,
            'etag': item.get('etag') or '',
            'modified': item.get('modified') or '',
        })
    return children


def _listing_is_usable(listing, parent_path):
    return bool(listing.get('complete')) and _contains_path(listing.get('items') or [], parent_path)


def _can_prune_missing_comics(listing, parent_path):
    if not _listing_is_usable(listing, parent_path):
        return False
    return any(item['is_dir'] for item in _direct_children(listing.get('items') or [], parent_path))


def _contains_path(items, parent_path):
    parent = _normalize_path(parent_path).rstrip('/')
    for item in items:
        path = _href_path(item.get('href') or '')
        if path and path.rstrip('/') == parent:
            return True
    return False


def _propfind(config, url):
    response = _request(config, 'PROPFIND', url, data=(
        b'<?xml version="1.0"?>'
        b'<D:propfind xmlns:D="DAV:"><D:prop>'
        b'<D:displayname/><D:getcontentlength/><D:resourcetype/><D:getetag/><D:getlastmodified/>'
        b'</D:prop></D:propfind>'
    ), headers={'Depth': '1', 'Content-Type': 'application/xml'})
    if response.status_code == 401:
        raise WebDavError('WebDAV 账号或密码不正确')
    if response.status_code in {301, 302, 303, 307, 308}:
        raise WebDavError('WebDAV 返回了跳转。Alist 请把 WebDAV 策略设为本地代理')
    if response.status_code != 207:
        raise WebDavError(f'WebDAV 目录读取失败（{response.status_code}）')
    return parse_listing(response.content)


def _download(config, chapter, destination):
    url = _url_for(config, chapter['path'])
    response = _request(config, 'GET', url, stream=True)
    try:
        if response.status_code == 401:
            raise WebDavError('WebDAV 账号或密码不正确')
        if response.status_code in {301, 302, 303, 307, 308}:
            raise WebDavError('WebDAV 下载被跳转，无法保存整章文件')
        if response.status_code != 200:
            raise WebDavError(f'章节下载失败（{response.status_code}）')
        expected = int(chapter.get('size') or 0)
        length = response.headers.get('Content-Length')
        if length:
            try:
                expected_header = int(length)
            except ValueError as exc:
                raise WebDavError('章节大小无效') from exc
            if expected_header > MAX_CHAPTER_BYTES:
                raise WebDavError('章节超过 2GB，未下载')
            if expected and expected_header != expected:
                raise WebDavError('章节大小与索引不一致')
            expected = expected_header
        elif expected > MAX_CHAPTER_BYTES:
            raise WebDavError('章节超过 2GB，未下载')
        ensure_directory(os.path.dirname(destination))
        written = 0
        with open(destination, 'wb') as handle:
            for chunk in response.iter_content(1024 * 256):
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_CHAPTER_BYTES:
                    raise WebDavError('章节超过 2GB，未下载')
                handle.write(chunk)
        if expected and written != expected:
            raise WebDavError('章节下载不完整')
        if written <= 0:
            raise WebDavError('章节内容为空')
    except requests.RequestException as exc:
        raise WebDavError('章节下载中断，请重试') from exc
    finally:
        response.close()


def _save_cover(config, comic_name, cover):
    if not cover:
        return
    destination = os.path.join(COVER_ROOT, comic_name + '.jpg')
    if os.path.isfile(destination):
        return
    if cover['size'] and cover['size'] > MAX_COVER_BYTES:
        return
    response = _request(config, 'GET', _url_for(config, cover['path']), stream=True)
    try:
        if response.status_code != 200:
            return
        chunks = []
        total = 0
        for chunk in response.iter_content(65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_COVER_BYTES:
                return
            chunks.append(chunk)
        content = b''.join(chunks)
    except requests.RequestException as exc:
        raise WebDavError('封面下载中断，请重试') from exc
    finally:
        response.close()
    if content:
        normalize_cover_bytes(content, destination)


def _request(config, method, url, data=None, headers=None, stream=False, session=None, timeout=(15, 300)):
    _assert_same_origin(config['url'], url)
    request_headers = {'User-Agent': 'MangaDock'}
    if headers:
        request_headers.update(headers)
    try:
        return (session.request if session is not None else requests.request)(
            method,
            url,
            data=data,
            headers=request_headers,
            auth=(config.get('username') or '', config.get('password') or ''),
            timeout=timeout,
            stream=stream,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise WebDavError('无法连接 WebDAV：' + str(exc)) from exc


def _require_config():
    config = _read_config()
    if not config.get('url') or not config.get('password'):
        raise WebDavError('请先保存 WebDAV 连接')
    return config


def _library_url(config):
    return _url_for(config, _library_path(config))


def _library_path(config):
    base_path = _path_of(config['url']).rstrip('/')
    root = (config.get('root') or '').strip('/')
    path = base_path + ('/' + root if root else '')
    if not path.endswith('/'):
        path += '/'
    return _normalize_path(path)


def _url_for(config, path):
    parsed = urlparse(config['url'])
    origin = f'{parsed.scheme}://{parsed.netloc}'
    cleaned = _normalize_path(path)
    encoded = '/'.join(quote(part, safe='') for part in cleaned.split('/'))
    if cleaned.endswith('/') and not encoded.endswith('/'):
        encoded += '/'
    return origin + encoded


def _href_path(href):
    raw = (href or '').strip()
    if raw.startswith('http://') or raw.startswith('https://'):
        raw = urlparse(raw).path
    else:
        raw = raw.split('?', 1)[0]
    return _normalize_path(unquote(raw))


def _normalize_path(path):
    raw = (path or '').replace('\\', '/')
    trailing = raw.endswith('/')
    parts = []
    for part in raw.split('/'):
        if not part or part == '.':
            continue
        if part == '..':
            raise WebDavError('WebDAV 路径不合法')
        parts.append(part)
    cleaned = '/' + '/'.join(parts)
    if trailing and cleaned != '/':
        cleaned += '/'
    return cleaned


def _path_of(url):
    return _normalize_path(unquote(urlparse(url).path or '/'))


def _assert_same_origin(base_url, url):
    base = urlparse(base_url)
    target = urlparse(url)
    if target.scheme not in {'http', 'https'} or target.netloc != base.netloc:
        raise WebDavError('WebDAV 地址超出已保存的服务器')


def _validate_base_url(url):
    parsed = urlparse((url or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise WebDavError('WebDAV 地址需要是 http 或 https，且不要把账号写在地址里')
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() == 'metadata.google.internal':
            raise WebDavError('这个 WebDAV 地址不能使用')
        return parsed._replace(params='', query='', fragment='').geturl().rstrip('/')
    if address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
        raise WebDavError('这个 WebDAV 地址不能使用')
    if not (address.is_loopback or address.is_private or address.is_global):
        raise WebDavError('这个 WebDAV 地址不能使用')
    return parsed._replace(params='', query='', fragment='').geturl().rstrip('/')


def _validate_root(root):
    text = (root or '').strip()
    if not text:
        return ''
    if text.startswith('http://') or text.startswith('https://'):
        raise WebDavError('漫画根目录填服务器上的路径，例如 /nas，不要填完整网址')
    return _normalize_path(unquote(text)).rstrip('/')


def _validate_cache_gb(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_CACHE_GB
    return min(200, max(1, number))


def _cache_limit_bytes(config):
    return int(config.get('cache_gb') or DEFAULT_CACHE_GB) * 1024 * 1024 * 1024


def _read_index(comic_dir):
    return _read_index_payload(comic_dir)['chapters']


def _read_index_payload(comic_dir):
    path = os.path.join(comic_dir, INDEX_FILENAME)
    try:
        with open(path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {'source': '', 'chapters': []}
    if not isinstance(payload, dict):
        return {'source': '', 'chapters': []}
    chapters = payload.get('chapters')
    if not isinstance(chapters, list):
        chapters = []
    return {
        'source': str(payload.get('source') or ''),
        'chapters': [
            chapter for chapter in chapters
            if isinstance(chapter, dict) and chapter.get('filename') and chapter.get('path')
        ],
    }


def _write_index(comic_dir, chapters, source):
    _atomic_json(os.path.join(comic_dir, INDEX_FILENAME), {
        'source': source,
        'chapters': chapters,
    })


def _source_id(config):
    raw = '\n'.join((
        config.get('url') or '',
        config.get('root') or '',
        (config.get('username') or '').strip(),
    ))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _stored_source_visible(payload):
    stored = str((payload or {}).get('source') or '')
    config = _read_config()
    if not config.get('url'):
        return False
    if not stored:
        return True
    return stored == _source_id(config)


def _chapter_identity(chapter, source):
    return {
        'source': source or '',
        'path': chapter.get('path') or '',
        'size': int(chapter.get('size') or 0),
        'etag': chapter.get('etag') or '',
        'modified': chapter.get('modified') or '',
    }


def _cache_matches(destination, chapter, source):
    if not os.path.isfile(destination):
        return False
    meta_path = destination + CACHE_META_SUFFIX
    try:
        with open(meta_path, encoding='utf-8') as handle:
            meta = json.load(handle)
        stat = os.stat(destination)
    except (OSError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    identity = _chapter_identity(chapter, source)
    if any(meta.get(key) != value for key, value in identity.items()):
        return False
    return stat.st_size == meta.get('local_size') and stat.st_mtime_ns == meta.get('local_mtime_ns')


def _content_digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def cached_chapter_version(path):
    if not is_cache_path(path):
        return None
    try:
        with open(path + CACHE_META_SUFFIX, encoding='utf-8') as handle:
            meta = json.load(handle)
        stat = os.stat(path)
        if not isinstance(meta, dict) or stat.st_size != meta.get('local_size') or stat.st_mtime_ns != meta.get('local_mtime_ns'):
            return None
        identity = {key: meta.get(key) for key in ('source', 'path', 'size', 'etag', 'modified')}
        if not meta.get('etag') or str(meta['etag']).startswith('W/'):
            if not meta.get('content_hash'):
                meta['content_hash'] = _content_digest(path)
                _atomic_json(path + CACHE_META_SUFFIX, meta)
            identity['content_hash'] = meta['content_hash']
        signature = json.dumps(identity, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(signature.encode('utf-8')).hexdigest()[:12]
    except (OSError, ValueError):
        return None


def _write_cache_meta(destination, chapter, source):
    stat = os.stat(destination)
    payload = _chapter_identity(chapter, source)
    payload['local_size'] = stat.st_size
    payload['local_mtime_ns'] = stat.st_mtime_ns
    payload['last_used'] = time.time_ns()
    if not payload.get('etag') or str(payload['etag']).startswith('W/'):
        payload['content_hash'] = _content_digest(destination)
    _atomic_json(destination + CACHE_META_SUFFIX, payload)


def _remove_cache_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    try:
        os.remove(path + CACHE_META_SUFFIX)
    except FileNotFoundError:
        pass

    for derivative in _chapter_derivatives(path):
        if os.path.isdir(derivative):
            shutil.rmtree(derivative)
        else:
            try:
                os.remove(derivative)
            except FileNotFoundError:
                pass
            parent = os.path.dirname(derivative)
            if os.path.basename(parent) != '.repaired':
                try:
                    os.rmdir(parent)
                except OSError:
                    pass


def _valid_chapter_file(path, filename):
    extension = os.path.splitext(filename)[1].lower()
    try:
        if os.path.getsize(path) <= 0:
            return False
        with open(path, 'rb') as handle:
            header = handle.read(64).lstrip().lower()
    except OSError:
        return False
    if header.startswith((b'<!doctype', b'<html', b'{', b'<error')):
        return False
    if extension == '.pdf':
        return header.startswith(b'%pdf-')
    if extension != '.cbz':
        return False
    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                return False
            return any(name and not name.endswith('/') for name in archive.namelist())
    except zipfile.BadZipFile:
        return False


@contextmanager
def _chapter_file_lock(destination):
    with _chapter_lock(destination, blocking=True) as acquired:
        if not acquired:
            raise WebDavError('章节缓存正忙')
        yield


@contextmanager
def chapter_range_read_lock(destination):
    """Allow parallel range readers while keeping cache mutation/eviction exclusive."""
    with _open_lock_file(os.path.dirname(destination)) as directory_handle:
        fcntl.flock(directory_handle.fileno(), fcntl.LOCK_SH)
        with _open_lock_file(destination) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            yield


@contextmanager
def _try_chapter_file_lock(destination):
    with _chapter_lock(destination, blocking=False) as acquired:
        yield acquired


def _open_lock_file(identity):
    root = os.path.join(app.instance_path, 'webdav_locks')
    ensure_directory(root)
    digest = hashlib.sha256(os.path.abspath(identity).encode('utf-8')).hexdigest()
    return open(os.path.join(root, digest + '.lock'), 'a+')


def _prune_comic_cache(comic_dir, blocking=False):
    """Defer directory deletion while any chapter operation is active."""
    if not is_cache_path(comic_dir):
        return False
    with _open_lock_file(comic_dir) as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            if os.path.isdir(comic_dir):
                from mangadock.services.comic_delete import _remove_tree
                from mangadock.services.webdav_metadata import clear_metadata
                clear_metadata(comic_dir)
                _remove_tree(comic_dir)
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _chapter_lock(destination, blocking):
    thread_lock = _lock_for(destination)
    if not thread_lock.acquire(blocking=blocking):
        yield False
        return
    try:
        with _open_lock_file(os.path.dirname(destination)) as directory_handle:
            flags = fcntl.LOCK_SH if blocking else fcntl.LOCK_SH | fcntl.LOCK_NB
            try:
                fcntl.flock(directory_handle.fileno(), flags)
            except BlockingIOError:
                yield False
                return
            try:
                with _open_lock_file(destination) as handle:
                    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                    try:
                        fcntl.flock(handle.fileno(), flags)
                    except BlockingIOError:
                        yield False
                        return
                    try:
                        yield True
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                fcntl.flock(directory_handle.fileno(), fcntl.LOCK_UN)
    finally:
        thread_lock.release()


def _chapter_last_used(path):
    try:
        with open(path + CACHE_META_SUFFIX if os.path.isfile(path + CACHE_META_SUFFIX) else os.path.join(path + '.webdav-ranges', 'meta.json'), encoding='utf-8') as handle:
            meta = json.load(handle)
        if isinstance(meta, dict) and meta.get('last_used'):
            return int(meta['last_used'])
    except (OSError, ValueError):
        pass
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def _invalidate_other_source_caches(new_source):
    root = cache_root()
    if not os.path.isdir(root):
        return
    for entry in list(os.scandir(root)):
        if entry.is_dir():
            _invalidate_comic_cache(entry.path, keep_source=new_source)


def _invalidate_comic_cache(comic_dir, keep_source=None):
    if not is_cache_path(comic_dir) or not os.path.isdir(comic_dir):
        return
    payload = _read_index_payload(comic_dir)
    if keep_source and payload.get('source') == keep_source:
        return
    from mangadock.services.webdav_metadata import clear_metadata
    clear_metadata(comic_dir)
    index_path = os.path.join(comic_dir, INDEX_FILENAME)
    try:
        os.remove(index_path)
    except FileNotFoundError:
        pass
    for chapter in list(os.scandir(comic_dir)):
        if not chapter.is_file():
            continue
        if os.path.splitext(chapter.name)[1].lower() not in CHAPTER_EXTENSIONS:
            continue
        with _try_chapter_file_lock(chapter.path) as acquired:
            if not acquired:
                continue
            _remove_cache_file(chapter.path)


def _config_path():
    return os.path.join(app.instance_path, CONFIG_FILENAME)


def _read_config():
    path = _config_path()
    try:
        with open(path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    password = payload.get('password') or ''
    payload['password'] = _decrypt(password) if password else ''
    return payload


def _write_config(config):
    stored = dict(config)
    stored['password'] = _encrypt(config.get('password') or '')
    ensure_directory(app.instance_path)
    _atomic_json(_config_path(), stored)
    try:
        os.chmod(_config_path(), 0o600)
    except OSError:
        pass


def _atomic_json(path, payload):
    directory = os.path.dirname(path) or '.'
    ensure_directory(directory)
    handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory, delete=False)
    try:
        json.dump(payload, handle, ensure_ascii=False)
        handle.close()
        os.replace(handle.name, path)
    finally:
        if os.path.exists(handle.name):
            try:
                os.remove(handle.name)
            except OSError:
                pass


def _cipher():
    key = hashlib.sha256(app.secret_key.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _encrypt(password):
    if not password:
        return ''
    return _cipher().encrypt(password.encode('utf-8')).decode('ascii')


def _decrypt(password):
    try:
        return _cipher().decrypt(password.encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError):
        return ''


def _lock_for(path):
    with _download_guard:
        lock = _download_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _download_locks[path] = lock
        return lock
