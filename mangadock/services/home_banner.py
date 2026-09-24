# -*- coding: utf-8 -*-
"""Find and cache landscape artwork used only by the comic homepage hero."""
import hashlib
import json
import os
import tempfile
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from urllib.parse import urlencode

from PIL import Image

from mangadock.core import app
from mangadock.services.tasks import update_task
from mangadock.utils.http import safe_http_get, safe_http_post, write_limited_response_to_file
from mangadock.utils.media import default_headers

HOME_BANNER_DIR = os.path.join(app.static_folder, 'cover', 'home-banner')
HOME_BANNER_MAX_BYTES = 12 * 1024 * 1024
HOME_BANNER_MAX_PIXELS = 40_000_000
HOME_BANNER_MIN_WIDTH = 960
HOME_BANNER_MIN_RATIO = 1.35
HOME_BANNER_MAX_RATIO = 3.2
HOME_BANNER_UPSCALE_SHORT_EDGE = 1080
TITLE_MATCH_THRESHOLD = 0.8
# Older scans treated upstream failures as successful completion; rescan them once.
HOME_BANNER_BACKFILL_PAYLOAD = {'scan_version': 3}

_pending_names = set()
_pending_lock = threading.Lock()
_pending_futures = {}
_lookup_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='home-banner')
_upscale_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='home-banner-upscale')
_upscale_pending = set()
_upscale_lock = threading.Lock()
_backfill_lock = threading.Lock()
_backfill_thread = None


def _asset_key(comic_name):
    return hashlib.sha256((comic_name or '').strip().encode('utf-8')).hexdigest()


def _existing_banner_path(comic_name):
    key = _asset_key(comic_name)
    for extension in ('.jpg', '.png', '.webp'):
        path = os.path.join(HOME_BANNER_DIR, key + extension)
        if not os.path.isfile(path):
            continue
        try:
            with Image.open(path) as image:
                if _banner_dimensions_acceptable(image.width, image.height, image.format):
                    return path
        except Exception:
            continue
    return None


def _upscaled_banner_path(comic_name, source_path):
    source = os.stat(source_path)
    revision = f'{source.st_mtime_ns:x}-{source.st_size:x}'
    filename = f'{_asset_key(comic_name)}-{revision}.jpg'
    return os.path.join(HOME_BANNER_DIR, 'upscaled', filename)


def _upscaled_banner_ready(comic_name, source_path):
    try:
        return os.path.isfile(_upscaled_banner_path(comic_name, source_path))
    except OSError:
        return False


def _upscale_home_banner(comic_name, source_path, target_path, pending_key):
    try:
        from mangadock.utils.cover_enhance import enhance_cover_file

        result = enhance_cover_file(
            source_path,
            target_path,
            target_short_edge=HOME_BANNER_UPSCALE_SHORT_EDGE,
        )
        if not result:
            print(f'首页横幅超分失败：{comic_name}')
    except Exception as exc:
        print(f'首页横幅超分失败：{comic_name} -> {exc}')
    finally:
        with _upscale_lock:
            _upscale_pending.discard(pending_key)


def _queue_home_banner_upscale(comic_name, source_path):
    if not source_path or _upscaled_banner_ready(comic_name, source_path):
        return False

    target_path = _upscaled_banner_path(comic_name, source_path)
    key = target_path
    with _upscale_lock:
        if key in _upscale_pending:
            return False
        _upscale_pending.add(key)
        try:
            _upscale_pool.submit(
                _upscale_home_banner,
                comic_name,
                source_path,
                target_path,
                key,
            )
        except Exception:
            _upscale_pending.discard(key)
            return False
    return True


def get_home_banner_url(comic_name):
    """Return a same-origin static URL, or None to keep the existing hero."""
    source_path = _existing_banner_path(comic_name)
    if not source_path:
        return None
    path = _upscaled_banner_path(comic_name, source_path)
    if not _upscaled_banner_ready(comic_name, source_path):
        _queue_home_banner_upscale(comic_name, source_path)
        path = source_path
    relative_path = os.path.relpath(path, app.static_folder).replace(os.sep, '/')
    version = int(os.path.getmtime(path))
    return f'{app.static_url_path}/{relative_path}?v={version}'


