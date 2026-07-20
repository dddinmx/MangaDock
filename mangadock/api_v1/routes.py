# -*- coding: utf-8 -*-
"""Versioned JSON API routes under /api/v1.

Write endpoints for downloads, updates, tasks, groups, library admin,
settings, and users. Existing /api/* read routes remain unchanged.
Old web form POSTs stay available for backward compatibility.
"""
import os
from datetime import datetime

from flask import request, session, url_for

from mangadock.api_v1.common import (
    api_fail,
    api_ok,
    parse_comic_format,
    request_json,
    require_write_auth,
)
from mangadock.auth import (
    api_datetime,
    api_login_required,
    authenticate_api_credentials,
    establish_user_session,
    get_api_request_user,
    is_safe_next_target,
    require_csrf_for_session_auth,
    save_uploaded_cover_image,
    serialize_api_user,
)
from mangadock.extensions import db
from mangadock.models import (
    ComicScanPath,
    LoginLog,
    ReadingProgress,
    ReadingSessionState,
    ReadingTime,
    User,
    UserGroupPermission,
)
from mangadock.services.download import is_supported_comic_url
from mangadock.services.groups import (
    assign_comic_group,
    delete_comic_group,
    ensure_comic_group,
    get_all_comic_groups,
    get_comic_group_map,
    get_user_group_permissions,
    normalize_group_name,
    set_admin_hidden_item,
    set_user_group_permissions,
)
from mangadock.services.library import (
    get_comic_directory,
    get_scan_path_entries,
    invalidate_comics_cache,
    load_comic_mapping,
    normalize_scan_path,
    refresh_comics_cache,
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
    is_update_check_refreshing,
    queue_background_command,
    set_comic_update_mode,
)
from mangadock.services.workers import start_download_task, start_update_task
from mangadock.settings import (
    APP_VERSION,
    COMIC_ROOT,
    COMIC_UPDATE_MODE_AUTO,
    china_tz,
)


def _serialize_task(task):
    log_entries = []
    if task.log:
        log_entries = [line for line in task.log.split('\n') if line.strip()]
    return {
        'id': task.id,
        'comic_name': task.comic_name,
        'url': task.url,
        'status': task.status,
        'progress_percent': task.progress_percent or 0,
        'total_chapters': task.total_chapters or 0,
        'completed_chapters': task.completed_chapters or 0,
        'comic_format': task.comic_format,
        'format': 'pdf' if task.comic_format == 1 else 'cbz' if task.comic_format == 2 else None,
        'is_update': bool(task.is_update),
        'group': getattr(task, 'group', None) or '默认分组',
        'log': log_entries,
        'start_time': api_datetime(task.start_time),
        'end_time': api_datetime(task.end_time),
        'created_at': api_datetime(task.created_at),
    }


def _serialize_update_candidate(item):
    return {
        'comic_name': item.comic_name,
        'source_url': item.source_url,
        'has_updates': bool(item.has_updates),
        'pending_chapters': item.pending_chapters or 0,
        'remote_total_chapters': item.remote_total_chapters or 0,
        'local_total_chapters': item.local_total_chapters or 0,
        'latest_chapter_title': item.latest_chapter_title,
        'status': item.status,
        'error_message': item.error_message,
        'last_checked_at': api_datetime(item.last_checked_at),
    }


def _serialize_scan_path(entry):
    if isinstance(entry, dict):
        return entry
    return {
        'id': entry.id,
        'path': entry.path,
        'enabled': bool(entry.enabled),
        'created_at': api_datetime(getattr(entry, 'created_at', None)),
        'updated_at': api_datetime(getattr(entry, 'updated_at', None)),
    }


