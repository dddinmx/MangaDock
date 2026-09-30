import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import BackgroundCommand, AniListComicLink, User
from mangadock.services import library, updates, webdav, webdav_metadata as metadata, mangaupdates_metadata as secondary


class WebDavMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.config = {'url': 'http://localhost/dav', 'root': '', 'username': 'test', 'password': 'test'}
        self.patches = [patch.object(app, 'instance_path', self.root),
                        patch.object(webdav, '_read_config', side_effect=lambda: dict(self.config)),
                        patch.object(library, 'get_comic_scan_roots', return_value=[os.path.join(self.root, 'webdav_comics')]),
                        patch.object(metadata, 'COVER_ROOT', os.path.join(self.root, 'covers')),
                        patch.object(secondary, 'search_metadata', return_value=[])]
        for mocked in self.patches:
            mocked.start()
            self.addCleanup(mocked.stop)
        self.comic = os.path.join(webdav.cache_root(), 'Book')
        os.makedirs(self.comic)
        self.source = webdav._source_id(self.config)
        webdav._write_index(self.comic, [{'filename': '1.cbz', 'path': '/dav/Book/1.cbz'}], self.source)
        self.payload = {'comic_name': 'Book', 'source': self.source, 'media_id': None}
        self.media = {'id': 123, 'title': {'native': 'Book', 'userPreferred': 'Book'}, 'synonyms': [],
                      'description': '<p>A <b>story</b>.</p><script>bad()</script>',
                      'coverImage': {'large': 'https://s4.anilist.co/cover.jpg'}}
        self.addCleanup(self.clean_commands)

    def clean_commands(self):
        with app.app_context():
            BackgroundCommand.query.filter_by(command_type='webdav_metadata').delete()
            db.session.commit()

    def image_response(self):
        from unittest.mock import Mock
        data = io.BytesIO()
        Image.new('RGB', (20, 30), 'blue').save(data, 'PNG')
        response = Mock(content=data.getvalue())
        return response

    def test_exact_match_rejects_ambiguous_or_fuzzy_names(self):
        self.assertEqual(metadata.exact_match('ＢＯＯＫ', [self.media])['id'], 123)
        chinese = {**self.media, 'title': {'native': '进击的巨人'}}
        self.assertEqual(metadata.exact_match('進擊的巨人', [chinese])['id'], 123)
        self.assertIsNone(metadata.exact_match('Book 2', [self.media]))
        self.assertIsNone(metadata.exact_match('Book', [self.media, {**self.media, 'id': 456}]))

    def test_auto_enrichment_saves_jpeg_and_plain_description_without_progress_link(self):
        from mangadock.utils import cover_enhance
        with app.app_context():
            before = AniListComicLink.query.count()
        with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
                patch.object(metadata, 'safe_http_get', return_value=self.image_response()), \
                patch.object(cover_enhance, 'request_hero_cover') as upscale:
            metadata.enrich_metadata(self.payload)
            upscale.assert_called_once_with('Book')
        value = metadata.metadata_view('Book')
        self.assertEqual(value['media_id'], 123)
        self.assertNotIn('bad()', value['description'])
        self.assertNotIn('<', value['description'])
        with Image.open(os.path.join(metadata.COVER_ROOT, 'Book.jpg')) as image:
            self.assertEqual(image.format, 'JPEG')
        self.assertEqual(library.get_comic_description('Book'), value['description'])
        self.assertEqual(library.get_comic_descriptions(['Book'])['Book'], value['description'])
        with app.app_context():
            self.assertEqual(AniListComicLink.query.count(), before)

    def test_upscale_queue_failure_keeps_saved_metadata(self):
        from mangadock.utils import cover_enhance
        with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
                patch.object(metadata, 'safe_http_get', return_value=self.image_response()), \
                patch.object(cover_enhance, 'request_hero_cover', side_effect=RuntimeError('queue failed')):
            metadata.enrich_metadata(self.payload)
        self.assertEqual(metadata.metadata_view('Book')['status'], 'completed')
        self.assertTrue(Path(metadata.COVER_ROOT, 'Book.jpg').is_file())

    def test_existing_cover_and_description_are_preserved(self):
        os.makedirs(metadata.COVER_ROOT)
        cover = Path(metadata.COVER_ROOT, 'Book.jpg')
        cover.write_bytes(b'original cover')
        library.save_comic_description('Book', 'Existing description')
        try:
            with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
                    patch.object(metadata, 'safe_http_get') as download:
                metadata.enrich_metadata(self.payload)
                download.assert_not_called()
            self.assertEqual(cover.read_bytes(), b'original cover')
            self.assertEqual(library.get_comic_description('Book'), 'Existing description')
        finally:
            with app.app_context():
                from mangadock.models import ComicIdentity
                row = ComicIdentity.query.filter_by(comic_name='Book').first()
                row.description = None
                db.session.commit()

    def test_unmatched_results_do_not_download_first_candidate(self):
        with patch.object(metadata, 'search_metadata', return_value=[{**self.media, 'title': {'native': 'Other'}}]), \
                patch.object(metadata, 'safe_http_get') as download:
            metadata.enrich_metadata(self.payload)
            download.assert_not_called()
        self.assertEqual(metadata.metadata_view('Book')['status'], 'unmatched')

    def test_source_change_during_request_prevents_writing(self):
        def search(name):
            self.config['root'] = '/other'
            return [self.media]
        with patch.object(metadata, 'search_metadata', side_effect=search), \
                patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            metadata.enrich_metadata(self.payload)
        self.assertFalse(os.path.isfile(os.path.join(metadata.COVER_ROOT, 'Book.jpg')))
        self.assertFalse(os.path.isfile(os.path.join(self.comic, metadata.METADATA_FILENAME)))

    def test_duplicate_queue_and_separate_claim_lane(self):
        first = metadata.queue_metadata('Book')
        self.assertEqual(first, metadata.queue_metadata('Book'))
        self.assertIsNone(updates.claim_next_background_command())
        self.assertEqual(updates.claim_next_background_command('webdav_metadata'), first)

    def test_cover_download_failure_keeps_description_and_does_not_auto_retry(self):
        with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
                patch.object(metadata, 'safe_http_get', side_effect=OSError('cover unavailable')):
            metadata.enrich_metadata(self.payload)
        value = metadata.metadata_view('Book')
        self.assertEqual(value['status'], 'partial')
        self.assertTrue(library.get_comic_description('Book'))
        self.assertIsNone(metadata.queue_metadata('Book'))
        self.assertEqual(metadata.metadata_progress()['partial'], 1)

    def test_cover_conversion_exception_keeps_description(self):
        with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
                patch.object(metadata, 'safe_http_get', return_value=self.image_response()), \
                patch.object(metadata, 'normalize_cover_bytes', side_effect=OSError('conversion failed')):
            metadata.enrich_metadata(self.payload)
        self.assertEqual(metadata.metadata_view('Book')['status'], 'partial')
        self.assertTrue(library.get_comic_description('Book'))

    def test_invalid_cover_keeps_description(self):
        from unittest.mock import Mock
        with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
                patch.object(metadata, 'safe_http_get', return_value=Mock(content=b'not an image')):
            metadata.enrich_metadata(self.payload)
        self.assertEqual(metadata.metadata_view('Book')['status'], 'partial')
        self.assertTrue(library.get_comic_description('Book'))

    def test_incomplete_legacy_results_requeue_with_new_provider(self):
        for status in ('completed', 'unmatched', 'error', 'partial'):
            with self.subTest(status=status):
                self.clean_commands()
                webdav._atomic_json(os.path.join(self.comic, metadata.METADATA_FILENAME),
                    {'source': self.source, 'status': status, 'checked_at': time.time()})
                self.assertIsNotNone(metadata.queue_metadata('Book'))

    def test_retry_cooldown_and_manual_retry(self):
        for status in ('unmatched', 'error', 'partial'):
            with self.subTest(status=status):
                self.clean_commands()
                webdav._atomic_json(os.path.join(self.comic, metadata.METADATA_FILENAME),
                    {'source': self.source, 'status': status, 'metadata_version': metadata.METADATA_VERSION,
                     'checked_at': time.time()})
                self.assertIsNone(metadata.queue_metadata('Book'))
                self.assertIsNotNone(metadata.queue_metadata('Book', force=True))
        self.clean_commands()
        webdav._atomic_json(os.path.join(self.comic, metadata.METADATA_FILENAME),
            {'source': self.source, 'status': 'unmatched', 'metadata_version': metadata.METADATA_VERSION,
             'checked_at': time.time() - 8 * 86400})
        self.assertIsNotNone(metadata.queue_metadata('Book'))

    def test_completed_metadata_requeues_after_cover_is_removed(self):
        with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
             patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            metadata.enrich_metadata(self.payload)
        self.assertIsNone(metadata.queue_metadata('Book'))
        os.remove(os.path.join(metadata.COVER_ROOT, 'Book.jpg'))
        self.assertIsNotNone(metadata.queue_metadata('Book'))

    def test_reselecting_a_queued_choice_updates_selection(self):
        first = metadata.queue_metadata('Book', media_id=123, force=True)
        metadata.queue_metadata('Book', media_id=456, force=True)
        self.assertEqual(metadata.queue_metadata('Book', media_id=123, force=True), first)
        self.assertEqual(metadata.metadata_view('Book')['selected_id'], 123)

    def test_manual_choice_uses_id_and_leaves_progress_unlinked(self):
        command_id = metadata.queue_metadata('Book', media_id=123, force=True)
        with patch.object(metadata, '_graphql', return_value={'Media': self.media}) as query, \
                patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            updates.execute_background_command(command_id)
            self.assertEqual(query.call_args.args[1], {'id': 123})
        self.assertEqual(metadata.metadata_view('Book')['media_id'], 123)
        with app.app_context():
            row = db.session.get(BackgroundCommand, command_id)
            self.assertEqual(row.status, 'completed')

    def test_upstream_error_finishes_command_and_records_retryable_state(self):
        command_id = metadata.queue_metadata('Book')
        with patch.object(metadata, 'search_metadata', side_effect=ValueError('upstream failed')):
            self.assertFalse(updates.execute_background_command(command_id))
        self.assertEqual(metadata.metadata_view('Book')['status'], 'error')
        with app.app_context():
            self.assertEqual(db.session.get(BackgroundCommand, command_id).status, 'error')

    def test_metadata_selection_does_not_require_anilist_account(self):
        with app.app_context():
            admin = User.query.filter_by(username='admin').first()
            admin_id = admin.id
            session_version = admin.session_version
        with app.test_client() as client, patch.dict(app.config, {'WTF_CSRF_ENABLED': False}), \
                patch('mangadock.blueprints.web.anilist.user_can_access_progress_key', return_value=True), \
                patch('mangadock.blueprints.web.anilist.list_local_chapters', return_value=[]), \
                patch('mangadock.blueprints.web.anilist.mangaupdates_metadata.search_metadata', return_value=[self.secondary_media()]) as manga_search, \
                patch('mangadock.blueprints.web.anilist.search_metadata', return_value=[self.media]) as search:
            with client.session_transaction() as session:
                session['user_id'] = admin_id
                session['session_version'] = session_version
            response = client.get('/anilist/match/Book?metadata=1')
            self.assertEqual(response.status_code, 200)
            search.assert_not_called()
            manga_search.assert_not_called()
            self.assertIn(b'<option value="mangaupdates" selected>', response.data)
            self.assertNotIn('暂时没有找到作品'.encode(), response.data)
            response = client.get('/anilist/match/Book?metadata=1&q=Book')
            self.assertEqual(response.status_code, 200)
            manga_search.assert_called_once_with('Book')
            self.assertIn('使用这部作品的资料'.encode(), response.data)
            response = client.get('/anilist/match/Book?metadata=1&provider=anilist&q=Book')
            self.assertEqual(response.status_code, 200)
            search.assert_called_once_with('Book')
            response = client.post('/anilist/metadata/Book', data={'media_id': '123'})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(metadata.metadata_view('Book')['selected_id'], 123)

    def test_local_comic_metadata_is_selectable_without_anilist_account(self):
        local = os.path.join(self.root, 'local', 'Book')
        os.makedirs(local)
        with patch.object(library, 'get_comic_directory', return_value=local):
            target = metadata._target('Book')
            self.assertTrue(target[1].startswith('local:'))
            command_id = metadata.queue_metadata('Book', media_id=123, force=True)
            with patch.object(metadata, '_graphql', return_value={'Media': self.media}), \
                    patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
                self.assertTrue(updates.execute_background_command(command_id))
            self.assertEqual(metadata.metadata_view('Book')['media_id'], 123)
            self.assertTrue(os.path.isfile(os.path.join(local, metadata.METADATA_FILENAME)))

    def test_rate_limit_cooldown_is_shared_and_429_is_preserved(self):
        import requests
        from mangadock.services import anilist
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '65'
        error = requests.HTTPError(response=response)
        with patch.object(anilist, 'graphql', side_effect=error), patch.object(metadata.time, 'sleep'):
            with self.assertRaises(requests.HTTPError):
                metadata._graphql('query', {})
        stamp = Path(self.root, 'anilist-metadata-rate.json')
        self.assertGreater(json.loads(stamp.read_text()), time.time() + 60)

    def secondary_media(self):
        return {**self.media, 'id': 456, 'provider': 'mangaupdates',
                'description': 'Secondary synopsis', 'coverImage': {'large': 'https://cdn.mangaupdates.com/cover.jpg'},
                'url': 'https://www.mangaupdates.com/series/example/book'}

    def test_no_anilist_match_falls_back_to_secondary_without_progress_link(self):
        with app.app_context():
            before = AniListComicLink.query.count()
        with patch.object(metadata, 'search_metadata', return_value=[]), \
             patch.object(secondary, 'search_metadata', return_value=[self.secondary_media()]), \
             patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            metadata.enrich_metadata(self.payload)
        value = metadata.metadata_view('Book')
        self.assertEqual(value['status'], 'completed')
        self.assertEqual(value['mangaupdates_id'], 456)
        self.assertNotIn('media_id', value)
        self.assertEqual(value['description_provider'], 'mangaupdates')
        self.assertEqual(value['cover_provider'], 'mangaupdates')
        with app.app_context():
            self.assertEqual(AniListComicLink.query.count(), before)

    def test_secondary_fills_missing_cover_without_replacing_anilist_description(self):
        media = {**self.media, 'coverImage': {}}
        with patch.object(metadata, 'search_metadata', return_value=[media]), \
             patch.object(secondary, 'search_metadata', return_value=[self.secondary_media()]), \
             patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            metadata.enrich_metadata(self.payload)
        value = metadata.metadata_view('Book')
        self.assertEqual(value['description_provider'], 'anilist')
        self.assertEqual(value['cover_provider'], 'mangaupdates')
        self.assertEqual(value['status'], 'completed')

    def test_secondary_fills_missing_description_without_replacing_anilist_cover(self):
        media = {**self.media, 'description': ''}
        with patch.object(metadata, 'search_metadata', return_value=[media]), \
             patch.object(secondary, 'search_metadata', return_value=[self.secondary_media()]), \
             patch.object(metadata, 'safe_http_get', return_value=self.image_response()) as download:
            metadata.enrich_metadata(self.payload)
        self.assertEqual(download.call_count, 1)
        value = metadata.metadata_view('Book')
        self.assertEqual(value['description_provider'], 'mangaupdates')
        self.assertEqual(value['cover_provider'], 'anilist')

    def test_invalid_primary_image_uses_secondary_cover(self):
        from unittest.mock import Mock
        with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
             patch.object(secondary, 'search_metadata', return_value=[self.secondary_media()]), \
             patch.object(metadata, 'safe_http_get', side_effect=[Mock(content=b'bad image'), self.image_response()]):
            metadata.enrich_metadata(self.payload)
        self.assertEqual(metadata.metadata_view('Book')['cover_provider'], 'mangaupdates')

    def test_primary_query_failure_can_still_complete_from_secondary(self):
        with patch.object(metadata, 'search_metadata', side_effect=OSError('upstream')), \
             patch.object(secondary, 'search_metadata', return_value=[self.secondary_media()]), \
             patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            metadata.enrich_metadata(self.payload)
        self.assertEqual(metadata.metadata_view('Book')['status'], 'completed')

    def test_manual_secondary_selection_uses_secondary_id_only(self):
        command_id = metadata.queue_metadata('Book', media_id=456, provider='mangaupdates', force=True)
        with patch.object(secondary, 'get_metadata', return_value=self.secondary_media()) as fetch, \
             patch.object(metadata, '_graphql', side_effect=AssertionError('AniList called for secondary ID')), \
             patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            self.assertTrue(updates.execute_background_command(command_id))
        fetch.assert_called_once_with(456)
        value = metadata.metadata_view('Book')
        self.assertEqual(value['mangaupdates_id'], 456)
        self.assertNotIn('media_id', value)

    def test_secondary_manual_search_and_choice_route(self):
        with app.app_context():
            admin_id = User.query.filter_by(username='admin').first().id
        with app.test_client() as client, patch.dict(app.config, {'WTF_CSRF_ENABLED': False}), \
             patch('mangadock.blueprints.web.anilist.user_can_access_progress_key', return_value=True), \
             patch('mangadock.blueprints.web.anilist.list_local_chapters', return_value=[]), \
             patch.object(secondary, 'search_metadata', return_value=[self.secondary_media()]):
            with client.session_transaction() as session:
                session['user_id'] = admin_id
            response = client.get('/anilist/match/Book?metadata=1&provider=mangaupdates&q=Book')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'name="provider" value="mangaupdates"', response.data)
            self.assertIn(b'Secondary synopsis', response.data)
            self.assertIn('https://cdn.mangaupdates.com', response.headers['Content-Security-Policy'])
            response = client.post('/anilist/metadata/Book', data={'media_id': '456', 'provider': 'mangaupdates'})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(metadata.metadata_view('Book')['selected_provider'], 'mangaupdates')

    def test_existing_local_library_with_missing_fields_is_queued(self):
        local_root = os.path.join(self.root, 'local')
        local = os.path.join(local_root, 'Book')
        os.makedirs(local)
        with patch.object(library, 'get_comic_scan_roots', return_value=[local_root]), \
             patch.object(library, 'get_comic_directory', return_value=local), \
             patch.object(metadata, 'queue_metadata') as queue:
            metadata.queue_existing_metadata()
            queue.assert_called_once_with('Book')

    def test_reselected_work_does_not_keep_old_generated_description(self):
        webdav._atomic_json(os.path.join(self.comic, metadata.METADATA_FILENAME),
            {'source': self.source, 'media_id': 123, 'description': 'Old work synopsis', 'provider': 'anilist'})
        payload = {**self.payload, 'media_id': 456, 'provider': 'mangaupdates'}
        metadata.queue_metadata('Book', media_id=456, provider='mangaupdates', force=True)
        with patch.object(secondary, 'get_metadata', return_value={**self.secondary_media(), 'description': ''}), \
             patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            metadata.enrich_metadata(payload)
        value = metadata.metadata_view('Book')
        self.assertFalse(value.get('description'))
        self.assertEqual(value['status'], 'partial')

    def test_same_numeric_id_in_other_provider_makes_old_task_stale(self):
        command_id = metadata.queue_metadata('Book', media_id=123, force=True)
        metadata.queue_metadata('Book', media_id=123, provider='mangaupdates', force=True)
        with patch.object(metadata, '_graphql', side_effect=AssertionError('stale request')):
            self.assertTrue(updates.execute_background_command(command_id))
        self.assertEqual(metadata.metadata_view('Book')['selected_provider'], 'mangaupdates')

    def test_settings_pages_keep_forms_on_admin_subpages(self):
        from types import SimpleNamespace
        with app.app_context():
            admin_id = User.query.filter_by(username='admin').first().id
        with app.test_client() as client:
            with client.session_transaction() as session:
                session['user_id'] = admin_id
            response = client.get('/settings')
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b'name="scan_path"', response.data)
            self.assertNotIn(b'name="cache_gb"', response.data)
            self.assertIn(b'/settings/webdav', response.data)
            self.assertIn(b'/settings/scan_paths', response.data)
            self.assertIn(b'name="cache_gb"', client.get('/settings/webdav').data)
            self.assertIn(b'name="scan_path"', client.get('/settings/scan_paths').data)
            user = SimpleNamespace(id=admin_id, role='user', is_admin=False, session_version=1)
            with patch('mangadock.auth.db.session.get', return_value=user):
                for route in ('/settings/webdav', '/settings/scan_paths'):
                    response = client.get(route)
                    self.assertEqual(response.status_code, 302)
                    self.assertIn('/comics', response.location)

    def test_cleanup_keeps_user_replaced_cover(self):
        with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
                patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            metadata.enrich_metadata(self.payload)
        cover = Path(metadata.COVER_ROOT, 'Book.jpg')
        cover.write_bytes(b'manual cover')
        metadata.clear_metadata(self.comic)
        self.assertEqual(cover.read_bytes(), b'manual cover')
        self.assertEqual(metadata.metadata_view('Book'), {})
