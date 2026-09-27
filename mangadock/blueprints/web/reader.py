# -*- coding: utf-8 -*-
"""漫画详情 / 阅读器 / 进度接口 / 静态漫画文件。"""
import os
import mimetypes
import re
import zipfile
import io
from functools import lru_cache

from PIL import Image

from flask import (
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    Response,
    send_from_directory,
    session,
    url_for,
)

from mangadock.auth import (
    admin_required,
    get_current_user,
    is_safe_next_target,
    login_required,
    require_csrf_for_session_auth,
    save_uploaded_cover_image,
)
from mangadock.core import app
from mangadock.extensions import csrf, db
from mangadock.models import AniListAccount, AniListComicLink
from mangadock.services.download import refresh_comic_description
from mangadock.services.groups import (
    can_user_access_group,
    filter_grouped_comics_for_user,
    get_admin_hidden_targets,
    get_comic_group_map,
)
from mangadock.services.library import (
    detect_local_comic_format,
    api_page_source_version,
    get_available_comics,
    get_comic_description,
    get_comic_directory,
    list_local_chapters,
    resolve_comic_file_request,
)
from mangadock.services.reading import (
    get_reading_progress,
    get_reading_time_for_comic,
    record_reading_time,
    save_reading_progress,
    user_can_access_progress_key,
)
from mangadock.services.tasks import get_task
from mangadock.services.webdav import is_cache_path
from mangadock.services.webdav_metadata import metadata_view
from mangadock.utils import safe_int
from mangadock.utils.media import repair_pdf_for_reading
from mangadock.blueprints.web.common import safe_print


def reconcile_anilist_if_linked(user_id, comic_name):
    from mangadock.services.anilist import reconcile_progress
    if not AniListComicLink.query.filter_by(user_id=user_id, comic_name=comic_name).first():
        return
    try:
        reconcile_progress(user_id, comic_name)
    except Exception:
        db.session.rollback()
        app.logger.exception('AniList automatic reconciliation failed')


