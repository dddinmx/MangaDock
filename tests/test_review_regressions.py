"""Run with python3 tests/test_review_regressions.py; all data stays in a temp copy."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


class ReviewRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix='mangadock-regressions-')
        root = Path(cls.workspace.name)
        source = Path(__file__).resolve().parents[1]
        shutil.copytree(source / 'mangadock', root / 'mangadock',
                        ignore=shutil.ignore_patterns('__pycache__', '._*'))
        shutil.copytree(source / 'templates', root / 'templates',
                        ignore=shutil.ignore_patterns('._*'))
        shutil.copy2(source / 'config.py', root / 'config.py')
        (root / 'static' / 'cover').mkdir(parents=True)
        (root / 'comic.json').write_text('{}')
        cls.environment = patch.dict(os.environ, {
            'MANGADOCK_ADMIN_PASSWORD': 'isolated-test-password',
            'MANGADOCK_FANQIE_API_AUTO_REGISTER': 'false',
            'MANGADOCK_REPORT_URL': '',
        })
        cls.environment.start()
        sys.path.insert(0, str(root))
        from mangadock import app, db
        from mangadock import auth, models
        from mangadock.services import groups, home_banner, library, updates
        cls.app, cls.db = app, db
        cls.auth, cls.models = auth, models
        cls.groups, cls.banners = groups, home_banner
        cls.library, cls.updates = library, updates
        cls.app.config['WTF_CSRF_ENABLED'] = False

    @classmethod
    def tearDownClass(cls):
        cls.banners._lookup_pool.shutdown(wait=True)
        cls.banners._upscale_pool.shutdown(wait=True)
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.engine.dispose()
        sys.path.pop(0)
        cls.environment.stop()
        cls.workspace.cleanup()

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        self.models.BackgroundCommand.query.delete()
        self.models.LoginFailure.query.delete()
        self.models.DownloadTask.query.delete()
        self.db.session.commit()

    def tearDown(self):
        self.db.session.remove()
        self.context.pop()

    def test_revoked_groups_stay_empty(self):
        user = self.models.User(username='group-test', role='user')
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        self.groups.set_user_group_permissions(user.id, ['默认分组'])
        self.assertTrue(self.groups.can_user_access_group('默认分组', user))
        self.groups.set_user_group_permissions(user.id, [])
        self.assertEqual(self.groups.get_accessible_group_names(user), [])
        self.assertFalse(self.groups.can_user_access_group('默认分组', user))
        admin = self.models.User.query.filter_by(username='admin').first()
        self.assertTrue(self.groups.can_user_access_group('默认分组', admin))

    def test_cbz_pages_load_individually_and_check_group_access(self):
        from io import BytesIO
        import zipfile
        from PIL import Image
        from mangadock.blueprints.web import reader

        user = self.models.User(username='cbz-page-test', role='user')
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        self.groups.set_user_group_permissions(user.id, ['默认分组'])
        archive_path = Path(self.workspace.name) / 'chapter-pages-test.cbz'
        image_bytes = BytesIO()
        Image.new('RGB', (720, 800)).save(image_bytes, format='JPEG')
        first_page = image_bytes.getvalue()
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('10.jpg', b'tenth-page')
            archive.writestr('2.jpg', b'second-page')
            archive.writestr('1.jpg', first_page)
        resolved = {'comic_name': 'cbz-page-test', 'file_path': str(archive_path)}
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user.id
        url = '/api/comic-pages/cbz-page-test/chapter-pages-test.cbz'
        with patch.object(reader, 'resolve_comic_file_request', return_value=resolved), \
             patch.object(reader, 'get_comic_group_map', return_value={'cbz-page-test': '默认分组'}):
            metadata = client.get(url).json
            self.assertEqual(metadata['pages'], 3)
            self.assertEqual(metadata['dimensions'], [[720, 800], [0, 0], [0, 0]])
            self.assertEqual(client.get(url + '?page=0').data, first_page)
            self.assertEqual(client.get(url + '?page=1').data, b'second-page')
            self.assertEqual(client.get(url + '?page=3').status_code, 404)
            self.groups.set_user_group_permissions(user.id, [])
            with patch('mangadock.services.webdav.chapter_access', side_effect=AssertionError('downloaded')):
                self.assertEqual(client.get(url + '?page=0').status_code, 403)

    def test_cached_auth_only_writes_when_failures_exist(self):
        from sqlalchemy import event
        user = self.models.User(username='auth-test', role='user')
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        args = (user.username, 'test-password', '127.0.0.1', 'regression-test')
        self.assertIsNone(self.auth.authenticate_api_credentials(*args)[1])
        writes = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith(('DELETE', 'INSERT', 'UPDATE')):
                writes.append(statement)

        event.listen(self.db.engine, 'before_cursor_execute', capture)
        try:
            with patch.object(self.auth, 'validate_user_credentials',
                              side_effect=AssertionError('cache was not used')):
                self.assertIsNone(self.auth.authenticate_api_credentials(*args)[1])
                self.assertEqual(writes, [])
                keys = self.auth.login_failure_keys(user.username, '127.0.0.1')
                self.auth.login_record_failure(keys)
                self.assertGreater(self.models.LoginFailure.query.count(), 0)
                self.assertIsNone(self.auth.authenticate_api_credentials(*args)[1])
                self.assertEqual(self.models.LoginFailure.query.count(), 0)
        finally:
            event.remove(self.db.engine, 'before_cursor_execute', capture)

    def run_scan(self, search, save=None):
        self.banners.queue_initial_home_banner_backfill()
        command_id = self.updates.claim_next_background_command()
        with patch.object(self.library, 'get_available_comics',
                          return_value=[{'comic_name': 'test-banner'}]), \
             patch.object(self.banners, '_search_anilist', side_effect=search), \
             patch.object(self.banners, '_search_kitsu', side_effect=search), \
             patch.object(self.banners, '_save_if_landscape', return_value=save):
            self.banners._run_home_banner_backfill(command_id)
        self.db.session.expire_all()
        return self.db.session.get(self.models.BackgroundCommand, command_id)

    def test_failed_metadata_scan_retries_next_startup(self):
        command = self.run_scan(TimeoutError('offline'))
        self.assertEqual(command.status, 'error')
        self.banners.queue_initial_home_banner_backfill()
        self.assertEqual(self.models.BackgroundCommand.query.filter_by(status='pending').count(), 1)

    def test_failed_image_download_retries(self):
        command = self.run_scan(lambda name: [{'image_url': 'unused'}], save=None)
        self.assertEqual(command.status, 'error')

    def test_image_network_error_is_retryable(self):
        with patch.object(self.banners, 'safe_http_get', side_effect=TimeoutError('offline')):
            self.assertIsNone(self.banners._save_if_landscape('test-banner', 'unused'))

    def test_no_match_completes_without_retry(self):
        command = self.run_scan(lambda name: [])
        self.assertEqual(command.status, 'completed')
        self.assertIsNone(self.banners.queue_initial_home_banner_backfill())

    def test_successful_image_completes(self):
        command = self.run_scan(lambda name: [{'image_url': 'unused'}], save=True)
        self.assertEqual(command.status, 'completed')
        self.assertIn('找到 1 张横幅', command.message)

    def test_legacy_completed_scan_is_revisited_once(self):
        self.db.session.add(self.models.BackgroundCommand(
            command_type='scan_home_banners', payload=json.dumps({}), status='completed'))
        self.db.session.commit()
        command = self.run_scan(lambda name: [])
        self.assertEqual(command.status, 'completed')
        self.assertIsNone(self.banners.queue_initial_home_banner_backfill())

    def test_private_cover_requires_group_permission(self):
        from mangadock.settings import COVER_ROOT
        from mangadock.services.groups import ensure_comic_group, assign_comic_group
        from pathlib import Path

        Path(COVER_ROOT, 'restricted-cover.jpg').write_bytes(b'private-cover')
        ensure_comic_group('restricted-cover-group')
        assign_comic_group('restricted-cover', 'restricted-cover-group')
        user = self.models.User(username='cover-test', role='user')
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        self.groups.set_user_group_permissions(user.id, ['默认分组'])

        client = self.app.test_client()
        path = '/static/cover/restricted-cover.jpg'
        self.assertEqual(client.get(path).status_code, 403)
        basic = {'Authorization': 'Basic Y292ZXItdGVzdDp0ZXN0LXBhc3N3b3Jk'}
        self.assertEqual(client.get(path, headers=basic).status_code, 403)
        with client.session_transaction() as session:
            session['user_id'] = user.id
        self.assertEqual(client.get(path).status_code, 403)
        self.groups.set_user_group_permissions(user.id, ['restricted-cover-group'])
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'private-cover')
        self.assertEqual(response.headers['Cache-Control'], 'private, no-cache')
        etag = response.headers['ETag']
        response.close()
        cached = client.get(path, headers={'If-None-Match': etag})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.data, b'')
        cached.close()
        self.groups.set_user_group_permissions(user.id, [])
        self.assertEqual(client.get(path, headers={'If-None-Match': etag}).status_code, 403)
        self.groups.set_user_group_permissions(user.id, ['restricted-cover-group'])
        basic_client = self.app.test_client()
        response = basic_client.get(path, headers=basic)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'private-cover')
        response.close()

    def test_restricted_task_is_hidden_and_cannot_be_cancelled(self):
        from datetime import datetime
        from mangadock.settings import china_tz
        from mangadock.services.groups import ensure_comic_group, assign_comic_group

        ensure_comic_group('restricted-task-group')
        assign_comic_group('restricted-task', 'restricted-task-group')
        user = self.models.User(username='task-test', role='user', can_download=True)
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        self.groups.set_user_group_permissions(user.id, ['默认分组'])
        task = self.models.DownloadTask(
            id='11111111-1111-4111-8111-111111111111', comic_name='restricted-task',
            url='https://baozimh.org/manga/restricted-task', status='pending',
            group='restricted-task-group', comic_format=2,
            start_time=datetime.now(china_tz), created_at=datetime.now(china_tz),
        )
        self.db.session.add(task)
        self.db.session.commit()
        task_id = task.id
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user.id
        response = client.get('/tasks')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'restricted-task', response.data)
        self.assertEqual(client.get('/task_status/' + task_id).status_code, 404)
        self.assertEqual(client.post('/cancel_task/' + task_id).status_code, 404)
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(self.models.DownloadTask, task_id).status, 'pending')

    def test_unresolved_task_is_visible_only_to_creator(self):
        from datetime import datetime
        from mangadock.settings import china_tz

        users = []
        for username in ('task-creator', 'task-other'):
            user = self.models.User(username=username, role='user', can_download=True)
            user.set_password('test-password')
            self.db.session.add(user)
            users.append(user)
        self.db.session.commit()
        user_ids = [user.id for user in users]
        for user_id in user_ids:
            self.groups.set_user_group_permissions(user_id, ['默认分组'])
        task = self.models.DownloadTask(
            id='66666666-6666-4666-8666-666666666666', comic_name='未知漫画',
            url='https://baozimh.org/manga/unresolved', status='pending',
            group='默认分组', comic_format=2, created_by_user_id=user_ids[0],
            start_time=datetime.now(china_tz), created_at=datetime.now(china_tz),
        )
        self.db.session.add(task)
        self.db.session.commit()
        task_id = task.id
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user_ids[1]
        self.assertEqual(client.get('/task_status/' + task_id).status_code, 404)
        self.assertEqual(client.post('/cancel_task/' + task_id).status_code, 404)
        with client.session_transaction() as session:
            session['user_id'] = user_ids[0]
        self.assertEqual(client.get('/task_status/' + task_id).status_code, 200)

    def test_resolved_title_is_checked_before_writing_library(self):
        from datetime import datetime
        from mangadock.services import download
        from mangadock.services.groups import ensure_comic_group, assign_comic_group
        from mangadock.settings import COMIC_ROOT, china_tz

        ensure_comic_group('restricted-download-group')
        assign_comic_group('restricted-download', 'restricted-download-group')
        user = self.models.User(username='download-test', role='user', can_download=True)
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        self.groups.set_user_group_permissions(user.id, ['默认分组'])
        task = self.models.DownloadTask(
            id='22222222-2222-4222-8222-222222222222', comic_name='未知漫画',
            url='https://baozimh.org/manga/alternate-source', status='pending',
            comic_format=2, created_by_user_id=user.id,
            start_time=datetime.now(china_tz), created_at=datetime.now(china_tz),
        )
        self.db.session.add(task)
        self.db.session.commit()
        task_id = task.id
        source = {'title': 'restricted-download', 'chapters': [{'title': 'chapter'}]}
        with patch.object(download, 'load_comic_source', return_value=source), \
             patch.object(download, 'is_adult_content_blocked', return_value=False), \
             patch.object(download, 'save_comic_mapping') as save_mapping:
            download.download_complete_book(task.url, 2, task.id)
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(self.models.DownloadTask, task_id).status, 'error')
        self.assertFalse(Path(COMIC_ROOT, 'restricted-download').exists())
        save_mapping.assert_not_called()

    def test_known_restricted_title_is_rejected_before_queueing(self):
        from mangadock.services import download, tasks
        from mangadock.services.groups import ensure_comic_group, assign_comic_group

        ensure_comic_group('restricted-preflight-group')
        assign_comic_group('restricted-preflight', 'restricted-preflight-group')
        user = self.models.User(username='preflight-test', role='user', can_download=True)
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        self.groups.set_user_group_permissions(user.id, ['默认分组'])
        with patch.object(download, 'load_comic_source',
                          return_value={'title': 'restricted-preflight'}):
            with self.assertRaises(PermissionError):
                tasks.create_task('https://baozimh.org/manga/alternate', 2,
                                  created_by_user_id=user.id)
        self.assertEqual(self.models.DownloadTask.query.count(), 0)

    def test_claim_records_worker_pid(self):
        from mangadock.services.workers import claim_next_pending_task
        self.db.session.add(self.models.DownloadTask(
            id='55555555-5555-4555-8555-555555555555',
            comic_name='claim-test', status='pending'))
        self.db.session.commit()
        task_id = claim_next_pending_task()
        self.assertEqual(task_id, '55555555-5555-4555-8555-555555555555')
        self.db.session.expire_all()
        task = self.db.session.get(self.models.DownloadTask, task_id)
        self.assertEqual(task.worker_pid, os.getpid())

    def test_only_dead_workers_tasks_are_requeued(self):
        from mangadock.services.workers import requeue_orphan_running_tasks

        for task_id, pid in [('33333333-3333-4333-8333-333333333333', 111),
                             ('44444444-4444-4444-8444-444444444444', 222)]:
            self.db.session.add(self.models.DownloadTask(
                id=task_id, status='running', comic_name='worker-test', worker_pid=pid))
        self.db.session.commit()
        self.assertEqual(requeue_orphan_running_tasks(worker_pids=[111]), 1)
        self.db.session.expire_all()
        statuses = {task.worker_pid: task.status for task in
                    self.models.DownloadTask.query.filter_by(comic_name='worker-test').all()}
        self.assertEqual(statuses[None], 'pending')
        self.assertEqual(statuses[222], 'running')


if __name__ == '__main__':
    unittest.main(verbosity=2)
