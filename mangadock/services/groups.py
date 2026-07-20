import re
# -*- coding: utf-8 -*-
"""Comic groups, permissions, and admin-hidden library items."""
from datetime import datetime

from flask import session

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import (
    AdminHiddenLibraryItem,
    ComicGroup,
    ComicGroupMembership,
    DownloadTask,
    UserGroupPermission,
)
from mangadock.settings import china_tz

def normalize_group_name(group_name):
    cleaned = re.sub(r'\s+', ' ', (group_name or '').strip())
    cleaned = re.sub(r'[\x00-\x1f\x7f]', '', cleaned)
    return cleaned[:50]


def get_group_filter_session_key(user_id=None):
    resolved_user_id = user_id or session.get('user_id') or 'guest'
    return f'comic_group_filter_{resolved_user_id}'


def get_saved_group_filter(user_id=None, default='全部'):
    saved_value = session.get(get_group_filter_session_key(user_id), default)
    return normalize_group_name(saved_value) or default


def save_group_filter(group_name, user_id=None):
    session[get_group_filter_session_key(user_id)] = normalize_group_name(group_name) or '全部'


def get_all_comic_groups():
    with app.app_context():
        groups = ComicGroup.query.order_by(
            db.case((ComicGroup.name == '默认分组', 0), else_=1),
            ComicGroup.created_at.asc(),
            ComicGroup.name.asc()
        ).all()
        return [group.name for group in groups]


def get_user_group_permissions(user_id):
    if not user_id:
        return []

    with app.app_context():
        return [
            permission.group_name
            for permission in UserGroupPermission.query.filter_by(user_id=user_id)
            .order_by(UserGroupPermission.group_name.asc())
            .all()
        ]


def get_accessible_group_names(user=None):
    from mangadock.auth import get_current_user
    target_user = user or get_current_user()
    all_groups = get_all_comic_groups()

    if target_user and target_user.is_admin:
        return all_groups

    allowed_groups = {
        normalize_group_name(group_name)
        for group_name in get_user_group_permissions(target_user.id if target_user else None)
    }
    return [group_name for group_name in all_groups if group_name in allowed_groups]


def can_user_access_group(group_name, user=None):
    from mangadock.auth import get_current_user
    normalized_group = normalize_group_name(group_name) or '默认分组'
    target_user = user or get_current_user()

    if target_user and target_user.is_admin:
        return True

    return normalized_group in set(get_accessible_group_names(target_user))


def set_user_group_permissions(user_id, group_names):
    if not user_id:
        return []

    normalized_group_names = []
    seen_groups = set()
    valid_groups = set(get_all_comic_groups())

    for group_name in group_names or []:
        normalized_group = normalize_group_name(group_name)
        if not normalized_group or normalized_group in seen_groups or normalized_group not in valid_groups:
            continue
        seen_groups.add(normalized_group)
        normalized_group_names.append(normalized_group)

    with app.app_context():
        UserGroupPermission.query.filter_by(user_id=user_id).delete(synchronize_session=False)
        for group_name in normalized_group_names:
            db.session.add(UserGroupPermission(user_id=user_id, group_name=group_name))
        db.session.commit()

    return normalized_group_names


def ensure_comic_group(group_name):
    normalized_name = normalize_group_name(group_name)
    if not normalized_name:
        return None

    with app.app_context():
        group = ComicGroup.query.filter_by(name=normalized_name).first()
        if not group:
            group = ComicGroup(name=normalized_name)
            db.session.add(group)
            db.session.commit()
        return group.name


def get_comic_group_map():
    with app.app_context():
        memberships = ComicGroupMembership.query.all()
        return {
            membership.comic_name: membership.group_name or '默认分组'
            for membership in memberships
        }


def normalize_hidden_target(target_type, target_value):
    if target_type == 'group':
        return normalize_group_name(target_value) or None
    if target_type == 'comic':
        cleaned = (target_value or '').strip()
        return cleaned[:255] if cleaned else None
    return None


def get_admin_hidden_targets(user=None):
    from mangadock.auth import get_current_user
    target_user = user or get_current_user()
    if not target_user or not target_user.is_admin:
        return set(), set()

    items = AdminHiddenLibraryItem.query.filter_by(user_id=target_user.id).all()
    hidden_comics = {
        item.target_value
        for item in items
        if item.target_type == 'comic' and item.target_value
    }
    hidden_groups = {
        item.target_value
        for item in items
        if item.target_type == 'group' and item.target_value
    }
    return hidden_comics, hidden_groups


