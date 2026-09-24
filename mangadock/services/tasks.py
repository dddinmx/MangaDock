# -*- coding: utf-8 -*-
"""Download task queue CRUD and lifecycle."""
import re
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import or_

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import DownloadTask
from mangadock.settings import china_tz

def safe_print(message, end="\n", flush=False):
    """安全打印函数，用于日志记录"""
    print(message, end=end, flush=flush)

def normalize_task_url(url):
    if not isinstance(url, str) or not url:
        return url
    try:
        from mangadock.services.manhuagui import normalize_book_url
        normalized_url = normalize_book_url(url)
        if normalized_url:
            return normalized_url
    except (ImportError, TypeError, ValueError):
        pass
    try:
        parsed_url = urlsplit(url)
    except ValueError:
        return url
    if (
        parsed_url.scheme.lower() in {'http', 'https'}
        and (parsed_url.hostname or '').lower() in {'guazimanhua.com', 'www.guazimanhua.com'}
        and parsed_url.path == '/comic.php'
    ):
        comic_id = (parse_qs(parsed_url.query).get('id') or [''])[0]
        if comic_id.isdigit():
            return f'https://www.guazimanhua.com/comic.php?id={comic_id}'
    return url


def normalize_comic_url_identity(url):
    """Canonical URL key for matching alternate links to a mapped comic."""
    normalized_url = normalize_task_url(url)
    if not isinstance(normalized_url, str):
        return normalized_url

    fanqie_comic_prefix = 'fanqie-comic://'
    if normalized_url.startswith(fanqie_comic_prefix):
        book_id = normalized_url[len(fanqie_comic_prefix):]
        if re.fullmatch(r'\d{8,24}', book_id):
            return f'{fanqie_comic_prefix}{book_id}'

    try:
        parsed = urlsplit(normalized_url)
    except ValueError:
        return normalized_url
    host = (parsed.hostname or '').lower()
    path = parsed.path or '/'

    if host in {'hipmh.com', 'www.hipmh.com', 'm.hipmh.com'}:
        host = 'hipmh.com'
        path = re.sub(r'^/[a-z]{2}(?=/works/)', '', path, flags=re.IGNORECASE)
    elif host in {'baozimh.org', 'www.baozimh.org'}:
        host = 'baozimh.org'
    elif host in {'baozimh.com', 'www.baozimh.com', 'cn.baozimhcn.com'}:
        host = 'baozimh.com'
    elif host in {'mxs12.cc', 'www.mxs12.cc', 'wzd1.cc', 'www.wzd1.cc'}:
        host = 'mxs12.cc'
        path = re.sub(r'^/book(?=/|$)', '', path, flags=re.IGNORECASE)
    else:
        return normalized_url

    return f'https://{host}{path.rstrip("/")}'

# 2026-09-24 code review P2：提交下载时同步抓标题，源站变慢/挂掉会拖住提交请求。
# 这里给预取加「成功缓存 + 失败冷却」：同一 URL 命中缓存不再访问源站；
# 失败的 URL 在冷却期内直接按「未知漫画」提交（worker 运行期会再解析标题）。
_TITLE_PREFETCH_SUCCESS_TTL = 24 * 3600
_TITLE_PREFETCH_FAILURE_COOLDOWN = 30 * 60
_TITLE_PREFETCH_CACHE_LIMIT = 512
_title_prefetch_cache = {}
_title_prefetch_lock = threading.Lock()


def _prefetch_comic_title(task_url):
    """预取漫画标题（带缓存/冷却），失败返回 None。"""
    now = time.time()
    with _title_prefetch_lock:
        cached = _title_prefetch_cache.get(task_url)
        if cached:
            cached_title, fetched_at = cached
            ttl = _TITLE_PREFETCH_SUCCESS_TTL if cached_title else _TITLE_PREFETCH_FAILURE_COOLDOWN
            if now - fetched_at < ttl:
                return cached_title

    title = None
    try:
        from mangadock.services.download import load_comic_source
        title = load_comic_source(task_url)['title']
    except Exception as exc:
        print(f"任务初始化时获取漫画标题失败: {exc}")

    with _title_prefetch_lock:
        if len(_title_prefetch_cache) >= _TITLE_PREFETCH_CACHE_LIMIT:
            _title_prefetch_cache.clear()
        _title_prefetch_cache[task_url] = (title, time.time())
    return title


def create_task(url, comic_format, is_update=False, comic_name=None, allow_adult=None, group=None,
                created_by_user_id=None):
    """创建新任务并返回任务ID"""
    with app.app_context():
        task_id = str(uuid.uuid4())
        task_url = normalize_task_url(url)

        resolved_comic_name = (comic_name or '').strip() or "未知漫画"
        if task_url and resolved_comic_name == "未知漫画":
            prefetched_title = _prefetch_comic_title(task_url)
            if prefetched_title:
                resolved_comic_name = prefetched_title

        if created_by_user_id is not None and resolved_comic_name != '未知漫画':
            from mangadock.services.groups import can_creator_download_comic
            if not can_creator_download_comic(resolved_comic_name, created_by_user_id):
                raise PermissionError('当前账号无权下载该漫画')

        task = DownloadTask(
            id=task_id,
            comic_name=resolved_comic_name,
            url=task_url,
            comic_format=comic_format,
            start_time=datetime.now(china_tz),
            is_update=is_update,
            group=group or '默认分组',
            created_by_user_id=created_by_user_id,
        )
        if allow_adult is not None:
            # 2026-09-20 P2：快照随 create_task 一次写入，消除落库→改列间隙被 worker 抢跑的窗口
            task.allow_adult = bool(allow_adult)
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
                from mangadock.services.library import schedule_comics_cache_refresh
                schedule_comics_cache_refresh()
                if task.is_update:
                    from mangadock.services.updates import schedule_update_checks_refresh
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


def finalize_task_status(task_id, status, log_message=None):
    """原子写入任务终态（completed/error）；任务已被取消时不覆盖，返回 False。

    2026-09-24 code review P1：下载循环只在每章开始前检查取消——用户若在
    最后一章下载期间取消，收尾代码仍会把状态覆盖成 completed/error。
    这里用条件 UPDATE（WHERE status != 'cancelled'）在数据库层面保证
    取消状态不被终态覆盖，消除「检查→写入」之间的竞态窗口。
    """
    with app.app_context():
        task = db.session.get(DownloadTask, task_id)
        if not task:
            return False
        new_log = (task.log or '') + log_message + '\n' if log_message else task.log
        updated_rows = DownloadTask.query.filter(
            DownloadTask.id == task_id,
            DownloadTask.status != 'cancelled',
        ).update(
            {
                'status': status,
                'end_time': datetime.now(china_tz),
                'log': new_log,
            },
            synchronize_session=False,
        )
        db.session.commit()
        if not updated_rows:
            return False
        if status == 'completed':
            from mangadock.services.library import schedule_comics_cache_refresh
            schedule_comics_cache_refresh()
            if task.is_update:
                from mangadock.services.updates import schedule_update_checks_refresh
                schedule_update_checks_refresh(force=True)
        return True


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
