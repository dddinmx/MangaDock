"""AniList and MangaUpdates metadata; separate from reading progress."""
import fcntl
import hashlib
import json
import os
import time
import tempfile
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from opencc import OpenCC
from urllib.parse import urlparse

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import AniListComicLink, BackgroundCommand, User
from mangadock.services import webdav
from mangadock.settings import COVER_ROOT, MAX_IMAGE_RESPONSE_BYTES, china_tz
from mangadock.utils.cover_image import normalize_cover_bytes
from mangadock.utils.http import safe_http_get

METADATA_FILENAME = '.webdav-anilist.json'
METADATA_VERSION = 2
PROVIDERS = {'anilist': 'AniList', 'mangaupdates': 'MangaUpdates'}
TITLE_CONVERTER = OpenCC('t2s')
MEDIA_FIELDS = 'id title { userPreferred romaji english native } synonyms description(asHtml: false) coverImage { extraLarge large }'


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_description(value):
    parser = _PlainText()
    parser.feed(str(value or '')[:20000])
    return ' '.join(''.join(parser.parts).split())[:2000]


def _normalized_title(value):
    title = TITLE_CONVERTER.convert(unicodedata.normalize('NFKC', str(value or '')).casefold())
    return ''.join(c for c in title if c.isalnum())


def exact_match(name, candidates):
    title = _normalized_title(name)
    matches = {item['id']: item for item in candidates if item.get('id') and title and
               title in {_normalized_title(value) for value in
                         list((item.get('title') or {}).values()) + (item.get('synonyms') or [])}}
    return next(iter(matches.values())) if len(matches) == 1 else None


def _target(comic_name, source=None):
    from mangadock.services.library import get_comic_directory, is_safe_comic_name
    if not is_safe_comic_name(comic_name):
        return None
    directory = get_comic_directory(comic_name)
    if not directory:
        return None
    if not webdav.is_cache_path(directory):
        local_source = 'local:' + hashlib.sha256(os.path.realpath(directory).encode()).hexdigest()[:16]
        return (directory, local_source) if source is None or source == local_source else None
    index = webdav._read_index_payload(directory)
    config = webdav._read_config()
    if not config.get('url') or not index['source'] or index['source'] != webdav._source_id(config):
        return None
    if source is not None and source != index['source']:
        return None
    return directory, index['source']


def metadata_view(comic_name):
    target = _target(comic_name)
    if not target:
        return {}
    try:
        with open(os.path.join(target[0], METADATA_FILENAME), encoding='utf-8') as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) and value.get('source') == target[1] else {}
    except (OSError, ValueError):
        return {}


def _graphql(query, variables):
    # Metadata gets at most 20 requests/minute across processes, leaving room
    # for the existing progress integration. Honour upstream cooldowns too.
    from mangadock.services.anilist import graphql
    identity = os.path.join(app.instance_path, 'anilist-metadata-rate')
    with webdav._open_lock_file(identity) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        stamp = identity + '.json'
        try:
            with open(stamp) as handle:
                next_request = float(json.load(handle))
        except (OSError, ValueError, TypeError):
            next_request = 0
        time.sleep(max(0, min(120, next_request - time.time())))
        try:
            return graphql(query, variables)
        except Exception as exc:
            response = getattr(exc, 'response', None)
            if response is not None and response.status_code == 429:
                try:
                    delay = max(60, min(120, float(response.headers.get('Retry-After', 60))))
                except (TypeError, ValueError):
                    delay = 60
                webdav._atomic_json(stamp, time.time() + delay)
                raise
            raise
        finally:
            # Keep a 429 cooldown rather than replacing it with the normal gap.
            try:
                with open(stamp) as handle:
                    cooldown = float(json.load(handle))
            except (OSError, ValueError, TypeError):
                cooldown = 0
            webdav._atomic_json(stamp, max(cooldown, time.time() + 3))


def search_metadata(title):
    data = _graphql('query ($search: String) { Page(page: 1, perPage: 10) { media(search: $search, type: MANGA) { ' + MEDIA_FIELDS + ' } } }', {'search': title[:100]})
    results = (data.get('Page') or {}).get('media') or []
    for media in results:
        media['description'] = plain_description(media.get('description'))
    return results


