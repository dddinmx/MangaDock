# -*- coding: utf-8 -*-
"""MangaDock /api/v1 blueprint package."""
from flask import Blueprint

from mangadock.api_v1.routes import register_routes
from mangadock.extensions import csrf


def create_api_v1_blueprint():
    bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')
    # Session writes validate CSRF manually via require_write_auth;
    # Basic Auth clients skip CSRF (same pattern as legacy /api/*).
    csrf.exempt(bp)
    register_routes(bp)
    return bp


def register_api_v1(app):
    app.register_blueprint(create_api_v1_blueprint())