@app.route('/comic/<task_id>')
@login_required
def comic_detail(task_id):
    """漫画详情页面 - 显示漫画信息和章节列表"""
    current_user = get_current_user()

    # 尝试通过任务ID获取任务
    task = get_task(task_id)
    if task:
        comic_name = task.comic_name
        comic_format = task.comic_format
    else:
        # 如果任务不存在，尝试将task_id作为漫画名称处理
        comic_name = task_id

        # 检查漫画文件是否存在
        comic_path = get_comic_directory(comic_name)
        if not comic_path or not os.path.exists(comic_path):
            return render_template('error.html', message="漫画不存在"), 404

        comic_format = detect_local_comic_format(comic_path)
        if not comic_format:
            return render_template('error.html', message="不支持的漫画格式"), 404

        # 创建一个模拟的task对象
        class MockTask:
            def __init__(self, comic_name, comic_format):
                self.id = comic_name
                self.comic_name = comic_name
                self.comic_format = comic_format
                self.status = 'completed'
                self.total_chapters = 0
                self.completed_chapters = 0
                self.available_chapters = 0
                self.created_at = None
                self.url = None
                self.group = '默认分组'  # 默认分组

        # 计算可用章节数
        mock_task = MockTask(comic_name, comic_format)
        local_chapters = list_local_chapters(comic_name)
        mock_task.available_chapters = len(local_chapters)
        mock_task.total_chapters = mock_task.available_chapters
        mock_task.completed_chapters = mock_task.available_chapters
        task = mock_task

    # 获取章节列表
    try:
        chapters = list_local_chapters(task.comic_name)
    except Exception as e:
        safe_print(f"获取章节列表失败: {e}")
        return render_template('error.html', message='获取章节列表失败'), 500

    task.available_chapters = len(chapters)
    task.total_chapters = max(getattr(task, 'total_chapters', 0), len(chapters))
    task.completed_chapters = max(getattr(task, 'completed_chapters', 0), len(chapters))

    # 获取阅读进度
    current_user_id = session.get('user_id')
    progress = get_reading_progress(task.comic_name, current_user_id)
    anilist_link = AniListComicLink.query.filter_by(user_id=current_user_id, comic_name=task.comic_name).first()
    # 获取阅读时间（分钟）
    reading_time = get_reading_time_for_comic(task.comic_name, current_user_id)
    current_group = get_comic_group_map().get(task.comic_name) or getattr(task, 'group', None) or '默认分组'
    if not can_user_access_group(current_group, current_user):
        flash('当前账号未被授权访问该分组漫画')
        return redirect(url_for('comics_list'))

    reconcile_anilist_if_linked(current_user_id, task.comic_name)
    progress = get_reading_progress(task.comic_name, current_user_id)
    hidden_comic_names, hidden_group_names = get_admin_hidden_targets(current_user)
    is_current_comic_directly_hidden = task.comic_name in hidden_comic_names
    is_current_group_hidden = current_group in hidden_group_names
    is_current_comic_hidden = is_current_comic_directly_hidden or is_current_group_hidden

    _, groups, group_counts, _ = filter_grouped_comics_for_user(get_available_comics(), current_user)
    if current_user and current_user.is_admin and current_group not in groups:
        groups.append(current_group)
        group_counts[current_group] = group_counts.get(current_group, 0)

    comic_description = get_comic_description(task.comic_name)
    if not comic_description:
        # 旧书库补全：有源站映射时按需抓取一次简介
        comic_description = refresh_comic_description(task.comic_name) or ''

    # 2026-09-22 用户要求：详情页返回固定回「漫画首页」，不再回书架（对齐小说详情回小说首页的语义）。
    # 不再读 return_to：书架/搜索/首页等入口带来的 return_to 一律忽略；阅读链路不受影响
    # （阅读器的返回仍是详情页，详情页的返回才是首页）。
    library_return_url = url_for('index')

    return render_template(
        'comic_detail.html',
        task=task,
        chapters=chapters,
        progress=progress,
        anilist_link=anilist_link,
        reading_time=reading_time,
        comic_description=comic_description,
        is_webdav_comic=is_cache_path(get_comic_directory(task.comic_name)),
        anilist_metadata=metadata_view(task.comic_name),
        current_group=current_group,
        groups=groups,
        group_counts=group_counts,
        group_total_count=sum(group_counts.values()),
        selected_group=current_group,
        show_group_menu=bool(groups),
        group_menu_return_to='detail',
        group_menu_task_id=task.comic_name,
        is_current_comic_hidden=is_current_comic_hidden,
        is_current_comic_directly_hidden=is_current_comic_directly_hidden,
        is_current_group_hidden=is_current_group_hidden,
        hidden_group_names=hidden_group_names,
        library_return_url=library_return_url,
        deletion_path=get_comic_directory(task.comic_name),
    )


@app.route('/comic/delete', methods=['POST'])
@login_required
@admin_required
def delete_comic_route():
    from mangadock.services.comic_delete import delete_local_comic
    comic_name = request.form.get('comic_name', '')
    try:
        delete_local_comic(comic_name, request.form.get('directory', ''))
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for('comic_detail', task_id=comic_name))
    except Exception as exc:
        app.logger.exception('删除漫画失败: %s', comic_name)
        flash('删除未完成，部分文件可能已删除。请检查目录权限或磁盘状态后重试。')
        return redirect(url_for('comics_list'))
    from mangadock.services.webdav import is_cache_path
    if is_cache_path(request.form.get('directory', '')):
        flash('已从本地缓存移除「' + comic_name + '」。云端文件未删除，下次同步会重新出现')
    else:
        flash('已删除漫画「' + comic_name + '」及本地源文件')
    return redirect(url_for('comics_list'))


@app.route('/comic/<task_id>/cover', methods=['POST'])
@admin_required
def update_comic_cover(task_id):
    task = get_task(task_id)
    comic_name = task.comic_name if task else task_id

    comic_path = get_comic_directory(comic_name)
    if not comic_path or not os.path.exists(comic_path):
        flash('漫画不存在，无法修改封面')
        return redirect(url_for('comics_list'))

    cover_file = request.files.get('cover_file')
    try:
        save_uploaded_cover_image(comic_name, cover_file)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for('comic_detail', task_id=task_id))
    except Exception:
        flash('封面保存失败，请稍后重试')
        return redirect(url_for('comic_detail', task_id=task_id))

    flash(f'《{comic_name}》封面已更新')
    return redirect(url_for('comic_detail', task_id=task_id))


