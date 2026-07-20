# -*- coding: utf-8 -*-
"""Flask application core: app instance, security headers, runtime dirs."""
import os
import secrets
from datetime import timedelta
from urllib.parse import urlparse

import urllib3
from flask import Flask, flash, redirect, request, url_for
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

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


def should_use_secure_session_cookie():
    configured_value = os.environ.get('MANGADOCK_COOKIE_SECURE')
    if configured_value is not None:
        return configured_value.lower() == 'true'
    return False


app.secret_key = load_persistent_secret_key()
app.config['SESSION_COOKIE_NAME'] = 'mangadock_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = should_use_secure_session_cookie()
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
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

if not os.path.exists(app.instance_path):
    os.makedirs(app.instance_path)

db.init_app(app)
csrf.init_app(app)


def is_safe_next_target(target):
    if not target:
        return False
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    return target.startswith('/')


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
        'Content-Security-Policy',
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdnjs.cloudflare.com; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'self'"
    )
    return response


def ensure_runtime_dirs():
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(COMIC_ROOT, exist_ok=True)
    os.makedirs(COVER_ROOT, exist_ok=True)
    os.makedirs(os.path.join(COVER_ROOT, 'hero'), exist_ok=True)


ensure_runtime_dirs()

# Worker lock paths depend on instance_path
TASK_WORKER_LOCK_PREFIX = os.path.join(app.instance_path, 'task_worker')
COMMAND_WORKER_LOCK_PATH = os.path.join(app.instance_path, 'command_worker.lock')
API_PAGE_CACHE_ROOT = os.path.join(app.instance_path, 'api_page_cache')
