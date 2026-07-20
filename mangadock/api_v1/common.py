# -*- coding: utf-8 -*-
"""Shared helpers for /api/v1 JSON endpoints."""
from functools import wraps

from flask import jsonify, request

from mangadock.auth import (
    api_admin_required,
    api_login_required,
    require_csrf_for_session_auth,
)


def api_ok(data=None, status=200, **extra):
    payload = {'ok': True}
    if data is not None:
        payload['data'] = data
    if extra:
        payload.update(extra)
    return jsonify(payload), status


def api_fail(code, message, status=400, **extra):
    payload = {
        'ok': False,
        'error': {
            'code': code,
            'message': message,
        },
    }
    if extra:
        payload.update(extra)
    return jsonify(payload), status


def request_json():
    """Parse JSON body; empty object when body is missing."""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return {}
    data = request.get_json(silent=True)
    if data is None:
        if request.content_length in (None, 0) and not request.data:
            return {}
        return None
    if not isinstance(data, dict):
        return None
    return data


def parse_comic_format(value, default=2):
    """Accept 1/2 or pdf/cbz. Default CBZ (2)."""
    if value is None or value == '':
        return default
    if isinstance(value, int):
        if value in (1, 2):
            return value
        raise ValueError('format 只能是 1(pdf) 或 2(cbz)')
    text = str(value).strip().lower()
    if text in {'1', 'pdf'}:
        return 1
    if text in {'2', 'cbz'}:
        return 2
    raise ValueError('format 只能是 pdf/cbz 或 1/2')


def require_write_auth(admin=False):
    """Decorator stack: login (or admin) + CSRF for session cookies."""
    auth_decorator = api_admin_required if admin else api_login_required

    def decorator(f):
        @auth_decorator
        @wraps(f)
        def wrapped(*args, **kwargs):
            if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                csrf_error = require_csrf_for_session_auth(api_response=True)
                if csrf_error:
                    # Normalize legacy api_error shape into v1 ok/error envelope
                    body, status = csrf_error
                    try:
                        payload = body.get_json(silent=True) or {}
                    except Exception:
                        payload = {}
                    err = payload.get('error') or {}
                    return api_fail(
                        err.get('code') or 'CSRF_REQUIRED',
                        err.get('message') or 'CSRF token 无效或缺失',
                        status if isinstance(status, int) else 400,
                    )
            return f(*args, **kwargs)
        return wrapped
    return decorator