@app.route('/reader/<task_id>')
@login_required
def comic_reader(task_id):
    """漫画阅读器页面 - 通过任务ID"""
    current_user = get_current_user()

    # 尝试通过任务ID获取任务
    task = get_task(task_id)
    if task:
        comic_name = task.comic_name
        comic_format = task.comic_format
    else:
        # 如果任务不存在，尝试将task_id作为漫画名称处理
        comic_name = task_id

        # 检查漫画文件是否存在
        comic_path = get_comic_directory(comic_name)
        if not comic_path or not os.path.exists(comic_path):
            return render_template('error.html', message="漫画不存在"), 404

        comic_format = detect_local_comic_format(comic_path)
        if not comic_format:
            return render_template('error.html', message="不支持的漫画格式"), 404

        # 创建一个模拟的task对象
        class MockTask:
            def __init__(self, comic_name, comic_format):
                self.id = comic_name
                self.comic_name = comic_name
                self.comic_format = comic_format
                self.status = 'completed'
                self.total_chapters = 0
                self.completed_chapters = 0
                self.available_chapters = 0
                self.created_at = None

        # 计算可用章节数
        mock_task = MockTask(comic_name, comic_format)
        local_chapters = list_local_chapters(comic_name)
        mock_task.available_chapters = len(local_chapters)
        mock_task.total_chapters = mock_task.available_chapters
        mock_task.completed_chapters = mock_task.available_chapters
        task = mock_task

    current_group = get_comic_group_map().get(task.comic_name) or getattr(task, 'group', None) or '默认分组'
    if not can_user_access_group(current_group, current_user):
        flash('当前账号未被授权访问该分组漫画')
        return redirect(url_for('comics_list'))

    # 获取阅读进度
    current_user_id = session.get('user_id')
    reconcile_anilist_if_linked(current_user_id, task.comic_name)
    progress = get_reading_progress(task.comic_name, current_user_id)
    # 检查是否有指定的起始章节
    start_chapter = request.args.get('start_chapter', None)
    if start_chapter is not None:
        # 2026-09-20 code review P2：?start_chapter=abc 此前直接 int() → 500
        start_chapter = safe_int(start_chapter)
    else:
        start_chapter = progress.last_chapter if progress else 0
    start_page = progress.last_page if progress else 0

    # 确定文件扩展名
    file_ext = 'pdf' if task.comic_format == 1 else 'cbz'

    library_return_url = request.args.get('return_to')
    if not is_safe_next_target(library_return_url) or not library_return_url.startswith('/comics'):
        library_return_url = url_for('comics_list', group=current_group)

    return render_template(
        'reader.html',
        task=task,
        file_ext=file_ext,
        start_chapter=start_chapter,
        start_page=start_page,
        library_return_url=library_return_url,
        deletion_path=get_comic_directory(task.comic_name),
        anilist_enabled=bool(
            db.session.get(AniListAccount, current_user_id) and
            AniListComicLink.query.filter_by(user_id=current_user_id, comic_name=task.comic_name).first()
        ),
    )

