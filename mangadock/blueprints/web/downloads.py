# -*- coding: utf-8 -*-
"""下载 / 更新 / 任务（页面与任务状态接口）。"""
from datetime import datetime

from flask import (
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from mangadock.auth import admin_required, library_write_required, login_required, get_current_user
from mangadock.core import app
from mangadock.extensions import db
from mangadock.services.adult_content import is_adult_content_enabled, is_adult_content_enabled_for
from mangadock.services.download import (
    is_adult_content_blocked,
    is_supported_comic_url,
    normalize_target_input,
)
from mangadock.services.fanqie import classify_fanqie_target
from mangadock.services.fanqie_api import FanqieApiError
from mangadock.services.groups import (
    can_user_access_group,
    can_user_access_task,
    get_accessible_group_names,
    get_comic_group_map,
    normalize_group_name,
)
from mangadock.services.library import load_comic_mapping
from mangadock.services.tasks import (
    delete_finished_tasks,
    delete_task,
    get_all_tasks,
    get_task,
    normalize_comic_url_identity,
    update_task,
)
from mangadock.services.updates import (
    get_comic_update_mode,
    get_last_update_check_time,
    get_update_candidates,
    is_update_check_refreshing,
    queue_background_command,
    set_comic_update_mode,
)
from mangadock.services.workers import start_download_task, start_novel_task, start_update_task
from mangadock.settings import (
    ADULT_CONTENT_DISABLED_MESSAGE,
    COMIC_UPDATE_MODE_AUTO,
    china_tz,
)
from mangadock.blueprints.web.common import safe_print
from mangadock.utils import safe_int


@app.route('/download', methods=['GET', 'POST'])
@library_write_required
def download():
    current_user = get_current_user()
    adult_enabled_for_user = is_adult_content_enabled_for(current_user)
    if request.method == 'POST':
        comic_url = normalize_target_input(request.form.get('comic_url'))
        comic_format = safe_int(request.form.get('format'), default=2)

        if not is_supported_comic_url(comic_url):
            return render_template(
                'download.html',
                error='请输入有效的漫画或小说链接/ID（支持包子漫画、漫画柜、嬉皮漫畫、'
                      '番茄图片漫画、番茄小说'
                      + ('、MXS' if adult_enabled_for_user else '')
                      + '）',
                adult_content_enabled=adult_enabled_for_user,
            )

        normalized_url = normalize_comic_url_identity(comic_url)
        mapped_names = [
            comic_name
            for comic_name, mapped_url in load_comic_mapping().items()
            if normalize_comic_url_identity(mapped_url) == normalized_url
        ]
        comic_group_map = get_comic_group_map()
        if any(
            not can_user_access_group(
                normalize_group_name(comic_group_map.get(comic_name)) or '默认分组',
                current_user,
            )
            for comic_name in mapped_names
        ):
            return render_template(
                'download.html',
                error='当前账号无权访问该漫画',
                adult_content_enabled=adult_enabled_for_user,
            )

        if is_adult_content_blocked(comic_url, allow_adult=adult_enabled_for_user):
            return render_template(
                'download.html',
                error=ADULT_CONTENT_DISABLED_MESSAGE,
                adult_content_enabled=adult_enabled_for_user,
            )

        # 番茄小说与图片漫画共用 fanqienovel.com 域名：提交时判定类型再分流
        # （小说固定走 EPUB 小说流水线，漫画仍走原有图片漫画管线）。
        try:
            fanqie_target = classify_fanqie_target(comic_url)
        except (FanqieApiError, ValueError) as exc:
            return render_template(
                'download.html',
                error=str(exc) or '番茄作品解析失败',
                adult_content_enabled=adult_enabled_for_user,
            )

        if fanqie_target and fanqie_target['kind'] == 'comic':
            fanqie_identity = normalize_comic_url_identity(
                f"fanqie-comic://{fanqie_target['book_id']}"
            )
            fanqie_mapped_names = [
                comic_name
                for comic_name, mapped_url in load_comic_mapping().items()
                if normalize_comic_url_identity(mapped_url) == fanqie_identity
            ]
            fanqie_group_map = get_comic_group_map()
            if any(
                not can_user_access_group(
                    normalize_group_name(fanqie_group_map.get(comic_name)) or '默认分组',
                    current_user,
                )
                for comic_name in fanqie_mapped_names
            ):
                return render_template(
                    'download.html',
                    error='当前账号无权访问该漫画',
                    adult_content_enabled=adult_enabled_for_user,
                )

        if fanqie_target and fanqie_target['kind'] == 'novel':
            task_id, _reused = start_novel_task(
                fanqie_target['book_id'], title=fanqie_target['title'],
            )
            return redirect(url_for('progress', task_id=task_id))

        # 启动下载线程并获取任务ID（带创建者 18+ 覆盖授权快照）
        try:
            task_id = start_download_task(
                comic_url, comic_format,
                allow_adult=bool(current_user and current_user.can_view_adult),
                created_by_user_id=current_user.id if current_user else None,
            )
        except PermissionError:
            return render_template(
                'download.html', error='当前账号无权访问该漫画',
                adult_content_enabled=adult_enabled_for_user,
            ), 403

        # 重定向到进度页
        return redirect(url_for('progress', task_id=task_id))

    return render_template('download.html', adult_content_enabled=adult_enabled_for_user)

@app.route('/update', methods=['GET', 'POST'])
@library_write_required
def update():
    # 获取已下载的漫画列表
    current_user = get_current_user()
    comic_data = load_comic_mapping()
    comic_group_map = get_comic_group_map()
    accessible_groups = set(get_accessible_group_names(current_user))
    if not current_user.is_admin:
        comic_data = {
            comic_name: comic_url
            for comic_name, comic_url in comic_data.items()
            if (normalize_group_name(comic_group_map.get(comic_name)) or '默认分组') in accessible_groups
        }
    update_candidates = get_update_candidates()
    if not current_user.is_admin:
        update_candidates = [
            candidate for candidate in update_candidates
            if (
                normalize_group_name(comic_group_map.get(candidate.comic_name)) or '默认分组'
            ) in accessible_groups
        ]
    comic_list = list(comic_data.keys())
    comic_update_mode = get_comic_update_mode()

    if request.method == 'POST':
        comic_name = request.form.get('comic_name')
        comic_format = safe_int(request.form.get('format'), default=2)

        # 获取该漫画的URL
        comic_url = comic_data.get(comic_name)

        if not comic_url:
            return render_template(
                'update.html',
                comics=comic_list,
                update_candidates=update_candidates,
                update_checking=is_update_check_refreshing(),
                last_update_checked_at=get_last_update_check_time(),
                comic_update_mode=comic_update_mode,
                error="未找到该漫画的URL信息"
            )

        task_id = start_update_task(
            comic_name, comic_format, comic_url,
            created_by_user_id=current_user.id if current_user else None,
        )

        return redirect(url_for('progress', task_id=task_id))

    return render_template(
        'update.html',
        comics=comic_list,
        update_candidates=update_candidates,
        update_checking=is_update_check_refreshing(),
        last_update_checked_at=get_last_update_check_time(),
        comic_update_mode=comic_update_mode
    )


@app.route('/update/mode', methods=['POST'])
@login_required
@admin_required
def update_mode():
    try:
        mode = set_comic_update_mode(request.form.get('mode'))
        if mode == COMIC_UPDATE_MODE_AUTO:
            flash('已切换为自动更新：后续扫描发现更新会自动加入任务队列')
        else:
            flash('已切换为手动更新：扫描结果需要手动选择漫画更新')
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for('update'))


