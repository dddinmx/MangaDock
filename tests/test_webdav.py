# -*- coding: utf-8 -*-
"""WebDAV 索引、目录解析和缓存清理。不访问真实服务器。"""
import json
import hashlib
import os
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch

from mangadock.services.webdav import (
    WebDavError,
    _cache_matches,
    _can_prune_missing_comics,
    _direct_children,
    _url_for,
    _valid_chapter_file,
    _validate_base_url,
    evict_cached_chapters,
    indexed_chapter_filenames,
    parse_listing,
    parse_propfind,
)


LISTING = """<?xml version="1.0" encoding="UTF-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/dav/nas/</D:href>
    <D:propstat>
      <D:status>HTTP/1.1 200 OK</D:status>
      <D:prop>
        <D:displayname>nas</D:displayname>
        <D:resourcetype><D:collection/></D:resourcetype>
      </D:prop>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/dav/nas/%E5%AD%A3%E8%8A%82%E4%B9%8B%E4%BA%A4/</D:href>
    <D:propstat>
      <D:status>HTTP/1.1 200 OK</D:status>
      <D:prop>
        <D:displayname>季节之交</D:displayname>
        <D:resourcetype><D:collection/></D:resourcetype>
      </D:prop>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/dav/nas/%E5%AD%A3%E8%8A%82%E4%B9%8B%E4%BA%A4/0068_%E9%80%9A%E7%9F%A5.cbz</D:href>
    <D:propstat>
      <D:prop>
        <D:displayname>0068_通知.cbz</D:displayname>
        <D:getcontentlength>107560</D:getcontentlength>
      </D:prop>
    </D:propstat>
  </D:response>
</D:multistatus>
""".encode()


