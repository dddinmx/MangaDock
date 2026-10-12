"""Regression checks for novel imports and concurrent reading-time reports."""
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import test_review_regressions as regression_tests


class FullStackFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = regression_tests.ReviewRegressions
        cls.sandbox.setUpClass()
        from mangadock.services import fanqie, novels, reading
        cls.fanqie, cls.novels, cls.reading = fanqie, novels, reading
        cls.app, cls.db, cls.models = cls.sandbox.app, cls.sandbox.db, cls.sandbox.models

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.tearDownClass()

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(self.db.session.remove)
        self.user_id = self.models.User.query.filter_by(username='admin').first().id

    def write_book(self, path, book_id, content='original'):
        with ZipFile(path, 'w') as epub:
            epub.writestr('META-INF/container.xml',
                          '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
            epub.writestr('OPS/book.opf', f'''<package><metadata><title>Same title</title>
                <creator>Same author</creator><identifier>fanqie-{book_id}</identifier></metadata>
                <manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
                </manifest><spine><itemref idref="chapter"/></spine></package>''')
            epub.writestr('OPS/chapter.xhtml', f'<html><body><p>{content}</p></body></html>')

    def download_books(self, book_ids, is_update=False):
        case = self
        class Client:
            def create_job(self, book_id, *args):
                return {'id':book_id, 'status':'completed',
                        'metadata':{'title':'Same title', 'author':'Same author'}}
            def download_artifact(self, job, path):
                case.write_book(path, job['id'], content='downloaded')
        task = SimpleNamespace(url='', status='pending', comic_name='Same title', is_update=is_update)
        results = []
        with patch.object(self.novels, 'NOVEL_ROOT', str(self.root)), \
                patch.object(self.fanqie, 'NOVEL_ROOT', str(self.root)), \
                patch.object(self.fanqie, 'get_task', return_value=task), \
                patch.object(self.fanqie, 'get_client', return_value=Client()), \
                patch.object(self.fanqie, 'update_task'), \
                patch.object(self.fanqie, 'is_task_cancel_requested', return_value=False), \
                patch.object(self.fanqie, 'finalize_task_status', return_value=True):
            for book_id in book_ids:
                task.url = f'fanqie://{book_id}'
                results.append(self.fanqie.execute_fanqie_task(f'task-{book_id}'))
            books = self.novels.get_novels()
        return results, books

    def test_same_title_books_preserve_each_other_and_legacy_files(self):
        legacy = self.root / 'Same title_Same author.epub'
        self.write_book(legacy, '11111111')
        original = legacy.read_bytes()
        results, books = self.download_books(['12345678', '87654321'])
        self.assertEqual(results, [True, True])
        self.assertEqual({book['fanqie_book_id'] for book in books}, {'11111111','12345678','87654321'})
        self.assertEqual(legacy.read_bytes(), original)

    def test_existing_novel_update_keeps_its_path(self):
        legacy = self.root / 'legacy-name.epub'
        self.write_book(legacy, '12345678')
        results, books = self.download_books(['12345678'], is_update=True)
        self.assertEqual(results, [True])
        self.assertEqual([book['file_path'] for book in books], [str(legacy)])
        with ZipFile(legacy) as epub:
            self.assertIn(b'downloaded', epub.read('OPS/chapter.xhtml'))

    def test_unrecognized_existing_destination_is_not_overwritten(self):
        destination = self.root / 'Same title_Same author_12345678.epub'
        destination.write_bytes(b'unrecognized existing book')
        results, _ = self.download_books(['12345678'])
        self.assertEqual(results, [False])
        self.assertEqual(destination.read_bytes(), b'unrecognized existing book')

    def test_book_id_suffix_does_not_replace_fallback_title_or_author(self):
        path = self.root / 'Same title_Same author_12345678.epub'
        self.write_book(path, '12345678')
        with ZipFile(path) as epub:
            entries = {name:epub.read(name) for name in epub.namelist()}
        entries['OPS/book.opf'] = entries['OPS/book.opf'].replace(
            b'<title>Same title</title>', b'<title/>').replace(
            b'<creator>Same author</creator>', b'<creator/>')
        with ZipFile(path, 'w') as epub:
            for name, content in entries.items():
                epub.writestr(name, content)
        with patch.object(self.novels, 'NOVEL_ROOT', str(self.root)):
            novel = self.novels.get_novels()[0]
        self.assertEqual(novel['title'], 'Same title')
        self.assertEqual(novel['author'], 'Same author')

    def check_concurrent_reports(self, initial, reports):
        from sqlalchemy.orm import Query
        name = self._testMethodName
        if initial:
            self.db.session.add(self.models.ReadingSessionState(
                user_id=self.user_id, comic_name=name, session_key='same-session', last_reported_seconds=initial,
            ))
            self.db.session.add(self.models.ReadingTime(
                user_id=self.user_id, comic_name=name, duration=1, duration_seconds=initial,
                read_at=self.reading.datetime.now(self.reading.china_tz),
            ))
            self.db.session.commit()
        self.db.session.remove()
        start = threading.Barrier(len(reports))
        read = threading.Barrier(len(reports))
        original_first = Query.first
        errors = []
        def synchronized_first(query):
            result = original_first(query)
            if (query.column_descriptions[0]['entity'] is self.models.ReadingSessionState
                    and threading.current_thread().name.startswith('reading-race')
                    and not query.session.connection().connection.in_transaction):
                # Reproduce competing reads only when no database write lock is held.
                read.wait(timeout=5)
            return result
        def report(seconds):
            try:
                start.wait(timeout=5)
                self.reading.record_reading_time(name, seconds, self.user_id, 'same-session')
            except Exception as exc:
                errors.append(exc)
        with patch.object(Query, 'first', synchronized_first):
            threads = [threading.Thread(target=report, args=(seconds,), name=f'reading-race-{index}')
                       for index, seconds in enumerate(reports)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        total = self.db.session.query(self.db.func.sum(self.models.ReadingTime.duration_seconds)).filter_by(
            user_id=self.user_id, comic_name=name).scalar()
        state = self.models.ReadingSessionState.query.filter_by(comic_name=name).first()
        self.assertEqual(total, max(reports))
        self.assertEqual(state.last_reported_seconds, max(reports))

    def test_existing_session_does_not_double_count_concurrent_reports(self):
        self.check_concurrent_reports(30, [60, 60])

    def test_out_of_order_cumulative_reports_do_not_lose_or_repeat_time(self):
        self.check_concurrent_reports(30, [60, 90])

    def test_first_concurrent_reports_are_deduplicated(self):
        self.check_concurrent_reports(0, [60, 60])

    def test_capped_report_can_drain_remaining_seconds(self):
        name = self._testMethodName
        for _ in range(3):
            self.reading.record_reading_time(name, 2000, self.user_id, 'same-session')
        total = self.db.session.query(self.db.func.sum(self.models.ReadingTime.duration_seconds)).filter_by(
            comic_name=name).scalar()
        self.assertEqual(total, 2000)


if __name__ == '__main__':
    unittest.main()
