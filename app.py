# -*- coding: utf-8 -*-
"""MangaDock process entrypoint.

Keeps CLI flags and Gunicorn bootstrap compatible with the previous monolith:
  python app.py
  python app.py --run-task-worker [index]
  python app.py --run-command-worker
"""
import atexit
import os
import subprocess
import sys
import threading
import time

from mangadock import app
from mangadock.services.workers import (
    requeue_orphan_running_tasks,
    run_command_worker,
    run_task_worker,
)
from mangadock.settings import TASK_WORKER_COUNT

# 后台 worker 守护线程的轮询间隔；单个 worker 的重启冷却（避免崩溃循环里疯狂重启）
SUPERVISE_INTERVAL_SECONDS = 10
RESTART_COOLDOWN_SECONDS = 60


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == '--run-task-worker':
        worker_index = int(sys.argv[2]) if len(sys.argv) >= 3 else 0
        run_task_worker(worker_index)
        return

    if len(sys.argv) >= 2 and sys.argv[1] == '--run-command-worker':
        run_command_worker()
        return

    # 每个后台子进程一个 slot：{'name':…, 'args':(…), 'process': Popen}
    # 用 slot 而非裸 Popen 列表，是因为守护线程要在进程死后原地重启它。
    background_slots = []
    background_cleanup_state = {'done': False}
    supervise_lock = threading.Lock()
    last_restart_at = {}

    def preload_app():
        from sqlalchemy import text
        from mangadock.extensions import db
        from mangadock.services.library import refresh_comics_cache

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
        return subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), *args],
            close_fds=True,
            start_new_session=True
        )

    def spawn_background_process(slot):
        """按 slot 描述启动或重启子进程，并把它记回 slot。"""
        slot['process'] = start_background_process(slot['name'], *slot['args'])
        return slot

    def add_background_process(process_name, *args):
        slot = {'name': process_name, 'args': args, 'process': None}
        background_slots.append(slot)
        return spawn_background_process(slot)

    def stop_background_processes():
        if background_cleanup_state['done']:
            return
        background_cleanup_state['done'] = True

        for slot in background_slots:
            process = slot['process']
            if process is None or process.poll() is not None:
                continue
            try:
                process.terminate()
            except OSError:
                continue

        for slot in background_slots:
            process = slot['process']
            if process is None or process.poll() is not None:
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

    def supervise_background_processes():
        """后台 worker 的最小守护（2026-09-21 加）。

        为什么必须有：这 3 个后台进程（2 个下载 worker + 1 个命令 worker）只在启动时
        fork 一次，之后**没有任何东西照看它们**。gunicorn 的 arbiter 会 `waitpid(-1)`
        把不属于它的子进程一并回收，只在日志里留一行极易误读的记录 ——
        `[ERROR] Worker (pid:X) exited with code 1` 看着像 gunicorn worker 死了，
        其实是下载 worker —— 而且**不会重启它**。

        进程一旦消失，下载队列就永久饿死：任务卡在「等待中 / 任务已加入后台队列，
        等待 worker 处理」，除整服务重启别无出路（2026-09-21 实际踩到，一天内多次）。

        这里做两件事：
          ① 发现子进程退出、且已过冷却期，就原地重启它（冷却避免崩溃循环里反复重启）；
          ② 若一个任务 worker 都不在，把残留的 running 任务退回 pending ——
             否则它们会永远占着「运行中」。
        已知局限：只剩部分任务 worker 死亡时无法判定某个 running 任务归属谁，
        此时只告警不退回（真正的修法是给任务加 worker 归属字段）。
        """
        while True:
            time.sleep(SUPERVISE_INTERVAL_SECONDS)
            if background_cleanup_state['done']:
                # 主进程正在/已经收摊，别再补员 —— 否则可能留下一个没人管的孤儿
                # worker 占着锁，下次启动的 worker 会「跳过重复启动」，
                # 于是服务带着旧环境继续跑（2026-09-21 复查时想到的窄口子）
                return
            try:
                with supervise_lock:
                    dead_slots = [
                        slot for slot in background_slots
                        if slot['process'] is not None and slot['process'].poll() is not None
                    ]
                    if not dead_slots:
                        continue

                    # 在重启之前判定：还活着的任务 worker 有几条
                    alive_task_workers = [
                        slot for slot in background_slots
                        if slot['name'].startswith('mangadock-task-worker')
                        and slot['process'] is not None
                        and slot['process'].poll() is None
                    ]

                    for slot in dead_slots:
                        name = slot['name']
                        previous = slot['process']
                        now = time.time()
                        if now - last_restart_at.get(name, 0.0) < RESTART_COOLDOWN_SECONDS:
                            continue
                        last_restart_at[name] = now
                        try:
                            spawn_background_process(slot)
                        except OSError as exc:
                            print(f"⚠️  后台 worker 无法重启：{name}（{exc}）")
                            continue
                        print(
                            f"♻️  后台 worker 已重启：{name}"
                            f"（原 PID {previous.pid} 已退出，新 PID {slot['process'].pid}）"
                        )

                    if not alive_task_workers:
                        try:
                            requeued = requeue_orphan_running_tasks()
                            if requeued:
                                print(f"♻️  任务 worker 全部离线，{requeued} 个孤儿任务已退回队列")
                        except Exception as exc:
                            print(f"⚠️  退回孤儿任务失败：{exc}")
                    elif any(slot['name'].startswith('mangadock-task-worker') for slot in dead_slots):
                        print("⚠️  有下载 worker 退出（其余仍在运行），若有任务卡在「运行中」需人工确认")
            except Exception as exc:
                # 守护线程自己绝不能死，否则又回到「队列饿死」的老路
                print(f"⚠️  后台 worker 守护异常：{exc}")

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
        # Native host deployments keep the loopback default; the container
        # overrides this with MANGADOCK_BIND=0.0.0.0:5001 so port mapping works.
        'bind': os.environ.get('MANGADOCK_BIND', '127.0.0.1:5001'),
        'workers': worker_count,
        # SQLite 单写者 + DB 在 SMB 上（WAL 不可用，见 mangadock/core.py）。
        # 限制每 worker 的线程数，给并发写（请求线程写任务状态 / task worker 写进度）
        # 设一个上限，避免大量线程同时抢同一把写锁把请求线程挂住。
        # 这里保持 4（已 ≤ 8 推荐上限）；不要为“降低竞争”再上调，也不要减少 worker 进程数。
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
        add_background_process(
            f'mangadock-task-worker-{worker_index + 1}',
            '--run-task-worker',
            str(worker_index)
        )
    add_background_process('mangadock-command-worker', '--run-command-worker')

    threading.Thread(
        target=supervise_background_processes,
        name='mangadock-background-supervisor',
        daemon=True
    ).start()

    preload_app()
    try:
        StandaloneGunicornApplication(app, gunicorn_options).run()
    finally:
        stop_background_processes()


if __name__ == '__main__':
    main()