@app.route('/update/check', methods=['POST'])
@library_write_required
def check_updates_now():
    queue_background_command('refresh_update_checks', {'force': True}, dedupe_pending=True)
    flash('已加入更新检查队列，稍后刷新页面可查看最新结果')
    return redirect(url_for('update'))

@app.route('/delete_task/<task_id>', methods=['POST'])
@login_required
@admin_required
def delete_task_route(task_id):
    """删除任务的路由"""
    try:
        if delete_task(task_id):
            return jsonify({'status': 'success', 'message': '任务已删除'})
        else:
            return jsonify({'status': 'error', 'message': '任务不存在'}), 404
    except Exception as e:
        safe_print(f"删除任务失败: {e}")
        return jsonify({'status': 'error', 'message': '删除任务失败'}), 500


@app.route('/delete_tasks', methods=['POST'])
@login_required
@admin_required
def delete_tasks_route():
    """批量删除已结束的任务记录"""
    try:
        deleted_count, active_count = delete_finished_tasks()
        if deleted_count:
            message = f'已删除 {deleted_count} 条已结束任务记录'
        else:
            message = '暂无可删除的已结束任务记录'
        return jsonify({
            'status': 'success',
            'message': message,
            'deleted_count': deleted_count,
            'active_count': active_count
        })
    except Exception as e:
        db.session.rollback()
        safe_print(f"批量删除任务失败: {e}")
        return jsonify({'status': 'error', 'message': '删除任务失败'}), 500



