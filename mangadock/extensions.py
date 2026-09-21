# -*- coding: utf-8 -*-
"""Flask extensions (initialized in create_app)."""
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
csrf = CSRFProtect()

# Mutable runtime state shared across threads within one worker process.
# 注意：登录失败计数已改为落库（models.LoginFailure），不在这里——
# 内存态在多进程 gunicorn 下不共享，登录锁定会被绕过。
comic_root_listing_cache = {}
comic_directory_scan_cache = {}
comics_cache = {
    'data': None,
    'timestamp': 0,
    'expiration': 300,
    'refreshing': False,
}
update_check_cache_state = {
    'refreshing': False,
}
