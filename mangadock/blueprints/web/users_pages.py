# -*- coding: utf-8 -*-
"""用户管理（页面与分组授权）。"""
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
from mangadock.models import (
    LoginLog,
    ReadingProgress,
    ReadingSessionState,
    ReadingTime,
    User,
    UserGroupPermission,
)
from mangadock.services.groups import (
    get_all_comic_groups,
    get_user_group_permissions,
    set_user_group_permissions,
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
