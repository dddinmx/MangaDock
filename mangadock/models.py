# -*- coding: utf-8 -*-
"""SQLAlchemy models and database initialization."""
import os
from datetime import datetime
import secrets
import time

from sqlalchemy import UniqueConstraint, or_, text
from sqlalchemy.exc import OperationalError
from werkzeug.security import check_password_hash, generate_password_hash

from mangadock.core import app
from mangadock.extensions import db
from mangadock.settings import china_tz

class DownloadTask(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    comic_name = db.Column(db.String(255))
    url = db.Column(db.String(512))
    status = db.Column(db.String(20), default='pending')  # pending, running, completed, cancelled, error
    progress_percent = db.Column(db.Integer, default=0)
    total_chapters = db.Column(db.Integer, default=0)
    completed_chapters = db.Column(db.Integer, default=0)
    comic_format = db.Column(db.Integer, default=1)
    log = db.Column(db.Text, default='')
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))
    is_update = db.Column(db.Boolean, default=False)
    group = db.Column(db.String(255), default='默认分组')  # 分组字段

class ReadingProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    comic_name = db.Column(db.String(255), nullable=False)
    last_chapter = db.Column(db.Integer, default=0)
    last_page = db.Column(db.Integer, default=0)
    scroll_position = db.Column(db.Integer, default=0)  # 滚动位置
    total_chapters = db.Column(db.Integer, default=0)
    total_pages = db.Column(db.Integer, default=0)
    last_read_at = db.Column(db.DateTime, default=datetime.now(china_tz))
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))


class ReadingTime(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    comic_name = db.Column(db.String(255), nullable=False)
    duration = db.Column(db.Integer, nullable=False)  # in minutes
    duration_seconds = db.Column(db.Integer, nullable=False, default=0)
    read_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))


class ReadingSessionState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    comic_name = db.Column(db.String(255), nullable=False)
    session_key = db.Column(db.String(128), nullable=False)
    last_reported_seconds = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))
    updated_at = db.Column(
        db.DateTime,
        default=datetime.now(china_tz),
        onupdate=datetime.now(china_tz)
    )

    __table_args__ = (
        UniqueConstraint('user_id', 'comic_name', 'session_key', name='uq_reading_session_state'),
    )


class ComicGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))


class ComicGroupMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    comic_name = db.Column(db.String(255), unique=True, nullable=False)
    group_name = db.Column(db.String(255), nullable=False, default='默认分组')
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz), onupdate=lambda: datetime.now(china_tz))


class UserGroupPermission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    group_name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))


class AdminHiddenLibraryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    target_type = db.Column(db.String(20), nullable=False)
    target_value = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))

    __table_args__ = (
        UniqueConstraint('user_id', 'target_type', 'target_value', name='uq_admin_hidden_library_item'),
    )


class ComicScanPath(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(1024), unique=True, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(china_tz),
        onupdate=lambda: datetime.now(china_tz)
    )


class ComicIdentity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    comic_id = db.Column(db.String(32), unique=True, nullable=False, index=True)
    comic_name = db.Column(db.String(255), unique=True, nullable=False)
    description = db.Column(db.Text)  # 站点爬取的作品简介
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(china_tz),
        onupdate=lambda: datetime.now(china_tz)
    )


class ComicUpdateCheck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    comic_name = db.Column(db.String(255), unique=True, nullable=False)
    source_url = db.Column(db.String(1024), nullable=False)
    has_updates = db.Column(db.Boolean, nullable=False, default=False)
    pending_chapters = db.Column(db.Integer, nullable=False, default=0)
    remote_total_chapters = db.Column(db.Integer, nullable=False, default=0)
    local_total_chapters = db.Column(db.Integer, nullable=False, default=0)
    latest_chapter_title = db.Column(db.String(255))
    status = db.Column(db.String(20), nullable=False, default='pending')
    error_message = db.Column(db.String(500))
    last_checked_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(china_tz),
        onupdate=lambda: datetime.now(china_tz)
    )


