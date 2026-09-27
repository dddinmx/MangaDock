"""AniList metadata for indexed WebDAV comics; separate from reading progress."""
import fcntl
import hashlib
import json
import os
import time
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import AniListComicLink, BackgroundCommand, User
from mangadock.services import webdav
from mangadock.settings import COVER_ROOT, MAX_IMAGE_RESPONSE_BYTES, china_tz
from mangadock.utils.cover_image import normalize_cover_bytes
from mangadock.utils.http import safe_http_get

METADATA_FILENAME = '.webdav-anilist.json'
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
    return ''.join(c for c in unicodedata.normalize('NFKC', str(value or '')).casefold() if c.isalnum())


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
    if not directory or not webdav.is_cache_path(directory):
        return None
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
    return (data.get('Page') or {}).get('media') or []


def queue_metadata(comic_name, media_id=None, force=False):
    target = _target(comic_name)
    if not target:
        return None
    with webdav._config_lock():
        if not _target(comic_name, target[1]):
            return None
        value = metadata_view(comic_name)
        if not force and value.get('status') in ('completed', 'unmatched') and time.time() - value.get('checked_at', 0) < 86400:
            return None
        payload = json.dumps({'comic_name': comic_name, 'source': target[1], 'media_id': media_id}, ensure_ascii=False, sort_keys=True)
        with webdav._chapter_file_lock(os.path.join(target[0], METADATA_FILENAME)):
            if not _target(comic_name, target[1]):
                return None
            with app.app_context():
                active = BackgroundCommand.query.filter_by(command_type='webdav_metadata', payload=payload).filter(
                    BackgroundCommand.status.in_(('pending', 'running'))).first()
                if active:
                    command_id = active.id
                    if media_id and value.get('selected_id') != media_id:
                        active.updated_at = datetime.now(china_tz)
                        db.session.flush()
                        value.update(selected_id=media_id, source=target[1], status='pending')
                        webdav._atomic_json(os.path.join(target[0], METADATA_FILENAME), value)
                        db.session.commit()
                    return command_id
                command = BackgroundCommand(command_type='webdav_metadata', payload=payload, status='pending')
                db.session.add(command)
                db.session.flush()
                command_id = command.id
                if media_id:
                    value['selected_id'] = media_id
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


def enrich_metadata(payload):
    comic_name, source = payload['comic_name'], payload['source']
    if not _target(comic_name, source):
        return '漫画已移除或 WebDAV 连接已更改'
    value = metadata_view(comic_name)
    selected = value.get('selected_id')
    if payload.get('media_id') and selected and payload['media_id'] != selected:
        return '已选择其他 AniList 作品，跳过旧任务'
    media_id = selected or payload.get('media_id') or value.get('media_id')
    if not media_id:
        with app.app_context():
            ids = {row.media_id for row in AniListComicLink.query.join(User, User.id == AniListComicLink.user_id).filter(
                AniListComicLink.comic_name == comic_name, User.role == 'admin').all()}
        if len(ids) == 1:
            media_id = ids.pop()
    if media_id:
        media = _graphql('query ($id: Int) { Media(id: $id, type: MANGA) { ' + MEDIA_FIELDS + ' } }', {'id': media_id}).get('Media')
        if not media or media.get('id') != media_id:
            raise ValueError('AniList 漫画条目不存在')
    else:
        media = exact_match(comic_name, search_metadata(comic_name))
    cover_content = None
    if media:
        cover_path = os.path.join(COVER_ROOT, comic_name + '.jpg')
        owned = value.get('cover_digest') and value['cover_digest'] == _cover_digest(cover_path)
        if not os.path.isfile(cover_path) or (owned and value.get('media_id') != media['id']):
            url = (media.get('coverImage') or {}).get('extraLarge') or (media.get('coverImage') or {}).get('large')
            parsed = urlparse(url or '')
            if parsed.scheme == 'https' and (parsed.hostname or '').endswith('.anilist.co'):
                response = safe_http_get(url, timeout=15, max_bytes=MAX_IMAGE_RESPONSE_BYTES)
                try:
                    response.raise_for_status()
                    cover_content = response.content
                finally:
                    response.close()
    with webdav._config_lock():
        target = _target(comic_name, source)
        if not target:
            return '漫画已移除或 WebDAV 连接已更改'
        current = metadata_view(comic_name)
        if current.get('selected_id') != selected:
            return '已选择其他 AniList 作品，跳过旧任务'
        with webdav._chapter_file_lock(os.path.join(target[0], METADATA_FILENAME)):
            if not os.path.isdir(target[0]):
                return '漫画已移除'
            if media:
                current.update(media_id=media['id'], title=(media.get('title') or {}).get('userPreferred') or comic_name,
                               description=plain_description(media.get('description')), status='completed')
                if cover_content:
                    cover_path = os.path.join(COVER_ROOT, comic_name + '.jpg')
                    owned = current.get('cover_digest') and current['cover_digest'] == _cover_digest(cover_path)
                    if not os.path.isfile(cover_path) or (owned and value.get('media_id') != media['id']):
                        os.makedirs(COVER_ROOT, exist_ok=True)
                        if not normalize_cover_bytes(cover_content, cover_path):
                            raise ValueError('AniList 封面格式无效')
                        current['cover_digest'] = _cover_digest(cover_path)
            else:
                current['status'] = 'unmatched'
            current.update(source=source, checked_at=time.time())
            webdav._atomic_json(os.path.join(target[0], METADATA_FILENAME), current)
            webdav._publish_library_change()
    return 'AniList 封面与简介已补全' if media else '未找到唯一精确匹配，请在漫画详情中选择 AniList 资料'


def mark_error(payload):
    try:
        with webdav._config_lock():
            target = _target(payload['comic_name'], payload['source'])
            if target:
                value = metadata_view(payload['comic_name'])
                if payload.get('media_id') and value.get('selected_id') != payload['media_id']:
                    return
                value.update(source=payload['source'], status='error')
                webdav._atomic_json(os.path.join(target[0], METADATA_FILENAME), value)
    except Exception:
        app.logger.warning('WebDAV 资料失败状态无法保存')


def queue_existing_metadata():
    root = webdav.cache_root()
    if not os.path.isdir(root):
        return
    for entry in os.scandir(root):
        if entry.is_dir():
            queue_metadata(entry.name)


def clear_metadata(comic_dir):
    """Remove only an AniList-generated cover still owned by this cache."""
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
