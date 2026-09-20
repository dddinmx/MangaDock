# -*- coding: utf-8 -*-
"""书库页（书架 / 分组管理 / 管理员隐藏区）。"""
from flask import (
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from mangadock.auth import admin_required, get_current_user, login_required
from mangadock.core import app
from mangadock.pagination import paginate_sequence
from mangadock.services.groups import (
    assign_comic_group,
    delete_comic_group,
    ensure_comic_group,
    filter_grouped_comics_for_user,
    get_admin_hidden_targets,
    get_all_comic_groups,
    get_comic_group_map,
    get_saved_group_filter,
    is_comic_hidden_for_admin,
    normalize_group_name,
    save_group_filter,
    set_admin_hidden_item,
)
from mangadock.services.library import (
    get_available_comics,
    get_comic_directory,
)
from mangadock.services.reading import get_all_reading_progress


@app.route('/comics')
@login_required
def comics_list():
    """显示漫画库页面"""
    current_user = get_current_user()
    comics = get_available_comics()
    progresses = {}
    current_user_id = session.get('user_id')
    can_show_hidden_library = bool(current_user and current_user.is_admin)
    show_hidden_library = can_show_hidden_library and request.args.get('show_hidden') == '1'
    requested_group = request.args.get('group')
    if requested_group is None:
        group_filter = get_saved_group_filter(current_user_id)
    else:
        group_filter = normalize_group_name(requested_group) or '全部'

    # 获取所有阅读进度
    all_progress = get_all_reading_progress(current_user_id)
    for progress in all_progress:
        progresses[progress.comic_name] = {
            'last_chapter': progress.last_chapter,
            'last_page': progress.last_page,
            'total_chapters': progress.total_chapters,
            'total_pages': progress.total_pages,
            'last_read_at': progress.last_read_at
        }

    # 对漫画列表进行排序：有阅读进度的漫画优先显示，并且按最后阅读时间倒序排列
    # 没有阅读进度的漫画放在后面，按创建时间倒序排列
    def sort_key(comic):
        # 先判断是否有阅读进度
        if comic['comic_name'] in progresses:
            # 有阅读进度的漫画，按最后阅读时间倒序（最新阅读的在前）
            last_read_at = progresses[comic['comic_name']]['last_read_at']
            # 如果没有 last_read_at，使用创建时间
            if last_read_at:
                # 为了在升序排序中让最新的在前，我们使用负的时间戳
                return (0, -last_read_at.timestamp())
            elif comic['created_at']:
                return (0, -comic['created_at'].timestamp())
            else:
                return (0, float('-inf'))
        else:
            # 没有阅读进度的漫画，按创建时间倒序
            if comic['created_at']:
                return (1, -comic['created_at'].timestamp())
            else:
                return (1, float('-inf'))

    # 使用升序排序
    sorted_comics = sorted(comics, key=sort_key)

    sorted_comics, _, _, _ = filter_grouped_comics_for_user(sorted_comics, current_user)
    hidden_comic_names, hidden_group_names = get_admin_hidden_targets(current_user)
    hidden_library_count = 0
    for comic in sorted_comics:
        comic['is_admin_hidden'] = can_show_hidden_library and is_comic_hidden_for_admin(
            comic,
            hidden_comic_names,
            hidden_group_names
        )
        if comic['is_admin_hidden']:
            hidden_library_count += 1

    display_comics = sorted_comics if show_hidden_library else [
        comic for comic in sorted_comics if not comic.get('is_admin_hidden')
    ]
    display_comics, group_names, group_counts, grouped_lookup = filter_grouped_comics_for_user(display_comics, current_user)

    if can_show_hidden_library and not show_hidden_library and hidden_group_names:
        group_names = [group_name for group_name in group_names if group_name not in hidden_group_names]
        for group_name in list(group_counts.keys()):
            if group_name in hidden_group_names:
                group_counts.pop(group_name, None)
        for group_name in list(grouped_lookup.keys()):
            if group_name in hidden_group_names:
                grouped_lookup.pop(group_name, None)

    ordered_group_names = []
    seen_group_names = set()
    for comic in display_comics:
        comic_group = normalize_group_name(comic.get('group')) or '默认分组'
        if comic_group in group_names and comic_group not in seen_group_names:
            ordered_group_names.append(comic_group)
            seen_group_names.add(comic_group)

    for group_name in group_names:
        if group_name not in seen_group_names:
            ordered_group_names.append(group_name)

    group_names = ordered_group_names

    if group_filter != '全部' and group_filter not in grouped_lookup:
        group_filter = '全部'

    save_group_filter(group_filter, current_user_id)

    grouped_tasks = []
    if group_filter == '全部':
        selected_comics = display_comics
        page_comics = display_comics
        pagination = None
        for group_name in group_names:
            comics_in_group = grouped_lookup.get(group_name, [])[:10]
            if comics_in_group:
                grouped_tasks.append({
                    'name': group_name,
                    'tasks': comics_in_group
                })
    else:
        selected_comics = grouped_lookup.get(group_filter, [])
        page_comics, pagination = paginate_sequence(selected_comics, request.args.get('page'))
        grouped_tasks.append({
            'name': group_filter,
            'tasks': page_comics
        })

    recent_comic = None
    recent_progress = None
    for comic in display_comics:
        progress_info = progresses.get(comic['comic_name'])
        if progress_info and progress_info.get('last_read_at'):
            recent_comic = comic
            recent_progress = progress_info
            break

    return render_template(
        'comics.html',
        tasks=page_comics,
        grouped_tasks=grouped_tasks,
        recent_comic=recent_comic,
        recent_progress=recent_progress,
        progresses=progresses,
        groups=group_names,
        group_counts=group_counts,
        group_total_count=len(display_comics),
        selected_group_count=len(selected_comics),
        pagination=pagination,
        selected_group=group_filter,
        library_return_url=request.full_path.rstrip('?'),
        show_group_menu=bool(group_names),
        group_menu_return_to='comics',
        show_hidden_library=show_hidden_library,
        hidden_library_count=hidden_library_count,
        hidden_comic_names=hidden_comic_names,
        hidden_group_names=hidden_group_names
    )


def redirect_after_hidden_library_change(return_to, redirect_group='全部', task_id=None, show_hidden=False):
    if return_to == 'detail' and task_id:
        return redirect(url_for('comic_detail', task_id=task_id))

    route_kwargs = {}
    normalized_group = normalize_group_name(redirect_group) or '全部'
    if normalized_group:
        route_kwargs['group'] = normalized_group
    if show_hidden:
        route_kwargs['show_hidden'] = '1'
    return redirect(url_for('comics_list', **route_kwargs))


@app.route('/admin_hidden/toggle_comic', methods=['POST'])
@login_required
@admin_required
def toggle_admin_hidden_comic():
    current_user = get_current_user()
    comic_name = (request.form.get('comic_name') or '').strip()
    action = request.form.get('action')
    return_to = request.form.get('return_to')
    redirect_group = normalize_group_name(request.form.get('redirect_group', '全部')) or '全部'
    return_task_id = (request.form.get('task_id') or comic_name).strip()
    show_hidden = request.form.get('show_hidden') == '1'

    if not comic_name or not get_comic_directory(comic_name):
        flash('漫画不存在，无法更新隐藏状态')
        return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)

    if action not in {'hide', 'show'}:
        flash('隐藏操作无效')
        return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)

    set_admin_hidden_item(current_user.id, 'comic', comic_name, action == 'hide')
    flash(f'《{comic_name}》已{"加入隐藏区域" if action == "hide" else "移出隐藏区域"}')
    return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)


