# -*- coding: utf-8 -*-
"""Flask application core: app instance, security headers, runtime dirs."""
import os
import secrets
import time
from datetime import timedelta
from urllib.parse import urlparse

import urllib3
from flask import Flask, abort, flash, redirect, request, send_file, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import safe_join

from mangadock.extensions import csrf, db
from mangadock.settings import BASE_DIR, COMIC_ROOT, COVER_ROOT, STATIC_FOLDER, TEMPLATE_FOLDER

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(
    __name__,
    template_folder=TEMPLATE_FOLDER,
    static_folder=STATIC_FOLDER,
    instance_path=os.path.join(BASE_DIR, 'instance'),
    instance_relative_config=False,
)


def load_persistent_secret_key():
    env_secret = os.environ.get('MANGADOCK_SECRET_KEY', '').strip()
    if env_secret:
        return env_secret

    os.makedirs(app.instance_path, exist_ok=True)
    secret_file = os.path.join(app.instance_path, 'session.secret')
    if os.path.exists(secret_file):
        with open(secret_file, 'r', encoding='utf-8') as secret_handle:
            stored_secret = secret_handle.read().strip()
            if stored_secret:
                return stored_secret

    generated_secret = os.urandom(32).hex()
    with open(secret_file, 'w', encoding='utf-8') as secret_handle:
        secret_handle.write(generated_secret)
    try:
        os.chmod(secret_file, 0o600)
    except OSError:
        pass
    return generated_secret


def _parse_cookie_secure_mode():
    """Parse MANGADOCK_COOKIE_SECURE: true/false/auto (default auto)."""
    configured_value = os.environ.get('MANGADOCK_COOKIE_SECURE')
    if configured_value is None or not str(configured_value).strip():
        return 'auto'
    value = str(configured_value).strip().lower()
    if value in ('1', 'true', 'yes', 'on'):
        return 'true'
    if value in ('0', 'false', 'no', 'off'):
        return 'false'
    return 'auto'


def should_use_secure_session_cookie():
    """Static SESSION_COOKIE_SECURE for Flask. 'auto' stays False and is upgraded on HTTPS."""
    return _parse_cookie_secure_mode() == 'true'


def request_is_https():
    if request.is_secure:
        return True
    forwarded = (request.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
    return forwarded == 'https'


class MangaDockSessionInterface(SecureCookieSessionInterface):
    """Emit Secure cookies on HTTPS when mode is auto/true (session is saved after after_request)."""

    def get_cookie_secure(self, app):
        mode = app.config.get('MANGADOCK_COOKIE_SECURE_MODE') or 'auto'
        if mode == 'true':
            return True
        if mode == 'false':
            return False
        # auto: Secure only when the client-facing request is HTTPS
        try:
            return request_is_https()
        except RuntimeError:
            return bool(app.config.get('SESSION_COOKIE_SECURE'))


app.secret_key = load_persistent_secret_key()
app.config['SESSION_COOKIE_NAME'] = 'mangadock_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = should_use_secure_session_cookie()
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MANGADOCK_COOKIE_SECURE_MODE'] = _parse_cookie_secure_mode()
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True
app.config['WTF_CSRF_SSL_STRICT'] = False
app.config['PREFERRED_URL_SCHEME'] = 'https'
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MANGADOCK_MAX_UPLOAD_MB', '8')) * 1024 * 1024
app.config['TEMPLATES_AUTO_RELOAD'] = False
app.jinja_env.cache_size = 1000
app.jinja_env.auto_reload = False
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.instance_path, 'download_tasks.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# 2026-09-22 code review P1/P2：DB 在 SMB 网络盘（/Volumes/NAS/...）上。
# SQLite 单写者 + 网络文件系统：WAL 在 SMB 上共享内存/文件锁语义不可靠 ——
# 实测 PRAGMA journal_mode=WAL 虽返回 'wal'，但未生成 -wal/-shm 文件（见修复说明），
# 存在损坏风险，故刻意不启用 WAL（DB 在 SMB 上，WAL 不可用）。
# 改用 busy_timeout=10000ms（二轮复审自 3000 上调：11 个写者共享单库，3s 偏紧）
# + synchronous=NORMAL：写冲突快速失败/重试，而不是像原 connect_args timeout=30
# 那样把请求线程挂住 30 秒。
# tasks.py 的频繁 commit 不在本次允许修改的文件内，靠此处 busy_timeout 缓解写竞争。
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    # 仅作兜底：连接级忙等上限（秒）。真正的忙等待由下方 busy_timeout PRAGMA(10000ms) 控制。
    'connect_args': {'timeout': 15},
}


