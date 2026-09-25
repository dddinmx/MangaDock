"""Per-user AniList connection and one-way chapter sync."""
import base64
import hashlib
import re

from cryptography.fernet import Fernet

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import AniListAccount, AniListComicLink
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
    link.media_id = media_id
    link.media_title = (media.get('title') or {}).get('userPreferred') or (media.get('title') or {}).get('romaji') or str(media_id)
    link.first_chapter = first_chapter
    db.session.commit()
    return link


def sync_completed_chapter(user_id, comic_name, chapter_index):
    link = AniListComicLink.query.filter_by(user_id=user_id, comic_name=comic_name).first()
    token = token_for(user_id)
    if not link or not token:
        return False
    chapters = list_local_chapters(comic_name)
    if chapter_index < 0 or chapter_index >= len(chapters):
        return False
    first_number = chapter_number(chapters[0]['title'])
    if first_number is not None and link.first_chapter == first_number:
        target = chapter_number(chapters[chapter_index]['title'])
        if target is None:  # 后记等沿用之前最近的正文章节，不额外计数
            target = next((chapter_number(chapter['title']) for chapter in reversed(chapters[:chapter_index])
                           if chapter_number(chapter['title']) is not None), None)
            if target is None:
                return False
    else:
        target = link.first_chapter + chapter_index
    if target <= link.synced_progress:
        return False
    data = graphql('''query ($id: Int) {
      Media(id: $id, type: MANGA) { id mediaListEntry { id progress } }
    }''', {'id': link.media_id}, token)
    media = data.get('Media') or {}
    if media.get('id') != link.media_id:
        raise ValueError('AniList 漫画条目不可用')
    entry = media.get('mediaListEntry') or {}
    remote_progress = entry.get('progress') or 0
    if target > remote_progress:
        mutation = '''mutation ($id: Int, $mediaId: Int, $progress: Int, $status: MediaListStatus) {
          SaveMediaListEntry(id: $id, mediaId: $mediaId, progress: $progress, status: $status) {
            id progress
          }
        }'''
        variables = {'id': entry.get('id'), 'mediaId': link.media_id,
                     'progress': target, 'status': 'CURRENT' if not entry else None}
        result = graphql(mutation, variables, token).get('SaveMediaListEntry')
        if not result or result.get('progress') < target:
            raise ValueError('AniList 未确认进度更新')
    link.synced_progress = max(link.synced_progress, target)
    db.session.commit()
    return target > remote_progress
