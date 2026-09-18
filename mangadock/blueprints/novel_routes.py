# -*- coding: utf-8 -*-
"""Server-rendered routes for the local EPUB novel mode."""
from io import BytesIO

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for

from mangadock.auth import admin_required, get_current_user, login_required
from mangadock.core import app
from mangadock.models import DownloadTask
from mangadock.services.novels import (
    NOVEL_PROGRESS_PREFIX,
    get_novel,
    get_novel_by_fanqie_id,
    get_novel_chapter,
    get_novel_chapters,
    get_novel_cover,
    get_novels,
    novel_progress_key,
    save_novel_cover,
)
from mangadock.services.fanqie import (
    fanqie_task_url,
    fetch_book_cover,
    fetch_cover,
    resolve_novel_target,
    search_books,
)
from mangadock.services.tasks import create_task, update_task
from mangadock.services.reading import (
    get_all_reading_progress,
    get_reading_progress,
    get_reading_time_for_comic,
)


def _novel_progress_lookup(user_id):
    return {
        progress.comic_name[len(NOVEL_PROGRESS_PREFIX):]: progress
        for progress in (get_all_reading_progress(user_id) or [])
        if progress.comic_name.startswith(NOVEL_PROGRESS_PREFIX)
    }


def _novel_with_progress(novel, progress):
    item = dict(novel)
    item['progress'] = progress
    item['last_chapter'] = progress.last_chapter if progress else 0
    item['last_read_at'] = progress.last_read_at if progress else None
    return item


@app.route('/novels')
@login_required
def novel_index():
    novels = get_novels()
    progresses = _novel_progress_lookup(session.get('user_id'))
    spotlight = None
    for novel_id, progress in sorted(
        progresses.items(), key=lambda item: item[1].last_read_at, reverse=True
    ):
        novel = next((item for item in novels if item['novel_id'] == novel_id), None)
        if novel:
            spotlight = _novel_with_progress(novel, progress)
            break
    if not spotlight and novels:
        spotlight = _novel_with_progress(novels[0], None)

    latest = [_novel_with_progress(novel, progresses.get(novel['novel_id'])) for novel in novels[:10]]
    return render_template(
        'novel_index.html',
        library_mode='novel',
        spotlight=spotlight,
        latest_novels=latest,
        novels=novels,
        current_user=get_current_user(),
    )


@app.route('/novels/library')
@login_required
def novels_list():
    progresses = _novel_progress_lookup(session.get('user_id'))
    novels = [_novel_with_progress(novel, progresses.get(novel['novel_id'])) for novel in get_novels()]
    novels.sort(
        key=lambda novel: (
            0 if novel['progress'] else 1,
            -(novel['last_read_at'].timestamp() if novel['last_read_at'] else novel['created_at'].timestamp()),
        )
    )
    return render_template(
        'novels.html',
        library_mode='novel',
        novels=novels,
        current_user=get_current_user(),
    )


@app.route('/novels/search')
@login_required
def novel_search():
    query = (request.args.get('q') or '').strip()
    results = []
    if query:
        lowered = query.casefold()
        results = [
            novel for novel in get_novels()
            if lowered in novel['title'].casefold() or lowered in novel['author'].casefold()
        ]
    return render_template(
        'novel_search.html',
        library_mode='novel',
        query=query,
        novels=results,
        result_count=len(results),
        current_user=get_current_user(),
    )


@app.route('/novels/fanqie')
@login_required
def fanqie_novel_search():
    query = (request.args.get('q') or '').strip()
    results = []
    error = ''
    if query:
        try:
            results = search_books(query)
        except Exception as exc:
            error = f'番茄搜索暂时不可用：{exc}'
    local_ids = {
        novel.get('fanqie_book_id'): novel
        for novel in get_novels()
        if novel.get('fanqie_book_id')
    }
    return render_template(
        'novel_fanqie.html',
        library_mode='novel',
        query=query,
        results=results,
        local_ids=local_ids,
        error=error,
        current_user=get_current_user(),
    )


@app.route('/novels/fanqie/cover')
@login_required
def fanqie_novel_cover():
    try:
        content, mimetype = fetch_cover(request.args.get('book_id') or '')
    except Exception:
        response = app.send_static_file('cover/cover.png')
        response.cache_control.no_cache = True
        response.cache_control.max_age = 0
        return response
    return send_file(BytesIO(content), mimetype=mimetype, max_age=3600)


@app.route('/novels/fanqie/<book_id>/cover')
@login_required
def fanqie_novel_book_cover(book_id):
    """Cover used while a Fanqie novel is still downloading."""
    try:
        content, mimetype = fetch_book_cover(book_id)
    except Exception:
        abort(404)
    return send_file(BytesIO(content), mimetype=mimetype, max_age=3600)