def _banner_dimensions_acceptable(width, height, image_format):
    if (
        image_format not in {'JPEG', 'PNG', 'WEBP'}
        or width < HOME_BANNER_MIN_WIDTH
        or height < 300
        or width / max(height, 1) < HOME_BANNER_MIN_RATIO
        or width / max(height, 1) > HOME_BANNER_MAX_RATIO
        or width * height > HOME_BANNER_MAX_PIXELS
    ):
        return False
    return True


def schedule_home_banner_search(task_id, comic_name):
    """Start one best-effort lookup without delaying the download response."""
    return _queue_home_banner_search(task_id, comic_name) is not None


def _queue_home_banner_search(task_id, comic_name):
    normalized_name = (comic_name or '').strip()
    if not normalized_name or normalized_name == '未知漫画' or _existing_banner_path(normalized_name):
        return None

    with _pending_lock:
        if normalized_name in _pending_names:
            return _pending_futures.get(normalized_name)
        _pending_names.add(normalized_name)
        try:
            future = _lookup_pool.submit(_search_and_save, task_id, normalized_name)
            _pending_futures[normalized_name] = future
            return future
        except Exception:
            _pending_names.discard(normalized_name)
            _pending_futures.pop(normalized_name, None)
            raise


def _normalize_title(value):
    normalized = unicodedata.normalize('NFKC', (value or '').casefold())
    return ''.join(character for character in normalized if character.isalnum())


def _title_score(query, titles):
    normalized_query = _normalize_title(query)
    if len(normalized_query) < 2:
        return 0.0

    best = 0.0
    for title in titles:
        normalized_title = _normalize_title(str(title or ''))
        if len(normalized_title) < 2:
            continue
        score = SequenceMatcher(None, normalized_query, normalized_title).ratio()
        if normalized_query in normalized_title or normalized_title in normalized_query:
            score = max(score, min(len(normalized_query), len(normalized_title)) / max(
                len(normalized_query), len(normalized_title)
            ))
        best = max(best, score)
    return best


def _rank_candidates(comic_name, candidates):
    ranked = []
    for candidate in candidates:
        score = _title_score(comic_name, candidate.get('titles') or [])
        image_url = candidate.get('image_url')
        if image_url and score >= TITLE_MATCH_THRESHOLD:
            ranked.append({**candidate, 'score': score})
    return sorted(ranked, key=lambda item: item['score'], reverse=True)


def _search_anilist(comic_name):
    query = '''
    query ($search: String) {
      Page(page: 1, perPage: 10) {
        media(search: $search, type: MANGA) {
          title { romaji english native userPreferred }
          synonyms
          bannerImage
        }
      }
    }
    '''
    response = safe_http_post(
        'https://graphql.anilist.co',
        json_body={'query': query, 'variables': {'search': comic_name}},
        headers={'User-Agent': 'MangaDock/1.0', 'Accept': 'application/json'},
        timeout=15,
        max_bytes=2 * 1024 * 1024,
    )
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()

    media = (payload.get('data') or {}).get('Page', {}).get('media') or []
    candidates = []
    for item in media:
        titles = list((item.get('title') or {}).values())
        titles.extend(item.get('synonyms') or [])
        candidates.append({'titles': titles, 'image_url': item.get('bannerImage')})
    return _rank_candidates(comic_name, candidates)


def _search_kitsu(comic_name):
    url = 'https://kitsu.io/api/edge/manga?' + urlencode({
        'filter[text]': comic_name,
        'page[limit]': 10,
    })
    response = safe_http_get(
        url,
        headers={'User-Agent': 'MangaDock/1.0', 'Accept': 'application/vnd.api+json'},
        timeout=15,
        max_bytes=2 * 1024 * 1024,
    )
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()

    candidates = []
    for item in payload.get('data') or []:
        attributes = item.get('attributes') or {}
        cover = attributes.get('coverImage') or {}
        image_url = cover.get('original') or cover.get('large') or cover.get('medium')
        titles = list((attributes.get('titles') or {}).values())
        titles.append(attributes.get('canonicalTitle'))
        candidates.append({'titles': titles, 'image_url': image_url})
    return _rank_candidates(comic_name, candidates)


