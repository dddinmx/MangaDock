# -*- coding: utf-8 -*-
import os, re, sys, time, math, atexit, requests, shutil, glob, json, img2pdf, random, threading, urllib3, subprocess, tempfile, hashlib, base64, secrets, ipaddress, socket
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from urllib.parse import urljoin, urlparse
import zipfile
from PIL import Image
from natsort import natsorted
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session, flash, g
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, or_, UniqueConstraint
from sqlalchemy.exc import OperationalError
import uuid
import fcntl
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf.csrf import CSRFProtect, CSRFError

china_tz = timezone(timedelta(hours=8))
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)


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


# 使用固定强密钥，避免服务重启后所有会话失效
app.secret_key = load_persistent_secret_key()

def should_use_secure_session_cookie():
    configured_value = os.environ.get('MANGADOCK_COOKIE_SECURE')
    if configured_value is not None:
        return configured_value.lower() == 'true'
    return False

# 加强 session 和 cookie 安全配置
app.config['SESSION_COOKIE_NAME'] = 'mangadock_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True  # 防止 JavaScript 访问 cookie
app.config['SESSION_COOKIE_SECURE'] = should_use_secure_session_cookie()
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # 防止 CSRF 攻击
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)  # 会话有效期 24 小时
app.config['SESSION_REFRESH_EACH_REQUEST'] = True  # 每次请求刷新会话
app.config['WTF_CSRF_SSL_STRICT'] = False  # 兼容反向代理 HTTPS 场景，避免 Referer/Host 不一致导致 400
app.config['PREFERRED_URL_SCHEME'] = 'https'
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MANGADOCK_MAX_UPLOAD_MB', '8')) * 1024 * 1024

# 模板渲染优化
app.config['TEMPLATES_AUTO_RELOAD'] = False  # 禁用模板自动重载（生产环境）
app.jinja_env.cache_size = 1000  # 增大模板缓存大小
app.jinja_env.auto_reload = False

app.wsgi_app = ProxyFix(
    app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1
)

# 静态文件缓存配置
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1年缓存

# 初始化 CSRF 保护
csrf = CSRFProtect(app)


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

# 登录失败计数存储（内存存储，重启后会重置）
login_failures = {}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_DURATION = timedelta(minutes=15)

# 数据库配置 - SQLite 特定配置（使用 instance 文件夹）
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.instance_path, 'download_tasks.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# 确保 instance 文件夹存在
if not os.path.exists(app.instance_path):
    os.makedirs(app.instance_path)
db = SQLAlchemy(app)

# 配置参数
CONFIG = {
    'max_workers': 2,
    'request_timeout': 20,
    'retry_times': 3,
    'queue_buffer': 10,
    'delay_range': (0.1, 0.5)  # 延迟
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMIC_ROOT = os.path.join(BASE_DIR, "comic")
COVER_ROOT = os.path.join(BASE_DIR, "static", "cover")
COMIC_MAPPING_FILE = os.path.join(BASE_DIR, "comic.json")
BAOZIMH_ORG_IMAGE_HOST = "https://f40-1-4.g-mh.online"
BAOZIMH_ORG_DEFAULT_IMAGE_HOST = "https://t40-1-4.g-mh.online"
BAOZIMH_ORG_ENCODED_IMAGE_TRANSLATION = str.maketrans(
    "_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
SAFE_HTTP_ALLOWED_HOST_SUFFIXES = (
    '6wm.top',
    'baozimh.com',
    'baozimhcn.com',
    'baozimh.org',
    'bzcdn.net',
    'cnbzmg.com',
    'mgsearcher.com',
    'g-mh.online',
    'jjmhw6.top',
    'mxs12.cc',
    'twbzmg.com',
    'wzd1.cc',
)
SAFE_HTTP_ALLOWED_PROXY_NETWORKS = (
    ipaddress.ip_network('198.18.0.0/15'),
)
MAX_HTML_RESPONSE_BYTES = int(os.environ.get('MANGADOCK_MAX_HTML_MB', '8')) * 1024 * 1024
MAX_IMAGE_RESPONSE_BYTES = int(os.environ.get('MANGADOCK_MAX_IMAGE_MB', '30')) * 1024 * 1024
MAX_ARCHIVE_ENTRY_BYTES = int(os.environ.get('MANGADOCK_MAX_ARCHIVE_ENTRY_MB', '50')) * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = int(os.environ.get('MANGADOCK_MAX_ARCHIVE_TOTAL_MB', '500')) * 1024 * 1024
MAX_ARCHIVE_PAGES = int(os.environ.get('MANGADOCK_MAX_ARCHIVE_PAGES', '1200'))
MAX_PDF_PAGES = int(os.environ.get('MANGADOCK_MAX_PDF_PAGES', '1200'))
PDF_TOOL_TIMEOUT_SECONDS = int(os.environ.get('MANGADOCK_PDF_TOOL_TIMEOUT', '30'))
Image.MAX_IMAGE_PIXELS = int(os.environ.get('MANGADOCK_MAX_IMAGE_PIXELS', '36000000'))


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def should_verify_upstream_tls(requested_verify=True):
    if requested_verify is False and env_flag('MANGADOCK_ALLOW_INSECURE_UPSTREAM', False):
        return False
    return True


def is_allowed_upstream_host(hostname):
    normalized_host = (hostname or '').strip().lower().rstrip('.')
    return any(
        normalized_host == suffix or normalized_host.endswith(f'.{suffix}')
        for suffix in SAFE_HTTP_ALLOWED_HOST_SUFFIXES
    )


def is_public_ip_address(ip_value):
    try:
        ip_address = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    return not (
        ip_address.is_private
        or ip_address.is_loopback
        or ip_address.is_link_local
        or ip_address.is_multicast
        or ip_address.is_reserved
        or ip_address.is_unspecified
    )


def is_safe_resolved_upstream_address(hostname, ip_value):
    if is_public_ip_address(ip_value):
        return True
    try:
        ip_address = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    return (
        is_allowed_upstream_host(hostname)
        and any(ip_address in network for network in SAFE_HTTP_ALLOWED_PROXY_NETWORKS)
    )


def validate_safe_upstream_url(url):
    parsed = urlparse(url or '')
    if parsed.scheme != 'https':
        raise ValueError('仅允许访问 HTTPS 上游地址')
    if not parsed.hostname or not is_allowed_upstream_host(parsed.hostname):
        raise ValueError('上游地址域名不在允许列表中')

    try:
        literal_ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_ip = None
    if literal_ip and not is_public_ip_address(str(literal_ip)):
        raise ValueError('不允许访问内网或保留地址')

    try:
        resolved_addresses = {
            result[4][0]
            for result in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError('上游域名无法解析') from exc

    if not resolved_addresses or any(
        not is_safe_resolved_upstream_address(parsed.hostname, address)
        for address in resolved_addresses
    ):
        raise ValueError('上游域名解析到不安全地址')

    return url


def enforce_response_size(response, max_bytes, stream=False):
    if not max_bytes:
        return
    content_length = response.headers.get('Content-Length')
    if content_length:
        try:
            parsed_content_length = int(content_length)
        except ValueError:
            raise ValueError('上游响应大小无效')
        if parsed_content_length > max_bytes:
            raise ValueError('上游响应超过大小限制')
    if not stream and len(response.content) > max_bytes:
        raise ValueError('上游响应超过大小限制')


def safe_http_get(url, headers=None, timeout=None, stream=False, verify=True, max_bytes=MAX_HTML_RESPONSE_BYTES, session_obj=None):
    current_url = validate_safe_upstream_url(url)
    requester = session_obj or requests
    timeout = timeout or CONFIG['request_timeout']
    verify = should_verify_upstream_tls(verify)

    for _ in range(4):
        response = requester.get(
            current_url,
            headers=headers,
            timeout=timeout,
            stream=stream,
            verify=verify,
            allow_redirects=False
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get('Location')
            response.close()
            if not location:
                raise ValueError('上游重定向缺少 Location')
            current_url = validate_safe_upstream_url(urljoin(current_url, location))
            continue
        enforce_response_size(response, max_bytes, stream=stream)
        return response

    raise ValueError('上游重定向次数过多')


def write_limited_response_to_file(response, output_path, max_bytes):
    written = 0
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    try:
        with open(output_path, 'wb') as output_file:
            for chunk in response.iter_content(8192):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError('下载内容超过大小限制')
                output_file.write(chunk)
    except Exception:
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise


def resolve_file_under_directory(base_dir, relative_filename):
    if not base_dir or not relative_filename:
        return None
    absolute_base = os.path.realpath(os.path.abspath(base_dir))
    candidate = os.path.realpath(os.path.abspath(os.path.join(absolute_base, relative_filename)))
    if candidate != absolute_base and candidate.startswith(absolute_base + os.sep):
        return candidate
    return None


def ensure_runtime_dirs():
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(COMIC_ROOT, exist_ok=True)
    os.makedirs(COVER_ROOT, exist_ok=True)


ensure_runtime_dirs()

class DownloadTask(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    comic_name = db.Column(db.String(255))
    url = db.Column(db.String(512))
    status = db.Column(db.String(20), default='pending')  # pending, running, completed, cancelled, error
    progress_percent = db.Column(db.Integer, default=0)
    total_chapters = db.Column(db.Integer, default=0)
    completed_chapters = db.Column(db.Integer, default=0)
    comic_format = db.Column(db.Integer, default=1)
    log = db.Column(db.Text, default='')
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))
    is_update = db.Column(db.Boolean, default=False)
    group = db.Column(db.String(255), default='默认分组')  # 分组字段

class ReadingProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    comic_name = db.Column(db.String(255), nullable=False)
    last_chapter = db.Column(db.Integer, default=0)
    last_page = db.Column(db.Integer, default=0)
    scroll_position = db.Column(db.Integer, default=0)  # 滚动位置
    total_chapters = db.Column(db.Integer, default=0)
    total_pages = db.Column(db.Integer, default=0)
    last_read_at = db.Column(db.DateTime, default=datetime.now(china_tz))
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))


class ReadingTime(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    comic_name = db.Column(db.String(255), nullable=False)
    duration = db.Column(db.Integer, nullable=False)  # in minutes
    duration_seconds = db.Column(db.Integer, nullable=False, default=0)
    read_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))


class ReadingSessionState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    comic_name = db.Column(db.String(255), nullable=False)
    session_key = db.Column(db.String(128), nullable=False)
    last_reported_seconds = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))
    updated_at = db.Column(
        db.DateTime,
        default=datetime.now(china_tz),
        onupdate=datetime.now(china_tz)
    )

    __table_args__ = (
        UniqueConstraint('user_id', 'comic_name', 'session_key', name='uq_reading_session_state'),
    )


class ComicGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))


class ComicGroupMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    comic_name = db.Column(db.String(255), unique=True, nullable=False)
    group_name = db.Column(db.String(255), nullable=False, default='默认分组')
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz), onupdate=lambda: datetime.now(china_tz))


class UserGroupPermission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    group_name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))


class AdminHiddenLibraryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    target_type = db.Column(db.String(20), nullable=False)
    target_value = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))

    __table_args__ = (
        UniqueConstraint('user_id', 'target_type', 'target_value', name='uq_admin_hidden_library_item'),
    )


class ComicScanPath(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(1024), unique=True, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(china_tz),
        onupdate=lambda: datetime.now(china_tz)
    )


class ComicIdentity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    comic_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    comic_name = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(china_tz),
        onupdate=lambda: datetime.now(china_tz)
    )


class ComicUpdateCheck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    comic_name = db.Column(db.String(255), unique=True, nullable=False)
    source_url = db.Column(db.String(1024), nullable=False)
    has_updates = db.Column(db.Boolean, nullable=False, default=False)
    pending_chapters = db.Column(db.Integer, nullable=False, default=0)
    remote_total_chapters = db.Column(db.Integer, nullable=False, default=0)
    local_total_chapters = db.Column(db.Integer, nullable=False, default=0)
    latest_chapter_title = db.Column(db.String(255))
    status = db.Column(db.String(20), nullable=False, default='pending')
    error_message = db.Column(db.String(500))
    last_checked_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(china_tz),
        onupdate=lambda: datetime.now(china_tz)
    )


class BackgroundCommand(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    command_type = db.Column(db.String(64), nullable=False, index=True)
    payload = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    requested_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    message = db.Column(db.String(500))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(china_tz),
        onupdate=lambda: datetime.now(china_tz)
    )


class AppSetting(db.Model):
    key = db.Column(db.String(128), primary_key=True)
    value = db.Column(db.String(500), nullable=False, default='')
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(china_tz),
        onupdate=lambda: datetime.now(china_tz)
    )


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')

    def set_password(self, password):
        # 使用 pbkdf2:sha256 算法，确保在 Docker 精简镜像中也能正常工作
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def check_password(self, password):
        try:
            return check_password_hash(self.password_hash, password)
        except (TypeError, ValueError):
            return False

    @property
    def is_admin(self):
        return self.role == 'admin'


class LoginLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    ip_address = db.Column(db.String(45), nullable=False)
    user_agent = db.Column(db.String(500))
    login_time = db.Column(db.DateTime, default=datetime.now(china_tz))
    success = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.String(200))

def ensure_sqlite_column(table_name, column_name, ddl):
    with db.engine.begin() as connection:
        result = connection.execute(text(f'PRAGMA table_info("{table_name}")'))
        existing_columns = {row[1] for row in result.fetchall()}
        if column_name in existing_columns:
            return
        try:
            connection.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN {column_name} {ddl}'))
        except OperationalError as exc:
            if 'duplicate column name' not in str(exc).lower():
                raise


def initialize_database():
    with app.app_context():
        for attempt in range(3):
            try:
                db.create_all()
                break
            except OperationalError as exc:
                if 'already exists' not in str(exc).lower() or attempt == 2:
                    raise
                time.sleep(0.1)

        ensure_sqlite_column(User.__table__.name, 'role', "VARCHAR(20) DEFAULT 'user'")
        ensure_sqlite_column(ReadingProgress.__table__.name, 'user_id', 'INTEGER')
        ensure_sqlite_column(ReadingTime.__table__.name, 'user_id', 'INTEGER')
        ensure_sqlite_column(ReadingTime.__table__.name, 'duration_seconds', 'INTEGER DEFAULT 0')

        if not ComicGroup.query.filter_by(name='默认分组').first():
            db.session.add(ComicGroup(name='默认分组'))
            db.session.commit()

        admin_user = User.query.filter_by(username="admin").first()
        if not admin_user:
            admin_user = User(username="admin", role='admin')
            initial_admin_password = os.environ.get('MANGADOCK_ADMIN_PASSWORD', '').strip()
            if not initial_admin_password:
                initial_admin_password = secrets.token_urlsafe(18)
                print(f"已生成初始管理员密码：admin / {initial_admin_password}")
            admin_user.set_password(initial_admin_password)
            db.session.add(admin_user)
            db.session.commit()
            print("已创建默认管理员用户：admin")
        elif admin_user.role != 'admin':
            admin_user.role = 'admin'
            db.session.commit()

        User.query.filter(
            or_(User.role.is_(None), User.role == '')
        ).update({'role': 'user'}, synchronize_session=False)
        db.session.commit()

        db.session.execute(
            text(f'UPDATE "{ReadingProgress.__table__.name}" SET user_id = :user_id WHERE user_id IS NULL'),
            {'user_id': admin_user.id}
        )
        db.session.execute(
            text(f'UPDATE "{ReadingTime.__table__.name}" SET user_id = :user_id WHERE user_id IS NULL'),
            {'user_id': admin_user.id}
        )
        db.session.execute(
            text(
                f'UPDATE "{ReadingTime.__table__.name}" '
                'SET duration_seconds = CASE '
                'WHEN duration_seconds IS NULL OR duration_seconds <= 0 THEN COALESCE(duration, 0) * 60 '
                'ELSE duration_seconds END'
            )
        )
        db.session.commit()
        sync_comic_identity_records()


def normalize_scan_path(scan_path):
    cleaned_path = (scan_path or '').strip().strip('"').strip("'")
    if not cleaned_path:
        return None
    expanded_path = os.path.expandvars(os.path.expanduser(cleaned_path))
    return os.path.abspath(os.path.normpath(expanded_path))


def get_comic_scan_roots(existing_only=False):
    roots = []
    seen_roots = set()

    default_root = normalize_scan_path(COMIC_ROOT)
    if default_root:
        seen_roots.add(default_root)
        if not existing_only or os.path.isdir(default_root):
            roots.append(default_root)

    with app.app_context():
        custom_paths = ComicScanPath.query.filter_by(enabled=True).order_by(ComicScanPath.created_at.asc()).all()

    for scan_path in custom_paths:
        normalized_path = normalize_scan_path(scan_path.path)
        if not normalized_path or normalized_path in seen_roots:
            continue
        seen_roots.add(normalized_path)
        if existing_only and not os.path.isdir(normalized_path):
            continue
        roots.append(normalized_path)

    return roots


def get_scan_path_entries():
    entries = []
    default_root = normalize_scan_path(COMIC_ROOT)
    if default_root:
        entries.append({
            'id': None,
            'path': default_root,
            'enabled': True,
            'is_default': True,
            'exists': os.path.isdir(default_root),
        })

    with app.app_context():
        custom_paths = ComicScanPath.query.order_by(ComicScanPath.created_at.asc()).all()

    for scan_path in custom_paths:
        normalized_path = normalize_scan_path(scan_path.path)
        entries.append({
            'id': scan_path.id,
            'path': normalized_path or scan_path.path,
            'enabled': bool(scan_path.enabled),
            'is_default': False,
            'exists': bool(normalized_path and os.path.isdir(normalized_path)),
        })

    return entries


def is_safe_comic_name(comic_name):
    normalized_name = (comic_name or '').strip()
    return bool(
        normalized_name
        and normalized_name not in {'.', '..'}
        and '/' not in normalized_name
        and '\\' not in normalized_name
    )


