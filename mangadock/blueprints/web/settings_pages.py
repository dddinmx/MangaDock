# -*- coding: utf-8 -*-
"""设置页（内容分级 / 扫盘路径）。"""
import os
import json
from datetime import datetime

from flask import (
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from mangadock.auth import admin_required, get_current_user, login_required
from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import AniListAccount, ComicScanPath, BackgroundCommand
from mangadock.services.adult_content import (
    is_adult_content_enabled,
    set_adult_content_enabled,
)
from mangadock.services.library import (
    get_scan_path_entries,
    invalidate_comics_cache,
    normalize_scan_path,
    refresh_comics_cache,
)
from mangadock.services.webdav import WebDavError, disconnect as disconnect_webdav
from mangadock.services.webdav import save_settings as save_webdav_settings
from mangadock.services.webdav import settings_view as webdav_settings_view
from mangadock.services.webdav import queue_library_sync
from mangadock.services.updates import update_webdav_sync_state
from mangadock.services.webdav_metadata import metadata_progress
from mangadock.settings import COMIC_ROOT, china_tz


@app.route('/settings')
@login_required
def settings():
    current_user = get_current_user()
    scan_paths = get_scan_path_entries() if current_user and current_user.is_admin else []
    return render_template(
        'settings.html',
        current_user=current_user,
        scan_paths=scan_paths,
        adult_content_enabled=is_adult_content_enabled(),
        anilist_account=db.session.get(AniListAccount, current_user.id),
        webdav=webdav_settings_view() if current_user and current_user.is_admin else None,
    )


@app.route('/settings/webdav', methods=['POST'])
@login_required
@admin_required
def save_webdav():
    try:
        save_webdav_settings(
            request.form.get('url'),
            request.form.get('username'),
            request.form.get('password') or '',
            request.form.get('root'),
            request.form.get('cache_gb'),
            keep_password=not (request.form.get('password') or '').strip(),
        )
    except WebDavError as exc:
        flash(str(exc))
        return redirect(url_for('settings'))
    flash('WebDAV 已保存，并确认根目录可以读取')
    return redirect(url_for('settings'))


@app.route('/settings/webdav/sync', methods=['POST'])
@login_required
@admin_required
def sync_webdav():
    try:
        queue_library_sync()
        flash('WebDAV 同步已加入后台队列，可以离开此页')
    except WebDavError as exc:
        flash(str(exc))
    return redirect(url_for('settings'))


@app.route('/settings/webdav/progress')
@login_required
@admin_required
def webdav_sync_progress():
    command = BackgroundCommand.query.filter_by(command_type='sync_webdav').order_by(
        BackgroundCommand.id.desc()).first()
    progress = {}
    if command:
        try:
            progress = json.loads(command.payload or '{}')
        except ValueError:
            pass
    from mangadock.services.library import comic_deletion_version
    response = jsonify(status=command.status if command else 'idle',
                       message=command.message if command else '',
                       progress=progress, metadata=metadata_progress(), version=str(comic_deletion_version()))
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/settings/webdav/control', methods=['POST'])
@login_required
@admin_required
def control_webdav_sync():
    action = request.form.get('action')
    if action not in ('pause', 'resume', 'cancel'):
        return jsonify(error='无效操作'), 400
    command = BackgroundCommand.query.filter_by(command_type='sync_webdav').order_by(BackgroundCommand.id.desc()).first()
    if command:
        update_webdav_sync_state(command.id, action=action)
    return redirect(url_for('settings'))


@app.route('/settings/webdav/disconnect', methods=['POST'])
@login_required
@admin_required
def disconnect_webdav_route():
    disconnect_webdav()
    invalidate_comics_cache()
    refresh_comics_cache(force=True)
    flash('已断开 WebDAV。已缓存的章节还在，云端文件未改动')
    return redirect(url_for('settings'))


@app.route('/settings/adult-content', methods=['POST'])
@login_required
@admin_required
def set_adult_content():
    enabled = (request.form.get('enabled') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    set_adult_content_enabled(enabled)
    if enabled:
        flash('已开启 18+ 内容来源：下载页将显示漫小肆韩漫入口')
    else:
        flash('已关闭 18+ 内容来源：漫小肆韩漫入口已隐藏，相关下载已停用')
    return redirect(url_for('settings'))


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