@app.route('/admin_hidden/toggle_group', methods=['POST'])
@login_required
@admin_required
def toggle_admin_hidden_group():
    current_user = get_current_user()
    group_name = normalize_group_name(request.form.get('group_name'))
    action = request.form.get('action')
    return_to = request.form.get('return_to')
    redirect_group = normalize_group_name(request.form.get('redirect_group', '全部')) or '全部'
    return_task_id = (request.form.get('task_id') or '').strip()
    show_hidden = request.form.get('show_hidden') == '1'

    if not group_name or group_name not in set(get_all_comic_groups()):
        flash('分组不存在，无法更新隐藏状态')
        return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)

    if action not in {'hide', 'show'}:
        flash('隐藏操作无效')
        return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)

    set_admin_hidden_item(current_user.id, 'group', group_name, action == 'hide')
    flash(f'分组「{group_name}」已{"加入隐藏区域" if action == "hide" else "移出隐藏区域"}')
    return redirect_after_hidden_library_change(return_to, redirect_group, return_task_id, show_hidden)


@app.route('/comic_groups', methods=['POST'])
@login_required
@admin_required
def create_comic_group_route():
    group_name = normalize_group_name(request.form.get('group_name'))
    redirect_group = normalize_group_name(request.form.get('redirect_group', '全部')) or '全部'
    return_to = request.form.get('return_to')
    return_task_id = (request.form.get('task_id') or '').strip()

    if not group_name:
        flash('分组名称不能为空')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    if group_name == '全部':
        flash('“全部”是系统筛选项，不能作为分组名称')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    existing_groups = set(get_all_comic_groups())
    created_group = ensure_comic_group(group_name)

    if not created_group:
        flash('分组名称无效')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    if created_group in existing_groups:
        flash('分组已存在')
    else:
        flash(f'已创建分组：{created_group}')

    save_group_filter(created_group)

    if return_to == 'detail' and return_task_id:
        return redirect(url_for('comic_detail', task_id=return_task_id))
    return redirect(url_for('comics_list', group=created_group))