def _save_if_landscape(comic_name, image_url):
    temp_path = None
    response = None
    try:
        os.makedirs(HOME_BANNER_DIR, exist_ok=True)
        descriptor, temp_path = tempfile.mkstemp(
            prefix='.home-banner-', suffix='.tmp', dir=HOME_BANNER_DIR
        )
        os.close(descriptor)
        response = safe_http_get(
            image_url,
            headers=default_headers(),
            timeout=20,
            stream=True,
            max_bytes=HOME_BANNER_MAX_BYTES,
        )
        response.raise_for_status()
        write_limited_response_to_file(response, temp_path, HOME_BANNER_MAX_BYTES)

        with Image.open(temp_path) as image:
            width, height = image.size
            image_format = image.format
            if not _banner_dimensions_acceptable(width, height, image_format):
                return False
            image.verify()

        extension = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}[image_format]
        target_path = os.path.join(HOME_BANNER_DIR, _asset_key(comic_name) + extension)
        os.replace(temp_path, target_path)
        _queue_home_banner_upscale(comic_name, target_path)
        return True
    except Exception:
        return None
    finally:
        if response is not None:
            response.close()
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _search_and_save(task_id, comic_name):
    """Return True when saved, False for no match, None for a retryable failure."""
    sources = (
        ('AniList', _search_anilist),
        ('Kitsu', _search_kitsu),
    )
    try:
        with app.app_context():
            if _existing_banner_path(comic_name):
                return False
            had_failure = False
            for source_name, search in sources:
                try:
                    candidates = search(comic_name)
                except Exception as exc:
                    print(f'{source_name} 首页横幅检索失败：{exc}')
                    had_failure = True
                    continue
                for candidate in candidates[:5]:
                    saved = _save_if_landscape(comic_name, candidate['image_url'])
                    if saved is None:
                        had_failure = True
                    if saved:
                        if task_id:
                            try:
                                update_task(task_id, log=f'已从 {source_name} 找到横图，首页横幅已替换')
                            except Exception as exc:
                                print(f'首页横幅已保存，但任务日志更新失败：{exc}')
                        return True
        return None if had_failure else False
    except Exception as exc:
        print(f'漫画《{comic_name}》首页横幅检索失败：{exc}')
        return None
    finally:
        with _pending_lock:
            _pending_names.discard(comic_name)
            _pending_futures.pop(comic_name, None)


def start_home_banner_backfill(command_id):
    """Run the one-time library scan outside the command worker's polling loop."""
    global _backfill_thread
    with _backfill_lock:
        if _backfill_thread and _backfill_thread.is_alive():
            return False

        _backfill_thread = threading.Thread(
            target=_run_home_banner_backfill,
            args=(command_id,),
            name='home-banner-backfill',
            daemon=True,
        )
        _backfill_thread.start()
    return True


def _run_home_banner_backfill(command_id):
    from mangadock.services.updates import finish_background_command

    try:
        from mangadock.services.library import get_available_comics

        comics = get_available_comics() or []
        futures = []
        already_have_banner = 0
        for comic in comics:
            comic_name = (comic.get('comic_name') or '').strip()
            if not comic_name or comic_name == '未知漫画':
                continue
            banner_path = _existing_banner_path(comic_name)
            if banner_path:
                _queue_home_banner_upscale(comic_name, banner_path)
                already_have_banner += 1
                continue
            future = _queue_home_banner_search(None, comic_name)
            if future is not None:
                futures.append(future)

        found = 0
        failed = 0
        for future in as_completed(futures):
            result = future.result()
            found += result is True
            failed += result is None
        finish_background_command(
            command_id,
            status='error' if failed else 'completed',
            message=(
                f'已补扫 {len(futures)} 部现有漫画，找到 {found} 张横幅；'
                f'{already_have_banner} 部已有横幅；'
                f'{failed} 部检索失败' + ('，下次启动重试' if failed else '')
            ),
        )
    except Exception as exc:
        finish_background_command(command_id, status='error', message=str(exc))


def queue_initial_home_banner_backfill():
    """Queue one initial scan; completed scans are not repeated on later startups."""
    from mangadock.models import BackgroundCommand
    from mangadock.services.updates import queue_background_command

    with app.app_context():
        completed = BackgroundCommand.query.filter_by(
            command_type='scan_home_banners',
            status='completed',
            payload=json.dumps(HOME_BANNER_BACKFILL_PAYLOAD, ensure_ascii=False),
        ).first()
    if completed:
        return None
    return queue_background_command(
        'scan_home_banners', payload=HOME_BANNER_BACKFILL_PAYLOAD, dedupe_pending=True
    )