def get_comic_directory(comic_name):
    if not is_safe_comic_name(comic_name):
        return None

    normalized_name = comic_name.strip()
    for root in get_comic_scan_roots(existing_only=True):
        comic_path = os.path.join(root, normalized_name)
        if os.path.isdir(comic_path):
            return comic_path
    return None


def iter_local_comic_directories():
    seen_comics = set()
    for root in get_comic_scan_roots(existing_only=True):
        for comic_name, comic_path in list_cached_root_directories(root):
            if comic_name in seen_comics:
                continue
            seen_comics.add(comic_name)
            yield comic_name, comic_path


def generate_comic_id():
    return f"c_{uuid.uuid4().hex[:12]}"


def sync_comic_identity_records(comic_names=None):
    candidate_names = []
    if comic_names is None:
        candidate_names.extend(comic_name for comic_name, _comic_path in iter_local_comic_directories())
        with app.app_context():
            candidate_names.extend(
                comic_name
                for (comic_name,) in db.session.query(DownloadTask.comic_name)
                .filter(DownloadTask.comic_name.isnot(None))
                .distinct()
                .all()
                if comic_name
            )
    else:
        candidate_names.extend(comic_names)

    normalized_names = []
    seen_names = set()
    for comic_name in candidate_names:
        normalized_name = (comic_name or '').strip()
        if not is_safe_comic_name(normalized_name) or normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)
        normalized_names.append(normalized_name)

    if not normalized_names:
        return {}

    with app.app_context():
        existing_records = {
            identity.comic_name: identity
            for identity in ComicIdentity.query.filter(ComicIdentity.comic_name.in_(normalized_names)).all()
        }
        has_changes = False
        now = datetime.now(china_tz)

        for comic_name in normalized_names:
            identity = existing_records.get(comic_name)
            if not identity:
                identity = ComicIdentity(
                    comic_id=generate_comic_id(),
                    comic_name=comic_name,
                    created_at=now,
                    updated_at=now
                )
                db.session.add(identity)
                existing_records[comic_name] = identity
                has_changes = True
            elif not identity.comic_id:
                identity.comic_id = generate_comic_id()
                identity.updated_at = now
                has_changes = True

        if has_changes:
            db.session.commit()

        return existing_records


def ensure_comic_identity(comic_name):
    normalized_name = (comic_name or '').strip()
    if not is_safe_comic_name(normalized_name):
        return None
    identity_map = sync_comic_identity_records([normalized_name])
    return identity_map.get(normalized_name)


def get_comic_identity_by_id(comic_id):
    normalized_id = (comic_id or '').strip()
    if not normalized_id:
        return None
    with app.app_context():
        return ComicIdentity.query.filter_by(comic_id=normalized_id).first()


def resolve_comic_name(comic_reference):
    normalized_reference = (comic_reference or '').strip()
    if not normalized_reference:
        return None

    identity = get_comic_identity_by_id(normalized_reference)
    if identity:
        return identity.comic_name

    if is_safe_comic_name(normalized_reference):
        with app.app_context():
            existing_identity = ComicIdentity.query.filter_by(comic_name=normalized_reference).first()
            if existing_identity:
                return existing_identity.comic_name
        return normalized_reference

    return None


def build_chapter_id(filename):
    normalized_name = (filename or '').strip()
    if not normalized_name:
        return None
    digest = hashlib.sha1(normalized_name.encode('utf-8')).hexdigest()
    return f"ch_{digest[:12]}"


SUPPORTED_PAGE_IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
API_PAGE_CACHE_ROOT = os.path.join(app.instance_path, 'api_page_cache')


def ensure_api_page_cache_dir(comic_id, chapter_id):
    safe_comic_id = re.sub(r'[^A-Za-z0-9_.-]', '_', (comic_id or '').strip()) or 'comic'
    safe_chapter_id = re.sub(r'[^A-Za-z0-9_.-]', '_', (chapter_id or '').strip()) or 'chapter'
    cache_dir = os.path.join(API_PAGE_CACHE_ROOT, safe_comic_id, safe_chapter_id)
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def get_chapter_file_path(comic_name, chapter):
    comic_dir = get_comic_directory(comic_name)
    filename = (chapter or {}).get('filename')
    if not comic_dir or not filename:
        return None
    file_path = resolve_file_under_directory(comic_dir, filename)
    if not file_path:
        return None
    return file_path if os.path.exists(file_path) else None


def list_archive_page_entries(file_path):
    with zipfile.ZipFile(file_path) as archive:
        entries = []
        total_uncompressed_size = 0
        for entry in archive.infolist():
            name = entry.filename
            if (
                name
                and not name.endswith('/')
                and not os.path.basename(name).startswith('.')
                and os.path.splitext(name)[1].lower() in SUPPORTED_PAGE_IMAGE_EXTENSIONS
            ):
                if entry.file_size > MAX_ARCHIVE_ENTRY_BYTES:
                    raise ValueError('CBZ 单页超过大小限制')
                total_uncompressed_size += entry.file_size
                if total_uncompressed_size > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ValueError('CBZ 解压后总大小超过限制')
                entries.append(name)
        if len(entries) > MAX_ARCHIVE_PAGES:
            raise ValueError('CBZ 页数超过限制')
    return natsorted(entries)


def get_pdf_page_count(pdf_path):
    try:
        result = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=PDF_TOOL_TIMEOUT_SECONDS
        )
    except Exception:
        return None

    match = re.search(r'^Pages:\s+(\d+)', result.stdout, re.MULTILINE)
    page_count = int(match.group(1)) if match else None
    if page_count and page_count > MAX_PDF_PAGES:
        raise ValueError('PDF 页数超过限制')
    return page_count


def get_pdf_page_renderer():
    for command in ("pdftoppm", "pdftocairo"):
        if shutil.which(command):
            return command
    return None


def get_chapter_page_count(file_path):
    extension = os.path.splitext(file_path)[1].lower()
    if extension == '.cbz':
        try:
            return len(list_archive_page_entries(file_path))
        except Exception:
            return None
    if extension == '.pdf':
        return get_pdf_page_count(repair_pdf_for_reading(file_path))
    return None


def build_api_page_list(comic_id, chapter_id, file_path):
    total_pages = get_chapter_page_count(file_path)
    if total_pages is None:
        raise ValueError('无法读取章节页数')

    pages = [
        {
            'index': page_index,
            'image_url': url_for(
                'api_comic_chapter_page_image',
                comic_id=comic_id,
                chapter_id=chapter_id,
                page_index=page_index
            ),
        }
        for page_index in range(total_pages)
    ]
    return pages, total_pages


def extract_cbz_page_to_cache(file_path, page_index, cache_dir):
    entries = list_archive_page_entries(file_path)
    if page_index < 0 or page_index >= len(entries):
        return None

    entry_name = entries[page_index]
    extension = os.path.splitext(entry_name)[1].lower() or '.jpg'
    cache_path = os.path.join(cache_dir, f"{page_index:04d}{extension}")
    if os.path.exists(cache_path):
        return cache_path

    with zipfile.ZipFile(file_path) as archive:
        with archive.open(entry_name) as source_handle, open(cache_path, 'wb') as target_handle:
            shutil.copyfileobj(source_handle, target_handle)

    return cache_path


def render_pdf_page_to_cache(file_path, page_index, cache_dir):
    renderer = get_pdf_page_renderer()
    if not renderer:
        raise RuntimeError('服务器缺少 PDF 页面渲染工具（pdftoppm/pdftocairo）')

    readable_pdf = repair_pdf_for_reading(file_path)
    total_pages = get_pdf_page_count(readable_pdf)
    if total_pages is None:
        raise RuntimeError('无法读取 PDF 页数')
    if page_index < 0 or page_index >= total_pages:
        return None

    output_prefix = os.path.join(cache_dir, f"{page_index:04d}")
    cache_path = f"{output_prefix}.png"
    if os.path.exists(cache_path):
        return cache_path

    page_number = page_index + 1
    command = [
        renderer,
        '-f', str(page_number),
        '-l', str(page_number),
        '-png',
        '-singlefile',
        readable_pdf,
        output_prefix,
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=PDF_TOOL_TIMEOUT_SECONDS)

    return cache_path if os.path.exists(cache_path) else None


def resolve_chapter_page_image(file_path, comic_id, chapter_id, page_index):
    cache_dir = ensure_api_page_cache_dir(comic_id, chapter_id)
    extension = os.path.splitext(file_path)[1].lower()
    if extension == '.cbz':
        return extract_cbz_page_to_cache(file_path, page_index, cache_dir)
    if extension == '.pdf':
        return render_pdf_page_to_cache(file_path, page_index, cache_dir)
    return None


def detect_local_comic_format(comic_path):
    comic_name = os.path.basename(os.path.normpath(comic_path))
    return get_cached_local_comic_scan(comic_name, comic_path).get('comic_format')


def resolve_comic_file_request(filename):
    normalized_request_path = os.path.normpath((filename or '').replace('\\', '/')).lstrip('/')
    if not normalized_request_path or normalized_request_path.startswith('..'):
        return None

    path_parts = [part for part in normalized_request_path.split('/') if part]
    if len(path_parts) < 2:
        return None

    comic_name = path_parts[0]
    relative_filename = os.path.join(*path_parts[1:])
    comic_dir = get_comic_directory(comic_name)
    if not comic_dir:
        return None

    absolute_comic_dir = os.path.realpath(os.path.abspath(comic_dir))
    absolute_file_path = resolve_file_under_directory(absolute_comic_dir, relative_filename)

    if not absolute_file_path or not os.path.isfile(absolute_file_path):
        return None

    return {
        'comic_name': comic_name,
        'comic_dir': absolute_comic_dir,
        'relative_filename': relative_filename,
        'file_path': absolute_file_path,
    }


comic_root_listing_cache = {}
comic_directory_scan_cache = {}
comic_scan_cache_lock = threading.Lock()
update_check_cache_state = {
    'refreshing': False,
}
update_check_cache_lock = threading.Lock()
UPDATE_CHECK_INTERVAL = timedelta(days=1)
WORKER_POLL_INTERVAL_SECONDS = 2
WORKER_SCHEDULE_HEARTBEAT_SECONDS = 600
TASK_WORKER_COUNT = 2
TASK_WORKER_LOCK_PREFIX = os.path.join(app.instance_path, 'task_worker')
COMMAND_WORKER_LOCK_PATH = os.path.join(app.instance_path, 'command_worker.lock')
COMIC_UPDATE_MODE_SETTING_KEY = 'comic_update_mode'
COMIC_UPDATE_MODE_MANUAL = 'manual'
COMIC_UPDATE_MODE_AUTO = 'auto'
COMIC_UPDATE_MODES = {COMIC_UPDATE_MODE_MANUAL, COMIC_UPDATE_MODE_AUTO}


def get_directory_signature(path):
    try:
        stat_result = os.stat(path)
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)


def list_cached_root_directories(root):
    normalized_root = normalize_scan_path(root)
    if not normalized_root or not os.path.isdir(normalized_root):
        return []

    signature = get_directory_signature(normalized_root)
    if signature is None:
        return []

    with comic_scan_cache_lock:
        cached_entry = comic_root_listing_cache.get(normalized_root)
        if cached_entry and cached_entry.get('signature') == signature:
            return list(cached_entry.get('directories', ()))

    directories = []
    try:
        with os.scandir(normalized_root) as root_entries:
            for entry in root_entries:
                if not entry.is_dir():
                    continue
                directories.append((entry.name, entry.path))
    except OSError:
        return []

    directories.sort(key=lambda item: item[0])

    with comic_scan_cache_lock:
        comic_root_listing_cache[normalized_root] = {
            'signature': signature,
            'directories': tuple(directories),
        }

    return directories


def get_cached_local_comic_scan(comic_name, comic_path=None):
    resolved_path = comic_path or get_comic_directory(comic_name)
    if not resolved_path or not os.path.isdir(resolved_path):
        return {
            'available_chapters': 0,
            'comic_format': None,
        }

    normalized_path = os.path.abspath(resolved_path)
    signature = get_directory_signature(normalized_path)
    if signature is None:
        return {
            'available_chapters': 0,
            'comic_format': None,
        }

    with comic_scan_cache_lock:
        cached_entry = comic_directory_scan_cache.get(normalized_path)
        if cached_entry and cached_entry.get('signature') == signature:
            return dict(cached_entry['data'])

    pdf_count = 0
    cbz_count = 0
    chapter_count = 0
    try:
        with os.scandir(normalized_path) as chapter_entries:
            for entry in chapter_entries:
                if not entry.is_file():
                    continue
                filename = entry.name
                if is_valid_local_chapter_file(filename, ('.pdf',)):
                    pdf_count += 1
                    chapter_count += 1
                elif is_valid_local_chapter_file(filename, ('.cbz',)):
                    cbz_count += 1
                    chapter_count += 1
    except OSError:
        return {
            'available_chapters': 0,
            'comic_format': None,
        }

    if pdf_count and cbz_count:
        comic_format = 1 if pdf_count > cbz_count else 2
    elif pdf_count:
        comic_format = 1
    elif cbz_count:
        comic_format = 2
    else:
        comic_format = None

    scan_data = {
        'available_chapters': chapter_count,
        'comic_format': comic_format,
    }

    with comic_scan_cache_lock:
        comic_directory_scan_cache[normalized_path] = {
            'signature': signature,
            'data': dict(scan_data),
        }

    return scan_data


def load_comic_mapping():
    json_file_path = COMIC_MAPPING_FILE
    if not os.path.exists(json_file_path) or os.path.getsize(json_file_path) <= 0:
        return {}
    try:
        with open(json_file_path, "r", encoding="utf-8") as json_file:
            comic_data = json.load(json_file)
            return comic_data if isinstance(comic_data, dict) else {}
    except Exception:
        return {}


def get_local_chapter_bases(comic_name):
    comic_path = get_comic_directory(comic_name)
    if not comic_path or not os.path.isdir(comic_path):
        return set()

    chapter_bases = set()
    try:
        with os.scandir(comic_path) as chapter_entries:
            for entry in chapter_entries:
                if not entry.is_file():
                    continue
                if is_valid_local_chapter_file(entry.name):
                    chapter_bases.add(os.path.splitext(entry.name)[0])
    except OSError:
        return set()
    return chapter_bases


def get_local_chapter_match_bases(comic_name):
    match_bases = set()
    for chapter in list_local_chapters(comic_name):
        filename = chapter.get('filename') or ''
        filename_stem = os.path.splitext(filename)[0] if filename else ''
        if filename_stem:
            match_bases.add(filename_stem)

        title = sanitize_filename(chapter.get('title') or '')
        if not title:
            continue

        match_bases.add(title)
        order = chapter.get('order')
        if isinstance(order, int) and order > 0:
            match_bases.add(f"{order:04d}_{title}")

    return match_bases


def is_existing_local_chapter(chapter, local_match_bases):
    title = sanitize_filename(chapter.get('title') or '')
    chapter_keys = set()

    filename_base = chapter.get('filename_base')
    if filename_base:
        chapter_keys.add(filename_base)

    if title:
        chapter_keys.add(title)

    order = chapter.get('order')
    if isinstance(order, int) and order > 0 and title:
        chapter_keys.add(f"{order:04d}_{title}")

    return any(key in local_match_bases for key in chapter_keys)


def get_update_check_map():
    with app.app_context():
        return {
            item.comic_name: item
            for item in ComicUpdateCheck.query.all()
        }


def get_last_update_check_time():
    with app.app_context():
        return db.session.query(db.func.max(ComicUpdateCheck.last_checked_at)).scalar()


def normalize_china_datetime(value):
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=china_tz)
    return value.astimezone(china_tz)


def get_update_candidates():
    with app.app_context():
        return ComicUpdateCheck.query.filter_by(has_updates=True).order_by(
            ComicUpdateCheck.pending_chapters.desc(),
            ComicUpdateCheck.last_checked_at.desc(),
            ComicUpdateCheck.comic_name.asc()
        ).all()


def get_app_setting(key, default_value=''):
    with app.app_context():
        setting = db.session.get(AppSetting, key)
        if not setting:
            return default_value
        return setting.value


def set_app_setting(key, value):
    with app.app_context():
        setting = db.session.get(AppSetting, key)
        if not setting:
            setting = AppSetting(key=key, value=value)
            db.session.add(setting)
        else:
            setting.value = value
            setting.updated_at = datetime.now(china_tz)
        db.session.commit()
        return setting.value


def get_comic_update_mode():
    mode = get_app_setting(COMIC_UPDATE_MODE_SETTING_KEY, COMIC_UPDATE_MODE_MANUAL)
    if mode not in COMIC_UPDATE_MODES:
        return COMIC_UPDATE_MODE_MANUAL
    return mode


def set_comic_update_mode(mode):
    normalized_mode = (mode or '').strip().lower()
    if normalized_mode not in COMIC_UPDATE_MODES:
        raise ValueError('更新模式无效')
    return set_app_setting(COMIC_UPDATE_MODE_SETTING_KEY, normalized_mode)


def resolve_update_format(comic_name):
    comic_path = get_comic_directory(comic_name)
    if comic_path:
        comic_format = detect_local_comic_format(comic_path)
        if comic_format in (1, 2):
            return comic_format
    return 2


def has_active_update_task(comic_name):
    with app.app_context():
        return DownloadTask.query.filter(
            DownloadTask.is_update.is_(True),
            DownloadTask.comic_name == comic_name,
            DownloadTask.status.in_(['pending', 'running'])
        ).first() is not None


def enqueue_auto_update_tasks(update_candidates):
    queued_count = 0
    for candidate in update_candidates:
        comic_name = (candidate.comic_name or '').strip()
        source_url = (candidate.source_url or '').strip()
        if not comic_name or not source_url:
            continue
        if has_active_update_task(comic_name):
            continue

        comic_format = resolve_update_format(comic_name)
        start_update_task(comic_name, comic_format, source_url)
        queued_count += 1
    return queued_count


