"""卡拉漫画 page layout, playlist pick, and omo_token gate."""
import unittest
from unittest.mock import MagicMock, patch

from mangadock.services import providers, tasks
from mangadock.services.providers import kalamanhua


URL = 'https://www.kalamanhua.com/comic/120428.html'

COMIC_HTML = """
<title>蛟龙的伴侣漫画免费在线免费下拉观看_连载中,第2话_卡拉漫画</title>
<div class="stui-content">
  <div class="stui-content__thumb">
    <a class="pic" title="蛟龙的伴侣">
      <img class="lazyload" src="/template/kalamh/statics/img/load.gif"
           data-original="https://image.kalaimg.top/image/cover/abc123">
    </a>
  </div>
  <div class="stui-content__detail">
    <h1 class="title">蛟龙的伴侣</h1>
    <div class="detail-content">悠莱总以没有合心意的男人为借口。</div>
  </div>
  <div id="playlist1" class="tab-pane">
    <ul class="stui-content__playlist">
      <li><a href="/chapter/120428-1-1.html">第1话</a></li>
      <li><a href="/chapter/120428-1-2.html">第2话</a></li>
      <li><a href="/chapter/120428-1-3.html">第3话</a></li>
    </ul>
  </div>
  <div id="playlist2" class="tab-pane">
    <ul class="stui-content__playlist">
      <li><a href="/chapter/120428-2-1.html">第1话</a></li>
      <li><a href="/chapter/120428-2-2.html">第2话</a></li>
    </ul>
  </div>
</div>
"""

CHAPTER_HTML = """
<title>蛟龙的伴侣第1话</title>
<script type="text/javascript">var player_aaaa={"flag":"play","encrypt":3,"link":"/chapter/120428-1-1.html","url":"aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899","from":"qimmh","id":"120428","sid":1,"nid":1};</script>
"""

READER_HTML = """
<title>漫画阅读器</title>
<div class="comic-container" id="container">
  <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
       data-src="/image/index/pageonehex?cf_verify=tok_live" alt="pic" class="comic-img">
  <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
       data-src="/image/index/pagetwohex" alt="pic" class="comic-img">
</div>
"""

GATE_HTML = """
<title>加载中...</title>
<script>
    var token = 'tok_live';
    document.cookie = "omo_token=" + token + "; max-age=86400; path=/";
</script>
"""


def _response(text, status=200):
    value = MagicMock()
    value.status_code = status
    value.text = text
    value.encoding = 'utf-8'
    value.raise_for_status = MagicMock()
    return value


class KalamanhuaTests(unittest.TestCase):
    def test_entry_url_and_adult_flag(self):
        self.assertTrue(providers.is_supported_url(URL))
        self.assertTrue(providers.is_supported_url('https://kalamanhua.com/comic/120428.html'))
        self.assertFalse(providers.is_supported_url('https://www.kalamanhua.com/chapter/120428-1-1.html'))
        self.assertEqual(providers.detect_provider_name(URL), 'kalamanhua')
        self.assertTrue(providers.is_adult_url(URL))
        self.assertEqual(
            providers.resolve_download_chapter('kalamanhua'),
            kalamanhua.download_kalamanhua_chapter,
        )

    def test_canonical_url_strips_www_and_query(self):
        self.assertEqual(
            kalamanhua.canonical_comic_url('https://kalamanhua.com/comic/120428.html?from=home'),
            URL,
        )
        self.assertEqual(
            tasks.normalize_task_url('https://kalamanhua.com/comic/120428.html/'),
            URL,
        )

    def test_longest_playlist_is_used_in_reading_order(self):
        with patch.object(kalamanhua, '_fetch_html', return_value=(COMIC_HTML, None)):
            source = kalamanhua.load_kalamanhua_source(URL)
        self.assertEqual(source['title'], '蛟龙的伴侣')
        self.assertEqual(source['cover_url'], 'https://image.kalaimg.top/image/cover/abc123')
        self.assertIn('悠莱', source['description'])
        self.assertEqual(len(source['chapters']), 3)
        self.assertEqual(
            source['chapters'][0]['chapter_url'],
            'https://www.kalamanhua.com/chapter/120428-1-1.html',
        )
        self.assertEqual(
            source['chapters'][-1]['chapter_url'],
            'https://www.kalamanhua.com/chapter/120428-1-3.html',
        )
        self.assertEqual(source['chapters'][0]['sid'], 1)
        self.assertEqual(source['chapters'][-1]['nid'], 3)

    def test_omo_gate_retries_with_cookie(self):
        gate = _response(GATE_HTML, status=403)
        body = _response('<div class="stui-content"><h1>ok</h1></div>')
        with patch.object(kalamanhua, 'safe_http_get', side_effect=[gate, body]) as get:
            html, token = kalamanhua._fetch_html(URL, referer=kalamanhua.KALAMANHUA_SITE_REFERER)
        self.assertEqual(token, 'tok_live')
        self.assertIn('stui-content', html)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs['headers']['Cookie'], 'omo_token=tok_live')
        gate.raise_for_status.assert_not_called()

    def test_chapter_images_use_play_min_and_cf_verify(self):
        chapter_url = 'https://www.kalamanhua.com/chapter/120428-1-1.html'
        play_url = (
            'https://image.kalaimg.top/play/min/'
            'aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899'
        )

        def fake_fetch(url, referer=None):
            if url == chapter_url:
                return CHAPTER_HTML, None
            if url == play_url:
                return READER_HTML, 'tok_live'
            raise AssertionError(url)

        with patch.object(kalamanhua, '_fetch_html', side_effect=fake_fetch):
            images = kalamanhua.load_kalamanhua_chapter_images(chapter_url)
        self.assertEqual(len(images), 2)
        self.assertTrue(images[0].startswith('https://image.kalaimg.top/image/index/pageonehex'))
        self.assertIn('cf_verify=tok_live', images[0])
        self.assertTrue(images[1].endswith('cf_verify=tok_live'))
        self.assertIn('/image/index/pagetwohex', images[1])

    def test_player_aaaa_json_roundtrip(self):
        payload = kalamanhua._parse_player_aaaa(CHAPTER_HTML)
        self.assertEqual(payload['from'], 'qimmh')
        self.assertEqual(payload['encrypt'], 3)
        self.assertEqual(
            kalamanhua._play_min_url(payload['url']),
            'https://image.kalaimg.top/play/min/'
            'aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899',
        )


if __name__ == '__main__':
    unittest.main()
