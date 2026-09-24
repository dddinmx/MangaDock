# -*- coding: utf-8 -*-
"""登录 / 登出 / 修改密码。"""
import json
import os
import random
import threading
import time
from datetime import datetime

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from mangadock.auth import (
    is_safe_next_target,
    login_clear_failures,
    login_failure_keys,
    login_lock_state,
    login_record_failure,
    login_required,
)
from mangadock.core import app
from mangadock.extensions import db
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
# 漫画位不足 24 个时，用随仓库分发的 login-covers/c*.jpg 补位（见 render_login_page）。
_LOGIN_COMIC_COVER_COUNT = 24
_LOGIN_COVER_POOL_TTL = 600
_login_cover_pool_cache = {'at': 0.0, 'names': []}
_login_cover_pool_lock = threading.Lock()

# 登录页专属的人工排除名单：封面画风偏情色、不适合出现在「未登录可见」的公开
# 登录页上。与 mxs（18+ 来源）无关 —— 这些漫画在库里是正常的，能读能下，
# 只是封面不上登录页。只影响登录页抽样，漫画库/书架/扩展的封面一律照旧。
_LOGIN_COVER_DENYLIST = frozenset({
    '暴君會長的嬌媳們',
    '理想型演算法',
    '太妹硬闖成人界',
    '水電工日誌',
    '潛入！財閥高校',
    '家庭教師的祕密課程',
    '尤物',
    '恨不得吃掉妳',
    '开局强吻裂口女',
    '魔教教主苟在我身边看我偷偷修炼',
    '我一个网约车司机有点钱怎么了？',
    '诸天至尊',
})

# 名字命中这些硬词的封面一律不上登录页。这是「mxs 源判定」失效时的兜底：
# 2026-09-24 实测候选池 67 张里有 32 张根本没有 comic.json 源映射，
# is_mxs_url() 对它们无从判断，成人向过滤形同虚设，只能靠名字再兜一层。
# 只收几乎不可能出现在正常漫画标题里的词，避免误伤（「诱她」「错撩」这类
# 正常恋爱向标题不含其中任何一个）。
_LOGIN_COVER_RISK_KEYWORDS = (
    '成人', '里番', '裏番', '18禁', '18+', '色情', '情色', '淫', '裸',
    '爆乳', '巨乳', '援交', 'r18', 'h漫',
)


def _is_risky_login_cover(name):
    """名字带硬关键词的封面不上公开登录页（mxs 判定失效时的兜底）。"""
    low = name.lower()
    return any(k in low for k in _LOGIN_COVER_RISK_KEYWORDS)


def _eligible_comic_cover_names():
    """候选池：cover 根目录的 jpg，排除三层：mxs（18+ 来源）、人工黑名单、名字硬关键词。"""
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
        if (name in adult_names or name in _LOGIN_COVER_DENYLIST
                or _is_risky_login_cover(name) or name.startswith('._')):
            continue
        names.append(name)
    return names


def _pick_comic_covers(count=_LOGIN_COMIC_COVER_COUNT):
    """按日期做确定性抽样：随机种子 = 当天日期（中国时区），同一天内
    所有请求、所有访客得到同一批封面；候选池带 10 分钟进程内缓存。
    池先排序再抽样，保证不受目录枚举顺序影响。"""
    with _login_cover_pool_lock:
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
                # 2026-09-24 code review P2：漫画封面改走登录页专用路由；
                # /static/cover/* 对未登录用户返回 403，封面墙无法正常加载。
                'url': url_for('login_comic_cover', slot=slot),
            })
        else:
            # 用户自己的漫画不足 24 部、甚至一部都还没下（全新部署时 static/cover
            # 只有 .gitkeep）时，用随仓库分发的 login-covers/c<slot>.jpg 补位。
            # 封面墙固定 48 格 = 12 列 × 4 行，少一格就会空出下半屏黑底；
            # 用户下载的漫画变多后，这些补位封面会被自己的封面逐个顶替掉。
            wall.append({'url': url_for('static', filename=f'login-covers/c{slot:02d}.jpg')})
    return render_template('login.html', login_wall=wall)


@app.route('/login-cover/<int:slot>.jpg')
def login_comic_cover(slot):
    """登录页封面墙专用封面路由（未登录可访问）。

    只服务「当日确定性抽样名单」内的封面：slot 超出名单或文件不存在一律 404，
    未登录可接触的范围与登录页本身要展示的内容完全一致（非 18+、每日轮换）。
    """
    comic_names = _pick_comic_covers()
    if slot < 0 or slot >= len(comic_names):
        abort(404)
    cover_path = os.path.join(COVER_ROOT, f'{comic_names[slot]}.jpg')
    if not os.path.isfile(cover_path):
        abort(404)
    response = send_file(cover_path, conditional=True)
    # 名单按天轮换，缓存 1 小时即可
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


@app.route('/login', methods=['GET', 'POST'])
def login():
    """用户登录页面"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        # 获取用户IP和User-Agent
        ip_address = request.remote_addr
        user_agent = request.user_agent.string
        failure_keys = login_failure_keys(username, ip_address)

        # 检查登录失败次数和锁定状态（用户名/IP 组合，2026-09-20 P2）
        locked, remaining_minutes, _hit = login_lock_state(failure_keys)
        if locked:
            flash(f'登录失败次数过多，请在 {remaining_minutes} 分钟后重试')
            # 记录登录失败日志
            login_log = LoginLog(
                username=username,
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                message=f'该账号在当前登录来源的失败次数过多，剩余 {remaining_minutes} 分钟'
            )
            db.session.add(login_log)
            db.session.commit()
            return render_login_page()

        # 验证用户信息
        user = User.query.filter_by(username=username).first()
        if user:
            login_success = user.check_password(password)

            if login_success:
                # 登录成功，清除失败计数
                login_clear_failures(failure_keys)
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
                new_fail_count = login_record_failure(failure_keys)

                # 显示剩余尝试次数
                remaining_attempts = LOGIN_MAX_ATTEMPTS - new_fail_count
                if remaining_attempts > 0:
                    flash(f'用户名或密码错误，还有 {remaining_attempts} 次尝试机会')
                    message = f'用户名或密码错误，剩余 {remaining_attempts} 次尝试机会'
                else:
                    flash(f'该账号在当前登录来源的失败次数过多，已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟')
                    message = f'该账号在当前登录来源的失败次数过多，已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟'

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
            new_fail_count = login_record_failure(failure_keys)

            # 显示剩余尝试次数
            remaining_attempts = LOGIN_MAX_ATTEMPTS - new_fail_count
            if remaining_attempts > 0:
                flash(f'用户名或密码错误，还有 {remaining_attempts} 次尝试机会')
                message = f'用户名或密码错误，剩余 {remaining_attempts} 次尝试机会'
            else:
                flash(f'该账号在当前登录来源的失败次数过多，已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟')
                message = f'该账号在当前登录来源的失败次数过多，已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟'

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