@app.route('/comic_groups/assign', methods=['POST'])
@login_required
@admin_required
def assign_comic_group_route():
    comic_name = (request.form.get('comic_name') or '').strip()
    group_name = normalize_group_name(request.form.get('group_name'))
    redirect_group = normalize_group_name(request.form.get('redirect_group', '全部')) or '全部'
    return_to = request.form.get('return_to')
    return_task_id = (request.form.get('task_id') or comic_name).strip()

    if not comic_name or not get_comic_directory(comic_name):
        flash('漫画不存在，无法设置分组')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    if not group_name:
        flash('请选择一个分组')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    if not assign_comic_group(comic_name, group_name):
        flash('设置分组失败，请重试')
        if return_to == 'detail' and return_task_id:
            return redirect(url_for('comic_detail', task_id=return_task_id))
        return redirect(url_for('comics_list', group=redirect_group))

    flash(f'《{comic_name}》已加入 {group_name}')
    if return_to == 'detail' and return_task_id:
        return redirect(url_for('comic_detail', task_id=return_task_id))
    target_group = redirect_group if redirect_group != '全部' else group_name
    save_group_filter(target_group)
    return redirect(url_for('comics_list', group=target_group))


@app.route('/comic_groups/delete', methods=['POST'])
@login_required
@admin_required
def delete_comic_group_route():
    group_name = normalize_group_name(request.form.get('group_name'))
    redirect_group = normalize_group_name(request.form.get('redirect_group', '全部')) or '全部'
    return_to = request.form.get('return_to')
    return_task_id = (request.form.get('task_id') or '').strip()

    if not group_name:
        flash('请选择要删除的分组')
    elif group_name == '默认分组':
        flash('默认分组不能删除')
    elif not delete_comic_group(group_name):
        flash('删除分组失败，请重试')
    else:
        flash(f'已删除分组：{group_name}')
        if redirect_group == group_name:
            redirect_group = '全部'
        save_group_filter(redirect_group)

    if return_to == 'detail' and return_task_id:
        return redirect(url_for('comic_detail', task_id=return_task_id))
    return redirect(url_for('comics_list', group=redirect_group))
