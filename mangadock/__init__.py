# -*- coding: utf-8 -*-
"""MangaDock application package.

Importing this package constructs the Flask app and registers all routes.
"""
from mangadock.core import app
from mangadock.extensions import csrf, db

# Models must load before DB init helpers
from mangadock import models  # noqa: F401
from mangadock.models import initialize_database

# Domain services (side-effect free at import except locks/constants)
from mangadock.services import library as library_service  # noqa: F401
from mangadock.services import reading as reading_service  # noqa: F401
from mangadock.services import groups as groups_service  # noqa: F401
from mangadock.services import tasks as tasks_service  # noqa: F401
from mangadock.services import updates as updates_service  # noqa: F401
from mangadock.services import download as download_service  # noqa: F401
from mangadock.services import workers as workers_service  # noqa: F401

from mangadock import auth  # noqa: F401

# Register HTTP routes (decorators bind to app)
from mangadock.blueprints import api_routes  # noqa: F401
from mangadock.blueprints import web_routes  # noqa: F401
from mangadock.api_v1 import register_api_v1

register_api_v1(app)


@app.context_processor
def _inject_user_context():
    return auth.inject_user_context()


# Initialize DB schema and defaults once at import (same as legacy monolith)
initialize_database()

__all__ = ['app', 'db', 'csrf', 'create_app']


def create_app():
    """Return the application instance (already configured)."""
    return app
