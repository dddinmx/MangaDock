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

from mangadock import app
from mangadock.services.workers import run_command_worker, run_task_worker
from mangadock.settings import TASK_WORKER_COUNT


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == '--run-task-worker':
        worker_index = int(sys.argv[2]) if len(sys.argv) >= 3 else 0
        run_task_worker(worker_index)
        return

    if len(sys.argv) >= 2 and sys.argv[1] == '--run-command-worker':
        run_command_worker()
        return

    background_processes = []
    background_cleanup_state = {'done': False}

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


if __name__ == '__main__':
    main()
