# -*- coding: utf-8 -*-
"""Read the local EPUB novel library without adding an EPUB dependency."""
import html
import hashlib
import logging
import os
import posixpath
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from io import BytesIO
from threading import Lock, get_ident
from zipfile import BadZipFile, ZipFile
from urllib.parse import unquote
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from PIL import Image

from mangadock.settings import NOVEL_COVER_ROOT, NOVEL_ROOT, china_tz


logger = logging.getLogger(__name__)

NOVEL_PROGRESS_PREFIX = "novel:"
NOVEL_COVER_CACHE_VERSION = 2
_cover_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='novel-cover-upscale')
_cover_pending = set()
_cover_lock = Lock()
MAX_EPUB_METADATA_BYTES = 2 * 1024 * 1024
MAX_EPUB_CHAPTER_BYTES = 16 * 1024 * 1024
MAX_EPUB_COVER_BYTES = 8 * 1024 * 1024

# EPUB 解析失败时 `_metadata()` 的返回值。
# 刻意用「正常返回值」而不是抛异常：functools.lru_cache 不缓存异常，
# 那样损坏的 EPUB 会在每次 get_novels() 时被重新打开并解析一遍中央目录
# （实测 17MB 的书 6ms/次，而一次「打开章节」会触发两次全目录扫描）。
METADATA_UNREADABLE = None

# 视为「这本书读不了」的异常集合（原先散落在 get_novels 的 except 里）。
EPUB_UNREADABLE_ERRORS = (
    OSError, BadZipFile, ET.ParseError, KeyError, StopIteration,
    ValueError, IndexError, EOFError,
)


def novel_progress_key(novel_id):
    return f"{NOVEL_PROGRESS_PREFIX}{novel_id}"


def _local_name(tag):
    return tag.rsplit('}', 1)[-1]


def _first_text(root, name):
    for element in root.iter():
        if _local_name(element.tag) == name and element.text:
            return element.text.strip()
    return ''


def _clean_text(value, limit=2000):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    text = re.sub(r'\s+', ' ', html.unescape(text)).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + '…'
    return text


def _resolve_epub_path(base_path, href):
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_path), unquote(href.split('#', 1)[0])))


def _read_epub_entry(epub, name, max_bytes):
    """Bound decompression even when a ZIP entry's declared size is wrong."""
    if epub.getinfo(name).file_size > max_bytes:
        raise ValueError('EPUB 条目超过大小限制')
    with epub.open(name) as source:
        content = source.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError('EPUB 条目超过大小限制')
    return content


def _read_package(epub):
    container_path = 'META-INF/container.xml'
    if container_path not in epub.namelist():
        candidates = [
            name for name in epub.namelist()
            if name.endswith('/' + container_path)
            and not any(part.startswith('._') or part == '__MACOSX' for part in name.split('/'))
        ]
        if len(candidates) != 1:
            raise ValueError('EPUB 入口文件缺失或不唯一')
        container_path = candidates[0]
    # full-path 相对于 EPUB 根目录；有些文件打包时额外套了外层目录。
    epub_root = container_path[:-len('META-INF/container.xml')]
    container = ET.fromstring(_read_epub_entry(epub, container_path, MAX_EPUB_METADATA_BYTES))
    rootfile = next(
        element for element in container.iter()
        if _local_name(element.tag) == 'rootfile'
    )
    package_path = posixpath.normpath(posixpath.join(epub_root, rootfile.attrib['full-path']))
    package = ET.fromstring(_read_epub_entry(epub, package_path, MAX_EPUB_METADATA_BYTES))
    return package_path, package


def _parse_metadata(file_path):
    """真正解析 EPUB 元数据；失败时向外抛异常，由调用方兜底。"""
    with ZipFile(file_path) as epub:
        package_path, package = _read_package(epub)
        manifest = {
            element.attrib.get('id'): element.attrib
            for element in package.iter()
            if _local_name(element.tag) == 'item' and element.attrib.get('id')
        }
        cover_id = ''
        for element in package.iter():
            if _local_name(element.tag) == 'meta' and element.attrib.get('name') == 'cover':
                cover_id = element.attrib.get('content', '')
                break
        cover_item = next((
            item for item in manifest.values()
            if 'cover-image' in item.get('properties', '').split()
        ), None) or manifest.get(cover_id)

        spine_items = []
        for element in package.iter():
            if _local_name(element.tag) != 'itemref':
                continue
            item = manifest.get(element.attrib.get('idref'))
            if not item or 'html' not in item.get('media-type', ''):
                continue
            href = item.get('href', '')
            properties = item.get('properties', '').split()
            if 'nav' in properties or 'cover' in href.lower():
                continue
            spine_items.append({
                'href': _resolve_epub_path(package_path, href),
                'manifest_href': href,
            })

        return {
            'title': _first_text(package, 'title'),
            'author': _first_text(package, 'creator'),
            'identifier': _first_text(package, 'identifier'),
            'source': _first_text(package, 'source'),
            'description': _clean_text(_first_text(package, 'description')),
            'publisher': _first_text(package, 'publisher'),
            'language': _first_text(package, 'language'),
            'package_path': package_path,
            'manifest': manifest,
            'cover_path': _resolve_epub_path(package_path, cover_item.get('href', '')) if cover_item else '',
            'cover_mimetype': cover_item.get('media-type', 'image/jpeg') if cover_item else 'image/jpeg',
            'spine': spine_items,
        }


