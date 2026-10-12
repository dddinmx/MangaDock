"""Verify enhanced selection, invalidation and HTTP caching without live data."""
import ast
from io import BytesIO
import os
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from flask import Flask, request, send_file
from PIL import Image
from werkzeug.datastructures import FileStorage

import test_novel_epub_layout as epub_layout


class NovelCoverSRTests(epub_layout.NovelEpubLayoutTests):
    def setUp(self):
        super().setUp()
        self.write_book()
        self.namespace = self.service.get_novel_cover_file.__globals__
        self.queue = Mock()
        self.queue_patch = patch.dict(self.namespace, {'_request_novel_cover_upscale': self.queue})
        self.queue_patch.start()
        self.addCleanup(self.queue_patch.stop)

    def enhanced_path(self):
        novel = self.service.get_novel('book')
        return Path(self.service._novel_upscaled_cover_path('book', novel['cover_source_version']))

    def make_enhanced(self):
        path = self.enhanced_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (1280, 1920), 'red').save(path, 'JPEG')
        return path

    def test_missing_cache_returns_source_and_queues_enhancement(self):
        path, _ = self.service.get_novel_cover_file('book')
        self.queue.assert_called_once_with(path, str(self.enhanced_path()))
        self.assertNotIn('upscaled', Path(path).parts)

    def test_completed_cache_is_served_and_changes_page_version(self):
        before = self.service.get_novel('book')['cover_version']
        target = self.make_enhanced()
        path, _ = self.service.get_novel_cover_file('book')
        self.assertEqual(path, str(target))
        self.queue.assert_not_called()
        self.assertNotEqual(before, self.service.get_novel('book')['cover_version'])

    def test_refreshing_enhanced_cache_changes_browser_version(self):
        target = self.make_enhanced()
        before = self.service.get_novel('book')['cover_version']
        stamp = target.stat().st_mtime_ns + 1_000_000_000
        os.utime(target, ns=(stamp, stamp))
        self.assertNotEqual(before, self.service.get_novel('book')['cover_version'])

    def test_replaced_epub_does_not_use_old_cache(self):
        old = self.make_enhanced()
        epub = self.root / 'book.epub'
        stamp = epub.stat().st_mtime_ns + 1_000_000_000
        os.utime(epub, ns=(stamp, stamp))
        path, _ = self.service.get_novel_cover_file('book')
        self.assertNotEqual(path, str(old))
        self.assertNotEqual(self.enhanced_path(), old)
        self.queue.assert_called_once()

    def test_upload_supersedes_epub_cache_and_queues_new_cover(self):
        old = self.make_enhanced()
        content = BytesIO()
        Image.new('RGB', (300, 450), 'blue').save(content, 'PNG')
        content.seek(0)
        self.service.save_novel_cover('book', FileStorage(content, filename='cover.png', content_type='image/png'))
        path, _ = self.service.get_novel_cover_file('book')
        self.assertEqual(path, str(self.root / 'covers' / 'book.jpg'))
        self.assertNotEqual(self.enhanced_path(), old)
        self.assertEqual(self.queue.call_args.args[0], path)

    def test_source_extraction_preserves_large_original(self):
        from zipfile import ZipFile
        content = BytesIO()
        Image.new('RGB', (1000, 1500), 'green').save(content, 'JPEG')
        # Replace the fixture image without duplicate ZIP members.
        epub_path = self.root / 'book.epub'
        with ZipFile(epub_path) as epub:
            entries = {name: epub.read(name) for name in epub.namelist()}
        entries['OPS/images/cover.jpg'] = content.getvalue()
        with ZipFile(epub_path, 'w') as epub:
            for name, data in entries.items():
                epub.writestr(name, data)
        path, _ = self.service.get_novel_cover_file('book', enhanced=False)
        with Image.open(path) as image:
            self.assertEqual(image.size, (1000, 1500))

    def test_request_queues_once_without_waiting_for_worker(self):
        pool = Mock()
        with patch.dict(self.namespace, {'_cover_pool': pool}):
            started = time.monotonic()
            for _ in range(3):
                self.service._request_novel_cover_upscale('source.jpg', 'target.jpg')
            self.assertLess(time.monotonic() - started, 1)
        pool.submit.assert_called_once()
        with patch.dict(self.namespace, {'_upscale_novel_cover': Mock(return_value=None)}):
            pool.submit.call_args.args[0]()
        self.assertNotIn('target.jpg', self.namespace['_cover_pending'])

    def test_large_jpeg_cover_is_safely_decoded_instead_of_rejected(self):
        from zipfile import ZipFile
        content = BytesIO()
        with Image.new('RGB', (4320, 5760), 'green') as image:
            image.save(content, 'JPEG')
        epub_path = self.root / 'book.epub'
        with ZipFile(epub_path) as epub:
            entries = {name: epub.read(name) for name in epub.namelist()}
        entries['OPS/images/cover.jpg'] = content.getvalue()
        with ZipFile(epub_path, 'w') as epub:
            for name, data in entries.items():
                epub.writestr(name, data)
        path, _ = self.service.get_novel_cover_file('book', enhanced=False)
        with Image.open(path) as image:
            self.assertLessEqual(image.width * image.height, 20_000_000)
            self.assertGreaterEqual(max(image.size), 1920)

    def test_route_revalidates_source_but_caches_enhanced_privately(self):
        source = Path(__file__).resolve().parents[1] / 'mangadock/blueprints/novel_routes.py'
        node = next(node for node in ast.parse(source.read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'novel_cover')
        node.decorator_list = []
        app = Flask('cover-test', static_folder=str(self.root))
        namespace = {'app': app, 'request': request, 'send_file': send_file,
                     'get_novel_cover_file': self.service.get_novel_cover_file, 'time': time}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
        app.add_url_rule('/cover/<novel_id>', view_func=namespace['novel_cover'])
        with app.test_client() as client:
            with client.get('/cover/book') as response:
                self.assertTrue(response.cache_control.no_cache)
                self.assertFalse(response.cache_control.immutable)
            self.make_enhanced()
            with client.get('/cover/book') as response:
                self.assertTrue(response.cache_control.private)
                self.assertFalse(response.cache_control.public)
                self.assertTrue(response.cache_control.immutable)
                with Image.open(BytesIO(response.data)) as image:
                    self.assertEqual(image.size, (1280, 1920))


if __name__ == '__main__':
    unittest.main()
