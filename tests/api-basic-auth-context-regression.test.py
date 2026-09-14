#!/usr/bin/env python3
"""Regression: Basic Auth must survive nested Flask app contexts in API helpers."""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_in_isolated_copy():
    with tempfile.TemporaryDirectory(prefix='mangadock-api-auth-test-') as temp_dir:
        copied_root = Path(temp_dir) / 'MangaDock'
        shutil.copytree(
            ROOT,
            copied_root,
            ignore=shutil.ignore_patterns(
                '.git', '.venv', '__pycache__', 'instance', 'data', 'comic', '小说',
                'dockerhub-src', 'github-mangadock',
            ),
        )
        env = os.environ.copy()
        env['MANGADOCK_API_AUTH_TEST_ISOLATED'] = '1'
        env['PYTHONPATH'] = os.pathsep.join(
            filter(None, [str(copied_root), env.get('PYTHONPATH')])
        )
        subprocess.run(
            [sys.executable, str(copied_root / 'tests' / Path(__file__).name)],
            cwd=copied_root,
            env=env,
            check=True,
        )


def run_regression():
    from sqlalchemy import text

    from mangadock import app
    from mangadock.auth import api_login_required, get_api_request_user
    from mangadock.extensions import db
    from mangadock.models import LoginLog, User

    username = 'api-auth-regression-user'
    password = 'api-auth-regression-password'
    with app.app_context():
        LoginLog.query.filter_by(username=username).delete()
        User.query.filter_by(username=username).delete()
        user = User(username=username, role='admin')
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

    credentials = base64.b64encode(f'{username}:{password}'.encode()).decode()
    try:
        with app.test_request_context('/', headers={'Authorization': f'Basic {credentials}'}):
            @api_login_required
            def protected_handler():
                # This mirrors library/group helpers that open and close a
                # nested app context during an authenticated API request.
                with app.app_context():
                    db.session.execute(text('SELECT 1'))
                request_user = get_api_request_user()
                assert request_user.id > 0
                assert request_user.username == username
                assert request_user.is_admin
                return 'ok'

            assert protected_handler() == 'ok'
    finally:
        with app.app_context():
            LoginLog.query.filter_by(username=username).delete()
            User.query.filter_by(username=username).delete()
            db.session.commit()


if __name__ == '__main__':
    if os.environ.get('MANGADOCK_API_AUTH_TEST_ISOLATED'):
        run_regression()
    else:
        run_in_isolated_copy()
