# -*- coding: utf-8 -*-
"""Download task queue CRUD and lifecycle."""
import uuid
from datetime import datetime

from sqlalchemy import or_

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import DownloadTask
from mangadock.settings import china_tz

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
                from mangadock.services.download import load_comic_source
                resolved_comic_name = load_comic_source(url)['title']
            except Exception as exc:
                print(f"任务初始化时获取漫画标题失败: {exc}")

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

