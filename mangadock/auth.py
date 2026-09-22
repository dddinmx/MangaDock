# -*- coding: utf-8 -*-
"""Authentication, session helpers, and API serializers."""
import base64
import hashlib
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote
from datetime import datetime, timedelta
from functools import wraps

from flask import flash, g, jsonify, redirect, request, session, url_for
from flask_wtf.csrf import CSRFError
from PIL import Image
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mangadock.core import app, is_safe_next_target
from mangadock.extensions import csrf, db
from mangadock.models import LoginFailure, LoginLog, User
from mangadock.settings import COVER_ROOT, LOGIN_LOCKOUT_DURATION, LOGIN_MAX_ATTEMPTS, china_tz


@dataclass(frozen=True)
class ApiRequestUser:
    """Immutable request-scoped identity used by JSON API decorators.

    Library helpers create short-lived application contexts.  Flask-SQLAlchemy
    removes their session on teardown, which can detach a ``User`` model saved
    on ``flask.g``.  Keep only the fields API authorization needs so a Basic
    Auth request remains valid across those helpers.
    """
    id: int
    username: str
    role: str

    @property
    def is_admin(self):
        return self.role == 'admin'


def make_api_request_user(user):
    return ApiRequestUser(id=user.id, username=user.username, role=user.role)

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


def library_write_required(f):
    """下载/更新/任务类页面权限：管理员，或被授予 can_download 的普通用户。"""
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
        session['user_role'] = user.role
        if not (user.is_admin or user.can_download):
            flash('该操作需要下载权限，请联系管理员在用户管理中开通')
            if (
                request.path.startswith('/task_')
                or request.path.startswith('/cancel_task')
                or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            ):
                return jsonify({'status': 'error', 'message': '该操作需要下载权限'}), 403
            return redirect(url_for('comics_list'))
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


def _static_cover_url(filename):
    """拼封面静态地址。filename 须为『已 URL 编码』的相对路径，直接拼 static 前缀，
    避免再经 url_for 对 % 二次编码；文件存在时附带 mtime 版本号以便浏览器破缓存。"""
    static_path = os.path.join(app.static_folder, filename)
    version = None
    try:
        version = int(os.path.getmtime(static_path))
    except OSError:
        version = None
    url = f'{app.static_url_path}/{filename}'
    return f'{url}?v={version}' if version else url


def cover_image_url(comic_name, variant='default'):
    """
    variant:
      - default: original downloaded cover
      - hero: upscaled cache for full-bleed banners (generated on demand)
    """
    if not comic_name:
        return asset_url('cover/cover.png')

    # 2026-09-22 code review P1：漫画名含空格/#/&/% 时直接拼进 URL 会让 src 截断 404。
    # 文件名部分统一用 quote 编码；注意只编码一次（下方直接拼前缀，不走 url_for，
    # 否则 % 会被二次编码）。与 API 侧 serialize_api_comic_summary 的 cover_url 保持一致。
    encoded_name = quote(comic_name, safe='')

    if variant == 'hero':
        # 只认「已经生成好」的缓存：命中就直接发超分图，没命中就丢给后台线程补，
        # 本次仍返回原图。绝不在请求线程里跑超分——Lanczos 要几百毫秒，接了
        # AI 引擎更是几秒起，会把首页首字节拖垮。
        from mangadock.utils.cover_enhance import hero_cover_ready, request_hero_cover
        if hero_cover_ready(comic_name):
            return _static_cover_url(f'cover/hero/{encoded_name}.jpg')
        try:
            request_hero_cover(comic_name)
        except Exception:
            pass
        # fall through to source cover

    # 2026-09-20 二次修复：不再在服务端回落占位图。SMB stat 瞬时失败曾让这里对
    # 确实存在的封面渲染 cover.png（绿猫直接进 HTML，刷新才恢复）。现在恒发真实
    # 地址：瞬时抖动由 core.py 的 /static/cover/ 重试路由兜底，文件真缺失由前端
    # 全局 onerror 重试后再回落占位图——最终表现不变，但不再误判。
    return _static_cover_url(f'cover/{encoded_name}.jpg')


