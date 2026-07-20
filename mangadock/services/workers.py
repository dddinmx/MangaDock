# -*- coding: utf-8 -*-
"""Background task and command workers."""
import fcntl
import os
import time
from datetime import datetime

from mangadock.core import COMMAND_WORKER_LOCK_PATH, TASK_WORKER_LOCK_PREFIX, app
from mangadock.extensions import db
from mangadock.models import BackgroundCommand, ComicUpdateCheck, DownloadTask
from mangadock.services.download import download_complete_book, update_comic
from mangadock.services.library import load_comic_mapping
from mangadock.services.tasks import create_task, get_task, update_task
from mangadock.services.updates import (
    claim_next_background_command,
    execute_background_command,
    refresh_update_checks,
    should_refresh_update_checks,
)
from mangadock.settings import WORKER_POLL_INTERVAL_SECONDS, WORKER_SCHEDULE_HEARTBEAT_SECONDS, china_tz

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