def queue_metadata(comic_name, media_id=None, force=False, provider='anilist'):
    if provider not in PROVIDERS:
        raise ValueError('无效的资料来源')
    target = _target(comic_name)
    if not target:
        return None
    with webdav._config_lock():
        if not _target(comic_name, target[1]):
            return None
        value = metadata_view(comic_name)
        if not force:
            status = value.get('status')
            if status == 'completed' and os.path.isfile(os.path.join(COVER_ROOT, comic_name + '.jpg')):
                from mangadock.services.library import get_comic_description
                if value.get('description') or get_comic_description(comic_name):
                    return None
            if status in ('unmatched', 'error', 'partial') and value.get('metadata_version') == METADATA_VERSION:
                retry_after = 7 * 86400 if status == 'unmatched' else 86400
                if time.time() - float(value.get('checked_at') or 0) < retry_after:
                    return None
        payload = json.dumps({'comic_name': comic_name, 'source': target[1], 'media_id': media_id,
                              'provider': provider}, ensure_ascii=False, sort_keys=True)
        with webdav._chapter_file_lock(os.path.join(target[0], METADATA_FILENAME)):
            if not _target(comic_name, target[1]):
                return None
            with app.app_context():
                active = BackgroundCommand.query.filter_by(command_type='webdav_metadata', payload=payload).filter(
                    BackgroundCommand.status.in_(('pending', 'running'))).first()
                if active:
                    command_id = active.id
                    if media_id and (value.get('selected_id') != media_id or value.get('selected_provider', 'anilist') != provider):
                        active.updated_at = datetime.now(china_tz)
                        db.session.flush()
                        value.update(selected_id=media_id, selected_provider=provider, source=target[1], status='pending')
                        webdav._atomic_json(os.path.join(target[0], METADATA_FILENAME), value)
                        db.session.commit()
                    return command_id
                command = BackgroundCommand(command_type='webdav_metadata', payload=payload, status='pending')
                db.session.add(command)
                db.session.flush()
                command_id = command.id
                if media_id:
                    value['selected_id'] = media_id
                    value['selected_provider'] = provider
                value.update(source=target[1], status='pending')
                webdav._atomic_json(os.path.join(target[0], METADATA_FILENAME), value)
                db.session.commit()
                return command_id


def _cover_digest(path):
    try:
        with open(path, 'rb') as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return ''


def _media_url(media, provider):
    if provider == 'anilist':
        return f"https://anilist.co/manga/{media['id']}"
    url = media.get('url') or ''
    parsed = urlparse(url)
    if parsed.scheme == 'https' and parsed.hostname in ('www.mangaupdates.com', 'mangaupdates.com'):
        return url
    return f"https://www.mangaupdates.com/series.html?id={media['id']}"


def _download_metadata_cover(media, provider):
    url = (media.get('coverImage') or {}).get('extraLarge') or (media.get('coverImage') or {}).get('large')
    parsed = urlparse(url or '')
    suffix = 'anilist.co' if provider == 'anilist' else 'mangaupdates.com'
    host = parsed.hostname or ''
    if parsed.scheme != 'https' or not (host == suffix or host.endswith('.' + suffix)):
        return None
    try:
        response = safe_http_get(url, timeout=15, max_bytes=MAX_IMAGE_RESPONSE_BYTES)
        try:
            response.raise_for_status()
            with tempfile.TemporaryDirectory(prefix='mangadock-cover-') as directory:
                path = os.path.join(directory, 'cover.jpg')
                if normalize_cover_bytes(response.content, path):
                    with open(path, 'rb') as handle:
                        return handle.read()
        finally:
            response.close()
    except Exception:
        app.logger.warning('%s 封面获取失败，尝试下一来源', PROVIDERS[provider])
    return None


