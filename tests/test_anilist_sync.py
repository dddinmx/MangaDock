"""Isolated AniList integration checks; no live AniList calls."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


class AniListSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix='mangadock-anilist-')
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
        from mangadock.models import AniListAccount, AniListComicLink, User
        from mangadock.services import anilist
        cls.app, cls.db = app, db
        cls.Account, cls.Link, cls.User = AniListAccount, AniListComicLink, User
        cls.service = anilist
        cls.app.config['WTF_CSRF_ENABLED'] = False

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.engine.dispose()
        sys.path.pop(0)
        cls.environment.stop()
        cls.workspace.cleanup()

    def setUp(self):
        self.context = self.app.app_context()
        self.context.push()
        self.Link.query.delete()
        self.Account.query.delete()
        self.db.session.commit()

    def tearDown(self):
        self.db.session.remove()
        self.context.pop()

    def _user(self, name):
        user = self.User(username=name)
        user.set_password('test-password')
        self.db.session.add(user)
        self.db.session.commit()
        return user.id

    def test_completed_chapter_uses_offset_and_never_lowers_remote(self):
        user_id = self._user('anilist-offset')
        token = self.service._cipher().encrypt(b'test-token').decode('ascii')
        self.db.session.add(self.Account(user_id=user_id, anilist_user_id=42,
                                         username='remote', encrypted_token=token))
        self.db.session.add(self.Link(user_id=user_id, comic_name='Local Manga',
                                      media_id=123, media_title='Remote Manga', first_chapter=11))
        self.db.session.commit()
        calls = []

        def fake_graphql(query, variables=None, token=None):
            calls.append((query, variables, token))
            if query.lstrip().startswith('mutation'):
                return {'SaveMediaListEntry': {'id': 9, 'progress': variables['progress']}}
            return {'Media': {'id': 123, 'mediaListEntry': {'id': 9, 'progress': 10}}}

        with patch.object(self.service, 'graphql', side_effect=fake_graphql), \
             patch.object(self.service, 'list_local_chapters', return_value=[{'title': '第1話'}, {'title': '第2話'}]):
            self.assertTrue(self.service.sync_completed_chapter(user_id, 'Local Manga', 0))
        self.assertEqual(calls[1][1]['progress'], 11)
        self.assertEqual(calls[1][2], 'test-token')
        self.db.session.expire_all()
        self.assertEqual(self.Link.query.filter_by(user_id=user_id).first().synced_progress, 11)

        calls.clear()
        with patch.object(self.service, 'list_local_chapters', return_value=[{'title': '第1話'}, {'title': '第2話'}]), \
             patch.object(self.service, 'graphql', return_value={
            'Media': {'id': 123, 'mediaListEntry': {'id': 9, 'progress': 20}}
        }):
            self.assertFalse(self.service.sync_completed_chapter(user_id, 'Local Manga', 1))

    def test_numbered_chapters_sync_actual_number_and_skip_afterword(self):
        user_id = self._user('anilist-numbered')
        token = self.service._cipher().encrypt(b'test-token').decode('ascii')
        self.db.session.add(self.Account(user_id=user_id, anilist_user_id=42,
                                         username='remote', encrypted_token=token))
        self.db.session.add(self.Link(user_id=user_id, comic_name='Numbered Manga',
                                      media_id=123, media_title='Remote Manga', first_chapter=1))
        self.db.session.commit()
        chapters = [{'title': '第1話'}, {'title': '第3話'}, {'title': '後記'}]
        calls = []

        def fake_graphql(query, variables=None, token=None):
            calls.append(variables)
            if query.lstrip().startswith('mutation'):
                return {'SaveMediaListEntry': {'id': 9, 'progress': variables['progress']}}
            return {'Media': {'id': 123, 'mediaListEntry': {'id': 9, 'progress': 0}}}

        with patch.object(self.service, 'list_local_chapters', return_value=chapters), \
             patch.object(self.service, 'graphql', side_effect=fake_graphql):
            self.assertTrue(self.service.sync_completed_chapter(user_id, 'Numbered Manga', 1))
            self.assertFalse(self.service.sync_completed_chapter(user_id, 'Numbered Manga', 2))
        self.assertEqual(calls[1]['progress'], 3)
        self.assertEqual(len(calls), 2)

    def test_prologue_does_not_shift_progress_and_scan_session_close_is_safe(self):
        user_id = self._user('anilist-prologue')
        token = self.service._cipher().encrypt(b'test-token').decode('ascii')
        self.db.session.add(self.Account(user_id=user_id, anilist_user_id=42,
                                         username='remote', encrypted_token=token))
        self.db.session.add(self.Link(user_id=user_id, comic_name='Prologue Manga',
                                      media_id=123, media_title='Remote Manga', first_chapter=1))
        self.db.session.commit()
        calls = []

        def scan(_):
            self.db.session.remove()
            return [{'title': '序章'}, {'title': '第1话'}, {'title': '第7话'}]

        def remote(query, variables=None, token=None):
            calls.append(variables)
            if query.lstrip().startswith('mutation'):
                return {'SaveMediaListEntry': {'id': 9, 'progress': variables['progress']}}
            return {'Media': {'id': 123, 'mediaListEntry': {'id': 9, 'progress': 0}}}

        with patch.object(self.service, 'list_local_chapters', side_effect=scan), \
             patch.object(self.service, 'graphql', side_effect=remote):
            self.assertFalse(self.service.sync_completed_chapter(user_id, 'Prologue Manga', 0))
            self.assertTrue(self.service.sync_completed_chapter(user_id, 'Prologue Manga', 2))
        self.assertEqual(calls[1]['progress'], 7)
        self.db.session.remove()
        self.assertEqual(self.Link.query.filter_by(user_id=user_id).first().synced_progress, 7)

    def test_match_rejects_first_chapter_after_known_end_and_preserves_progress(self):
        user_id = self._user('anilist-match-validation')
        self.db.session.add(self.Link(user_id=user_id, comic_name='Local Manga',
                                      media_id=123, media_title='Remote Manga',
                                      first_chapter=1, synced_progress=7))
        self.db.session.commit()
        media = {'Media': {'id': 123, 'chapters': 10,
                           'title': {'userPreferred': 'Remote Manga'}}}
        with patch.object(self.service, 'graphql', return_value=media):
            with self.assertRaises(ValueError):
                self.service.link_manga(user_id, 'Local Manga', 123, 11)
            self.service.link_manga(user_id, 'Local Manga', 123, 1)
        link = self.Link.query.filter_by(user_id=user_id, comic_name='Local Manga').first()
        self.assertEqual((link.first_chapter, link.synced_progress), (1, 7))

    def _linked_progress_user(self, name):
        from mangadock.models import ReadingProgress
        user_id = self._user(name)
        token = self.service._cipher().encrypt(b'test-token').decode('ascii')
        self.db.session.add(self.Account(user_id=user_id, anilist_user_id=42,
                                         username='remote', encrypted_token=token))
        self.db.session.add(self.Link(user_id=user_id, comic_name='Sync Manga',
                                      media_id=123, media_title='Remote Manga', first_chapter=1))
        self.db.session.commit()
        return user_id, ReadingProgress

    def test_remote_ahead_resumes_next_main_chapter_and_equal_keeps_page(self):
        user_id, Progress = self._linked_progress_user('anilist-pull')
        self.db.session.add(Progress(user_id=user_id, comic_name='Sync Manga',
                                     last_chapter=3, last_page=12, scroll_position=900))
        self.db.session.commit()
        chapters = [{'title': '序章'}] + [{'title': f'第{i}话'} for i in range(1, 11)]
        remote = {'Media': {'id': 123, 'mediaListEntry': {'id': 9, 'progress': 8}}}
        with patch.object(self.service, 'list_local_chapters', return_value=chapters), \
             patch.object(self.service, 'graphql', return_value=remote) as api:
            self.assertFalse(self.service.reconcile_progress(user_id, 'Sync Manga'))
            self.assertEqual(api.call_count, 1)
            p = Progress.query.filter_by(user_id=user_id).first()
            self.assertEqual((p.last_chapter, p.last_page, p.scroll_position), (9, 0, 0))
            p.last_page, p.scroll_position = 5, 600
            self.db.session.commit()
            self.service.reconcile_progress(user_id, 'Sync Manga')
            self.db.session.expire_all()
            self.assertEqual((p.last_chapter, p.last_page, p.scroll_position), (9, 5, 600))

    def test_local_cursor_pushes_only_previous_completed_chapter(self):
        user_id, Progress = self._linked_progress_user('anilist-push')
        self.db.session.add(Progress(user_id=user_id, comic_name='Sync Manga',
                                     last_chapter=8, last_page=5, scroll_position=600))
        self.db.session.commit()
        chapters = [{'title': '序章'}] + [{'title': f'第{i}话'} for i in range(1, 11)]
        calls = []

        def remote(query, variables=None, token=None):
            calls.append(variables)
            if query.lstrip().startswith('mutation'):
                return {'SaveMediaListEntry': {'id': 9, 'progress': variables['progress']}}
            return {'Media': {'id': 123, 'mediaListEntry': {'id': 9, 'progress': 2}}}

        with patch.object(self.service, 'list_local_chapters', return_value=chapters), \
             patch.object(self.service, 'graphql', side_effect=remote):
            self.assertTrue(self.service.reconcile_progress(user_id, 'Sync Manga'))
        self.assertEqual(calls[1]['progress'], 7)
        p = Progress.query.filter_by(user_id=user_id).first()
        self.assertEqual((p.last_chapter, p.last_page, p.scroll_position), (8, 5, 600))

    def test_completion_survives_network_failure_for_background_retry(self):
        user_id, _ = self._linked_progress_user('anilist-retry')
        chapters = [{'title': f'第{i}话'} for i in range(1, 11)]
        with patch.object(self.service, 'list_local_chapters', return_value=chapters), \
             patch.object(self.service, 'graphql', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                self.service.sync_completed_chapter(user_id, 'Sync Manga', 7)
        self.db.session.rollback()
        link = self.Link.query.filter_by(user_id=user_id).first()
        self.assertEqual((link.completed_progress, link.synced_progress), (8, 0))

        def remote(query, variables=None, token=None):
            if query.lstrip().startswith('mutation'):
                return {'SaveMediaListEntry': {'id': 9, 'progress': variables['progress']}}
            return {'Media': {'id': 123, 'mediaListEntry': {'id': 9, 'progress': 2}}}

        with patch.object(self.service, 'list_local_chapters', return_value=chapters), \
             patch.object(self.service, 'graphql', side_effect=remote):
            self.assertTrue(self.service.reconcile_progress(user_id, 'Sync Manga'))
        self.db.session.expire_all()
        self.assertEqual(link.synced_progress, 8)

    def test_remote_beyond_downloads_is_retained_until_chapters_arrive(self):
        user_id, Progress = self._linked_progress_user('anilist-missing')
        remote = {'Media': {'id': 123, 'mediaListEntry': {'id': 9, 'progress': 8}}}
        with patch.object(self.service, 'list_local_chapters',
                          return_value=[{'title': f'第{i}话'} for i in range(1, 4)]), \
             patch.object(self.service, 'graphql', return_value=remote):
            self.service.reconcile_progress(user_id, 'Sync Manga')
        self.assertEqual(Progress.query.filter_by(user_id=user_id).first().last_chapter, 2)
        self.assertEqual(self.Link.query.filter_by(user_id=user_id).first().completed_progress, 8)
        with patch.object(self.service, 'list_local_chapters',
                          return_value=[{'title': f'第{i}话'} for i in range(1, 11)]), \
             patch.object(self.service, 'graphql', return_value=remote):
            self.service.reconcile_progress(user_id, 'Sync Manga')
        self.assertEqual(Progress.query.filter_by(user_id=user_id).first().last_chapter, 8)

    def test_unlinked_manga_makes_no_remote_call(self):
        user_id = self._user('anilist-unlinked')
        with patch.object(self.service, 'list_local_chapters', return_value=[{'title': '第1话'}]), \
             patch.object(self.service, 'graphql') as api:
            self.assertFalse(self.service.reconcile_progress(user_id, 'Unlinked Manga'))
            api.assert_not_called()

    def test_finishing_afterword_syncs_last_numbered_chapter(self):
        user_id = self._user('anilist-afterword')
        token = self.service._cipher().encrypt(b'test-token').decode('ascii')
        self.db.session.add(self.Account(user_id=user_id, anilist_user_id=42,
                                         username='remote', encrypted_token=token))
        self.db.session.add(self.Link(user_id=user_id, comic_name='Finished Manga',
                                      media_id=123, media_title='Remote Manga', first_chapter=1))
        self.db.session.commit()
        chapters = [{'title': '第1話'}, {'title': '第123話（完結）'}, {'title': '後記'}]
        calls = []

        def fake_graphql(query, variables=None, token=None):
            calls.append(variables)
            if query.lstrip().startswith('mutation'):
                return {'SaveMediaListEntry': {'id': 9, 'progress': variables['progress']}}
            return {'Media': {'id': 123, 'mediaListEntry': {'id': 9, 'progress': 61}}}

        with patch.object(self.service, 'list_local_chapters', return_value=chapters), \
             patch.object(self.service, 'graphql', side_effect=fake_graphql):
            self.assertTrue(self.service.sync_completed_chapter(user_id, 'Finished Manga', 2))
        self.assertEqual(calls[1]['progress'], 123)

    def test_connection_is_per_user_and_token_is_encrypted(self):
        first = self._user('anilist-first')
        second = self._user('anilist-second')
        with patch.object(self.service, 'graphql', return_value={'Viewer': {'id': 42, 'name': 'remote'}}) as graphql:
            self.service.connect(first, ' private-token ')
        self.assertEqual(graphql.call_args.kwargs['token'], 'private-token')
        self.assertEqual(self.service.token_for(first), 'private-token')
        self.assertIsNone(self.service.token_for(second))
        self.assertNotIn('private-token', self.db.session.get(self.Account, first).encrypted_token)

    def test_pin_page_and_submit_connect_current_user(self):
        user_id = self._user('anilist-state')
        client = self.app.test_client()
        with client.session_transaction() as state:
            state['user_id'] = user_id
        page = client.get('/anilist/connect')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'client_id=51982', page.data)
        self.assertIn(b'response_type=token', page.data)
        with patch('mangadock.blueprints.web.anilist.connect') as connect:
            response = client.post('/anilist/connect', data={'access_token': 'pasted-token'}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        connect.assert_called_once_with(user_id, 'pasted-token')

    def test_invalid_pin_does_not_replace_existing_account(self):
        user_id = self._user('anilist-invalid')
        token = self.service._cipher().encrypt(b'old-token').decode('ascii')
        self.db.session.add(self.Account(user_id=user_id, anilist_user_id=42,
                                         username='original', encrypted_token=token))
        self.db.session.commit()
        with patch.object(self.service, 'graphql', return_value={}), self.assertRaises(ValueError):
            self.service.connect(user_id, 'bad-token')
        self.assertEqual(self.service.token_for(user_id), 'old-token')

    def test_reauthorize_preserves_links_only_for_same_anilist_user(self):
        user_id = self._user('anilist-reconnect')
        self.db.session.add(self.Account(user_id=user_id, anilist_user_id=42,
                                         username='original', encrypted_token='unused'))
        self.db.session.add(self.Link(user_id=user_id, comic_name='Local Manga',
                                      media_id=123, media_title='Remote Manga', first_chapter=1))
        self.db.session.commit()
        with patch.object(self.service, 'graphql', return_value={'Viewer': {'id': 42, 'name': 'original'}}):
            self.service.connect(user_id, 'new-token')
        self.assertEqual(self.Link.query.filter_by(user_id=user_id).count(), 1)
        with patch.object(self.service, 'graphql', return_value={'Viewer': {'id': 43, 'name': 'different'}}):
            self.service.connect(user_id, 'different-token')
        self.assertEqual(self.Link.query.filter_by(user_id=user_id).count(), 0)

    def test_completion_rejects_inaccessible_comic(self):
        user_id = self._user('anilist-acl')
        client = self.app.test_client()
        with client.session_transaction() as state:
            state['user_id'] = user_id
        with patch('mangadock.blueprints.web.anilist.user_can_access_progress_key', return_value=False), \
             patch('mangadock.blueprints.web.anilist.require_csrf_for_session_auth', return_value=None), \
             patch('mangadock.blueprints.web.anilist.sync_completed_chapter') as sync:
            response = client.post('/anilist/complete', json={
                'comic_name': 'Private Manga', 'chapter_index': 0,
            })
        self.assertEqual(response.status_code, 403)
        sync.assert_not_called()

    def test_settings_and_matching_page_render_for_connected_user(self):
        user_id = self._user('anilist-pages')
        self.db.session.add(self.Account(user_id=user_id, anilist_user_id=42,
                                         username='remote', encrypted_token='unused'))
        self.db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as state:
            state['user_id'] = user_id
        settings_response = client.get('/settings')
        self.assertEqual(settings_response.status_code, 200)
        self.assertIn(b'remote', settings_response.data)
        with patch('mangadock.blueprints.web.anilist.user_can_access_progress_key', return_value=True), \
             patch('mangadock.blueprints.web.anilist.get_comic_directory', return_value='/fake'), \
             patch('mangadock.blueprints.web.anilist.list_local_chapters', return_value=[{}]), \
             patch('mangadock.blueprints.web.anilist.search_manga', return_value=[{
                 'id': 123, 'title': {'userPreferred': 'Remote Manga'}, 'chapters': 20,
                 'siteUrl': 'https://anilist.co/manga/123',
             }]):
            response = client.get('/anilist/match/Local%20Manga')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Remote Manga', response.data)


if __name__ == '__main__':
    unittest.main()