@app.route('/progress/<task_id>')
@library_write_required
def progress(task_id):
    task = get_task(task_id)
    if not task:
        return render_template('error.html', message="任务不存在或已过期"), 404
    if not can_user_access_task(task, get_current_user()):
        return render_template('error.html', message="任务不存在或已过期"), 404
    from mangadock.services.fanqie import book_id_from_task_url

    novel_book_id = book_id_from_task_url(task.url)
    return render_template(
        'progress.html',
        task_id=task_id,
        is_novel_task=bool(novel_book_id),
        novel_cover_url=(
            url_for('fanqie_novel_book_cover', book_id=novel_book_id)
            if novel_book_id else ''
        ),
    )

@app.route('/task_status/<task_id>')
@library_write_required
def task_status(task_id):
    task = get_task(task_id)
    if not task or not can_user_access_task(task, get_current_user()):
        return jsonify({'error': '任务不存在'}), 404

    # 日志
    log_entries = []
    if task.log:
        log_entries = [line for line in task.log.split('\n') if line.strip()]

    return jsonify({
        'task_id': task.id,
        'comic_name': task.comic_name,
        'status': task.status,
        'progress_percent': task.progress_percent,
        'total_chapters': task.total_chapters,
        'completed_chapters': task.completed_chapters,
        'log': log_entries,
        'start_time': task.start_time.strftime('%H:%M:%S') if task.start_time else None,
        'end_time': task.end_time.strftime('%H:%M:%S') if task.end_time else None
    })

@app.route('/cancel_task/<task_id>', methods=['POST'])
@library_write_required
def cancel_task(task_id):
    task = get_task(task_id)
    if not task or not can_user_access_task(task, get_current_user()):
        return jsonify({'status': 'error', 'message': '任务不存在'}), 404
    if task and task.status in {'pending', 'running'}:
        update_task(task_id, status='cancelled', end_time=datetime.now(china_tz), log="用户已取消任务")
        return jsonify({'status': 'success', 'message': '任务已取消'})
    return jsonify({'status': 'error', 'message': '无法取消任务，任务可能已完成或不存在'})

@app.route('/tasks')
@library_write_required
def tasks():
    """显示所有任务列表"""
    current_user = get_current_user()
    group_map = get_comic_group_map()
    allowed_groups = set(get_accessible_group_names(current_user))
    all_tasks = [
        task for task in get_all_tasks()
        if can_user_access_task(task, current_user, group_map, allowed_groups)
    ]
    active_statuses = {'pending', 'running'}
    active_task_count = sum(1 for task in all_tasks if task.status in active_statuses)
    deletable_task_count = len(all_tasks) - active_task_count
    return render_template(
        'tasks.html',
        tasks=all_tasks,
        active_task_count=active_task_count,
        deletable_task_count=deletable_task_count
    )
