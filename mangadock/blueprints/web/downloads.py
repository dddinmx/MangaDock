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

from mangadock.auth import admin_required, login_required
from mangadock.core import app
from mangadock.extensions import db
from mangadock.services.adult_content import is_adult_content_enabled
from mangadock.services.download import (
    is_adult_content_blocked,
    is_supported_comic_url,
)
from mangadock.services.library import load_comic_mapping
from mangadock.services.tasks import (
    delete_finished_tasks,
    delete_task,
    get_all_tasks,
    get_task,
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
from mangadock.services.workers import start_download_task, start_update_task
from mangadock.settings import (
    ADULT_CONTENT_DISABLED_MESSAGE,
    COMIC_UPDATE_MODE_AUTO,
    china_tz,
)
from mangadock.blueprints.web.common import safe_print


@app.route('/download', methods=['GET', 'POST'])
@login_required
@admin_required
def download():
    if request.method == 'POST':
        comic_url = request.form.get('comic_url')
        comic_format = int(request.form.get('format', 2))

        if not is_supported_comic_url(comic_url):
            return render_template(
                'download.html',
                error='请输入有效的漫画链接或 ID（支持包子漫画、漫画柜、番茄图片漫画'
                      + ('、MXS' if is_adult_content_enabled() else '')
                      + '）',
                adult_content_enabled=is_adult_content_enabled(),
            )

        if is_adult_content_blocked(comic_url):
            return render_template(
                'download.html',
                error=ADULT_CONTENT_DISABLED_MESSAGE,
                adult_content_enabled=is_adult_content_enabled(),
            )

        # 启动下载线程并获取任务ID
        task_id = start_download_task(comic_url, comic_format)

        # 重定向到进度页
        return redirect(url_for('progress', task_id=task_id))

    return render_template('download.html', adult_content_enabled=is_adult_content_enabled())

@app.route('/update', methods=['GET', 'POST'])
@login_required
@admin_required
def update():
    # 获取已下载的漫画列表
    comic_data = load_comic_mapping()
    comic_list = list(comic_data.keys())
    comic_update_mode = get_comic_update_mode()

    if request.method == 'POST':
        comic_name = request.form.get('comic_name')
        comic_format = int(request.form.get('format', 2))

        # 获取该漫画的URL
        comic_url = comic_data.get(comic_name)

        if not comic_url:
            return render_template(
                'update.html',
                comics=comic_list,
                update_candidates=get_update_candidates(),
                update_checking=is_update_check_refreshing(),
                last_update_checked_at=get_last_update_check_time(),
                comic_update_mode=comic_update_mode,
                error="未找到该漫画的URL信息"
            )

        task_id = start_update_task(comic_name, comic_format, comic_url)

        return redirect(url_for('progress', task_id=task_id))

    return render_template(
        'update.html',
        comics=comic_list,
        update_candidates=get_update_candidates(),
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
@login_required
@admin_required
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
@login_required
@admin_required
def progress(task_id):
    task = get_task(task_id)
    if not task:
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
@login_required
@admin_required
def task_status(task_id):
    task = get_task(task_id)
    if not task:
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
@login_required
@admin_required
def cancel_task(task_id):
    task = get_task(task_id)
    if task and task.status in {'pending', 'running'}:
        update_task(task_id, status='cancelled', end_time=datetime.now(china_tz), log="用户已取消任务")
        return jsonify({'status': 'success', 'message': '任务已取消'})
    return jsonify({'status': 'error', 'message': '无法取消任务，任务可能已完成或不存在'})

@app.route('/tasks')
@login_required
@admin_required
def tasks():
    """显示所有任务列表"""
    all_tasks = get_all_tasks()
    active_statuses = {'pending', 'running'}
    active_task_count = sum(1 for task in all_tasks if task.status in active_statuses)
    deletable_task_count = len(all_tasks) - active_task_count
    return render_template(
        'tasks.html',
        tasks=all_tasks,
        active_task_count=active_task_count,
        deletable_task_count=deletable_task_count
    )
