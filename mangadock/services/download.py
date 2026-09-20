# -*- coding: utf-8 -*-
"""章节/整本下载共享管线。

站点专属实现已拆分至 ``mangadock/services/providers/``：
- 识别（detect_source_provider / is_supported_comic_url）与分发
  （load_comic_source / download_provider_chapter）全部经 providers 注册表；
- 本文件只保留跨站点共享的管线：图片并发下载、CBZ/PDF 打包、
  通用 crawl_chapter 兜底、整本下载与更新任务流。
- 站点专属函数名在此 re-export，保持既有导入路径兼容。
"""
import concurrent.futures
import json
import os
import random
import re
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from natsort import natsorted

from mangadock.core import app
from mangadock.extensions import db
from mangadock.models import DownloadTask
from mangadock.settings import (
    ADULT_CONTENT_DISABLED_MESSAGE,
    COMIC_MAPPING_FILE,
    COMIC_ROOT,
    CONFIG,
    COVER_ROOT,
    MAX_IMAGE_RESPONSE_BYTES,
    china_tz,
)
from mangadock.services.library import (
    chapter_output_exists,
    is_valid_local_chapter_file,
    get_local_chapter_bases,
    get_local_chapter_match_bases,
    is_existing_local_chapter,
    load_comic_mapping,
    normalize_comic_description,
    save_comic_description,
    save_comic_mapping,
    save_cover_image,
    target_extension,
)
from mangadock.services import providers
from mangadock.services.providers.common import extract_description_from_html, safe_print
from mangadock.services.providers.baozimh_org import (
    build_baozimh_org_image_url,
    decode_baozimh_org_image_payload,
    download_baozimh_org_chapter,
    load_baozimh_org_chapter_images,
    load_baozimh_org_source,
    resolve_baozimh_org_image_host,
)
from mangadock.services.providers.baozimhcn import load_baozimhcn_source
from mangadock.services.providers.mxs import (
    crawl_chapter_mxs,
    download_complete_book_mxs,
    download_image_mxs,
    download_images_concurrently_mxs,
    download_mxs_chapter,
    is_mxs_url,
    load_mxs_source,
    title,
    title_mxs,
)
from mangadock.services.tasks import get_task, is_task_cancel_requested, update_task
from mangadock.utils.cover_image import normalize_cover_bytes
from mangadock.utils.http import (
    safe_http_get,
    should_verify_upstream_tls,
    write_limited_response_to_file,
)
from mangadock.utils.media import (
    build_pdf_from_image_list,
    default_headers,
    ensure_directory,
    repair_pdf_for_reading,
    sanitize_filename,
)


def detect_source_provider(url):
    """识别 URL 所属站点 provider（识别保持纯净，18+ 拦截见 is_adult_content_blocked）。"""
    return providers.detect_provider_name(url)


def is_adult_content_blocked(url, allow_adult=False):
    """18+ 拦截判定（只拦「新增下载」）。

    - URL 已存在于库映射（comic.json）视为既有收藏：允许继续更新/重新下载；
    - 全局开关关闭时，创建者被授予 can_view_adult 的任务（allow_adult=True）
      仍可下载 mxs 源（2026-09-20 用户级 18+ 授权）。
    """
    if not is_mxs_url(url):
        return False
    from mangadock.services.adult_content import is_adult_content_enabled
    if is_adult_content_enabled() or allow_adult:
        return False
    try:
        mapping = load_comic_mapping()
    except Exception:
        mapping = {}
    normalized = (url or '').strip()
    return normalized not in {(u or '').strip() for u in mapping.values()}


def is_supported_comic_url(url):
    return providers.is_supported_url(url)


def load_comic_source(url):
    return providers.load_source(url)


def persist_source_description(comic_name, source):
    """把 source['description'] 落到库里（下载/更新共用）。"""
    description = ''
    if isinstance(source, dict):
        description = source.get('description') or ''
    if not description:
        return False
    return save_comic_description(comic_name, description)


