import io
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from mangadock.services import webdav
from mangadock.services.webdav_cbz import RemoteCbz, RangeUnsupported, BLOCK_BYTES, read_remote_cbz


class Response:
    def __init__(self, data, start, end, status=206, etag='"v1"'):
        self.status_code = status
        self.headers = {'Content-Range': f'bytes {start}-{end}/{len(data)}', 'ETag': etag}
        self.data = data[start:end + 1]
        self.closed = False

    def iter_content(self, size):
        for offset in range(0, len(self.data), size):
            yield self.data[offset:offset + size]

    def close(self):
        self.closed = True


class RemoteCbzTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, '1.cbz')
        archive = io.BytesIO()
        self.first = os.urandom(400000)
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('1.jpg', self.first)
            z.writestr('2.jpg', os.urandom(8 * 1024 * 1024))
            z.writestr('__MACOSX/._1.jpg', b'ignored')
        self.data = archive.getvalue()
        self.chapter = {'filename': '1.cbz', 'path': '/dav/1.cbz', 'size': len(self.data), 'etag': '"v1"'}
        self.config = {'url': 'http://localhost/dav', 'username': 'user', 'password': 'pass'}
        self.requests = []
        self.responses = []

    def request(self, config, method, url, **kwargs):
        self.assertEqual(kwargs['headers']['Accept-Encoding'], 'identity')
        start, end = map(int, kwargs['headers']['Range'][6:].split('-'))
        self.requests.append((start, end))
        response = Response(self.data, start, end)
        self.responses.append(response)
        return response

    def reader(self):
        return RemoteCbz(self.config, self.chapter, 'source', self.path)

    def test_directory_and_first_page_do_not_download_whole_archive(self):
        with patch.object(webdav, '_request', side_effect=self.request):
            with self.reader() as remote, zipfile.ZipFile(remote) as z:
                self.assertIn('2.jpg', z.namelist())
                directory_bytes = sum(b - a + 1 for a, b in self.requests)
                self.assertLess(directory_bytes, BLOCK_BYTES * 2)
                self.assertEqual(z.read('1.jpg'), self.first)
            self.assertLess(sum(b - a + 1 for a, b in self.requests), len(self.data) // 4)
            count = len(self.requests)
            with self.reader() as remote, zipfile.ZipFile(remote) as z:
                self.assertEqual(z.read('1.jpg'), self.first)
            self.assertEqual(len(self.requests), count)
        self.assertTrue(all(r.closed for r in self.responses))
        self.assertFalse(os.path.exists(self.path))

    def test_unsupported_range_closes_without_reading_full_response(self):
        response = Response(self.data, 0, 0, status=200)
        with patch.object(webdav, '_request', return_value=response), self.reader() as remote:
            with self.assertRaises(RangeUnsupported):
                remote.read(1)
        self.assertTrue(response.closed)

    def test_wrong_content_range_is_rejected(self):
        response = Response(self.data, 1, 2)
        with patch.object(webdav, '_request', return_value=response), self.reader() as remote:
            with self.assertRaises(webdav.WebDavError):
                remote.read(1)
        self.assertTrue(response.closed)

    def test_changed_etag_is_rejected(self):
        response = Response(self.data, 0, BLOCK_BYTES - 1, etag='"v2"')
        with patch.object(webdav, '_request', return_value=response), self.reader() as remote:
            with self.assertRaises(webdav.WebDavError):
                remote.read(1)

    def test_truncated_response_is_not_cached(self):
        response = Response(self.data, 0, BLOCK_BYTES - 1)
        response.data = b'broken'
        with patch.object(webdav, '_request', return_value=response), self.reader() as remote:
            with self.assertRaises(webdav.WebDavError):
                remote.read(1)
        self.assertFalse(os.path.isfile(os.path.join(self.path + '.webdav-ranges', '0')))

    def test_real_http_range_requests_read_first_page_only(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        data, requests_seen = self.data, []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                start, end = map(int, self.headers['Range'][6:].split('-'))
                requests_seen.append((start, end))
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
                self.send_header('Content-Length', str(end - start + 1))
                self.send_header('ETag', '"v1"')
                self.end_headers()
                self.wfile.write(data[start:end + 1])
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.config['url'] = f'http://127.0.0.1:{server.server_port}/dav'
            with patch.dict(os.environ, {'NO_PROXY': '127.0.0.1', 'no_proxy': '127.0.0.1'}):
                with self.reader() as remote, zipfile.ZipFile(remote) as archive:
                    self.assertEqual(archive.read('1.jpg'), self.first)
            self.assertLess(sum(end - start + 1 for start, end in requests_seen), len(data) // 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_index_change_discards_old_blocks(self):
        with patch.object(webdav, '_request', side_effect=self.request):
            with self.reader() as remote:
                remote.read(1)
            self.chapter['modified'] = 'new date'
            with self.reader() as remote:
                remote.read(1)
        self.assertEqual(len(self.requests), 2)

    def test_last_modified_validator_works_when_response_also_has_etag(self):
        self.chapter['etag'] = ''
        self.chapter['modified'] = 'Mon, 28 Sep 2026 00:00:00 GMT'
        response = Response(self.data, 0, BLOCK_BYTES - 1)
        response.headers['Last-Modified'] = self.chapter['modified']
        with patch.object(webdav, '_request', return_value=response), self.reader() as remote:
            self.assertEqual(remote.read(1), self.data[:1])

    def test_reader_filters_hidden_entries_and_rejects_bad_page(self):
        from mangadock.core import app
        from mangadock.blueprints.web.reader import _read_cbz_page
        with patch.object(webdav, '_request', side_effect=self.request):
            with app.test_request_context('/'), self.reader() as remote:
                images, dimensions, _, data = _read_cbz_page(remote)
                self.assertEqual(images, ['1.jpg', '2.jpg'])
                self.assertEqual(dimensions, [(0, 0), (0, 0)])
                self.assertIsNone(data)
            with app.test_request_context('/?page=0'), self.reader() as remote:
                self.assertEqual(_read_cbz_page(remote)[3], self.first)
            with app.test_request_context('/?page=2'), self.reader() as remote:
                with self.assertRaises(LookupError):
                    _read_cbz_page(remote)

    def test_loaded_page_dimensions_are_cached_without_scanning_other_pages(self):
        from PIL import Image
        from mangadock.core import app
        from mangadock.blueprints.web.reader import _read_cbz_page
        image = io.BytesIO()
        Image.new('RGB', (320, 4800)).save(image, format='PNG')
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('1.png', image.getvalue())
            z.writestr('2.jpg', os.urandom(8 * 1024 * 1024))
        self.data = archive.getvalue()
        self.chapter['size'] = len(self.data)
        with patch.object(webdav, '_request', side_effect=self.request):
            with app.test_request_context('/?page=0'), self.reader() as remote:
                self.assertEqual(_read_cbz_page(remote)[3], image.getvalue())
            with app.test_request_context('/'), self.reader() as remote:
                result = _read_cbz_page(remote)
                self.assertEqual(result[1], [[320, 4800], (0, 0)])
        self.assertLess(sum(b - a + 1 for a, b in self.requests), len(self.data) // 4)

    def test_eviction_removes_range_blocks(self):
        from mangadock.core import app
        with patch.object(app, 'instance_path', self.temp.name):
            directory = os.path.join(webdav.cache_root(), 'Book')
            os.makedirs(directory)
            self.path = os.path.join(directory, '1.cbz')
            webdav._write_index(directory, [self.chapter], 'source')
            with patch.object(webdav, '_request', side_effect=self.request), self.reader() as remote:
                remote.read(1)
            self.assertEqual(webdav.evict_cached_chapters(webdav.cache_root(), 1, None), [self.path])
            self.assertFalse(os.path.exists(self.path + '.webdav-ranges'))

    def test_wrong_source_never_requests_remote_file(self):
        with patch.object(webdav, 'is_cache_path', return_value=True), \
             patch.object(webdav, '_read_config', return_value=self.config), \
             patch.object(webdav, '_read_index_payload', return_value={'chapters': [self.chapter], 'source': 'other'}), \
             patch.object(webdav, '_request', side_effect=AssertionError('downloaded')):
            with self.assertRaises(webdav.WebDavError):
                read_remote_cbz(self.path, lambda remote: remote.read(1))


if __name__ == '__main__':
    unittest.main()
