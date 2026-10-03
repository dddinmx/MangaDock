"""Test EPUB layouts without importing the live app or database."""
import ast
from datetime import timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from zipfile import ZipFile

from PIL import Image


def load_novel_service(novel_root, cover_root):
    source = Path(__file__).resolve().parents[1] / 'mangadock/services/novels.py'
    tree = ast.parse(source.read_text())
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ImportFrom) and node.module == 'mangadock.settings')]
    namespace = {'__name__': 'isolated_novels', 'NOVEL_ROOT': str(novel_root),
                 'NOVEL_COVER_ROOT': str(cover_root), 'china_tz': timezone.utc}
    exec(compile(tree, str(source), 'exec'), namespace)
    return SimpleNamespace(**namespace)


class NovelEpubLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = load_novel_service(self.root, self.root / 'covers')

    def write_book(self, prefix='', extra_containers=()):
        container = '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>'
        package = '''<package><metadata><title>测试小说</title><creator>作者</creator></metadata>
        <manifest>
          <item id="chapter" href="text/chapter.xhtml" media-type="application/xhtml+xml"/>
          <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
          <item id="image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>
        </manifest><spine><itemref idref="chapter"/></spine></package>'''
        cover = BytesIO()
        Image.new('RGB', (20, 30), 'red').save(cover, 'JPEG')
        path = self.root / 'book.epub'
        with ZipFile(path, 'w') as epub:
            epub.writestr(prefix + 'META-INF/container.xml', container)
            epub.writestr(prefix + 'OPS/book.opf', package)
            epub.writestr(prefix + 'OPS/nav.xhtml', '<a href="text/chapter.xhtml">第一章</a>')
            epub.writestr(prefix + 'OPS/text/chapter.xhtml', '<html><body><p>章节正文</p></body></html>')
            epub.writestr(prefix + 'OPS/images/cover.jpg', cover.getvalue())
            for name in extra_containers:
                epub.writestr(name, container)
        return path

    def assert_book_readable(self):
        novels = self.service.get_novels()
        self.assertEqual(len(novels), 1)
        self.assertEqual(novels[0]['title'], '测试小说')
        self.assertEqual(novels[0]['chapter_count'], 1)
        self.assertEqual(self.service.get_novel_chapters('book')[0]['title'], '第一章')
        self.assertEqual(self.service.get_novel_chapter('book', 0)['paragraphs'], ['章节正文'])
        cover_path, mimetype = self.service.get_novel_cover_file('book')
        self.assertEqual(mimetype, 'image/jpeg')
        with Image.open(cover_path) as cover:
            self.assertEqual(cover.size, (20, 30))

    def test_standard_layout(self):
        self.write_book()
        self.assert_book_readable()

    def test_nested_unicode_layout(self):
        self.write_book('目录/盗墓笔记 1-8 全集).epub/')
        self.assert_book_readable()

    def test_standard_container_takes_priority(self):
        self.write_book(extra_containers=('backup/META-INF/container.xml',))
        self.assert_book_readable()

    def test_nested_layout_ignores_macos_sidecars(self):
        self.write_book('book/', ('__MACOSX/book/META-INF/container.xml',
                                  '._book/META-INF/container.xml'))
        self.assert_book_readable()

    def test_ambiguous_nested_books_are_not_selected_arbitrarily(self):
        self.write_book('one/', ('two/META-INF/container.xml',))
        self.assertEqual(self.service.get_novels(), [])


if __name__ == '__main__':
    unittest.main()