def refresh_comic_description(comic_name, source_url=None):
    """按 comic.json 源站 URL 重新抓取简介（用于旧书补全）。"""
    normalized_name = (comic_name or '').strip()
    if not normalized_name:
        return ''
    url = (source_url or '').strip() or load_comic_mapping().get(normalized_name)
    if not url:
        return ''
    try:
        source = load_comic_source(url)
        persist_source_description(normalized_name, source)
        return normalize_comic_description(source.get('description'))
    except Exception:
        return ''


def images_to_cbz(folder_path):
    """将图片转换为CBZ格式"""
    try:
        images = []
        for fname in os.listdir(folder_path):
            # SMB 挂载会为每个文件生成 AppleDouble 元数据文件（._xxx）。
            # 不排除的话它们会被打进 cbz，前端 Safari 排序后将其排在首位，
            # 解码失败会导致整章报「章节图片加载失败」（Chrome 嗅探排序侥幸正常）。
            if fname.startswith('.'):
                continue
            if fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                images.append(os.path.join(folder_path, fname))

        # 排序
        images = natsorted(images)
        if not images:
            return False, f"文件夹 {folder_path} 中没有图片"

        cbz_name = os.path.join(os.path.dirname(folder_path), f"{os.path.basename(folder_path)}.cbz")
        with zipfile.ZipFile(cbz_name, 'w') as cbz_file:
            for image_path in images:
                cbz_file.write(
                    image_path,
                    arcname=os.path.basename(image_path),
                    compress_type=zipfile.ZIP_STORED
                )
        return True, f"成功生成CBZ：{cbz_name}"
    except Exception as e:
        return False, f"CBZ生成失败：{str(e)}"


def images_to_cbz_watch(folder_path):
    try:
        single_image_path = os.path.join(COVER_ROOT, "bzmh.png")
        if not os.path.exists(single_image_path):
            return False, f"错误：图片文件不存在，请检查路径 -> {single_image_path}"
        cbz_name = os.path.join(folder_path, "00.cbz")
        with zipfile.ZipFile(cbz_name, 'w') as cbz_file:
            cbz_file.write(
                single_image_path,
                arcname=os.path.basename(single_image_path),
                compress_type=zipfile.ZIP_STORED
            )
        return True, f"成功生成 CBZ：{cbz_name}（包含图片：{os.path.basename(single_image_path)}）"
    except Exception as e:
        return False, f"CBZ 生成失败：{str(e)}"


def images_to_pdf(folder_path):
    """将图片转换为PDF格式，页尺寸跟随图片内容。"""
    try:
        images = []
        for fname in os.listdir(folder_path):
            if fname.startswith('.'):
                continue
            if fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                images.append(os.path.join(folder_path, fname))
        images = natsorted(images)
        if not images:
            return False, f"文件夹 {folder_path} 中没有图片"

        pdf_name = os.path.join(os.path.dirname(folder_path), f"{os.path.basename(folder_path)}.pdf")

        return build_pdf_from_image_list(images, pdf_name)
    except Exception as e:
        return False, f"PDF生成失败：{str(e)}"


def download_image(session, base_url, save_dir, n, task_id, retries=CONFIG['retry_times']):
    """下载单张图片"""
    img_url = base_url.format(n)
    file_path = os.path.join(save_dir, f"{n}.jpg")

    for attempt in range(retries):
        if is_task_cancel_requested(task_id):
            return False, n, True
        time.sleep(random.uniform(*CONFIG['delay_range']))
        try:
            with safe_http_get(
                img_url,
                stream=True,
                timeout=15,
                max_bytes=MAX_IMAGE_RESPONSE_BYTES,
                session_obj=session
            ) as response:
                if response.status_code == 200:
                    if is_task_cancel_requested(task_id):
                        return False, n, True
                    write_limited_response_to_file(response, file_path, MAX_IMAGE_RESPONSE_BYTES)
                    return True, n, False
                else:
                    if response.status_code == 404:
                        return False, n, True
                    else:
                        safe_print(f"图片{n} 下载失败，第{attempt+1}次重试。")
        except Exception as e:
            safe_print(f"图片{n} 下载失败，第{attempt+1}次重试。")

        if attempt < retries - 1:
            if is_task_cancel_requested(task_id):
                return False, n, True
            time.sleep(2 ** attempt)

    return False, n, False


