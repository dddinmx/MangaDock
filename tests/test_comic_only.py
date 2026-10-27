"""Comic-only behavior, using an isolated application and database."""
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_review_regressions as regressions


class ComicOnlyTests(unittest.TestCase):
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
        self.user = self.models.User.query.filter_by(username='admin').one()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.user.id

    def test_novel_routes_are_removed(self):
        self.assertFalse(any('novel' in rule.endpoint for rule in self.app.url_map.iter_rules()))
        for path in ('/novels', '/novels/fanqie', '/novels/search', '/novels/groups'):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_comic_pages_ignore_old_mode_and_render(self):
        for path in ('/', '/comics', '/history', '/statistics?mode=novel', '/settings?mode=novel', '/download'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn(b'/novels', response.data, path)
            self.assertNotIn(b'mode-switcher-toggle', response.data, path)
        self.assertEqual(self.client.get('/login').status_code, 200)

    def test_novel_download_rejected_but_comic_queued(self):
        from mangadock.services import enqueue
        with patch.object(enqueue, 'is_supported_comic_url', return_value=True), \
                patch.object(enqueue, 'load_fanqie_comic_source') as resolve, \
                patch.object(enqueue, 'start_download_task', return_value='comic-task') as start:
            from mangadock.services.fanqie_api import FanqieApiError
            resolve.side_effect = FanqieApiError('Not an image comic', 'INVALID_TARGET')
            result = enqueue.enqueue_user_download(self.user, 'https://fanqienovel.com/page/12345678')
            self.assertFalse(result.ok)
            self.assertEqual(result.code, 'UNSUPPORTED_MEDIA')
            start.assert_not_called()
            response = self.client.post('/api/v1/downloads', json={'url': 'https://fanqienovel.com/page/12345678'})
            self.assertEqual(response.status_code, 400)
            start.assert_not_called()
            resolve.side_effect = None
            resolve.return_value = {'book_id': '12345678', 'title': 'comic'}
            result = enqueue.enqueue_user_download(self.user, 'https://fanqienovel.com/page/12345678')
            self.assertTrue(result.ok)
            self.assertEqual(result.task_id, 'comic-task')
            start.assert_called_once()

    def test_fanqie_resolver_only_requests_comics(self):
        from mangadock.services import fanqie_comics
        from mangadock.services.fanqie_api import FanqieApiError
        from unittest.mock import Mock
        client = Mock()
        client.resolve_resource.return_value = {
            'metadata': {'book_id': '12345678', 'title': 'Comic'},
            'chapters': [{'id': '1', 'title': 'Chapter 1'}],
        }
        source = fanqie_comics.load_fanqie_comic_source('fanqie-comic://12345678', client)
        self.assertEqual(source['source_url'], 'fanqie-comic://12345678')
        self.assertEqual(source['chapters'][0]['title'], 'Chapter 1')
        client.resolve_resource.assert_called_once_with('12345678', 'comic')
        client.reset_mock()
        client.resolve_resource.side_effect = FanqieApiError('Wrong type', 'INVALID_TARGET')
        with self.assertRaises(FanqieApiError):
            fanqie_comics.load_fanqie_comic_source('fanqie-comic://12345678', client)
        client.resolve_resource.assert_called_once_with('12345678', 'comic')

    def test_old_novel_worker_is_cancelled(self):
        from mangadock.services import workers
        task = SimpleNamespace(url='fanqie://12345678', status='pending')
        with patch.object(workers, 'get_task', return_value=task), \
                patch.object(workers, 'update_task') as update:
            self.assertFalse(workers.execute_download_task('old-task'))
            self.assertEqual(update.call_args.kwargs['status'], 'cancelled')

    def test_statistics_exclude_novels_without_deleting_data(self):
        from mangadock.services import reading
        from mangadock.settings import china_tz
        now = datetime.now(china_tz)
        self.models.ReadingTime.query.delete()
        self.db.session.add_all([
            self.models.ReadingTime(user_id=self.user.id, comic_name='comic', duration=2, duration_seconds=120, read_at=now),
            self.models.ReadingTime(user_id=self.user.id, comic_name='novel:old', duration=10, duration_seconds=600, read_at=now),
        ])
        self.db.session.commit()
        user_id = self.user.id
        self.assertEqual(reading.get_total_reading_time(user_id), 2)
        self.assertEqual([row['comic_name'] for row in reading.get_reading_time_by_comic(user_id)], ['comic'])
        self.assertEqual(reading.get_reading_time_monthly_for_year(now.year, user_id)[0][1], 2)
        self.assertEqual(reading.get_reading_time_daily_for_year(now.year, user_id)[0][1], 2)
        self.assertEqual(self.models.ReadingTime.query.count(), 2)
        self.assertFalse(reading.user_can_access_progress_key('novel:old', self.user))
        self.assertEqual(self.client.get('/static/cover/novels/old.jpg').status_code, 404)


if __name__ == '__main__':
    unittest.main()
