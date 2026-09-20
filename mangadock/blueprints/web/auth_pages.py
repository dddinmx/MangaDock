# -*- coding: utf-8 -*-
"""登录 / 登出 / 修改密码。"""
import json
import os
import random
import time
from datetime import datetime

from flask import (
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from mangadock.auth import (
    is_safe_next_target,
    login_failure_key,
    login_required,
)
from mangadock.core import app
from mangadock.extensions import db, login_failures
from mangadock.models import LoginLog, User
from mangadock.services.providers.mxs import is_mxs_url
from mangadock.settings import (
    BASE_DIR,
    COVER_ROOT,
    LOGIN_LOCKOUT_DURATION,
    LOGIN_MAX_ATTEMPTS,
    china_tz,
)

# 登录页封面墙：24 小说封面 + 24 漫画封面。漫画封面按「日期」确定性抽样：
# 同一天所有访客看到完全相同的一批（用户要求全员一致），跨天自动轮换。
_LOGIN_COMIC_COVER_COUNT = 24
_LOGIN_COVER_POOL_TTL = 600
_login_cover_pool_cache = {'at': 0.0, 'names': []}


def _eligible_comic_cover_names():
    """候选池：cover 根目录的 jpg，排除 18+（mxs 源）漫画的封面。"""
    try:
        with open(os.path.join(BASE_DIR, 'comic.json'), 'r', encoding='utf-8') as fh:
            comic_index = json.load(fh)
    except (OSError, ValueError):
        comic_index = {}
    adult_names = {
        name for name, source_url in comic_index.items()
        if isinstance(source_url, str) and is_mxs_url(source_url)
    }
    names = []
    try:
        entries = os.listdir(COVER_ROOT)
    except OSError:
        return names
    for entry in entries:
        if not entry.lower().endswith('.jpg'):
            continue
        name = entry[:-4]
        if name in adult_names or name.startswith('._'):
            continue
        names.append(name)
    return names


def _pick_comic_covers(count=_LOGIN_COMIC_COVER_COUNT):
    """按日期做确定性抽样：随机种子 = 当天日期（中国时区），同一天内
    所有请求、所有访客得到同一批封面；候选池带 10 分钟进程内缓存。
    池先排序再抽样，保证不受目录枚举顺序影响。"""
    now = time.time()
    if now - _login_cover_pool_cache['at'] > _LOGIN_COVER_POOL_TTL:
        _login_cover_pool_cache['at'] = now
        _login_cover_pool_cache['names'] = _eligible_comic_cover_names()
    pool = sorted(_login_cover_pool_cache['names'])
    if not pool:
        return []
    today = datetime.now(china_tz).date().isoformat()
    rng = random.Random(f'mangadock-login-covers/{today}')
    if len(pool) <= count:
        picks = list(pool)
        rng.shuffle(picks)
        return picks
    return rng.sample(pool, count)


def render_login_page():
    """渲染登录页：小说封面与漫画封面交错排布的背景墙。"""
    comic_names = _pick_comic_covers()
    wall = []
    for slot in range(_LOGIN_COMIC_COVER_COUNT):
        wall.append({'url': url_for('static', filename=f'login-covers/{slot:02d}.jpg')})
        if slot < len(comic_names):
            wall.append({
                'url': url_for('static', filename=f'cover/{comic_names[slot]}.jpg'),
            })
    return render_template('login.html', login_wall=wall)


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
                    return render_login_page()
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

                return render_login_page()
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

            return render_login_page()

    # GET请求，返回登录页面
    return render_login_page()

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    """用户登出：清除session中的登录信息"""
    session.pop('user_id', None)
    session.pop('username', None)
    session.pop('user_role', None)
    flash('已成功登出')
    return redirect(url_for('login'))

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