@lru_cache(maxsize=128)
def _metadata_for_file(file_path, modified_ns):
    """带缓存的元数据读取；解析失败返回 METADATA_UNREADABLE。

    失败也走正常返回值是刻意的 —— lru_cache 不缓存异常，
    而 get_novel_chapter() 会经由 get_novels() 触发两次全目录扫描，
    于是损坏的 EPUB 每次都被重新打开 + 解析中央目录。
    """
    del modified_ns
    try:
        return _parse_metadata(file_path)
    except EPUB_UNREADABLE_ERRORS as exc:
        logger.warning(
            'EPUB 解析失败，已标记为不可读（按 mtime 缓存，文件更新后自动重试）：'
            '%s —— %s: %s',
            file_path, type(exc).__name__, exc,
        )
        return METADATA_UNREADABLE


def _metadata(file_path):
    stat = os.stat(file_path)
    return _metadata_for_file(file_path, stat.st_mtime_ns)


def get_novels():
    novels = []
    if not os.path.isdir(NOVEL_ROOT):
        return novels
    for filename in os.listdir(NOVEL_ROOT):
        if filename.startswith('._') or not filename.lower().endswith('.epub'):
            continue
        file_path = os.path.join(NOVEL_ROOT, filename)
        if not os.path.isfile(file_path):
            continue
        try:
            file_stat = os.stat(file_path)
            metadata = _metadata(file_path)
        except OSError:
            continue
        if metadata is METADATA_UNREADABLE:
            continue
        file_stem = os.path.splitext(filename)[0]
        book_id = _fanqie_book_id(metadata)
        fallback_stem = file_stem.removesuffix(f'_{book_id}') if book_id else file_stem
        fallback_title, _, fallback_author = fallback_stem.rpartition('_')
        custom_cover_path = os.path.join(NOVEL_COVER_ROOT, f'{file_stem}.jpg')
        cover_source_path = custom_cover_path if os.path.isfile(custom_cover_path) else file_path
        cover_stat = os.stat(cover_source_path)
        source_version = (f'{NOVEL_COVER_CACHE_VERSION}-{cover_stat.st_mtime_ns}-'
                          f'{cover_stat.st_size}-'
                          f'{"custom" if cover_source_path == custom_cover_path else "epub"}')
        enhanced_path = _novel_upscaled_cover_path(file_stem, source_version)
        try:
            enhanced_version = os.stat(enhanced_path).st_mtime_ns
        except OSError:
            enhanced_version = None
        novels.append({
            'novel_id': file_stem,
            'title': metadata['title'] or fallback_title or file_stem,
            'author': metadata['author'] or fallback_author or '未知作者',
            'description': metadata['description'],
            'publisher': metadata['publisher'],
            'language': metadata['language'],
            'chapter_count': len(metadata['spine']),
            'file_path': file_path,
            'fanqie_book_id': book_id,
            'created_at': datetime.fromtimestamp(file_stat.st_mtime, tz=china_tz),
            'cover_source_version': source_version,
            'cover_version': (f'{source_version}-{enhanced_version}-sr'
                              if enhanced_version is not None else source_version + '-source'),
        })
    return sorted(novels, key=lambda item: item['created_at'], reverse=True)


def get_novel(novel_id):
    if not novel_id or '/' in novel_id or '\\' in novel_id:
        return None
    return next((novel for novel in get_novels() if novel['novel_id'] == novel_id), None)


def _fanqie_book_id(metadata):
    for value in (metadata.get('source'), metadata.get('identifier')):
        match = re.search(r'(?:fanqie-|/page/)(\d{8,24})', str(value or ''))
        if match:
            return match.group(1)
    return ''


def get_novel_by_fanqie_id(book_id):
    book_id = str(book_id or '')
    return next((novel for novel in get_novels() if novel.get('fanqie_book_id') == book_id), None)


