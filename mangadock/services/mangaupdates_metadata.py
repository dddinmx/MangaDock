"""Public MangaUpdates metadata, independent of reading progress."""
import fcntl
import json
import os
import time
from urllib.parse import urlparse

import requests

from mangadock.core import app
from mangadock.services import webdav

API_ROOT = 'https://api.mangaupdates.com/v1'


def _request(method, path, **kwargs):
    identity = os.path.join(app.instance_path, 'mangaupdates-metadata-rate')
    with webdav.open_lock_file(identity) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        stamp = identity + '.json'
        try:
            with open(stamp) as handle:
                next_request = float(json.load(handle))
        except (OSError, ValueError, TypeError):
            next_request = 0
        time.sleep(max(0, min(120, next_request - time.time())))
        response = requests.request(method, API_ROOT + path, headers={'User-Agent': 'MangaDock/metadata'},
                                    timeout=(10, 20), allow_redirects=False, **kwargs)
        try:
            delay = 1
            if response.status_code == 429:
                try:
                    delay = max(60, min(120, float(response.headers.get('Retry-After', 60))))
                except (TypeError, ValueError):
                    delay = 60
            webdav._atomic_json(stamp, time.time() + delay)
            response.raise_for_status()
            return response.json()
        finally:
            response.close()


def _media(record, hit_title=''):
    from mangadock.services.webdav_metadata import plain_description
    series_id = int(record['series_id'])
    if series_id <= 0:
        raise ValueError('无效的 MangaUpdates 条目')
    image = (record.get('image') or {}).get('url') or {}
    url = record.get('url') or ''
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('www.mangaupdates.com', 'mangaupdates.com'):
        url = f'https://www.mangaupdates.com/series.html?id={series_id}'
    return {
        'id': series_id, 'provider': 'mangaupdates',
        'title': {'userPreferred': record.get('title') or '', 'native': record.get('title') or ''},
        'synonyms': [hit_title] + [item.get('title') or '' for item in record.get('associated') or []],
        'description': plain_description(record.get('description')),
        'coverImage': {'large': image.get('original') or image.get('thumb') or ''},
        'url': url,
    }


def search_metadata(title):
    data = _request('POST', '/series/search', json={'search': title[:100], 'perpage': 10})
    return [_media(item['record'], item.get('hit_title') or '') for item in data.get('results') or []]


def get_metadata(series_id):
    series_id = int(series_id)
    if series_id <= 0:
        raise ValueError('无效的 MangaUpdates 条目')
    media = _media(_request('GET', f'/series/{series_id}'))
    if media['id'] != series_id:
        raise ValueError('MangaUpdates 返回的条目不一致')
    return media


def match_metadata(title):
    from mangadock.services.webdav_metadata import exact_match
    candidates = search_metadata(title)
    match = exact_match(title, candidates)
    if match:
        return match
    # Search exposes only its best hit title; details contain the other translations.
    detailed = [get_metadata(item['id']) for item in candidates[:5]]
    return exact_match(title, detailed)
