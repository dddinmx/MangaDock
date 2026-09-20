# -*- coding: utf-8 -*-
"""包子漫画 cn.baozimhcn.com / baozimh.com provider：源解析。

单章下载走 download.py 的通用 crawl_chapter 兜底（scomic 图片规则）。
"""
import re

from bs4 import BeautifulSoup

from mangadock.services.providers.common import extract_description_from_html
from mangadock.utils.http import safe_http_get
from mangadock.utils.media import default_headers, sanitize_filename


def load_baozimhcn_source(url):
    response = safe_http_get(url, headers=default_headers(), timeout=30)
    response.raise_for_status()
    response.encoding = 'utf-8'
    html_content = response.text

    soup = BeautifulSoup(html_content, "html.parser")
    title_tag = soup.find("h1", class_="comics-detail__title") or soup.find("title")
    title = sanitize_filename(title_tag.get_text(strip=True) if title_tag else "未知漫画")

    cover_match = re.search(
        r'<meta data-n-head="ssr" data-hid="og:image" name="og:image" content="(https?://[^"]+)"',
        html_content
    )

    chapter_max = 0
    chapter_slot = re.search(r'chapter_slot=(\d+)', html_content)
    if chapter_slot:
        chapter_max = int(chapter_slot.group(1))
    if chapter_max == 0:
        chapter_match = re.search(r'共(\d+)话', html_content)
        if chapter_match:
            chapter_max = int(chapter_match.group(1))
    if chapter_max == 0:
        chapter_links = re.findall(r'/comic/chapter/[^/]+/0_(\d+)\.html', html_content)
        if chapter_links:
            chapter_max = max(map(int, chapter_links)) + 1
    if chapter_max <= 0:
        raise ValueError("未获取到有效章节数")

    base_chapter_url = url.rstrip('/').replace("/comic/", "/comic/chapter/") + "/0_{}.html"
    chapters = [
        {
            'order': index + 1,
            'title': f"第{index + 1}章",
            'filename_base': f"{index + 1:02d}",
            'chapter_url': base_chapter_url.format(index)
        }
        for index in range(chapter_max)
    ]

    return {
        'provider': 'baozimhcn',
        'title': title,
        'cover_url': cover_match.group(1) if cover_match else None,
        'cover_verify': True,
        'description': extract_description_from_html(html_content, soup=soup),
        'chapters': chapters
    }