def crawl_chapter(chapter_url, folder, chapter, comic_format, task_id):
    """下载单个章节（通用兜底：scomic 顺序图片规则，baozimhcn / fanqie 走此路径）"""
    save_dir = os.path.join(COMIC_ROOT, folder, f"{chapter:02d}")
    os.makedirs(save_dir, exist_ok=True)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    try:
        with requests.Session() as session:
            for attempt in range(3):
                if is_task_cancel_requested(task_id):
                    shutil.rmtree(save_dir, ignore_errors=True)
                    return False, "任务已取消"
                try:
                    response = safe_http_get(chapter_url, headers=headers, timeout=10, session_obj=session)
                    # 如果成功获取200响应，直接返回成功
                    if response.status_code == 200:
                        break
                    # 非200状态码，记录并继续重试
                    safe_print(f"章节页访问失败，状态码: {response.status_code}，第{attempt+1}次尝试")
                except Exception as e:
                    safe_print(f"章节页访问异常: {str(e)}，第{attempt+1}次尝试")
            else:
                # 当循环完成且未通过break退出时，说明3次尝试都失败
                return False, f"章节页经过3次尝试后仍访问失败"

            match = re.search(r'(https?://[^/]+/scomic/[^/]+/\d+/[^/]+/1\.jpg)', response.text)
            if not match:
                return False, "未找到图片地址"

            base_url = match.group(1).replace("1.jpg", "{}.jpg")
            update_task(task_id, log=f"开始下载章节：{chapter}")

            max_workers = CONFIG['max_workers']
            success_count = 0
            n = 1
            stop_flag = False

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                while not stop_flag:
                    # 检查任务是否已取消
                    if is_task_cancel_requested(task_id):
                        executor.shutdown(wait=False)
                        return False, "任务已取消"

                    futures = []
                    for _ in range(max_workers * 2):
                        futures.append(executor.submit(
                            download_image, session, base_url, save_dir, n, task_id
                        ))
                        n += 1

                    for future in as_completed(futures):
                        if is_task_cancel_requested(task_id):
                            executor.shutdown(wait=False)
                            return False, "任务已取消"

                        success, num, stop_download = future.result()
                        if success:
                            success_count += 1
                        else:
                            if stop_download:
                                stop_flag = True
                                executor.shutdown(wait=False)
                                break

                    time.sleep(random.uniform(*CONFIG['delay_range']))

            if is_task_cancel_requested(task_id):
                executor.shutdown(wait=False)
                return False, "任务已取消"

            if comic_format == 1:
                update_task(task_id, log=f"章节 {chapter} 下载完成，开始生成PDF...")
                success, msg = images_to_pdf(save_dir)
            else:
                update_task(task_id, log=f"章节 {chapter} 下载完成，开始生成CBZ...")
                success, msg = images_to_cbz(save_dir)

            update_task(task_id, log=msg)

            if os.path.isdir(save_dir):
                try:
                    shutil.rmtree(save_dir)
                except Exception as e:
                    update_task(task_id, log=f"删除临时目录时发生错误: {e}")

            if success:
                return True, f"章节 {chapter} 处理完成"
            else:
                return False, f"章节 {chapter} 处理失败: {msg}"

    except Exception as e:
        error_msg = f"章节处理异常：{str(e)}"
        update_task(task_id, log=error_msg)
        return False, error_msg


