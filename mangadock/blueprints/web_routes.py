# -*- coding: utf-8 -*-
"""Server-rendered web routes."""
import os
from datetime import datetime, timedelta

from flask import (
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from sqlalchemy import or_

from mangadock.auth import (
    require_csrf_for_session_auth,
    admin_required,
    establish_user_session,
    get_current_user,
    get_login_redirect_target,
    is_safe_next_target,
    login_failure_key,
    login_required,
    record_login_log,
    save_uploaded_cover_image,
    validate_user_credentials,
)
from mangadock.core import app
from mangadock.extensions import csrf, db, login_failures
from mangadock.models import (
    ComicScanPath,
    DownloadTask,
    LoginLog,
    ReadingProgress,
    ReadingSessionState,
    ReadingTime,
    User,
    UserGroupPermission,
)
from mangadock.services.download import (
    is_supported_comic_url,
    load_comic_source,
    refresh_comic_description,
)
from mangadock.services.groups import (
    assign_comic_group,
    can_user_access_group,
    delete_comic_group,
    enrich_comics_with_groups,
    ensure_comic_group,
    filter_grouped_comics_for_user,
    get_accessible_group_names,
    get_admin_hidden_targets,
    get_all_comic_groups,
    get_comic_group_map,
    get_group_filter_session_key,
    get_saved_group_filter,
    get_user_group_permissions,
    is_comic_hidden_for_admin,
    normalize_group_name,
    save_group_filter,
    set_admin_hidden_item,
    set_user_group_permissions,
)
from mangadock.services.library import (
    chapter_display_title,
    detect_local_comic_format,
    get_available_comics,
    get_comic_description,
    get_comic_descriptions,
    get_comic_directory,
    get_scan_path_entries,
    invalidate_comics_cache,
    is_safe_comic_name,
    list_local_chapters,
    load_comic_mapping,
    normalize_scan_path,
    refresh_comics_cache,
    resolve_comic_file_request,
    save_comic_mapping,
)
from mangadock.services.reading import (
    display_minutes_from_seconds,
    get_all_reading_progress,
    get_reading_progress,
    get_reading_time_by_comic,
    get_reading_time_daily_for_year,
    get_reading_time_for_comic,
    get_reading_time_monthly_for_year,
    get_total_reading_time,
    record_reading_time,
    save_reading_progress,
)
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
    get_update_check_map,
    is_update_check_refreshing,
    queue_background_command,
    schedule_update_checks_refresh,
    set_comic_update_mode,
)
from mangadock.services.workers import start_download_task, start_update_task
from mangadock.settings import (
    COMIC_ROOT,
    COMIC_UPDATE_MODE_AUTO,
    COMIC_UPDATE_MODE_MANUAL,
    COMIC_UPDATE_MODES,
    COVER_ROOT,
    LOGIN_LOCKOUT_DURATION,
    LOGIN_MAX_ATTEMPTS,
    china_tz,
)
from mangadock.utils.media import repair_pdf_for_reading


def safe_print(message, end="\n", flush=False):
    print(message, end=end, flush=flush)


