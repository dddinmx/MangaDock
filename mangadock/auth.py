# -*- coding: utf-8 -*-
"""Authentication, session helpers, and API serializers."""
import base64
import os
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import flash, g, jsonify, redirect, request, session, url_for
from flask_wtf.csrf import CSRFError
from PIL import Image

from mangadock.core import app, is_safe_next_target
from mangadock.extensions import csrf, db, login_failures
from mangadock.models import LoginLog, User
from mangadock.settings import COVER_ROOT, LOGIN_LOCKOUT_DURATION, LOGIN_MAX_ATTEMPTS, china_tz

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


def cover_image_url(comic_name, variant='default'):
    """
    variant:
      - default: original downloaded cover
      - hero: upscaled cache for full-bleed banners (generated on demand)
    """
    if not comic_name:
        return asset_url('cover/cover.png')

    if variant == 'hero':
        from mangadock.utils.cover_enhance import ensure_hero_cover, hero_cover_path
        hero_path = ensure_hero_cover(comic_name)
        cached = hero_cover_path(comic_name)
        if hero_path and cached and os.path.isfile(cached):
            return asset_url(f'cover/hero/{comic_name}.jpg')
        # fall through to source cover

    cover_filename = f'cover/{comic_name}.jpg'
    cover_path = os.path.join(app.static_folder, cover_filename)
    if os.path.exists(cover_path):
        return asset_url(cover_filename)
    return asset_url('cover/cover.png')


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

    try:
        from mangadock.utils.cover_enhance import refresh_hero_cover
        refresh_hero_cover(comic_name)
    except Exception:
        pass
    return cover_path


def inject_user_context():
    current_user = get_current_user()
    is_admin_user = bool(current_user and current_user.is_admin)
    return {
        'current_user': current_user,
        'is_admin_user': is_admin_user,
        'can_manage_library': is_admin_user,
        'asset_url': asset_url,
        'cover_image_url': cover_image_url,
        'cover_hero_image_url': cover_hero_image_url,
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
        g.api_user = user
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


def serialize_api_comic_summary(comic, progress=None, source_mapping=None):
    from mangadock.services.groups import normalize_group_name
    from mangadock.services.library import ensure_comic_identity
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
    from mangadock.services.library import get_available_comics, get_comic_identity_by_id
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