def enrich_metadata(payload):
    from mangadock.services import mangaupdates_metadata
    from mangadock.services.library import get_comic_description
    comic_name, source = payload['comic_name'], payload['source']
    if not _target(comic_name, source):
        return '漫画已移除或来源已更改'
    value = metadata_view(comic_name)
    selected = value.get('selected_id')
    selected_provider = value.get('selected_provider', 'anilist')
    if payload.get('media_id') and selected and (payload['media_id'] != selected or payload.get('provider', 'anilist') != selected_provider):
        return '已选择其他作品，跳过旧任务'
    media_id = selected or payload.get('media_id') or value.get('media_id')
    provider = selected_provider if selected else payload.get('provider', 'anilist')
    if not media_id:
        with app.app_context():
            ids = {row.media_id for row in AniListComicLink.query.join(User, User.id == AniListComicLink.user_id).filter(
                AniListComicLink.comic_name == comic_name, User.role == 'admin').all()}
        if len(ids) == 1:
            media_id = ids.pop()
    cover_path = os.path.join(COVER_ROOT, comic_name + '.jpg')
    owned = value.get('cover_digest') and value['cover_digest'] == _cover_digest(cover_path)
    previous_id = value.get('mangaupdates_id') if provider == 'mangaupdates' else value.get('media_id')
    choice_changed = bool(selected and (previous_id != selected or value.get('provider', 'anilist') != provider))
    replace_owned = bool(owned and choice_changed)
    need_cover = not os.path.isfile(cover_path) or replace_owned
    description = value.get('description') or ''
    description_provider = value.get('description_provider') or ('anilist' if value.get('media_id') and description else '')
    description_url = value.get('description_url') or (f"https://anilist.co/manga/{value['media_id']}" if description_provider == 'anilist' else '')
    existing_description = get_comic_description(comic_name)
    if choice_changed:
        if existing_description == description:
            existing_description = ''
        description, description_provider, description_url = '', '', ''
    cover_content, cover_provider, cover_url = None, '', ''
    matches, errors = {}, []
    primary = None
    try:
        if provider == 'mangaupdates' and media_id:
            primary = mangaupdates_metadata.get_metadata(media_id)
        elif media_id:
            primary = _graphql('query ($id: Int) { Media(id: $id, type: MANGA) { ' + MEDIA_FIELDS + ' } }', {'id': media_id}).get('Media')
            if not primary or primary.get('id') != media_id:
                raise ValueError('AniList 漫画条目不存在')
        else:
            primary = exact_match(comic_name, search_metadata(comic_name))
    except Exception as exc:
        errors.append(exc)
        app.logger.warning('%s 资料查询失败，尝试下一来源', PROVIDERS[provider])
    if primary:
        matches[provider] = primary
        primary_description = plain_description(primary.get('description'))
        if primary_description:
            description, description_provider, description_url = primary_description, provider, _media_url(primary, provider)
        if need_cover:
            cover_content = _download_metadata_cover(primary, provider)
            if cover_content:
                cover_provider, cover_url = provider, _media_url(primary, provider)
    if provider != 'mangaupdates' and (not primary or (need_cover and not cover_content) or not (description or existing_description)):
        try:
            secondary = mangaupdates_metadata.match_metadata(comic_name)
            if not secondary and primary:
                # AniList's original title can bridge a local translated title to MangaUpdates aliases.
                title = (primary.get('title') or {}).get('native') or (primary.get('title') or {}).get('english')
                if title and _normalized_title(title) != _normalized_title(comic_name):
                    secondary = mangaupdates_metadata.match_metadata(title)
            if secondary:
                matches['mangaupdates'] = secondary
                if not (description or existing_description):
                    description = plain_description(secondary.get('description'))
                    if description:
                        description_provider, description_url = 'mangaupdates', _media_url(secondary, 'mangaupdates')
                if need_cover and not cover_content:
                    cover_content = _download_metadata_cover(secondary, 'mangaupdates')
                    if cover_content:
                        cover_provider, cover_url = 'mangaupdates', _media_url(secondary, 'mangaupdates')
        except Exception as exc:
            errors.append(exc)
            app.logger.warning('MangaUpdates 资料查询失败')
    with webdav._config_lock():
        target = _target(comic_name, source)
        if not target:
            return '漫画已移除或来源已更改'
        current = metadata_view(comic_name)
        if current.get('selected_id') != selected or current.get('selected_provider', 'anilist') != selected_provider:
            return '已选择其他作品，跳过旧任务'
        with webdav._chapter_file_lock(os.path.join(target[0], METADATA_FILENAME)):
            if not os.path.isdir(target[0]):
                return '漫画已移除'
            for matched_provider, media in matches.items():
                current['media_id' if matched_provider == 'anilist' else 'mangaupdates_id'] = media['id']
            if matches:
                media = primary or matches.get('mangaupdates')
                current.update(title=(media.get('title') or {}).get('userPreferred') or comic_name,
                               provider=provider if primary else 'mangaupdates')
            if choice_changed and not description:
                for key in ('description', 'description_provider', 'description_url'):
                    current.pop(key, None)
            if description:
                current.update(description=description, description_provider=description_provider,
                               description_url=description_url)
            if cover_content:
                still_owned = current.get('cover_digest') and current['cover_digest'] == _cover_digest(cover_path)
                if not os.path.isfile(cover_path) or (replace_owned and still_owned):
                    os.makedirs(COVER_ROOT, exist_ok=True)
                    handle = tempfile.NamedTemporaryFile('wb', prefix='.metadata-', dir=COVER_ROOT, delete=False)
                    try:
                        with handle:
                            handle.write(cover_content)
                        os.replace(handle.name, cover_path)
                    finally:
                        if os.path.exists(handle.name):
                            os.remove(handle.name)
                    current.update(cover_digest=_cover_digest(cover_path), cover_provider=cover_provider, cover_url=cover_url)
            has_cover = os.path.isfile(cover_path)
            has_description = bool(description or existing_description)
            if has_cover and has_description:
                status = 'completed'
            elif has_cover or has_description:
                status = 'partial'
            else:
                status = 'error' if errors else 'unmatched'
            current.update(source=source, status=status, checked_at=time.time(), metadata_version=METADATA_VERSION)
            webdav._atomic_json(os.path.join(target[0], METADATA_FILENAME), current)
            webdav._publish_library_change()
    if status == 'error':
        raise ValueError('资料来源暂时不可用，稍后会自动重试') from errors[0]
    return {'completed': '封面与简介已补全', 'partial': '部分资料已补全，缺失资料稍后重试',
            'unmatched': '两个来源都未找到唯一匹配，请搜索并选择作品'}[status]


