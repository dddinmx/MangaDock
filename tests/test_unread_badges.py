"""Unread cover badges, with temporary source and database only."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup
import test_review_regressions as regressions


class UnreadBadgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = regressions.ReviewRegressions
        cls.sandbox.setUpClass()
        cls.app, cls.db, cls.models = cls.sandbox.app, cls.sandbox.db, cls.sandbox.models

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.tearDownClass()

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(self.db.session.remove)
        self.user_id = self.models.User.query.filter_by(username='admin').one().id
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.user_id

    def test_counts_new_chapters_and_incomplete_open_chapter(self):
        from mangadock.services.reading import get_unread_chapter_count
        progress = SimpleNamespace(last_chapter=125, last_page=0, total_pages=0, chapter_completed=True)
        self.assertEqual(get_unread_chapter_count(128, progress), 2)
        progress.last_chapter = 126
        progress.chapter_completed = False
        self.assertEqual(get_unread_chapter_count(128, progress), 2)
        progress.chapter_completed = True
        self.assertEqual(get_unread_chapter_count(128, progress), 1)
        progress.last_chapter = 127
        self.assertEqual(get_unread_chapter_count(128, progress), 0)
        self.assertEqual(get_unread_chapter_count(0, progress), 0)
        self.assertEqual(get_unread_chapter_count(128, None), 128)

    def test_legacy_opened_chapters_are_not_assumed_complete(self):
        from mangadock.services.reading import get_unread_chapter_count
        progress = SimpleNamespace(last_chapter=54, last_page=0, total_pages=0, chapter_completed=None)
        self.assertEqual(get_unread_chapter_count(55, progress), 1)
        self.assertEqual(get_unread_chapter_count(58, progress), 4)
        progress.total_pages, progress.last_page = 10, 9
        self.assertEqual(get_unread_chapter_count(58, progress), 3)
        progress.last_page = 4
        self.assertEqual(get_unread_chapter_count(58, progress), 4)
        progress.chapter_completed = True
        self.assertEqual(get_unread_chapter_count(58, progress), 3)

    def test_both_cover_surfaces_show_two_and_hide_zero(self):
        from mangadock.services import reading
        from mangadock.blueprints.web import home, library
        name = 'Unread badge comic'
        comic = {'comic_name': name, 'available_chapters': 128, 'total_chapters': 2,
                 'comic_format': 2, 'created_at': None, 'group': '默认分组'}
        for completed_index, completed, expected in [(125, True, '2'), (127, True, None), (127, None, '1')]:
            self.models.ReadingProgress.query.filter_by(comic_name=name, user_id=self.user_id).delete()
            self.db.session.commit()
            reading.save_reading_progress(name, completed_index, 0, 0, 126, 0, self.user_id,
                                          chapter_completed=completed)
            with patch.object(home, 'get_available_comics', return_value=[comic]), \
                    patch.object(library, 'get_available_comics', return_value=[comic]), \
                    patch.object(home, 'get_comic_descriptions', return_value={name: 'Description'}), \
                    patch.object(home, 'get_home_banner_url', return_value=''):
                for path in ['/', '/comics?group=全部']:
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    badge = BeautifulSoup(response.data, 'html.parser').select_one('.md-cover-unread')
                    self.assertEqual(badge.get_text(strip=True) if badge else None, expected, path)

    def test_counts_are_automatic_for_multiple_legacy_books_and_accounts(self):
        from mangadock.services import reading
        from mangadock.blueprints.web import home, library
        other = self.models.User(username='unread-other-account', role='user')
        other.set_password('test-password')
        self.db.session.add(other)
        self.db.session.commit()
        other_id = other.id
        self.sandbox.groups.set_user_group_permissions(other_id, ['默认分组'])
        books = [
            {'comic_name': 'Legacy A', 'available_chapters': 55, 'total_chapters': 1,
             'comic_format': 2, 'created_at': None, 'group': '默认分组'},
            {'comic_name': 'Legacy B', 'available_chapters': 128, 'total_chapters': 2,
             'comic_format': 2, 'created_at': None, 'group': '默认分组'},
        ]
        for name, chapter in [('Legacy A', 54), ('Legacy B', 126)]:
            reading.save_reading_progress(name, chapter, 0, 0, 0, 0, self.user_id)
            reading.save_reading_progress(name, chapter, 0, 0, 0, 0, other_id,
                                         chapter_completed=True)
        before = [(r.id, r.last_chapter, r.chapter_completed) for r in
                  self.models.ReadingProgress.query.filter(
                      self.models.ReadingProgress.comic_name.in_(['Legacy A', 'Legacy B'])).all()]
        with patch.object(home, 'get_available_comics', return_value=books), \
                patch.object(library, 'get_available_comics', return_value=books), \
                patch.object(home, 'get_comic_descriptions', return_value={'Legacy A': 'A', 'Legacy B': 'B'}), \
                patch.object(home, 'get_home_banner_url', return_value=''):
            for user_id, expected in [(self.user_id, {'Legacy A': '1', 'Legacy B': '2'}),
                                      (other_id, {'Legacy A': None, 'Legacy B': '1'})]:
                with self.client.session_transaction() as session:
                    session['user_id'] = user_id
                for path in ['/', '/comics?group=全部']:
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    soup = BeautifulSoup(response.data, 'html.parser')
                    for name, count in expected.items():
                        link = next(x for x in soup.select('a[href^="/comic/"]') if name in x.get_text())
                        badge = link.select_one('.md-cover-unread')
                        self.assertEqual(badge.get_text(strip=True) if badge else None, count)
        after = [(r.id, r.last_chapter, r.chapter_completed) for r in
                 self.models.ReadingProgress.query.filter(
                     self.models.ReadingProgress.comic_name.in_(['Legacy A', 'Legacy B'])).all()]
        self.assertEqual(before, after)
        self.assertTrue(all('unread_chapters' not in book for book in books))

    def test_completion_is_persisted_without_replacing_reading_position(self):
        from mangadock.services import reading
        name = 'completion-state'
        first = reading.save_reading_progress(name, 126, 0, 100, 128, 0, self.user_id,
                                             chapter_completed=False, client_progress_ms=100)
        self.assertFalse(first['chapter_completed'])
        done = reading.save_reading_progress(name, 126, 0, 200, 128, 0, self.user_id,
                                            chapter_completed=True, client_progress_ms=200)
        self.assertTrue(done['chapter_completed'])
        stale = reading.save_reading_progress(name, 126, 0, 100, 128, 0, self.user_id,
                                             chapter_completed=False, client_progress_ms=150)
        self.assertTrue(stale['chapter_completed'])
        self.assertTrue(stale['stale'])
        next_chapter = reading.save_reading_progress(name, 127, 0, 0, 128, 0, self.user_id,
                                                    chapter_completed=False, client_progress_ms=300)
        self.assertFalse(next_chapter['chapter_completed'])
        self.assertEqual(next_chapter['chapter_index'], 127)

    def test_web_save_accepts_completion_flag(self):
        from mangadock.blueprints.web import reader
        with patch.object(reader, 'user_can_access_progress_key', return_value=True), \
                patch.object(reader, 'require_csrf_for_session_auth', return_value=None):
            response = self.client.post('/save_progress', json={
                'comic_name': 'web-completion', 'chapter': 126, 'total_chapters': 128,
                'chapter_completed': False,
            })
        self.assertEqual(response.status_code, 200)
        saved = self.models.ReadingProgress.query.filter_by(comic_name='web-completion').one()
        self.assertFalse(saved.chapter_completed)


if __name__ == '__main__':
    unittest.main()
