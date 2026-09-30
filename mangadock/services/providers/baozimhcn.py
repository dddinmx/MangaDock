# -*- coding: utf-8 -*-
"""包子漫画 baozimh.com / cn.baozimhcn.com 作品与章节解析。"""
import hashlib
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from mangadock.services.providers.common import extract_description_from_html
from mangadock.utils.http import safe_http_get, validate_safe_upstream_url
from mangadock.utils.media import default_headers, sanitize_filename


def _challenge_config(html):
    match = re.search(r'\bg\.start\((\{[^;]*?\})\)', html)
    if not match:
        raise ValueError('包子漫画浏览器验证页面格式已变化')
    fields = dict(re.findall(r'(\w+):"([^"]*)"', match.group(1)))
    bits = re.search(r'difficultyBits:(\d+)', match.group(1))
    if not bits or not all(fields.get(key) for key in ('challengeId', 'ticket', 'verifyUrl')):
        raise ValueError('包子漫画浏览器验证参数不完整')
    fields['difficultyBits'] = int(bits.group(1))
    return fields


def _solve_challenge(challenge_id, bits):
    if not 0 < bits <= 22:
        raise ValueError('包子漫画浏览器验证难度超出支持范围')
    prefix = f'gatekeeper-pow-v1:{challenge_id}:'.encode()
    limit = 1 << (256 - bits)
    deadline = time.monotonic() + 110
    for nonce in range(1 << 26):
        if int.from_bytes(hashlib.sha256(prefix + str(nonce).encode()).digest(), 'big') < limit:
            return str(nonce)
        if nonce % 8192 == 0 and time.monotonic() >= deadline:
            break
    raise ValueError('包子漫画浏览器验证计算超时')


def _fetch_page(url, session):
    headers = default_headers()
    with safe_http_get(url, headers=headers, timeout=30, session_obj=session) as response:
        if response.status_code != 403:
            response.raise_for_status()
            return response.text
        try:
            challenge_path = response.json().get('challenge_url', '')
        except ValueError:
            challenge_path = ''
        if not challenge_path.startswith('/__gatekeeper_challenge/start?'):
            response.raise_for_status()
    if challenge_path:
        challenge_url = urljoin(url, challenge_path)
        validate_safe_upstream_url(challenge_url)
        with safe_http_get(challenge_url, headers=headers, timeout=30, session_obj=session) as challenge:
            challenge.raise_for_status()
            config = _challenge_config(challenge.text)
        if config['verifyUrl'] != '/__gatekeeper_challenge/verify':
            raise ValueError('包子漫画浏览器验证地址无效')
        verify_url = urljoin(url, config['verifyUrl'])
        validate_safe_upstream_url(verify_url)
        nonce = _solve_challenge(config['challengeId'], config['difficultyBits'])
        origin = f'{urlparse(url).scheme}://{urlparse(url).netloc}'
        with session.post(
            verify_url,
            json={'challenge_id': config['challengeId'], 'ticket': config['ticket'], 'nonce': nonce},
            headers={**headers, 'Origin': origin, 'Referer': challenge_url,
                     'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'},
            timeout=15, allow_redirects=False,
        ) as verified:
            if verified.status_code != 200 or verified.json().get('status') != 'passed':
                raise ValueError('包子漫画浏览器验证失败')
    with safe_http_get(url, headers=headers, timeout=30, session_obj=session) as response:
        response.raise_for_status()
        return response.text


def load_baozimhcn_source(url):
    session = requests.Session()
    html_content = _fetch_page(url, session)
    soup = BeautifulSoup(html_content, 'html.parser')
    title_tag = soup.find('h1', class_='comics-detail__title') or soup.find('title')
    title = sanitize_filename(title_tag.get_text(strip=True) if title_tag else '未知漫画')

    cover = soup.find('meta', attrs={'property': 'og:image'}) or soup.find('meta', attrs={'name': 'og:image'})
    comic_id = urlparse(url).path.rstrip('/').split('/')[-1]
    chapter_slots = {}
    for anchor in soup.find_all('a', href=True):
        parsed = urlparse(anchor['href'])
        query = parse_qs(parsed.query)
        if parsed.path != '/user/page_direct' or query.get('comic_id') != [comic_id]:
            continue
        try:
            if query.get('section_slot') != ['0']:
                continue
            slot = int(query['chapter_slot'][0])
        except (KeyError, IndexError, ValueError):
            continue
        if slot >= 0:
            chapter_slots[slot] = anchor.get_text(' ', strip=True) or f'第{slot + 1}章'

    if not chapter_slots:
        for slot in re.findall(r'/comic/chapter/[^/]+/0_(\d+)\.html', html_content):
            chapter_slots[int(slot)] = f'第{int(slot) + 1}章'
    if not chapter_slots:
        raise ValueError('未获取到有效章节数')

    base_url = f'{urlparse(url).scheme}://{urlparse(url).netloc}/comic/chapter/{comic_id}/0_{{}}.html'
    chapters = [
        {'order': index, 'title': chapter_slots[slot], 'filename_base': f'{index:02d}',
         'chapter_url': base_url.format(slot)}
        for index, slot in enumerate(sorted(chapter_slots), 1)
    ]
    return {
        'provider': 'baozimhcn',
        'title': title,
        'cover_url': cover.get('content') if cover else None,
        'cover_verify': True,
        'description': extract_description_from_html(html_content, soup=soup),
        'chapters': chapters,
        '_session': session,
    }


def download_baozimhcn_chapter(source, chapter, folder, comic_format, task_id):
    session = source.get('_session') or requests.Session()
    try:
        html = _fetch_page(chapter['chapter_url'], session)
    except Exception as exc:
        return False, f"章节 {chapter['order']} 页面访问失败：{exc}"
    from mangadock.services.download import crawl_chapter
    return crawl_chapter(chapter['chapter_url'], folder, chapter['order'], comic_format,
                         task_id, response_text=html)
