# -*- coding: utf-8 -*-
"""Read the local EPUB novel library without adding an EPUB dependency."""
import html
import os
import posixpath
import re
from datetime import datetime
from functools import lru_cache
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from PIL import Image

from mangadock.settings import NOVEL_COVER_ROOT, NOVEL_ROOT, china_tz


NOVEL_PROGRESS_PREFIX = "novel:"


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
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_path), href.split('#', 1)[0]))


def _read_package(epub):
    container = ET.fromstring(epub.read('META-INF/container.xml'))
    rootfile = next(
        element for element in container.iter()
        if _local_name(element.tag) == 'rootfile'
    )
    package_path = rootfile.attrib['full-path']
    package = ET.fromstring(epub.read(package_path))
    return package_path, package


@lru_cache(maxsize=128)
def _metadata_for_file(file_path, modified_ns):
    del modified_ns
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
            'description': _clean_text(_first_text(package, 'description')),
            'publisher': _first_text(package, 'publisher'),
            'language': _first_text(package, 'language'),
            'package_path': package_path,
            'manifest': manifest,
            'cover_path': _resolve_epub_path(package_path, cover_item.get('href', '')) if cover_item else '',
            'cover_mimetype': cover_item.get('media-type', 'image/jpeg') if cover_item else 'image/jpeg',
            'spine': spine_items,
        }


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
            metadata = _metadata(file_path)
        except (OSError, BadZipFile, ET.ParseError, KeyError, StopIteration):
            continue
        file_stem = os.path.splitext(filename)[0]
        fallback_title, _, fallback_author = file_stem.rpartition('_')
        novels.append({
            'novel_id': file_stem,
            'title': metadata['title'] or fallback_title or file_stem,
            'author': metadata['author'] or fallback_author or '未知作者',
            'description': metadata['description'],
            'publisher': metadata['publisher'],
            'language': metadata['language'],
            'chapter_count': len(metadata['spine']),
            'file_path': file_path,
            'created_at': datetime.fromtimestamp(os.path.getmtime(file_path), tz=china_tz),
        })
    return sorted(novels, key=lambda item: item['created_at'], reverse=True)


def get_novel(novel_id):
    if not novel_id or '/' in novel_id or '\\' in novel_id:
        return None
    return next((novel for novel in get_novels() if novel['novel_id'] == novel_id), None)


@lru_cache(maxsize=64)
def _chapter_titles(file_path, modified_ns):
    del modified_ns
    metadata = _metadata(file_path)
    title_by_path = {}
    with ZipFile(file_path) as epub:
        nav_item = next((
            item for item in metadata['manifest'].values()
            if 'nav' in item.get('properties', '').split()
        ), None)
        if nav_item:
            nav_path = _resolve_epub_path(metadata['package_path'], nav_item.get('href', ''))
            soup = BeautifulSoup(epub.read(nav_path), 'html.parser')
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
    if chapter_index < 0 or chapter_index >= len(metadata['spine']):
        return None
    chapter_path = metadata['spine'][chapter_index]['href']
    with ZipFile(novel['file_path']) as epub:
        soup = BeautifulSoup(epub.read(chapter_path), 'html.parser')
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


def get_novel_cover(novel_id):
    novel = get_novel(novel_id)
    if not novel:
        return None
    custom_cover_path = os.path.join(NOVEL_COVER_ROOT, f'{novel_id}.jpg')
    if os.path.isfile(custom_cover_path):
        with open(custom_cover_path, 'rb') as cover_file:
            return cover_file.read(), 'image/jpeg'
    metadata = _metadata(novel['file_path'])
    cover_path = metadata.get('cover_path')
    if not cover_path:
        return None
    with ZipFile(novel['file_path']) as epub:
        try:
            content = epub.read(cover_path)
        except KeyError:
            return None
    return content, metadata.get('cover_mimetype') or 'image/jpeg'


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
    return cover_path
