"""Per-user AniList connection and chapter progress reconciliation."""
import base64
import hashlib
import re
import os
import fcntl
from datetime import datetime, timedelta

from cryptography.fernet import Fernet

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import AniListAccount, AniListComicLink, ReadingProgress, User
from mangadock.settings import china_tz
from mangadock.services.library import list_local_chapters
from mangadock.utils.http import safe_http_post

GRAPHQL_URL = 'https://graphql.anilist.co'
CLIENT_ID = '51982'


def _cipher():
    key = hashlib.sha256(app.secret_key.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _post(url, body, token=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'MangaDock/1.0'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    response = safe_http_post(url, json_body=body, headers=headers, timeout=10,
                              max_bytes=1024 * 1024)
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()
    if payload.get('errors'):
        raise ValueError('AniList 返回了错误')
    return payload


def graphql(query, variables=None, token=None):
    return (_post(GRAPHQL_URL, {'query': query, 'variables': variables or {}}, token)
            .get('data') or {})


def connect(user_id, token):
    token = token.strip()
    if not token or len(token) > 4096:
        raise ValueError('无效的 AniList 访问令牌')
    viewer = graphql('query { Viewer { id name } }', token=token).get('Viewer')
    if not viewer or not viewer.get('id'):
        raise ValueError('无法读取 AniList 用户')
    account = db.session.get(AniListAccount, user_id)
    if account and account.anilist_user_id != viewer['id']:
        AniListComicLink.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    if not account:
        account = AniListAccount(user_id=user_id)
        db.session.add(account)
    account.anilist_user_id = viewer['id']
    account.username = viewer['name']
    account.encrypted_token = _cipher().encrypt(token.encode('utf-8')).decode('ascii')
    db.session.commit()
    return account


def token_for(user_id):
    account = db.session.get(AniListAccount, user_id)
    return _cipher().decrypt(account.encrypted_token.encode('ascii')).decode('utf-8') if account else None


def chapter_number(title):
    match = re.match(r'^第\s*(\d+)\s*[话話章回](?:\D|$)', title or '')
    return int(match.group(1)) if match else None


def search_manga(title):
    data = graphql('''query ($search: String) {
      Page(page: 1, perPage: 10) {
        media(search: $search, type: MANGA) {
          id title { userPreferred romaji native } chapters
        }
      }
    }''', {'search': title})
    return ((data.get('Page') or {}).get('media') or [])


def link_manga(user_id, comic_name, media_id, first_chapter):
    data = graphql('''query ($id: Int) {
      Media(id: $id, type: MANGA) { id title { userPreferred romaji } chapters }
    }''', {'id': media_id})
    media = data.get('Media')
    if not media or media.get('id') != media_id:
        raise ValueError('AniList 漫画条目不存在')
    if media.get('chapters') and first_chapter > media['chapters']:
        raise ValueError('首章章节号超过 AniList 总章节数')
    link = AniListComicLink.query.filter_by(user_id=user_id, comic_name=comic_name).first()
    if not link:
        link = AniListComicLink(user_id=user_id, comic_name=comic_name)
        db.session.add(link)
    elif link.media_id != media_id or link.first_chapter != first_chapter:
        link.synced_progress = 0
        link.completed_progress = 0
        link.last_sync_at = None
    link.media_id = media_id
    link.media_title = (media.get('title') or {}).get('userPreferred') or (media.get('title') or {}).get('romaji') or str(media_id)
    link.first_chapter = first_chapter
    db.session.commit()
    return link


def _chapter_progress(chapters, first_chapter, chapter_index):
    if chapter_index < 0 or chapter_index >= len(chapters):
        return 0
    first_number = next((chapter_number(chapter['title']) for chapter in chapters
                         if chapter_number(chapter['title']) is not None), None)
    if first_number is not None and first_chapter == first_number:
        target = chapter_number(chapters[chapter_index]['title'])
        if target is None:  # 后记等沿用之前最近的正文章节，不额外计数
            target = next((chapter_number(chapter['title']) for chapter in reversed(chapters[:chapter_index])
                           if chapter_number(chapter['title']) is not None), None)
            if target is None:
                return 0
    else:
        target = first_chapter + chapter_index
    return target


def sync_completed_chapter(user_id, comic_name, chapter_index):
    return reconcile_progress(user_id, comic_name, completed_index=chapter_index)


def reconcile_progress(user_id, comic_name, completed_index=None):
    # Scan first: library helpers may close the current SQLAlchemy session.
    chapters = list_local_chapters(comic_name)
    if not chapters or (completed_index is not None and
                        not 0 <= completed_index < len(chapters)):
        return False
    link = AniListComicLink.query.filter_by(user_id=user_id, comic_name=comic_name).first()
    if not link or not db.session.get(AniListAccount, user_id):
        return False
    if completed_index is not None:
        completed = _chapter_progress(chapters, link.first_chapter, completed_index)
        if completed == 0 or (completed <= link.completed_progress and completed <= link.synced_progress):
            return False
        # Persist a completion even when the network or another sync is busy.
        AniListComicLink.query.filter_by(id=link.id).filter(
            AniListComicLink.completed_progress < completed
        ).update({'completed_progress': completed}, synchronize_session=False)
        db.session.commit()
    lock_dir = os.path.join(app.instance_path, 'anilist_sync_locks')
    os.makedirs(lock_dir, exist_ok=True)
    with open(os.path.join(lock_dir, str(link.id) + '.lock'), 'a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return _reconcile_locked(user_id, comic_name, chapters)


def _reconcile_locked(user_id, comic_name, chapters):
    db.session.expire_all()
    link = AniListComicLink.query.filter_by(user_id=user_id, comic_name=comic_name).first()
    if not link:
        return False
    token = token_for(user_id)
    if not token:
        return False
    link.last_sync_at = datetime.now(china_tz)
    db.session.commit()
    media_id = link.media_id
    data = graphql('''query ($id: Int) {
      Media(id: $id, type: MANGA) { id mediaListEntry { id progress } }
    }''', {'id': media_id}, token)
    media = data.get('Media') or {}
    if media.get('id') != media_id:
        raise ValueError('AniList 漫画条目不可用')
    entry = media.get('mediaListEntry') or {}
    remote_progress = entry.get('progress') or 0
    # Read again after the network call, preserving newer local reading saves.
    db.session.expire_all()
    link = AniListComicLink.query.filter_by(user_id=user_id, comic_name=comic_name).first()
    if not link or link.media_id != media_id:
        return False
    progress = ReadingProgress.query.filter_by(user_id=user_id, comic_name=comic_name).order_by(
        ReadingProgress.last_read_at.desc(), ReadingProgress.id.desc()).first()
    cursor_completed = _chapter_progress(chapters, link.first_chapter,
                                        progress.last_chapter - 1) if progress else 0
    target = max(link.completed_progress, link.synced_progress, cursor_completed)
    if target > remote_progress:
        mutation = '''mutation ($id: Int, $mediaId: Int, $progress: Int, $status: MediaListStatus) {
          SaveMediaListEntry(id: $id, mediaId: $mediaId, progress: $progress, status: $status) {
            id progress
          }
        }'''
        variables = {'id': entry.get('id'), 'mediaId': media_id,
                     'progress': target, 'status': 'CURRENT' if not entry else None}
        result = graphql(mutation, variables, token).get('SaveMediaListEntry')
        if not result or result.get('progress') < target:
            raise ValueError('AniList 未确认进度更新')
    merged = max(target, remote_progress)
    db.session.expire_all()
    progress = ReadingProgress.query.filter_by(user_id=user_id, comic_name=comic_name).order_by(
        ReadingProgress.last_read_at.desc(), ReadingProgress.id.desc()).first()
    # An imported completed chapter resumes at the next available main chapter.
    numbers = [_chapter_progress(chapters, link.first_chapter, i) for i in range(len(chapters))]
    resume = next((i for i, n in enumerate(numbers) if n > merged), len(chapters) - 1)
    if merged > 0 and (not progress or resume > progress.last_chapter):
        if not progress:
            progress = ReadingProgress(user_id=user_id, comic_name=comic_name)
            db.session.add(progress)
        progress.last_chapter = resume
        progress.last_page = 0
        progress.scroll_position = 0
        progress.total_chapters = len(chapters)
        progress.total_pages = 0
        progress.anchor_paragraph = None
        progress.anchor_offset = None
        progress.last_read_at = datetime.now(china_tz)
    link.completed_progress = merged
    link.synced_progress = merged
    db.session.commit()
    app.logger.info('AniList reconciled user=%s comic=%s local=%s remote=%s merged=%s',
                    user_id, comic_name, target, remote_progress, merged)
    return target > remote_progress


def sync_next_link():
    """Check one due link per heartbeat; spread calls below API rate limits."""
    from mangadock.services.reading import user_can_access_progress_key
    from mangadock.auth import make_api_request_user
    with app.app_context():
        cutoff = datetime.now(china_tz) - timedelta(minutes=5)
        row = AniListComicLink.query.filter(
            (AniListComicLink.last_sync_at.is_(None)) | (AniListComicLink.last_sync_at < cutoff)
        ).order_by(AniListComicLink.last_sync_at.asc(), AniListComicLink.id.asc()).first()
        if not row:
            return
        user_id, comic_name = row.user_id, row.comic_name
        row.last_sync_at = datetime.now(china_tz)
        db.session.commit()
        user = db.session.get(User, user_id)
        user = make_api_request_user(user) if user else None
        if user and user_can_access_progress_key(comic_name, user):
            reconcile_progress(user_id, comic_name)