def cover_hero_image_url(comic_name):
    return cover_image_url(comic_name, variant='hero')


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

    # Keep more source detail for hero upscale (was 1200)
    max_width = 1800
    if image.width > max_width:
        resize_height = int(image.height * (max_width / image.width))
        resample_filter = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS')
        image = image.resize((max_width, resize_height), resample_filter)

    os.makedirs(COVER_ROOT, exist_ok=True)
    cover_path = os.path.join(COVER_ROOT, f'{comic_name}.jpg')
    image.save(cover_path, 'JPEG', quality=92, optimize=True)

    # 超分缓存后台补：AI 引擎要 7~11s，放在 POST 请求里会把上传响应卡死
    try:
        from mangadock.utils.cover_enhance import request_hero_cover
        request_hero_cover(comic_name)
    except Exception:
        pass
    return cover_path


# comic_name -> bool：18+ 源判定按名字缓存（源 URL 不随会话变化）
_ADULT_COMIC_CACHE = {}


# 2026-09-22 code review P2：Basic Auth 每请求重算 pbkdf2 代价高。进程内缓存
# 「用户名+密码」的 SHA256 -> (校验成功的 User, 时间戳)，TTL 300s，最多 256 条
# （超出清最旧），线程安全；只缓存校验成功的结果，不改对外行为。
_BASIC_AUTH_CACHE = {}
_BASIC_AUTH_CACHE_LOCK = threading.Lock()
_BASIC_AUTH_CACHE_TTL_SECONDS = 300
_BASIC_AUTH_CACHE_MAX_ENTRIES = 256


def _basic_auth_cache_key(username, password):
    return hashlib.sha256(f'{username}:{password}'.encode('utf-8')).hexdigest()


def _basic_auth_cache_get(username, password):
    """命中且未过期返回缓存的 User，否则返回 None。"""
    key = _basic_auth_cache_key(username, password)
    with _BASIC_AUTH_CACHE_LOCK:
        entry = _BASIC_AUTH_CACHE.get(key)
        if entry is None:
            return None
        user, ts = entry
        if time.time() - ts > _BASIC_AUTH_CACHE_TTL_SECONDS:
            _BASIC_AUTH_CACHE.pop(key, None)
            return None
        return user


def _basic_auth_cache_put(username, password, user):
    """写入一条成功校验结果；超出上限清最旧一条。"""
    key = _basic_auth_cache_key(username, password)
    with _BASIC_AUTH_CACHE_LOCK:
        _BASIC_AUTH_CACHE[key] = (user, time.time())
        if len(_BASIC_AUTH_CACHE) > _BASIC_AUTH_CACHE_MAX_ENTRIES:
            oldest_key = min(_BASIC_AUTH_CACHE, key=lambda k: _BASIC_AUTH_CACHE[k][1])
            _BASIC_AUTH_CACHE.pop(oldest_key, None)


def is_adult_comic(target):
    """
    模板用：该漫画是否来自 18+ 源（封面渲染红色 18+ 徽章）。

    target 可为：
    - DownloadTask 对象：直接看 task.url（最快路径）；
    - dict（get_available_comics 快照 / history items）：无 url 字段，回查任务表；
    - comic_name 字符串：回查任务表。
    任何异常一律 False，绝不影响页面渲染。
    """
    from mangadock.models import DownloadTask
    from mangadock.services.providers import is_adult_url

    try:
        if target is None:
            return False
        if not isinstance(target, str):
            url = getattr(target, 'url', None)
            if not url and isinstance(target, dict):
                url = target.get('url')
            name = getattr(target, 'comic_name', None)
            if not name and isinstance(target, dict):
                name = target.get('comic_name')
            if url:
                return is_adult_url(url)
        else:
            name = target
        if not name:
            return False
        cached = _ADULT_COMIC_CACHE.get(name)
        if cached is not None:
            return cached
        row = (DownloadTask.query
               .filter(DownloadTask.comic_name == name, DownloadTask.url.isnot(None))
               .order_by(DownloadTask.created_at.desc())
               .first())
        result = is_adult_url(row.url) if row else False
        _ADULT_COMIC_CACHE[name] = result
        return result
    except Exception:
        return False


