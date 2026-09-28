# -*- coding: utf-8 -*-
"""Reading progress and reading-time statistics."""
import math
from datetime import datetime

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import ReadingProgress, ReadingSessionState, ReadingTime
from mangadock.settings import china_tz
from sqlalchemy import or_, update as sa_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # 并发 upsert 用

MAX_PROGRESS_KEY_LENGTH = 255
MAX_READING_REPORT_SECONDS = 8 * 60 * 60
MAX_READING_DELTA_SECONDS = 15 * 60
MAX_CLIENT_PROGRESS_MS = 9_007_199_254_740_991  # JS Number.MAX_SAFE_INTEGER


def _client_progress_ms(value):
    """解析客户端快照时钟。缺失或无法解析时返回 None，表示沿用到达顺序。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0 or parsed > MAX_CLIENT_PROGRESS_MS:
        return None
    return parsed


def _progress_order():
    # 同一本书若短暂存在重复行，以客户端快照时钟而不是请求到达时间分先后。
    return (
        ReadingProgress.client_progress_ms.desc(),
        ReadingProgress.last_read_at.desc(),
        ReadingProgress.id.desc(),
    )


def _progress_snapshot(progress, stale):
    return {
        'chapter_index': progress.last_chapter or 0,
        'page_index': progress.last_page or 0,
        'scroll_position': progress.scroll_position or 0,
        'anchor_paragraph': progress.anchor_paragraph,
        'anchor_offset': progress.anchor_offset,
        'total_chapters': progress.total_chapters or 0,
        'total_pages': progress.total_pages or 0,
        'updated_at': _format_api_datetime(progress.last_read_at),
        'client_progress_ms': progress.client_progress_ms,
        'stale': stale,
    }


def user_can_access_progress_key(progress_key, user):
    if not user or not isinstance(progress_key, str):
        return False
    progress_key = progress_key.strip()
    if not progress_key or len(progress_key) > MAX_PROGRESS_KEY_LENGTH:
        return False

    from mangadock.services.novels import NOVEL_PROGRESS_PREFIX, get_novel
    if progress_key.startswith(NOVEL_PROGRESS_PREFIX):
        return get_novel(progress_key[len(NOVEL_PROGRESS_PREFIX):]) is not None

    from mangadock.services.groups import can_user_access_group, get_comic_group_map
    from mangadock.services.library import get_comic_directory, is_safe_comic_name

    if not is_safe_comic_name(progress_key) or not get_comic_directory(progress_key):
        return False
    group = get_comic_group_map().get(progress_key) or '默认分组'
    return can_user_access_group(group, user)


def _format_api_datetime(value):
    from mangadock.services.updates import normalize_china_datetime
    normalized = normalize_china_datetime(value)
    return normalized.isoformat() if normalized else None


def note_server_progress_write(progress):
    """服务端主动改写进度时把时钟推到现在，避免更早的阅读器快照再盖回来。"""
    now_ms = int(datetime.now(china_tz).timestamp() * 1000)
    if now_ms > MAX_CLIENT_PROGRESS_MS:
        now_ms = MAX_CLIENT_PROGRESS_MS
    if progress.client_progress_ms is None or now_ms > progress.client_progress_ms:
        progress.client_progress_ms = now_ms


def get_reading_progress(comic_name, user_id):
    """获取漫画阅读进度"""
    with app.app_context():
        return ReadingProgress.query.filter_by(
            comic_name=comic_name,
            user_id=user_id
        ).order_by(*_progress_order()).first()

def save_reading_progress(
    comic_name, chapter, page, scroll_position, total_chapters, total_pages, user_id,
    anchor_paragraph=None, anchor_offset=None, client_progress_ms=None,
):
    """保存阅读进度。

    client_progress_ms 是客户端生成这份快照时的时钟。更小的值视为迟到请求，
    不覆盖已经记下的进度；用户主动翻回旧章节时，新快照会带上更大的时钟。
    已经有时钟的进度，也不会被没带时钟的旧请求覆盖。双方都没有时钟时才按到达顺序覆盖。
    """
    with app.app_context():
        now = datetime.now(china_tz)
        incoming_ms = _client_progress_ms(client_progress_ms)
        progress_records = ReadingProgress.query.filter_by(
            comic_name=comic_name,
            user_id=user_id
        ).order_by(*_progress_order()).all()

        progress = progress_records[0] if progress_records else None
        stored_ms = progress.client_progress_ms if progress is not None else None
        if stored_ms is not None and (incoming_ms is None or incoming_ms < stored_ms):
            return _progress_snapshot(progress, stale=True)

        for duplicate_progress in progress_records[1:]:
            db.session.delete(duplicate_progress)

        values = {
            'last_chapter': chapter,
            'last_page': page,
            'scroll_position': scroll_position,
            'total_chapters': total_chapters,
            'total_pages': total_pages,
            'last_read_at': now,
        }
        if anchor_paragraph is not None and anchor_offset is not None:
            values['anchor_paragraph'] = anchor_paragraph
            values['anchor_offset'] = anchor_offset
        if incoming_ms is not None:
            values['client_progress_ms'] = incoming_ms

        if progress is None:
            progress = ReadingProgress(user_id=user_id, comic_name=comic_name, **values)
            db.session.add(progress)
            db.session.commit()
            return _progress_snapshot(progress, stale=False)

        # 条件更新避免两个在途请求都读到旧时钟后，迟到的那个把新进度盖掉。
        stmt = sa_update(ReadingProgress).where(ReadingProgress.id == progress.id)
        if incoming_ms is not None:
            stmt = stmt.where(or_(
                ReadingProgress.client_progress_ms.is_(None),
                ReadingProgress.client_progress_ms <= incoming_ms,
            ))
        else:
            stmt = stmt.where(ReadingProgress.client_progress_ms.is_(None))
        result = db.session.execute(stmt.values(**values))
        if result.rowcount == 0:
            db.session.rollback()
            current = ReadingProgress.query.filter_by(
                comic_name=comic_name,
                user_id=user_id,
            ).order_by(*_progress_order()).first()
            if current is not None:
                return _progress_snapshot(current, stale=True)
            progress = ReadingProgress(user_id=user_id, comic_name=comic_name, **values)
            db.session.add(progress)
            db.session.commit()
            return _progress_snapshot(progress, stale=False)

        db.session.commit()
        db.session.refresh(progress)
        return _progress_snapshot(progress, stale=False)

def get_all_reading_progress(user_id):
    """获取所有阅读进度"""
    with app.app_context():
        progress_rows = ReadingProgress.query.filter_by(user_id=user_id).order_by(
            *_progress_order()
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
        normalized_seconds = min(normalized_seconds, MAX_READING_REPORT_SECONDS)
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
                # code review P1：并发两请求同读 None 再各 insert 会撞 UNIQUE 约束
                # (user_id, comic_name, session_key)，原 flush() 触发 IntegrityError 被
                # 外层 except 吞成 400，进度静默丢失。改用 upsert（INSERT … ON CONFLICT
                # DO NOTHING）保证该行存在，再由下方统一 re-query 取回状态；do_nothing 会
                # 保留并发方已写入的 baseline，不会互相覆盖丢进度。delta 计算依赖读回的值，
                # 故此处只用 do_nothing 建行、不在此更新业务字段。
                now = datetime.now(china_tz)
                insert_stmt = sqlite_insert(ReadingSessionState).values(
                    user_id=user_id,
                    comic_name=comic_name,
                    session_key=session_key,
                    last_reported_seconds=0,
                    created_at=now,
                    updated_at=now,
                ).on_conflict_do_nothing(
                    index_elements=['user_id', 'comic_name', 'session_key']
                )
                db.session.execute(insert_stmt)
                db.session.flush()

                session_state = ReadingSessionState.query.filter_by(
                    user_id=user_id,
                    comic_name=comic_name,
                    session_key=session_key
                ).first()
                if not session_state:
                    # 极端兜底（如并发删除该行）：显式 ORM 插入一次
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

            # 2026-09-20 code review P2：单次上报跨度可能超过 MAX_READING_DELTA_SECONDS
            # （客户端长时间挂后台后一次性上报累计值）。基线只能前进「实际入库的秒数」；
            # 若直接把 last_reported_seconds 设为 normalized_seconds，被 cap 掉的部分会
            # 永久丢失——因为之后上报的累计值小于基线，delta <= 0 会直接 return None。
            counted_seconds = min(delta_seconds, MAX_READING_DELTA_SECONDS)
            session_state.last_reported_seconds = (
                max(session_state.last_reported_seconds or 0, 0) + counted_seconds
            )
            session_state.updated_at = datetime.now(china_tz)
            delta_seconds = counted_seconds

        # 无 session_key 时没有去重基线，只能按「单次增量」记账并套用同一上限
        delta_seconds = min(delta_seconds, MAX_READING_DELTA_SECONDS)
        if delta_seconds <= 0:
            db.session.commit()
            return None

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
            ReadingTime.user_id == user_id,
            ~ReadingTime.comic_name.like('drama:%')
        ).scalar()
        return display_minutes_from_seconds(total_seconds)


def get_reading_time_by_comic(user_id):
    """按漫画分组获取阅读时间"""
    with app.app_context():
        result = db.session.query(
            ReadingTime.comic_name,
            db.func.sum(reading_time_seconds_expression()).label('total_duration_seconds')
        ).filter(
            ReadingTime.user_id == user_id,
            ~ReadingTime.comic_name.like('drama:%')
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
            ~ReadingTime.comic_name.like('drama:%'),
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
            ~ReadingTime.comic_name.like('drama:%'),
            db.func.strftime('%Y', ReadingTime.read_at) == f"{year:04d}"
        ).group_by('day').order_by('day').all()
        return [
            (row.day, display_minutes_from_seconds(row.total_duration_seconds))
            for row in result
        ]
