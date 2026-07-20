# -*- coding: utf-8 -*-
"""Reading progress and reading-time statistics."""
import math
from datetime import datetime

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import ReadingProgress, ReadingSessionState, ReadingTime
from mangadock.settings import china_tz


def _format_api_datetime(value):
    from mangadock.services.updates import normalize_china_datetime
    normalized = normalize_china_datetime(value)
    return normalized.isoformat() if normalized else None


def get_reading_progress(comic_name, user_id):
    """获取漫画阅读进度"""
    with app.app_context():
        return ReadingProgress.query.filter_by(
            comic_name=comic_name,
            user_id=user_id
        ).order_by(
            ReadingProgress.last_read_at.desc(),
            ReadingProgress.id.desc()
        ).first()

def save_reading_progress(comic_name, chapter, page, scroll_position, total_chapters, total_pages, user_id):
    """保存阅读进度 - 修复��数顺序"""
    with app.app_context():
        now = datetime.now(china_tz)
        progress_records = ReadingProgress.query.filter_by(
            comic_name=comic_name,
            user_id=user_id
        ).order_by(
            ReadingProgress.last_read_at.desc(),
            ReadingProgress.id.desc()
        ).all()

        progress = progress_records[0] if progress_records else None
        if progress:
            progress.last_chapter = chapter
            progress.last_page = page
            progress.scroll_position = scroll_position
            progress.total_chapters = total_chapters
            progress.total_pages = total_pages
            progress.last_read_at = now

            for duplicate_progress in progress_records[1:]:
                db.session.delete(duplicate_progress)
        else:
            progress = ReadingProgress(
                user_id=user_id,
                comic_name=comic_name,
                last_chapter=chapter,
                last_page=page,
                scroll_position=scroll_position,
                total_chapters=total_chapters,
                total_pages=total_pages,
                last_read_at=now
            )
            db.session.add(progress)
        db.session.commit()
        return {
            'chapter_index': chapter,
            'page_index': page,
            'scroll_position': scroll_position,
            'total_chapters': total_chapters,
            'total_pages': total_pages,
            'updated_at': _format_api_datetime(now),
        }

def get_all_reading_progress(user_id):
    """获取所有阅读进度"""
    with app.app_context():
        progress_rows = ReadingProgress.query.filter_by(user_id=user_id).order_by(
            ReadingProgress.last_read_at.desc(),
            ReadingProgress.id.desc()
        ).all()

        latest_progress_by_comic = {}
        duplicate_rows = []
        for row in progress_rows:
            if row.comic_name in latest_progress_by_comic:
                duplicate_rows.append(row)
                continue
            latest_progress_by_comic[row.comic_name] = row

        if duplicate_rows:
            for duplicate_row in duplicate_rows:
                db.session.delete(duplicate_row)
            db.session.commit()

        return list(latest_progress_by_comic.values())


def display_minutes_from_seconds(total_seconds):
    safe_seconds = max(int(total_seconds or 0), 0)
    if safe_seconds <= 0:
        return 0
    return math.ceil(safe_seconds / 60)


def reading_time_seconds_expression():
    return db.func.coalesce(ReadingTime.duration_seconds, ReadingTime.duration * 60)


def record_reading_time(comic_name, duration_seconds, user_id, session_key=None):
    """按阅读会话累计秒数记录阅读时间，服务端去重避免重复记时。"""
    with app.app_context():
        normalized_seconds = max(int(duration_seconds or 0), 0)
        if not comic_name or not user_id or normalized_seconds <= 0:
            return None

        delta_seconds = normalized_seconds
        if session_key:
            session_state = ReadingSessionState.query.filter_by(
                user_id=user_id,
                comic_name=comic_name,
                session_key=session_key
            ).first()

            if not session_state:
                session_state = ReadingSessionState(
                    user_id=user_id,
                    comic_name=comic_name,
                    session_key=session_key,
                    last_reported_seconds=0
                )
                db.session.add(session_state)
                db.session.flush()

            delta_seconds = normalized_seconds - max(session_state.last_reported_seconds or 0, 0)
            if delta_seconds <= 0:
                session_state.updated_at = datetime.now(china_tz)
                db.session.commit()
                return None

            session_state.last_reported_seconds = normalized_seconds
            session_state.updated_at = datetime.now(china_tz)

        reading_time = ReadingTime(
            user_id=user_id,
            comic_name=comic_name,
            duration=display_minutes_from_seconds(delta_seconds),
            duration_seconds=delta_seconds,
            read_at=datetime.now(china_tz)
        )
        db.session.add(reading_time)
        db.session.commit()
        return reading_time


def get_total_reading_time(user_id):
    """获取总阅读时间（分钟）"""
    with app.app_context():
        total_seconds = db.session.query(db.func.sum(reading_time_seconds_expression())).filter(
            ReadingTime.user_id == user_id
        ).scalar()
        return display_minutes_from_seconds(total_seconds)


def get_reading_time_by_comic(user_id):
    """按漫画分组获取阅读时间"""
    with app.app_context():
        result = db.session.query(
            ReadingTime.comic_name,
            db.func.sum(reading_time_seconds_expression()).label('total_duration_seconds')
        ).filter(
            ReadingTime.user_id == user_id
        ).group_by(ReadingTime.comic_name).order_by(db.desc('total_duration_seconds')).all()
        return [
            {
                'comic_name': row.comic_name,
                'total_duration': display_minutes_from_seconds(row.total_duration_seconds)
            }
            for row in result
        ]


def get_reading_time_for_comic(comic_name, user_id):
    """获取单个漫画的总阅读时间（分钟）"""
    with app.app_context():
        total_seconds = db.session.query(db.func.sum(reading_time_seconds_expression())).filter(
            ReadingTime.comic_name == comic_name,
            ReadingTime.user_id == user_id
        ).scalar()
        return display_minutes_from_seconds(total_seconds)


def get_reading_time_monthly_for_year(year, user_id):
    """获取指定年份的月度阅读时间"""
    with app.app_context():
        result = db.session.query(
            db.func.strftime('%Y-%m', ReadingTime.read_at).label('month'),
            db.func.sum(reading_time_seconds_expression()).label('total_duration_seconds')
        ).filter(
            ReadingTime.user_id == user_id,
            db.func.strftime('%Y', ReadingTime.read_at) == f"{year:04d}"
        ).group_by('month').order_by('month').all()
        return [
            (row.month, display_minutes_from_seconds(row.total_duration_seconds))
            for row in result
        ]


def get_reading_time_daily_for_year(year, user_id):
    """获取指定年份按天聚合的阅读时间（分钟）"""
    with app.app_context():
        result = db.session.query(
            db.func.strftime('%Y-%m-%d', ReadingTime.read_at).label('day'),
            db.func.sum(reading_time_seconds_expression()).label('total_duration_seconds')
        ).filter(
            ReadingTime.user_id == user_id,
            db.func.strftime('%Y', ReadingTime.read_at) == f"{year:04d}"
        ).group_by('day').order_by('day').all()
        return [
            (row.day, display_minutes_from_seconds(row.total_duration_seconds))
            for row in result
        ]