class BackgroundCommand(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    command_type = db.Column(db.String(64), nullable=False, index=True)
    payload = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    requested_at = db.Column(db.DateTime, default=lambda: datetime.now(china_tz))
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    message = db.Column(db.String(500))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(china_tz),
        onupdate=lambda: datetime.now(china_tz)
    )


class AppSetting(db.Model):
    key = db.Column(db.String(128), primary_key=True)
    value = db.Column(db.String(500), nullable=False, default='')
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(china_tz),
        onupdate=lambda: datetime.now(china_tz)
    )


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')

    def set_password(self, password):
        # 使用 pbkdf2:sha256 算法，确保在 Docker 精简镜像中也能正常工作
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def check_password(self, password):
        try:
            return check_password_hash(self.password_hash, password)
        except (TypeError, ValueError):
            return False

    @property
    def is_admin(self):
        return self.role == 'admin'


class LoginLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    ip_address = db.Column(db.String(45), nullable=False)
    user_agent = db.Column(db.String(500))
    login_time = db.Column(db.DateTime, default=datetime.now(china_tz))
    success = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.String(200))

def ensure_sqlite_column(table_name, column_name, ddl):
    with db.engine.begin() as connection:
        result = connection.execute(text(f'PRAGMA table_info("{table_name}")'))
        existing_columns = {row[1] for row in result.fetchall()}
        if column_name in existing_columns:
            return
        try:
            connection.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN {column_name} {ddl}'))
        except OperationalError as exc:
            if 'duplicate column name' not in str(exc).lower():
                raise


def initialize_database():
    with app.app_context():
        for attempt in range(3):
            try:
                db.create_all()
                break
            except OperationalError as exc:
                if 'already exists' not in str(exc).lower() or attempt == 2:
                    raise
                time.sleep(0.1)

        ensure_sqlite_column(User.__table__.name, 'role', "VARCHAR(20) DEFAULT 'user'")
        ensure_sqlite_column(ReadingProgress.__table__.name, 'user_id', 'INTEGER')
        ensure_sqlite_column(ReadingTime.__table__.name, 'user_id', 'INTEGER')
        ensure_sqlite_column(ReadingTime.__table__.name, 'duration_seconds', 'INTEGER DEFAULT 0')
        ensure_sqlite_column(ComicIdentity.__table__.name, 'description', 'TEXT')

        if not ComicGroup.query.filter_by(name='默认分组').first():
            db.session.add(ComicGroup(name='默认分组'))
            db.session.commit()

        admin_user = User.query.filter_by(username="admin").first()
        if not admin_user:
            admin_user = User(username="admin", role='admin')
            initial_admin_password = os.environ.get('MANGADOCK_ADMIN_PASSWORD', '').strip()
            if not initial_admin_password:
                initial_admin_password = secrets.token_urlsafe(18)
                print(f"已生成初始管理员密码：admin / {initial_admin_password}")
            admin_user.set_password(initial_admin_password)
            db.session.add(admin_user)
            db.session.commit()
            print("已创建默认管理员用户：admin")
        elif admin_user.role != 'admin':
            admin_user.role = 'admin'
            db.session.commit()

        User.query.filter(
            or_(User.role.is_(None), User.role == '')
        ).update({'role': 'user'}, synchronize_session=False)
        db.session.commit()

        db.session.execute(
            text(f'UPDATE "{ReadingProgress.__table__.name}" SET user_id = :user_id WHERE user_id IS NULL'),
            {'user_id': admin_user.id}
        )
        db.session.execute(
            text(f'UPDATE "{ReadingTime.__table__.name}" SET user_id = :user_id WHERE user_id IS NULL'),
            {'user_id': admin_user.id}
        )
        db.session.execute(
            text(
                f'UPDATE "{ReadingTime.__table__.name}" '
                'SET duration_seconds = CASE '
                'WHEN duration_seconds IS NULL OR duration_seconds <= 0 THEN COALESCE(duration, 0) * 60 '
                'ELSE duration_seconds END'
            )
        )
        db.session.commit()
        from mangadock.services.library import sync_comic_identity_records
        sync_comic_identity_records()