def queue_background_command(command_type, payload=None, dedupe_pending=True):
    serialized_payload = json.dumps(payload or {}, ensure_ascii=False)
    with app.app_context():
        if dedupe_pending:
            existing_command = BackgroundCommand.query.filter(
                BackgroundCommand.command_type == command_type,
                BackgroundCommand.status.in_(['pending', 'running'])
            ).order_by(BackgroundCommand.requested_at.asc()).first()
            if existing_command:
                return existing_command

        command = BackgroundCommand(
            command_type=command_type,
            payload=serialized_payload,
            status='pending'
        )
        db.session.add(command)
        db.session.commit()
        return command


def claim_next_background_command():
    with app.app_context():
        while True:
            command = BackgroundCommand.query.filter_by(status='pending').order_by(
                BackgroundCommand.requested_at.asc(),
                BackgroundCommand.id.asc()
            ).first()
            if not command:
                return None

            now = datetime.now(china_tz)
            updated_rows = BackgroundCommand.query.filter_by(id=command.id, status='pending').update(
                {
                    'status': 'running',
                    'started_at': now,
                    'finished_at': None,
                    'updated_at': now,
                },
                synchronize_session=False
            )
            db.session.commit()
            if updated_rows:
                return command.id


def finish_background_command(command_id, status='completed', message=None):
    with app.app_context():
        command = db.session.get(BackgroundCommand, command_id)
        if not command:
            return
        command.status = status
        command.message = message[:500] if message else None
        command.finished_at = datetime.now(china_tz)
        command.updated_at = datetime.now(china_tz)
        db.session.commit()


def execute_background_command(command_id):
    with app.app_context():
        command = db.session.get(BackgroundCommand, command_id)
        if not command:
            return False
        payload = {}
        if command.payload:
            try:
                payload = json.loads(command.payload)
            except Exception:
                payload = {}
        command_type = command.command_type

    try:
        if command_type == 'refresh_update_checks':
            refresh_update_checks(force=bool(payload.get('force', True)))
            finish_background_command(command_id, status='completed', message='更新检查已完成')
            return True

        finish_background_command(command_id, status='error', message=f'未知后台命令: {command_type}')
        return False
    except Exception as exc:
        finish_background_command(command_id, status='error', message=str(exc))
        return False


def should_refresh_update_checks(comic_mapping, force=False):
    if force:
        return True

    if not comic_mapping:
        return False

    now = datetime.now(china_tz)
    existing_checks = get_update_check_map()

    for comic_name, source_url in comic_mapping.items():
        check_item = existing_checks.get(comic_name)
        if not check_item:
            return True
        if (check_item.source_url or '') != (source_url or ''):
            return True
        last_checked_at = normalize_china_datetime(check_item.last_checked_at)
        if not last_checked_at:
            return True
        if now - last_checked_at >= UPDATE_CHECK_INTERVAL:
            return True

    extra_names = set(existing_checks) - set(comic_mapping)
    if extra_names:
        return True

    return False


def refresh_update_checks(force=False, async_refresh=False):
    comic_mapping = load_comic_mapping()
    if not should_refresh_update_checks(comic_mapping, force=force):
        return get_update_candidates()

    with update_check_cache_lock:
        if update_check_cache_state['refreshing']:
            return get_update_candidates()
        update_check_cache_state['refreshing'] = True

    try:
        now = datetime.now(china_tz)
        with app.app_context():
            existing_checks = {
                item.comic_name: item
                for item in ComicUpdateCheck.query.all()
            }

            for comic_name, check_item in list(existing_checks.items()):
                if comic_name not in comic_mapping:
                    db.session.delete(check_item)
                    existing_checks.pop(comic_name, None)

            for comic_name, source_url in comic_mapping.items():
                check_item = existing_checks.get(comic_name)
                if not check_item:
                    check_item = ComicUpdateCheck(
                        comic_name=comic_name,
                        source_url=source_url or '',
                    )
                    db.session.add(check_item)
                    existing_checks[comic_name] = check_item

                check_item.source_url = source_url or ''
                check_item.status = 'checking'
                check_item.error_message = None
                check_item.updated_at = now
            db.session.commit()

        for comic_name, source_url in comic_mapping.items():
            pending_chapters = 0
            remote_total = 0
            local_total = 0
            latest_title = None
            status = 'ready'
            error_message = None
            has_updates = False

            try:
                source = load_comic_source(source_url)
                remote_chapters = source.get('chapters', []) or []
                remote_total = len(remote_chapters)
                local_bases = get_local_chapter_bases(comic_name)
                local_match_bases = get_local_chapter_match_bases(comic_name)
                local_total = len(local_bases)
                pending_items = [
                    chapter for chapter in remote_chapters
                    if not is_existing_local_chapter(chapter, local_match_bases)
                ]
                pending_chapters = len(pending_items)
                has_updates = pending_chapters > 0
                latest_title = pending_items[-1]['title'] if pending_items else (
                    remote_chapters[-1]['title'] if remote_chapters else None
                )
            except Exception as exc:
                status = 'error'
                error_message = str(exc)[:500]

            with app.app_context():
                check_item = ComicUpdateCheck.query.filter_by(comic_name=comic_name).first()
                if not check_item:
                    continue
                check_item.has_updates = has_updates
                check_item.pending_chapters = pending_chapters
                check_item.remote_total_chapters = remote_total
                check_item.local_total_chapters = local_total
                check_item.latest_chapter_title = latest_title
                check_item.status = status
                check_item.error_message = error_message
                check_item.last_checked_at = now
                check_item.updated_at = now
                db.session.commit()
        update_candidates = get_update_candidates()
        if get_comic_update_mode() == COMIC_UPDATE_MODE_AUTO:
            enqueue_auto_update_tasks(update_candidates)
        return update_candidates
    finally:
        with update_check_cache_lock:
            update_check_cache_state['refreshing'] = False

initialize_database()

green = "\033[1;32m"
red =  "\033[1;31m"
dark_gray = "\033[1;30m"
light_red = "\033[1;31m"
reset = "\033[0;0m"

def safe_print(message, end="\n", flush=False):
    """安全打印函数，用于日志记录"""
    print(message, end=end, flush=flush)

def create_task(url, comic_format, is_update=False, comic_name=None):
    """创建新任务并返回任务ID"""
    with app.app_context():
        task_id = str(uuid.uuid4())
        resolved_comic_name = (comic_name or '').strip() or "未知漫画"
        if url and resolved_comic_name == "未知漫画":
            try:
                resolved_comic_name = load_comic_source(url)['title']
            except Exception as exc:
                safe_print(f"任务初始化时获取漫画标题失败: {exc}")

        task = DownloadTask(
            id=task_id,
            comic_name=resolved_comic_name,
            url=url,
            comic_format=comic_format,
            start_time=datetime.now(china_tz),
            is_update=is_update
        )
        db.session.add(task)
        db.session.commit()
        
        return task_id

def update_task(task_id, **kwargs):
    """更新任务状态"""
    with app.app_context(): 
        task = db.session.get(DownloadTask, task_id) 
        if task:
            should_refresh_library = False
            for key, value in kwargs.items():
                if key == 'log':
                    task.log = (task.log or '') + value + '\n'
                else:
                    previous_value = getattr(task, key, None)
                    setattr(task, key, value)
                    if key == 'status' and value == 'completed':
                        should_refresh_library = True
                    elif key == 'completed_chapters':
                        previous_completed = int(previous_value or 0)
                        current_completed = int(value or 0)
                        if previous_completed < 1 and current_completed >= 1:
                            should_refresh_library = True
            db.session.commit()
            if should_refresh_library:
                schedule_comics_cache_refresh()
                if task.is_update:
                    schedule_update_checks_refresh(force=True)
        return task

def get_task(task_id):
    """获取任务信息"""
    with app.app_context():  
        return db.session.get(DownloadTask, task_id)  


def is_task_cancel_requested(task_id):
    """检查任务是否已被用户取消。"""
    task = get_task(task_id)
    return bool(task and task.status == 'cancelled')


def get_all_tasks():
    """获取所有任务"""
    with app.app_context():
        return DownloadTask.query.order_by(DownloadTask.created_at.desc()).all()


def delete_task(task_id):
    """仅删除任务记录，保留漫画文件"""
    with app.app_context():
        task = db.session.get(DownloadTask, task_id)
        if task:
            # 删除数据库记录
            db.session.delete(task)
            db.session.commit()
            print(f"成功删除任务记录: {task_id}")
            return True
        return False


def delete_finished_tasks():
    """批量删除已结束的任务记录，保留进行中任务和漫画文件"""
    active_statuses = ('pending', 'running')
    with app.app_context():
        active_count = DownloadTask.query.filter(
            DownloadTask.status.in_(active_statuses)
        ).count()
        deleted_count = DownloadTask.query.filter(
            or_(
                DownloadTask.status.is_(None),
                ~DownloadTask.status.in_(active_statuses)
            )
        ).delete(synchronize_session=False)
        db.session.commit()
        print(f"成功批量删除任务记录: {deleted_count}，保留活动任务: {active_count}")
        return deleted_count, active_count


def get_reading_progress(comic_name, user_id):
    """获取漫画阅读进度"""
    with app.app_context():
        return ReadingProgress.query.filter_by(
            comic_name=comic_name,
            user_id=user_id
        ).order_by(
            ReadingProgress.last_read_at.desc(),
            ReadingProgress.id.desc()
        ).first()

def save_reading_progress(comic_name, chapter, page, scroll_position, total_chapters, total_pages, user_id):
    """保存阅读进度 - 修复��数顺序"""
    with app.app_context():
        now = datetime.now(china_tz)
        progress_records = ReadingProgress.query.filter_by(
            comic_name=comic_name,
            user_id=user_id
        ).order_by(
            ReadingProgress.last_read_at.desc(),
            ReadingProgress.id.desc()
        ).all()

        progress = progress_records[0] if progress_records else None
        if progress:
            progress.last_chapter = chapter
            progress.last_page = page
            progress.scroll_position = scroll_position
            progress.total_chapters = total_chapters
            progress.total_pages = total_pages
            progress.last_read_at = now

            for duplicate_progress in progress_records[1:]:
                db.session.delete(duplicate_progress)
        else:
            progress = ReadingProgress(
                user_id=user_id,
                comic_name=comic_name,
                last_chapter=chapter,
                last_page=page,
                scroll_position=scroll_position,
                total_chapters=total_chapters,
                total_pages=total_pages,
                last_read_at=now
            )
            db.session.add(progress)
        db.session.commit()
        return {
            'chapter_index': chapter,
            'page_index': page,
            'scroll_position': scroll_position,
            'total_chapters': total_chapters,
            'total_pages': total_pages,
            'updated_at': api_datetime(now),
        }

def get_all_reading_progress(user_id):
    """获取所有阅读进度"""
    with app.app_context():
        progress_rows = ReadingProgress.query.filter_by(user_id=user_id).order_by(
            ReadingProgress.last_read_at.desc(),
            ReadingProgress.id.desc()
        ).all()

        latest_progress_by_comic = {}
        duplicate_rows = []
        for row in progress_rows:
            if row.comic_name in latest_progress_by_comic:
                duplicate_rows.append(row)
                continue
            latest_progress_by_comic[row.comic_name] = row

        if duplicate_rows:
            for duplicate_row in duplicate_rows:
                db.session.delete(duplicate_row)
            db.session.commit()

        return list(latest_progress_by_comic.values())


def normalize_group_name(group_name):
    cleaned = re.sub(r'\s+', ' ', (group_name or '').strip())
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', cleaned)
    return cleaned[:50]


def get_group_filter_session_key(user_id=None):
    resolved_user_id = user_id or session.get('user_id') or 'guest'
    return f'comic_group_filter_{resolved_user_id}'


def get_saved_group_filter(user_id=None, default='全部'):
    saved_value = session.get(get_group_filter_session_key(user_id), default)
    return normalize_group_name(saved_value) or default


def save_group_filter(group_name, user_id=None):
    session[get_group_filter_session_key(user_id)] = normalize_group_name(group_name) or '全部'


def get_all_comic_groups():
    with app.app_context():
        groups = ComicGroup.query.order_by(
            db.case((ComicGroup.name == '默认分组', 0), else_=1),
            ComicGroup.created_at.asc(),
            ComicGroup.name.asc()
        ).all()
        return [group.name for group in groups]


def get_user_group_permissions(user_id):
    if not user_id:
        return []

    with app.app_context():
        return [
            permission.group_name
            for permission in UserGroupPermission.query.filter_by(user_id=user_id)
            .order_by(UserGroupPermission.group_name.asc())
            .all()
        ]


def get_accessible_group_names(user=None):
    target_user = user or get_current_user()
    all_groups = get_all_comic_groups()

    if target_user and target_user.is_admin:
        return all_groups

    allowed_groups = {
        normalize_group_name(group_name)
        for group_name in get_user_group_permissions(target_user.id if target_user else None)
    }
    return [group_name for group_name in all_groups if group_name in allowed_groups]


def can_user_access_group(group_name, user=None):
    normalized_group = normalize_group_name(group_name) or '默认分组'
    target_user = user or get_current_user()

    if target_user and target_user.is_admin:
        return True

    return normalized_group in set(get_accessible_group_names(target_user))


def set_user_group_permissions(user_id, group_names):
    if not user_id:
        return []

    normalized_group_names = []
    seen_groups = set()
    valid_groups = set(get_all_comic_groups())

    for group_name in group_names or []:
        normalized_group = normalize_group_name(group_name)
        if not normalized_group or normalized_group in seen_groups or normalized_group not in valid_groups:
            continue
        seen_groups.add(normalized_group)
        normalized_group_names.append(normalized_group)

    with app.app_context():
        UserGroupPermission.query.filter_by(user_id=user_id).delete(synchronize_session=False)
        for group_name in normalized_group_names:
            db.session.add(UserGroupPermission(user_id=user_id, group_name=group_name))
        db.session.commit()

    return normalized_group_names


def ensure_comic_group(group_name):
    normalized_name = normalize_group_name(group_name)
    if not normalized_name:
        return None

    with app.app_context():
        group = ComicGroup.query.filter_by(name=normalized_name).first()
        if not group:
            group = ComicGroup(name=normalized_name)
            db.session.add(group)
            db.session.commit()
        return group.name


def get_comic_group_map():
    with app.app_context():
        memberships = ComicGroupMembership.query.all()
        return {
            membership.comic_name: membership.group_name or '默认分组'
            for membership in memberships
        }


def normalize_hidden_target(target_type, target_value):
    if target_type == 'group':
        return normalize_group_name(target_value) or None
    if target_type == 'comic':
        cleaned = (target_value or '').strip()
        return cleaned[:255] if cleaned else None
    return None


def get_admin_hidden_targets(user=None):
    target_user = user or get_current_user()
    if not target_user or not target_user.is_admin:
        return set(), set()

    items = AdminHiddenLibraryItem.query.filter_by(user_id=target_user.id).all()
    hidden_comics = {
        item.target_value
        for item in items
        if item.target_type == 'comic' and item.target_value
    }
    hidden_groups = {
        item.target_value
        for item in items
        if item.target_type == 'group' and item.target_value
    }
    return hidden_comics, hidden_groups


def is_comic_hidden_for_admin(comic, hidden_comics, hidden_groups):
    comic_name = (comic.get('comic_name') or '').strip()
    group_name = normalize_group_name(comic.get('group')) or '默认分组'
    return comic_name in hidden_comics or group_name in hidden_groups


def set_admin_hidden_item(user_id, target_type, target_value, hidden):
    normalized_value = normalize_hidden_target(target_type, target_value)
    if not user_id or target_type not in {'comic', 'group'} or not normalized_value:
        return False

    existing_item = AdminHiddenLibraryItem.query.filter_by(
        user_id=user_id,
        target_type=target_type,
        target_value=normalized_value
    ).first()

    if hidden:
        if not existing_item:
            db.session.add(AdminHiddenLibraryItem(
                user_id=user_id,
                target_type=target_type,
                target_value=normalized_value
            ))
            db.session.commit()
        return True

    if existing_item:
        db.session.delete(existing_item)
        db.session.commit()
    return True


def assign_comic_group(comic_name, group_name):
    normalized_comic_name = (comic_name or '').strip()
    normalized_group_name = ensure_comic_group(group_name)

    if not normalized_comic_name or not normalized_group_name:
        return False

    with app.app_context():
        membership = ComicGroupMembership.query.filter_by(comic_name=normalized_comic_name).first()
        if membership:
            membership.group_name = normalized_group_name
            membership.updated_at = datetime.now(china_tz)
        else:
            membership = ComicGroupMembership(
                comic_name=normalized_comic_name,
                group_name=normalized_group_name,
                updated_at=datetime.now(china_tz)
            )
            db.session.add(membership)

        DownloadTask.query.filter_by(comic_name=normalized_comic_name).update(
            {'group': normalized_group_name},
            synchronize_session=False
        )
        db.session.commit()

    return True


def delete_comic_group(group_name):
    normalized_group_name = normalize_group_name(group_name)
    if not normalized_group_name or normalized_group_name == '默认分组':
        return False

    with app.app_context():
        group = ComicGroup.query.filter_by(name=normalized_group_name).first()
        if not group:
            return False

        ComicGroupMembership.query.filter_by(group_name=normalized_group_name).update(
            {
                'group_name': '默认分组',
                'updated_at': datetime.now(china_tz)
            },
            synchronize_session=False
        )
        DownloadTask.query.filter_by(group=normalized_group_name).update(
            {'group': '默认分组'},
            synchronize_session=False
        )
        UserGroupPermission.query.filter_by(group_name=normalized_group_name).delete(synchronize_session=False)
        AdminHiddenLibraryItem.query.filter_by(
            target_type='group',
            target_value=normalized_group_name
        ).delete(synchronize_session=False)
        db.session.delete(group)
        db.session.commit()

    return True