@lru_cache(maxsize=64)
def _chapter_titles(file_path, modified_ns):
    del modified_ns
    metadata = _metadata(file_path)
    if metadata is METADATA_UNREADABLE:
        return []
    title_by_path = {}
    with ZipFile(file_path) as epub:
        nav_item = next((
            item for item in metadata['manifest'].values()
            if 'nav' in item.get('properties', '').split()
        ), None)
        if nav_item:
            nav_path = _resolve_epub_path(metadata['package_path'], nav_item.get('href', ''))
            try:
                nav_content = _read_epub_entry(epub, nav_path, MAX_EPUB_METADATA_BYTES)
            except ValueError:
                nav_content = None
            if nav_content is not None:
                soup = BeautifulSoup(nav_content, 'html.parser')
                for link in soup.find_all('a', href=True):
                    target = _resolve_epub_path(nav_path, link['href'])
                    title_by_path[target] = _clean_text(link.get_text(' ', strip=True), 300)

    chapters = []
    for index, item in enumerate(metadata['spine']):
        chapters.append({
            'number': index,
            'index': index,
            'title': title_by_path.get(item['href']) or ('作品简介' if index == 0 else f'第 {index} 章'),
            'filename': posixpath.basename(item['href']),
            'format': 'EPUB',
        })
    return chapters


def get_novel_chapters(novel_id):
    novel = get_novel(novel_id)
    if not novel:
        return []
    stat = os.stat(novel['file_path'])
    return list(_chapter_titles(novel['file_path'], stat.st_mtime_ns))


def get_novel_chapter(novel_id, chapter_index):
    novel = get_novel(novel_id)
    if not novel:
        return None
    metadata = _metadata(novel['file_path'])
    if metadata is METADATA_UNREADABLE:
        return None
    if chapter_index < 0 or chapter_index >= len(metadata['spine']):
        return None
    chapter_path = metadata['spine'][chapter_index]['href']
    try:
        with ZipFile(novel['file_path']) as epub:
            content = _read_epub_entry(epub, chapter_path, MAX_EPUB_CHAPTER_BYTES)
    except (KeyError, ValueError, OSError, BadZipFile):
        return None
    soup = BeautifulSoup(content, 'html.parser')
    for unwanted in soup(['script', 'style', 'noscript', 'svg']):
        unwanted.decompose()
    body = soup.body or soup
    paragraphs = []
    for element in body.find_all(['h1', 'h2', 'h3', 'p', 'blockquote', 'li']):
        text = _clean_text(element.get_text(' ', strip=True), 10000)
        if text and (not paragraphs or paragraphs[-1] != text):
            paragraphs.append(text)
    if not paragraphs:
        paragraphs = [
            _clean_text(line, 10000)
            for line in body.get_text('\n', strip=True).splitlines()
            if _clean_text(line, 10000)
        ]
    chapters = get_novel_chapters(novel_id)
    return {
        'index': chapter_index,
        'title': chapters[chapter_index]['title'],
        'paragraphs': paragraphs,
        'total_chapters': len(metadata['spine']),
    }


def _get_novel_source_cover_file(novel):
    """Return a local cover file, extracting the EPUB cover only when needed."""
    novel_id = novel['novel_id']
    custom_cover_path = os.path.join(NOVEL_COVER_ROOT, f'{novel_id}.jpg')
    if os.path.isfile(custom_cover_path):
        return custom_cover_path, 'image/jpeg'

    source_stat = os.stat(novel['file_path'])
    generated_root = os.path.join(NOVEL_COVER_ROOT, f'.generated-v{NOVEL_COVER_CACHE_VERSION}')
    generated_cover_path = os.path.join(generated_root, f'{novel_id}.jpg')
    try:
        generated_stat = os.stat(generated_cover_path)
        if generated_stat.st_mtime_ns == source_stat.st_mtime_ns:
            return generated_cover_path, 'image/jpeg'
    except OSError:
        pass

    metadata = _metadata(novel['file_path'])
    if metadata is METADATA_UNREADABLE:
        return None
    cover_path = metadata.get('cover_path')
    if not cover_path:
        return None
    with ZipFile(novel['file_path']) as epub:
        try:
            content = _read_epub_entry(epub, cover_path, MAX_EPUB_COVER_BYTES)
        except (KeyError, ValueError):
            return None

    try:
        image = Image.open(BytesIO(content))
        if image.width * image.height > 20_000_000:
            # Large JPEG covers can be decoded at reduced resolution safely.
            image.draft('RGB', (1920, 1920))
            if image.width * image.height > 20_000_000:
                return None
        image.load()
        if image.mode in {'RGBA', 'LA'}:
            canvas = Image.new('RGB', image.size, 'white')
            alpha = image.getchannel('A')
            canvas.paste(image.convert('RGB'), mask=alpha)
            image = canvas
        elif image.mode != 'RGB':
            image = image.convert('RGB')

        os.makedirs(generated_root, exist_ok=True)
        temporary_path = f'{generated_cover_path}.{os.getpid()}.{get_ident()}.tmp'
        try:
            image.save(temporary_path, 'JPEG', quality=84, optimize=True)
            os.replace(temporary_path, generated_cover_path)
            os.utime(
                generated_cover_path,
                ns=(source_stat.st_mtime_ns, source_stat.st_mtime_ns),
            )
        finally:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
    except (OSError, ValueError, Image.DecompressionBombError):
        return None

    return generated_cover_path, 'image/jpeg'


