# -*- coding: utf-8 -*-
"""JSON API routes."""
import os
import subprocess
import zipfile
from datetime import datetime

from flask import jsonify, request, send_file, send_from_directory, session

from mangadock.auth import (
    api_comic_format,
    api_datetime,
    api_display_reading_minutes,
    api_error,
    api_login_required,
    authenticate_api_credentials,
    build_api_statistics_payload,
    build_local_comic_entry,
    establish_user_session,
    find_chapter_by_id,
    get_api_comic_entry,
    get_api_request_user,
    require_csrf_for_session_auth,
    serialize_api_chapter,
    serialize_api_comic_summary,
    serialize_api_progress,
    serialize_api_user,
    sort_comics_for_api,
)
from mangadock.core import app
from mangadock.extensions import csrf
from mangadock.services.groups import (
    can_user_access_group,
    filter_grouped_comics_for_user,
    get_comic_group_map,
    normalize_group_name,
)
from mangadock.services.library import (
    build_api_page_list,
    get_available_comics,
    get_chapter_file_path,
    get_comic_directory,
    list_local_chapters,
    load_comic_mapping,
    resolve_chapter_page_image,
)
from mangadock.services.reading import (
    get_all_reading_progress,
    get_reading_progress,
    record_reading_time,
    save_reading_progress,
)
from mangadock.settings import APP_VERSION, COVER_ROOT, china_tz
from mangadock.utils.files import resolve_file_under_directory
from mangadock.utils.media import repair_pdf_for_reading


def safe_print(message, end="\n", flush=False):
    print(message, end=end, flush=flush)

@app.route('/api/auth/login', methods=['POST'])
@csrf.exempt
def api_auth_login():
    data = request.get_json(silent=True) or {}
    username = data.get('username')
    password = data.get('password')
    ip_address = request.remote_addr or ''
    user_agent = request.user_agent.string

    user, error_response = authenticate_api_credentials(username, password, ip_address, user_agent)
    if error_response:
        return error_response

    establish_user_session(user)
    return jsonify({
        'user': serialize_api_user(user),
    })


@app.route('/api/auth/logout', methods=['POST'])
@csrf.exempt
def api_auth_logout():
    csrf_error = require_csrf_for_session_auth(api_response=True)
    if csrf_error:
        return csrf_error
    session.clear()
    return jsonify({'success': True})


@app.route('/api/info')
def api_info():
    return jsonify({
        'name': 'MangaDock',
        'version': '1',
        'app_version': APP_VERSION,
        'supports_basic_auth': True,
        'supports_page_images': True,
        'page_formats': ['cbz', 'pdf'],
        'api_v1': '/api/v1',
    })


@app.route('/api/auth/me')
@api_login_required
def api_auth_me():
    current_user = get_api_request_user()
    return jsonify({
        'user': serialize_api_user(current_user),
    })


@app.route('/api/comics')
@api_login_required
def api_comics():
    current_user = get_api_request_user()
    current_user_id = current_user.id
    query = (request.args.get('query') or '').strip().lower()
    group_name = normalize_group_name(request.args.get('group'))
    page = max(1, int(request.args.get('page', 1) or 1))
    page_size = min(100, max(1, int(request.args.get('page_size', 50) or 50)))

    comics = get_available_comics()
    comics, _, _, _ = filter_grouped_comics_for_user(comics, current_user)
    progress_lookup = {
        progress.comic_name: progress
        for progress in get_all_reading_progress(current_user_id)
    }
    comics = sort_comics_for_api(comics, progress_lookup)

    if group_name and group_name != '全部':
        comics = [
            comic for comic in comics
            if (normalize_group_name(comic.get('group')) or '默认分组') == group_name
        ]

    if query:
        comics = [
            comic for comic in comics
            if query in (comic.get('comic_name') or '').lower()
        ]

    total = len(comics)
    start = (page - 1) * page_size
    end = start + page_size
    source_mapping = load_comic_mapping()

    items = []
    for comic in comics[start:end]:
        serialized_item = serialize_api_comic_summary(
            comic,
            progress=progress_lookup.get(comic.get('comic_name')),
            source_mapping=source_mapping
        )
        if serialized_item:
            items.append(serialized_item)

    return jsonify({
        'items': items,
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total,
            'has_more': end < total,
        }
    })


@app.route('/api/comics/<comic_id>')
@api_login_required
def api_comic_detail(comic_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    progress = get_reading_progress(identity.comic_name, current_user.id)
    chapters = [
        serialize_api_chapter(identity.comic_id, chapter)
        for chapter in list_local_chapters(identity.comic_name)
    ]
    item = serialize_api_comic_summary(comic_entry, progress=progress, source_mapping=load_comic_mapping())
    if not item:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)
    item['chapters'] = chapters
    item['progress'] = serialize_api_progress(progress)

    return jsonify({'item': item})