def display_minutes_from_seconds(total_seconds):
    safe_seconds = max(int(total_seconds or 0), 0)
    if safe_seconds <= 0:
        return 0
    return math.ceil(safe_seconds / 60)


def reading_time_seconds_expression():
    return db.func.coalesce(ReadingTime.duration_seconds, ReadingTime.duration * 60)


def record_reading_time(comic_name, duration_seconds, user_id, session_key=None):
    """按阅读会话累计秒数记录阅读时间，服务端去重避免重复记时。"""
    with app.app_context():
        normalized_seconds = max(int(duration_seconds or 0), 0)
        if not comic_name or not user_id or normalized_seconds <= 0:
            return None

        delta_seconds = normalized_seconds
        if session_key:
            session_state = ReadingSessionState.query.filter_by(
                user_id=user_id,
                comic_name=comic_name,
                session_key=session_key
            ).first()

            if not session_state:
                session_state = ReadingSessionState(
                    user_id=user_id,
                    comic_name=comic_name,
                    session_key=session_key,
                    last_reported_seconds=0
                )
                db.session.add(session_state)
                db.session.flush()

            delta_seconds = normalized_seconds - max(session_state.last_reported_seconds or 0, 0)
            if delta_seconds <= 0:
                session_state.updated_at = datetime.now(china_tz)
                db.session.commit()
                return None

            session_state.last_reported_seconds = normalized_seconds
            session_state.updated_at = datetime.now(china_tz)

        reading_time = ReadingTime(
            user_id=user_id,
            comic_name=comic_name,
            duration=display_minutes_from_seconds(delta_seconds),
            duration_seconds=delta_seconds,
            read_at=datetime.now(china_tz)
        )
        db.session.add(reading_time)
        db.session.commit()
        return reading_time


def get_total_reading_time(user_id):
    """获取总阅读时间（分钟）"""
    with app.app_context():
        total_seconds = db.session.query(db.func.sum(reading_time_seconds_expression())).filter(
            ReadingTime.user_id == user_id
        ).scalar()
        return display_minutes_from_seconds(total_seconds)


def get_reading_time_by_comic(user_id):
    """按漫画分组获取阅读时间"""
    with app.app_context():
        result = db.session.query(
            ReadingTime.comic_name,
            db.func.sum(reading_time_seconds_expression()).label('total_duration_seconds')
        ).filter(
            ReadingTime.user_id == user_id
        ).group_by(ReadingTime.comic_name).order_by(db.desc('total_duration_seconds')).all()
        return [
            {
                'comic_name': row.comic_name,
                'total_duration': display_minutes_from_seconds(row.total_duration_seconds)
            }
            for row in result
        ]


def get_reading_time_for_comic(comic_name, user_id):
    """获取单个漫画的总阅读时间（分钟）"""
    with app.app_context():
        total_seconds = db.session.query(db.func.sum(reading_time_seconds_expression())).filter(
            ReadingTime.comic_name == comic_name,
            ReadingTime.user_id == user_id
        ).scalar()
        return display_minutes_from_seconds(total_seconds)


def get_reading_time_monthly_for_year(year, user_id):
    """获取指定年份的月度阅读时间"""
    with app.app_context():
        result = db.session.query(
            db.func.strftime('%Y-%m', ReadingTime.read_at).label('month'),
            db.func.sum(reading_time_seconds_expression()).label('total_duration_seconds')
        ).filter(
            ReadingTime.user_id == user_id,
            db.func.strftime('%Y', ReadingTime.read_at) == f"{year:04d}"
        ).group_by('month').order_by('month').all()
        return [
            (row.month, display_minutes_from_seconds(row.total_duration_seconds))
            for row in result
        ]


def get_reading_time_daily_for_year(year, user_id):
    """获取指定年份按天聚合的阅读时间（分钟）"""
    with app.app_context():
        result = db.session.query(
            db.func.strftime('%Y-%m-%d', ReadingTime.read_at).label('day'),
            db.func.sum(reading_time_seconds_expression()).label('total_duration_seconds')
        ).filter(
            ReadingTime.user_id == user_id,
            db.func.strftime('%Y', ReadingTime.read_at) == f"{year:04d}"
        ).group_by('day').order_by('day').all()
        return [
            (row.day, display_minutes_from_seconds(row.total_duration_seconds))
            for row in result
        ]

# 缓存变量
comics_cache = {
    'data': None,
    'timestamp': 0,
    'expiration': 300,  # 5分钟缓存
    'refreshing': False,
}
comics_cache_lock = threading.Lock()


def build_available_comics_snapshot():
    with app.app_context():
        query = DownloadTask.query.filter(
            DownloadTask.status.in_(['completed', 'running', 'error', 'cancelled'])
        )
        all_tasks = query.all()
        comics = {}

        for task in all_tasks:
            if task.comic_name not in comics:
                comic_path = get_comic_directory(task.comic_name)
                has_content = False
                available_chapters = 0
                detected_format = None

                if comic_path and os.path.exists(comic_path):
                    try:
                        scan_data = get_cached_local_comic_scan(task.comic_name, comic_path)
                        available_chapters = scan_data.get('available_chapters', 0)
                        detected_format = scan_data.get('comic_format')
                        if available_chapters > 0:
                            has_content = True
                    except Exception:
                        continue

                if has_content:
                    comics[task.comic_name] = {
                        'id': task.id,
                        'comic_name': task.comic_name,
                        'comic_format': detected_format or task.comic_format,
                        'status': task.status,
                        'total_chapters': task.total_chapters,
                        'completed_chapters': task.completed_chapters,
                        'available_chapters': available_chapters,
                        'created_at': task.created_at,
                        'group': task.group or '默认分组'
                    }

        for comic_name, _comic_path in iter_local_comic_directories():
            if comic_name not in comics:
                try:
                    scan_data = get_cached_local_comic_scan(comic_name, _comic_path)
                    available_chapters = scan_data.get('available_chapters', 0)
                    comic_format = scan_data.get('comic_format')
                    if available_chapters and comic_format:
                        comics[comic_name] = {
                            'id': comic_name,
                            'comic_name': comic_name,
                            'comic_format': comic_format,
                            'status': 'completed',
                            'total_chapters': available_chapters,
                            'completed_chapters': available_chapters,
                            'available_chapters': available_chapters,
                            'created_at': None,
                            'group': '默认分组'
                        }
                except Exception:
                    continue

        sync_comic_identity_records(comics.keys())
        return list(comics.values())


def refresh_comics_cache(force=False, async_refresh=False):
    current_time = time.time()

    with comics_cache_lock:
        cache_is_fresh = (
            comics_cache['data'] is not None and
            not force and
            (current_time - comics_cache['timestamp'] < comics_cache['expiration'])
        )
        if cache_is_fresh:
            return comics_cache['data']

        if async_refresh:
            if comics_cache['refreshing']:
                return comics_cache['data']
            comics_cache['refreshing'] = True

    def _refresh():
        try:
            refreshed_data = build_available_comics_snapshot()
            with comics_cache_lock:
                comics_cache['data'] = refreshed_data
                comics_cache['timestamp'] = time.time()
        finally:
            with comics_cache_lock:
                comics_cache['refreshing'] = False

    if async_refresh:
        refresh_thread = threading.Thread(target=_refresh, daemon=True)
        refresh_thread.start()
        return comics_cache['data']

    _refresh()
    return comics_cache['data']


def get_available_comics():
    """获取可阅读的漫画列表（包括未完成的和已删除任务但文件仍存在的）"""
    current_time = time.time()

    with comics_cache_lock:
        cached_data = comics_cache['data']
        cache_timestamp = comics_cache['timestamp']
        cache_expiration = comics_cache['expiration']

    if cached_data and (current_time - cache_timestamp < cache_expiration):
        return cached_data

    if cached_data:
        refresh_comics_cache(async_refresh=True)
        return cached_data

    return refresh_comics_cache(force=True) or []


def invalidate_comics_cache():
    with comics_cache_lock:
        comics_cache['data'] = None
        comics_cache['timestamp'] = 0
        comics_cache['refreshing'] = False


def schedule_comics_cache_refresh():
    refresh_comics_cache(force=True, async_refresh=True)


def is_update_check_refreshing():
    with app.app_context():
        active_command = BackgroundCommand.query.filter(
            BackgroundCommand.command_type == 'refresh_update_checks',
            BackgroundCommand.status.in_(['pending', 'running'])
        ).first()
        if active_command:
            return True
        checking_item = ComicUpdateCheck.query.filter_by(status='checking').first()
        return bool(checking_item)


def schedule_update_checks_refresh(force=False):
    queue_background_command('refresh_update_checks', {'force': bool(force)}, dedupe_pending=True)


def enrich_comics_with_groups(comics):
    comic_group_map = get_comic_group_map()
    group_names = get_all_comic_groups()
    if '默认分组' not in group_names:
        group_names.insert(0, '默认分组')

    normalized_comics = []
    grouped_lookup = {group_name: [] for group_name in group_names}

    for comic in comics:
        comic_copy = dict(comic)
        comic_group = comic_group_map.get(comic_copy['comic_name']) or comic_copy.get('group') or '默认分组'
        comic_copy['group'] = comic_group
        if comic_group not in group_names:
            group_names.append(comic_group)
            grouped_lookup[comic_group] = []
        grouped_lookup.setdefault(comic_group, []).append(comic_copy)
        normalized_comics.append(comic_copy)

    group_counts = {
        group_name: len(grouped_lookup.get(group_name, []))
        for group_name in group_names
    }

    return normalized_comics, group_names, group_counts, grouped_lookup


def filter_grouped_comics_for_user(comics, user=None):
    target_user = user or get_current_user()
    normalized_comics, group_names, group_counts, grouped_lookup = enrich_comics_with_groups(comics)

    if target_user and target_user.is_admin:
        return normalized_comics, group_names, group_counts, grouped_lookup

    allowed_group_names = set(get_accessible_group_names(target_user))
    filtered_group_names = [group_name for group_name in group_names if group_name in allowed_group_names]
    filtered_lookup = {
        group_name: grouped_lookup.get(group_name, [])
        for group_name in filtered_group_names
    }
    filtered_comics = [
        comic for comic in normalized_comics
        if comic.get('group') in allowed_group_names
    ]
    filtered_counts = {
        group_name: len(filtered_lookup.get(group_name, []))
        for group_name in filtered_group_names
    }

    return filtered_comics, filtered_group_names, filtered_counts, filtered_lookup


def default_headers(referer=None):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    if referer:
        headers['Referer'] = referer
    return headers


def sanitize_filename(name):
    safe_name = re.sub(r'[\\/:*?"<>|]', '', (name or '').strip())
    return safe_name or "untitled"


def ensure_directory(path):
    os.makedirs(path, exist_ok=True)


def build_pdf_from_image_list(image_paths, output_path):
    """Normalize images and build a PDF whose page size matches each image."""
    max_size = 14400
    min_size = 3

    with tempfile.TemporaryDirectory(prefix="pdf-build-", dir=os.path.dirname(output_path)) as temp_dir:
        normalized_paths = []

        for index, image_path in enumerate(natsorted(image_paths), start=1):
            with Image.open(image_path) as img:
                if img.mode not in ('RGB', 'L'):
                    img = img.convert('RGB')
                elif img.mode == 'L':
                    img = img.convert('RGB')

                width, height = img.size
                scale = 1.0

                if width > max_size:
                    scale = min(scale, max_size / width)
                if height > max_size:
                    scale = min(scale, max_size / height)
                if width < min_size:
                    scale = max(scale, min_size / width)
                if height < min_size:
                    scale = max(scale, min_size / height)

                if scale != 1.0:
                    width = max(min(int(round(width * scale)), max_size), min_size)
                    height = max(min(int(round(height * scale)), max_size), min_size)
                    img = img.resize((width, height), Image.Resampling.LANCZOS)

                normalized_path = os.path.join(temp_dir, f"{index:04d}.jpg")
                img.save(normalized_path, "JPEG", quality=95)
                normalized_paths.append(normalized_path)

        if not normalized_paths:
            return False, "没有有效的图片可以生成PDF"

        pdf_bytes = img2pdf.convert(normalized_paths)
        with open(output_path, "wb") as output_file:
            output_file.write(pdf_bytes)

    return True, f"成功生成PDF：{output_path}"


def get_pdf_page_size(pdf_path):
    try:
        result = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=PDF_TOOL_TIMEOUT_SECONDS
        )
        match = re.search(r'Page size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts', result.stdout)
        if match:
            return float(match.group(1)), float(match.group(2))
    except Exception:
        return None, None
    return None, None


def pdf_needs_repair(pdf_path):
    width, height = get_pdf_page_size(pdf_path)
    if width is None or height is None:
        return False
    return width <= 10 or height <= 10


def repair_pdf_for_reading(pdf_path):
    """Repair previously generated malformed PDFs by extracting embedded images."""
    if not pdf_path.lower().endswith('.pdf') or not os.path.exists(pdf_path):
        return pdf_path
    if not pdf_needs_repair(pdf_path):
        return pdf_path

    repaired_dir = os.path.join(os.path.dirname(pdf_path), ".repaired")
    repaired_path = os.path.join(repaired_dir, os.path.basename(pdf_path))
    ensure_directory(repaired_dir)

    if os.path.exists(repaired_path) and not pdf_needs_repair(repaired_path):
        return repaired_path

    try:
        with tempfile.TemporaryDirectory(prefix="pdf-repair-", dir=repaired_dir) as temp_dir:
            prefix = os.path.join(temp_dir, "page")
            subprocess.run(
                ["pdfimages", "-png", pdf_path, prefix],
                check=True,
                capture_output=True,
                text=True,
                timeout=PDF_TOOL_TIMEOUT_SECONDS
            )
            extracted_images = natsorted(glob.glob(os.path.join(temp_dir, "page-*.png")))
            if not extracted_images:
                return pdf_path

            success, _ = build_pdf_from_image_list(extracted_images, repaired_path)
            if success and os.path.exists(repaired_path) and not pdf_needs_repair(repaired_path):
                return repaired_path
    except Exception as exc:
        safe_print(f"修复PDF失败: {exc}")

    return pdf_path


def save_comic_mapping(comic_name, url):
    json_file_path = COMIC_MAPPING_FILE
    try:
        if os.path.exists(json_file_path) and os.path.getsize(json_file_path) > 0:
            with open(json_file_path, "r", encoding="utf-8") as json_file:
                existing_data = json.load(json_file)
        else:
            existing_data = {}
    except Exception:
        existing_data = {}

    existing_data[comic_name] = url
    with open(json_file_path, "w", encoding="utf-8") as json_file:
        json.dump(existing_data, json_file, ensure_ascii=False, indent=4)


def save_cover_image(comic_name, cover_url, referer=None, verify=True):
    if not cover_url:
        return

    ensure_directory(COVER_ROOT)
    cover_path = os.path.join(COVER_ROOT, f"{comic_name}.jpg")

    try:
        response = safe_http_get(
            cover_url,
            headers=default_headers(referer=referer),
            timeout=CONFIG['request_timeout'],
            verify=verify,
            max_bytes=MAX_IMAGE_RESPONSE_BYTES
        )
        response.raise_for_status()
        with open(cover_path, "wb") as cover_file:
            cover_file.write(response.content)
    except Exception as exc:
        safe_print(f"下载封面失败: {exc}")


def chapter_sort_key(filename):
    stem = os.path.splitext(filename)[0]
    prefix_match = re.match(r'^(\d+)[_-](.+)$', stem)
    if prefix_match:
        return (0, int(prefix_match.group(1)), prefix_match.group(2))
    if stem.isdigit():
        return (0, int(stem), "")

    number_match = re.search(r'(\d+)', stem)
    if number_match:
        return (1, int(number_match.group(1)), stem)

    return (2, stem)


def chapter_display_title(filename):
    stem = os.path.splitext(filename)[0]
    prefix_match = re.match(r'^(\d+)[_-](.+)$', stem)
    if prefix_match:
        return prefix_match.group(2)
    if stem.isdigit():
        return f"第{int(stem)}章"
    return stem


def is_valid_local_chapter_file(filename, extensions=('.pdf', '.cbz')):
    stem, ext = os.path.splitext(filename)
    return (
        not filename.startswith('._')
        and ext.lower() in extensions
        and stem != '00'
    )


def list_local_chapters(comic_name):
    comic_path = get_comic_directory(comic_name)
    if not comic_path or not os.path.exists(comic_path):
        return []

    chapter_files = [
        filename for filename in os.listdir(comic_path)
        if is_valid_local_chapter_file(filename)
    ]
    chapter_files.sort(key=chapter_sort_key)

    chapters = []
    for index, filename in enumerate(chapter_files):
        stem = os.path.splitext(filename)[0]
        order_match = re.match(r'^(\d+)', stem)
        chapters.append({
            'number': index + 1,
            'order': int(order_match.group(1)) if order_match else index + 1,
            'title': chapter_display_title(filename),
            'filename': filename,
            'format': os.path.splitext(filename)[1][1:].lower()
        })
    return chapters


def target_extension(comic_format):
    return 'pdf' if comic_format == 1 else 'cbz'


def chapter_output_exists(comic_name, filename_base, comic_format):
    return os.path.exists(
        os.path.join(COMIC_ROOT, comic_name, f"{filename_base}.{target_extension(comic_format)}")
    )


def resolve_baozimh_org_image_host(line):
    try:
        image_line = int(line)
    except (TypeError, ValueError):
        image_line = None
    return BAOZIMH_ORG_IMAGE_HOST if image_line == 2 else BAOZIMH_ORG_DEFAULT_IMAGE_HOST


def build_baozimh_org_image_url(image_path, image_host=BAOZIMH_ORG_IMAGE_HOST):
    if not image_path:
        return None
    if image_path.startswith('http://') or image_path.startswith('https://'):
        return image_path
    return f"{image_host}{image_path}"