@app.route('/novels/fanqie/download', methods=['POST'])
@login_required
@admin_required
def fanqie_novel_download():
    target = (request.form.get('book_id') or request.form.get('target') or '').strip()
    try:
        metadata = resolve_novel_target(target)
        book_id = metadata['book_id']
    except Exception as exc:
        flash(str(exc))
        return redirect(url_for('fanqie_novel_search', q=request.form.get('return_query') or ''))

    existing = get_novel_by_fanqie_id(book_id)
    task_url = fanqie_task_url(book_id)
    active_task = DownloadTask.query.filter(
        DownloadTask.url == task_url,
        DownloadTask.status.in_(('pending', 'running')),
    ).order_by(DownloadTask.created_at.asc()).first()
    if active_task:
        flash('这本小说已有下载或更新任务，已为你打开现有任务')
        return redirect(url_for('progress', task_id=active_task.id))
    task_id = create_task(
        task_url,
        0,
        is_update=bool(existing),
        comic_name=existing['title'] if existing else (
            metadata.get('title') or request.form.get('title') or f'番茄小说 {book_id}'
        ),
    )
    update_task(task_id, log='番茄小说任务已加入后台队列')
    flash('已加入更新队列' if existing else '已加入下载队列，完成后会自动出现在小说书架')
    return redirect(url_for('progress', task_id=task_id))


@app.route('/novels/history')
@login_required
def novel_history():
    progress_lookup = _novel_progress_lookup(session.get('user_id'))
    items = [
        _novel_with_progress(novel, progress_lookup[novel['novel_id']])
        for novel in get_novels()
        if novel['novel_id'] in progress_lookup
    ]
    items.sort(key=lambda novel: novel['last_read_at'], reverse=True)
    return render_template(
        'novel_history.html',
        library_mode='novel',
        items=items,
        current_user=get_current_user(),
    )


@app.route('/novel/<path:novel_id>')
@login_required
def novel_detail(novel_id):
    novel = get_novel(novel_id)
    if not novel:
        abort(404)
    progress_key = novel_progress_key(novel_id)
    user_id = session.get('user_id')
    progress = get_reading_progress(progress_key, user_id)
    return render_template(
        'novel_detail.html',
        library_mode='novel',
        novel=novel,
        chapters=get_novel_chapters(novel_id),
        progress=progress,
        reading_time=get_reading_time_for_comic(progress_key, user_id),
        current_user=get_current_user(),
    )


@app.route('/novel/<path:novel_id>/read')
@login_required
def novel_reader(novel_id):
    novel = get_novel(novel_id)
    if not novel:
        abort(404)
    progress = get_reading_progress(novel_progress_key(novel_id), session.get('user_id'))
    requested = request.args.get('start_chapter')
    try:
        chapter_index = int(requested) if requested is not None else (progress.last_chapter if progress else 0)
    except (TypeError, ValueError):
        chapter_index = 0
    chapter_index = max(0, min(chapter_index, max(novel['chapter_count'] - 1, 0)))
    chapter = get_novel_chapter(novel_id, chapter_index)
    if not chapter:
        abort(404)
    return render_template(
        'novel_reader.html',
        novel=novel,
        chapter=chapter,
        saved_page=progress.last_page if progress and progress.last_chapter == chapter_index else 0,
        saved_anchor_paragraph=progress.anchor_paragraph if progress and progress.last_chapter == chapter_index else None,
        saved_anchor_offset=progress.anchor_offset if progress and progress.last_chapter == chapter_index else None,
        progress_key=novel_progress_key(novel_id),
        chapter_api_base_url=url_for('novel_chapter_data', novel_id=novel_id, chapter_index=0).rsplit('/', 1)[0],
        reader_base_url=url_for('novel_reader', novel_id=novel_id),
    )


@app.route('/novel/<path:novel_id>/chapter/<int:chapter_index>')
@login_required
def novel_chapter_data(novel_id, chapter_index):
    chapter = get_novel_chapter(novel_id, chapter_index)
    if not chapter:
        abort(404)
    return jsonify(chapter)


@app.route('/novel/<path:novel_id>/cover')
@login_required
def novel_cover(novel_id):
    cover = get_novel_cover(novel_id)
    if not cover:
        return app.send_static_file('cover/cover.png')
    content, mimetype = cover
    return send_file(
        BytesIO(content),
        mimetype=mimetype,
        max_age=0,
        download_name=f'{novel_id}.jpg',
    )


@app.route('/novel/<path:novel_id>/cover', methods=['POST'])
@login_required
@admin_required
def update_novel_cover(novel_id):
    try:
        save_novel_cover(novel_id, request.files.get('cover_file'))
        flash('小说封面已更新')
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for('novel_detail', novel_id=novel_id))
