# -*- coding: utf-8 -*-
"""共享小工具：无副作用、不依赖 download.py（避免循环导入）。"""
from bs4 import BeautifulSoup

from mangadock.services.library import normalize_comic_description


def safe_print(message, end="\n", flush=False):
    print(message, end=end, flush=flush)


def extract_description_from_html(html_content, soup=None):
    """从详情页 HTML 抽取作品简介（多站点通用）。"""
    if soup is None and html_content:
        soup = BeautifulSoup(html_content, "html.parser")
    if soup is None:
        return ''

    candidates = []

    for prop in ('og:description', 'description', 'twitter:description'):
        if prop.startswith('og:') or prop.startswith('twitter:'):
            node = soup.find('meta', attrs={'property': prop}) or soup.find('meta', property=prop)
        else:
            node = soup.find('meta', attrs={'name': prop})
        if node and node.get('content'):
            candidates.append(node.get('content'))

    for selector in (
        '.comics-detail__desc',
        '.comics-detail__info',
        '.comic-detail__desc',
        '.manga-detail__desc',
        '.book-desc',
        '.intro',
        '#intro',
        '.description',
        '.summary',
        '[class*="detail__desc"]',
        '[class*="comics-detail__desc"]',
        'div.desc',
        'p.desc',
    ):
        try:
            nodes = soup.select(selector)
        except Exception:
            nodes = []
        for node in nodes[:3]:
            text = node.get_text(' ', strip=True)
            if text:
                candidates.append(text)

    # 优先选信息量更足的候选
    best = ''
    for raw in candidates:
        cleaned = normalize_comic_description(raw)
        if len(cleaned) > len(best):
            best = cleaned
    return best