def decode_baozimh_org_image_payload(encoded_images):
    prefix = 'J7r'
    inner_marker = 'kD'
    split_marker = 'W4s'
    suffix = 'nQ'
    chunk_size = 7

    if (
        not isinstance(encoded_images, str)
        or not encoded_images.startswith(prefix)
        or not encoded_images.endswith(suffix)
    ):
        raise ValueError("章节图片数据格式无效")

    body = encoded_images[len(prefix):-len(suffix)]
    payload_length = len(body) - len(inner_marker) - len(split_marker)
    if payload_length <= 0:
        raise ValueError("章节图片数据格式无效")

    trailing_length = payload_length // 3
    leading_length = (payload_length - trailing_length) // 2
    middle_length = payload_length - trailing_length - leading_length
    leading = body[:leading_length]
    inner = body[leading_length:leading_length + len(inner_marker)]
    middle_start = leading_length + len(inner_marker)
    middle = body[middle_start:middle_start + middle_length]
    split_start = middle_start + middle_length
    split = body[split_start:split_start + len(split_marker)]
    trailing = body[split_start + len(split_marker):]

    if inner != inner_marker or split != split_marker or len(trailing) != trailing_length:
        raise ValueError("章节图片数据格式无效")

    mixed_payload = trailing + leading + middle
    restored_chunks = []
    for chunk_index, start in enumerate(range(0, len(mixed_payload), chunk_size)):
        chunk = mixed_payload[start:start + chunk_size]
        restored_chunks.append(chunk[::-1] if chunk_index % 2 else chunk)

    translated_payload = ''.join(restored_chunks).translate(BAOZIMH_ORG_ENCODED_IMAGE_TRANSLATION)
    padding = '=' * ((4 - len(translated_payload) % 4) % 4)
    try:
        decoded_payload = base64.urlsafe_b64decode(
            f"{translated_payload}{padding}".encode('ascii')
        ).decode('utf-8')
        images = json.loads(decoded_payload)
    except Exception as exc:
        raise ValueError("章节图片数据解析失败") from exc

    if not isinstance(images, list):
        raise ValueError("章节图片数据格式无效")
    return images


def detect_source_provider(url):
    host = urlparse(url).netloc.lower()
    if 'mxs12.cc' in host or 'wzd1.cc' in host:
        return 'mxs'
    if 'baozimh.org' in host:
        return 'baozimh_org'
    if 'baozimhcn.com' in host or 'baozimh.com' in host:
        return 'baozimhcn'
    raise ValueError("暂不支持该站点")


def is_supported_comic_url(url):
    patterns = [
        r'^https://(?:cn\.baozimhcn\.com|(?:www\.)?baozimh\.com)/comic/[^/?#]+/?$',
        r'^https://(?:www\.)?baozimh\.org/manga/[^/?#]+/?$',
        r'^https://(?:www\.)?(?:mxs12|wzd1)\.cc/(?:book/)?[^/?#]+/?$',
    ]
    return any(re.match(pattern, url or '') for pattern in patterns)


def load_baozimh_org_source(url):
    response = safe_http_get(url, headers=default_headers(), timeout=30, verify=False)
    response.raise_for_status()
    response.encoding = 'utf-8'

    soup = BeautifulSoup(response.text, "html.parser")
    history_node = soup.find(id='MangaHistoryStorage')
    page_title = sanitize_filename(
        history_node.get('data-title') if history_node else ''
    )
    page_cover = history_node.get('data-cover') if history_node else None

    mid_match = re.search(r'data-mid="([0-9]+)"', response.text)
    if not mid_match:
        raise ValueError("未找到 baozimh.org 漫画 ID")

    mid = int(mid_match.group(1))
    if not soup.find(id='mangachapters'):
        return {
            'provider': 'baozimh_org',
            'title': page_title or "未知漫画",
            'cover_url': page_cover,
            'cover_verify': False,
            'chapters': [],
            'mid': mid
        }

    manga_response = safe_http_get(
        f"https://api-get-v3.mgsearcher.com/api/manga/get?mid={mid}&mode=all",
        headers=default_headers(),
        timeout=30,
        verify=False
    )
    manga_response.raise_for_status()
    manga_info = manga_response.json()
    if manga_info.get('code') != 200:
        raise ValueError("获取 baozimh.org 漫画信息失败")

    manga_data = manga_info.get('data', {})
    remote_chapters = manga_data.get('chapters', [])
    remote_chapters.sort(key=lambda item: int(item['attributes'].get('order', 0)))

    chapters = []
    for index, chapter in enumerate(remote_chapters, start=1):
        order = int(chapter['attributes'].get('order', index))
        title = chapter['attributes'].get('title') or f"第{order}话"
        chapters.append({
            'order': order,
            'title': title,
            'filename_base': f"{order:04d}_{sanitize_filename(title)}",
            'chapter_id': chapter['id']
        })

    return {
        'provider': 'baozimh_org',
        'title': sanitize_filename(manga_data.get('title') or "未知漫画"),
        'cover_url': manga_data.get('cover'),
        'cover_verify': False,
        'chapters': chapters,
        'mid': mid
    }