def register_routes(bp):
    """Attach all /api/v1 routes onto the blueprint."""

    # ------------------------------------------------------------------ meta
    @bp.get('')
    @bp.get('/')
    def api_v1_root():
        return api_ok({
            'name': 'MangaDock API',
            'version': 'v1',
            'app_version': APP_VERSION,
            'auth': {
                'session_cookie': True,
                'basic_auth': True,
                'csrf_required_for_session_writes': True,
            },
            'resources': [
                'GET /api/v1',
                'POST /api/v1/auth/login',
                'POST /api/v1/auth/logout',
                'GET /api/v1/auth/me',
                'GET|POST /api/v1/tasks',
                'GET /api/v1/tasks/<id>',
                'POST /api/v1/tasks/<id>/cancel',
                'DELETE /api/v1/tasks/<id>',
                'POST /api/v1/tasks/delete-finished',
                'POST /api/v1/downloads',
                'GET|POST /api/v1/updates',
                'PUT /api/v1/updates/mode',
                'POST /api/v1/updates/check',
                'GET|POST /api/v1/groups',
                'DELETE /api/v1/groups/<name>',
                'POST /api/v1/groups/assign',
                'POST /api/v1/library/hidden/comics',
                'POST /api/v1/library/hidden/groups',
                'POST /api/v1/comics/by-name/<name>/cover',
                'GET|POST /api/v1/settings/scan-paths',
                'DELETE /api/v1/settings/scan-paths/<id>',
                'POST /api/v1/settings/scan-paths/rescan',
                'GET|POST /api/v1/users',
                'DELETE /api/v1/users/<id>',
                'PUT /api/v1/users/<id>/groups',
                'POST /api/v1/account/password',
            ],
        })

    # ------------------------------------------------------------------ auth
    def _normalize_api_error(error_response):
        """Convert legacy api_error(Response, status) into v1 envelope."""
        if not error_response:
            return api_fail('AUTH_FAILED', '认证失败', 401)
        if isinstance(error_response, tuple) and len(error_response) == 2:
            body, status = error_response
            try:
                payload = body.get_json(silent=True) or {}
            except Exception:
                payload = {}
            err = payload.get('error') or {}
            return api_fail(
                err.get('code') or 'AUTH_FAILED',
                err.get('message') or '认证失败',
                status if isinstance(status, int) else 401,
            )
        return error_response

    @bp.post('/auth/login')
    def api_v1_login():
        # Browser form login should send CSRF; Basic-style clients can omit.
        csrf_error = require_csrf_for_session_auth(api_response=True)
        if csrf_error:
            return _normalize_api_error(csrf_error)

        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')

        username = data.get('username')
        password = data.get('password')
        next_target = data.get('next') or request.args.get('next')
        user, error_response = authenticate_api_credentials(
            username,
            password,
            request.remote_addr or '',
            request.user_agent.string,
        )
        if error_response:
            return _normalize_api_error(error_response)

        establish_user_session(user)
        if not is_safe_next_target(next_target):
            next_target = url_for('index')
        return api_ok({
            'user': serialize_api_user(user),
            'next': next_target or url_for('index'),
        })

    @bp.post('/auth/logout')
    @require_write_auth(admin=False)
    def api_v1_logout():
        session.clear()
        return api_ok({
            'message': '已退出登录',
            'next': url_for('login'),
        })

    @bp.get('/auth/me')
    @api_login_required
    def api_v1_me():
        current_user = get_api_request_user()
        return api_ok({'user': serialize_api_user(current_user)})

    # ------------------------------------------------------------------ tasks
    @bp.get('/tasks')
    @require_write_auth(admin=True)
    def list_tasks():
        all_tasks = get_all_tasks()
        active_statuses = {'pending', 'running'}
        items = [_serialize_task(task) for task in all_tasks]
        active_count = sum(1 for task in all_tasks if task.status in active_statuses)
        return api_ok({
            'items': items,
            'active_count': active_count,
            'deletable_count': len(all_tasks) - active_count,
            'total': len(all_tasks),
        })

    @bp.get('/tasks/<task_id>')
    @require_write_auth(admin=True)
    def get_task_detail(task_id):
        task = get_task(task_id)
        if not task:
            return api_fail('NOT_FOUND', '任务不存在', 404)
        return api_ok(_serialize_task(task))

    @bp.post('/tasks/<task_id>/cancel')
    @require_write_auth(admin=True)
    def cancel_task_v1(task_id):
        task = get_task(task_id)
        if task and task.status in {'pending', 'running'}:
            update_task(
                task_id,
                status='cancelled',
                end_time=datetime.now(china_tz),
                log='用户已取消任务',
            )
            return api_ok({'task_id': task_id, 'status': 'cancelled'})
        return api_fail('INVALID_STATE', '无法取消任务，任务可能已完成或不存在', 400)

    @bp.delete('/tasks/<task_id>')
    @require_write_auth(admin=True)
    def delete_task_v1(task_id):
        if delete_task(task_id):
            return api_ok({'task_id': task_id, 'deleted': True})
        return api_fail('NOT_FOUND', '任务不存在', 404)

    @bp.post('/tasks/delete-finished')
    @require_write_auth(admin=True)
    def delete_finished_tasks_v1():
        try:
            deleted_count, active_count = delete_finished_tasks()
        except Exception as exc:
            db.session.rollback()
            return api_fail('INTERNAL_ERROR', str(exc), 500)
        if deleted_count:
            message = f'已删除 {deleted_count} 条已结束任务记录'
        else:
            message = '暂无可删除的已结束任务记录'
        return api_ok({
            'deleted_count': deleted_count,
            'active_count': active_count,
            'message': message,
        })

    # ------------------------------------------------------------------ downloads
    @bp.post('/downloads')
    @require_write_auth(admin=True)
    def create_download():
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')

        comic_url = (data.get('url') or data.get('comic_url') or '').strip()
        try:
            comic_format = parse_comic_format(data.get('format'), default=2)
        except ValueError as exc:
            return api_fail('INVALID_FORMAT', str(exc))

        if not is_supported_comic_url(comic_url):
            return api_fail(
                'UNSUPPORTED_URL',
                '请输入有效的漫画详情页 URL（支持 baozimh.com、baozimhcn.com、baozimh.org 或 mxs12.cc）',
            )

        task_id = start_download_task(comic_url, comic_format)
        task = get_task(task_id)
        return api_ok({
            'task_id': task_id,
            'task': _serialize_task(task) if task else None,
        }, status=201)

    # ------------------------------------------------------------------ updates
    @bp.get('/updates')
    @require_write_auth(admin=True)
    def get_updates_status():
        mapping = load_comic_mapping()
        candidates = get_update_candidates()
        return api_ok({
            'mode': get_comic_update_mode(),
            'checking': is_update_check_refreshing(),
            'last_checked_at': api_datetime(get_last_update_check_time()),
            'tracked_comics': list(mapping.keys()),
            'candidates': [_serialize_update_candidate(item) for item in candidates],
        })

    @bp.post('/updates')
    @require_write_auth(admin=True)
    def start_update():
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')

        comic_name = (data.get('comic_name') or '').strip()
        try:
            comic_format = parse_comic_format(data.get('format'), default=2)
        except ValueError as exc:
            return api_fail('INVALID_FORMAT', str(exc))

        mapping = load_comic_mapping()
        comic_url = mapping.get(comic_name)
        if not comic_name or not comic_url:
            return api_fail('NOT_FOUND', '未找到该漫画的源 URL 信息', 404)

        task_id = start_update_task(comic_name, comic_format, comic_url)
        task = get_task(task_id)
        return api_ok({
            'task_id': task_id,
            'task': _serialize_task(task) if task else None,
        }, status=201)

    @bp.put('/updates/mode')
    @require_write_auth(admin=True)
    def set_update_mode():
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')
        try:
            mode = set_comic_update_mode(data.get('mode'))
        except ValueError as exc:
            return api_fail('INVALID_MODE', str(exc))
        message = (
            '已切换为自动更新：后续扫描发现更新会自动加入任务队列'
            if mode == COMIC_UPDATE_MODE_AUTO
            else '已切换为手动更新：扫描结果需要手动选择漫画更新'
        )
        return api_ok({'mode': mode, 'message': message})

    @bp.post('/updates/check')
    @require_write_auth(admin=True)
    def check_updates():
        queue_background_command('refresh_update_checks', {'force': True}, dedupe_pending=True)
        return api_ok({
            'queued': True,
            'message': '已加入更新检查队列，稍后刷新可查看最新结果',
        })

    # ------------------------------------------------------------------ groups
    @bp.get('/groups')
    @api_login_required
    def list_groups():
        groups = get_all_comic_groups()
        group_map = get_comic_group_map()
        counts = {}
        for group_name in groups:
            counts[group_name] = 0
        for assigned in group_map.values():
            counts[assigned] = counts.get(assigned, 0) + 1
        return api_ok({
            'items': [
                {'name': name, 'comic_count': counts.get(name, 0)}
                for name in groups
            ],
        })

    @bp.post('/groups')
    @require_write_auth(admin=True)
    def create_group():
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')
        group_name = normalize_group_name(data.get('name') or data.get('group_name'))
        if not group_name:
            return api_fail('INVALID_NAME', '分组名称不能为空')
        if group_name == '全部':
            return api_fail('INVALID_NAME', '“全部”是系统筛选项，不能作为分组名称')

        existing = set(get_all_comic_groups())
        created = ensure_comic_group(group_name)
        if not created:
            return api_fail('INVALID_NAME', '分组名称无效')
        already = created in existing
        return api_ok({
            'name': created,
            'created': not already,
            'message': '分组已存在' if already else f'已创建分组：{created}',
        }, status=200 if already else 201)

    @bp.delete('/groups/<path:group_name>')
    @require_write_auth(admin=True)
    def remove_group(group_name):
        normalized = normalize_group_name(group_name)
        if not normalized:
            return api_fail('INVALID_NAME', '请选择要删除的分组')
        if normalized == '默认分组':
            return api_fail('FORBIDDEN', '默认分组不能删除', 403)
        if not delete_comic_group(normalized):
            return api_fail('DELETE_FAILED', '删除分组失败，请重试')
        return api_ok({'name': normalized, 'deleted': True})

    @bp.post('/groups/assign')
    @require_write_auth(admin=True)
    def assign_group():
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')
        comic_name = (data.get('comic_name') or '').strip()
        group_name = normalize_group_name(data.get('group_name') or data.get('group'))
        if not comic_name or not get_comic_directory(comic_name):
            return api_fail('NOT_FOUND', '漫画不存在，无法设置分组', 404)
        if not group_name:
            return api_fail('INVALID_NAME', '请选择一个分组')
        if not assign_comic_group(comic_name, group_name):
            return api_fail('ASSIGN_FAILED', '设置分组失败，请重试')
        return api_ok({
            'comic_name': comic_name,
            'group_name': group_name,
        })

    # ------------------------------------------------------------------ library hidden / cover
    @bp.post('/library/hidden/comics')
    @require_write_auth(admin=True)
    def toggle_hidden_comic():
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')
        current_user = get_api_request_user()
        comic_name = (data.get('comic_name') or '').strip()
        hidden = data.get('hidden')
        action = (data.get('action') or '').strip().lower()
        if hidden is None and action in {'hide', 'show'}:
            hidden = action == 'hide'
        if not comic_name or not get_comic_directory(comic_name):
            return api_fail('NOT_FOUND', '漫画不存在，无法更新隐藏状态', 404)
        if hidden is None:
            return api_fail('INVALID_ACTION', '请提供 hidden(bool) 或 action(hide|show)')
        set_admin_hidden_item(current_user.id, 'comic', comic_name, bool(hidden))
        return api_ok({
            'comic_name': comic_name,
            'hidden': bool(hidden),
        })

    @bp.post('/library/hidden/groups')
    @require_write_auth(admin=True)
    def toggle_hidden_group():
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')
        current_user = get_api_request_user()
        group_name = normalize_group_name(data.get('group_name') or data.get('group'))
        hidden = data.get('hidden')
        action = (data.get('action') or '').strip().lower()
        if hidden is None and action in {'hide', 'show'}:
            hidden = action == 'hide'
        if not group_name or group_name not in set(get_all_comic_groups()):
            return api_fail('NOT_FOUND', '分组不存在，无法更新隐藏状态', 404)
        if hidden is None:
            return api_fail('INVALID_ACTION', '请提供 hidden(bool) 或 action(hide|show)')
        set_admin_hidden_item(current_user.id, 'group', group_name, bool(hidden))
        return api_ok({
            'group_name': group_name,
            'hidden': bool(hidden),
        })

    @bp.post('/comics/by-name/<path:comic_name>/cover')
    @require_write_auth(admin=True)
    def upload_cover(comic_name):
        comic_path = get_comic_directory(comic_name)
        if not comic_path or not os.path.exists(comic_path):
            return api_fail('NOT_FOUND', '漫画不存在，无法修改封面', 404)
        cover_file = request.files.get('cover_file') or request.files.get('file')
        try:
            save_uploaded_cover_image(comic_name, cover_file)
        except ValueError as exc:
            return api_fail('INVALID_FILE', str(exc))
        except Exception:
            return api_fail('SAVE_FAILED', '封面保存失败，请稍后重试', 500)
        return api_ok({
            'comic_name': comic_name,
            'message': f'《{comic_name}》封面已更新',
        })

    # ------------------------------------------------------------------ settings / scan paths
    @bp.get('/settings/scan-paths')
    @require_write_auth(admin=True)
    def list_scan_paths():
        entries = get_scan_path_entries()
        items = []
        for entry in entries:
            if isinstance(entry, dict):
                items.append(entry)
            else:
                items.append(_serialize_scan_path(entry))
        return api_ok({
            'default_root': normalize_scan_path(COMIC_ROOT),
            'items': items,
        })

    @bp.post('/settings/scan-paths')
    @require_write_auth(admin=True)
    def add_scan_path_v1():
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')
        scan_path = normalize_scan_path(data.get('path') or data.get('scan_path'))
        if not scan_path:
            return api_fail('INVALID_PATH', '请输入有效的漫画目录路径')
        if not os.path.isdir(scan_path):
            return api_fail('NOT_FOUND', '目录不存在，无法加入扫盘路径', 404)

        default_root = normalize_scan_path(COMIC_ROOT)
        if scan_path == default_root:
            return api_fail('INVALID_PATH', '该路径已经是系统默认漫画目录')

        existing = ComicScanPath.query.filter_by(path=scan_path).first()
        if existing:
            existing.enabled = True
            existing.updated_at = datetime.now(china_tz)
            db.session.commit()
            invalidate_comics_cache()
            refresh_comics_cache(force=True)
            return api_ok({
                'path': scan_path,
                'id': existing.id,
                'reenabled': True,
                'message': '扫盘路径已存在，已重新启用并刷新漫画库',
            })

        row = ComicScanPath(path=scan_path, enabled=True)
        db.session.add(row)
        db.session.commit()
        invalidate_comics_cache()
        refresh_comics_cache(force=True)
        return api_ok({
            'path': scan_path,
            'id': row.id,
            'created': True,
            'message': f'已加入扫盘路径：{scan_path}',
        }, status=201)

    @bp.delete('/settings/scan-paths/<int:scan_path_id>')
    @require_write_auth(admin=True)
    def delete_scan_path_v1(scan_path_id):
        scan_path = db.session.get(ComicScanPath, scan_path_id)
        if not scan_path:
            return api_fail('NOT_FOUND', '扫盘路径不存在', 404)
        removed = scan_path.path
        db.session.delete(scan_path)
        db.session.commit()
        invalidate_comics_cache()
        refresh_comics_cache(force=True)
        return api_ok({
            'id': scan_path_id,
            'path': removed,
            'deleted': True,
        })

    @bp.post('/settings/scan-paths/rescan')
    @require_write_auth(admin=True)
    def rescan_library():
        invalidate_comics_cache()
        refreshed = refresh_comics_cache(force=True) or []
        return api_ok({
            'comic_count': len(refreshed),
            'message': f'扫盘完成，当前共发现 {len(refreshed)} 本漫画',
        })

    # ------------------------------------------------------------------ users
    @bp.get('/users')
    @require_write_auth(admin=True)
    def list_users():
        all_users = User.query.order_by(
            db.case((User.role == 'admin', 0), else_=1),
            User.username.asc()
        ).all()
        items = []
        for user in all_users:
            payload = serialize_api_user(user)
            if not user.is_admin:
                payload['group_names'] = get_user_group_permissions(user.id)
            else:
                payload['group_names'] = get_all_comic_groups()
            items.append(payload)
        return api_ok({'items': items})

    @bp.post('/users')
    @require_write_auth(admin=True)
    def create_user():
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')

        username = (data.get('username') or '').strip()
        password = data.get('password') or ''
        confirm_password = data.get('confirm_password')
        if confirm_password is None:
            confirm_password = password
        role = (data.get('role') or 'user').strip()
        selected_groups = data.get('group_names') or data.get('groups') or []
        if isinstance(selected_groups, str):
            selected_groups = [selected_groups]

        if not username:
            return api_fail('INVALID_USERNAME', '用户名不能为空')
        if len(username) < 3:
            return api_fail('INVALID_USERNAME', '用户名长度不能少于3位')
        if password != confirm_password:
            return api_fail('PASSWORD_MISMATCH', '两次输入的密码不一致')
        if len(password) < 6:
            return api_fail('INVALID_PASSWORD', '密码长度不能少于6位')
        if role not in {'admin', 'user'}:
            return api_fail('INVALID_ROLE', '无效的用户角色')
        if User.query.filter_by(username=username).first():
            return api_fail('USERNAME_EXISTS', '用户名已存在', 409)

        user = User(username=username, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        if role == 'user':
            set_user_group_permissions(user.id, selected_groups)
        return api_ok({'user': serialize_api_user(user)}, status=201)

    @bp.delete('/users/<int:user_id>')
    @require_write_auth(admin=True)
    def delete_user_v1(user_id):
        current_user = get_api_request_user()
        user = db.session.get(User, user_id)
        if not user:
            return api_fail('NOT_FOUND', '用户不存在', 404)
        if current_user and user.id == current_user.id:
            return api_fail('FORBIDDEN', '不能删除当前登录账号', 403)
        if user.role == 'admin':
            return api_fail('FORBIDDEN', '不能删除管理员账号', 403)

        db.session.query(ReadingProgress).filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.query(ReadingTime).filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.query(ReadingSessionState).filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.query(UserGroupPermission).filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.query(LoginLog).filter_by(username=user.username).delete(synchronize_session=False)
        username = user.username
        db.session.delete(user)
        db.session.commit()
        return api_ok({'id': user_id, 'username': username, 'deleted': True})

    @bp.put('/users/<int:user_id>/groups')
    @require_write_auth(admin=True)
    def update_user_groups_v1(user_id):
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')
        user = db.session.get(User, user_id)
        if not user:
            return api_fail('NOT_FOUND', '用户不存在', 404)
        if user.is_admin:
            return api_fail('FORBIDDEN', '管理员默认拥有全部分组权限，无需单独授权', 403)

        selected_groups = data.get('group_names') or data.get('groups') or []
        if isinstance(selected_groups, str):
            selected_groups = [selected_groups]
        saved = set_user_group_permissions(user.id, selected_groups)
        return api_ok({
            'user_id': user.id,
            'username': user.username,
            'group_names': list(saved) if saved is not None else get_user_group_permissions(user.id),
        })

    # ------------------------------------------------------------------ account
    @bp.post('/account/password')
    @require_write_auth(admin=False)
    def change_password_v1():
        data = request_json()
        if data is None:
            return api_fail('INVALID_JSON', '请求体必须是 JSON 对象')

        current_user = get_api_request_user()
        user = db.session.get(User, current_user.id) if current_user else None
        if not user:
            return api_fail('AUTH_REQUIRED', '用户不存在，请重新登录', 401)

        old_password = data.get('old_password') or data.get('current_password') or ''
        new_password = data.get('new_password') or ''
        confirm_password = data.get('confirm_password')
        if confirm_password is None:
            confirm_password = new_password

        if not user.check_password(old_password):
            return api_fail('INVALID_PASSWORD', '原密码输入错误，请重试')
        if new_password != confirm_password:
            return api_fail('PASSWORD_MISMATCH', '新密码和确认密码不一致')
        if len(new_password) < 6:
            return api_fail('INVALID_PASSWORD', '新密码长度不能少于6位')

        user.set_password(new_password)
        db.session.commit()
        session.clear()
        return api_ok({
            'message': '密码修改成功，请使用新密码登录',
            'relogin_required': True,
        })
