"""Seekable WebDAV CBZ backed by bounded HTTP Range requests."""
import io
import json
import os
import re
import shutil
import time

import requests

from mangadock.services import webdav

BLOCK_BYTES = 256 * 1024
MAX_READ_BYTES = 64 * 1024 * 1024


class RangeUnsupported(webdav.WebDavError):
    pass


class RemoteCbz(io.RawIOBase):
    def __init__(self, config, chapter, source, file_path):
        super().__init__()
        self.config, self.chapter = config, chapter
        self.size = int(chapter.get('size') or 0)
        if not 0 < self.size <= webdav.MAX_CHAPTER_BYTES:
            raise RangeUnsupported('章节大小未知，使用整章缓存')
        self.position = 0
        self.directory = file_path + '.webdav-ranges'
        self.meta_path = os.path.join(self.directory, 'meta.json')
        self.identity = webdav._chapter_identity(chapter, source)
        # Only keep blocks across requests when the index carries a version validator.
        try:
            with open(self.meta_path, encoding='utf-8') as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        etag = chapter.get('etag') or ''
        version = etag if etag and not etag.startswith('W/') else chapter.get('modified') or ''
        if meta.get('identity') != self.identity or not version:
            if os.path.isdir(self.directory):
                shutil.rmtree(self.directory)
            meta = {}
        os.makedirs(self.directory, exist_ok=True)
        self.dimensions = meta.get('dimensions') if isinstance(meta.get('dimensions'), dict) else {}
        self.validator = meta.get('validator') or version
        self._touch()

    def _touch(self):
        webdav._atomic_json(self.meta_path, {'identity': self.identity, 'validator': self.validator,
                                           'last_used': time.time_ns(), 'dimensions': self.dimensions})

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=os.SEEK_SET):
        position = offset if whence == os.SEEK_SET else (self.position if whence == os.SEEK_CUR else self.size) + offset
        if whence not in (os.SEEK_SET, os.SEEK_CUR, os.SEEK_END) or position < 0:
            raise ValueError('invalid seek')
        self.position = position
        return position

    def read(self, size=-1):
        self._checkClosed()
        length = min(self.size - self.position, size if size >= 0 else self.size)
        if length <= 0:
            return b''
        if length > MAX_READ_BYTES:
            raise webdav.WebDavError('单页或压缩包目录超过 64MB')
        first = self.position // BLOCK_BYTES
        last = (self.position + length - 1) // BLOCK_BYTES
        chunks = []
        block = first
        while block <= last:
            filename = os.path.join(self.directory, str(block))
            expected = min(BLOCK_BYTES, self.size - block * BLOCK_BYTES)
            if not os.path.isfile(filename) or os.path.getsize(filename) != expected:
                end_block = block
                while end_block < last and not os.path.isfile(os.path.join(self.directory, str(end_block + 1))):
                    end_block += 1
                start = block * BLOCK_BYTES
                end = min(self.size, (end_block + 1) * BLOCK_BYTES) - 1
                data = self._fetch(start, end)
                for number in range(block, end_block + 1):
                    offset = (number - block) * BLOCK_BYTES
                    target = os.path.join(self.directory, str(number))
                    temporary = target + '.part'
                    with open(temporary, 'wb') as handle:
                        handle.write(data[offset:offset + BLOCK_BYTES])
                    os.replace(temporary, target)
            with open(filename, 'rb') as handle:
                chunks.append(handle.read())
            block += 1
        offset = self.position - first * BLOCK_BYTES
        self.position += length
        self._touch()
        return b''.join(chunks)[offset:offset + length]

    def _fetch(self, start, end):
        headers = {'Range': f'bytes={start}-{end}', 'Accept-Encoding': 'identity'}
        if self.validator and not self.validator.startswith('W/'):
            headers['If-Range'] = self.validator
        response = webdav._request(self.config, 'GET', webdav._url_for(self.config, self.chapter['path']),
                                   headers=headers, stream=True)
        try:
            if response.status_code == 200:
                raise RangeUnsupported('WebDAV 未返回分段数据，使用整章缓存')
            if response.status_code != 206:
                raise webdav.WebDavError(f'分段读取失败（{response.status_code}）')
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
            if not match or tuple(map(int, match.groups())) != (start, end, self.size):
                raise webdav.WebDavError('WebDAV 返回的分段位置或文件大小不一致，请重新同步书库')
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise webdav.WebDavError('WebDAV 分段数据被重新编码')
            validator = (response.headers.get('Last-Modified') if self.validator and not self.validator.startswith(('"', 'W/'))
                         else response.headers.get('ETag')) or ''
            if validator and self.validator and validator != self.validator:
                raise webdav.WebDavError('章节已改变，请重新同步书库')
            if validator:
                self.validator = validator
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > end - start + 1:
                    raise webdav.WebDavError('WebDAV 返回的分段数据过长')
            if len(content) != end - start + 1:
                raise webdav.WebDavError('WebDAV 分段下载不完整')
            return bytes(content)
        except requests.RequestException as exc:
            raise webdav.WebDavError('WebDAV 分段下载中断，请重试') from exc
        finally:
            response.close()


def read_remote_cbz(file_path, read_page):
    """Return None to use the full-file path; lock blocks against eviction and sync."""
    if not webdav.is_cache_path(file_path):
        return None
    with webdav._chapter_file_lock(file_path):
        payload = webdav._read_index_payload(os.path.dirname(file_path))
        chapter = next((item for item in payload['chapters']
                        if item.get('filename') == os.path.basename(file_path)), None)
        config = webdav._read_config()
        if not chapter or not config.get('url') or not config.get('password'):
            return None
        source = webdav._source_id(config)
        if payload.get('source') and payload['source'] != source:
            raise webdav.WebDavError('WebDAV 来源已更改，请重新同步书库')
        if webdav._cache_matches(file_path, chapter, source):
            return None
        try:
            with RemoteCbz(config, chapter, source, file_path) as remote:
                result = read_page(remote)
        except RangeUnsupported:
            # The existing downloader validates and caches a complete archive.
            return None
    webdav._maybe_evict_cached_chapters(config, file_path)
    return result
