"""Baozimh page layout and browser challenge regressions."""
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