def load_mxs_source(url):
    response = safe_http_get(url, headers=default_headers(), timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    title_tag = soup.find("h1") or soup.find("title")
    title = sanitize_filename(title_tag.get_text(strip=True) if title_tag else "未知漫画")

    path_parts = [part for part in urlparse(url).path.split('/') if part]
    if 'book' in path_parts:
        book_index = path_parts.index('book')
        cover_id = path_parts[book_index + 1] if book_index + 1 < len(path_parts) else path_parts[-1]
    else:
        cover_id = path_parts[-1]

    links = soup.select('ul#detail-list-select li a')
    chapters = []
    for index, link in enumerate(links, start=1):
        href = link.get('href')
        if not href:
            continue
        chapters.append({
            'order': index,
            'title': link.get_text(strip=True) or f"第{index}章",
            'filename_base': f"{index:02d}",
            'chapter_url': urljoin(url, href)
        })

    return {
        'provider': 'mxs',
        'title': title,
        'cover_url': f"https://www.wzd1.cc/static/upload/book/{cover_id}/cover.jpg",
        'cover_verify': True,
        'chapters': chapters
    }


def load_baozimhcn_source(url):
    response = safe_http_get(url, headers=default_headers(), timeout=30)
    response.raise_for_status()
    response.encoding = 'utf-8'
    html_content = response.text

    soup = BeautifulSoup(html_content, "html.parser")
    title_tag = soup.find("h1", class_="comics-detail__title") or soup.find("title")
    title = sanitize_filename(title_tag.get_text(strip=True) if title_tag else "未知漫画")

    cover_match = re.search(
        r'<meta data-n-head="ssr" data-hid="og:image" name="og:image" content="(https?://[^"]+)"',
        html_content
    )

    chapter_max = 0
    chapter_slot = re.search(r'chapter_slot=(\d+)', html_content)
    if chapter_slot:
        chapter_max = int(chapter_slot.group(1))
    if chapter_max == 0:
        chapter_match = re.search(r'共(\d+)话', html_content)
        if chapter_match:
            chapter_max = int(chapter_match.group(1))
    if chapter_max == 0:
        chapter_links = re.findall(r'/comic/chapter/[^/]+/0_(\d+)\.html', html_content)
        if chapter_links:
            chapter_max = max(map(int, chapter_links)) + 1
    if chapter_max <= 0:
        raise ValueError("未获取到有效章节数")

    base_chapter_url = url.rstrip('/').replace("/comic/", "/comic/chapter/") + "/0_{}.html"
    chapters = [
        {
            'order': index + 1,
            'title': f"第{index + 1}章",
            'filename_base': f"{index + 1:02d}",
            'chapter_url': base_chapter_url.format(index)
        }
        for index in range(chapter_max)
    ]

    return {
        'provider': 'baozimhcn',
        'title': title,
        'cover_url': cover_match.group(1) if cover_match else None,
        'cover_verify': True,
        'chapters': chapters
    }


def load_comic_source(url):
    provider = detect_source_provider(url)
    if provider == 'mxs':
        return load_mxs_source(url)
    if provider == 'baozimh_org':
        return load_baozimh_org_source(url)
    return load_baozimhcn_source(url)

def is_mxs_url(url):
    """判断是否为 mxs12.cc 网站的 URL"""
    return 'mxs12.cc' in url or 'wzd1.cc' in url


def title_mxs(url):
    """获取 mxs12.cc 漫画标题和总章节数"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        response = safe_http_get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html_content = response.text

        if not html_content.strip():
            raise Exception("获取到空的网页内容")

        soup = BeautifulSoup(html_content, "html.parser")
        title_tag = soup.find("h1")

        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else "未知漫画"

        # 获取章节列表
        links = soup.select('ul#detail-list-select li a')
        chapter_max = len(links)

        # 获取封面图片 - 处理 /book/ 路径
        path_parts = url.strip('/').split('/')
        # 查找 book 后的 ID，或者直接取最后一段
        if 'book' in path_parts:
            book_index = path_parts.index('book')
            if book_index + 1 < len(path_parts):
                cid = path_parts[book_index + 1]
            else:
                cid = path_parts[-1]
        else:
            cid = path_parts[-1]
        cover_url = f"https://www.wzd1.cc/static/upload/book/{cid}/cover.jpg"
        try:
            response = safe_http_get(cover_url, timeout=10, max_bytes=MAX_IMAGE_RESPONSE_BYTES)
            if response.status_code == 200:
                if not os.path.exists(COVER_ROOT):
                    os.makedirs(COVER_ROOT)
                with open(os.path.join(COVER_ROOT, f"{title}.jpg"), 'wb') as f:
                    f.write(response.content)
                print(f"封面已保存到: {os.path.join(COVER_ROOT, f'{title}.jpg')}")
        except Exception as e:
            print(f"下载封面时出错: {e}")

        safe_print(f"提取到的章节数={chapter_max}")

        return str(title), chapter_max, html_content

    except Exception as e:
        error_msg = f"获取漫画信息失败: {str(e)}"
        safe_print(error_msg)
        return "未知漫画", 0, ""


def title(url):
    """获取漫画标题和总章节数（自动识别网站）"""
    if is_mxs_url(url):
        return title_mxs(url)

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        response = safe_http_get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html_content = response.text

        if not html_content.strip():
            raise Exception("获取到空的网页内容")

        soup = BeautifulSoup(html_content, "html.parser")
        title_tag = soup.find("h1", class_="comics-detail__title")

        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else "未知漫画"

        pattern = r'<meta data-n-head="ssr" data-hid="og:image" name="og:image" content="(https?://[^"]+)"'
        match = re.search(pattern, html_content)
        if match:
            image_url = match.group(1)
            print(f"找到图片URL: {image_url}")
            try:
                response = safe_http_get(image_url, max_bytes=MAX_IMAGE_RESPONSE_BYTES)
                response.raise_for_status()
                # 保存图片到本地
                if not os.path.exists(COVER_ROOT):
                    os.makedirs(COVER_ROOT)
                with open(os.path.join(COVER_ROOT, f"{title}.jpg"), 'wb') as f:
                    f.write(response.content)
                print(f"图片已保存到: {os.path.join(COVER_ROOT, f'{title}.jpg')}")
            except Exception as e:
                print(f"下载或保存图片时出错: {e}")

        chapter_max = 0
        chapter_slot = re.search(r'chapter_slot=(\d+)', html_content)
        if chapter_slot:
            chapter_max = int(chapter_slot.group(1))
        if chapter_max == 0:
            chapter_match = re.search(r'共(\d+)话', html_content)
            if chapter_match:
                chapter_max = int(chapter_match.group(1))
        if chapter_max == 0:
            chapter_links = re.findall(r'/comic/chapter/[^/]+/0_(\d+)\.html', html_content)
            if chapter_links:
                chapter_max = max(map(int, chapter_links)) + 1  # 加1因为章节通常从0开始

        safe_print(f"提取到的章节数={chapter_max}")
        safe_print(f"HTML片段包含chapter_slot? {('chapter_slot' in html_content)}")

        return str(title), chapter_max, html_content

    except Exception as e:
        error_msg = f"获取漫画信息失败: {str(e)}"
        safe_print(error_msg)
        return "未知漫画", 0, ""

def images_to_cbz(folder_path):
    """将图片转换为CBZ格式"""
    try:
        images = []
        for fname in os.listdir(folder_path):
            if fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                images.append(os.path.join(folder_path, fname))
        
        # 排序
        images = natsorted(images)
        if not images:
            return False, f"文件夹 {folder_path} 中没有图片"
        
        cbz_name = os.path.join(os.path.dirname(folder_path), f"{os.path.basename(folder_path)}.cbz")
        with zipfile.ZipFile(cbz_name, 'w') as cbz_file:
            for image_path in images:
                cbz_file.write(
                    image_path,
                    arcname=os.path.basename(image_path),
                    compress_type=zipfile.ZIP_STORED
                )
        return True, f"成功生成CBZ：{cbz_name}"
    except Exception as e:
        return False, f"CBZ生成失败：{str(e)}"
    
def images_to_cbz_watch(folder_path):
    try:
        single_image_path = os.path.join(COVER_ROOT, "bzmh.png")
        if not os.path.exists(single_image_path):
            return False, f"错误：图片文件不存在，请检查路径 -> {single_image_path}"
        cbz_name = os.path.join(folder_path, "00.cbz")
        with zipfile.ZipFile(cbz_name, 'w') as cbz_file:
            cbz_file.write(
                single_image_path,
                arcname=os.path.basename(single_image_path),
                compress_type=zipfile.ZIP_STORED
            )
        return True, f"成功生成 CBZ：{cbz_name}（包含图片：{os.path.basename(single_image_path)}）"
    except Exception as e:
        return False, f"CBZ 生成失败：{str(e)}"

def images_to_pdf(folder_path):
    """将图片转换为PDF格式，页尺寸跟随图片内容。"""
    try:
        images = []
        for fname in os.listdir(folder_path):
            if fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                images.append(os.path.join(folder_path, fname))

        images = natsorted(images)
        if not images:
            return False, f"文件夹 {folder_path} 中没有图片"

        pdf_name = os.path.join(os.path.dirname(folder_path), f"{os.path.basename(folder_path)}.pdf")

        return build_pdf_from_image_list(images, pdf_name)
    except Exception as e:
        return False, f"PDF生成失败：{str(e)}"

def download_image_mxs(session, img_url, save_path, retries=3, cancel_checker=None):
    """下载单张图片（mxs12.cc 专用）"""
    for attempt in range(retries):
        if cancel_checker and cancel_checker():
            return False
        try:
            safe_print(f"正在下载: {os.path.basename(save_path)}")
            with safe_http_get(
                img_url,
                stream=True,
                timeout=30,
                max_bytes=MAX_IMAGE_RESPONSE_BYTES,
                session_obj=session
            ) as response:
                if response.status_code == 200:
                    if cancel_checker and cancel_checker():
                        return False
                    write_limited_response_to_file(response, save_path, MAX_IMAGE_RESPONSE_BYTES)
                    return True
                else:
                    safe_print(f"图片请求失败，状态码: {response.status_code}，第{attempt+1}次重试。")
        except Exception as e:
            safe_print(f"图片请求失败，错误: {str(e)}，第{attempt+1}次重试。")
        if attempt < retries - 1:
            if cancel_checker and cancel_checker():
                return False
            time.sleep(2)
    safe_print(f"图片多次尝试失败: {os.path.basename(save_path)}")
    return False


def download_images_concurrently_mxs(session, img_urls, save_dir, max_workers=2, cancel_checker=None):
    """并发下载图片（mxs12.cc 专用）"""
    os.makedirs(save_dir, exist_ok=True)
    safe_print(f"开始下载 {len(img_urls)} 张图片到: {save_dir}")

    success_count = 0
    failed_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        job_iter = iter(enumerate(img_urls, start=1))

        def submit_next():
            try:
                img_idx, img_url = next(job_iter)
            except StopIteration:
                return False
            img_name = f"{img_idx:02d}.jpg"
            img_path = os.path.join(save_dir, img_name)
            future = executor.submit(
                download_image_mxs,
                session,
                img_url,
                img_path,
                3,
                cancel_checker
            )
            future_map[future] = img_url
            return True

        for _ in range(max_workers):
            if not submit_next():
                break

        while future_map:
            if cancel_checker and cancel_checker():
                executor.shutdown(wait=False, cancel_futures=True)
                return success_count, True

            done, _ = concurrent.futures.wait(
                future_map.keys(),
                timeout=0.5,
                return_when=concurrent.futures.FIRST_COMPLETED
            )
            if not done:
                continue

            for future in done:
                future_map.pop(future, None)
                if future.result():
                    success_count += 1
                else:
                    failed_count += 1
                submit_next()

    safe_print(f"图片下载完成: 成功 {success_count} 张，失败 {failed_count} 张")
    return success_count, False


def crawl_chapter_mxs(chapter_url, folder, chapter, comic_format, task_id):
    """下载单个章节（mxs12.cc 专用）"""
    save_dir = os.path.join(COMIC_ROOT, folder, f"{chapter:02d}")
    os.makedirs(save_dir, exist_ok=True)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0'
    }

    try:
        with requests.Session() as session:
            # 配置会话参数
            session.headers.update(headers)
            session.timeout = 30  # 增加超时时间

            for attempt in range(3):
                if is_task_cancel_requested(task_id):
                    shutil.rmtree(save_dir, ignore_errors=True)
                    return False, "任务已取消"
                try:
                    safe_print(f"正在访问章节 {chapter}: {chapter_url}")
                    response = safe_http_get(chapter_url, timeout=30, session_obj=session)
                    response.raise_for_status()

                    safe_print(f"章节 {chapter} 页面获取成功，状态码: {response.status_code}")
                    safe_print(f"页面大小: {len(response.text)} 字节")

                    soup = BeautifulSoup(response.text, 'html.parser')
                    img_tags = soup.find_all('img', class_='lazy')
                    img_urls = [img['data-original'] for img in img_tags if img.has_attr('data-original')]

                    safe_print(f"章节 {chapter} 找到 {len(img_urls)} 张图片")

                    if img_urls:
                        success_count, cancelled = download_images_concurrently_mxs(
                            session,
                            img_urls,
                            save_dir,
                            cancel_checker=lambda: is_task_cancel_requested(task_id)
                        )
                        if cancelled:
                            shutil.rmtree(save_dir, ignore_errors=True)
                            return False, "任务已取消"
                        safe_print(f"章节 {chapter} 图片下载成功: {success_count} 张")

                        if success_count > 0:
                            if is_task_cancel_requested(task_id):
                                shutil.rmtree(save_dir, ignore_errors=True)
                                return False, "任务已取消"
                            # 章节下载完成后压缩成 CBZ
                            cbz_path = os.path.join(COMIC_ROOT, folder, f"{chapter:02d}.cbz")
                            success, msg = images_to_cbz(save_dir)
                            if success:
                                safe_print(f"已压缩为: {os.path.basename(cbz_path)}")
                            else:
                                safe_print(f"压缩失败: {msg}")

                            # 删除原文件夹
                            shutil.rmtree(save_dir)

                            return True, f"章节 {chapter} 下载完成（成功 {success_count} 张）"
                        else:
                            safe_print(f"章节 {chapter} 图片下载全部失败")
                            shutil.rmtree(save_dir)
                    else:
                        safe_print(f"章节 {chapter} 未找到图片")

                except Exception as e:
                    safe_print(f"章节 {chapter} 第 {attempt + 1} 次尝试失败: {str(e)}")
                    if attempt < 2:
                        if is_task_cancel_requested(task_id):
                            shutil.rmtree(save_dir, ignore_errors=True)
                            return False, "任务已取消"
                        time.sleep(3)  # 增加重试间隔
                    else:
                        safe_print(f"章节 {chapter} 三次尝试都失败，放弃")

        return False, f"章节 {chapter} 下载失败"

    except Exception as e:
        safe_print(f"章节 {chapter} 下载异常: {str(e)}")
        return False, f"章节 {chapter} 下载失败: {str(e)}"


def download_image(session, base_url, save_dir, n, task_id, retries=CONFIG['retry_times']):
    """下载单张图片"""
    img_url = base_url.format(n)
    file_path = os.path.join(save_dir, f"{n}.jpg")

    for attempt in range(retries):
        if is_task_cancel_requested(task_id):
            return False, n, True
        time.sleep(random.uniform(*CONFIG['delay_range']))
        try:
            with safe_http_get(
                img_url,
                stream=True,
                timeout=15,
                max_bytes=MAX_IMAGE_RESPONSE_BYTES,
                session_obj=session
            ) as response:
                if response.status_code == 200:
                    if is_task_cancel_requested(task_id):
                        return False, n, True
                    write_limited_response_to_file(response, file_path, MAX_IMAGE_RESPONSE_BYTES)
                    return True, n, False
                else:
                    if response.status_code == 404:
                        return False, n, True
                    else:
                        safe_print(f"图片{n} 下载失败，第{attempt+1}次重试。")
        except Exception as e:
            safe_print(f"图片{n} 下载失败，第{attempt+1}次重试。")

        if attempt < retries - 1:
            if is_task_cancel_requested(task_id):
                return False, n, True
            time.sleep(2 ** attempt)

    return False, n, False

def crawl_chapter(chapter_url, folder, chapter, comic_format, task_id):
    """下载单��章��"""
    save_dir = os.path.join(COMIC_ROOT, folder, f"{chapter:02d}")
    os.makedirs(save_dir, exist_ok=True)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        with requests.Session() as session:
            for attempt in range(3):
                if is_task_cancel_requested(task_id):
                    shutil.rmtree(save_dir, ignore_errors=True)
                    return False, "任务已取消"
                try:
                    response = safe_http_get(chapter_url, headers=headers, timeout=10, session_obj=session)
                    # 如果成功获取200响应，直接返回成功
                    if response.status_code == 200:
                        break
                    # 非200状态码，记录并继续重试
                    safe_print(f"章节页访问失败，状态码: {response.status_code}，第{attempt+1}次尝试")
                except Exception as e:
                    safe_print(f"章节页访问异常: {str(e)}，第{attempt+1}次尝试")
            else:
                # 当循环完成且未通过break退出时，说明3次尝试都失败
                return False, f"章节页经过3次尝试后仍访问失败"

            match = re.search(r'(https?://[^/]+/scomic/[^/]+/\d+/[^/]+/1\.jpg)', response.text)
            if not match:
                return False, "未找到图片地址"
            
            base_url = match.group(1).replace("1.jpg", "{}.jpg")
            update_task(task_id, log=f"开始下载章节：{chapter}")

            max_workers = CONFIG['max_workers']
            success_count = 0
            n = 1
            stop_flag = False
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                while not stop_flag:
                    # 检查任务是否已取消
                    if is_task_cancel_requested(task_id):
                        executor.shutdown(wait=False)
                        return False, "任务已取消"
                        
                    futures = []
                    for _ in range(max_workers * 2):
                        futures.append(executor.submit(
                            download_image, session, base_url, save_dir, n, task_id
                        ))
                        n += 1
                    
                    for future in as_completed(futures):
                        if is_task_cancel_requested(task_id):
                            executor.shutdown(wait=False)
                            return False, "任务已取消"
                            
                        success, num, stop_download = future.result()
                        if success:
                            success_count += 1
                        else:
                            if stop_download:
                                stop_flag = True
                                executor.shutdown(wait=False)
                                break

                    time.sleep(random.uniform(*CONFIG['delay_range']))

            if is_task_cancel_requested(task_id):
                executor.shutdown(wait=False)
                return False, "任务已取消"

            if comic_format == 1:
                update_task(task_id, log=f"章节 {chapter} 下载完成，开始生成PDF...")
                success, msg = images_to_pdf(save_dir)
            else:
                update_task(task_id, log=f"章节 {chapter} 下载完成，开始生成CBZ...")
                success, msg = images_to_cbz(save_dir)
            
            update_task(task_id, log=msg)
            
            if os.path.isdir(save_dir):
                try:
                    shutil.rmtree(save_dir)
                except Exception as e:
                    update_task(task_id, log=f"删除临时目录时发生错误: {e}")
            
            if success:
                return True, f"章节 {chapter} 处理完成"
            else:
                return False, f"章节 {chapter} 处理失败: {msg}"
                
    except Exception as e:
        error_msg = f"章节处理异常：{str(e)}"
        update_task(task_id, log=error_msg)
        return False, error_msg


def extract_image_extension(image_url, default_ext=".jpg"):
    ext = os.path.splitext(urlparse(image_url).path)[1].lower()
    return ext if ext in {'.jpg', '.jpeg', '.png', '.webp', '.gif'} else default_ext


def download_binary_image(image_url, save_path, referer=None, verify=True, retries=None, cancel_checker=None):
    retries = retries or CONFIG['retry_times']
    headers = default_headers(referer=referer)

    for attempt in range(retries):
        if cancel_checker and cancel_checker():
            return False, "cancelled"
        try:
            response = safe_http_get(
                image_url,
                headers=headers,
                stream=True,
                timeout=30,
                verify=verify,
                max_bytes=MAX_IMAGE_RESPONSE_BYTES
            )
            if response.status_code == 200:
                if cancel_checker and cancel_checker():
                    return False, "cancelled"
                write_limited_response_to_file(response, save_path, MAX_IMAGE_RESPONSE_BYTES)
                return True, save_path
            safe_print(f"图片下载失败，状态码: {response.status_code}，第{attempt + 1}次重试")
        except Exception as exc:
            safe_print(f"图片下载异常: {exc}，第{attempt + 1}次重试")

        if attempt < retries - 1:
            if cancel_checker and cancel_checker():
                return False, "cancelled"
            time.sleep(2)

    return False, image_url


def download_chapter_images(image_jobs, save_dir, referer=None, verify=True, max_workers=None, cancel_checker=None):
    ensure_directory(save_dir)
    success_count = 0
    failed_items = []
    max_workers = max_workers or CONFIG['max_workers']

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        job_iter = iter(image_jobs)

        def submit_next():
            try:
                job = next(job_iter)
            except StopIteration:
                return False
            future = executor.submit(
                download_binary_image,
                job['url'],
                os.path.join(save_dir, job['filename']),
                referer,
                verify,
                None,
                cancel_checker
            )
            future_map[future] = job
            return True

        for _ in range(max_workers):
            if not submit_next():
                break

        while future_map:
            if cancel_checker and cancel_checker():
                executor.shutdown(wait=False, cancel_futures=True)
                return success_count, failed_items, True

            done, _ = concurrent.futures.wait(
                future_map.keys(),
                timeout=0.5,
                return_when=concurrent.futures.FIRST_COMPLETED
            )
            if not done:
                continue

            for future in done:
                job = future_map.pop(future)
                success, result = future.result()
                if success:
                    success_count += 1
                elif result != "cancelled":
                    failed_items.append(job['url'])
                submit_next()

    return success_count, failed_items, False


def finalize_downloaded_chapter(save_dir, comic_format):
    if comic_format == 1:
        return images_to_pdf(save_dir)
    return images_to_cbz(save_dir)


def download_mxs_chapter(chapter, folder, comic_format, task_id):
    save_dir = os.path.join(COMIC_ROOT, folder, chapter['filename_base'])
    ensure_directory(save_dir)

    try:
        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        response = safe_http_get(chapter['chapter_url'], headers=default_headers(), timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        image_urls = [
            urljoin(chapter['chapter_url'], img['data-original'])
            for img in soup.find_all('img', class_='lazy')
            if img.has_attr('data-original')
        ]
        if not image_urls:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 未找到图片"

        image_jobs = [
            {
                'url': image_url,
                'filename': f"{index:03d}{extract_image_extension(image_url)}"
            }
            for index, image_url in enumerate(image_urls, start=1)
        ]
        success_count, failed_items, cancelled = download_chapter_images(
            image_jobs,
            save_dir,
            referer=chapter['chapter_url'],
            verify=True,
            cancel_checker=lambda: is_task_cancel_requested(task_id)
        )
        if cancelled:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        if success_count == 0:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 下载失败"

        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        success, message = finalize_downloaded_chapter(save_dir, comic_format)
        shutil.rmtree(save_dir, ignore_errors=True)
        if not success:
            return False, message

        if failed_items:
            update_task(task_id, log=f"章节 {chapter['title']} 有 {len(failed_items)} 张图片下载失败")
        return True, f"章节 {chapter['title']} 处理完成"
    except Exception as exc:
        shutil.rmtree(save_dir, ignore_errors=True)
        return False, f"章节 {chapter['title']} 下载失败: {exc}"


def load_baozimh_org_chapter_images(mid, chapter_id):
    response = safe_http_get(
        f"https://api-get-v3.mgsearcher.com/api/v2/chapter/getinfo?m={mid}&c={chapter_id}",
        headers=default_headers(referer='https://baozimh.org/'),
        timeout=30,
        verify=False
    )
    response.raise_for_status()
    chapter_info = response.json()
    if chapter_info.get('code') != 200:
        raise ValueError("获取章节图片信息失败")
    chapter_data = chapter_info.get('data', {})
    image_data = chapter_data.get('info', {}).get('images', {})
    raw_images = image_data.get('images', [])
    if isinstance(raw_images, str):
        raw_images = decode_baozimh_org_image_payload(raw_images)
    return raw_images, image_data.get('line')


def download_baozimh_org_chapter(source, chapter, folder, comic_format, task_id):
    save_dir = os.path.join(COMIC_ROOT, folder, chapter['filename_base'])
    ensure_directory(save_dir)

    try:
        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        raw_images, image_line = load_baozimh_org_chapter_images(source['mid'], chapter['chapter_id'])
        image_host = resolve_baozimh_org_image_host(image_line)
        image_jobs = []
        for fallback_order, image in enumerate(raw_images, start=1):
            if not isinstance(image, dict):
                continue
            image_url = build_baozimh_org_image_url(image.get('url'), image_host)
            if not image_url:
                continue
            image_order = int(image.get('order') or fallback_order)
            image_jobs.append({
                'url': image_url,
                'filename': f"{image_order:03d}{extract_image_extension(image_url, '.webp')}"
            })

        if not image_jobs:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 未找到图片"

        success_count, failed_items, cancelled = download_chapter_images(
            image_jobs,
            save_dir,
            referer='https://baozimh.org/',
            verify=False,
            max_workers=5,
            cancel_checker=lambda: is_task_cancel_requested(task_id)
        )
        if cancelled:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        if success_count == 0:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 下载失败"

        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        success, message = finalize_downloaded_chapter(save_dir, comic_format)
        shutil.rmtree(save_dir, ignore_errors=True)
        if not success:
            return False, message

        if failed_items:
            update_task(task_id, log=f"章节 {chapter['title']} 有 {len(failed_items)} 张图片下载失败")
        return True, f"章节 {chapter['title']} 处理完成"
    except Exception as exc:
        shutil.rmtree(save_dir, ignore_errors=True)
        return False, f"章节 {chapter['title']} 下载失败: {exc}"


def download_provider_chapter(source, chapter, folder, comic_format, task_id):
    if source['provider'] == 'mxs':
        return download_mxs_chapter(chapter, folder, comic_format, task_id)
    if source['provider'] == 'baozimh_org':
        return download_baozimh_org_chapter(source, chapter, folder, comic_format, task_id)
    return crawl_chapter(chapter['chapter_url'], folder, chapter['order'], comic_format, task_id)

def download_complete_book_mxs(url, comic_format, task_id):
    """下载整本漫画（mxs12.cc 专用）"""
    try:
        update_task(task_id, status='running')

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        folder, chapter_max, html_content = title(url)
        update_task(task_id, comic_name=folder)
        update_task(task_id, log=f"开始下载漫画: {folder}")
        update_task(task_id, log=f"总章节数: {chapter_max}")

        if chapter_max <= 0:
            update_task(task_id, log="错误：未获取到有效的章节数，无法开始下载")
            update_task(task_id, log=f"请检查URL是否正确: {url}")
            update_task(task_id, status='error')
            return

        update_task(task_id, total_chapters=chapter_max)

        # 解析章节链接
        soup = BeautifulSoup(html_content, 'html.parser')
        links = soup.select('ul#detail-list-select li a')
        base_url = "https://mxs12.cc"
        chapter_urls = [base_url + a['href'] for a in links]

        json_file_path = COMIC_MAPPING_FILE
        try:
            if os.path.exists(json_file_path):
                if os.path.getsize(json_file_path) > 0:
                    with open(json_file_path, "r", encoding="utf-8") as json_file:
                        existing_data = json.load(json_file)
                else:
                    existing_data = {}  # 空文件时初始化空字典
            else:
                existing_data = {}

            existing_data[folder] = url
            with open(json_file_path, "w", encoding="utf-8") as json_file:
                json.dump(existing_data, json_file, ensure_ascii=False, indent=4)
        except json.JSONDecodeError:
            error_msg = f"JSON文件格式错误，已创建新文件: {json_file_path}"
            update_task(task_id, log=error_msg)
            with open(json_file_path, "w", encoding="utf-8") as json_file:
                json.dump({folder: url}, json_file, ensure_ascii=False, indent=4)
        except Exception as e:
            error_msg = f"处理JSON文件失败: {str(e)}"
            update_task(task_id, log=error_msg)

        comic_path = os.path.join(COMIC_ROOT, folder)
        if not os.path.exists(comic_path):
            os.makedirs(comic_path)

        for idx, chapter_url in enumerate(chapter_urls, start=1):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            update_task(task_id, log=f"开始处理第 {idx} 章")
            success, msg = crawl_chapter_mxs(chapter_url, folder, idx, comic_format, task_id)
            update_task(task_id, log=msg)

            if task:
                new_completed = task.completed_chapters + 1
                progress = int((new_completed / chapter_max) * 100)
                update_task(task_id, completed_chapters=new_completed, progress_percent=progress)
            time.sleep(2)  # 减小延迟，避免被封禁

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有章节处理完成")

    except Exception as e:
        error_msg = f"下载过程出错: {str(e)}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))


def download_complete_book(url, comic_format, task_id):
    """下载整本漫画（统一站点任务流）"""
    try:
        update_task(task_id, status='running')

        source = load_comic_source(url)
        folder = source['title']
        chapters = source['chapters']

        update_task(task_id, comic_name=folder)
        update_task(task_id, log=f"开始下载漫画: {folder}")
        update_task(task_id, log=f"站点类型: {source['provider']}")
        update_task(task_id, total_chapters=len(chapters))

        if not chapters:
            raise ValueError("未获取到任何章节")

        ensure_directory(os.path.join(COMIC_ROOT, folder))
        save_comic_mapping(folder, url)
        save_cover_image(
            folder,
            source.get('cover_url'),
            referer=url,
            verify=source.get('cover_verify', True)
        )

        for index, chapter in enumerate(chapters, start=1):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            update_task(task_id, log=f"开始处理章节: {chapter['title']}")
            success, message = download_provider_chapter(source, chapter, folder, comic_format, task_id)
            update_task(task_id, log=message)
            if not success:
                update_task(task_id, status='error', end_time=datetime.now(china_tz))
                return

            progress = int((index / len(chapters)) * 100)
            update_task(task_id, completed_chapters=index, progress_percent=progress)

            if source['provider'] == 'baozimhcn':
                time.sleep(2)

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有章节处理完成")
    except Exception as exc:
        error_msg = f"下载过程出错: {exc}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))

def update_comic(comic_name, comic_format, task_id):
    try:
        update_task(task_id, status='running')
        update_task(task_id, comic_name=comic_name)
        update_task(task_id, log=f"开始更新漫画: {comic_name}")

        comic_path = os.path.join(COMIC_ROOT, comic_name)
        if not os.path.isdir(comic_path):
            raise ValueError(f"漫画目录不存在: {comic_path}")

        json_file_path = COMIC_MAPPING_FILE
        if not os.path.exists(json_file_path) or os.path.getsize(json_file_path) == 0:
            raise ValueError("comic.json文件不存在或为空")

        with open(json_file_path, "r", encoding="utf-8") as json_file:
            comic_mapping = json.load(json_file)

        update_url = comic_mapping.get(comic_name)
        if not update_url:
            raise ValueError(f"漫画 {comic_name} 的URL信息不存在于JSON文件中")

        update_task(task_id, url=update_url)
        source = load_comic_source(update_url)
        save_cover_image(
            comic_name,
            source.get('cover_url'),
            referer=update_url,
            verify=source.get('cover_verify', True)
        )

        existing_outputs = {
            os.path.splitext(filename)[0]
            for filename in os.listdir(comic_path)
            if is_valid_local_chapter_file(filename, (f".{target_extension(comic_format)}",))
        }
        existing_match_bases = get_local_chapter_match_bases(comic_name)
        chapters_to_download = [
            chapter for chapter in source['chapters']
            if not is_existing_local_chapter(chapter, existing_match_bases)
        ]

        update_task(task_id, log=f"远端章节总数: {len(source['chapters'])}")
        update_task(task_id, log=f"目标格式已存在章节数: {len(existing_outputs)}")

        if not chapters_to_download:
            update_task(task_id, log="未找到更新，当前已是最新版本")
            update_task(task_id, status='completed', end_time=datetime.now(china_tz))
            return

        update_task(task_id, total_chapters=len(chapters_to_download))
        update_task(task_id, log=f"找到 {len(chapters_to_download)} 个新章节，开始下载")

        for index, chapter in enumerate(chapters_to_download, start=1):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            update_task(task_id, log=f"开始处理章节: {chapter['title']}")
            success, message = download_provider_chapter(source, chapter, comic_name, comic_format, task_id)
            update_task(task_id, log=message)
            if not success:
                update_task(task_id, status='error', end_time=datetime.now(china_tz))
                return

            progress = int((index / len(chapters_to_download)) * 100)
            update_task(task_id, completed_chapters=index, progress_percent=progress)

            if source['provider'] == 'baozimhcn':
                time.sleep(2)

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有更新章节处理完成")

    except Exception as e:
        error_msg = f"更新过程出错: {str(e)}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))

def start_download_task(url, comic_format):
    """创建下载任务并加入后台队列"""
    task_id = create_task(url, comic_format)
    update_task(task_id, log="任务已加入后台队列，等待 worker 处理")
    return task_id

def start_update_task(comic_name, comic_format, url):
    """创建更新任务并加入后台队列"""
    task_id = create_task(url, comic_format, is_update=True, comic_name=comic_name)
    update_task(task_id, comic_name=comic_name, log="任务已加入后台队列，等待 worker 处理")
    return task_id


def claim_next_pending_task():
    with app.app_context():
        while True:
            task = DownloadTask.query.filter_by(status='pending').order_by(
                DownloadTask.created_at.asc(),
                DownloadTask.id.asc()
            ).first()
            if not task:
                return None

            now = datetime.now(china_tz)
            if task.is_update and task.comic_name:
                active_same_comic_task = DownloadTask.query.filter(
                    DownloadTask.id != task.id,
                    DownloadTask.is_update.is_(True),
                    DownloadTask.comic_name == task.comic_name,
                    DownloadTask.status == 'running'
                ).first()
                if active_same_comic_task:
                    task.status = 'cancelled'
                    task.end_time = now
                    task.log = (
                        (task.log or '')
                        + f'已有同名更新任务正在运行（{active_same_comic_task.id}），已跳过重复任务\n'
                    )
                    db.session.commit()
                    continue

            updated_rows = DownloadTask.query.filter_by(id=task.id, status='pending').update(
                {
                    'status': 'running',
                    'start_time': task.start_time or now,
                    'end_time': None,
                    'log': (task.log or '') + '后台 worker 已开始处理任务\n',
                },
                synchronize_session=False
            )
            db.session.commit()
            if updated_rows:
                return task.id


def execute_download_task(task_id):
    task = get_task(task_id)
    if not task:
        return False
    if task.status == 'cancelled':
        return False

    if task.is_update:
        with app.app_context():
            running_same_comic_tasks = DownloadTask.query.filter(
                DownloadTask.is_update.is_(True),
                DownloadTask.comic_name == task.comic_name,
                DownloadTask.status == 'running'
            ).order_by(
                DownloadTask.start_time.asc(),
                DownloadTask.created_at.asc(),
                DownloadTask.id.asc()
            ).all()
            first_running_task = running_same_comic_tasks[0] if running_same_comic_tasks else None
            if first_running_task and first_running_task.id != task.id:
                duplicate_task = db.session.get(DownloadTask, task.id)
                if duplicate_task:
                    duplicate_task.status = 'cancelled'
                    duplicate_task.end_time = datetime.now(china_tz)
                    duplicate_task.log = (
                        (duplicate_task.log or '')
                        + f'已有同名更新任务正在运行（{first_running_task.id}），已跳过重复任务\n'
                    )
                    db.session.commit()
                return False
        update_comic(task.comic_name, task.comic_format, task.id)
    else:
        download_complete_book(task.url, task.comic_format, task.id)
    return True


def recover_background_queue_state():
    with app.app_context():
        running_tasks = DownloadTask.query.filter_by(status='running').all()
        for task in running_tasks:
            task.status = 'pending'
            task.end_time = None
            task.log = (task.log or '') + '检测到服务重启，任务已重新加入队列\n'

        running_commands = BackgroundCommand.query.filter_by(status='running').all()
        for command in running_commands:
            command.status = 'pending'
            command.started_at = None
            command.finished_at = None
            command.message = '检测到服务重启，命令已重新加入队列'

        checking_items = ComicUpdateCheck.query.filter_by(status='checking').all()
        for item in checking_items:
            item.status = 'pending'

        db.session.commit()


def run_scheduled_update_checks_if_needed():
    comic_mapping = load_comic_mapping()
    if should_refresh_update_checks(comic_mapping, force=False):
        refresh_update_checks(force=False)


def acquire_background_worker_lock(lock_path):
    os.makedirs(app.instance_path, exist_ok=True)
    lock_handle = open(lock_path, 'w')
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        return None

    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    return lock_handle


def run_task_worker(worker_index):
    lock_handle = acquire_background_worker_lock(f"{TASK_WORKER_LOCK_PREFIX}_{worker_index}.lock")
    if not lock_handle:
        print(f"⚠️  下载 worker {worker_index + 1} 已在运行，跳过重复启动")
        return

    print(f"✅ 下载 worker {worker_index + 1} 已启动")

    try:
        while True:
            task_id = claim_next_pending_task()
            if task_id:
                execute_download_task(task_id)
                continue

            time.sleep(WORKER_POLL_INTERVAL_SECONDS)
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_handle.close()


def run_command_worker():
    lock_handle = acquire_background_worker_lock(COMMAND_WORKER_LOCK_PATH)
    if not lock_handle:
        print("⚠️  命令 worker 已在运行，跳过重复启动")
        return

    print("✅ 命令 worker 已启动")
    recover_background_queue_state()

    last_schedule_check = 0.0
    try:
        while True:
            command_id = claim_next_background_command()
            if command_id:
                execute_background_command(command_id)
                continue

            now_ts = time.time()
            if now_ts - last_schedule_check >= WORKER_SCHEDULE_HEARTBEAT_SECONDS:
                last_schedule_check = now_ts
                try:
                    run_scheduled_update_checks_if_needed()
                except Exception as exc:
                    print(f"⚠️  定时检查更新失败：{exc}")

            time.sleep(WORKER_POLL_INTERVAL_SECONDS)
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_handle.close()

def login_required(f):
    """登录认证装饰器：验证用户是否已登录"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            # 保存当前访问地址，登录后重定向回来
            return redirect(url_for("login", next=get_login_redirect_target()))
        user = db.session.get(User, session.get('user_id'))
        if not user:
            session.clear()
            flash('登录信息已失效，请重新登录')
            return redirect(url_for("login", next=get_login_redirect_target()))
        session.permanent = True
        session['user_role'] = user.role
        return f(*args, **kwargs)
    return decorated_function


def get_login_redirect_target():
    if request.query_string:
        return request.full_path[:-1] if request.full_path.endswith('?') else request.full_path
    return request.path or '/'


def is_safe_next_target(target):
    if not target:
        return False

    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False

    return target.startswith('/')


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=get_login_redirect_target()))
        user = db.session.get(User, session.get('user_id'))
        if not user:
            session.clear()
            flash('登录信息已失效，请重新登录')
            return redirect(url_for("login", next=get_login_redirect_target()))
        session.permanent = True
        if not user.is_admin:
            flash('当前账号没有此操作权限')
            if (
                request.path.startswith('/task_')
                or request.path.startswith('/delete_task')
                or request.path.startswith('/delete_tasks')
                or request.path.startswith('/cancel_task')
            ):
                return jsonify({'status': 'error', 'message': '无权限'}), 403
            return redirect(url_for('comics_list'))
        session['user_role'] = user.role
        return f(*args, **kwargs)
    return decorated_function