def mark_error(payload):
    try:
        with webdav._config_lock():
            target = _target(payload['comic_name'], payload['source'])
            if target:
                value = metadata_view(payload['comic_name'])
                if payload.get('media_id') and (value.get('selected_id') != payload['media_id'] or value.get('selected_provider', 'anilist') != payload.get('provider', 'anilist')):
                    return
                value.update(source=payload['source'], status='error', checked_at=time.time(), metadata_version=METADATA_VERSION)
                webdav._atomic_json(os.path.join(target[0], METADATA_FILENAME), value)
    except Exception:
        app.logger.warning('WebDAV 资料失败状态无法保存')


def queue_existing_metadata():
    from mangadock.services.library import get_comic_scan_roots, get_comic_descriptions
    for root in get_comic_scan_roots(existing_only=True):
        if not os.path.isdir(root):
            continue
        entries = [entry for entry in os.scandir(root) if entry.is_dir() and not entry.name.startswith('.')]
        descriptions = get_comic_descriptions([entry.name for entry in entries])
        for entry in entries:
            if not os.path.isfile(os.path.join(entry.path, METADATA_FILENAME)) and descriptions.get(entry.name) and os.path.isfile(os.path.join(COVER_ROOT, entry.name + '.jpg')):
                continue
            target = _target(entry.name)
            if target and os.path.realpath(target[0]) == os.path.realpath(entry.path):
                queue_metadata(entry.name)


def clear_metadata(comic_dir):
    """Remove only a metadata-generated cover still owned by this cache."""
    from mangadock.services.library import get_comic_directory
    name = os.path.basename(comic_dir)
    try:
        with open(os.path.join(comic_dir, METADATA_FILENAME), encoding='utf-8') as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return
    cover_path = os.path.join(COVER_ROOT, name + '.jpg')
    visible = get_comic_directory(name)
    if isinstance(value, dict) and value.get('cover_digest') and \
            (not visible or os.path.abspath(visible) == os.path.abspath(comic_dir)) and \
            value['cover_digest'] == _cover_digest(cover_path):
        try:
            os.remove(cover_path)
        except FileNotFoundError:
            pass
    try:
        os.remove(os.path.join(comic_dir, METADATA_FILENAME))
    except FileNotFoundError:
        pass


def metadata_progress():
    from mangadock.services.library import get_comic_scan_roots
    counts = {key: 0 for key in ('pending', 'completed', 'unmatched', 'error', 'partial')}
    seen = set()
    for root in get_comic_scan_roots(existing_only=True):
        if not os.path.isdir(root):
            continue
        for entry in os.scandir(root):
            if not entry.is_dir() or entry.name in seen or not os.path.isfile(os.path.join(entry.path, METADATA_FILENAME)):
                continue
            value = metadata_view(entry.name)
            status = value.get('status')
            if status in counts:
                counts[status] += 1
                seen.add(entry.name)
    counts['total'] = sum(counts.values())
    return counts
