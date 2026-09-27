# -*- coding: utf-8 -*-
"""Delete a local comic and its library metadata (administrator only at route)."""
import fcntl
import glob
import hashlib
import json
import os
import shutil
import tempfile
import time

from sqlalchemy import or_, text

from mangadock.core import API_PAGE_CACHE_ROOT, app
from mangadock.extensions import db
from mangadock.models import (
    AdminHiddenLibraryItem, AniListComicLink, BackgroundCommand, ComicGroupMembership,
    ComicIdentity, ComicUpdateCheck, DownloadTask, ReadingProgress, ReadingSessionState,
)
from mangadock.services import library
from mangadock.services.tasks import normalize_comic_url_identity
from mangadock.services.home_banner import HOME_BANNER_DIR
from mangadock.settings import COMIC_MAPPING_FILE, COVER_ROOT


def deletion_directory(comic_name):
    if not library.is_safe_comic_name(comic_name) or comic_name != comic_name.strip():
        raise ValueError('漫画名称无效')
    roots = library.get_comic_scan_roots(existing_only=True)
    paths = set()
    for root in roots:
        candidate = os.path.join(root, comic_name)
        if not os.path.lexists(candidate):
            continue
        if os.path.islink(candidate) or not os.path.isdir(candidate):
            raise ValueError('漫画目录是符号链接或不是目录，不能删除')
        real_root, real_path = os.path.realpath(root), os.path.realpath(candidate)
        if os.path.dirname(real_path) != real_root:
            raise ValueError('漫画目录超出扫描目录，不能删除')
        if any(os.path.commonpath([real_path, os.path.realpath(other)]) == real_path
               for other in roots):
            raise ValueError('漫画目录包含扫描根目录，不能删除')
        paths.add(real_path)
    if len(paths) > 1:
        raise ValueError('多个扫描目录中有同名漫画，请先整理同名目录再删除')
    if not paths:
        raise ValueError('漫画源文件目录不存在')
    return paths.pop()


def _worker_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _remove_tree(directory):
    def handle_error(function, path, exc_info):
        error = exc_info[1]
        if not isinstance(error, FileNotFoundError):
            raise error
        # macOS SMB can return ENOENT for unlink(dir_fd=...) even when the
        # entry exists. Retry the absolute path; a genuinely missing entry is OK.
        if function in (os.unlink, os.rmdir):
            try:
                function(path)
            except FileNotFoundError:
                pass
        elif os.path.lexists(path):
            raise error

    shutil.rmtree(directory, onerror=handle_error)
    if os.path.lexists(directory):
        raise OSError('删除后源目录仍然存在：' + directory)


def _unlink_if_present(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def delete_local_comic(comic_name, expected_directory):
    # SQLite write lock prevents queue creation/claiming during the deletion checks
    # and filesystem work. Already executing workers are checked before touching files.
    db.session.commit()
    db.session.execute(text('BEGIN IMMEDIATE'))
    temp_path = None
    files_touched = False
    try:
        directory = deletion_directory(comic_name)
        if not expected_directory or directory != os.path.realpath(expected_directory):
            raise ValueError('漫画目录已变化，请刷新详情页后重试')
        with open(os.path.join(app.instance_path, 'comic_mapping.lock'), 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            mapping = {} if not os.path.exists(COMIC_MAPPING_FILE) else None
            if mapping is None:
                with open(COMIC_MAPPING_FILE, encoding='utf-8') as source:
                    mapping = json.load(source)
            if not isinstance(mapping, dict):
                raise ValueError('漫画源地址映射异常，未删除任何文件')
            source_url = normalize_comic_url_identity(mapping.get(comic_name, ''))
            tasks = DownloadTask.query.filter(or_(
                DownloadTask.status.in_(['pending', 'running']),
                DownloadTask.worker_pid.isnot(None),
            )).all()
            for task in tasks:
                related = (task.comic_name == comic_name or task.comic_name == '未知漫画'
                           or bool(source_url and normalize_comic_url_identity(task.url) == source_url))
                if related and (task.status in ('pending', 'running') or _worker_alive(task.worker_pid)):
                    raise ValueError('漫画仍有下载或更新任务，请取消并等待后台退出后再删除')
            if ComicUpdateCheck.query.filter_by(status='checking').first():
                raise ValueError('后台正在检查漫画更新，请稍后重试')
            if BackgroundCommand.query.filter(
                BackgroundCommand.command_type.in_(['refresh_update_checks', 'scan_home_banners']),
                BackgroundCommand.status == 'running',
            ).first():
                raise ValueError('后台正在检查或更新漫画，请稍后重试')
            for command in BackgroundCommand.query.filter_by(command_type='webdav_metadata', status='running').all():
                try:
                    related_comic = json.loads(command.payload or '{}').get('comic_name')
                except (ValueError, AttributeError):
                    continue
                if related_comic == comic_name:
                    raise ValueError('后台正在补全这部漫画的封面与简介，请稍后再删除')

            identity = ComicIdentity.query.filter_by(comic_name=comic_name).first()
            cache_path = None
            if identity:
                cache_path = os.path.join(API_PAGE_CACHE_ROOT, identity.comic_id)
                if (os.path.basename(identity.comic_id) != identity.comic_id
                        or identity.comic_id in ('.', '..')):
                    raise ValueError('漫画缓存标识无效')
            # Prepare the atomic mapping replacement before deleting source files.
            mapping.pop(comic_name, None)
            descriptor, temp_path = tempfile.mkstemp(prefix='.comic-json-', suffix='.tmp',
                                                    dir=os.path.dirname(COMIC_MAPPING_FILE) or '.')
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                json.dump(mapping, target, ensure_ascii=False, indent=4)

            files_touched = True
            _remove_tree(directory)
            for cover in (os.path.join(COVER_ROOT, comic_name + '.jpg'),
                          os.path.join(COVER_ROOT, 'hero', comic_name + '.jpg')):
                if os.path.lexists(cover):
                    _unlink_if_present(cover)
            banner_key = hashlib.sha256(comic_name.encode('utf-8')).hexdigest()
            banner_files = glob.glob(os.path.join(HOME_BANNER_DIR, banner_key + '.*'))
            banner_files += glob.glob(os.path.join(HOME_BANNER_DIR, 'upscaled', banner_key + '-*.jpg'))
            for banner in banner_files:
                _unlink_if_present(banner)
            if cache_path and os.path.lexists(cache_path):
                if os.path.islink(cache_path):
                    _unlink_if_present(cache_path)
                else:
                    _remove_tree(cache_path)
            os.replace(temp_path, COMIC_MAPPING_FILE)
            temp_path = None
            for model in (DownloadTask, ComicGroupMembership, ComicUpdateCheck,
                          ReadingProgress, ReadingSessionState, AniListComicLink, ComicIdentity):
                model.query.filter_by(comic_name=comic_name).delete(synchronize_session=False)
            AdminHiddenLibraryItem.query.filter_by(
                target_type='comic', target_value=comic_name,
            ).delete(synchronize_session=False)
            db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
        # Notify other web processes, including after partial filesystem failure.
        if files_touched:
            with open(os.path.join(app.instance_path, 'comic-deletion.version'), 'w') as marker:
                marker.write(str(time.time_ns()))
        # A failed filesystem deletion may already have removed some files.
        with library.comic_scan_cache_lock:
            library.comic_directory_scan_cache.clear()
            library.comic_root_listing_cache.clear()
        library.invalidate_comics_cache()