def inject_user_context():
    current_user = get_current_user()
    is_admin_user = bool(current_user and current_user.is_admin)
    can_manage_library = is_admin_user or bool(
        current_user and getattr(current_user, 'can_download', False)
    )
    return {
        'current_user': current_user,
        'is_admin_user': is_admin_user,
        'can_manage_library': can_manage_library,
        'can_view_adult_override': bool(
            current_user and getattr(current_user, 'can_view_adult', False)
        ),
        'asset_url': asset_url,
        'cover_image_url': cover_image_url,
        'cover_hero_image_url': cover_hero_image_url,
        'is_adult_comic': is_adult_comic,
        'display_reading_minutes': api_display_reading_minutes,
    }

def api_error(code, message, status=400):
    headers = None
    if status == 401:
        # 2026-09-20：带 Basic 挑战头，iOS URLSession / OkHttp 等客户端在
        # 收到 challenge 时可用已存凭据自动重试（扩展偶发丢凭据的场景）
        headers = {'WWW-Authenticate': 'Basic realm="MangaDock"'}
    resp = jsonify({
        'error': {
            'code': code,
            'message': message,
        }
    })
    resp.status_code = status
    if headers:
        for k, v in headers.items():
            resp.headers[k] = v
    return resp, status


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


def login_failure_keys(username, ip_address=None):
    """按「用户名 + IP」双维度生成锁定键，任一键被锁即拒绝：
    user 键防跨 IP 撞同一账号，ip 键防单 IP 用户名喷洒；第三方无法用单一维度恶意锁死别人。"""
    normalized_username = (username or '').strip().lower()
    keys = []
    if normalized_username:
        keys.append(f'user:{normalized_username}')
    if ip_address:
        keys.append(f'ip:{ip_address}')
    return keys or ['unknown']