def get_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    return db.session.get(User, user_id)


def asset_url(filename):
    static_path = os.path.join(app.static_folder, filename)
    version = None
    try:
        version = int(os.path.getmtime(static_path))
    except OSError:
        version = None
    return url_for('static', filename=filename, v=version) if version else url_for('static', filename=filename)


def cover_image_url(comic_name):
    cover_filename = f'cover/{comic_name}.jpg'
    cover_path = os.path.join(app.static_folder, cover_filename)
    if os.path.exists(cover_path):
        return asset_url(cover_filename)
    return asset_url('cover/cover.png')


def save_uploaded_cover_image(comic_name, file_storage):
    if not file_storage or not getattr(file_storage, 'filename', ''):
        raise ValueError('请选择封面图片')

    mime_type = (file_storage.mimetype or '').lower()
    if mime_type and not mime_type.startswith('image/'):
        raise ValueError('仅支持上传图片文件')

    try:
        file_storage.stream.seek(0)
        image = Image.open(file_storage.stream)
        if image.format not in {'JPEG', 'PNG', 'WEBP'}:
            raise ValueError('仅支持 JPG、PNG 或 WEBP 图片')
        image.load()
    except Exception as exc:
        raise ValueError('封面图片无法识别，请更换文件后重试') from exc

    if image.mode not in ('RGB', 'L'):
        image = image.convert('RGB')
    elif image.mode == 'L':
        image = image.convert('RGB')

    max_width = 1200
    if image.width > max_width:
        resize_height = int(image.height * (max_width / image.width))
        resample_filter = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS')
        image = image.resize((max_width, resize_height), resample_filter)

    os.makedirs(COVER_ROOT, exist_ok=True)
    cover_path = os.path.join(COVER_ROOT, f'{comic_name}.jpg')
    image.save(cover_path, 'JPEG', quality=90, optimize=True)
    return cover_path


@app.context_processor
def inject_user_context():
    current_user = get_current_user()
    is_admin_user = bool(current_user and current_user.is_admin)
    return {
        'current_user': current_user,
        'is_admin_user': is_admin_user,
        'can_manage_library': is_admin_user,
        'asset_url': asset_url,
        'cover_image_url': cover_image_url,
    }


def api_error(code, message, status=400):
    return jsonify({
        'error': {
            'code': code,
            'message': message,
        }
    }), status


def serialize_api_user(user):
    return {
        'id': user.id,
        'username': user.username,
        'role': user.role,
        'is_admin': bool(user.is_admin),
    }


def establish_user_session(user):
    session.permanent = True
    session['user_id'] = user.id
    session['username'] = user.username
    session['user_role'] = user.role


def record_login_log(username, ip_address, user_agent, success, message):
    login_log = LoginLog(
        username=username,
        ip_address=ip_address,
        user_agent=user_agent,
        success=success,
        message=message,
    )
    db.session.add(login_log)
    db.session.commit()


def validate_user_credentials(username, password):
    normalized_username = (username or '').strip()
    raw_password = password or ''
    if not normalized_username or not raw_password:
        return None

    user = User.query.filter_by(username=normalized_username).first()
    if not user:
        return None

    return user if user.check_password(raw_password) else None


def login_failure_key(username, ip_address):
    normalized_username = (username or '').strip().lower()
    normalized_ip = (ip_address or 'unknown').strip() or 'unknown'
    return f'{normalized_username}:{normalized_ip}'


def authenticate_api_credentials(username, password, ip_address, user_agent):
    normalized_username = (username or '').strip()
    raw_password = password or ''

    if not normalized_username or not raw_password:
        return None, api_error('INVALID_CREDENTIALS', '用户名或密码错误', 401)

    failure_key = login_failure_key(normalized_username, ip_address)
    if failure_key in login_failures:
        fail_count, lock_time = login_failures[failure_key]
        if fail_count >= LOGIN_MAX_ATTEMPTS:
            if datetime.now(china_tz) - lock_time < LOGIN_LOCKOUT_DURATION:
                remaining_time = LOGIN_LOCKOUT_DURATION - (datetime.now(china_tz) - lock_time)
                remaining_minutes = max(1, remaining_time.seconds // 60)
                record_login_log(
                    normalized_username,
                    ip_address,
                    user_agent,
                    False,
                    f'账户已锁定，剩余锁定时间 {remaining_minutes} 分钟'
                )
                return None, api_error('ACCOUNT_LOCKED', f'登录失败次数过多，请在 {remaining_minutes} 分钟后重试', 423)
            del login_failures[failure_key]

    user = validate_user_credentials(normalized_username, raw_password)
    login_success = user is not None

    if login_success and user:
        login_failures.pop(failure_key, None)
        record_login_log(normalized_username, ip_address, user_agent, True, '登录成功')
        return user, None

    fail_count, _lock_time = login_failures.get(failure_key, (0, datetime.now(china_tz)))
    new_fail_count = fail_count + 1
    login_failures[failure_key] = (new_fail_count, datetime.now(china_tz))
    remaining_attempts = LOGIN_MAX_ATTEMPTS - new_fail_count
    if remaining_attempts > 0:
        failure_message = f'用户名或密码错误，还有 {remaining_attempts} 次尝试机会'
    else:
        failure_message = f'登录失败次数过多，账户已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟'

    record_login_log(normalized_username, ip_address, user_agent, False, failure_message)
    return None, api_error('INVALID_CREDENTIALS', failure_message, 401)


def get_basic_auth_credentials():
    authorization = request.headers.get('Authorization', '')
    if not authorization.lower().startswith('basic '):
        return None, None

    encoded_credentials = authorization.split(' ', 1)[1].strip()
    try:
        decoded_credentials = base64.b64decode(encoded_credentials).decode('utf-8')
    except Exception:
        return None, None

    if ':' not in decoded_credentials:
        return None, None

    username, password = decoded_credentials.split(':', 1)
    return username, password


def is_basic_auth_request():
    return request.headers.get('Authorization', '').lower().startswith('basic ')


def require_csrf_for_session_auth(api_response=True):
    if is_basic_auth_request():
        return None
    try:
        csrf.protect()
        return None
    except CSRFError:
        if api_response:
            return api_error('CSRF_REQUIRED', 'CSRF token 无效或缺失', 400)
        return jsonify({'status': 'error', 'message': 'CSRF token 无效或缺失'}), 400


def get_api_authenticated_user():
    if 'user_id' in session:
        user = db.session.get(User, session.get('user_id'))
        if user:
            session.permanent = True
            session['user_role'] = user.role
            return user, None
        session.clear()

    username, password = get_basic_auth_credentials()
    if username or password:
        user, error_response = authenticate_api_credentials(
            username,
            password,
            request.remote_addr or '',
            request.user_agent.string
        )
        if user:
            return user, None
        return None, error_response

    return None, None


def get_api_request_user():
    return getattr(g, 'api_user', None) or get_current_user()


def api_login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user, error_response = get_api_authenticated_user()
        if error_response:
            return error_response
        if not user:
            return api_error('AUTH_REQUIRED', '登录信息已失效，请重新登录', 401)
        g.api_user = user
        return f(*args, **kwargs)
    return decorated_function


def api_datetime(value):
    normalized_value = normalize_china_datetime(value)
    return normalized_value.isoformat() if normalized_value else None


def api_comic_format(value):
    return 'pdf' if value == 1 else 'cbz' if value == 2 else None


def api_display_reading_minutes(minutes):
    safe_minutes = max(int(minutes or 0), 0)
    hours, remaining_minutes = divmod(safe_minutes, 60)

    if hours and remaining_minutes:
        return f'{hours}小时 {remaining_minutes}分钟'
    if hours:
        return f'{hours}小时'
    if remaining_minutes:
        return f'{remaining_minutes}分钟'
    return '0分钟'


def serialize_api_progress(progress):
    if not progress:
        return None
    if isinstance(progress, dict):
        return {
            'chapter_index': progress.get('chapter_index', 0),
            'page_index': progress.get('page_index', 0),
            'scroll_position': progress.get('scroll_position', 0),
            'total_chapters': progress.get('total_chapters', 0),
            'total_pages': progress.get('total_pages', 0),
            'updated_at': progress.get('updated_at'),
        }
    return {
        'chapter_index': progress.last_chapter,
        'page_index': progress.last_page,
        'scroll_position': progress.scroll_position or 0,
        'total_chapters': progress.total_chapters or 0,
        'total_pages': progress.total_pages or 0,
        'updated_at': api_datetime(progress.last_read_at),
    }


def build_local_comic_entry(comic_name):
    comic_path = get_comic_directory(comic_name)
    if not comic_path or not os.path.exists(comic_path):
        return None

    scan_data = get_cached_local_comic_scan(comic_name, comic_path)
    available_chapters = scan_data.get('available_chapters', 0)
    comic_format = scan_data.get('comic_format')
    if not available_chapters or not comic_format:
        return None

    return {
        'id': comic_name,
        'comic_name': comic_name,
        'comic_format': comic_format,
        'status': 'completed',
        'total_chapters': available_chapters,
        'completed_chapters': available_chapters,
        'available_chapters': available_chapters,
        'created_at': None,
        'group': get_comic_group_map().get(comic_name) or '默认分组',
    }


def serialize_api_comic_summary(comic, progress=None, source_mapping=None):
    identity = ensure_comic_identity(comic.get('comic_name'))
    if not identity:
        return None

    comic_name = comic.get('comic_name')
    return {
        'id': identity.comic_id,
        'title': comic_name,
        'cover_url': url_for('api_comic_cover', comic_id=identity.comic_id),
        'group_name': normalize_group_name(comic.get('group')) or '默认分组',
        'format': api_comic_format(comic.get('comic_format')),
        'chapter_count': comic.get('available_chapters') or comic.get('total_chapters') or 0,
        'source_url': (source_mapping or {}).get(comic_name),
        'created_at': api_datetime(comic.get('created_at')),
        'updated_at': api_datetime(progress.last_read_at if progress else comic.get('created_at')),
        'progress': serialize_api_progress(progress),
    }


def serialize_api_chapter(comic_id, chapter):
    chapter_id = build_chapter_id(chapter.get('filename'))
    return {
        'id': chapter_id,
        'index': max((chapter.get('number') or 1) - 1, 0),
        'order': chapter.get('order') or chapter.get('number') or 0,
        'title': chapter.get('title') or '',
        'format': chapter.get('format'),
        'page_count': None,
        'pages_url': url_for('api_comic_chapter_pages', comic_id=comic_id, chapter_id=chapter_id),
        'resource_url': url_for('api_comic_chapter_file', comic_id=comic_id, chapter_id=chapter_id),
    }


def find_chapter_by_id(comic_name, chapter_id):
    for chapter in list_local_chapters(comic_name):
        if build_chapter_id(chapter.get('filename')) == chapter_id:
            return chapter
    return None


def sort_comics_for_api(comics, progress_lookup):
    def sort_key(comic):
        progress = progress_lookup.get(comic['comic_name'])
        if progress and progress.last_read_at:
            return (0, -progress.last_read_at.timestamp())
        created_at = comic.get('created_at')
        if created_at:
            return (1, -created_at.timestamp())
        return (2, comic.get('comic_name', ''))

    return sorted(comics, key=sort_key)


def get_api_comic_entry(comic_id):
    identity = get_comic_identity_by_id(comic_id)
    if not identity:
        return None, None

    comic_lookup = {
        comic['comic_name']: comic
        for comic in get_available_comics()
    }
    comic_entry = comic_lookup.get(identity.comic_name)
    if not comic_entry:
        comic_entry = build_local_comic_entry(identity.comic_name)

    return identity, comic_entry


def build_api_statistics_payload(year, user_id):
    total_time = get_total_reading_time(user_id)
    reading_time_rank = get_reading_time_by_comic(user_id)
    reading_time_monthly = get_reading_time_monthly_for_year(year, user_id)
    reading_time_daily = get_reading_time_daily_for_year(year, user_id)
    comic_lookup = {
        comic['comic_name']: comic
        for comic in get_available_comics()
    }
    source_mapping = load_comic_mapping()
    ranking_items = []

    for index, entry in enumerate(reading_time_rank, start=1):
        comic_name = entry.get('comic_name')
        if not comic_name:
            continue

        comic_entry = comic_lookup.get(comic_name) or build_local_comic_entry(comic_name)
        if not comic_entry:
            continue
        summary = serialize_api_comic_summary(comic_entry, progress=None, source_mapping=source_mapping)
        if not summary:
            continue

        total_duration = int(entry.get('total_duration') or 0)
        ranking_items.append({
            'rank': index,
            'comic_id': summary['id'],
            'title': summary['title'],
            'cover_url': summary['cover_url'],
            'total_time_minutes': total_duration,
            'total_time_display': api_display_reading_minutes(total_duration),
        })

    monthly_data = {
        'labels': ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'],
        'durations': [0] * 12
    }

    for entry in reading_time_monthly:
        month = entry[0]
        duration = int(entry[1] or 0)
        month_num = int(month.split('-')[1]) - 1
        if 0 <= month_num < 12:
            monthly_data['durations'][month_num] = duration

    max_duration = max(monthly_data['durations']) if monthly_data['durations'] else 0

    daily_duration_map = {
        entry[0]: int(entry[1] or 0)
        for entry in reading_time_daily
    }
    max_daily_duration = max(daily_duration_map.values()) if daily_duration_map else 0

    year_start = datetime(year, 1, 1).date()
    year_end = datetime(year, 12, 31).date()
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
                'is_current_year': current_day.year == year,
                'is_today': current_day == today
            })

        month_labels.append(week_month_label)
        heatmap_weeks.append(week_cells)
        cursor += timedelta(days=7)
        week_index += 1

    return {
        'year': year,
        'total_time_minutes': total_time,
        'total_time_display': api_display_reading_minutes(total_time),
        'monthly': {
            'labels': monthly_data['labels'],
            'durations': monthly_data['durations'],
            'max_duration': max_duration,
        },
        'heatmap': {
            'month_labels': month_labels,
            'weeks': heatmap_weeks,
            'max_daily_duration': max_daily_duration,
        },
        'rankings': ranking_items,
    }


