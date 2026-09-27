import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import BackgroundCommand, User
from mangadock.services import updates, webdav, library


class WebDavBackgroundTests(unittest.TestCase):
    def test_command_returns_while_sync_is_blocked_and_persists_progress(self):
        entered, release = threading.Event(), threading.Event()
        with app.app_context():
            command = updates.queue_background_command('sync_webdav', dedupe_pending=False)
            command_id = command.id
        def slow_sync(progress=None):
            progress({'done': 1, 'total': 2, 'added': 1})
            entered.set()
            release.wait(5)
            return 'finished'
        try:
            with patch.object(webdav, 'sync_library', side_effect=slow_sync):
                self.assertTrue(updates.execute_background_command(command_id))
                self.assertTrue(entered.wait(2))
                with app.app_context():
                    row = db.session.get(BackgroundCommand, command_id)
                    self.assertEqual(json.loads(row.payload)['done'], 1)
                release.set()
                for _ in range(100):
                    with app.app_context():
                        row = db.session.get(BackgroundCommand, command_id)
                        if row.status == 'completed':
                            self.assertEqual(row.message, 'finished')
                            break
                    time.sleep(.02)
                else:
                    self.fail('background sync did not complete')
        finally:
            release.set()
            with app.app_context():
                BackgroundCommand.query.filter_by(id=command_id).delete()
                db.session.commit()

    def test_enqueue_route_returns_without_sync_and_status_survives_new_client(self):
        from mangadock.blueprints.web import settings_pages
        with app.app_context():
            user_id = User.query.filter_by(username='admin').first().id
        with patch.dict(app.config, {'WTF_CSRF_ENABLED': False}), \
                patch.object(settings_pages, 'queue_library_sync') as queued, \
                patch.object(webdav, 'sync_library') as sync:
            with app.test_client() as client:
                with client.session_transaction() as session:
                    session['user_id'] = user_id
                response = client.post('/settings/webdav/sync')
                self.assertEqual(response.status_code, 302)
                queued.assert_called_once()
                sync.assert_not_called()
            with app.test_client() as client:
                with client.session_transaction() as session:
                    session['user_id'] = user_id
                response = client.get('/settings/webdav/progress')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertIn('version', response.json)

    def test_published_index_invalidates_another_process_snapshot(self):
        with tempfile.TemporaryDirectory() as root, patch.object(app, 'instance_path', root):
            old_version = library.comic_deletion_version()
            with patch.dict(library.comics_cache, {'data': [{'comic_name': 'old'}],
                    'timestamp': time.time(), 'deletion_version': old_version}), \
                    patch.object(library, 'refresh_comics_cache', return_value=[{'comic_name': 'new'}]) as refresh:
                webdav._publish_library_change()
                self.assertEqual(library.get_available_comics(), [{'comic_name': 'new'}])
                refresh.assert_called_once_with(force=True)

    def test_queue_deduplicates_active_sync(self):
        with tempfile.TemporaryDirectory() as root, patch.object(app, 'instance_path', root), \
                patch.object(webdav, '_require_config', return_value={'url': 'http://localhost/dav'}):
            first = webdav.queue_library_sync()
            second = webdav.queue_library_sync()
            self.assertEqual(first.id, second.id)
            with app.app_context():
                BackgroundCommand.query.filter_by(id=first.id).delete()
                db.session.commit()

    def test_each_index_is_published_before_next_directory_and_cover(self):
        config = {'url': 'http://localhost/dav', 'root': '', 'username': 'test', 'password': 'test'}
        items = [{'is_dir': True, 'name': name, 'path': '/dav/' + name + '/'} for name in ('one', 'two')]
        chapter = {'filename': '001.cbz', 'path': '/dav/one/001.cbz', 'size': 1}
        reports = []
        with tempfile.TemporaryDirectory() as root, patch.object(app, 'instance_path', root), \
                patch.object(webdav, '_require_config', return_value=config), \
                patch.object(webdav, '_propfind', return_value={'items': []}), \
                patch.object(webdav, '_direct_children', return_value=items), \
                patch.object(webdav, '_listing_is_usable', return_value=True), \
                patch.object(webdav, '_chapters_and_cover', return_value=([chapter], None)), \
                patch.object(webdav, '_sync_config', return_value=config), \
                patch.object(webdav, '_write_config'), \
                patch.object(webdav, '_can_prune_missing_comics', return_value=False), \
                patch.object(library, 'get_comic_scan_roots', return_value=[]), \
                patch.object(library, 'refresh_comics_cache'), \
                patch.object(webdav, '_save_cover') as cover:
            def report(value):
                reports.append(value)
                if value['added'] == 1:
                    self.assertTrue(os.path.isfile(os.path.join(webdav.cache_root(), 'one', webdav.INDEX_FILENAME)))
                    self.assertGreater(library.comic_deletion_version(), 0)
                    if value['done'] == 1 and not value['current']:
                        self.assertFalse(os.path.exists(os.path.join(webdav.cache_root(), 'two')))
                        cover.assert_not_called()
            webdav.sync_library(progress=report)
        self.assertEqual(reports[-1]['done'], 2)
        self.assertEqual(reports[-1]['added'], 2)