@app.route('/api/comics/<comic_id>/cover')
@api_login_required
def api_comic_cover(comic_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    cover_filename = f'{identity.comic_name}.jpg'
    cover_path = os.path.join(COVER_ROOT, cover_filename)
    if os.path.exists(cover_path):
        return send_from_directory(COVER_ROOT, cover_filename)
    return send_from_directory(os.path.join(app.static_folder, 'cover'), 'cover.png')


@app.route('/api/comics/<comic_id>/chapters')
@api_login_required
def api_comic_chapters(comic_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    chapters = [
        serialize_api_chapter(identity.comic_id, chapter)
        for chapter in list_local_chapters(identity.comic_name)
    ]
    return jsonify({
        'comic_id': identity.comic_id,
        'comic_name': identity.comic_name,
        'chapters': chapters,
        'total_chapters': len(chapters),
    })


@app.route('/api/comics/<comic_id>/chapters/<chapter_id>/file')
@api_login_required
def api_comic_chapter_file(comic_id, chapter_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    chapter = find_chapter_by_id(identity.comic_name, chapter_id)
    if not chapter:
        return api_error('CHAPTER_NOT_FOUND', '章节不存在', 404)

    comic_dir = get_comic_directory(identity.comic_name)
    if not comic_dir:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    filename = chapter.get('filename')
    file_path = resolve_file_under_directory(comic_dir, filename)
    if not file_path or not os.path.exists(file_path):
        return api_error('CHAPTER_NOT_FOUND', '章节文件不存在', 404)

    if file_path.lower().endswith('.pdf'):
        readable_pdf = repair_pdf_for_reading(file_path)
        return send_from_directory(os.path.dirname(readable_pdf), os.path.basename(readable_pdf))
    return send_from_directory(comic_dir, filename)


@app.route('/api/comics/<comic_id>/chapters/<chapter_id>/pages')
@api_login_required
def api_comic_chapter_pages(comic_id, chapter_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    chapter = find_chapter_by_id(identity.comic_name, chapter_id)
    if not chapter:
        return api_error('CHAPTER_NOT_FOUND', '章节不存在', 404)

    file_path = get_chapter_file_path(identity.comic_name, chapter)
    if not file_path:
        return api_error('CHAPTER_NOT_FOUND', '章节文件不存在', 404)

    try:
        pages, total_pages = build_api_page_list(identity.comic_id, chapter_id, file_path)
    except ValueError as exc:
        return api_error('PAGE_LIST_FAILED', str(exc), 500)

    return jsonify({
        'comic_id': identity.comic_id,
        'chapter_id': chapter_id,
        'pages': pages,
        'total_pages': total_pages,
    })


@app.route('/api/comics/<comic_id>/chapters/<chapter_id>/pages/<int:page_index>')
@api_login_required
def api_comic_chapter_page_image(comic_id, chapter_id, page_index):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    chapter = find_chapter_by_id(identity.comic_name, chapter_id)
    if not chapter:
        return api_error('CHAPTER_NOT_FOUND', '章节不存在', 404)

    file_path = get_chapter_file_path(identity.comic_name, chapter)
    if not file_path:
        return api_error('CHAPTER_NOT_FOUND', '章节文件不存在', 404)

    try:
        image_path = resolve_chapter_page_image(file_path, identity.comic_id, chapter_id, page_index)
    except RuntimeError as exc:
        return api_error('PAGE_RENDER_FAILED', str(exc), 500)
    except subprocess.CalledProcessError as exc:
        return api_error('PAGE_RENDER_FAILED', exc.stderr or 'PDF 页面渲染失败', 500)
    except zipfile.BadZipFile:
        return api_error('PAGE_RENDER_FAILED', 'CBZ 文件损坏或格式不受支持', 500)
    except Exception as exc:
        safe_print(f"页面渲染失败: {exc}")
        return api_error('PAGE_RENDER_FAILED', '页面渲染失败', 500)

    if not image_path or not os.path.exists(image_path):
        return api_error('PAGE_NOT_FOUND', '页面不存在', 404)

    return send_from_directory(os.path.dirname(image_path), os.path.basename(image_path))


@app.route('/api/comics/<comic_id>/progress')
@api_login_required
def api_get_comic_progress(comic_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    progress = get_reading_progress(identity.comic_name, current_user.id)
    return jsonify({
        'progress': serialize_api_progress(progress) or {
            'chapter_index': 0,
            'page_index': 0,
            'scroll_position': 0,
            'total_chapters': 0,
            'total_pages': 0,
            'updated_at': None,
        }
    })


@app.route('/api/comics/<comic_id>/progress', methods=['POST'])
@api_login_required
@csrf.exempt
def api_save_comic_progress(comic_id):
    csrf_error = require_csrf_for_session_auth(api_response=True)
    if csrf_error:
        return csrf_error

    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    data = request.get_json(silent=True) or {}
    chapter_index = int(data.get('chapter_index', 0) or 0)
    page_index = int(data.get('page_index', 0) or 0)
    scroll_position = int(data.get('scroll_position', 0) or 0)
    total_chapters = int(data.get('total_chapters', 0) or 0)
    total_pages = int(data.get('total_pages', 0) or 0)
    reading_time_seconds = int(data.get('reading_time_seconds', 0) or 0)
    reading_session_id = (data.get('reading_session_id') or '').strip()

    progress = save_reading_progress(
        identity.comic_name,
        chapter_index,
        page_index,
        scroll_position,
        total_chapters,
        total_pages,
        current_user.id
    )
    if reading_time_seconds > 0:
        record_reading_time(identity.comic_name, reading_time_seconds, current_user.id, reading_session_id or None)

    return jsonify({
        'success': True,
        'progress': serialize_api_progress(progress),
    })


@app.route('/api/statistics')
@api_login_required
def api_statistics():
    try:
        requested_year = int(request.args.get('year') or datetime.now(china_tz).year)
    except (TypeError, ValueError):
        return api_error('INVALID_YEAR', '统计年份无效', 400)

    if requested_year < 2000 or requested_year > 2100:
        return api_error('INVALID_YEAR', '统计年份无效', 400)

    current_user = get_api_request_user()
    payload = build_api_statistics_payload(requested_year, current_user.id)
    return jsonify(payload)

