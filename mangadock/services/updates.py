# -*- coding: utf-8 -*-
"""Comic update checks and background command queue."""
import json
import threading
from datetime import datetime

from mangadock.core import app
from mangadock.extensions import db, update_check_cache_state
from mangadock.models import AppSetting, BackgroundCommand, ComicUpdateCheck, DownloadTask
from mangadock.settings import (
    COMIC_UPDATE_MODE_AUTO,
    COMIC_UPDATE_MODE_MANUAL,
    COMIC_UPDATE_MODE_SETTING_KEY,
    COMIC_UPDATE_MODES,
    UPDATE_CHECK_INTERVAL,
    china_tz,
)
from mangadock.services.library import (
    detect_local_comic_format,
    get_comic_directory,
    get_local_chapter_bases,
    get_local_chapter_match_bases,
    is_existing_local_chapter,
    load_comic_mapping,
)
from mangadock.services.tasks import create_task

update_check_cache_lock = threading.Lock()

def get_update_check_map():
    with app.app_context():
        return {
            item.comic_name: item
            for item in ComicUpdateCheck.query.all()
        }


def get_last_update_check_time():
    with app.app_context():
        return db.session.query(db.func.max(ComicUpdateCheck.last_checked_at)).scalar()


def normalize_china_datetime(value):
    if not value:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=china_tz)
    return value.astimezone(china_tz)


def get_update_candidates():
    with app.app_context():
        return ComicUpdateCheck.query.filter_by(has_updates=True).order_by(
            ComicUpdateCheck.pending_chapters.desc(),
            ComicUpdateCheck.last_checked_at.desc(),
            ComicUpdateCheck.comic_name.asc()
        ).all()


def get_app_setting(key, default_value=''):
    with app.app_context():
        setting = db.session.get(AppSetting, key)
        if not setting:
            return default_value
        return setting.value


def set_app_setting(key, value):
    with app.app_context():
        setting = db.session.get(AppSetting, key)
        if not setting:
            setting = AppSetting(key=key, value=value)
            db.session.add(setting)
        else:
            setting.value = value
            setting.updated_at = datetime.now(china_tz)
        db.session.commit()
        return setting.value


def get_comic_update_mode():
    mode = get_app_setting(COMIC_UPDATE_MODE_SETTING_KEY, COMIC_UPDATE_MODE_MANUAL)
    if mode not in COMIC_UPDATE_MODES:
        return COMIC_UPDATE_MODE_MANUAL
    return mode


def set_comic_update_mode(mode):
    normalized_mode = (mode or '').strip().lower()
    if normalized_mode not in COMIC_UPDATE_MODES:
        raise ValueError('更新模式无效')
    return set_app_setting(COMIC_UPDATE_MODE_SETTING_KEY, normalized_mode)


def resolve_update_format(comic_name):
    comic_path = get_comic_directory(comic_name)
    if comic_path:
        comic_format = detect_local_comic_format(comic_path)
        if comic_format in (1, 2):
            return comic_format
    return 2


def has_active_update_task(comic_name):
    with app.app_context():
        return DownloadTask.query.filter(
            DownloadTask.is_update.is_(True),
            DownloadTask.comic_name == comic_name,
            DownloadTask.status.in_(['pending', 'running'])
        ).first() is not None


def enqueue_auto_update_tasks(update_candidates):
    from mangadock.services.workers import start_update_task
    queued_count = 0
    for candidate in update_candidates:
        comic_name = (candidate.comic_name or '').strip()
        source_url = (candidate.source_url or '').strip()
        if not comic_name or not source_url:
            continue
        if has_active_update_task(comic_name):
            continue

        comic_format = resolve_update_format(comic_name)
        start_update_task(comic_name, comic_format, source_url)
        queued_count += 1
    return queued_count


def queue_background_command(command_type, payload=None, dedupe_pending=True):
    serialized_payload = json.dumps(payload or {}, ensure_ascii=False)
    with app.app_context():
        if dedupe_pending:
            existing_command = BackgroundCommand.query.filter(
                BackgroundCommand.command_type == command_type,
                BackgroundCommand.status.in_(['pending', 'running'])
            ).order_by(BackgroundCommand.requested_at.asc()).first()
            if existing_command:
                return existing_command

        command = BackgroundCommand(
            command_type=command_type,
            payload=serialized_payload,
            status='pending'
        )
        db.session.add(command)
        db.session.commit()
        return command


def claim_next_background_command():
    with app.app_context():
        while True:
            command = BackgroundCommand.query.filter_by(status='pending').order_by(
                BackgroundCommand.requested_at.asc(),
                BackgroundCommand.id.asc()
            ).first()
            if not command:
                return None

            now = datetime.now(china_tz)
            updated_rows = BackgroundCommand.query.filter_by(id=command.id, status='pending').update(
                {
                    'status': 'running',
                    'started_at': now,
                    'finished_at': None,
                    'updated_at': now,
                },
                synchronize_session=False
            )
            db.session.commit()
            if updated_rows:
                return command.id