@app.route('/save_progress', methods=['POST'])
@login_required
@csrf.exempt
def save_progress():
    """保存阅读进度（修改路由名避免与函数名冲突）"""
    csrf_error = require_csrf_for_session_auth(api_response=False)
    if csrf_error:
        return csrf_error

    try:
        data = request.get_json()

        if not data:
            return jsonify({'status': 'error', 'message': '无效的JSON格式'}), 400

        comic_name = (data.get('comic_name') or '').strip()
        chapter = data.get('chapter', 0)
        page = data.get('page', 0)
        scroll_position = data.get('scroll_position', 0)
        total_chapters = data.get('total_chapters', 0)
        total_pages = data.get('total_pages', 0)
        anchor_paragraph = data.get('anchor_paragraph')
        anchor_offset = data.get('anchor_offset')
        reading_time = data.get('reading_time', 0)
        reading_session_id = (data.get('reading_session_id') or '').strip()[:128]

        if not comic_name:
            return jsonify({'status': 'error', 'message': '漫画名称不能为空'}), 400

        current_user = get_current_user()
        if not user_can_access_progress_key(comic_name, current_user):
            return jsonify({'status': 'error', 'message': '无权访问该内容'}), 403

        current_user_id = session.get('user_id')
        def bounded_int(value, default=0, minimum=0, maximum=1_000_000):
            try:
                number = int(value)
            except (TypeError, ValueError):
                return default
            return max(minimum, min(number, maximum))

        chapter = bounded_int(chapter)
        page = bounded_int(page)
        scroll_position = bounded_int(scroll_position)
        total_chapters = bounded_int(total_chapters)
        total_pages = bounded_int(total_pages)
        if anchor_paragraph is not None:
            anchor_paragraph = bounded_int(anchor_paragraph)
        if anchor_offset is not None:
            anchor_offset = bounded_int(anchor_offset)
        save_reading_progress(
            comic_name, chapter, page, scroll_position, total_chapters, total_pages,
            current_user_id, anchor_paragraph, anchor_offset
        )

        # 记录阅读时间（按会话累计秒数去重，避免页面隐藏/切换时重复记时）
        if reading_time > 0:
            record_reading_time(comic_name, reading_time, current_user_id, reading_session_id or None)

        return jsonify({'status': 'success'})
    except Exception as e:
        # code review P2：异常时回滚可能挂起的会话写操作，避免死事务占用连接导致后续
        # 请求全部 500；也确保并发重试失败时不会留下半提交状态。
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': '保存阅读进度失败'}), 400

@app.route('/get_progress/<comic_name>')
@login_required
def get_progress(comic_name):
    """获取阅读进度"""
    try:
        current_user = get_current_user()
        if not user_can_access_progress_key(comic_name, current_user):
            return jsonify({'status': 'error', 'message': '无权访问该内容'}), 403
        current_user_id = session.get('user_id')
        progress = get_reading_progress(comic_name, current_user_id)
        if progress:
            return jsonify({
                'status': 'success',
                'chapter': progress.last_chapter,
                'page': progress.last_page,
                'scroll_position': progress.scroll_position or 0,
                'anchor_paragraph': progress.anchor_paragraph,
                'anchor_offset': progress.anchor_offset
            })
        else:
            return jsonify({
                'status': 'success',
                'chapter': 0,
                'page': 0,
                'scroll_position': 0,
                'anchor_paragraph': None,
                'anchor_offset': None
            })
    except Exception as e:
        safe_print(f"获取阅读进度失败: {e}")
        return jsonify({'status': 'error', 'message': '获取阅读进度失败'})

@app.route('/static/comic/<path:filename>')
@login_required
def serve_comic_file(filename):
    """提供漫画文件访问"""
    resolved_file = resolve_comic_file_request(filename)
    if not resolved_file:
        return jsonify({'error': '文件不存在'}), 404

    current_user = get_current_user()
    current_group = get_comic_group_map().get(resolved_file['comic_name']) or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return jsonify({'error': '当前账号未被授权访问该分组漫画'}), 403

    from mangadock.services.webdav import chapter_access, chapter_file_response
    with chapter_access(resolved_file['file_path']) as file_path:
        if not file_path:
            return jsonify({'error': '文件不存在'}), 404
        readable = repair_pdf_for_reading(file_path) if file_path.lower().endswith('.pdf') else file_path
        handle = open(readable, 'rb')
    return chapter_file_response(handle, os.path.basename(file_path), mimetypes.guess_type(file_path)[0] or 'application/octet-stream')


def _cbz_images(archive):
    images = [name for name in archive.namelist()
              if not name.endswith('/') and not any(part.startswith('._') or part == '__MACOSX' for part in name.split('/'))
              and re.search(r'\.(jpg|jpeg|png|gif|webp)$', name, re.I)]
    images.sort(key=lambda name: [(1, int(part)) if part.isdigit() else (0, part.casefold())
                                  for part in re.split(r'(\d+)', name)])
    return images


@lru_cache(maxsize=64)
def cbz_page_manifest(file_path, source_version):
    with zipfile.ZipFile(file_path) as archive:
        images = _cbz_images(archive)
        dimensions = []
        for name in images:
            try:
                with archive.open(name) as image_file, Image.open(image_file) as image:
                    dimensions.append(image.size)
            except (OSError, ValueError):
                dimensions.append((0, 0))
    return tuple(images), tuple(dimensions)


