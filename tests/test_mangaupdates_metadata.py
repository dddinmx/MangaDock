import tempfile
import unittest
from unittest.mock import Mock, patch

from mangadock.core import app
from mangadock.services import mangaupdates_metadata as provider, webdav_metadata as metadata


class MangaUpdatesMetadataTests(unittest.TestCase):
    def test_search_uses_matched_alias_and_returns_cover_and_plain_description(self):
        record = {'series_id': 123, 'title': 'English Title', 'description': '<p>A story</p>',
                  'image': {'url': {'original': 'https://cdn.mangaupdates.com/cover.jpg'}}}
        with patch.object(provider, '_request', return_value={'results': [{'record': record, 'hit_title': '中文标题'}]}) as fetch:
            candidates = provider.search_metadata('中文标题')
        self.assertEqual(metadata.exact_match('中文标题', candidates)['id'], 123)
        self.assertEqual(candidates[0]['description'], 'A story')
        self.assertEqual(candidates[0]['coverImage']['large'], 'https://cdn.mangaupdates.com/cover.jpg')
        fetch.assert_called_once_with('POST', '/series/search', json={'search': '中文标题', 'perpage': 10})

    def test_details_include_aliases_and_validate_id(self):
        record = {'series_id': 123, 'title': 'Book', 'associated': [{'title': '别名'}]}
        with patch.object(provider, '_request', return_value=record):
            self.assertIn('别名', provider.get_metadata(123)['synonyms'])
            with self.assertRaises(ValueError):
                provider.get_metadata(456)

    def test_matching_checks_other_translations_in_series_details(self):
        candidate = {'id': 123, 'title': {'native': 'Shingeki no Kyojin'}, 'synonyms': ['進撃の巨人']}
        detailed = {**candidate, 'synonyms': ['進撃の巨人', '進擊的巨人']}
        with patch.object(provider, 'search_metadata', return_value=[candidate]), \
             patch.object(provider, 'get_metadata', return_value=detailed):
            self.assertEqual(provider.match_metadata('進擊的巨人')['id'], 123)
            self.assertIsNone(provider.match_metadata('Unrelated'))

    def test_public_request_is_rate_limited_and_closes_response(self):
        response = Mock(status_code=200)
        response.json.return_value = {'results': []}
        with tempfile.TemporaryDirectory() as root, patch.object(app, 'instance_path', root), \
             patch.object(provider.requests, 'request', return_value=response) as request, \
             patch.object(provider.time, 'sleep') as sleep:
            provider.search_metadata('Book')
            provider.search_metadata('Book')
            self.assertGreater(sleep.call_args.args[0], 0)
        self.assertEqual(request.call_count, 2)
        self.assertFalse(request.call_args.kwargs['allow_redirects'])
        self.assertEqual(response.close.call_count, 2)