@app.route('/login', methods=['GET', 'POST'])
def login():
    """用户登录页面"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        # 获取用户IP和User-Agent
        ip_address = request.remote_addr
        user_agent = request.user_agent.string
        failure_key = login_failure_key(username, ip_address)

        # 检查登录失败次数和锁定状态
        if failure_key in login_failures:
            fail_count, lock_time = login_failures[failure_key]
            if fail_count >= LOGIN_MAX_ATTEMPTS:
                # 检查锁定是否已过期
                if datetime.now(china_tz) - lock_time < LOGIN_LOCKOUT_DURATION:
                    remaining_time = LOGIN_LOCKOUT_DURATION - (datetime.now(china_tz) - lock_time)
                    flash(f'登录失败次数过多，请在 {remaining_time.seconds // 60} 分钟后重试')
                    # 记录登录失败日志
                    login_log = LoginLog(
                        username=username,
                        ip_address=ip_address,
                        user_agent=user_agent,
                        success=False,
                        message=f'账户已锁定，剩余锁定时间 {remaining_time.seconds // 60} 分钟'
                    )
                    db.session.add(login_log)
                    db.session.commit()
                    return render_template('login.html')
                else:
                    # 锁定过期，重置失败计数
                    del login_failures[failure_key]

        # 验证用户信息
        user = User.query.filter_by(username=username).first()
        if user:
            login_success = user.check_password(password)

            if login_success:
                # 登录成功，清除失败计数
                if failure_key in login_failures:
                    del login_failures[failure_key]
                # 登录成功，保存用户ID到session
                session.permanent = True
                session['user_id'] = user.id
                session['username'] = user.username
                session['user_role'] = user.role
                # 记录登录成功日志
                login_log = LoginLog(
                    username=username,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    success=True,
                    message='登录成功'
                )
                db.session.add(login_log)
                db.session.commit()
                # 重定向到之前访问的页面（如果有），否则跳转到首页
                next_page = request.args.get('next')
                if not is_safe_next_target(next_page):
                    next_page = url_for('index')
                return redirect(next_page or url_for('index'))
            else:
                # 用户存在但密码错误，记录失败次数
                if failure_key not in login_failures:
                    login_failures[failure_key] = (0, datetime.now(china_tz))
                fail_count, lock_time = login_failures[failure_key]
                new_fail_count = fail_count + 1
                login_failures[failure_key] = (new_fail_count, datetime.now(china_tz))

                # 显示剩余尝试次数
                remaining_attempts = LOGIN_MAX_ATTEMPTS - new_fail_count
                if remaining_attempts > 0:
                    flash(f'用户名或密码错误，还有 {remaining_attempts} 次尝试机会')
                    message = f'用户名或密码错误，剩余 {remaining_attempts} 次尝试机会'
                else:
                    flash(f'登录失败次数过多，账户已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟')
                    message = f'登录失败次数过多，账户已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟'

                # 记录登录失败日志
                login_log = LoginLog(
                    username=username,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    success=False,
                    message=message
                )
                db.session.add(login_log)
                db.session.commit()

                return render_template('login.html')
        else:
            # 用户不存在，记录失败次数
            if failure_key not in login_failures:
                login_failures[failure_key] = (0, datetime.now(china_tz))
            fail_count, lock_time = login_failures[failure_key]
            new_fail_count = fail_count + 1
            login_failures[failure_key] = (new_fail_count, datetime.now(china_tz))

            # 显示剩余尝试次数
            remaining_attempts = LOGIN_MAX_ATTEMPTS - new_fail_count
            if remaining_attempts > 0:
                flash(f'用户名或密码错误，还有 {remaining_attempts} 次尝试机会')
                message = f'用户名或密码错误，剩余 {remaining_attempts} 次尝试机会'
            else:
                flash(f'登录失败次数过多，账户已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟')
                message = f'登录失败次数过多，账户已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟'

            # 记录登录失败日志
            login_log = LoginLog(
                username=username,
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                message=message
            )
            db.session.add(login_log)
            db.session.commit()

            return render_template('login.html')

    # GET请求，返回登录页面
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    """用户登出：清除session中的登录信息"""
    session.pop('user_id', None)
    session.pop('username', None)
    session.pop('user_role', None)
    flash('已成功登出')
    return redirect(url_for('login'))

def _build_home_spotlight(user_id):
    """Pick continue-reading comic for homepage hero."""
    current_user = get_current_user()
    comics = get_available_comics() or []
    # Apply membership groups + per-user group permissions (bookshelf already does this).
    comics, _, _, _ = filter_grouped_comics_for_user(comics, current_user)

    # Homepage always hides admin-hidden comics/groups (no show_hidden toggle here).
    hidden_comics, hidden_groups = get_admin_hidden_targets(current_user)
    comics = [
        comic for comic in comics
        if not is_comic_hidden_for_admin(comic, hidden_comics, hidden_groups)
    ]
    comics, group_names, _group_counts, grouped_lookup = filter_grouped_comics_for_user(
        comics, current_user
    )
    if current_user and current_user.is_admin and hidden_groups:
        group_names = [name for name in group_names if name not in hidden_groups]
        for name in list(grouped_lookup.keys()):
            if name in hidden_groups:
                grouped_lookup.pop(name, None)

    comic_lookup = {c.get('comic_name'): c for c in comics if c.get('comic_name')}
    progresses = get_all_reading_progress(user_id) or []

    spotlight = None
    for progress in progresses:
        comic = comic_lookup.get(progress.comic_name)
        if not comic:
            continue
        spotlight = {
            'comic_name': progress.comic_name,
            'comic_format': comic.get('comic_format'),
            'group': comic.get('group') or '默认分组',
            'available_chapters': comic.get('available_chapters') or comic.get('total_chapters') or 0,
            'last_chapter': progress.last_chapter or 0,
            'last_page': progress.last_page or 0,
            'last_read_at': progress.last_read_at,
            'has_progress': True,
        }
        break

    if not spotlight and comics:
        # Fallback: most recently created / first in library list
        sorted_comics = sorted(
            comics,
            key=lambda c: (c.get('created_at') or datetime.min.replace(tzinfo=china_tz)),
            reverse=True,
        )
        comic = sorted_comics[0]
        spotlight = {
            'comic_name': comic.get('comic_name'),
            'comic_format': comic.get('comic_format'),
            'group': comic.get('group') or '默认分组',
            'available_chapters': comic.get('available_chapters') or comic.get('total_chapters') or 0,
            'last_chapter': 0,
            'last_page': 0,
            'last_read_at': None,
            'has_progress': False,
        }

    # 批量注入简介；首页焦点位缺简介时尝试从源站补全一次
    desc_names = set()
    if spotlight and spotlight.get('comic_name'):
        desc_names.add(spotlight['comic_name'])
    for progress in progresses[:12]:
        if progress.comic_name:
            desc_names.add(progress.comic_name)
    descriptions = get_comic_descriptions(desc_names)

    if spotlight:
        name = spotlight.get('comic_name')
        description = descriptions.get(name) or ''
        if not description and name:
            description = refresh_comic_description(name) or ''
            if description:
                descriptions[name] = description
        spotlight['description'] = description

    recent = []
    for progress in progresses[:12]:
        comic = comic_lookup.get(progress.comic_name)
        if comic:
            recent.append({
                **comic,
                'last_chapter': progress.last_chapter or 0,
                'last_read_at': progress.last_read_at,
                'description': descriptions.get(progress.comic_name, ''),
            })

    # Fresh library items not in recent
    recent_names = {item.get('comic_name') for item in recent}
    latest = [c for c in comics if c.get('comic_name') not in recent_names][:12]

    # Prefer ComicGroup order so rails match bookshelf grouping.
    group_rails = [
        {'name': name, 'comics': (grouped_lookup.get(name) or [])[:10]}
        for name in group_names
        if grouped_lookup.get(name)
    ][:6]

    return spotlight, recent, latest, group_rails


@app.route('/')
@login_required
def index():
    current_user = get_current_user()
    user_id = session.get('user_id')
    spotlight, recent, latest, group_rails = _build_home_spotlight(user_id)
    return render_template(
        'index.html',
        spotlight=spotlight,
        recent_comics=recent,
        latest_comics=latest,
        group_rails=group_rails,
        current_user=current_user,
    )


@app.route('/search')
@login_required
def search_page():
    # 独立搜索页已下线；保留路由以免旧链接 404，统一回到书架（书架内联搜索仍可用）
    return redirect(url_for('comics_list'))


@app.route('/history')
@login_required
def history_page():
    user_id = session.get('user_id')
    comics = get_available_comics() or []
    comic_lookup = {c.get('comic_name'): c for c in comics}
    progresses = get_all_reading_progress(user_id) or []
    items = []
    for progress in progresses:
        comic = comic_lookup.get(progress.comic_name)
        if not comic:
            continue
        items.append({
            **comic,
            'last_chapter': progress.last_chapter or 0,
            'last_page': progress.last_page or 0,
            'last_read_at': progress.last_read_at,
        })
    return render_template('history.html', items=items)

@app.route('/download', methods=['GET', 'POST'])
@login_required
@admin_required
def download():
    if request.method == 'POST':
        comic_url = request.form.get('comic_url')
        comic_format = int(request.form.get('format', 2))

        if not is_supported_comic_url(comic_url):
            return render_template('download.html', error='请输入有效的漫画详情页URL（支持 baozimh.com、baozimhcn.com、baozimh.org 或 mxs12.cc）')

        # 启动下载线程并获取任务ID
        task_id = start_download_task(comic_url, comic_format)

        # 重定向到进度页
        return redirect(url_for('progress', task_id=task_id))

    return render_template('download.html')

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
        return jsonify({'status': 'error', 'message': str(e)}), 500


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
        return jsonify({'status': 'error', 'message': str(e)}), 500



@app.route('/progress/<task_id>')
@login_required
@admin_required
def progress(task_id):
    task = get_task(task_id)
    if not task:
        return render_template('error.html', message="任务不存在或已过期"), 404
    return render_template('progress.html', task_id=task_id)

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


@app.route('/users', methods=['GET', 'POST'])
@login_required
@admin_required
def users():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        confirm_password = request.form.get('confirm_password') or ''
        role = (request.form.get('role') or 'user').strip()
        selected_groups = request.form.getlist('group_names')

        if not username:
            flash('用户名不能为空')
            return redirect(url_for('users'))
        if len(username) < 3:
            flash('用户名长度不能少于3位')
            return redirect(url_for('users'))
        if password != confirm_password:
            flash('两次输入的密码不一致')
            return redirect(url_for('users'))
        if len(password) < 6:
            flash('密码长度不能少于6位')
            return redirect(url_for('users'))
        if role not in {'admin', 'user'}:
            flash('无效的用户角色')
            return redirect(url_for('users'))
        if User.query.filter_by(username=username).first():
            flash('用户名已存在')
            return redirect(url_for('users'))

        user = User(username=username, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        if role == 'user':
            set_user_group_permissions(user.id, selected_groups)
        flash(f'已创建用户：{username}')
        return redirect(url_for('users'))

    all_users = User.query.order_by(
        db.case((User.role == 'admin', 0), else_=1),
        User.username.asc()
    ).all()
    all_groups = get_all_comic_groups()
    user_group_permissions = {
        user.id: set(get_user_group_permissions(user.id))
        for user in all_users
        if not user.is_admin
    }
    return render_template(
        'users.html',
        users=all_users,
        groups=all_groups,
        user_group_permissions=user_group_permissions
    )


@app.route('/users/<int:user_id>/groups', methods=['POST'])
@login_required
@admin_required
def update_user_groups(user_id):
    user = db.session.get(User, user_id)

    if not user:
        flash('用户不存在')
        return redirect(url_for('users'))

    if user.is_admin:
        flash('管理员默认拥有全部分组权限，无需单独授权')
        return redirect(url_for('users'))

    selected_groups = request.form.getlist('group_names')
    saved_groups = set_user_group_permissions(user.id, selected_groups)
    if saved_groups:
        flash(f'已更新 {user.username} 的分组权限')
    else:
        flash(f'已清空 {user.username} 的分组权限')
    return redirect(url_for('users'))


@app.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    current_user = get_current_user()
    user = db.session.get(User, user_id)

    if not user:
        flash('用户不存在')
        return redirect(url_for('users'))

    if current_user and user.id == current_user.id:
        flash('不能删除当前登录账号')
        return redirect(url_for('users'))

    if user.role == 'admin':
        flash('不能删除管理员账号')
        return redirect(url_for('users'))

    db.session.query(ReadingProgress).filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.query(ReadingTime).filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.query(ReadingSessionState).filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.query(UserGroupPermission).filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.query(LoginLog).filter_by(username=user.username).delete(synchronize_session=False)
    db.session.delete(user)
    db.session.commit()

    flash(f'已删除用户：{user.username}')
    return redirect(url_for('users'))

@app.route('/comics')
@login_required
def comics_list():
    """显示漫画库页面"""
    current_user = get_current_user()
    comics = get_available_comics()
    progresses = {}
    current_user_id = session.get('user_id')
    can_show_hidden_library = bool(current_user and current_user.is_admin)
    show_hidden_library = can_show_hidden_library and request.args.get('show_hidden') == '1'
    search_query = (request.args.get('q') or '').strip()
    requested_group = request.args.get('group')
    if search_query and requested_group is None:
        # 顶栏搜索默认在全部分组中过滤
        group_filter = '全部'
    elif requested_group is None:
        group_filter = get_saved_group_filter(current_user_id)
    else:
        group_filter = normalize_group_name(requested_group) or '全部'

    # 获取所有阅读进度
    all_progress = get_all_reading_progress(current_user_id)
    for progress in all_progress:
        progresses[progress.comic_name] = {
            'last_chapter': progress.last_chapter,
            'last_page': progress.last_page,
            'total_chapters': progress.total_chapters,
            'total_pages': progress.total_pages,
            'last_read_at': progress.last_read_at
        }

    # 对漫画列表进行排序：有阅读进度的漫画优先显示，并且按最后阅读时间倒序排列
    # 没有阅读进度的漫画放在后面，按创建时间倒序排列
    def sort_key(comic):
        # 先判断是否有阅读进度
        if comic['comic_name'] in progresses:
            # 有阅读进度的漫画，按最后阅读时间倒序（最新阅读的在前）
            last_read_at = progresses[comic['comic_name']]['last_read_at']
            # 如果没有 last_read_at，使用创建时间
            if last_read_at:
                # 为了在升序排序中让最新的在前，我们使用负的时间戳
                return (0, -last_read_at.timestamp())
            elif comic['created_at']:
                return (0, -comic['created_at'].timestamp())
            else:
                return (0, float('-inf'))
        else:
            # 没有阅读进度的漫画，按创建时间倒序
            if comic['created_at']:
                return (1, -comic['created_at'].timestamp())
            else:
                return (1, float('-inf'))

    # 使用升序排序
    sorted_comics = sorted(comics, key=sort_key)

    sorted_comics, _, _, _ = filter_grouped_comics_for_user(sorted_comics, current_user)
    hidden_comic_names, hidden_group_names = get_admin_hidden_targets(current_user)
    hidden_library_count = 0
    for comic in sorted_comics:
        comic['is_admin_hidden'] = can_show_hidden_library and is_comic_hidden_for_admin(
            comic,
            hidden_comic_names,
            hidden_group_names
        )
        if comic['is_admin_hidden']:
            hidden_library_count += 1

    display_comics = sorted_comics if show_hidden_library else [
        comic for comic in sorted_comics if not comic.get('is_admin_hidden')
    ]
    display_comics, group_names, group_counts, grouped_lookup = filter_grouped_comics_for_user(display_comics, current_user)

    if can_show_hidden_library and not show_hidden_library and hidden_group_names:
        group_names = [group_name for group_name in group_names if group_name not in hidden_group_names]
        for group_name in list(group_counts.keys()):
            if group_name in hidden_group_names:
                group_counts.pop(group_name, None)
        for group_name in list(grouped_lookup.keys()):
            if group_name in hidden_group_names:
                grouped_lookup.pop(group_name, None)

    ordered_group_names = []
    seen_group_names = set()
    for comic in display_comics:
        comic_group = normalize_group_name(comic.get('group')) or '默认分组'
        if comic_group in group_names and comic_group not in seen_group_names:
            ordered_group_names.append(comic_group)
            seen_group_names.add(comic_group)

    for group_name in group_names:
        if group_name not in seen_group_names:
            ordered_group_names.append(group_name)

    group_names = ordered_group_names

    if group_filter != '全部' and group_filter not in grouped_lookup:
        group_filter = '全部'

    save_group_filter(group_filter, current_user_id)

    grouped_tasks = []
    if group_filter == '全部':
        for group_name in group_names:
            comics_in_group = grouped_lookup.get(group_name, [])
            if comics_in_group:
                grouped_tasks.append({
                    'name': group_name,
                    'tasks': comics_in_group
                })
    else:
        grouped_tasks.append({
            'name': group_filter,
            'tasks': grouped_lookup.get(group_filter, [])
        })

    recent_comic = None
    recent_progress = None
    for comic in display_comics:
        progress_info = progresses.get(comic['comic_name'])
        if progress_info and progress_info.get('last_read_at'):
            recent_comic = comic
            recent_progress = progress_info
            break

    return render_template(
        'comics.html',
        tasks=display_comics,
        grouped_tasks=grouped_tasks,
        recent_comic=recent_comic,
        recent_progress=recent_progress,
        progresses=progresses,
        groups=group_names,
        group_counts=group_counts,
        group_total_count=len(display_comics),
        selected_group=group_filter,
        show_group_menu=bool(group_names),
        group_menu_return_to='comics',
        show_hidden_library=show_hidden_library,
        hidden_library_count=hidden_library_count,
        hidden_comic_names=hidden_comic_names,
        hidden_group_names=hidden_group_names
    )


def redirect_after_hidden_library_change(return_to, redirect_group='全部', task_id=None, show_hidden=False):
    if return_to == 'detail' and task_id:
        return redirect(url_for('comic_detail', task_id=task_id))

    route_kwargs = {}
    normalized_group = normalize_group_name(redirect_group) or '全部'
    if normalized_group:
        route_kwargs['group'] = normalized_group
    if show_hidden:
        route_kwargs['show_hidden'] = '1'
    return redirect(url_for('comics_list', **route_kwargs))


@app.route('/admin_hidden/toggle_comic', methods=['POST'])
@login_required
@admin_required
def toggle_admin_hidden_comic():
    current_user = get_current_user()
    comic_name = (request.form.get('comic_name') or '').strip()
    action = request.form.get('action')
    return_to = request.form.get('return_to')
    redirect_group = normalize_group_name(request.form.get('redirect_group', '全部')) or '全部'
    return_task_id = (request.form.get('task_id') or comic_name).strip()
    show_hidden = request.form.get('show_hidden') == '1'

    if not comic_name or not get_comic_directory(comic_name):
        flash('漫画不存在，无法更新隐藏状态')
        return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)

    if action not in {'hide', 'show'}:
        flash('隐藏操作无效')
        return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)

    set_admin_hidden_item(current_user.id, 'comic', comic_name, action == 'hide')
    flash(f'《{comic_name}》已{"加入隐藏区域" if action == "hide" else "移出隐藏区域"}')
    return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)


@app.route('/admin_hidden/toggle_group', methods=['POST'])
@login_required
@admin_required
def toggle_admin_hidden_group():
    current_user = get_current_user()
    group_name = normalize_group_name(request.form.get('group_name'))
    action = request.form.get('action')
    return_to = request.form.get('return_to')
    redirect_group = normalize_group_name(request.form.get('redirect_group', '全部')) or '全部'
    return_task_id = (request.form.get('task_id') or '').strip()
    show_hidden = request.form.get('show_hidden') == '1'

    if not group_name or group_name not in set(get_all_comic_groups()):
        flash('分组不存在，无法更新隐藏状态')
        return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)

    if action not in {'hide', 'show'}:
        flash('隐藏操作无效')
        return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)

    set_admin_hidden_item(current_user.id, 'group', group_name, action == 'hide')
    flash(f'分组「{group_name}」已{"加入隐藏区域" if action == "hide" else "移出隐藏区域"}')
    return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)


@app.route('/comic_groups', methods=['POST'])
@login_required
@admin_required
def create_comic_group_route():
    group_name = normalize_group_name(request.form.get('group_name'))
    redirect_group = normalize_group_name(request.form.get('redirect_group', '全部')) or '全部'
    return_to = request.form.get('return_to')
    return_task_id = (request.form.get('task_id') or '').strip()

    if not group_name:
        flash('分组名称不能为空')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    if group_name == '全部':
        flash('“全部”是系统筛选项，不能作为分组名称')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    existing_groups = set(get_all_comic_groups())
    created_group = ensure_comic_group(group_name)

    if not created_group:
        flash('分组名称无效')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    if created_group in existing_groups:
        flash('分组已存在')
    else:
        flash(f'已创建分组：{created_group}')

    save_group_filter(created_group)

    if return_to == 'detail' and return_task_id:
        return redirect(url_for('comic_detail', task_id=return_task_id))
    return redirect(url_for('comics_list', group=created_group))


@app.route('/comic_groups/assign', methods=['POST'])
@login_required
@admin_required
def assign_comic_group_route():
    comic_name = (request.form.get('comic_name') or '').strip()
    group_name = normalize_group_name(request.form.get('group_name'))
    redirect_group = normalize_group_name(request.form.get('redirect_group', '全部')) or '全部'
    return_to = request.form.get('return_to')
    return_task_id = (request.form.get('task_id') or comic_name).strip()

    if not comic_name or not get_comic_directory(comic_name):
        flash('漫画不存在，无法设置分组')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    if not group_name:
        flash('请选择一个分组')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    if not assign_comic_group(comic_name, group_name):
        flash('设置分组失败，请重试')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    flash(f'《{comic_name}》已加入 {group_name}')
    if return_to == 'detail' and return_task_id:
        return redirect(url_for('comic_detail', task_id=return_task_id))
    target_group = redirect_group if redirect_group != '全部' else group_name
    save_group_filter(target_group)
    return redirect(url_for('comics_list', group=target_group))


@app.route('/comic_groups/delete', methods=['POST'])
@login_required
@admin_required
def delete_comic_group_route():
    group_name = normalize_group_name(request.form.get('group_name'))
    redirect_group = normalize_group_name(request.form.get('redirect_group', '全部')) or '全部'
    return_to = request.form.get('return_to')
    return_task_id = (request.form.get('task_id') or '').strip()

    if not group_name:
        flash('请选择要删除的分组')
    elif group_name == '默认分组':
        flash('默认分组不能删除')
    elif not delete_comic_group(group_name):
        flash('删除分组失败，请重试')
    else:
        flash(f'已删除分组：{group_name}')
        if redirect_group == group_name:
            redirect_group = '全部'
        save_group_filter(redirect_group)

    if return_to == 'detail' and return_task_id:
        return redirect(url_for('comic_detail', task_id=return_task_id))
    return redirect(url_for('comics_list', group=redirect_group))


@app.route('/statistics')
@login_required
def statistics():
    """Reading statistics page"""
    stats_year = datetime.now(china_tz).year
    current_user_id = session.get('user_id')
    total_time = get_total_reading_time(current_user_id)
    reading_time_rank = get_reading_time_by_comic(current_user_id)
    reading_time_monthly = get_reading_time_monthly_for_year(stats_year, current_user_id)
    reading_time_daily = get_reading_time_daily_for_year(stats_year, current_user_id)

    # Process monthly data for chart rendering
    monthly_data = {
        'months': ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'],
        'durations': [0] * 12  # Initialize with 0 for all months
    }

    for entry in reading_time_monthly:
        month = entry[0]  # Format: YYYY-MM
        duration = entry[1]  # minutes
        month_num = int(month.split('-')[1]) - 1  # Convert to 0-based index
        if 0 <= month_num < 12:
            monthly_data['durations'][month_num] = duration

    # Calculate max duration for chart scaling
    max_duration = max(monthly_data['durations']) if monthly_data['durations'] else 1

    daily_duration_map = {
        entry[0]: int(entry[1] or 0)
        for entry in reading_time_daily
    }
    max_daily_duration = max(daily_duration_map.values()) if daily_duration_map else 0

    year_start = datetime(stats_year, 1, 1).date()
    year_end = datetime(stats_year, 12, 31).date()
    grid_start = year_start - timedelta(days=year_start.weekday())
    grid_end = year_end + timedelta(days=(6 - year_end.weekday()))
    today = datetime.now(china_tz).date()

    month_labels = []
    heatmap_weeks = []
    cursor = grid_start
    week_index = 0

    while cursor <= grid_end:
        week_cells = []
        week_month_label = ''

        for day_offset in range(7):
            current_day = cursor + timedelta(days=day_offset)
            iso_day = current_day.isoformat()
            duration = daily_duration_map.get(iso_day, 0)

            if duration <= 0 or max_daily_duration <= 0:
                level = 0
            else:
                ratio = duration / max_daily_duration
                if ratio <= 0.25:
                    level = 1
                elif ratio <= 0.5:
                    level = 2
                elif ratio <= 0.75:
                    level = 3
                else:
                    level = 4

            if current_day.day == 1 and current_day.month <= 12:
                week_month_label = f"{current_day.month}月"
            elif week_index == 0 and day_offset == 0:
                week_month_label = '1月'

            week_cells.append({
                'date': iso_day,
                'day': current_day.day,
                'duration': duration,
                'level': level,
                'is_current_year': current_day.year == stats_year,
                'is_today': current_day == today
            })

        month_labels.append(week_month_label)
        heatmap_weeks.append(week_cells)
        cursor += timedelta(days=7)
        week_index += 1

    return render_template(
        'statistics.html',
        stats_year=stats_year,
        total_time=total_time,
        reading_time_rank=reading_time_rank,
        monthly_data=monthly_data,
        max_duration=max_duration,
        heatmap_weeks=heatmap_weeks,
        heatmap_month_labels=month_labels,
        max_daily_duration=max_daily_duration
    )

@app.route('/comic/<task_id>')
@login_required
def comic_detail(task_id):
    """漫画详情页面 - 显示漫画信息和章节列表"""
    import os
    current_user = get_current_user()

    # 尝试通过任务ID获取任务
    task = get_task(task_id)
    if task:
        comic_name = task.comic_name
        comic_format = task.comic_format
    else:
        # 如果任务不存在，尝试将task_id作为漫画名称处理
        comic_name = task_id

        # 检查漫画文件是否存在
        comic_path = get_comic_directory(comic_name)
        if not comic_path or not os.path.exists(comic_path):
            return render_template('error.html', message="漫画不存在"), 404

        comic_format = detect_local_comic_format(comic_path)
        if not comic_format:
            return render_template('error.html', message="不支持的漫画格式"), 404

        # 创建一个模拟的task对象
        class MockTask:
            def __init__(self, comic_name, comic_format):
                self.id = comic_name
                self.comic_name = comic_name
                self.comic_format = comic_format
                self.status = 'completed'
                self.total_chapters = 0
                self.completed_chapters = 0
                self.available_chapters = 0
                self.created_at = None
                self.url = None
                self.group = '默认分组'  # 默认分组

        # 计算可用章节数
        mock_task = MockTask(comic_name, comic_format)
        local_chapters = list_local_chapters(comic_name)
        mock_task.available_chapters = len(local_chapters)
        mock_task.total_chapters = mock_task.available_chapters
        mock_task.completed_chapters = mock_task.available_chapters
        task = mock_task

    # 获取章节列表
    try:
        chapters = list_local_chapters(task.comic_name)
    except Exception as e:
        return render_template('error.html', message=f'获取章节列表失败: {str(e)}'), 500

    task.available_chapters = len(chapters)
    task.total_chapters = max(getattr(task, 'total_chapters', 0), len(chapters))
    task.completed_chapters = max(getattr(task, 'completed_chapters', 0), len(chapters))

    # 获取阅读进度
    current_user_id = session.get('user_id')
    progress = get_reading_progress(task.comic_name, current_user_id)
    # 获取阅读时间（分钟）
    reading_time = get_reading_time_for_comic(task.comic_name, current_user_id)
    current_group = get_comic_group_map().get(task.comic_name) or getattr(task, 'group', None) or '默认分组'
    if not can_user_access_group(current_group, current_user):
        flash('当前账号未被授权访问该分组漫画')
        return redirect(url_for('comics_list'))

    hidden_comic_names, hidden_group_names = get_admin_hidden_targets(current_user)
    is_current_comic_directly_hidden = task.comic_name in hidden_comic_names
    is_current_group_hidden = current_group in hidden_group_names
    is_current_comic_hidden = is_current_comic_directly_hidden or is_current_group_hidden

    _, groups, group_counts, _ = filter_grouped_comics_for_user(get_available_comics(), current_user)
    if current_user and current_user.is_admin and current_group not in groups:
        groups.append(current_group)
        group_counts[current_group] = group_counts.get(current_group, 0)

    comic_description = get_comic_description(task.comic_name)
    if not comic_description:
        # 旧书库补全：有源站映射时按需抓取一次简介
        comic_description = refresh_comic_description(task.comic_name) or ''

    return render_template(
        'comic_detail.html',
        task=task,
        chapters=chapters,
        progress=progress,
        reading_time=reading_time,
        comic_description=comic_description,
        current_group=current_group,
        groups=groups,
        group_counts=group_counts,
        group_total_count=sum(group_counts.values()),
        selected_group=current_group,
        show_group_menu=bool(groups),
        group_menu_return_to='detail',
        group_menu_task_id=task.comic_name,
        is_current_comic_hidden=is_current_comic_hidden,
        is_current_comic_directly_hidden=is_current_comic_directly_hidden,
        is_current_group_hidden=is_current_group_hidden,
        hidden_group_names=hidden_group_names
    )


@app.route('/comic/<task_id>/cover', methods=['POST'])
@admin_required
def update_comic_cover(task_id):
    task = get_task(task_id)
    comic_name = task.comic_name if task else task_id

    comic_path = get_comic_directory(comic_name)
    if not comic_path or not os.path.exists(comic_path):
        flash('漫画不存在，无法修改封面')
        return redirect(url_for('comics_list'))

    cover_file = request.files.get('cover_file')
    try:
        save_uploaded_cover_image(comic_name, cover_file)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for('comic_detail', task_id=task_id))
    except Exception:
        flash('封面保存失败，请稍后重试')
        return redirect(url_for('comic_detail', task_id=task_id))

    flash(f'《{comic_name}》封面已更新')
    return redirect(url_for('comic_detail', task_id=task_id))


@app.route('/reader/<task_id>')
@login_required
def comic_reader(task_id):
    """漫画阅读器页面 - 通过任务ID"""
    import os
    current_user = get_current_user()

    # 尝试通过任务ID获取任务
    task = get_task(task_id)
    if task:
        comic_name = task.comic_name
        comic_format = task.comic_format
    else:
        # 如果任务不存在，尝试将task_id作为漫画名称处理
        comic_name = task_id

        # 检查漫画文件是否存在
        comic_path = get_comic_directory(comic_name)
        if not comic_path or not os.path.exists(comic_path):
            return render_template('error.html', message="漫画不存在"), 404

        comic_format = detect_local_comic_format(comic_path)
        if not comic_format:
            return render_template('error.html', message="不支持的漫画格式"), 404

        # 创建一个模拟的task对象
        class MockTask:
            def __init__(self, comic_name, comic_format):
                self.id = comic_name
                self.comic_name = comic_name
                self.comic_format = comic_format
                self.status = 'completed'
                self.total_chapters = 0
                self.completed_chapters = 0
                self.available_chapters = 0
                self.created_at = None

        # 计算可用章节数
        mock_task = MockTask(comic_name, comic_format)
        local_chapters = list_local_chapters(comic_name)
        mock_task.available_chapters = len(local_chapters)
        mock_task.total_chapters = mock_task.available_chapters
        mock_task.completed_chapters = mock_task.available_chapters
        task = mock_task

    current_group = get_comic_group_map().get(task.comic_name) or getattr(task, 'group', None) or '默认分组'
    if not can_user_access_group(current_group, current_user):
        flash('当前账号未被授权访问该分组漫画')
        return redirect(url_for('comics_list'))

    # 获取阅读进度
    current_user_id = session.get('user_id')
    progress = get_reading_progress(task.comic_name, current_user_id)
    # 检查是否有指定的起始章节
    start_chapter = request.args.get('start_chapter', None)
    if start_chapter is not None:
        start_chapter = int(start_chapter)
    else:
        start_chapter = progress.last_chapter if progress else 0
    start_page = progress.last_page if progress else 0

    # 确定文件扩展名
    file_ext = 'pdf' if task.comic_format == 1 else 'cbz'

    return render_template('reader.html', task=task, file_ext=file_ext, start_chapter=start_chapter, start_page=start_page)

@app.route('/save_progress', methods=['POST'])
@login_required
@csrf.exempt
def save_progress():
    """保存阅读进度（修改路由名避免与函数名冲突）"""
    csrf_error = require_csrf_for_session_auth(api_response=False)
    if csrf_error:
        return csrf_error

    try:
        data = request.get_json()

        if not data:
            return jsonify({'status': 'error', 'message': '无效的JSON格式'}), 400

        comic_name = data.get('comic_name')
        chapter = data.get('chapter', 0)
        page = data.get('page', 0)
        scroll_position = data.get('scroll_position', 0)
        total_chapters = data.get('total_chapters', 0)
        total_pages = data.get('total_pages', 0)
        reading_time = data.get('reading_time', 0)
        reading_session_id = (data.get('reading_session_id') or '').strip()

        if not comic_name:
            return jsonify({'status': 'error', 'message': '漫画名称不能为空'}), 400

        current_user_id = session.get('user_id')
        save_reading_progress(comic_name, chapter, page, scroll_position, total_chapters, total_pages, current_user_id)

        # 记录阅读时间（按会话累计秒数去重，避免页面隐藏/切换时重复记时）
        if reading_time > 0:
            record_reading_time(comic_name, reading_time, current_user_id, reading_session_id or None)

        return jsonify({'status': 'success'})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': '保存阅读进度失败'}), 400

@app.route('/get_progress/<comic_name>')
@login_required
def get_progress(comic_name):
    """获取阅读进度"""
    try:
        current_user_id = session.get('user_id')
        progress = get_reading_progress(comic_name, current_user_id)
        if progress:
            return jsonify({
                'status': 'success',
                'chapter': progress.last_chapter,
                'page': progress.last_page,
                'scroll_position': progress.scroll_position or 0
            })
        else:
            return jsonify({
                'status': 'success',
                'chapter': 0,
                'page': 0,
                'scroll_position': 0
            })
    except Exception as e:
        safe_print(f"获取阅读进度失败: {e}")
        return jsonify({'status': 'error', 'message': '获取阅读进度失败'})

@app.route('/static/comic/<path:filename>')
@login_required
def serve_comic_file(filename):
    """提供漫画文件访问"""
    resolved_file = resolve_comic_file_request(filename)
    if not resolved_file:
        return jsonify({'error': '文件不存在'}), 404

    current_user = get_current_user()
    current_group = get_comic_group_map().get(resolved_file['comic_name']) or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return jsonify({'error': '当前账号未被授权访问该分组漫画'}), 403

    file_path = resolved_file['file_path']
    if file_path.lower().endswith('.pdf'):
        readable_pdf = repair_pdf_for_reading(file_path)
        return send_from_directory(os.path.dirname(readable_pdf), os.path.basename(readable_pdf))
    return send_from_directory(resolved_file['comic_dir'], resolved_file['relative_filename'])

@app.route('/api/chapters/<task_id>')
@login_required
def get_chapters(task_id):
    """获取漫画章节列表 - 支持任务ID和漫画名称"""
    import os
    current_user = get_current_user()

    # 尝试通过任务ID获取任务
    task = get_task(task_id)
    if task:
        comic_name = task.comic_name
    else:
        # 将task_id作为漫画名称处理
        comic_name = task_id

    comic_path = get_comic_directory(comic_name)
    if not comic_path or not os.path.exists(comic_path):
        return jsonify({'error': '漫画文件不存在'}), 404

    current_group = get_comic_group_map().get(comic_name) or getattr(task, 'group', None) or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return jsonify({'error': '当前账号未被授权访问该分组漫画'}), 403

    try:
        chapters = list_local_chapters(comic_name)
    except Exception as e:
        return jsonify({'error': f'获取章节列表失败: {str(e)}'}), 500

    return jsonify({
        'comic_name': comic_name,
        'chapters': chapters,
        'total_chapters': len(chapters)
    })

@app.route('/settings')
@login_required
def settings():
    current_user = get_current_user()
    scan_paths = get_scan_path_entries() if current_user and current_user.is_admin else []
    return render_template('settings.html', current_user=current_user, scan_paths=scan_paths)


@app.route('/settings/scan_paths', methods=['POST'])
@login_required
@admin_required
def add_scan_path():
    scan_path = normalize_scan_path(request.form.get('scan_path'))

    if not scan_path:
        flash('请输入有效的漫画目录路径')
        return redirect(url_for('settings'))

    if not os.path.isdir(scan_path):
        flash('目录不存在，无法加入扫盘路径')
        return redirect(url_for('settings'))

    default_root = normalize_scan_path(COMIC_ROOT)
    if scan_path == default_root:
        flash('该路径已经是系统默认漫画目录')
        return redirect(url_for('settings'))

    existing_scan_path = ComicScanPath.query.filter_by(path=scan_path).first()
    if existing_scan_path:
        existing_scan_path.enabled = True
        existing_scan_path.updated_at = datetime.now(china_tz)
        db.session.commit()
        invalidate_comics_cache()
        refresh_comics_cache(force=True)
        flash('扫盘路径已存在，已重新启用并刷新漫画库')
        return redirect(url_for('settings'))

    db.session.add(ComicScanPath(path=scan_path, enabled=True))
    db.session.commit()
    invalidate_comics_cache()
    refresh_comics_cache(force=True)
    flash(f'已加入扫盘路径：{scan_path}')
    return redirect(url_for('settings'))


@app.route('/settings/scan_paths/<int:scan_path_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_scan_path(scan_path_id):
    scan_path = db.session.get(ComicScanPath, scan_path_id)
    if not scan_path:
        flash('扫盘路径不存在')
        return redirect(url_for('settings'))

    removed_path = scan_path.path
    db.session.delete(scan_path)
    db.session.commit()
    invalidate_comics_cache()
    refresh_comics_cache(force=True)
    flash(f'已移除扫盘路径：{removed_path}')
    return redirect(url_for('settings'))


@app.route('/settings/scan_paths/rescan', methods=['POST'])
@login_required
@admin_required
def rescan_comic_library():
    invalidate_comics_cache()
    refreshed_comics = refresh_comics_cache(force=True) or []
    flash(f'扫盘完成，当前共发现 {len(refreshed_comics)} 本漫画')
    return redirect(url_for('settings'))

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    """修改用户密码功能"""
    if request.method == 'POST':
        old_password = request.form.get('old_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        user_id = session.get('user_id')
        user = User.query.get(user_id)
        
        if not user:
            flash('用户不存在，请重新登录')
            return redirect(url_for('login'))
        
        if not user.check_password(old_password):
            flash('原密码输入错误，请重试')
            return render_template('change_password.html')
        
        if new_password != confirm_password:
            flash('新密码和确认密码不一致，请重试')
            return render_template('change_password.html')
        
        if len(new_password) < 6:
            flash('新密码长度不能少于6位，请设置更安全的密码')
            return render_template('change_password.html')
        
        user.set_password(new_password)
        db.session.commit()
        
        flash('密码修改成功，请使用新密码登录')
        session.pop('user_id', None)
        session.pop('username', None)
        session.pop('user_role', None)
        return redirect(url_for('login'))
    
    return render_template('change_password.html')