def is_comic_hidden_for_admin(comic, hidden_comics, hidden_groups):
    comic_name = (comic.get('comic_name') or '').strip()
    group_name = normalize_group_name(comic.get('group')) or '默认分组'
    return comic_name in hidden_comics or group_name in hidden_groups


def set_admin_hidden_item(user_id, target_type, target_value, hidden):
    normalized_value = normalize_hidden_target(target_type, target_value)
    if not user_id or target_type not in {'comic', 'group'} or not normalized_value:
        return False

    existing_item = AdminHiddenLibraryItem.query.filter_by(
        user_id=user_id,
        target_type=target_type,
        target_value=normalized_value
    ).first()

    if hidden:
        if not existing_item:
            db.session.add(AdminHiddenLibraryItem(
                user_id=user_id,
                target_type=target_type,
                target_value=normalized_value
            ))
            db.session.commit()
        return True

    if existing_item:
        db.session.delete(existing_item)
        db.session.commit()
    return True


def assign_comic_group(comic_name, group_name):
    normalized_comic_name = (comic_name or '').strip()
    normalized_group_name = ensure_comic_group(group_name)

    if not normalized_comic_name or not normalized_group_name:
        return False

    with app.app_context():
        membership = ComicGroupMembership.query.filter_by(comic_name=normalized_comic_name).first()
        if membership:
            membership.group_name = normalized_group_name
            membership.updated_at = datetime.now(china_tz)
        else:
            membership = ComicGroupMembership(
                comic_name=normalized_comic_name,
                group_name=normalized_group_name,
                updated_at=datetime.now(china_tz)
            )
            db.session.add(membership)

        DownloadTask.query.filter_by(comic_name=normalized_comic_name).update(
            {'group': normalized_group_name},
            synchronize_session=False
        )
        db.session.commit()

    return True


def delete_comic_group(group_name):
    normalized_group_name = normalize_group_name(group_name)
    if not normalized_group_name or normalized_group_name == '默认分组':
        return False

    with app.app_context():
        group = ComicGroup.query.filter_by(name=normalized_group_name).first()
        if not group:
            return False

        ComicGroupMembership.query.filter_by(group_name=normalized_group_name).update(
            {
                'group_name': '默认分组',
                'updated_at': datetime.now(china_tz)
            },
            synchronize_session=False
        )
        DownloadTask.query.filter_by(group=normalized_group_name).update(
            {'group': '默认分组'},
            synchronize_session=False
        )
        UserGroupPermission.query.filter_by(group_name=normalized_group_name).delete(synchronize_session=False)
        AdminHiddenLibraryItem.query.filter_by(
            target_type='group',
            target_value=normalized_group_name
        ).delete(synchronize_session=False)
        db.session.delete(group)
        db.session.commit()

    return True

def enrich_comics_with_groups(comics):
    comic_group_map = get_comic_group_map()
    group_names = get_all_comic_groups()
    if '默认分组' not in group_names:
        group_names.insert(0, '默认分组')

    normalized_comics = []
    grouped_lookup = {group_name: [] for group_name in group_names}

    for comic in comics:
        comic_copy = dict(comic)
        comic_group = comic_group_map.get(comic_copy['comic_name']) or comic_copy.get('group') or '默认分组'
        comic_copy['group'] = comic_group
        if comic_group not in group_names:
            group_names.append(comic_group)
            grouped_lookup[comic_group] = []
        grouped_lookup.setdefault(comic_group, []).append(comic_copy)
        normalized_comics.append(comic_copy)

    group_counts = {
        group_name: len(grouped_lookup.get(group_name, []))
        for group_name in group_names
    }

    return normalized_comics, group_names, group_counts, grouped_lookup


def filter_grouped_comics_for_user(comics, user=None):
    from mangadock.auth import get_current_user
    target_user = user or get_current_user()
    normalized_comics, group_names, group_counts, grouped_lookup = enrich_comics_with_groups(comics)

    if target_user and target_user.is_admin:
        return normalized_comics, group_names, group_counts, grouped_lookup

    allowed_group_names = set(get_accessible_group_names(target_user))
    filtered_group_names = [group_name for group_name in group_names if group_name in allowed_group_names]
    filtered_lookup = {
        group_name: grouped_lookup.get(group_name, [])
        for group_name in filtered_group_names
    }
    filtered_comics = [
        comic for comic in normalized_comics
        if comic.get('group') in allowed_group_names
    ]
    filtered_counts = {
        group_name: len(filtered_lookup.get(group_name, []))
        for group_name in filtered_group_names
    }

    return filtered_comics, filtered_group_names, filtered_counts, filtered_lookup