def _as_aware_china(value):
    """SQLite 存的是 naive datetime，读回来要补上 China 时区再参与比较。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=china_tz)
    return value


def login_lock_state(keys):
    """检查锁定状态，返回 (locked, remaining_minutes, hit_key)；过期的锁定记录顺手清除。

    2026-09-20 code review P2：失败计数改为落库（models.LoginFailure）。
    此前用模块级内存 dict，而 gunicorn 起 2~4 个 worker 进程，各持一份计数，
    攻击者把尝试分散到不同进程即可让任一进程都达不到阈值 → 登录锁定被绕过。
    现在多进程共享同一份 DB 计数，并用 SQLite 原子 UPSERT 累加。
    """
    if not keys:
        return False, 0, None
    now = datetime.now(china_tz)
    rows = LoginFailure.query.filter(LoginFailure.failure_key.in_(list(keys))).all()
    expired_rows = []
    for row in rows:
        if row.fail_count < LOGIN_MAX_ATTEMPTS:
            continue
        last_failure_at = _as_aware_china(row.last_failure_at) or now
        elapsed = now - last_failure_at
        if elapsed < LOGIN_LOCKOUT_DURATION:
            remaining_time = LOGIN_LOCKOUT_DURATION - elapsed
            return True, max(1, remaining_time.seconds // 60), row.failure_key
        expired_rows.append(row)
    if expired_rows:
        for row in expired_rows:
            db.session.delete(row)
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            app.logger.warning('清除过期登录锁定记录失败: %s', exc)
    return False, 0, None


def login_record_failure(keys):
    """对每个键原子累加失败次数并刷新时间戳，返回最大计数（用于剩余次数提示）。"""
    if not keys:
        return 1
    now = datetime.now(china_tz)
    for key in keys:
        statement = sqlite_insert(LoginFailure).values(
            failure_key=key,
            fail_count=1,
            last_failure_at=now,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=[LoginFailure.failure_key],
            set_={
                'fail_count': LoginFailure.fail_count + 1,
                'last_failure_at': now,
                'updated_at': now,
            },
        )
        db.session.execute(statement)
    try:
        db.session.commit()
    except Exception as exc:
        # 计数写失败不应让登录流程 500：退回「至少记了 1 次」的语义
        db.session.rollback()
        app.logger.warning('记录登录失败次数失败: %s', exc)
        return 1

    counts = [
        row.fail_count
        for row in LoginFailure.query.filter(LoginFailure.failure_key.in_(list(keys))).all()
    ]
    return max(counts) if counts else 1


def login_clear_failures(keys):
    """登录成功后清除该用户名/IP 的全部失败计数。"""
    if not keys:
        return
    try:
        LoginFailure.query.filter(LoginFailure.failure_key.in_(list(keys))).delete(
            synchronize_session=False
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.warning('清除登录失败计数失败: %s', exc)


def authenticate_api_credentials(username, password, ip_address, user_agent):
    normalized_username = (username or '').strip()
    raw_password = password or ''

    if not normalized_username or not raw_password:
        return None, api_error('INVALID_CREDENTIALS', '用户名或密码错误', 401)

    failure_keys = login_failure_keys(normalized_username, ip_address)
    locked, remaining_minutes, _hit = login_lock_state(failure_keys)
    if locked:
        record_login_log(
            normalized_username,
            ip_address,
            user_agent,
            False,
            f'账户已锁定，剩余锁定时间 {remaining_minutes} 分钟'
        )
        return None, api_error('ACCOUNT_LOCKED', f'登录失败次数过多，请在 {remaining_minutes} 分钟后重试', 423)

    # 2026-09-22 code review P2：成功校验结果命中缓存则跳过 pbkdf2 重算。
    # 命中路径不记登录日志、不清失败计数——每次 API 请求都走这里，
    # 写库会造成日志膨胀；失败计数由真实校验成功时清理即可。
    cached_user = _basic_auth_cache_get(normalized_username, raw_password)
    if cached_user is not None:
        return cached_user, None

    user = validate_user_credentials(normalized_username, raw_password)
    login_success = user is not None

    if login_success and user:
        _basic_auth_cache_put(normalized_username, raw_password, user)
        login_clear_failures(failure_keys)
        record_login_log(normalized_username, ip_address, user_agent, True, '登录成功')
        return user, None

    new_fail_count = login_record_failure(failure_keys)
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
        g.api_user = make_api_request_user(user)
        return f(*args, **kwargs)
    return decorated_function


def api_admin_required(f):
    """Require authenticated admin user for JSON APIs."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user, error_response = get_api_authenticated_user()
        if error_response:
            return error_response
        if not user:
            return api_error('AUTH_REQUIRED', '登录信息已失效，请重新登录', 401)
        if not user.is_admin:
            return api_error('FORBIDDEN', '当前账号没有此操作权限', 403)
        g.api_user = make_api_request_user(user)
        return f(*args, **kwargs)
    return decorated_function


def api_datetime(value):
    from mangadock.services.updates import normalize_china_datetime
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
    from mangadock.services.groups import get_comic_group_map
    from mangadock.services.library import get_cached_local_comic_scan, get_comic_directory
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


