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

def start_download_task(url, comic_format, allow_adult=False):
    """创建下载任务并加入后台队列（allow_adult=创建者的 18+ 覆盖授权快照）"""
    task_id = create_task(url, comic_format, allow_adult=bool(allow_adult))
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
            candidates = DownloadTask.query.filter_by(status='pending').order_by(
                DownloadTask.created_at.asc(),
                DownloadTask.id.asc()
            ).all()
            if not candidates:
                return None

            now = datetime.now(china_tz)
            chosen = None

            for task in candidates:
                # ---- 同漫画并发防护 ----
                # 完整下载会先清空再重建章节目录，任何任务与它并发操作同一本漫画
                # 都会撞上目录被删的窗口（CBZ 生成 FileNotFoundError）。
                # 规则：
                # 1) 更新任务：同漫画已有活动任务（下载/更新）→ 取消更新
                #    （完整下载会重新抓取全部章节，更新内容被覆盖）
                # 2) 下载任务：同漫画已有相同 URL 的活动任务 → 取消（重复提交）
                # 3) 下载任务：同漫画有不同 URL 的活动任务 → 暂缓，保持 pending 等待
                conflict = DownloadTask.query.filter(
                    DownloadTask.id != task.id,
                    DownloadTask.comic_name == task.comic_name,
                    DownloadTask.comic_name != '未知漫画',
                    DownloadTask.status.in_(('pending', 'running')),
                ).order_by(
                    DownloadTask.created_at.asc(),
                    DownloadTask.id.asc()
                ).first()

                if conflict is not None:
                    conflict_desc = f'（{conflict.id[:8]}…）'
                    if task.is_update:
                        task.status = 'cancelled'
                        task.end_time = now
                        task.log = (
                            (task.log or '')
                            + f'已有该漫画的任务正在排队或运行{conflict_desc}，更新任务已跳过（下载任务会一并刷新全部章节）\n'
                        )
                        db.session.commit()
                        continue
                    if conflict.url and task.url and conflict.url == task.url:
                        task.status = 'cancelled'
                        task.end_time = now
                        task.log = (
                            (task.log or '')
                            + f'该漫画已有相同链接的任务正在排队或运行{conflict_desc}，重复任务已跳过\n'
                        )
                        db.session.commit()
                        continue
                    # 不同链接的同漫画任务 → 暂缓，继续看队列里其他漫画的任务
                    continue

                chosen = task
                break

            if chosen is None:
                return None

            updated_rows = DownloadTask.query.filter_by(id=chosen.id, status='pending').update(
                {
                    'status': 'running',
                    'start_time': chosen.start_time or now,
                    'end_time': None,
                    'log': (chosen.log or '') + '后台 worker 已开始处理任务\n',
                },
                synchronize_session=False
            )
            db.session.commit()
            if updated_rows:
                return chosen.id
            # chosen 被并发抢占 → 重新查询


def execute_download_task(task_id):
    task = get_task(task_id)
    if not task:
        return False
    if task.status == 'cancelled':
        return False

    from mangadock.services.fanqie_comics import is_fanqie_comic_target, execute_fanqie_comic_task
    if is_fanqie_comic_target(task.url):
        return execute_fanqie_comic_task(task_id)

    from mangadock.services.fanqie import book_id_from_task_url, execute_fanqie_task
    if book_id_from_task_url(task.url):
        return execute_fanqie_task(task_id)

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
                try:
                    execute_download_task(task_id)
                except Exception as e:
                    # 2026-09-20 code review：未捕获异常会杀死 worker 循环，
                    # 后续任务永久卡 pending（仅重启可恢复）。兜底：任务置 error 后继续。
                    print(f"❌ 任务 {task_id} 执行异常：{e}")
                    try:
                        update_task(task_id, status='error', log=f"执行异常：{e}")
                    except Exception:
                        pass
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