def _novel_upscaled_cover_path(novel_id, source_version):
    key = hashlib.sha256(f'{novel_id}\0{source_version}'.encode('utf-8')).hexdigest()
    return os.path.join(NOVEL_COVER_ROOT, 'upscaled', f'{key}.jpg')


def _upscale_novel_cover(source_path, target_path):
    from mangadock.utils.cover_enhance import (
        _CROSS_LOCK_UNAVAILABLE, _cross_process_lock, _release_lock, enhance_cover_file,
    )

    handle = _cross_process_lock(f'novel:{target_path}')
    if handle is None:
        return target_path if os.path.isfile(target_path) else None
    try:
        if os.path.isfile(target_path):
            return target_path
        return enhance_cover_file(
            source_path, target_path, force_pillow=(handle is _CROSS_LOCK_UNAVAILABLE),
        )
    finally:
        if handle is not _CROSS_LOCK_UNAVAILABLE:
            _release_lock(handle)


def _request_novel_cover_upscale(source_path, target_path):
    def worker():
        try:
            _upscale_novel_cover(source_path, target_path)
        except Exception:
            logger.exception('小说封面超分失败：%s', source_path)
        finally:
            with _cover_lock:
                _cover_pending.discard(target_path)

    with _cover_lock:
        if target_path in _cover_pending or os.path.isfile(target_path):
            return
        _cover_pending.add(target_path)
        try:
            _cover_pool.submit(worker)
        except Exception:
            _cover_pending.discard(target_path)
            logger.exception('小说封面超分排队失败：%s', source_path)


def get_novel_cover_file(novel_id, enhanced=True):
    """Prefer the enhanced cache; queue missing covers without delaying the request."""
    novel = get_novel(novel_id)
    if not novel:
        return None
    target = _novel_upscaled_cover_path(novel_id, novel['cover_source_version'])
    if enhanced and os.path.isfile(target):
        return target, 'image/jpeg'
    source = _get_novel_source_cover_file(novel)
    if source and enhanced:
        _request_novel_cover_upscale(source[0], target)
    return source


def ensure_novel_cover(novel_id):
    """Synchronously enhance an existing cover for the backfill tool."""
    novel = get_novel(novel_id)
    if not novel:
        return None
    target = _novel_upscaled_cover_path(novel_id, novel['cover_source_version'])
    if os.path.isfile(target):
        return target
    source = _get_novel_source_cover_file(novel)
    return _upscale_novel_cover(source[0], target) if source else None


def get_novel_cover(novel_id):
    cover_file = get_novel_cover_file(novel_id)
    if not cover_file:
        return None
    cover_path, mimetype = cover_file
    with open(cover_path, 'rb') as cover_handle:
        return cover_handle.read(), mimetype


def save_novel_cover(novel_id, file_storage):
    if not get_novel(novel_id):
        raise ValueError('小说不存在')
    if not file_storage or not getattr(file_storage, 'filename', ''):
        raise ValueError('请选择封面图片')
    if file_storage.mimetype and not file_storage.mimetype.lower().startswith('image/'):
        raise ValueError('仅支持上传图片文件')

    try:
        file_storage.stream.seek(0)
        image = Image.open(file_storage.stream)
        if image.format not in {'JPEG', 'PNG', 'WEBP'}:
            raise ValueError('仅支持 JPG、PNG 或 WEBP 图片')
        image.load()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('封面图片无法识别，请更换文件后重试') from exc

    if image.mode != 'RGB':
        image = image.convert('RGB')
    if image.width > 1800:
        height = int(image.height * (1800 / image.width))
        resample = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS')
        image = image.resize((1800, height), resample)

    os.makedirs(NOVEL_COVER_ROOT, exist_ok=True)
    cover_path = os.path.join(NOVEL_COVER_ROOT, f'{novel_id}.jpg')
    image.save(cover_path, 'JPEG', quality=92, optimize=True)
    get_novel_cover_file(novel_id)
    return cover_path
