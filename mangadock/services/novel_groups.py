# -*- coding: utf-8 -*-
"""Novel library groups and batch membership helpers."""
from datetime import datetime

from flask import session

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import NovelGroup, NovelGroupMembership
from mangadock.services.groups import normalize_group_name
from mangadock.settings import china_tz


DEFAULT_NOVEL_GROUP = '默认分组'


def get_novel_group_filter_session_key(user_id=None):
    resolved_user_id = user_id or session.get('user_id') or 'guest'
    return f'novel_group_filter_{resolved_user_id}'


def get_saved_novel_group_filter(user_id=None, default='全部'):
    saved_value = session.get(get_novel_group_filter_session_key(user_id), default)
    return normalize_group_name(saved_value) or default


def save_novel_group_filter(group_name, user_id=None):
    session[get_novel_group_filter_session_key(user_id)] = normalize_group_name(group_name) or '全部'


def get_all_novel_groups():
    with app.app_context():
        groups = NovelGroup.query.order_by(
            db.case((NovelGroup.name == DEFAULT_NOVEL_GROUP, 0), else_=1),
            NovelGroup.created_at.asc(),
            NovelGroup.name.asc(),
        ).all()
        return [group.name for group in groups]


def ensure_novel_group(group_name):
    normalized_name = normalize_group_name(group_name)
    if not normalized_name or normalized_name == '全部':
        return None

    with app.app_context():
        group = NovelGroup.query.filter_by(name=normalized_name).first()
        if not group:
            group = NovelGroup(name=normalized_name)
            db.session.add(group)
            db.session.commit()
        return group.name


def get_novel_group_map():
    with app.app_context():
        return {
            membership.novel_id: membership.group_name or DEFAULT_NOVEL_GROUP
            for membership in NovelGroupMembership.query.all()
        }


def enrich_novels_with_groups(novels):
    group_names = get_all_novel_groups()
    if DEFAULT_NOVEL_GROUP not in group_names:
        group_names.insert(0, DEFAULT_NOVEL_GROUP)

    group_map = get_novel_group_map()
    grouped_lookup = {group_name: [] for group_name in group_names}
    enriched = []
    for novel in novels:
        item = dict(novel)
        group_name = group_map.get(item['novel_id']) or DEFAULT_NOVEL_GROUP
        item['group'] = group_name
        if group_name not in grouped_lookup:
            group_names.append(group_name)
            grouped_lookup[group_name] = []
        grouped_lookup[group_name].append(item)
        enriched.append(item)

    group_counts = {
        group_name: len(grouped_lookup.get(group_name, []))
        for group_name in group_names
    }
    return enriched, group_names, group_counts, grouped_lookup


def assign_novels_to_group(novel_ids, group_name, valid_novel_ids):
    normalized_group = normalize_group_name(group_name)
    valid_ids = set(valid_novel_ids or [])
    selected_ids = []
    seen = set()
    for novel_id in novel_ids or []:
        cleaned_id = (novel_id or '').strip()
        if not cleaned_id or cleaned_id in seen or cleaned_id not in valid_ids:
            continue
        seen.add(cleaned_id)
        selected_ids.append(cleaned_id)
        if len(selected_ids) >= 500:
            break

    if not normalized_group or not selected_ids:
        return 0

    with app.app_context():
        if not NovelGroup.query.filter_by(name=normalized_group).first():
            return 0
        existing = {
            membership.novel_id: membership
            for membership in NovelGroupMembership.query.filter(
                NovelGroupMembership.novel_id.in_(selected_ids)
            ).all()
        }
        now = datetime.now(china_tz)
        for novel_id in selected_ids:
            membership = existing.get(novel_id)
            if membership:
                membership.group_name = normalized_group
                membership.updated_at = now
            else:
                db.session.add(NovelGroupMembership(
                    novel_id=novel_id,
                    group_name=normalized_group,
                    updated_at=now,
                ))
        db.session.commit()
    return len(selected_ids)


def delete_novel_group(group_name):
    normalized_group = normalize_group_name(group_name)
    if not normalized_group or normalized_group == DEFAULT_NOVEL_GROUP:
        return False

    with app.app_context():
        group = NovelGroup.query.filter_by(name=normalized_group).first()
        if not group:
            return False
        NovelGroupMembership.query.filter_by(group_name=normalized_group).update(
            {
                'group_name': DEFAULT_NOVEL_GROUP,
                'updated_at': datetime.now(china_tz),
            },
            synchronize_session=False,
        )
        db.session.delete(group)
        db.session.commit()
    return True