def finish_background_command(command_id, status='completed', message=None):
    with app.app_context():
        command = db.session.get(BackgroundCommand, command_id)
        if not command:
            return
        command.status = status
        command.message = message[:500] if message else None
        command.finished_at = datetime.now(china_tz)
        command.updated_at = datetime.now(china_tz)
        db.session.commit()


def execute_background_command(command_id):
    with app.app_context():
        command = db.session.get(BackgroundCommand, command_id)
        if not command:
            return False
        payload = {}
        if command.payload:
            try:
                payload = json.loads(command.payload)
            except Exception:
                payload = {}
        command_type = command.command_type

    try:
        if command_type == 'refresh_update_checks':
            refresh_update_checks(force=bool(payload.get('force', True)))
            finish_background_command(command_id, status='completed', message='更新检查已完成')
            return True

        finish_background_command(command_id, status='error', message=f'未知后台命令: {command_type}')
        return False
    except Exception as exc:
        finish_background_command(command_id, status='error', message=str(exc))
        return False


def should_refresh_update_checks(comic_mapping, force=False):
    if force:
        return True

    if not comic_mapping:
        return False

    now = datetime.now(china_tz)
    existing_checks = get_update_check_map()

    for comic_name, source_url in comic_mapping.items():
        check_item = existing_checks.get(comic_name)
        if not check_item:
            return True
        if (check_item.source_url or '') != (source_url or ''):
            return True
        last_checked_at = normalize_china_datetime(check_item.last_checked_at)
        if not last_checked_at:
            return True
        if now - last_checked_at >= UPDATE_CHECK_INTERVAL:
            return True

    extra_names = set(existing_checks) - set(comic_mapping)
    if extra_names:
        return True

    return False


def refresh_update_checks(force=False, async_refresh=False):
    from mangadock.services.download import load_comic_source
    comic_mapping = load_comic_mapping()
    if not should_refresh_update_checks(comic_mapping, force=force):
        return get_update_candidates()

    with update_check_cache_lock:
        if update_check_cache_state['refreshing']:
            return get_update_candidates()
        update_check_cache_state['refreshing'] = True

    try:
        now = datetime.now(china_tz)
        with app.app_context():
            existing_checks = {
                item.comic_name: item
                for item in ComicUpdateCheck.query.all()
            }

            for comic_name, check_item in list(existing_checks.items()):
                if comic_name not in comic_mapping:
                    db.session.delete(check_item)
                    existing_checks.pop(comic_name, None)

            for comic_name, source_url in comic_mapping.items():
                check_item = existing_checks.get(comic_name)
                if not check_item:
                    check_item = ComicUpdateCheck(
                        comic_name=comic_name,
                        source_url=source_url or '',
                    )
                    db.session.add(check_item)
                    existing_checks[comic_name] = check_item

                check_item.source_url = source_url or ''
                check_item.status = 'checking'
                check_item.error_message = None
                check_item.updated_at = now
            db.session.commit()

        for comic_name, source_url in comic_mapping.items():
            pending_chapters = 0
            remote_total = 0
            local_total = 0
            latest_title = None
            status = 'ready'
            error_message = None
            has_updates = False

            try:
                source = load_comic_source(source_url)
                remote_chapters = source.get('chapters', []) or []
                remote_total = len(remote_chapters)
                local_bases = get_local_chapter_bases(comic_name)
                local_match_bases = get_local_chapter_match_bases(comic_name)
                local_total = len(local_bases)
                pending_items = [
                    chapter for chapter in remote_chapters
                    if not is_existing_local_chapter(chapter, local_match_bases)
                ]
                pending_chapters = len(pending_items)
                has_updates = pending_chapters > 0
                latest_title = pending_items[-1]['title'] if pending_items else (
                    remote_chapters[-1]['title'] if remote_chapters else None
                )
            except Exception as exc:
                status = 'error'
                error_message = str(exc)[:500]

            with app.app_context():
                check_item = ComicUpdateCheck.query.filter_by(comic_name=comic_name).first()
                if not check_item:
                    continue
                check_item.has_updates = has_updates
                check_item.pending_chapters = pending_chapters
                check_item.remote_total_chapters = remote_total
                check_item.local_total_chapters = local_total
                check_item.latest_chapter_title = latest_title
                check_item.status = status
                check_item.error_message = error_message
                check_item.last_checked_at = now
                check_item.updated_at = now
                db.session.commit()
        update_candidates = get_update_candidates()
        if get_comic_update_mode() == COMIC_UPDATE_MODE_AUTO:
            enqueue_auto_update_tasks(update_candidates)
        return update_candidates
    finally:
        with update_check_cache_lock:
            update_check_cache_state['refreshing'] = False

def is_update_check_refreshing():
    with app.app_context():
        active_command = BackgroundCommand.query.filter(
            BackgroundCommand.command_type == 'refresh_update_checks',
            BackgroundCommand.status.in_(['pending', 'running'])
        ).first()
        if active_command:
            return True
        checking_item = ComicUpdateCheck.query.filter_by(status='checking').first()
        return bool(checking_item)


def schedule_update_checks_refresh(force=False):
    queue_background_command('refresh_update_checks', {'force': bool(force)}, dedupe_pending=True)