class WebDavListingTests(unittest.TestCase):
    def test_propfind_reads_directory_and_chapter(self):
        items = parse_propfind(LISTING)
        children = _direct_children(items, '/dav/nas/')
        self.assertEqual([item['name'] for item in children], ['季节之交'])
        chapters = _direct_children(items, '/dav/nas/季节之交/')
        self.assertEqual(chapters[0]['name'], '0068_通知.cbz')
        self.assertEqual(chapters[0]['size'], 107560)
        self.assertFalse(chapters[0]['is_dir'])

    def test_private_alist_address_is_allowed_and_metadata_address_is_not(self):
        self.assertTrue(_validate_base_url('http://127.0.0.1:5244/dav').endswith('/dav'))
        self.assertTrue(_validate_base_url('http://192.168.1.8:5244/dav').startswith('http://'))
        with self.assertRaises(WebDavError):
            _validate_base_url('http://169.254.169.254/latest')
        with self.assertRaises(WebDavError):
            _validate_base_url('http://user:pass@127.0.0.1:5244/dav')

    def test_index_lists_chapters_before_files_exist(self):
        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, '季节之交')
            os.makedirs(comic)
            with open(os.path.join(comic, '.webdav-index.json'), 'w', encoding='utf-8') as handle:
                handle.write('{"chapters":[{"filename":"0002.cbz","path":"/dav/a.cbz"},'
                             '{"filename":"0001.pdf","path":"/dav/b.pdf"},'
                             '{"filename":"note.txt","path":"/dav/note.txt"}]}')
            with patch('mangadock.services.webdav._read_config', return_value={'url': 'http://example/dav'}):
                self.assertEqual(
                    indexed_chapter_filenames(comic),
                    ['0002.cbz', '0001.pdf', 'note.txt'],
                )
            self.assertIsNone(indexed_chapter_filenames(root))

    def test_disconnected_index_does_not_list_chapters(self):
        from mangadock.services import webdav
        from mangadock.services import library
        from mangadock.core import app
        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, 'webdav_comics', 'book')
            os.makedirs(comic)
            webdav._write_index(comic, [{'filename': '1.cbz', 'path': '/dav/1.cbz'}], 'source')
            with app.app_context(), patch.object(app, 'instance_path', root), \
                    patch.object(webdav, '_read_config', return_value={}):
                self.assertEqual(indexed_chapter_filenames(comic), [])
                self.assertNotIn(webdav.cache_root(), library.get_comic_scan_roots(existing_only=True))

    def test_disconnect_removes_webdav_books_and_progress(self):
        from mangadock.core import app
        from mangadock.extensions import db
        from mangadock.models import ComicIdentity, ReadingProgress, ReadingSessionState
        from mangadock.services import home_banner, library, webdav
        with app.app_context(), tempfile.TemporaryDirectory() as root, patch.object(app, 'instance_path', root):
            comic = os.path.join(webdav.cache_root(), 'cloud-book')
            os.makedirs(comic)
            webdav._write_index(comic, [{'filename': '1.cbz', 'path': '/dav/1.cbz'}], 'source')
            covers = os.path.join(root, 'covers')
            os.makedirs(covers)
            cover = os.path.join(covers, 'cloud-book.jpg')
            with open(cover, 'wb') as handle:
                handle.write(b'cover')
            hero = os.path.join(covers, 'hero', 'cloud-book.jpg')
            os.makedirs(os.path.dirname(hero))
            with open(hero, 'wb') as handle:
                handle.write(b'hero')
            banners = os.path.join(root, 'banners')
            os.makedirs(banners)
            banner = os.path.join(banners, hashlib.sha256(b'cloud-book').hexdigest() + '.jpg')
            with open(banner, 'wb') as handle:
                handle.write(b'banner')
            page_cache = os.path.join(root, 'api_page_cache', 'c_cloudbook')
            os.makedirs(page_cache)
            with open(os.path.join(root, 'webdav.json'), 'w') as handle:
                handle.write('{}')
            progress_query, session_query, identity_query = MagicMock(), MagicMock(), MagicMock()
            identity_query.filter.return_value.all.return_value = [MagicMock(comic_id='c_cloudbook')]
            with patch.object(library, 'get_comic_scan_roots', return_value=[webdav.cache_root()]), \
                    patch.object(webdav, 'COVER_ROOT', covers), \
                    patch.object(home_banner, 'HOME_BANNER_DIR', banners), \
                    patch.object(ReadingProgress, 'query', progress_query), \
                    patch.object(ReadingSessionState, 'query', session_query), \
                    patch.object(ComicIdentity, 'query', identity_query), \
                    patch.object(db.session, 'commit'):
                webdav.disconnect()
            self.assertFalse(os.path.exists(comic))
            self.assertFalse(os.path.exists(cover))
            self.assertFalse(os.path.exists(hero))
            self.assertFalse(os.path.exists(banner))
            self.assertFalse(os.path.exists(page_cache))
            self.assertFalse(os.path.exists(os.path.join(root, 'webdav.json')))
            progress_query.filter.return_value.delete.assert_called_once()
            session_query.filter.return_value.delete.assert_called_once()

    def test_disconnect_failure_keeps_connection_for_retry(self):
        from mangadock.core import app
        from mangadock.extensions import db
        from mangadock.models import ComicIdentity, ReadingProgress, ReadingSessionState
        from mangadock.services import library, webdav
        with app.app_context(), tempfile.TemporaryDirectory() as root, patch.object(app, 'instance_path', root):
            comic = os.path.join(webdav.cache_root(), 'cloud-book')
            os.makedirs(comic)
            config = {'url': 'http://example.test/dav', 'root': '', 'username': ''}
            webdav._write_index(comic, [{'filename': '1.cbz', 'path': '/dav/1.cbz'}], webdav._source_id(config))
            config_path = os.path.join(root, 'webdav.json')
            with open(config_path, 'w') as handle:
                handle.write('{}')
            with patch.object(library, 'get_comic_scan_roots', return_value=[webdav.cache_root()]), \
                    patch.object(webdav, 'COVER_ROOT', root), \
                    patch.object(ReadingProgress, 'query', MagicMock()), \
                    patch.object(ReadingSessionState, 'query', MagicMock()), \
                    patch.object(ComicIdentity, 'query', MagicMock()), \
                    patch.object(db.session, 'commit'), \
                    patch.object(webdav, '_prune_comic_cache', side_effect=OSError('delete failed')):
                with self.assertRaises(OSError):
                    webdav.disconnect()
            self.assertTrue(os.path.isfile(config_path))
            self.assertTrue(webdav.settings_view()['cleanup_pending'])
            with patch.object(library, 'get_comic_scan_roots', return_value=[webdav.cache_root()]), \
                    patch.object(webdav, 'COVER_ROOT', root), \
                    patch.object(ReadingProgress, 'query', MagicMock()), \
                    patch.object(ReadingSessionState, 'query', MagicMock()), \
                    patch.object(ComicIdentity, 'query', MagicMock()), \
                    patch.object(db.session, 'commit'):
                webdav.disconnect()
            self.assertFalse(os.path.exists(comic))
            self.assertFalse(os.path.exists(config_path))

    def test_disconnect_preserves_progress_when_local_scan_root_is_offline(self):
        from mangadock.core import app
        from mangadock.models import ComicIdentity, ReadingProgress
        from mangadock.services import library, webdav
        with app.app_context(), tempfile.TemporaryDirectory() as root, patch.object(app, 'instance_path', root):
            comic = os.path.join(webdav.cache_root(), 'shared-name')
            os.makedirs(comic)
            webdav._write_index(comic, [{'filename': '1.cbz', 'path': '/dav/1.cbz'}], 'source')
            with open(os.path.join(root, 'webdav.json'), 'w') as handle:
                handle.write('{}')
            progress_query = MagicMock()
            with patch.object(library, 'get_comic_scan_roots', return_value=[webdav.cache_root(), os.path.join(root, 'offline-local')]), \
                    patch.object(ReadingProgress, 'query', progress_query), \
                    patch.object(ComicIdentity, 'query', MagicMock()):
                webdav.disconnect()
            progress_query.filter.assert_not_called()
            self.assertFalse(os.path.exists(comic))

    def test_eviction_removes_oldest_chapter_and_keeps_index(self):
        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, '书')
            os.makedirs(comic)
            index = os.path.join(comic, '.webdav-index.json')
            old = os.path.join(comic, '0001.cbz')
            new = os.path.join(comic, '0002.cbz')
            for path, stamp in ((old, 10), (new, 20), (index, 30)):
                with open(path, 'wb') as handle:
                    handle.write(b'x' * 100)
                os.utime(path, (stamp, stamp))
            removed = evict_cached_chapters(root, 100, new)
            self.assertEqual(removed, [old])
            self.assertTrue(os.path.isfile(new))
            self.assertTrue(os.path.isfile(index))

    def test_href_is_decoded_once_and_query_is_removed_first(self):
        payload = """<?xml version="1.0" encoding="UTF-8"?>
        <D:multistatus xmlns:D="DAV:">
          <D:response><D:href>/dav/</D:href><D:propstat><D:prop>
            <D:displayname>dav</D:displayname><D:resourcetype><D:collection/></D:resourcetype>
          </D:prop></D:propstat></D:response>
          <D:response><D:href>/dav/ch%3F1.cbz?token=1</D:href><D:propstat><D:prop>
            <D:getcontentlength>4</D:getcontentlength>
          </D:prop></D:propstat></D:response>
          <D:response><D:href>/dav/ch%2520name.cbz</D:href><D:propstat><D:prop>
            <D:getcontentlength>4</D:getcontentlength>
          </D:prop></D:propstat></D:response>
        </D:multistatus>""".encode()
        names = [item['name'] for item in _direct_children(parse_propfind(payload), '/dav/')]
        self.assertEqual(names, ['ch?1.cbz', 'ch%20name.cbz'])
        config = {'url': 'http://127.0.0.1/dav'}
        self.assertIn('/ch%3F1.cbz', _url_for(config, '/dav/ch?1.cbz'))
        self.assertIn('/ch%2520name.cbz', _url_for(config, '/dav/ch%20name.cbz'))

    def test_incomplete_or_empty_listing_does_not_prune(self):
        parent = """<?xml version="1.0"?>
        <D:multistatus xmlns:D="DAV:">
          <D:response><D:href>/dav/nas/</D:href><D:propstat><D:prop>
            <D:displayname>nas</D:displayname><D:resourcetype><D:collection/></D:resourcetype>
          </D:prop></D:propstat></D:response>
        </D:multistatus>""".encode()
        self.assertFalse(_can_prune_missing_comics(parse_listing(parent), '/dav/nas/'))
        broken = """<?xml version="1.0"?>
        <D:multistatus xmlns:D="DAV:">
          <D:response><D:href>/dav/nas/</D:href><D:propstat><D:status>HTTP/1.1 404 Not Found</D:status></D:propstat></D:response>
          <D:response><D:href>/dav/nas/book/</D:href><D:propstat><D:prop>
            <D:displayname>book</D:displayname><D:resourcetype><D:collection/></D:resourcetype>
          </D:prop></D:propstat></D:response>
        </D:multistatus>""".encode()
        self.assertFalse(parse_listing(broken)['complete'])
        self.assertFalse(_can_prune_missing_comics(parse_listing(broken), '/dav/nas/'))

    def test_changed_remote_identity_does_not_match_old_cache(self):
        with tempfile.TemporaryDirectory() as root:
            chapter = os.path.join(root, '0001.cbz')
            with zipfile.ZipFile(chapter, 'w') as archive:
                archive.writestr('1.jpg', b'old')
            stat = os.stat(chapter)
            meta = {
                'source': 'source-a',
                'path': '/dav/nas/book/0001.cbz',
                'size': 1,
                'etag': '"old"',
                'modified': 'Mon, 01 Jan 2024 00:00:00 GMT',
                'local_size': stat.st_size,
                'local_mtime_ns': stat.st_mtime_ns,
            }
            with open(chapter + '.webdav-meta', 'w', encoding='utf-8') as handle:
                json.dump(meta, handle)
            self.assertTrue(_cache_matches(chapter, {
                'path': meta['path'], 'size': 1, 'etag': '"old"', 'modified': meta['modified'],
            }, 'source-a'))
            self.assertFalse(_cache_matches(chapter, {
                'path': '/dav/other/0001.cbz', 'size': 2, 'etag': '"new"', 'modified': meta['modified'],
            }, 'source-a'))
            self.assertFalse(_cache_matches(chapter, {
                'path': meta['path'], 'size': 1, 'etag': '"old"', 'modified': meta['modified'],
            }, 'source-b'))

    def test_access_time_does_not_change_chapter_version(self):
        from mangadock.services import webdav
        from mangadock.services.library import api_page_source_version

        with tempfile.TemporaryDirectory() as root:
            chapter = os.path.join(root, '0001.cbz')
            with zipfile.ZipFile(chapter, 'w') as archive:
                archive.writestr('1.jpg', b'page')
            webdav._write_cache_meta(chapter, {
                'path': '/dav/book/0001.cbz', 'size': 1, 'etag': '"a"', 'modified': '',
            }, 'source-a')
            before_version = api_page_source_version(chapter)
            before_mtime = os.stat(chapter).st_mtime_ns
            with patch.object(webdav, 'is_cache_path', return_value=True):
                webdav.note_chapter_used(chapter)
            self.assertEqual(os.stat(chapter).st_mtime_ns, before_mtime)
            self.assertEqual(api_page_source_version(chapter), before_version)
            with open(chapter + '.webdav-meta', encoding='utf-8') as handle:
                self.assertGreater(json.load(handle)['last_used'], 0)

    def test_eviction_skips_chapter_locked_by_another_thread(self):
        import threading
        from mangadock.services import webdav

        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, '书')
            os.makedirs(comic)
            old = os.path.join(comic, '0001.cbz')
            new = os.path.join(comic, '0002.cbz')
            for path, stamp in ((old, 10), (new, 50)):
                with open(path, 'wb') as handle:
                    handle.write(b'x' * 100)
                os.utime(path, (stamp, stamp))
            started = threading.Event()
            release = threading.Event()

            def hold_lock():
                with webdav._chapter_file_lock(old):
                    started.set()
                    release.wait(2)

            worker = threading.Thread(target=hold_lock)
            worker.start()
            self.assertTrue(started.wait(2))
            try:
                removed = webdav.evict_cached_chapters(root, 100, new)
            finally:
                release.set()
                worker.join(2)
            self.assertNotIn(old, removed)
            self.assertTrue(os.path.isfile(old))

    def test_source_id_includes_username_and_hides_other_account_index(self):
        from mangadock.services import webdav

        base = {'url': 'http://127.0.0.1:5244/dav', 'root': '/nas'}
        alice = webdav._source_id({**base, 'username': 'alice'})
        bob = webdav._source_id({**base, 'username': 'bob'})
        self.assertNotEqual(alice, bob)
        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, '书')
            os.makedirs(comic)
            webdav._write_index(comic, [{
                'filename': '0001.cbz', 'path': '/dav/nas/书/0001.cbz', 'size': 1,
            }], alice)
            with patch.object(webdav, '_read_config', return_value={**base, 'username': 'bob', 'password': 'x'}):
                self.assertEqual(webdav.indexed_chapter_filenames(comic), [])
            with patch.object(webdav, '_read_config', return_value={**base, 'username': 'alice', 'password': 'x'}):
                self.assertEqual(webdav.indexed_chapter_filenames(comic), ['0001.cbz'])

    def test_successful_property_blocks_are_merged(self):
        listing = parse_listing(b'''<D:multistatus xmlns:D="DAV:">
          <D:response><D:href>/dav/book/</D:href>
            <D:propstat><D:status>HTTP/1.1 200 OK</D:status><D:prop>
              <D:displayname>book</D:displayname></D:prop></D:propstat>
            <D:propstat><D:status>HTTP/1.1 200 OK</D:status><D:prop>
              <D:resourcetype><D:collection/></D:resourcetype>
              <D:getetag>v2</D:getetag></D:prop></D:propstat>
          </D:response></D:multistatus>''')
        self.assertTrue(listing['complete'])
        self.assertTrue(listing['items'][0]['is_dir'])
        self.assertEqual(listing['items'][0]['etag'], 'v2')
        missing_type = parse_listing(b'''<D:multistatus xmlns:D="DAV:">
          <D:response><D:href>/dav/book/</D:href><D:propstat><D:prop>
          <D:displayname>book</D:displayname></D:prop></D:propstat>
          </D:response></D:multistatus>''')
        self.assertFalse(missing_type['complete'])

    def test_open_file_response_supports_range_and_etag(self):
        from flask import Flask
        from mangadock.services import webdav
        application = Flask('webdav-range-test')
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'chapter.pdf')
            with open(path, 'wb') as handle:
                handle.write(b'%PDF-1.4\n' + b'x' * 100)

            @application.route('/file')
            def serve():
                return webdav.chapter_file_response(open(path, 'rb'), 'chapter.pdf', 'application/pdf')

            with application.test_client() as client:
                response = client.get('/file', headers={'Range': 'bytes=0-9'})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(len(response.data), 10)
                self.assertEqual(response.headers['Content-Range'], 'bytes 0-9/109')
                etag = response.headers['ETag']
                response.close()
                response = client.get('/file', headers={'If-None-Match': etag})
                self.assertEqual(response.status_code, 304)
                response.close()
                response = client.get('/file', headers={'Range': 'bytes=200-300'})
                self.assertEqual(response.status_code, 416)
                response.close()

    def test_sync_prune_defers_busy_directory_and_preserves_lock_files(self):
        import threading
        from mangadock.services import webdav
        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, 'comics', 'book')
            os.makedirs(comic)
            chapter = os.path.join(comic, '1.cbz')
            with open(chapter, 'wb') as handle:
                handle.write(b'page')
            entered = threading.Event()
            release = threading.Event()

            def hold():
                with webdav._chapter_file_lock(chapter):
                    entered.set()
                    release.wait(5)

            with patch.object(webdav.app, 'instance_path', root), \
                    patch.object(webdav, 'is_cache_path', return_value=True):
                worker = threading.Thread(target=hold)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(webdav._prune_comic_cache(comic))
                    self.assertTrue(os.path.isfile(chapter))
                finally:
                    release.set()
                    worker.join(5)
                locks = set(os.listdir(os.path.join(root, 'webdav_locks')))
                self.assertTrue(webdav._prune_comic_cache(comic))
                self.assertFalse(os.path.exists(comic))
                self.assertEqual(set(os.listdir(os.path.join(root, 'webdav_locks'))), locks)

    def test_sync_rejects_changed_or_disconnected_connection(self):
        from mangadock.services import webdav
        snapshot = {'url': 'http://a/dav', 'username': 'alice', 'password': 'old', 'revision': 1}
        for current in ({}, {**snapshot, 'password': 'new'}, {**snapshot, 'revision': 2}):
            with patch.object(webdav, '_read_config', return_value=current):
                with self.assertRaises(WebDavError):
                    webdav._sync_config(snapshot)

    def test_read_access_protects_download_until_source_is_read(self):
        import threading
        from mangadock.services import webdav
        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, 'book')
            os.makedirs(comic)
            path = os.path.join(comic, '1.cbz')
            config = {'url': 'http://a/dav', 'password': 'dummy'}
            webdav._write_index(comic, [{'filename': '1.cbz', 'path': '/dav/book/1.cbz'}], webdav._source_id(config))

            def download(_config, _chapter, destination):
                with zipfile.ZipFile(destination, 'w') as archive:
                    archive.writestr('1.jpg', b'page')

            with patch.object(webdav, 'cache_root', return_value=root), \
                    patch.object(webdav, '_read_config', return_value=config), \
                    patch.object(webdav, '_download', side_effect=download):
                with webdav.chapter_access(path) as cached:
                    self.assertEqual(cached, path)
                    removed = []
                    worker = threading.Thread(target=lambda: removed.extend(webdav.evict_cached_chapters(root, 1, None)))
                    worker.start()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(removed, [])
                    with zipfile.ZipFile(cached) as archive:
                        self.assertEqual(archive.read('1.jpg'), b'page')
                self.assertEqual(webdav.evict_cached_chapters(root, 1, None), [path])

    def test_overlapping_sync_is_rejected_and_lock_is_released(self):
        import threading
        from mangadock.services import webdav
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def slow_sync():
            entered.set()
            release.wait(5)
            return 'done'

        def run():
            try:
                webdav.sync_library()
            except Exception as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as root, \
                patch.object(webdav.app, 'instance_path', root), \
                patch.object(webdav, '_sync_library_locked', side_effect=slow_sync):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(WebDavError, '正在同步'):
                    webdav.sync_library()
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            with patch.object(webdav, '_sync_library_locked', side_effect=WebDavError('failed')):
                with self.assertRaises(WebDavError):
                    webdav.sync_library()
            self.assertEqual(webdav.sync_library(), 'done')

    def test_complete_empty_directory_clears_index_but_incomplete_preserves_it(self):
        from mangadock.services import webdav, library
        config = {'url': 'http://a/dav', 'username': 'alice', 'password': 'dummy'}
        root_listing = {'complete': True, 'items': [
            {'href': '/dav/', 'is_dir': True, 'name': 'dav'},
            {'href': '/dav/book/', 'is_dir': True, 'name': 'book'},
        ]}
        with tempfile.TemporaryDirectory() as root, \
                patch.object(webdav.app, 'instance_path', root), \
                patch.object(webdav, 'cache_root', return_value=os.path.join(root, 'comics')), \
                patch.object(webdav, '_read_config', return_value=config), \
                patch.object(webdav, '_write_config'), \
                patch.object(webdav, '_save_cover'), \
                patch.object(library, 'get_comic_scan_roots', return_value=[]), \
                patch.object(library, 'invalidate_comics_cache'), \
                patch.object(library, 'refresh_comics_cache'):
            comic = os.path.join(root, 'comics', 'book')
            os.makedirs(comic)
            chapters = [{'filename': '1.cbz', 'path': '/dav/book/1.cbz'}]
            for complete, expected in ((False, ['1.cbz']), (True, [])):
                webdav._write_index(comic, chapters, webdav._source_id(config))
                child_listing = {'complete': complete, 'items': [
                    {'href': '/dav/book/', 'is_dir': True, 'name': 'book'},
                ]}
                with patch.object(webdav, '_propfind', side_effect=[root_listing, child_listing]):
                    webdav.sync_library()
                self.assertEqual(webdav.indexed_chapter_filenames(comic), expected)

    def test_stream_disconnect_is_handled_and_partial_file_is_removed(self):
        import requests
        from mangadock.services import webdav
        class BrokenResponse:
            status_code = 200
            headers = {}
            closed = False
            def iter_content(self, _size):
                yield b'partial'
                raise requests.ConnectionError('connection lost')
            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, 'book')
            os.makedirs(comic)
            config = {'url': 'http://a/dav', 'password': 'dummy'}
            webdav._write_index(comic, [{'filename': '1.cbz', 'path': '/dav/book/1.cbz'}], webdav._source_id(config))
            response = BrokenResponse()
            with patch.object(webdav, 'cache_root', return_value=root), \
                    patch.object(webdav, '_read_config', return_value=config), \
                    patch.object(webdav, '_request', return_value=response):
                self.assertIsNone(webdav.ensure_chapter_cached(comic, '1.cbz'))
            self.assertTrue(response.closed)
            self.assertFalse(any(name.endswith('.part') for name in os.listdir(comic)))
            response = BrokenResponse()
            with patch.object(webdav, 'COVER_ROOT', root), \
                    patch.object(webdav, '_request', return_value=response):
                with self.assertRaisesRegex(WebDavError, '封面下载中断'):
                    webdav._save_cover(config, 'book', {'path': '/dav/book/cover.jpg', 'size': 0})
            self.assertTrue(response.closed)
            self.assertFalse(os.path.exists(os.path.join(root, 'book.jpg')))

    def test_pdf_repair_is_versioned_and_eviction_removes_derivatives(self):
        from mangadock.utils import media
        from mangadock.services import webdav
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, '1.pdf')
            with open(source, 'wb') as handle:
                handle.write(b'%PDF-old')

            def extract(command, **_kwargs):
                with open(command[-1] + '-1.png', 'wb') as handle:
                    handle.write(b'image')

            def build(_images, destination):
                with open(source, 'rb') as handle:
                    content = handle.read()
                with open(destination, 'wb') as handle:
                    handle.write(content)
                return True, ''

            with patch.object(media, 'pdf_needs_repair', side_effect=lambda path: path == source), \
                    patch.object(media.subprocess, 'run', side_effect=extract), \
                    patch.object(media, 'build_pdf_from_image_list', side_effect=build) as builder:
                first = media.repair_pdf_for_reading(source)
                self.assertEqual(media.repair_pdf_for_reading(source), first)
                self.assertEqual(builder.call_count, 1)
                with open(source, 'wb') as handle:
                    handle.write(b'%PDF-new-longer')
                second = media.repair_pdf_for_reading(source)
                self.assertNotEqual(first, second)
                with open(second, 'rb') as handle:
                    self.assertEqual(handle.read(), b'%PDF-new-longer')
            webdav._remove_cache_file(source)
            self.assertFalse(os.path.exists(first))
            self.assertFalse(os.path.exists(second))
            self.assertFalse(os.path.exists(source))

    def test_cache_limit_counts_repairs_and_existing_api_images(self):
        from types import SimpleNamespace
        from mangadock.services import webdav, library
        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, 'webdav_comics', 'book')
            os.makedirs(comic)
            source = os.path.join(comic, '1.pdf')
            repair = os.path.join(comic, '.repaired', 'version', '1.pdf')
            api_dir = os.path.join(root, 'api_page_cache', 'comic-id', library.build_chapter_id('1.pdf'), 'version')
            os.makedirs(os.path.dirname(repair))
            os.makedirs(api_dir)
            for path, size in ((source, 100), (repair, 1000), (os.path.join(api_dir, '0.png'), 1000)):
                with open(path, 'wb') as handle:
                    handle.write(b'x' * size)
            with patch.object(webdav.app, 'instance_path', root), \
                    patch.object(library, 'get_comic_directory', return_value=comic), \
                    patch.object(library, 'get_comic_identity', return_value=SimpleNamespace(comic_id='comic-id')):
                self.assertEqual(webdav.evict_cached_chapters(webdav.cache_root(), 200, None), [source])
            self.assertFalse(os.path.exists(source))
            self.assertFalse(os.path.exists(repair))
            self.assertFalse(os.path.exists(api_dir))

    def test_orphan_images_are_evicted_and_local_comic_cache_is_preserved(self):
        from types import SimpleNamespace
        from mangadock.services import webdav, library
        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, 'webdav_comics', 'book')
            os.makedirs(comic)
            webdav._write_index(comic, [{'filename': '1.cbz', 'path': '/dav/book/1.cbz'}], '')
            api_dir = os.path.join(root, 'api_page_cache', 'comic-id', library.build_chapter_id('1.cbz'))
            os.makedirs(api_dir)
            with open(os.path.join(api_dir, '0.png'), 'wb') as handle:
                handle.write(b'x' * 1000)
            with patch.object(webdav.app, 'instance_path', root), \
                    patch.object(library, 'get_comic_identity', return_value=SimpleNamespace(comic_id='comic-id')):
                with patch.object(library, 'get_comic_directory', return_value=os.path.join(root, 'local', 'book')):
                    self.assertEqual(webdav.evict_cached_chapters(webdav.cache_root(), 1, None), [])
                    self.assertTrue(os.path.exists(api_dir))
                with patch.object(library, 'get_comic_directory', return_value=comic):
                    self.assertEqual(webdav.evict_cached_chapters(webdav.cache_root(), 1, None), [os.path.join(comic, '1.cbz')])
                    self.assertFalse(os.path.exists(api_dir))

    def test_saving_smaller_limit_triggers_cleanup(self):
        from mangadock.services import webdav
        current = {'url': 'http://a/dav', 'username': 'alice', 'password': 'dummy', 'root': '/book', 'cache_gb': 20}
        with tempfile.TemporaryDirectory() as root, \
                patch.object(webdav.app, 'instance_path', root), \
                patch.object(webdav, '_read_config', return_value=current), \
                patch.object(webdav, '_propfind'), \
                patch.object(webdav, '_write_config'), \
                patch.object(webdav, 'evict_cached_chapters') as evict:
            webdav.save_settings(current['url'], 'alice', '', '/book', 1, keep_password=True)
            evict.assert_called_once_with(webdav.cache_root(), 1024 ** 3, None)

    def test_read_cleanup_is_throttled_and_protects_current_chapter(self):
        from mangadock.services import webdav
        config = {'url': 'http://a/dav', 'cache_gb': 1}
        with tempfile.TemporaryDirectory() as root, \
                patch.object(webdav.app, 'instance_path', root), \
                patch.object(webdav, 'evict_cached_chapters') as evict, \
                patch.object(webdav.time, 'time', side_effect=[100, 101, 131]):
            for _ in range(3):
                webdav._maybe_evict_cached_chapters(config, '/current/1.cbz')
            self.assertEqual(evict.call_count, 2)
            evict.assert_called_with(webdav.cache_root(), 1024 ** 3, '/current/1.cbz')

    def test_opened_image_response_survives_cache_removal(self):
        from flask import Flask
        from mangadock.services import webdav
        application = Flask('webdav-image-cleanup-test')
        with tempfile.TemporaryDirectory() as root:
            image = os.path.join(root, '0.jpg')
            with open(image, 'wb') as handle:
                handle.write(b'image-bytes')
            with application.test_request_context('/image'):
                handle = open(image, 'rb')
                os.remove(image)
                response = webdav.chapter_file_response(handle, '0.jpg', 'image/jpeg')
                self.assertEqual(b''.join(response.response), b'image-bytes')
                self.assertEqual(response.status_code, 200)
                response.close()
                self.assertTrue(handle.closed)

    def test_remote_page_version_survives_recaching_and_tracks_content(self):
        from mangadock.services import webdav, library
        with tempfile.TemporaryDirectory() as root, \
                patch.object(webdav, 'cache_root', return_value=root):
            path = os.path.join(root, 'book', '1.cbz')
            os.makedirs(os.path.dirname(path))
            chapter = {'path': '/dav/book/1.cbz', 'size': 4, 'etag': '', 'modified': ''}
            with open(path, 'wb') as handle:
                handle.write(b'old!')
            webdav._write_cache_meta(path, chapter, 'source-a')
            first = library.api_page_source_version(path)
            os.remove(path)
            with open(path, 'wb') as handle:
                handle.write(b'old!')
            webdav._write_cache_meta(path, chapter, 'source-a')
            self.assertEqual(library.api_page_source_version(path), first)
            with open(path, 'wb') as handle:
                handle.write(b'new!')
            webdav._write_cache_meta(path, chapter, 'source-a')
            self.assertNotEqual(library.api_page_source_version(path), first)
            chapter['etag'] = '"v1"'
            webdav._write_cache_meta(path, chapter, 'source-a')
            etag_version = library.api_page_source_version(path)
            os.utime(path, (1, 1))
            webdav._write_cache_meta(path, chapter, 'source-a')
            self.assertEqual(library.api_page_source_version(path), etag_version)
            chapter['etag'] = '"v2"'
            webdav._write_cache_meta(path, chapter, 'source-a')
            self.assertNotEqual(library.api_page_source_version(path), etag_version)

    def test_html_error_page_is_not_a_valid_chapter(self):
        with tempfile.TemporaryDirectory() as root:
            html_path = os.path.join(root, '0001.cbz')
            with open(html_path, 'wb') as handle:
                handle.write(b'<!DOCTYPE html><p>error</p>')
            self.assertFalse(_valid_chapter_file(html_path, '0001.cbz'))
            valid_path = os.path.join(root, '0002.cbz')
            with zipfile.ZipFile(valid_path, 'w') as archive:
                archive.writestr('1.jpg', b'page')
            part_path = valid_path + '.part'
            os.replace(valid_path, part_path)
            self.assertTrue(_valid_chapter_file(part_path, '0002.cbz'))
            self.assertFalse(_valid_chapter_file(part_path, '0002.txt'))

    def test_download_publishes_part_file_and_hits_skip_full_validation(self):
        from mangadock.services import webdav

        with tempfile.TemporaryDirectory() as root:
            comic = os.path.join(root, '书')
            os.makedirs(comic)
            webdav._write_index(comic, [{
                'filename': '0001.cbz',
                'path': '/dav/book/0001.cbz',
                'size': 0,
                'etag': '"cbz"',
                'modified': '',
            }, {
                'filename': '0002.pdf',
                'path': '/dav/book/0002.pdf',
                'size': 0,
                'etag': '"pdf"',
                'modified': '',
            }], '')
            checks = {'count': 0}
            original = webdav._valid_chapter_file

            def counting_valid(path, filename):
                checks['count'] += 1
                return original(path, filename)

            def fake_download(_config, chapter, destination):
                if chapter['filename'].endswith('.pdf'):
                    with open(destination, 'wb') as handle:
                        handle.write(b'%PDF-1.4\n%fake')
                    return
                with zipfile.ZipFile(destination, 'w') as archive:
                    archive.writestr('1.jpg', b'page')

            config = {
                'url': 'http://127.0.0.1:5244/dav',
                'password': 'secret',
                'root': '/nas',
                'cache_gb': 20,
            }
            with patch.object(webdav, 'cache_root', return_value=root), \
                    patch.object(webdav, '_read_config', return_value=config), \
                    patch.object(webdav, '_download', side_effect=fake_download), \
                    patch.object(webdav, '_valid_chapter_file', side_effect=counting_valid):
                cbz_path = webdav.ensure_chapter_cached(comic, '0001.cbz')
                pdf_path = webdav.ensure_chapter_cached(comic, '0002.pdf')
                self.assertEqual(os.path.basename(cbz_path), '0001.cbz')
                self.assertEqual(os.path.basename(pdf_path), '0002.pdf')
                self.assertTrue(zipfile.is_zipfile(cbz_path))
                with open(pdf_path, 'rb') as handle:
                    self.assertTrue(handle.read().startswith(b'%PDF-'))
                self.assertEqual(checks['count'], 2)
                self.assertEqual(webdav.ensure_chapter_cached(comic, '0001.cbz'), cbz_path)
                self.assertEqual(webdav.ensure_chapter_cached(comic, '0001.cbz'), cbz_path)
                self.assertEqual(webdav.ensure_chapter_cached(comic, '0001.cbz'), cbz_path)
                self.assertEqual(checks['count'], 2)


if __name__ == '__main__':
    unittest.main()