# 每次新建底层 SQLite 连接时执行：busy_timeout=10000 让写冲突快速重试/失败而不长时间
# 阻塞请求线程；synchronous=NORMAL 在掉电安全与写入性能间取平衡（配合网络盘更稳妥）。
# 注：WAL 已在上方说明因 SMB 不可用而刻意不开。
def _configure_sqlite_connection(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


from sqlalchemy import event
from sqlalchemy.engine import Engine

event.listen(Engine, 'connect', _configure_sqlite_connection)
app.session_interface = MangaDockSessionInterface()

if not os.path.exists(app.instance_path):
    os.makedirs(app.instance_path)

db.init_app(app)
csrf.init_app(app)


def is_safe_next_target(target):
    if not target or not isinstance(target, str) or len(target) > 512:
        return False
    if any(ord(character) < 32 or character in '\\' for character in target):
        return False
    if not target.startswith('/') or target.startswith('//'):
        return False
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    path = parsed.path or ''
    if not path.startswith('/') or path.startswith('//'):
        return False
    return True


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    flash('表单已失效或代理校验失败，请刷新页面后重试登录')
    next_target = request.args.get('next')
    if not next_target and request.referrer:
        referrer_path = urlparse(request.referrer).path
        if referrer_path and referrer_path != request.path:
            next_target = referrer_path
    if not next_target or next_target == request.path:
        next_target = url_for('index')
    if not is_safe_next_target(next_target):
        next_target = url_for('index')
    return redirect(url_for('login', next=next_target)), 400


@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault(
        'Permissions-Policy',
        'camera=(), microphone=(), geolocation=(), payment=()',
    )
    # Scripts/styles are self-hosted; keep unsafe-inline for existing inline bootstraps.
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'self'"
    )
    if request_is_https():
        response.headers.setdefault(
            'Strict-Transport-Security',
            'max-age=31536000; includeSubDomains',
        )
    return response


def ensure_runtime_dirs():
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(COMIC_ROOT, exist_ok=True)
    os.makedirs(COVER_ROOT, exist_ok=True)
    os.makedirs(os.path.join(COVER_ROOT, 'hero'), exist_ok=True)


ensure_runtime_dirs()


# --- 封面静态路由：SMB 抖动重试（2026-09-20）---
# NAS 挂载上 stat/open 偶发瞬时失败（表现为同一文件先 404 几秒后 200），
# Flask 内建静态路由一次失败就 404，前端 onerror 立即换上 cover.png 占位图。
# 本路由拦截 /static/cover/*，失败时在数秒窗口内重试后再放弃。
_COVER_RETRY_DELAYS = (0.0, 0.4, 1.0, 2.5)  # 总重试窗口约 4 秒


@app.route('/static/cover/<path:filename>')
def resilient_cover_file(filename):
    if filename != 'cover.png':
        from mangadock.auth import (
            authenticate_api_credentials, get_basic_auth_credentials,
            get_current_user, is_basic_auth_request,
        )
        from mangadock.services.groups import can_user_access_group, get_comic_group_map

        if is_basic_auth_request():
            username, password = get_basic_auth_credentials()
            user = None
            if username is not None:
                user, _ = authenticate_api_credentials(
                    username, password, request.remote_addr or '', request.user_agent.string
                )
        else:
            user = get_current_user()
        if not user:
            abort(403)
        if not user.is_admin and not filename.startswith('novels/'):
            if filename.startswith('home-banner/'):
                import hashlib
                from mangadock.services.library import get_available_comics

                banner_key = os.path.basename(filename).split('-', 1)[0].split('.', 1)[0]
                comic_name = next((
                    comic['comic_name'] for comic in get_available_comics()
                    if hashlib.sha256(comic['comic_name'].strip().encode('utf-8')).hexdigest()
                    == banner_key
                ), None)
                if not comic_name:
                    abort(403)
            else:
                comic_name = os.path.splitext(os.path.basename(filename))[0]
            group = get_comic_group_map().get(comic_name) or '默认分组'
            if not can_user_access_group(group, user):
                abort(403)

    target = safe_join(app.static_folder, 'cover', filename)
    if target is None:
        abort(404)
    for delay in _COVER_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            os.stat(target)
            response = send_file(target, conditional=True)
            if filename != 'cover.png':
                response.headers['Cache-Control'] = 'private, no-cache'
            return response
        except FileNotFoundError:
            # 文件确实不存在：仍走完短重试，规避 SMB 目录负缓存滞后
            continue
        except OSError:
            continue
    app.logger.warning('cover serve failed after retries: %s', filename)
    abort(404)


# Worker lock paths depend on instance_path
TASK_WORKER_LOCK_PREFIX = os.path.join(app.instance_path, 'task_worker')
COMMAND_WORKER_LOCK_PATH = os.path.join(app.instance_path, 'command_worker.lock')
API_PAGE_CACHE_ROOT = os.path.join(app.instance_path, 'api_page_cache')