@app.route('/api/comic-pages/<path:filename>')
@login_required
def comic_pages(filename):
    """返回 CBZ 页目录，或单独返回一张图片。"""
    resolved_file = resolve_comic_file_request(filename)
    if not resolved_file or not resolved_file['file_path'].lower().endswith('.cbz'):
        return jsonify({'error': '章节不存在'}), 404

    current_group = get_comic_group_map().get(resolved_file['comic_name']) or '默认分组'
    if not can_user_access_group(current_group, get_current_user()):
        return jsonify({'error': '当前账号未被授权访问该分组漫画'}), 403

    from mangadock.services.webdav import chapter_access, WebDavError
    from mangadock.services.webdav_cbz import read_remote_cbz, RemoteReadInterrupted
    try:
        remote_result = read_remote_cbz(resolved_file['file_path'], _read_cbz_page)
        if remote_result is not None:
            images, dimensions, image_name, image_data = remote_result
        else:
            with chapter_access(resolved_file['file_path']) as file_path:
                if not file_path:
                    return jsonify({'error': '章节不存在'}), 404
                images, dimensions, image_name, image_data = _read_cbz_page(file_path)
    except LookupError:
        return jsonify({'error': '页面不存在'}), 404
    except RemoteReadInterrupted:
        return jsonify({'error': '网盘连接暂时中断，请重试'}), 503
    except (OSError, zipfile.BadZipFile, RuntimeError, WebDavError):
        from mangadock.services.webdav import discard_cached_chapter
        discard_cached_chapter(resolved_file['file_path'])
        return jsonify({'error': '章节读取失败'}), 500

    if image_data is None:
        return jsonify({'pages': len(images), 'dimensions': dimensions})
    response = Response(image_data, mimetype=mimetypes.guess_type(image_name)[0] or 'application/octet-stream')
    response.headers['Cache-Control'] = 'private, no-store'
    return response


def _read_cbz_page(file_path):
    if hasattr(file_path, 'read'):
        with zipfile.ZipFile(file_path) as archive:
            images = _cbz_images(archive)
            page = request.args.get('page')
            if page is None:
                return images, [file_path.dimensions.get(name, (0, 0)) for name in images], None, None
            if not page.isdecimal() or int(page) >= len(images):
                raise LookupError('页面不存在')
            image_name = images[int(page)]
            if archive.getinfo(image_name).file_size > 64 * 1024 * 1024:
                raise RuntimeError('单页图片超过 64MB')
            image_data = archive.read(image_name)
            try:
                with Image.open(io.BytesIO(image_data)) as image:
                    file_path.dimensions[image_name] = list(image.size)
                file_path._touch()
            except (OSError, ValueError):
                pass
            return images, [], image_name, image_data
    images, dimensions = cbz_page_manifest(file_path, api_page_source_version(file_path))
    page = request.args.get('page')
    if page is None:
        return images, dimensions, None, None
    if not page.isdecimal() or int(page) >= len(images):
        raise LookupError('页面不存在')
    image_name = images[int(page)]
    with zipfile.ZipFile(file_path) as archive:
        return images, dimensions, image_name, archive.read(image_name)

@app.route('/api/chapters/<task_id>')
@login_required
def get_chapters(task_id):
    """获取漫画章节列表 - 支持任务ID和漫画名称"""
    current_user = get_current_user()

    # 尝试通过任务ID获取任务
    task = get_task(task_id)
    if task:
        comic_name = task.comic_name
    else:
        # 将task_id作为漫画名称处理
        comic_name = task_id

    comic_path = get_comic_directory(comic_name)
    if not comic_path or not os.path.exists(comic_path):
        return jsonify({'error': '漫画文件不存在'}), 404

    current_group = get_comic_group_map().get(comic_name) or getattr(task, 'group', None) or '默认分组'
    if not can_user_access_group(current_group, current_user):
        return jsonify({'error': '当前账号未被授权访问该分组漫画'}), 403

    try:
        chapters = list_local_chapters(comic_name)
    except Exception as e:
        safe_print(f"获取章节列表失败: {e}")
        return jsonify({'error': '获取章节列表失败'}), 500

    return jsonify({
        'comic_name': comic_name,
        'chapters': chapters,
        'total_chapters': len(chapters)
    })
