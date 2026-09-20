# -*- coding: utf-8 -*-
"""漫小肆（mxs12.cc / wzd1.cc）provider：源解析 + 单章/整本下载。

注意：依赖 download.py 共享管线的函数（images_to_cbz / title /
is_adult_content_blocked）一律在函数体内延迟导入，避免循环导入。
"""
import concurrent.futures
import json
import os
import re
import shutil
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from mangadock.settings import (
    ADULT_CONTENT_DISABLED_MESSAGE,
    COMIC_MAPPING_FILE,
    COMIC_ROOT,
    COVER_ROOT,
    MAX_IMAGE_RESPONSE_BYTES,
    china_tz,
)
from mangadock.services.library import save_comic_description
from mangadock.services.providers.common import extract_description_from_html, safe_print
from mangadock.services.tasks import get_task, is_task_cancel_requested, update_task
from mangadock.utils.cover_image import normalize_cover_bytes
from mangadock.utils.http import safe_http_get, write_limited_response_to_file
from mangadock.utils.media import default_headers, ensure_directory, sanitize_filename


def is_mxs_url(url):
    """判断是否为 mxs12.cc 网站的 URL"""
    return 'mxs12.cc' in url or 'wzd1.cc' in url


def load_mxs_source(url):
    response = safe_http_get(url, headers=default_headers(), timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    title_tag = soup.find("h1") or soup.find("title")
    title = sanitize_filename(title_tag.get_text(strip=True) if title_tag else "未知漫画")

    path_parts = [part for part in urlparse(url).path.split('/') if part]
    if 'book' in path_parts:
        book_index = path_parts.index('book')
        cover_id = path_parts[book_index + 1] if book_index + 1 < len(path_parts) else path_parts[-1]
    else:
        cover_id = path_parts[-1]

    links = soup.select('ul#detail-list-select li a')
    chapters = []
    for index, link in enumerate(links, start=1):
        href = link.get('href')
        if not href:
            continue
        chapters.append({
            'order': index,
            'title': link.get_text(strip=True) or f"第{index}章",
            'filename_base': f"{index:02d}",
            'chapter_url': urljoin(url, href)
        })

    return {
        'provider': 'mxs',
        'title': title,
        'cover_url': f"https://www.wzd1.cc/static/upload/book/{cover_id}/cover.jpg",
        'cover_verify': True,
        'description': extract_description_from_html(response.text, soup=soup),
        'chapters': chapters
    }


def title_mxs(url):
    """获取 mxs12.cc 漫画标题和总章节数"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        response = safe_http_get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html_content = response.text

        if not html_content.strip():
            raise Exception("获取到空的网页内容")

        soup = BeautifulSoup(html_content, "html.parser")
        title_tag = soup.find("h1")

        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else "未知漫画"

        mxs_description = extract_description_from_html(html_content, soup=soup)

        # 获取章节列表
        links = soup.select('ul#detail-list-select li a')
        chapter_max = len(links)

        # 获取封面图片 - 处理 /book/ 路径
        path_parts = url.strip('/').split('/')
        # 查找 book 后的 ID，或者直接取最后一段
        if 'book' in path_parts:
            book_index = path_parts.index('book')
            if book_index + 1 < len(path_parts):
                cid = path_parts[book_index + 1]
            else:
                cid = path_parts[-1]
        else:
            cid = path_parts[-1]
        cover_url = f"https://www.wzd1.cc/static/upload/book/{cid}/cover.jpg"
        try:
            response = safe_http_get(cover_url, timeout=10, max_bytes=MAX_IMAGE_RESPONSE_BYTES)
            if response.status_code == 200:
                cover_path = os.path.join(COVER_ROOT, f"{title}.jpg")
                # 统一转码为 JPEG 落盘，非 JPEG 内容仅在可解码时才写入
                if normalize_cover_bytes(response.content, cover_path):
                    print(f"封面已保存到: {cover_path}")
                    try:
                        from mangadock.utils.cover_enhance import refresh_hero_cover
                        refresh_hero_cover(title)
                    except Exception as enhance_exc:
                        print(f"封面超分缓存失败: {enhance_exc}")
                else:
                    print(f"封面格式无法转码，已跳过保存: {cover_url}")
        except Exception as e:
            print(f"下载封面时出错: {e}")

        safe_print(f"提取到的章节数={chapter_max}")
        if mxs_description:
            save_comic_description(str(title), mxs_description)

        return str(title), chapter_max, html_content

    except Exception as e:
        error_msg = f"获取漫画信息失败: {str(e)}"
        safe_print(error_msg)
        return "未知漫画", 0, ""


def title(url):
    """获取漫画标题和总章节数（自动识别网站；当前仅 mxs 整本流在使用）。"""
    if is_mxs_url(url):
        return title_mxs(url)

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        response = safe_http_get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html_content = response.text

        if not html_content.strip():
            raise Exception("获取到空的网页内容")

        soup = BeautifulSoup(html_content, "html.parser")
        title_tag = soup.find("h1", class_="comics-detail__title")

        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else "未知漫画"

        pattern = r'<meta data-n-head="ssr" data-hid="og:image" name="og:image" content="(https?://[^"]+)"'
        match = re.search(pattern, html_content)
        if match:
            image_url = match.group(1)
            print(f"找到图片URL: {image_url}")
            try:
                response = safe_http_get(image_url, max_bytes=MAX_IMAGE_RESPONSE_BYTES)
                response.raise_for_status()
                # 保存图片到本地（统一转码为 JPEG）
                cover_path = os.path.join(COVER_ROOT, f"{title}.jpg")
                if normalize_cover_bytes(response.content, cover_path):
                    print(f"图片已保存到: {cover_path}")
                    try:
                        from mangadock.utils.cover_enhance import refresh_hero_cover
                        refresh_hero_cover(title)
                    except Exception as enhance_exc:
                        print(f"封面超分缓存失败: {enhance_exc}")
                else:
                    print(f"封面格式无法转码，已跳过保存: {image_url}")
            except Exception as e:
                print(f"下载或保存图片时出错: {e}")

        chapter_max = 0
        chapter_slot = re.search(r'chapter_slot=(\d+)', html_content)
        if chapter_slot:
            chapter_max = int(chapter_slot.group(1))
        if chapter_max == 0:
            chapter_match = re.search(r'共(\d+)话', html_content)
            if chapter_match:
                chapter_max = int(chapter_match.group(1))
        if chapter_max == 0:
            chapter_links = re.findall(r'/comic/chapter/[^/]+/0_(\d+)\.html', html_content)
            if chapter_links:
                chapter_max = max(map(int, chapter_links)) + 1  # 加1因为章节通常从0开始

        safe_print(f"提取到的章节数={chapter_max}")
        safe_print(f"HTML片段包含chapter_slot? {('chapter_slot' in html_content)}")
        cn_description = extract_description_from_html(html_content, soup=soup)
        if cn_description:
            save_comic_description(str(title), cn_description)

        return str(title), chapter_max, html_content

    except Exception as e:
        error_msg = f"获取漫画信息失败: {str(e)}"
        safe_print(error_msg)
        return "未知漫画", 0, ""


def download_image_mxs(session, img_url, save_path, retries=3, cancel_checker=None):
    """下载单张图片（mxs12.cc 专用）"""
    for attempt in range(retries):
        if cancel_checker and cancel_checker():
            return False
        try:
            safe_print(f"正在下载: {os.path.basename(save_path)}")
            with safe_http_get(
                img_url,
                stream=True,
                timeout=30,
                max_bytes=MAX_IMAGE_RESPONSE_BYTES,
                session_obj=session
            ) as response:
                if response.status_code == 200:
                    if cancel_checker and cancel_checker():
                        return False
                    write_limited_response_to_file(response, save_path, MAX_IMAGE_RESPONSE_BYTES)
                    return True
                else:
                    safe_print(f"图片请求失败，状态码: {response.status_code}，第{attempt+1}次重试。")
        except Exception as e:
            safe_print(f"图片请求失败，错误: {str(e)}，第{attempt+1}次重试。")
        if attempt < retries - 1:
            if cancel_checker and cancel_checker():
                return False
            time.sleep(2)
    safe_print(f"图片多次尝试失败: {os.path.basename(save_path)}")
    return False


def download_images_concurrently_mxs(session, img_urls, save_dir, max_workers=2, cancel_checker=None):
    """并发下载图片（mxs12.cc 专用）"""
    os.makedirs(save_dir, exist_ok=True)
    safe_print(f"开始下载 {len(img_urls)} 张图片到: {save_dir}")

    success_count = 0
    failed_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        job_iter = iter(enumerate(img_urls, start=1))

        def submit_next():
            try:
                img_idx, img_url = next(job_iter)
            except StopIteration:
                return False
            img_name = f"{img_idx:02d}.jpg"
            img_path = os.path.join(save_dir, img_name)
            future = executor.submit(
                download_image_mxs,
                session,
                img_url,
                img_path,
                3,
                cancel_checker
            )
            future_map[future] = img_url
            return True

        for _ in range(max_workers):
            if not submit_next():
                break

        while future_map:
            if cancel_checker and cancel_checker():
                executor.shutdown(wait=False, cancel_futures=True)
                return success_count, True

            done, _ = concurrent.futures.wait(
                future_map.keys(),
                timeout=0.5,
                return_when=concurrent.futures.FIRST_COMPLETED
            )
            if not done:
                continue

            for future in done:
                future_map.pop(future, None)
                if future.result():
                    success_count += 1
                else:
                    failed_count += 1
                submit_next()

    safe_print(f"图片下载完成: 成功 {success_count} 张，失败 {failed_count} 张")
    return success_count, False


def crawl_chapter_mxs(chapter_url, folder, chapter, comic_format, task_id):
    """下载单个章节（mxs12.cc 专用）"""
    save_dir = os.path.join(COMIC_ROOT, folder, f"{chapter:02d}")
    os.makedirs(save_dir, exist_ok=True)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0'
    }

    try:
        with requests.Session() as session:
            # 配置会话参数
            session.headers.update(headers)
            session.timeout = 30  # 增加超时时间

            for attempt in range(3):
                if is_task_cancel_requested(task_id):
                    shutil.rmtree(save_dir, ignore_errors=True)
                    return False, "任务已取消"
                try:
                    safe_print(f"正在访问章节 {chapter}: {chapter_url}")
                    response = safe_http_get(chapter_url, timeout=30, session_obj=session)
                    response.raise_for_status()

                    safe_print(f"章节 {chapter} 页面获取成功，状态码: {response.status_code}")
                    safe_print(f"页面大小: {len(response.text)} 字节")

                    soup = BeautifulSoup(response.text, 'html.parser')
                    img_tags = soup.find_all('img', class_='lazy')
                    img_urls = [img['data-original'] for img in img_tags if img.has_attr('data-original')]

                    safe_print(f"章节 {chapter} 找到 {len(img_urls)} 张图片")

                    if img_urls:
                        success_count, cancelled = download_images_concurrently_mxs(
                            session,
                            img_urls,
                            save_dir,
                            cancel_checker=lambda: is_task_cancel_requested(task_id)
                        )
                        if cancelled:
                            shutil.rmtree(save_dir, ignore_errors=True)
                            return False, "任务已取消"
                        safe_print(f"章节 {chapter} 图片下载成功: {success_count} 张")

                        if success_count > 0:
                            if is_task_cancel_requested(task_id):
                                shutil.rmtree(save_dir, ignore_errors=True)
                                return False, "任务已取消"
                            # 章节下载完成后压缩成 CBZ
                            cbz_path = os.path.join(COMIC_ROOT, folder, f"{chapter:02d}.cbz")
                            from mangadock.services.download import images_to_cbz
                            success, msg = images_to_cbz(save_dir)
                            if success:
                                safe_print(f"已压缩为: {os.path.basename(cbz_path)}")
                            else:
                                safe_print(f"压缩失败: {msg}")

                            # 删除原文件夹
                            shutil.rmtree(save_dir)

                            return True, f"章节 {chapter} 下载完成（成功 {success_count} 张）"
                        else:
                            safe_print(f"章节 {chapter} 图片下载全部失败")
                            shutil.rmtree(save_dir)
                    else:
                        safe_print(f"章节 {chapter} 未找到图片")

                except Exception as e:
                    safe_print(f"章节 {chapter} 第 {attempt + 1} 次尝试失败: {str(e)}")
                    if attempt < 2:
                        if is_task_cancel_requested(task_id):
                            shutil.rmtree(save_dir, ignore_errors=True)
                            return False, "任务已取消"
                        time.sleep(3)  # 增加重试间隔
                    else:
                        safe_print(f"章节 {chapter} 三次尝试都失败，放弃")

        return False, f"章节 {chapter} 下载失败"

    except Exception as e:
        safe_print(f"章节 {chapter} 下载异常: {str(e)}")
        return False, f"章节 {chapter} 下载失败: {str(e)}"


def download_mxs_chapter(chapter, folder, comic_format, task_id):
    save_dir = os.path.join(COMIC_ROOT, folder, chapter['filename_base'])

    from mangadock.services.download import (
        download_chapter_images,
        extract_image_extension,
        finalize_downloaded_chapter,
    )

    ensure_directory(save_dir)

    try:
        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        response = safe_http_get(chapter['chapter_url'], headers=default_headers(), timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        image_urls = [
            urljoin(chapter['chapter_url'], img['data-original'])
            for img in soup.find_all('img', class_='lazy')
            if img.has_attr('data-original')
        ]
        if not image_urls:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 未找到图片"

        image_jobs = [
            {
                'url': image_url,
                'filename': f"{index:03d}{extract_image_extension(image_url)}"
            }
            for index, image_url in enumerate(image_urls, start=1)
        ]
        success_count, failed_items, cancelled = download_chapter_images(
            image_jobs,
            save_dir,
            referer=chapter['chapter_url'],
            verify=True,
            cancel_checker=lambda: is_task_cancel_requested(task_id)
        )
        if cancelled:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        if success_count == 0:
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, f"章节 {chapter['title']} 下载失败"

        if is_task_cancel_requested(task_id):
            shutil.rmtree(save_dir, ignore_errors=True)
            return False, "任务已取消"
        success, message = finalize_downloaded_chapter(save_dir, comic_format)
        shutil.rmtree(save_dir, ignore_errors=True)
        if not success:
            return False, message

        if failed_items:
            update_task(task_id, log=f"章节 {chapter['title']} 有 {len(failed_items)} 张图片下载失败")
        return True, f"章节 {chapter['title']} 处理完成"
    except Exception as exc:
        shutil.rmtree(save_dir, ignore_errors=True)
        return False, f"章节 {chapter['title']} 下载失败: {exc}"


def download_complete_book_mxs(url, comic_format, task_id):
    """下载整本漫画（mxs12.cc 专用）"""
    try:
        update_task(task_id, status='running')

        from mangadock.services.download import is_adult_content_blocked
        if is_adult_content_blocked(url):
            raise ValueError(ADULT_CONTENT_DISABLED_MESSAGE)

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        folder, chapter_max, html_content = title(url)
        update_task(task_id, comic_name=folder)
        update_task(task_id, log=f"开始下载漫画: {folder}")
        update_task(task_id, log=f"总章节数: {chapter_max}")

        if chapter_max <= 0:
            update_task(task_id, log="错误：未获取到有效的章节数，无法开始下载")
            update_task(task_id, log=f"请检查URL是否正确: {url}")
            update_task(task_id, status='error')
            return

        update_task(task_id, total_chapters=chapter_max)

        # 解析章节链接
        soup = BeautifulSoup(html_content, 'html.parser')
        links = soup.select('ul#detail-list-select li a')
        base_url = "https://mxs12.cc"
        chapter_urls = [base_url + a['href'] for a in links]

        json_file_path = COMIC_MAPPING_FILE
        try:
            if os.path.exists(json_file_path):
                if os.path.getsize(json_file_path) > 0:
                    with open(json_file_path, "r", encoding="utf-8") as json_file:
                        existing_data = json.load(json_file)
                else:
                    existing_data = {}  # 空文件时初始化空字典
            else:
                existing_data = {}

            existing_data[folder] = url
            with open(json_file_path, "w", encoding="utf-8") as json_file:
                json.dump(existing_data, json_file, ensure_ascii=False, indent=4)
        except json.JSONDecodeError:
            error_msg = f"JSON文件格式错误，已创建新文件: {json_file_path}"
            update_task(task_id, log=error_msg)
            with open(json_file_path, "w", encoding="utf-8") as json_file:
                json.dump({folder: url}, json_file, ensure_ascii=False, indent=4)
        except Exception as e:
            error_msg = f"处理JSON文件失败: {str(e)}"
            update_task(task_id, log=error_msg)

        comic_path = os.path.join(COMIC_ROOT, folder)
        if not os.path.exists(comic_path):
            os.makedirs(comic_path)

        for idx, chapter_url in enumerate(chapter_urls, start=1):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            update_task(task_id, log=f"开始处理第 {idx} 章")
            success, msg = crawl_chapter_mxs(chapter_url, folder, idx, comic_format, task_id)
            update_task(task_id, log=msg)

            if task:
                new_completed = task.completed_chapters + 1
                progress = int((new_completed / chapter_max) * 100)
                update_task(task_id, completed_chapters=new_completed, progress_percent=progress)
            time.sleep(2)  # 减小延迟，避免被封禁

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有章节处理完成")

    except Exception as e:
        error_msg = f"下载过程出错: {str(e)}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))