def cover_version_token(comic_name):
    """封面文件的版本号（mtime 秒）。

    ⚠️ 2026-09-22 起**不再用于 `cover_url`**：当天实测 Tachimanga 无法显示
    「路径段带版本号」的封面 URL（`/cover/<mtime>.jpg`），改成裸形态
    `/api/comics/<id>/cover` 才恢复。函数保留备用（例如将来给 Web 端做破缓存），
    但**不要**再把它的值拼进 API 下发的 `cover_url`。

    历史背景：封面在下载流程里后落盘，而端点缺图时会回落 140x140 的占位图 PNG，
    API 客户端会把占位图当有效图片缓存住 —— 实测 Tachimanga 对同一个无参数 URL
    请求 33 次拿到占位图后就不再请求。现在该端点缺图返回 404，客户端不会缓存，
    所以已经不需要靠 URL 变化来破缓存。Web 端另有 ``cover-retry.js`` 靠 404 重试。
    """
    try:
        return int(os.path.getmtime(os.path.join(COVER_ROOT, f'{comic_name}.jpg')))
    except OSError:
        return 0


def serialize_api_comic_summary(comic, progress=None, source_mapping=None):
    from mangadock.services.groups import normalize_group_name
    from mangadock.services.library import get_comic_identity
    # 2026-09-22 code review P2：列表/详情等读路径走只读查询，缺失身份不再写库。
    # 身份缺失极罕见（快照阶段已批量预建），此时用 comic_name 兜底 id，不阻断返回。
    comic_name = comic.get('comic_name')
    if not comic_name:
        return None
    identity = get_comic_identity(comic_name)
    comic_id = identity.comic_id if identity else comic_name
    return {
        'id': comic_id,
        'title': comic_name,
        # 2026-09-22 回滚（第三次修复作废）：封面地址回到**裸形态**
        # `/api/comics/<id>/cover`，不再带任何版本号。
        #
        # 证据（同日 App 侧访问日志，按分钟切开对比）：
        #   08:43–08:44 裸 `/cover`      70 请求 / 70 本          → 68/70 正常显示
        #   08:47–08:48 `/cover?v=`      82 请求 / 66 本          → 正常显示
        #   09:15 起    `/cover/<mtime>.jpg`  38~40 本被反复重拉（每本 2~6 次）→ 整架全空
        # 三种形态服务端返回的都是同一张真 JPEG（md5 与磁盘原图一致、全 200），
        # 所以差别只在 URL 形态本身：Tachimanga 拿得到图却用不上「路径段带版本号」的
        # 封面 URL。结论——不要在这个 URL 上做任何花样。
        #
        # 破缓存这件事交给服务端更干净：封面未落盘时返回 404（见
        # api_routes._comic_cover_response），客户端不会把 404 写进图片缓存，
        # 下次展示自然重试。这正是本次问题的真解，版本号是多余且有害的。
        # 2026-09-22 10:1x 判别实验（第五次，也是最有判别力的一次）：
        # 把 API 下发的封面地址换成 **Web 端一直在用的静态路径**
        # `/static/cover/<URL 编码的漫画名>.jpg`，即**整个绕开 `/api` 端点**。
        #
        # 为什么现在做这件事（新增的决定性事实）：
        #   ① 用户实测「**自定义封面能显示，所有在线封面都不行**」——这证明
        #      App 的封面渲染 + 写入链路是好的，病在「网络封面的下载/落盘」这一层。
        #   ② 同一台 iPhone 用 Safari / PWA 打开 Web 端（正是这个静态路径）
        #      **封面完全正常**。也就是同一个域名、同一个端口、同一个反代之下：
        #      静态路径能显示、`/api` 路径不能。
        #   ③ 服务端已彻底证死无罪：近 12 小时封面请求 1006 次全部 200，
        #      字节 md5 与磁盘原图一致，`sips`（iOS 同源解码器）全量扫描 0 失败。
        #   ④ 刚测出的**响应头实质差异**（这条是本次改动的直接依据）：
        #        /api/comics/<id>/cover → 需要认证 → Flask 读 session
        #                                 ⇒ 带 `Vary: Cookie` + `Set-Cookie`
        #        /static/cover/<名>.jpg → 不碰 session
        #                                 ⇒ **既无 `Vary: Cookie` 也无 `Set-Cookie`**
        #      对带磁盘缓存的 iOS 图片加载器来说，`Vary: Cookie`（且每次请求
        #      Cookie 都会因 `Set-Cookie` 而变化）会让响应**几乎永远无法命中缓存**，
        #      表现就是「每本书每次刷新都被重下 8~9 次、界面始终是占位图」——
        #      与实测的请求形态完全吻合。
        #
        # 判读方式（用户下拉刷新一次书架即可）：
        #   换完**能**显示 → 病根就在 `/api` 端点的这些响应特征上，本改动即修复。
        #   换完**仍**不显示 → 与 URL、端点、响应头全都无关，100% 在 App 的
        #                      封面下载/落盘，直接按官方处置走「移除→重新添加 / 重装」。
        # 顺带收益：静态路径无需任何认证（已实测无凭据直接 200），
        # 可一并消掉日志里那条会返回 401 的支路。
        'cover_url': f'/static/cover/{quote(comic_name, safe="")}.jpg',
        'group_name': normalize_group_name(comic.get('group')) or '默认分组',
        'format': api_comic_format(comic.get('comic_format')),
        'chapter_count': comic.get('available_chapters') or comic.get('total_chapters') or 0,
        'source_url': (source_mapping or {}).get(comic_name),
        'created_at': api_datetime(comic.get('created_at')),
        'updated_at': api_datetime(progress.last_read_at if progress else comic.get('created_at')),
        'progress': serialize_api_progress(progress),
    }