@app.route('/api/auth/login', methods=['POST'])
@csrf.exempt
def api_auth_login():
    data = request.get_json(silent=True) or {}
    username = data.get('username')
    password = data.get('password')
    ip_address = request.remote_addr or ''
    user_agent = request.user_agent.string

    user, error_response = authenticate_api_credentials(username, password, ip_address, user_agent)
    if error_response:
        return error_response

    establish_user_session(user)
    return jsonify({
        'user': serialize_api_user(user),
    })


@app.route('/api/auth/logout', methods=['POST'])
@csrf.exempt
def api_auth_logout():
    csrf_error = require_csrf_for_session_auth(api_response=True)
    if csrf_error:
        return csrf_error
    session.clear()
    return jsonify({'success': True})


@app.route('/api/info')
def api_info():
    return jsonify({
        'name': 'MangaDock',
        'version': '1',
        'supports_basic_auth': True,
        'supports_page_images': True,
        'page_formats': ['cbz', 'pdf'],
    })


@app.route('/api/auth/me')
@api_login_required
def api_auth_me():
    current_user = get_api_request_user()
    return jsonify({
        'user': serialize_api_user(current_user),
    })


@app.route('/api/comics')
@api_login_required
def api_comics():
    current_user = get_api_request_user()
    current_user_id = current_user.id
    query = (request.args.get('query') or '').strip().lower()
    group_name = normalize_group_name(request.args.get('group'))
    page = max(1, int(request.args.get('page', 1) or 1))
    page_size = min(100, max(1, int(request.args.get('page_size', 50) or 50)))

    comics = get_available_comics()
    comics, _, _, _ = filter_grouped_comics_for_user(comics, current_user)
    progress_lookup = {
        progress.comic_name: progress
        for progress in get_all_reading_progress(current_user_id)
    }
    comics = sort_comics_for_api(comics, progress_lookup)

    if group_name and group_name != '全部':
        comics = [
            comic for comic in comics
            if (normalize_group_name(comic.get('group')) or '默认分组') == group_name
        ]

    if query:
        comics = [
            comic for comic in comics
            if query in (comic.get('comic_name') or '').lower()
        ]

    total = len(comics)
    start = (page - 1) * page_size
    end = start + page_size
    source_mapping = load_comic_mapping()

    items = []
    for comic in comics[start:end]:
        serialized_item = serialize_api_comic_summary(
            comic,
            progress=progress_lookup.get(comic.get('comic_name')),
            source_mapping=source_mapping
        )
        if serialized_item:
            items.append(serialized_item)

    return jsonify({
        'items': items,
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total,
            'has_more': end < total,
        }
    })


@app.route('/api/comics/<comic_id>')
@api_login_required
def api_comic_detail(comic_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    progress = get_reading_progress(identity.comic_name, current_user.id)
    chapters = [
        serialize_api_chapter(identity.comic_id, chapter)
        for chapter in list_local_chapters(identity.comic_name)
    ]
    item = serialize_api_comic_summary(comic_entry, progress=progress, source_mapping=load_comic_mapping())
    if not item:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)
    item['chapters'] = chapters
    item['progress'] = serialize_api_progress(progress)

    return jsonify({'item': item})


@app.route('/api/comics/<comic_id>/cover')
@api_login_required
def api_comic_cover(comic_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    cover_filename = f'{identity.comic_name}.jpg'
    cover_path = os.path.join(COVER_ROOT, cover_filename)
    if os.path.exists(cover_path):
        return send_from_directory(COVER_ROOT, cover_filename)
    return send_from_directory(os.path.join(app.static_folder, 'cover'), 'cover.png')


@app.route('/api/comics/<comic_id>/chapters')
@api_login_required
def api_comic_chapters(comic_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    chapters = [
        serialize_api_chapter(identity.comic_id, chapter)
        for chapter in list_local_chapters(identity.comic_name)
    ]
    return jsonify({
        'comic_id': identity.comic_id,
        'comic_name': identity.comic_name,
        'chapters': chapters,
        'total_chapters': len(chapters),
    })


@app.route('/api/comics/<comic_id>/chapters/<chapter_id>/file')
@api_login_required
def api_comic_chapter_file(comic_id, chapter_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    chapter = find_chapter_by_id(identity.comic_name, chapter_id)
    if not chapter:
        return api_error('CHAPTER_NOT_FOUND', '章节不存在', 404)

    comic_dir = get_comic_directory(identity.comic_name)
    if not comic_dir:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    filename = chapter.get('filename')
    file_path = resolve_file_under_directory(comic_dir, filename)
    if not file_path or not os.path.exists(file_path):
        return api_error('CHAPTER_NOT_FOUND', '章节文件不存在', 404)

    if file_path.lower().endswith('.pdf'):
        readable_pdf = repair_pdf_for_reading(file_path)
        return send_from_directory(os.path.dirname(readable_pdf), os.path.basename(readable_pdf))
    return send_from_directory(comic_dir, filename)


@app.route('/api/comics/<comic_id>/chapters/<chapter_id>/pages')
@api_login_required
def api_comic_chapter_pages(comic_id, chapter_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    chapter = find_chapter_by_id(identity.comic_name, chapter_id)
    if not chapter:
        return api_error('CHAPTER_NOT_FOUND', '章节不存在', 404)

    file_path = get_chapter_file_path(identity.comic_name, chapter)
    if not file_path:
        return api_error('CHAPTER_NOT_FOUND', '章节文件不存在', 404)

    try:
        pages, total_pages = build_api_page_list(identity.comic_id, chapter_id, file_path)
    except ValueError as exc:
        return api_error('PAGE_LIST_FAILED', str(exc), 500)

    return jsonify({
        'comic_id': identity.comic_id,
        'chapter_id': chapter_id,
        'pages': pages,
        'total_pages': total_pages,
    })


@app.route('/api/comics/<comic_id>/chapters/<chapter_id>/pages/<int:page_index>')
@api_login_required
def api_comic_chapter_page_image(comic_id, chapter_id, page_index):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    chapter = find_chapter_by_id(identity.comic_name, chapter_id)
    if not chapter:
        return api_error('CHAPTER_NOT_FOUND', '章节不存在', 404)

    file_path = get_chapter_file_path(identity.comic_name, chapter)
    if not file_path:
        return api_error('CHAPTER_NOT_FOUND', '章节文件不存在', 404)

    try:
        image_path = resolve_chapter_page_image(file_path, identity.comic_id, chapter_id, page_index)
    except RuntimeError as exc:
        return api_error('PAGE_RENDER_FAILED', str(exc), 500)
    except subprocess.CalledProcessError as exc:
        return api_error('PAGE_RENDER_FAILED', exc.stderr or 'PDF 页面渲染失败', 500)
    except zipfile.BadZipFile:
        return api_error('PAGE_RENDER_FAILED', 'CBZ 文件损坏或格式不受支持', 500)
    except Exception as exc:
        safe_print(f"页面渲染失败: {exc}")
        return api_error('PAGE_RENDER_FAILED', '页面渲染失败', 500)

    if not image_path or not os.path.exists(image_path):
        return api_error('PAGE_NOT_FOUND', '页面不存在', 404)

    return send_from_directory(os.path.dirname(image_path), os.path.basename(image_path))


@app.route('/api/comics/<comic_id>/progress')
@api_login_required
def api_get_comic_progress(comic_id):
    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    progress = get_reading_progress(identity.comic_name, current_user.id)
    return jsonify({
        'progress': serialize_api_progress(progress) or {
            'chapter_index': 0,
            'page_index': 0,
            'scroll_position': 0,
            'total_chapters': 0,
            'total_pages': 0,
            'updated_at': None,
        }
    })


@app.route('/api/comics/<comic_id>/progress', methods=['POST'])
@api_login_required
@csrf.exempt
def api_save_comic_progress(comic_id):
    csrf_error = require_csrf_for_session_auth(api_response=True)
    if csrf_error:
        return csrf_error

    identity, comic_entry = get_api_comic_entry(comic_id)
    if not identity or not comic_entry:
        return api_error('COMIC_NOT_FOUND', '漫画不存在', 404)

    current_user = get_api_request_user()
    current_group = get_comic_group_map().get(identity.comic_name) or comic_entry.get('group') or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return api_error('FORBIDDEN', '当前账号未被授权访问该分组漫画', 403)

    data = request.get_json(silent=True) or {}
    chapter_index = int(data.get('chapter_index', 0) or 0)
    page_index = int(data.get('page_index', 0) or 0)
    scroll_position = int(data.get('scroll_position', 0) or 0)
    total_chapters = int(data.get('total_chapters', 0) or 0)
    total_pages = int(data.get('total_pages', 0) or 0)
    reading_time_seconds = int(data.get('reading_time_seconds', 0) or 0)
    reading_session_id = (data.get('reading_session_id') or '').strip()

    progress = save_reading_progress(
        identity.comic_name,
        chapter_index,
        page_index,
        scroll_position,
        total_chapters,
        total_pages,
        current_user.id
    )
    if reading_time_seconds > 0:
        record_reading_time(identity.comic_name, reading_time_seconds, current_user.id, reading_session_id or None)

    return jsonify({
        'success': True,
        'progress': serialize_api_progress(progress),
    })


@app.route('/api/statistics')
@api_login_required
def api_statistics():
    try:
        requested_year = int(request.args.get('year') or datetime.now(china_tz).year)
    except (TypeError, ValueError):
        return api_error('INVALID_YEAR', '统计年份无效', 400)

    if requested_year < 2000 or requested_year > 2100:
        return api_error('INVALID_YEAR', '统计年份无效', 400)

    current_user = get_api_request_user()
    payload = build_api_statistics_payload(requested_year, current_user.id)
    return jsonify(payload)

# ---------------------- 新增/修改 路由 ----------------------
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

@app.route('/')
@login_required
def index():
    # 登录成功后直接重定向到漫画库
    return redirect(url_for('comics_list'))

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
    requested_group = request.args.get('group')
    if requested_group is None:
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

    return render_template(
        'comic_detail.html',
        task=task,
        chapters=chapters,
        progress=progress,
        reading_time=reading_time,
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

if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == '--run-task-worker':
        worker_index = int(sys.argv[2]) if len(sys.argv) >= 3 else 0
        run_task_worker(worker_index)
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == '--run-command-worker':
        run_command_worker()
        sys.exit(0)

    background_processes = []
    background_cleanup_state = {'done': False}

    def preload_app():
        try:
            print("✅ WSGI 服务启动，开始预热核心组件...")
            with app.app_context():
                db.session.execute(text('SELECT 1'))
                db.session.commit()
            refresh_comics_cache(force=True)
            print("✅ 核心组件预热完成")
        except Exception as e:
            print(f"⚠️  预热失败：{str(e)}")

    def start_background_process(process_name, *args):
        process = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), *args],
            close_fds=True,
            start_new_session=True
        )
        return process

    def stop_background_processes():
        if background_cleanup_state['done']:
            return
        background_cleanup_state['done'] = True

        for process in background_processes:
            if process.poll() is not None:
                continue
            try:
                process.terminate()
            except OSError:
                continue

        for process in background_processes:
            if process.poll() is not None:
                continue
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
            except OSError:
                continue

    atexit.register(stop_background_processes)

    class StandaloneGunicornApplication:
        def __init__(self, flask_app, options=None):
            from gunicorn.app.base import BaseApplication

            class _Application(BaseApplication):
                def __init__(self, application, app_options):
                    self.application = application
                    self.app_options = app_options or {}
                    super().__init__()

                def load_config(self):
                    valid_options = {
                        key: value for key, value in self.app_options.items()
                        if key in self.cfg.settings and value is not None
                    }
                    for key, value in valid_options.items():
                        self.cfg.set(key.lower(), value)

                def load(self):
                    return self.application

            self.server = _Application(flask_app, options)

        def run(self):
            self.server.run()

    worker_count = max(2, min(4, os.cpu_count() or 2))
    gunicorn_options = {
        'bind': '0.0.0.0:5001',
        'workers': worker_count,
        'threads': 4,
        'worker_class': 'gthread',
        'timeout': 120,
        'graceful_timeout': 30,
        'keepalive': 5,
        'accesslog': '-',
        'errorlog': '-',
        'capture_output': True,
        'preload_app': False,
    }

    for worker_index in range(TASK_WORKER_COUNT):
        background_processes.append(
            start_background_process(
                f'mangadock-task-worker-{worker_index + 1}',
                '--run-task-worker',
                str(worker_index)
            )
        )
    background_processes.append(
        start_background_process('mangadock-command-worker', '--run-command-worker')
    )
    preload_app()
    try:
        StandaloneGunicornApplication(app, gunicorn_options).run()
    finally:
        stop_background_processes()
