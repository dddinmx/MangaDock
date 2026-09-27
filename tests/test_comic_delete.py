"""Deletion tests use a temporary application copy; never touch the live library."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_review_regressions as regressions


class ComicDeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        regressions.ReviewRegressions.setUpClass.__func__(cls)
        from mangadock.services import comic_delete
        cls.deletion = comic_delete

    @classmethod
    def tearDownClass(cls):
        regressions.ReviewRegressions.tearDownClass.__func__(cls)

    def setUp(self):
        regressions.ReviewRegressions.setUp(self)
        self.root = Path(self.workspace.name).resolve() / 'deletion-fixtures'
        shutil.rmtree(self.root, ignore_errors=True)
        self.source = self.root / 'comics'
        self.source.mkdir(parents=True)
        self.comic = self.source / '待删除漫画'
        self.comic.mkdir()
        (self.comic / 'chapter.cbz').write_bytes(b'local-source')
        self.other = self.source / '其他漫画'
        self.other.mkdir()
        (self.other / 'chapter.cbz').write_bytes(b'keep')
        self.covers = self.root / 'cover'
        (self.covers / 'hero').mkdir(parents=True)
        for folder in (self.covers, self.covers / 'hero'):
            (folder / '待删除漫画.jpg').write_bytes(b'cover')
            (folder / '其他漫画.jpg').write_bytes(b'keep')
        self.banners_dir = self.root / 'banners'
        (self.banners_dir / 'upscaled').mkdir(parents=True)
        key = hashlib.sha256('待删除漫画'.encode()).hexdigest()
        (self.banners_dir / (key + '.jpg')).write_bytes(b'banner')
        (self.banners_dir / 'upscaled' / (key + '-revision.jpg')).write_bytes(b'banner')
        (self.banners_dir / 'other.jpg').write_bytes(b'keep')
        self.mapping = self.root / 'comic.json'
        self.mapping.write_text(json.dumps({'待删除漫画': 'https://baozimh.org/comic/a',
                                           '其他漫画': 'https://baozimh.org/comic/b'}))
        self.cache = self.root / 'pages'
        (self.cache / 'delete-id').mkdir(parents=True)
        (self.cache / 'delete-id' / 'page.jpg').write_bytes(b'cache')
        self.patches = [
            patch.object(self.library, 'get_comic_scan_roots', return_value=[str(self.source)]),
            patch.object(self.deletion, 'COMIC_MAPPING_FILE', str(self.mapping)),
            patch.object(self.deletion, 'COVER_ROOT', str(self.covers)),
            patch.object(self.deletion, 'HOME_BANNER_DIR', str(self.banners_dir)),
            patch.object(self.deletion, 'API_PAGE_CACHE_ROOT', str(self.cache)),
        ]
        for item in self.patches:
            item.start()
        for model in (self.models.ComicIdentity, self.models.ComicGroupMembership,
                      self.models.ReadingProgress, self.models.AniListComicLink,
                      self.models.ReadingSessionState, self.models.ComicUpdateCheck,
                      self.models.AdminHiddenLibraryItem, self.models.ReadingTime):
            model.query.delete()
        self.db.session.commit()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        regressions.ReviewRegressions.tearDown(self)

    def delete(self):
        self.deletion.delete_local_comic('待删除漫画', str(self.comic))

    def test_removes_sources_metadata_and_cache_preserves_other_and_history(self):
        m = self.models
        from datetime import datetime
        self.db.session.add_all([
            m.DownloadTask(id='delete-task', comic_name='待删除漫画', status='completed'),
            m.DownloadTask(id='keep-task', comic_name='其他漫画', status='completed'),
            m.ComicIdentity(comic_id='delete-id', comic_name='待删除漫画'),
            m.ComicGroupMembership(comic_name='待删除漫画'),
            m.ComicUpdateCheck(comic_name='待删除漫画', source_url='https://baozimh.org/comic/a'),
            m.AdminHiddenLibraryItem(user_id=1, target_type='comic', target_value='待删除漫画'),
            m.ReadingTime(user_id=1, comic_name='待删除漫画', duration=10, read_at=datetime.now()),
        ])
        for user_id in (1, 2):
            self.db.session.add_all([
                m.ReadingProgress(user_id=user_id, comic_name='待删除漫画'),
                m.ReadingSessionState(user_id=user_id, comic_name='待删除漫画', session_key='session'),
                m.AniListComicLink(user_id=user_id, comic_name='待删除漫画', media_id=42, media_title='title'),
            ])
        self.db.session.commit()
        (self.comic / 'external-link').symlink_to(self.other)
        self.library.comic_directory_scan_cache['old'] = {}
        self.library.comic_root_listing_cache['old'] = {}
        self.delete()
        self.assertFalse(self.comic.exists())
        self.assertFalse((self.covers / '待删除漫画.jpg').exists())
        self.assertFalse((self.covers / 'hero' / '待删除漫画.jpg').exists())
        self.assertFalse((self.cache / 'delete-id').exists())
        self.assertEqual(list((self.banners_dir / 'upscaled').iterdir()), [])
        self.assertEqual(list(self.banners_dir.glob('*.jpg')), [self.banners_dir / 'other.jpg'])
        self.assertEqual((self.other / 'chapter.cbz').read_bytes(), b'keep')
        self.assertEqual((self.covers / '其他漫画.jpg').read_bytes(), b'keep')
        self.assertEqual(set(json.loads(self.mapping.read_text())), {'其他漫画'})
        for model in (m.DownloadTask, m.ComicIdentity, m.ComicGroupMembership,
                      m.ComicUpdateCheck, m.ReadingProgress, m.ReadingSessionState, m.AniListComicLink):
            self.assertEqual(model.query.filter_by(comic_name='待删除漫画').count(), 0)
        self.assertEqual(m.ReadingTime.query.count(), 1)
        self.assertEqual(m.AdminHiddenLibraryItem.query.count(), 0)
        self.assertEqual(m.DownloadTask.query.count(), 1)
        self.assertEqual(self.library.comic_root_listing_cache, {})
        self.assertEqual(self.library.comic_directory_scan_cache, {})
        self.assertNotIn('待删除漫画', dict(self.library.iter_local_comic_directories()))

    def test_active_tasks_block_even_after_cancel_or_different_title(self):
        for status, pid, name in [('pending', None, '待删除漫画'),
                                  ('running', os.getpid(), '待删除漫画'),
                                  ('cancelled', os.getpid(), '待删除漫画'),
                                  ('running', None, '未知漫画'),
                                  ('pending', None, '另一个标题')]:
            task = self.models.DownloadTask(id='busy', comic_name=name, status=status,
                                           worker_pid=pid, url='https://www.baozimh.org/comic/a/')
            self.db.session.add(task)
            self.db.session.commit()
            with self.assertRaisesRegex(ValueError, '任务'):
                self.delete()
            self.assertTrue((self.comic / 'chapter.cbz').exists())
            self.db.session.delete(self.db.session.get(self.models.DownloadTask, 'busy'))
            self.db.session.commit()

    def test_duplicate_symlink_traversal_and_changed_path_are_rejected(self):
        for bad_name in ('../其他漫画', '..', '/tmp', '待删除漫画 '):
            with self.assertRaises(ValueError):
                self.deletion.delete_local_comic(bad_name, str(self.comic))
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.deletion.delete_local_comic('待删除漫画', str(self.other))
        extra = self.root / 'extra'
        (extra / '待删除漫画').mkdir(parents=True)
        with patch.object(self.library, 'get_comic_scan_roots', return_value=[str(self.source), str(extra)]):
            with self.assertRaisesRegex(ValueError, '同名'):
                self.delete()
        shutil.rmtree(extra / '待删除漫画')
        (extra / '待删除漫画').symlink_to(self.other)
        with patch.object(self.library, 'get_comic_scan_roots', return_value=[str(extra)]):
            with self.assertRaisesRegex(ValueError, '符号链接'):
                self.delete()
        self.assertTrue((self.other / 'chapter.cbz').exists())

    def test_bad_mapping_or_filesystem_error_does_not_report_success(self):
        self.mapping.write_text('{broken')
        with self.assertRaises(json.JSONDecodeError):
            self.delete()
        self.assertTrue(self.comic.exists())
        self.mapping.write_text('{}')
        self.db.session.add(self.models.DownloadTask(id='completed', comic_name='待删除漫画', status='completed'))
        self.db.session.commit()
        with patch.object(self.deletion.shutil, 'rmtree', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.delete()
        self.assertIsNotNone(self.db.session.get(self.models.DownloadTask, 'completed'))
        self.assertTrue(self.comic.exists())
        self.assertFalse(list(self.root.glob('.comic-json-*')))

    def test_other_web_process_discards_cached_deleted_comic(self):
        old_version = self.library.comic_deletion_version()
        self.delete()
        self.library.comics_cache.update({
            'data': [{'comic_name': '待删除漫画'}], 'timestamp': self.library.time.time(),
            'deletion_version': old_version,
        })
        with patch.object(self.library, 'build_available_comics_snapshot', return_value=[]):
            self.assertEqual(self.library.get_available_comics(), [])
        self.assertEqual(self.library.comics_cache['deletion_version'],
                         self.library.comic_deletion_version())

    def test_worker_releases_cancelled_task_marker_after_exit(self):
        from mangadock.services import workers
        import tempfile
        self.db.session.add(self.models.DownloadTask(
            id='cancelled-worker', comic_name='待删除漫画', status='cancelled', worker_pid=os.getpid()))
        self.db.session.commit()
        lock = tempfile.TemporaryFile()
        with patch.object(workers, 'acquire_background_worker_lock', return_value=lock), \
             patch.object(workers, 'claim_next_pending_task', side_effect=['cancelled-worker', KeyboardInterrupt]), \
             patch.object(workers, 'execute_download_task', return_value=False):
            with self.assertRaises(KeyboardInterrupt):
                workers.run_task_worker(0)
        self.db.session.expire_all()
        self.assertIsNone(self.db.session.get(self.models.DownloadTask, 'cancelled-worker').worker_pid)
        self.delete()
        self.assertFalse(self.comic.exists())

    def test_running_metadata_job_blocks_deletion_of_its_comic(self):
        command = self.models.BackgroundCommand(command_type='webdav_metadata', status='running',
                    payload=json.dumps({'comic_name': '待删除漫画'}))
        self.db.session.add(command)
        self.db.session.commit()
        try:
            with self.assertRaisesRegex(ValueError, '封面与简介'):
                self.delete()
            self.assertTrue(self.comic.exists())
        finally:
            self.db.session.rollback()
            self.models.BackgroundCommand.query.filter_by(command_type='webdav_metadata').delete()
            self.db.session.commit()

    def test_background_update_check_blocks_deletion(self):
        self.db.session.add(self.models.ComicUpdateCheck(
            comic_name='其他漫画', source_url='https://example.invalid', status='checking'))
        self.db.session.commit()
        with self.assertRaisesRegex(ValueError, '正在检查'):
            self.delete()
        self.assertTrue(self.comic.exists())

    def test_smb_dir_fd_unlink_retries_absolute_path(self):
        original_unlink = os.unlink
        attempts = []
        def smb_unlink(path, *args, **kwargs):
            if kwargs.get('dir_fd') is not None:
                attempts.append(path)
                raise FileNotFoundError(2, 'SMB dir_fd failure', path)
            return original_unlink(path, *args, **kwargs)
        with patch.object(self.deletion.os, 'unlink', side_effect=smb_unlink):
            self.delete()
        self.assertTrue(attempts)
        self.assertFalse(self.comic.exists())
        self.assertTrue(self.other.exists())

    def test_disappeared_child_is_ignored_but_permissions_still_fail(self):
        original_unlink = os.unlink
        def disappearing_unlink(path, *args, **kwargs):
            if kwargs.get('dir_fd') is not None:
                original_unlink(path, *args, **kwargs)
                raise FileNotFoundError(2, 'entry disappeared', path)
            return original_unlink(path, *args, **kwargs)
        with patch.object(self.deletion.os, 'unlink', side_effect=disappearing_unlink):
            self.delete()
        self.assertFalse(self.comic.exists())
        self.comic.mkdir()
        (self.comic / 'chapter.cbz').write_bytes(b'keep')
        with patch.object(self.deletion.os, 'unlink', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.delete()
        self.assertTrue((self.comic / 'chapter.cbz').exists())

    def test_template_shows_confirmation_only_for_admin(self):
        from types import SimpleNamespace
        from jinja2 import Environment
        source = (Path(self.workspace.name) / 'templates' / 'comic_detail.html').read_text()
        start = source.index('{% if is_admin_user and deletion_path %}')
        end = source.index('{% endif %}', start) + len('{% endif %}')
        template = Environment(autoescape=True).from_string(source[start:end])
        values = dict(is_admin_user=True, deletion_path=str(self.comic),
                      task=SimpleNamespace(comic_name='漫画"<test>'),
                      url_for=lambda name: '/comic/delete', csrf_token=lambda: 'token')
        rendered = template.render(**values)
        self.assertIn('删除漫画及源文件', rendered)
        self.assertIn('漫画&#34;&lt;test&gt;', rendered)
        self.assertIn(str(self.comic), rendered)
        values['is_admin_user'] = False
        self.assertEqual(template.render(**values).strip(), '')
        self.app.jinja_env.get_template('comic_detail.html')

    def test_route_requires_admin_and_csrf_then_deletes(self):
        user = self.models.User(username='delete-user', role='user')
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        client = self.app.test_client()
        data = {'comic_name': '待删除漫画', 'directory': str(self.comic)}
        self.assertEqual(client.post('/comic/delete', data=data).status_code, 302)
        with client.session_transaction() as session:
            session['user_id'] = user.id
        self.assertEqual(client.post('/comic/delete', data=data).status_code, 302)
        self.assertTrue(self.comic.exists())
        admin = self.models.User.query.filter_by(username='admin').first()
        with client.session_transaction() as session:
            session['user_id'] = admin.id
        self.app.config['WTF_CSRF_ENABLED'] = True
        try:
            self.assertEqual(client.post('/comic/delete', data=data).status_code, 400)
            self.assertTrue(self.comic.exists())
        finally:
            self.app.config['WTF_CSRF_ENABLED'] = False
        response = client.post('/comic/delete', data=data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/comics'))
        self.assertFalse(self.comic.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