def serialize_api_chapter(comic_id, chapter):
    from mangadock.services.library import build_chapter_id
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
    from mangadock.services.library import build_chapter_id, list_local_chapters
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
    from mangadock.services.library import (
        get_available_comics, get_comic_identity, get_comic_identity_by_id,
    )
    identity = get_comic_identity_by_id(comic_id)
    if not identity:
        # 2026-09-22 code review 复审补充：列表接口在身份缺失时会用 comic_name
        # 兜底当 id，这里必须同样能按名字解析回来，否则该漫画详情/封面 404。
        from mangadock.services.library import is_safe_comic_name
        if is_safe_comic_name(comic_id or ''):
            identity = get_comic_identity(comic_id)
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
    from mangadock.services.groups import filter_grouped_comics_for_user
    from mangadock.services.library import get_available_comics, load_comic_mapping
    from mangadock.services.reading import (
        get_reading_time_by_comic,
        get_reading_time_daily_for_year,
        get_reading_time_monthly_for_year,
        get_total_reading_time,
    )
    total_time = get_total_reading_time(user_id)
    reading_time_rank = get_reading_time_by_comic(user_id)
    reading_time_monthly = get_reading_time_monthly_for_year(year, user_id)
    reading_time_daily = get_reading_time_daily_for_year(year, user_id)
    # 2026-09-20 code review P2：排名也要按分组权限过滤。
    # 否则「曾被授权、读过、之后被撤权」的漫画标题仍会出现在 /api/statistics，
    # 与已经修好的 /history 口径不一致（信息泄露）。
    # 注意只剔除「确实存在于漫画库、但当前账号无权的分组」的条目；
    # 小说等不在 get_available_comics() 里的条目继续走 build_local_comic_entry 兜底。
    all_comics = get_available_comics()
    statistic_user = User.query.filter_by(id=user_id).first()
    accessible_comics, _stat_groups, _stat_counts, _stat_lookup = filter_grouped_comics_for_user(
        all_comics, statistic_user
    )
    comic_lookup = {
        comic.get('comic_name'): comic
        for comic in accessible_comics
        if comic.get('comic_name')
    }
    restricted_comic_names = {
        comic.get('comic_name')
        for comic in all_comics
        if comic.get('comic_name')
    } - set(comic_lookup)
    source_mapping = load_comic_mapping()
    ranking_items = []

    for index, entry in enumerate(reading_time_rank, start=1):
        comic_name = entry.get('comic_name')
        if not comic_name:
            continue
        if comic_name in restricted_comic_names:
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
