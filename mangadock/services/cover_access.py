# -*- coding: utf-8 -*-
"""Cover file ACL and SMB-retry send path.

The Flask route in core.py stays a one-liner so group/banner checks do not
live next to session cookies and SQLite pragmas.
"""
import hashlib
import os
import time

from flask import abort, request, send_file
from werkzeug.utils import safe_join

from mangadock.core import app
from mangadock.services.groups import can_user_access_group, get_comic_group_map

_COVER_RETRY_DELAYS = (0.0, 0.4, 1.0, 2.5)


def resolve_cover_request_user():
    from mangadock.auth import (
        authenticate_api_credentials,
        get_basic_auth_credentials,
        get_current_user,
        is_basic_auth_request,
    )

    if is_basic_auth_request():
        username, password = get_basic_auth_credentials()
        if username is None:
            return None
        user, _ = authenticate_api_credentials(
            username, password, request.remote_addr or '', request.user_agent.string
        )
        return user
    return get_current_user()


def comic_name_for_cover_filename(filename):
    if filename.startswith('home-banner/'):
        from mangadock.services.library import get_available_comics

        banner_key = os.path.basename(filename).split('-', 1)[0].split('.', 1)[0]
        return next((
            comic['comic_name'] for comic in get_available_comics()
            if hashlib.sha256(comic['comic_name'].strip().encode('utf-8')).hexdigest()
            == banner_key
        ), None)
    return os.path.splitext(os.path.basename(filename))[0]


def authorize_cover_file(filename):
    if filename == 'cover.png':
        return
    user = resolve_cover_request_user()
    if not user:
        abort(403)
    if user.is_admin or filename.startswith('novels/'):
        return
    comic_name = comic_name_for_cover_filename(filename)
    if not comic_name:
        abort(403)
    group = get_comic_group_map().get(comic_name) or '默认分组'
    if not can_user_access_group(group, user):
        abort(403)


def send_cover_file(filename):
    target = safe_join(app.static_folder, 'cover', filename)
    if target is None:
        abort(404)
    for delay in _COVER_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            os.stat(target)
            response = send_file(target, conditional=True)
            if filename != 'cover.png':
                response.headers['Cache-Control'] = 'private, no-cache'
            return response
        except FileNotFoundError:
            continue
        except OSError:
            continue
    app.logger.warning('cover serve failed after retries: %s', filename)
    abort(404)
