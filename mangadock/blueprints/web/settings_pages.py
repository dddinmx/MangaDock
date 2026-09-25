# -*- coding: utf-8 -*-
"""设置页（内容分级 / 扫盘路径）。"""
import os
from datetime import datetime

from flask import (
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from mangadock.auth import admin_required, get_current_user, login_required
from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import AniListAccount, ComicScanPath
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
    )


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
