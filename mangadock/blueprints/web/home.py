# -*- coding: utf-8 -*-
"""首页 / 搜索 / 历史。"""
from flask import (
    render_template,
    request,
    session,
)

from mangadock.auth import get_current_user, login_required
from mangadock.core import app
from mangadock.services.download import refresh_comic_description
from mangadock.services.groups import (
    filter_grouped_comics_for_user,
    get_admin_hidden_targets,
    is_comic_hidden_for_admin,
)
from mangadock.services.library import (
    get_available_comics,
    get_comic_descriptions,
)
from mangadock.services.reading import get_all_reading_progress


def _build_home_spotlight(user_id):
    """Pick continue-reading comic for homepage hero."""
    current_user = get_current_user()
    comics = get_available_comics() or []
    # Apply membership groups + per-user group permissions (bookshelf already does this).
    comics, _, _, _ = filter_grouped_comics_for_user(comics, current_user)

    # Homepage always hides admin-hidden comics/groups (no show_hidden toggle here).
    hidden_comics, hidden_groups = get_admin_hidden_targets(current_user)
    comics = [
        comic for comic in comics
        if not is_comic_hidden_for_admin(comic, hidden_comics, hidden_groups)
    ]
    comics, group_names, _group_counts, grouped_lookup = filter_grouped_comics_for_user(
        comics, current_user
    )
    if current_user and current_user.is_admin and hidden_groups:
        group_names = [name for name in group_names if name not in hidden_groups]
        for name in list(grouped_lookup.keys()):
            if name in hidden_groups:
                grouped_lookup.pop(name, None)

    comic_lookup = {c.get('comic_name'): c for c in comics if c.get('comic_name')}
    progresses = get_all_reading_progress(user_id) or []

    spotlight = None
    for progress in progresses:
        comic = comic_lookup.get(progress.comic_name)
        if not comic:
            continue
        spotlight = {
            'comic_name': progress.comic_name,
            'comic_format': comic.get('comic_format'),
            'group': comic.get('group') or '默认分组',
            'available_chapters': comic.get('available_chapters') or comic.get('total_chapters') or 0,
            'last_chapter': progress.last_chapter or 0,
            'last_page': progress.last_page or 0,
            'last_read_at': progress.last_read_at,
            'has_progress': True,
        }
        break

    if not spotlight and comics:
        # Fallback: most recently created / first in library list
        sorted_comics = sorted(
            comics,
            key=lambda c: c['created_at'].timestamp() if c.get('created_at') else float('-inf'),
            reverse=True,
        )
        comic = sorted_comics[0]
        spotlight = {
            'comic_name': comic.get('comic_name'),
            'comic_format': comic.get('comic_format'),
            'group': comic.get('group') or '默认分组',
            'available_chapters': comic.get('available_chapters') or comic.get('total_chapters') or 0,
            'last_chapter': 0,
            'last_page': 0,
            'last_read_at': None,
            'has_progress': False,
        }

    # 批量注入简介；首页焦点位缺简介时尝试从源站补全一次
    desc_names = set()
    if spotlight and spotlight.get('comic_name'):
        desc_names.add(spotlight['comic_name'])
    for progress in progresses[:12]:
        if progress.comic_name:
            desc_names.add(progress.comic_name)
    descriptions = get_comic_descriptions(desc_names)

    if spotlight:
        name = spotlight.get('comic_name')
        description = descriptions.get(name) or ''
        if not description and name:
            description = refresh_comic_description(name) or ''
            if description:
                descriptions[name] = description
        spotlight['description'] = description

    recent = []
    for progress in progresses[:12]:
        comic = comic_lookup.get(progress.comic_name)
        if comic:
            recent.append({
                **comic,
                'last_chapter': progress.last_chapter or 0,
                'last_read_at': progress.last_read_at,
                'description': descriptions.get(progress.comic_name, ''),
            })

    # Fresh library items not in recent
    recent_names = {item.get('comic_name') for item in recent}
    latest = [c for c in comics if c.get('comic_name') not in recent_names][:12]

    # Prefer ComicGroup order so rails match bookshelf grouping.
    group_rails = [
        {'name': name, 'comics': (grouped_lookup.get(name) or [])[:10]}
        for name in group_names
        if grouped_lookup.get(name)
    ][:6]

    return spotlight, recent, latest, group_rails


@app.route('/')
@login_required
def index():
    current_user = get_current_user()
    user_id = session.get('user_id')
    spotlight, recent, latest, group_rails = _build_home_spotlight(user_id)
    return render_template(
        'index.html',
        spotlight=spotlight,
        recent_comics=recent,
        latest_comics=latest,
        group_rails=group_rails,
        current_user=current_user,
    )


@app.route('/search')
@login_required
def search_page():
    """独立搜索结果页：按标题筛选本地书库，不与书架混排。"""
    current_user = get_current_user()
    query = (request.args.get('q') or '').strip()
    comics = get_available_comics() or []
    comics, _group_names, _counts, _lookup = filter_grouped_comics_for_user(comics, current_user)

    hidden_comics, hidden_groups = get_admin_hidden_targets(current_user)
    comics = [
        comic for comic in comics
        if not is_comic_hidden_for_admin(comic, hidden_comics, hidden_groups)
    ]

    results = []
    if query:
        lowered = query.casefold()
        results = [
            comic for comic in comics
            if lowered in (comic.get('comic_name') or '').casefold()
        ]
        results.sort(key=lambda c: (c.get('comic_name') or ''))

    return render_template(
        'search.html',
        query=query,
        comics=results,
        result_count=len(results),
        current_user=current_user,
    )


@app.route('/history')
@login_required
def history_page():
    user_id = session.get('user_id')
    current_user = get_current_user()
    comics = get_available_comics() or []
    # 与书架一致：按分组归属 + 用户分组权限过滤，防止已撤权漫画在历史页泄露标题/封面（2026-09-20 P2）
    comics, _, _, _ = filter_grouped_comics_for_user(comics, current_user)
    comic_lookup = {c.get('comic_name'): c for c in comics}
    progresses = get_all_reading_progress(user_id) or []
    items = []
    for progress in progresses:
        comic = comic_lookup.get(progress.comic_name)
        if not comic:
            continue
        items.append({
            **comic,
            'last_chapter': progress.last_chapter or 0,
            'last_page': progress.last_page or 0,
            'last_read_at': progress.last_read_at,
        })
    return render_template('history.html', items=items)
