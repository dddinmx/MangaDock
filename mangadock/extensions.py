# -*- coding: utf-8 -*-
"""Flask extensions (initialized in create_app)."""
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
csrf = CSRFProtect()

# Mutable runtime state shared across workers/request handlers
login_failures = {}
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
