# -*- coding: utf-8 -*-
"""Server-rendered routes for the local EPUB novel mode."""
from io import BytesIO
import time

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for

from mangadock.auth import admin_required, get_current_user, login_required
from mangadock.core import app
from mangadock.models import DownloadTask
from mangadock.pagination import paginate_sequence
from mangadock.services.groups import normalize_group_name
from mangadock.services.novel_groups import (
    assign_novels_to_group,
    delete_novel_group,
    enrich_novels_with_groups,
    ensure_novel_group,
    get_all_novel_groups,
    get_saved_novel_group_filter,
    save_novel_group_filter,
)
from mangadock.services.novels import (
    NOVEL_PROGRESS_PREFIX,
    get_novel,
    get_novel_by_fanqie_id,
    get_novel_chapter,
    get_novel_chapters,
    get_novel_cover_file,
    get_novels,
    novel_progress_key,
    save_novel_cover,
)
from mangadock.services.download import normalize_target_input
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
    novels, group_names, group_counts, grouped_lookup = enrich_novels_with_groups(novels)
    novels.sort(
        key=lambda novel: (
            0 if novel['progress'] else 1,
            -(novel['last_read_at'].timestamp() if novel['last_read_at'] else novel['created_at'].timestamp()),
        )
    )
    requested_group = request.args.get('group')
    if requested_group is None:
        selected_group = get_saved_novel_group_filter(session.get('user_id'))
    else:
        selected_group = normalize_group_name(requested_group) or '全部'
    if selected_group != '全部' and selected_group not in grouped_lookup:
        selected_group = '全部'
    save_novel_group_filter(selected_group, session.get('user_id'))

    selected_novels = novels if selected_group == '全部' else [
        novel for novel in novels if novel.get('group') == selected_group
    ]
    novels, pagination = paginate_sequence(selected_novels, request.args.get('page'))
    return render_template(
        'novels.html',
        library_mode='novel',
        novels=novels,
        pagination=pagination,
        groups=group_names,
        group_counts=group_counts,
        selected_group=selected_group,
        current_user=get_current_user(),
    )


@app.route('/novels/groups')
@login_required
@admin_required
def novel_group_manager():
    novels, groups, group_counts, _ = enrich_novels_with_groups(get_novels())
    novels.sort(key=lambda item: (item.get('group') or '', item.get('title') or ''))
    return render_template(
        'novel_groups.html',
        library_mode='novel',
        novels=novels,
        groups=groups,
        group_counts=group_counts,
        current_user=get_current_user(),
    )


@app.route('/novels/groups/create', methods=['POST'])
@login_required
@admin_required
def create_novel_group():
    group_name = normalize_group_name(request.form.get('group_name'))
    if not group_name:
        flash('分组名称不能为空')
    elif group_name == '全部':
        flash('“全部”是系统筛选项，不能作为分组名称')
    elif group_name in set(get_all_novel_groups()):
        flash('分组已存在')
    elif ensure_novel_group(group_name):
        flash(f'已创建小说分组：{group_name}')
    else:
        flash('创建分组失败，请重试')
    return redirect(url_for('novel_group_manager'))


@app.route('/novels/groups/assign', methods=['POST'])
@login_required
@admin_required
def batch_assign_novel_group():
    group_name = normalize_group_name(request.form.get('group_name'))
    selected_ids = request.form.getlist('novel_ids')
    valid_ids = {novel['novel_id'] for novel in get_novels()}
    if group_name not in set(get_all_novel_groups()):
        flash('请选择有效的目标分组')
    elif not selected_ids:
        flash('请至少选择一本小说')
    else:
        assigned_count = assign_novels_to_group(selected_ids, group_name, valid_ids)
        if assigned_count:
            flash(f'已将 {assigned_count} 本小说加入「{group_name}」')
        else:
            flash('没有可分配的小说，请重新选择')
    return redirect(url_for('novel_group_manager'))


@app.route('/novels/groups/delete', methods=['POST'])
@login_required
@admin_required
def remove_novel_group():
    group_name = normalize_group_name(request.form.get('group_name'))
    if not group_name:
        flash('请选择要删除的分组')
    elif group_name == '默认分组':
        flash('默认分组不能删除')
    elif delete_novel_group(group_name):
        flash(f'已删除分组「{group_name}」，其中小说已移回默认分组')
    else:
        flash('分组不存在或删除失败')
    return redirect(url_for('novel_group_manager'))


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
    try:
        from mangadock.services.fanqie_api import check_api_health
        fanqie_api_health = check_api_health()
    except Exception:
        fanqie_api_health = {'ok': False, 'message': '番茄中转服务状态检查失败'}
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
        fanqie_api_health=fanqie_api_health,
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
    target = normalize_target_input(
        request.form.get('book_id') or request.form.get('target') or ''
    )
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
    cover = None
    for delay in (0.0, 0.5, 1.5):  # SMB 瞬时抖动重试（2026-09-20）
        if delay:
            time.sleep(delay)
        cover = get_novel_cover_file(novel_id)
        if cover:
            break
    if not cover:
        resp = app.send_static_file('cover/cover.png')
        # 占位图禁止长缓存：真封面就绪后客户端要能立即换新（2026-09-20 P2）
        resp.headers['Cache-Control'] = 'no-cache, max-age=0'
        return resp
    cover_path, mimetype = cover
    response = send_file(
        cover_path,
        mimetype=mimetype,
        conditional=True,
        etag=True,
        max_age=31536000,
        download_name=f'{novel_id}.jpg',
    )
    response.cache_control.public = False
    response.cache_control.private = True
    response.cache_control.immutable = True
    return response


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