def extract_image_extension(image_url, default_ext=".jpg"):
    ext = os.path.splitext(urlparse(image_url).path)[1].lower()
    return ext if ext in {'.jpg', '.jpeg', '.png', '.webp', '.gif'} else default_ext


def download_binary_image(image_url, save_path, referer=None, verify=True, retries=None, cancel_checker=None):
    retries = retries or CONFIG['retry_times']
    headers = default_headers(referer=referer)

    for attempt in range(retries):
        if cancel_checker and cancel_checker():
            return False, "cancelled"
        try:
            response = safe_http_get(
                image_url,
                headers=headers,
                stream=True,
                timeout=30,
                verify=verify,
                max_bytes=MAX_IMAGE_RESPONSE_BYTES
            )
            if response.status_code == 200:
                if cancel_checker and cancel_checker():
                    return False, "cancelled"
                write_limited_response_to_file(response, save_path, MAX_IMAGE_RESPONSE_BYTES)
                return True, save_path
            safe_print(f"图片下载失败，状态码: {response.status_code}，第{attempt + 1}次重试")
        except Exception as exc:
            safe_print(f"图片下载异常: {exc}，第{attempt + 1}次重试")

        if attempt < retries - 1:
            if cancel_checker and cancel_checker():
                return False, "cancelled"
            time.sleep(2)

    return False, image_url


def download_chapter_images(image_jobs, save_dir, referer=None, verify=True, max_workers=None, cancel_checker=None):
    ensure_directory(save_dir)
    success_count = 0
    failed_items = []
    max_workers = max_workers or CONFIG['max_workers']

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        job_iter = iter(image_jobs)

        def submit_next():
            try:
                job = next(job_iter)
            except StopIteration:
                return False
            future = executor.submit(
                download_binary_image,
                job['url'],
                os.path.join(save_dir, job['filename']),
                referer,
                verify,
                None,
                cancel_checker
            )
            future_map[future] = job
            return True

        for _ in range(max_workers):
            if not submit_next():
                break

        while future_map:
            if cancel_checker and cancel_checker():
                executor.shutdown(wait=False, cancel_futures=True)
                return success_count, failed_items, True

            done, _ = concurrent.futures.wait(
                future_map.keys(),
                timeout=0.5,
                return_when=concurrent.futures.FIRST_COMPLETED
            )
            if not done:
                continue

            for future in done:
                job = future_map.pop(future)
                success, result = future.result()
                if success:
                    success_count += 1
                elif result != "cancelled":
                    failed_items.append(job['url'])
                submit_next()

    return success_count, failed_items, False


def finalize_downloaded_chapter(save_dir, comic_format):
    if comic_format == 1:
        return images_to_pdf(save_dir)
    return images_to_cbz(save_dir)


def download_provider_chapter(source, chapter, folder, comic_format, task_id):
    provider_fn = providers.resolve_download_chapter(source['provider'])
    if provider_fn:
        return provider_fn(source, chapter, folder, comic_format, task_id)
    return crawl_chapter(chapter['chapter_url'], folder, chapter['order'], comic_format, task_id)


