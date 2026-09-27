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
from mangadock.services import library, updates, webdav, webdav_metadata as metadata


class WebDavMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.config = {'url': 'http://localhost/dav', 'root': '', 'username': 'test', 'password': 'test'}
        self.patches = [patch.object(app, 'instance_path', self.root),
                        patch.object(webdav, '_read_config', side_effect=lambda: dict(self.config)),
                        patch.object(library, 'get_comic_scan_roots', return_value=[os.path.join(self.root, 'webdav_comics')]),
                        patch.object(metadata, 'COVER_ROOT', os.path.join(self.root, 'covers'))]
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
        self.assertIsNone(metadata.exact_match('Book 2', [self.media]))
        self.assertIsNone(metadata.exact_match('Book', [self.media, {**self.media, 'id': 456}]))

    def test_auto_enrichment_saves_jpeg_and_plain_description_without_progress_link(self):
        with app.app_context():
            before = AniListComicLink.query.count()
        with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
                patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            metadata.enrich_metadata(self.payload)
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
            admin_id = User.query.filter_by(username='admin').first().id
        with app.test_client() as client, patch.dict(app.config, {'WTF_CSRF_ENABLED': False}), \
                patch('mangadock.blueprints.web.anilist.user_can_access_progress_key', return_value=True), \
                patch('mangadock.blueprints.web.anilist.list_local_chapters', return_value=[]), \
                patch('mangadock.blueprints.web.anilist.search_metadata', return_value=[self.media]):
            with client.session_transaction() as session:
                session['user_id'] = admin_id
            response = client.get('/anilist/match/Book?metadata=1')
            self.assertEqual(response.status_code, 200)
            self.assertIn('使用此作品的封面与简介'.encode(), response.data)
            response = client.post('/anilist/metadata/Book', data={'media_id': '123'})
            self.assertEqual(response.status_code, 302)
        self.assertEqual(metadata.metadata_view('Book')['selected_id'], 123)

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

    def test_cleanup_keeps_user_replaced_cover(self):
        with patch.object(metadata, 'search_metadata', return_value=[self.media]), \
                patch.object(metadata, 'safe_http_get', return_value=self.image_response()):
            metadata.enrich_metadata(self.payload)
        cover = Path(metadata.COVER_ROOT, 'Book.jpg')
        cover.write_bytes(b'manual cover')
        metadata.clear_metadata(self.comic)
        self.assertEqual(cover.read_bytes(), b'manual cover')
        self.assertEqual(metadata.metadata_view('Book'), {})
