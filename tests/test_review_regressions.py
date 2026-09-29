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
        cls.previous_modules = {name: module for name, module in sys.modules.items()
                                if name == 'mangadock' or name.startswith('mangadock.') or name == 'config'}
        for name in cls.previous_modules:
            sys.modules.pop(name)
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
        for name in list(sys.modules):
            if name == 'mangadock' or name.startswith('mangadock.') or name == 'config':
                sys.modules.pop(name)
        sys.modules.update(cls.previous_modules)
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
            first_response = client.get(url + '?page=0')
            self.assertEqual(first_response.data, first_page)
            self.assertEqual(first_response.headers['Cache-Control'], 'private, no-cache')
            etag = first_response.headers['ETag']
            with patch.object(reader, '_read_cbz_page', side_effect=AssertionError('reopened archive')):
                cached = client.get(url + '?page=0', headers={'If-None-Match': etag})
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.headers['ETag'], etag)
            self.assertEqual(client.get(url + '?page=1').data, b'second-page')
            self.assertEqual(client.get(url + '?page=3').status_code, 404)
            self.groups.set_user_group_permissions(user.id, [])
            with patch('mangadock.services.webdav_cbz.read_remote_cbz', side_effect=AssertionError('downloaded')):
                self.assertEqual(client.get(url + '?page=0').status_code, 403)

    def test_webdav_cached_page_revalidates_without_reading_ranges(self):
        from mangadock.blueprints.web import reader
        from mangadock.services import webdav

        user = self.models.User(username='webdav-cache-test', role='user')
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        self.groups.set_user_group_permissions(user.id, ['默认分组'])
        root = Path(self.workspace.name) / 'webdav-cache-test'
        comic = root / 'Book'
        comic.mkdir(parents=True)
        chapter = comic / '1.cbz'
        config = {'url': 'http://localhost/dav', 'root': '/book', 'username': 'user', 'password': 'secret'}
        indexed = {'filename': '1.cbz', 'path': '/book/1.cbz', 'size': 1024, 'etag': '"v1"'}
        source = webdav._source_id(config)
        webdav._write_index(str(comic), [indexed], source)
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user.id
        url = '/api/comic-pages/Book/1.cbz?page=0'
        with patch.object(webdav, 'cache_root', return_value=str(root)), \
             patch.object(webdav, '_read_config', return_value=config), \
             patch.object(reader, 'resolve_comic_file_request',
                          return_value={'comic_name': 'Book', 'file_path': str(chapter)}), \
             patch.object(reader, 'get_comic_group_map', return_value={'Book': '默认分组'}), \
             patch('mangadock.services.webdav_cbz.read_remote_cbz',
                   return_value=(['0.jpg'], [], '0.jpg', b'cached-image')) as read:
            first = client.get(url)
            self.assertEqual(first.data, b'cached-image')
            self.assertEqual(first.headers['Cache-Control'], 'private, no-cache')
            etag = first.headers['ETag']
            cached = client.get(url, headers={'If-None-Match': etag})
            self.assertEqual(cached.status_code, 304)
            read.assert_called_once()
            chapter.write_bytes(b'old cached chapter')
            webdav._write_cache_meta(str(chapter), indexed, source)
            webdav._write_index(str(comic), [{**indexed, 'etag': '"v2"'}], source)
            changed = client.get(url, headers={'If-None-Match': etag})
            self.assertEqual(changed.status_code, 200)
            self.assertNotEqual(changed.headers['ETag'], etag)
            self.assertEqual(read.call_count, 2)
            self.groups.set_user_group_permissions(user.id, [])
            self.assertEqual(client.get(url, headers={'If-None-Match': etag}).status_code, 403)

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

    def test_fanqie_novel_task_is_visible_only_to_its_creator(self):
        from datetime import datetime
        from mangadock.settings import china_tz
        from mangadock.services.groups import assign_comic_group, ensure_comic_group
        from mangadock.services.workers import start_novel_task

        ensure_comic_group('restricted-novel-name')
        assign_comic_group('私有小说', 'restricted-novel-name')
        creator = self.models.User(username='fanqie-task-owner', role='user', can_download=True)
        other = self.models.User(username='fanqie-task-other', role='user', can_download=True)
        creator.set_password('test-password')
        other.set_password('test-password')
        self.db.session.add_all([creator, other])
        self.db.session.commit()
        creator_id, other_id = creator.id, other.id
        self.groups.set_user_group_permissions(creator_id, ['默认分组'])

        with patch('mangadock.services.novels.get_novel_by_fanqie_id', return_value=None):
            task_id, reused = start_novel_task(
                '12345678', title='私有小说', created_by_user_id=creator_id,
            )
        self.assertFalse(reused)
        self.db.session.expire_all()
        task = self.db.session.get(self.models.DownloadTask, task_id)
        self.assertEqual(task.created_by_user_id, creator_id)
        self.assertEqual(task.url, 'fanqie://12345678')

        other_client = self.app.test_client()
        with other_client.session_transaction() as session:
            session['user_id'] = other_id
        self.assertEqual(other_client.get('/task_status/' + task_id).status_code, 404)
        self.assertEqual(other_client.post('/cancel_task/' + task_id).status_code, 404)
        self.assertNotIn('私有小说'.encode(), other_client.get('/tasks').data)
        self.db.session.expire_all()
        self.assertEqual(self.db.session.get(self.models.DownloadTask, task_id).status, 'pending')

        owner_client = self.app.test_client()
        with owner_client.session_transaction() as session:
            session['user_id'] = creator_id
        self.assertEqual(owner_client.get('/task_status/' + task_id).status_code, 200)

        with patch('mangadock.services.novels.get_novel_by_fanqie_id', return_value=None):
            hidden_id, hidden_reused = start_novel_task(
                '12345678', title='私有小说', created_by_user_id=other_id,
            )
        self.assertTrue(hidden_reused)
        self.assertIsNone(hidden_id)
        self.assertEqual(
            self.models.DownloadTask.query.filter_by(url='fanqie://12345678').count(), 1,
        )

        unowned = self.models.DownloadTask(
            id='77777777-7777-4777-8777-777777777777',
            comic_name='无主小说', url='fanqie://87654321', status='pending',
            comic_format=0, created_by_user_id=None,
            start_time=datetime.now(china_tz), created_at=datetime.now(china_tz),
        )
        self.db.session.add(unowned)
        self.db.session.commit()
        unowned_id = unowned.id
        self.assertEqual(other_client.get('/task_status/' + unowned_id).status_code, 404)
        self.assertEqual(other_client.post('/cancel_task/' + unowned_id).status_code, 404)
        admin = self.models.User.query.filter_by(username='admin').first()
        admin_client = self.app.test_client()
        with admin_client.session_transaction() as session:
            session['user_id'] = admin.id
        self.assertEqual(admin_client.get('/task_status/' + unowned_id).status_code, 200)

    def test_older_progress_snapshot_does_not_overwrite_a_newer_one(self):
        from mangadock.services.reading import save_reading_progress

        user = self.models.User(username='progress-order-user', role='user')
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        user_id = user.id
        name = 'progress-order'
        self.models.ReadingProgress.query.filter_by(comic_name=name).delete()
        self.db.session.commit()

        newer = save_reading_progress(name, 5, 1, 2, 10, 3, user_id, client_progress_ms=5_000)
        self.assertFalse(newer['stale'])
        older = save_reading_progress(name, 2, 0, 0, 10, 3, user_id, client_progress_ms=4_000)
        self.assertTrue(older['stale'])
        self.assertEqual(older['chapter_index'], 5)
        self.db.session.expire_all()
        stored = self.models.ReadingProgress.query.filter_by(comic_name=name, user_id=user_id).one()
        self.assertEqual(stored.last_chapter, 5)
        self.assertEqual(stored.last_page, 1)
        self.assertEqual(stored.client_progress_ms, 5_000)

        reread = save_reading_progress(name, 2, 4, 9, 10, 3, user_id, client_progress_ms=6_000)
        self.assertFalse(reread['stale'])
        self.assertEqual(reread['chapter_index'], 2)
        self.db.session.expire_all()
        stored = self.models.ReadingProgress.query.filter_by(comic_name=name, user_id=user_id).one()
        self.assertEqual(stored.last_chapter, 2)
        self.assertEqual(stored.last_page, 4)
        self.assertEqual(stored.scroll_position, 9)
        self.assertEqual(stored.client_progress_ms, 6_000)

    def test_clockless_save_cannot_overwrite_a_timestamped_snapshot(self):
        from mangadock.services.reading import note_server_progress_write, save_reading_progress

        user = self.models.User(username='clockless-progress-user', role='user')
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        user_id = user.id
        name = 'clockless-progress'
        self.models.ReadingProgress.query.filter_by(comic_name=name).delete()
        self.db.session.commit()

        save_reading_progress(name, 1, 0, 0, 4, 0, user_id)
        replaced = save_reading_progress(name, 3, 0, 0, 4, 0, user_id)
        self.assertFalse(replaced['stale'])
        self.assertEqual(replaced['chapter_index'], 3)
        self.db.session.expire_all()
        stored = self.models.ReadingProgress.query.filter_by(comic_name=name, user_id=user_id).one()
        self.assertIsNone(stored.client_progress_ms)

        save_reading_progress(name, 8, 0, 0, 4, 0, user_id, client_progress_ms=9_000)
        blocked = save_reading_progress(name, 2, 0, 0, 4, 0, user_id)
        self.assertTrue(blocked['stale'])
        self.assertEqual(blocked['chapter_index'], 8)
        self.db.session.expire_all()
        stored = self.models.ReadingProgress.query.filter_by(comic_name=name, user_id=user_id).one()
        self.assertEqual(stored.last_chapter, 8)
        self.assertEqual(stored.client_progress_ms, 9_000)

        note_server_progress_write(stored)
        self.db.session.commit()
        self.assertGreater(stored.client_progress_ms, 9_000)
        undone = save_reading_progress(
            name, 2, 0, 0, 4, 0, user_id, client_progress_ms=stored.client_progress_ms - 1,
        )
        self.assertTrue(undone['stale'])
        self.assertEqual(undone['chapter_index'], 8)

    def test_queued_novel_download_explains_instead_of_failing(self):
        user = self.models.User(username='queued-novel-user', role='user', can_download=True)
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user.id
        novel = {
            'kind': 'novel', 'book_id': '12345678', 'title': '排队小说', 'media_label': '番茄小说',
        }
        from mangadock.services.enqueue import EnqueueResult
        queued = EnqueueResult(
            True, 'NOVEL_IN_PROGRESS',
            '这本小说已在下载队列中，完成后会出现在小说书架',
            media_kind='novel', reused=True,
            fanqie_target={'media_label': '番茄小说'}, http_status=200,
        )
        with patch('mangadock.api_v1.common.require_csrf_for_session_auth', return_value=None), \
             patch('mangadock.api_v1.routes.enqueue_user_download', return_value=queued):
            response = client.post('/api/v1/downloads', json={
                'url': 'https://fanqienovel.com/page/12345678', 'format': 2,
            })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body['ok'])
        self.assertIsNone(body['data']['task_id'])
        self.assertIn('小说书架', body['data']['message'])
        self.assertEqual(body['message'], body['data']['message'])
        page = (Path(self.workspace.name) / 'templates' / 'download.html').read_text()
        self.assertIn('result.data.message', page)
        self.assertNotIn('!result.data.task_id) {\n                    showError(result.message', page)

    def test_string_reading_time_does_not_fail_a_saved_progress(self):
        user = self.models.User(username='reading-time-user', role='user')
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        user_id = user.id
        name = 'string-reading-time'
        self.models.ReadingProgress.query.filter_by(comic_name=name).delete()
        self.models.ReadingTime.query.filter_by(comic_name=name).delete()
        self.db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user_id
        with patch('mangadock.blueprints.web.reader.require_csrf_for_session_auth', return_value=None), \
             patch('mangadock.blueprints.web.reader.user_can_access_progress_key', return_value=True):
            saved = client.post('/save_progress', json={
                'comic_name': name, 'chapter': 2, 'reading_time': '1',
                'client_progress_ms': 3_000,
            })
            late = client.post('/save_progress', json={
                'comic_name': name, 'chapter': 1, 'reading_time': 0,
                'client_progress_ms': 2_000,
            })
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json['status'], 'success')
        self.assertEqual(late.status_code, 200)
        self.db.session.expire_all()
        stored = self.models.ReadingProgress.query.filter_by(comic_name=name, user_id=user_id).one()
        self.assertEqual(stored.last_chapter, 2)
        recorded = self.models.ReadingTime.query.filter_by(comic_name=name, user_id=user_id).all()
        self.assertEqual(sum(row.duration_seconds or 0 for row in recorded), 1)

    def test_deleted_group_does_not_fall_back_to_the_default_group(self):
        from mangadock.services.groups import assign_comic_group, delete_comic_group, ensure_comic_group

        ensure_comic_group('private-shelf')
        assign_comic_group('secret-book', 'private-shelf')
        user = self.models.User(username='default-only-reader', role='user')
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        self.groups.set_user_group_permissions(user.id, ['默认分组'])
        self.assertTrue(delete_comic_group('private-shelf'))
        self.assertEqual(self.groups.get_comic_group_map().get('secret-book'), 'private-shelf')
        self.assertFalse(self.groups.can_user_access_group('private-shelf', user))
        self.assertTrue(self.groups.can_user_access_group('默认分组', user))

    def test_reused_user_id_does_not_keep_the_old_session(self):
        user = self.models.User(username='recycled-user', role='user', session_version=9)
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user.id
            session['session_version'] = 1
        response = client.get('/comics', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])

    def test_deleted_user_tasks_cannot_pass_to_reused_user_id(self):
        admin = self.models.User.query.filter_by(username='admin').one()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = admin.id
            session['session_version'] = admin.session_version

        for suffix, delete_request in (
            ('web', lambda user_id: client.post(f'/users/{user_id}/delete')),
            ('api', lambda user_id: client.delete(f'/api/v1/users/{user_id}')),
        ):
            owner = self.models.User(username=f'deleted-task-owner-{suffix}', role='user')
            owner.set_password('test-password')
            self.db.session.add(owner)
            self.db.session.commit()
            owner_id = owner.id
            task = self.models.DownloadTask(
                id=f'deleted-task-{suffix}', comic_name=f'private-novel-{suffix}',
                url='fanqie://12345678', status='pending', created_by_user_id=owner_id,
            )
            self.db.session.add(task)
            self.db.session.commit()
            task_id = task.id

            if suffix == 'api':
                with patch('mangadock.api_v1.common.require_csrf_for_session_auth', return_value=None):
                    response = delete_request(owner_id)
            else:
                response = delete_request(owner_id)
            self.assertIn(response.status_code, (200, 302), f'{suffix}: {response.data[:200]!r}')
            self.db.session.expire_all()
            self.assertIsNone(self.db.session.get(self.models.User, owner_id))
            retired = self.db.session.get(self.models.DownloadTask, task_id)
            self.assertEqual(retired.created_by_user_id, -owner_id)
            self.assertEqual(retired.status, 'cancelled')
            from mangadock.services.tasks import update_task
            update_task(task_id, status='running')
            self.db.session.expire_all()
            retired = self.db.session.get(self.models.DownloadTask, task_id)
            self.assertEqual(retired.status, 'cancelled')

            replacement = self.models.User(username=f'replacement-task-user-{suffix}', role='user', can_download=True)
            replacement.set_password('test-password')
            self.db.session.add(replacement)
            self.db.session.commit()
            self.assertEqual(replacement.id, owner_id)
            self.assertFalse(self.groups.can_user_access_task(retired, replacement))

    def test_update_check_includes_marked_incomplete_chapter(self):
        chapter = {'title': '第一章', 'order': 1, 'filename_base': '0001_first'}
        queued = []
        with patch.object(self.updates, 'load_comic_mapping', return_value={'incomplete-review-book': 'https://baozimh.org/book/1'}), \
             patch('mangadock.services.download.load_comic_source', return_value={'chapters': [chapter]}), \
             patch.object(self.updates, 'get_local_chapter_bases', return_value={'0001_first'}), \
             patch.object(self.updates, 'get_local_chapter_match_bases', return_value={'0001_first'}), \
             patch.object(self.updates, 'get_incomplete_chapter_bases', return_value={'0001_first'}), \
             patch.object(self.updates, 'get_comic_update_mode', return_value='auto'), \
             patch.object(self.updates, 'enqueue_auto_update_tasks', side_effect=lambda candidates: queued.extend(candidates)):
            candidates = self.updates.refresh_update_checks(force=True)
        match = next(item for item in candidates if item.comic_name == 'incomplete-review-book')
        self.assertTrue(match.has_updates)
        self.assertEqual(match.pending_chapters, 1)
        self.assertIn(match, queued)

    def test_command_worker_recovery_leaves_live_downloads_running(self):
        from mangadock.services.workers import recover_background_queue_state

        alive_id = '88888888-8888-4888-8888-888888888888'
        dead_id = '99999999-9999-4999-8999-999999999999'
        self.db.session.add(self.models.DownloadTask(
            id=alive_id, status='running', comic_name='live-download', worker_pid=os.getpid(),
        ))
        self.db.session.add(self.models.DownloadTask(
            id=dead_id, status='running', comic_name='dead-download', worker_pid=2 ** 22,
        ))
        self.db.session.commit()
        recover_background_queue_state()
        self.db.session.expire_all()
        alive = self.db.session.get(self.models.DownloadTask, alive_id)
        dead = self.db.session.get(self.models.DownloadTask, dead_id)
        self.assertEqual(alive.status, 'running')
        self.assertEqual(alive.worker_pid, os.getpid())
        self.assertEqual(dead.status, 'pending')
        self.assertIsNone(dead.worker_pid)

    def test_webdav_hostname_is_rejected_when_it_resolves_to_link_local(self):
        from mangadock.services.webdav import WebDavError, _validate_base_url

        link_local = [(2, 1, 6, '', ('169.254.169.254', 0))]
        private = [(2, 1, 6, '', ('192.168.1.8', 0))]
        with patch('mangadock.services.webdav.socket.getaddrinfo', return_value=link_local):
            with self.assertRaises(WebDavError):
                _validate_base_url('http://cloud-metadata.example/dav')
        with patch('mangadock.services.webdav.socket.getaddrinfo', return_value=private):
            self.assertTrue(_validate_base_url('http://nas.home/dav').startswith('http://nas.home'))

    def test_readers_send_snapshot_time_and_webdav_poll_survives_http_errors(self):
        root = Path(self.workspace.name) / 'templates'
        for name in ('reader.html', 'novel_reader.html'):
            self.assertIn('client_progress_ms: nextProgressMs()', (root / name).read_text())
        for name in ('comics.html', 'settings_webdav.html'):
            poll = (root / name).read_text().split('async function poll()', 1)[1]
            self.assertNotIn('if (!response.ok) return', poll)
            self.assertIn('if (response.ok)', poll)
            if name == 'comics.html':
                self.assertIn('finally {', poll)
                self.assertIn('schedulePoll(nextDelay);', poll)
            else:
                self.assertIn('catch (_) {}', poll)
                self.assertIn('setTimeout(poll, 2000);', poll)

    def test_adult_gate_uses_provider_registry(self):
        from mangadock.services import download

        with patch('mangadock.services.providers.is_adult_url', return_value=False):
            self.assertFalse(download.is_adult_content_blocked('https://baozimh.org/manga/x'))
        with patch('mangadock.services.providers.is_adult_url', return_value=True), \
             patch('mangadock.services.adult_content.is_adult_content_enabled', return_value=False), \
             patch.object(download, 'load_comic_mapping', return_value={}):
            self.assertTrue(download.is_adult_content_blocked('https://baozimh.org/manga/x'))
            self.assertFalse(download.is_adult_content_blocked(
                'https://baozimh.org/manga/x', allow_adult=True,
            ))

    def test_enqueue_rejects_restricted_group_before_queueing(self):
        from mangadock.services.enqueue import enqueue_user_download
        from mangadock.services.groups import assign_comic_group, ensure_comic_group
        from mangadock.services.library import save_comic_mapping

        ensure_comic_group('restricted-enqueue-group')
        assign_comic_group('restricted-enqueue', 'restricted-enqueue-group')
        save_comic_mapping('restricted-enqueue', 'https://baozimh.org/manga/restricted-enqueue')
        user = self.models.User(username='enqueue-test', role='user', can_download=True)
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        self.groups.set_user_group_permissions(user.id, ['默认分组'])
        result = enqueue_user_download(user, 'https://baozimh.org/manga/restricted-enqueue', 2)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'FORBIDDEN')
        self.assertEqual(self.models.DownloadTask.query.count(), 0)

    def test_create_named_comic_group_rejects_reserved_name(self):
        from mangadock.services.groups import create_named_comic_group

        empty = create_named_comic_group('   ')
        self.assertFalse(empty.ok)
        self.assertEqual(empty.code, 'EMPTY')
        reserved = create_named_comic_group('全部')
        self.assertFalse(reserved.ok)
        self.assertEqual(reserved.code, 'RESERVED')
        created = create_named_comic_group('review-group')
        self.assertTrue(created.ok)
        self.assertTrue(created.created)
        again = create_named_comic_group('review-group')
        self.assertTrue(again.ok)
        self.assertFalse(again.created)
        self.assertEqual(again.code, 'EXISTS')

    def test_configured_cache_root_requires_remote_url(self):
        from mangadock.services import webdav

        with patch.object(webdav, '_read_config', return_value={}):
            self.assertIsNone(webdav.configured_cache_root())
        with patch.object(webdav, '_read_config', return_value={'url': 'http://nas/dav'}), \
             patch.object(webdav, 'cache_root', return_value='/tmp/webdav-comics'):
            self.assertEqual(webdav.configured_cache_root(), '/tmp/webdav-comics')

    def _session_client(self, user):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user.id
        return client

    def test_http_download_forbidden_is_403_on_web_and_404_on_api(self):
        from mangadock.services.groups import assign_comic_group, ensure_comic_group
        from mangadock.services.library import save_comic_mapping

        ensure_comic_group('http-restricted-group')
        assign_comic_group('http-restricted', 'http-restricted-group')
        save_comic_mapping('http-restricted', 'https://baozimh.org/manga/http-restricted')
        user = self.models.User(username='http-enqueue-acl', role='user', can_download=True)
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        self.groups.set_user_group_permissions(user.id, ['默认分组'])
        url = 'https://baozimh.org/manga/http-restricted'
        client = self._session_client(user)

        page = client.post('/download', data={'comic_url': url, 'format': '2'})
        self.assertEqual(page.status_code, 403)
        self.assertIn('无权访问该漫画'.encode(), page.data)
        self.assertEqual(self.models.DownloadTask.query.count(), 0)

        with patch('mangadock.api_v1.common.require_csrf_for_session_auth', return_value=None):
            api = client.post('/api/v1/downloads', json={'url': url, 'format': 2})
        self.assertEqual(api.status_code, 404)
        body = api.get_json()
        self.assertFalse(body['ok'])
        self.assertEqual(body['error']['code'], 'NOT_FOUND')
        self.assertEqual(body['error']['message'], '未找到该漫画')
        self.assertEqual(self.models.DownloadTask.query.count(), 0)

    def test_http_download_adult_url_is_blocked_when_disabled(self):
        from mangadock.settings import ADULT_CONTENT_DISABLED_MESSAGE

        user = self.models.User(username='http-adult-gate', role='user', can_download=True)
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        client = self._session_client(user)
        url = 'https://mxs12.cc/book/http-adult'

        page = client.post('/download', data={'comic_url': url, 'format': '2'})
        self.assertEqual(page.status_code, 200)
        self.assertIn(ADULT_CONTENT_DISABLED_MESSAGE.encode(), page.data)

        with patch('mangadock.api_v1.common.require_csrf_for_session_auth', return_value=None):
            api = client.post('/api/v1/downloads', json={'url': url, 'format': 2})
        self.assertEqual(api.status_code, 403)
        body = api.get_json()
        self.assertEqual(body['error']['code'], 'ADULT_CONTENT_DISABLED')
        self.assertEqual(body['error']['message'], ADULT_CONTENT_DISABLED_MESSAGE)
        self.assertEqual(self.models.DownloadTask.query.count(), 0)

    def test_http_download_queues_supported_comic(self):
        user = self.models.User(username='http-enqueue-ok', role='user', can_download=True)
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        client = self._session_client(user)
        url = 'https://baozimh.org/manga/http-enqueue-ok'

        with patch('mangadock.services.enqueue.start_download_task', return_value='queued-task-id') as start:
            page = client.get('/download')
            self.assertEqual(page.status_code, 200)
            created = client.post('/download', data={'comic_url': url, 'format': '2'})
            start.assert_called_once()
        self.assertEqual(created.status_code, 302)
        self.assertTrue(created.headers['Location'].endswith('/progress/queued-task-id'))

        with patch('mangadock.api_v1.common.require_csrf_for_session_auth', return_value=None), \
             patch('mangadock.services.enqueue.start_download_task', return_value='api-task-id'), \
             patch('mangadock.api_v1.routes.get_task', return_value=None):
            api = client.post('/api/v1/downloads', json={'url': url, 'format': 2})
        self.assertEqual(api.status_code, 201)
        body = api.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['data']['task_id'], 'api-task-id')
        self.assertEqual(body['data']['media_kind'], 'comic')

    def test_http_group_create_assign_delete_on_web_and_api(self):
        from mangadock.settings import COMIC_ROOT

        admin = self.models.User.query.filter_by(role='admin').one()
        client = self._session_client(admin)
        comic_name = 'http-group-comic'
        os.makedirs(os.path.join(COMIC_ROOT, comic_name), exist_ok=True)

        reserved = client.post('/comic_groups', data={'group_name': '全部'})
        self.assertEqual(reserved.status_code, 302)
        with client.session_transaction() as session:
            flashes = [message for _category, message in session.get('_flashes', [])]
        self.assertTrue(any('不能作为分组名称' in message for message in flashes))

        created = client.post('/comics/groups/create', data={'group_name': 'http-func-group'})
        self.assertEqual(created.status_code, 302)
        self.assertIn('http-func-group', self.groups.get_all_comic_groups())

        assigned = client.post('/comic_groups/assign', data={
            'comic_name': comic_name, 'group_name': 'http-func-group',
        })
        self.assertEqual(assigned.status_code, 302)
        self.assertEqual(self.groups.get_comic_group_map().get(comic_name), 'http-func-group')

        with patch('mangadock.api_v1.common.require_csrf_for_session_auth', return_value=None):
            api_exists = client.post('/api/v1/groups', json={'name': 'http-func-group'})
            api_default = client.delete('/api/v1/groups/默认分组')
            api_delete = client.delete('/api/v1/groups/http-func-group')
        self.assertEqual(api_exists.status_code, 200)
        self.assertFalse(api_exists.get_json()['data']['created'])
        self.assertEqual(api_default.status_code, 403)
        self.assertEqual(api_default.get_json()['error']['code'], 'FORBIDDEN')
        self.assertEqual(api_delete.status_code, 200)
        self.assertNotIn('http-func-group', self.groups.get_all_comic_groups())

    def test_login_page_renders_after_adult_url_cover_filter(self):
        response = self.app.test_client().get('/login')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'login', response.data.lower())

    def test_login_cover_requires_opt_in_and_rechecks_group(self):
        from mangadock.blueprints.web import auth_pages

        cover_name = 'public-cover-test'
        cover_path = Path(auth_pages.COVER_ROOT) / f'{cover_name}.jpg'
        cover_path.write_bytes(b'public-cover')
        client = self.app.test_client()
        with patch.object(auth_pages, '_pick_comic_covers', return_value=[cover_name]):
            self.assertEqual(client.get('/login-cover/0.jpg').status_code, 404)
            with patch.dict(os.environ, {'MANGADOCK_PUBLIC_LOGIN_COVERS': json.dumps([cover_name])}):
                self.assertIn(cover_name, auth_pages._eligible_comic_cover_names())
                response = client.get('/login-cover/0.jpg')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, b'public-cover')
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                response.close()
                index_path = Path(auth_pages.BASE_DIR) / 'comic.json'
                old_index = index_path.read_text(encoding='utf-8')
                try:
                    index = json.loads(old_index)
                    index[cover_name] = 'https://mxs12.cc/book/public-cover-test'
                    index_path.write_text(json.dumps(index), encoding='utf-8')
                    self.assertEqual(client.get('/login-cover/0.jpg').status_code, 404)
                finally:
                    index_path.write_text(old_index, encoding='utf-8')
                self.groups.ensure_comic_group('private-cover-test')
                self.groups.assign_comic_group(cover_name, 'private-cover-test')
                self.assertNotIn(cover_name, auth_pages._eligible_comic_cover_names())
                self.assertEqual(client.get('/login-cover/0.jpg').status_code, 404)

    def test_login_page_handles_non_object_comic_index_with_public_covers(self):
        from mangadock.blueprints.web import auth_pages

        cover_path = Path(auth_pages.COVER_ROOT) / 'public-cover-test.jpg'
        cover_path.write_bytes(b'public-cover')
        with patch.dict(os.environ, {'MANGADOCK_PUBLIC_LOGIN_COVERS': '["public-cover-test"]'}), \
             patch.object(auth_pages.json, 'load', return_value=[]):
            self.assertEqual(auth_pages._eligible_comic_cover_names(), [])
            self.assertEqual(self.app.test_client().get('/login').status_code, 200)

    def test_cbz_page_read_is_bounded_for_local_archives(self):
        import zipfile
        from mangadock.blueprints.web import reader

        archive_path = Path(self.workspace.name) / 'oversized-page.cbz'
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('1.jpg', b'12345')
        with self.app.test_request_context('/api/comic-pages/test/oversized-page.cbz?page=0'), \
             patch.object(reader, 'MAX_CBZ_PAGE_BYTES', 4):
            with self.assertRaisesRegex(RuntimeError, '超过 64MB'):
                reader._read_cbz_page(str(archive_path))

    def test_epub_entry_and_chapter_reads_are_bounded(self):
        import zipfile
        from mangadock.services import novels

        epub_path = Path(self.workspace.name) / 'oversized-chapter.epub'
        with zipfile.ZipFile(epub_path, 'w') as epub:
            epub.writestr('chapter.xhtml', b'12345')
        with zipfile.ZipFile(epub_path) as epub:
            self.assertEqual(novels._read_epub_entry(epub, 'chapter.xhtml', 5), b'12345')
            with self.assertRaisesRegex(ValueError, '大小限制'):
                novels._read_epub_entry(epub, 'chapter.xhtml', 4)
        metadata = {'spine': [{'href': 'chapter.xhtml'}]}
        with patch.object(novels, 'get_novel', return_value={'file_path': str(epub_path)}), \
             patch.object(novels, '_metadata', return_value=metadata), \
             patch.object(novels, 'MAX_EPUB_CHAPTER_BYTES', 4):
            self.assertIsNone(novels.get_novel_chapter('oversized-chapter', 0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