def download_complete_book(url, comic_format, task_id):
    """下载整本漫画（统一站点任务流）"""
    try:
        update_task(task_id, status='running')

        allow_adult = False
        try:
            from mangadock.services.tasks import get_task
            task = get_task(task_id)
            allow_adult = bool(getattr(task, 'allow_adult', False)) if task else False
        except Exception:
            allow_adult = False
        if is_adult_content_blocked(url, allow_adult=allow_adult):
            raise ValueError(ADULT_CONTENT_DISABLED_MESSAGE)

        source = load_comic_source(url)
        folder = source['title']
        chapters = source['chapters']

        update_task(task_id, comic_name=folder)
        update_task(task_id, log=f"开始下载漫画: {folder}")
        update_task(task_id, log=f"站点类型: {source['provider']}")
        update_task(task_id, total_chapters=len(chapters))

        if not chapters:
            raise ValueError("未获取到任何章节")

        ensure_directory(os.path.join(COMIC_ROOT, folder))
        save_comic_mapping(folder, url)
        save_cover_image(
            folder,
            source.get('cover_url'),
            referer=url,
            verify=source.get('cover_verify', True)
        )
        if persist_source_description(folder, source):
            update_task(task_id, log="已保存作品简介")
        elif source.get('description'):
            update_task(task_id, log="作品简介保存失败，可稍后在详情页重试补全")

        for index, chapter in enumerate(chapters, start=1):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            update_task(task_id, log=f"开始处理章节: {chapter['title']}")
            success, message = download_provider_chapter(source, chapter, folder, comic_format, task_id)
            update_task(task_id, log=message)
            if not success:
                update_task(task_id, status='error', end_time=datetime.now(china_tz))
                return

            progress = int((index / len(chapters)) * 100)
            update_task(task_id, completed_chapters=index, progress_percent=progress)

            if source['provider'] == 'baozimhcn':
                time.sleep(2)

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有章节处理完成")
    except Exception as exc:
        error_msg = f"下载过程出错: {exc}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))

def update_comic(comic_name, comic_format, task_id):
    try:
        update_task(task_id, status='running')
        update_task(task_id, comic_name=comic_name)
        update_task(task_id, log=f"开始更新漫画: {comic_name}")

        comic_path = os.path.join(COMIC_ROOT, comic_name)
        if not os.path.isdir(comic_path):
            raise ValueError(f"漫画目录不存在: {comic_path}")

        json_file_path = COMIC_MAPPING_FILE
        if not os.path.exists(json_file_path) or os.path.getsize(json_file_path) == 0:
            raise ValueError("comic.json文件不存在或为空")

        with open(json_file_path, "r", encoding="utf-8") as json_file:
            comic_mapping = json.load(json_file)

        update_url = comic_mapping.get(comic_name)
        if not update_url:
            raise ValueError(f"漫画 {comic_name} 的URL信息不存在于JSON文件中")

        update_task(task_id, url=update_url)
        source = load_comic_source(update_url)
        save_cover_image(
            comic_name,
            source.get('cover_url'),
            referer=update_url,
            verify=source.get('cover_verify', True)
        )
        if persist_source_description(comic_name, source):
            update_task(task_id, log="已同步作品简介")

        existing_outputs = {
            os.path.splitext(filename)[0]
            for filename in os.listdir(comic_path)
            if is_valid_local_chapter_file(filename, (f".{target_extension(comic_format)}",))
        }
        existing_match_bases = get_local_chapter_match_bases(comic_name)
        chapters_to_download = [
            chapter for chapter in source['chapters']
            if not is_existing_local_chapter(chapter, existing_match_bases)
        ]

        update_task(task_id, log=f"远端章节总数: {len(source['chapters'])}")
        update_task(task_id, log=f"目标格式已存在章节数: {len(existing_outputs)}")

        if not chapters_to_download:
            update_task(task_id, log="未找到更新，当前已是最新版本")
            update_task(task_id, status='completed', end_time=datetime.now(china_tz))
            return

        update_task(task_id, total_chapters=len(chapters_to_download))
        update_task(task_id, log=f"找到 {len(chapters_to_download)} 个新章节，开始下载")

        for index, chapter in enumerate(chapters_to_download, start=1):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            update_task(task_id, log=f"开始处理章节: {chapter['title']}")
            success, message = download_provider_chapter(source, chapter, comic_name, comic_format, task_id)
            update_task(task_id, log=message)
            if not success:
                update_task(task_id, status='error', end_time=datetime.now(china_tz))
                return

            progress = int((index / len(chapters_to_download)) * 100)
            update_task(task_id, completed_chapters=index, progress_percent=progress)

            if source['provider'] == 'baozimhcn':
                time.sleep(2)

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有更新章节处理完成")

    except Exception as e:
        error_msg = f"更新过程出错: {str(e)}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))
