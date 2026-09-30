"""Baozimh page layout and browser challenge regressions."""
import unittest
from unittest.mock import MagicMock, patch

from mangadock.services import providers
from mangadock.services.providers import baozimhcn


URL = 'https://www.baozimh.com/comic/santi-huanchuangweilai'


class BaozimhCnTests(unittest.TestCase):
    def test_current_chapter_links_are_sorted_and_include_last_slot(self):
        html = """
        <title>三體 - 包子漫畫</title>
        <h1 class="comics-detail__title">三體</h1>
        <meta property="og:image" content="https://static-tw.baozimh.com/cover/santi.jpg">
        <a href="/user/page_direct?comic_id=santi-huanchuangweilai&amp;section_slot=0&amp;chapter_slot=335">最新章</a>
        <a href="/user/page_direct?comic_id=santi-huanchuangweilai&amp;section_slot=0&amp;chapter_slot=0">第一章</a>
        <a href="/user/page_direct?comic_id=santi-huanchuangweilai&amp;section_slot=0&amp;chapter_slot=335">最新章</a>
        <a href="/user/page_direct?comic_id=other&amp;section_slot=0&amp;chapter_slot=9">其他漫画</a>
        """
        with patch.object(baozimhcn, '_fetch_page', return_value=html):
            source = baozimhcn.load_baozimhcn_source(URL)
        self.assertEqual(source['title'], '三體')
        self.assertEqual(len(source['chapters']), 2)
        self.assertEqual(source['chapters'][0]['chapter_url'],
                         'https://www.baozimh.com/comic/chapter/santi-huanchuangweilai/0_0.html')
        self.assertEqual(source['chapters'][-1]['chapter_url'],
                         'https://www.baozimh.com/comic/chapter/santi-huanchuangweilai/0_335.html')
        self.assertEqual(source['chapters'][-1]['title'], '最新章')
        self.assertEqual(providers.resolve_download_chapter('baozimhcn'),
                         baozimhcn.download_baozimhcn_chapter)

    def test_challenge_config_and_work_are_compatible(self):
        html = '<script>g.start({challengeId:"test",ticket:"ticket",verifyUrl:"/__gatekeeper_challenge/verify",difficultyBits:8});</script>'
        config = baozimhcn._challenge_config(html)
        nonce = baozimhcn._solve_challenge(config['challengeId'], config['difficultyBits'])
        import hashlib
        digest = hashlib.sha256(f'gatekeeper-pow-v1:test:{nonce}'.encode()).digest()
        self.assertEqual(digest[0], 0)

    def test_challenge_uses_final_host_after_legacy_redirect(self):
        legacy_url = 'https://cn.baozimhcn.com/comic/santi-huanchuangweilai'
        current_url = 'https://www.baozimh.com/comic/santi-huanchuangweilai'
        challenge_path = '/__gatekeeper_challenge/start?challenge=one'

        def response(status, url, body='', payload=None):
            value = MagicMock()
            value.__enter__.return_value = value
            value.status_code = status
            value.url = url
            value.text = body
            value.json.return_value = payload
            return value

        blocked = response(403, current_url, payload={'challenge_url': challenge_path})
        challenge = response(200, 'https://www.baozimh.com' + challenge_path,
                             '<script>g.start({challengeId:"id",ticket:"ticket",'
                             'verifyUrl:"/__gatekeeper_challenge/verify",difficultyBits:8});</script>')
        passed = response(200, current_url, body='<title>漫画</title>')
        verified = response(200, 'https://www.baozimh.com/__gatekeeper_challenge/verify',
                            payload={'status': 'passed'})
        with patch.object(baozimhcn, 'safe_http_get', side_effect=[blocked, challenge, passed]) as get, \
             patch.object(baozimhcn, 'safe_http_post', return_value=verified) as post, \
             patch.object(baozimhcn, 'validate_safe_upstream_url'), \
             patch.object(baozimhcn, '_solve_challenge', return_value='42'):
            self.assertEqual(baozimhcn._fetch_page(legacy_url, MagicMock()), '<title>漫画</title>')
        self.assertEqual(get.call_args_list[1].args[0], 'https://www.baozimh.com' + challenge_path)
        self.assertEqual(get.call_args_list[2].args[0], current_url)
        self.assertEqual(post.call_args.args[0], 'https://www.baozimh.com/__gatekeeper_challenge/verify')
        self.assertEqual(post.call_args.kwargs['headers']['Origin'], 'https://www.baozimh.com')


if __name__ == '__main__':
    unittest.main()
