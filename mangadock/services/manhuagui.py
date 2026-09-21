# -*- coding: utf-8 -*-
"""漫画柜 (manhuagui.com) provider —— 纯 HTTP，不需要无头浏览器。

链路（2026-09-20 实测，服务端全部静态下发）：
  章节页 HTML
    └─ window["\\x65\\x76\\x61\\x6c"]( function(p,a,c,k,e,d){...}( payload, a, c, k, 0, {} ) )
         ├─ payload : JS 源码，内嵌图片配置 SMH.imgData({...})
         └─ k       : '<LZStringBase64>'['\\x73\\x70\\x6c\\x69\\x63']('\\x7c')
                      站点自定义 String.prototype.splic
                      = LZString.decompressFromBase64(this).split(f)
                      —— **不要按常规 split 理解**，这是最容易踩空的一处
    └─ 解包后：{ bid, bname, cid, cname, files[], path, sl:{e,m}, nextId, prevId }

图片直链： https://{us|us2|us3}.hamreus.com{path}{file}?e={sl.e}&m={sl.m}
  - 必须带 Referer: 章节页 URL（防盗链）
  - sl.e / sl.m 由服务端下发（e 是过期 unix 时间戳，约 8 天），客户端不计算
  - 主站 manhuagui.com 可直连；图床 hamreus.com 直连不通，必须走代理

章节列表：书籍页 /comic/{bid}/ 静态 HTML。站点把章节分散在若干 <h4> 分节里
（单行本 / 单话 / 番外篇），**DOM 顺序不是阅读顺序**，且同一分节 id 会重复、
还会混入别的书的跨书链接。因此：按 bid 过滤 → 按 cid 升序排序（实测等于阅读顺序）。
"""
import json
import os
import re
import urllib.parse

from bs4 import BeautifulSoup

from mangadock.settings import (
    COMIC_ROOT,
    MANHUAGUI_IMAGE_HOSTS,
    MANHUAGUI_IMAGE_REFERER_FALLBACK,
)
from mangadock.utils.http import safe_http_get
from mangadock.utils.media import default_headers, ensure_directory, sanitize_filename

MANHUAGUI_BOOK_URL_PATTERN = re.compile(r'^https?://(?:www\.)?manhuagui\.com/comic/(\d+)/?')
MANHUAGUI_CHAPTER_URL_PATTERN = re.compile(
    r'^https?://(?:www\.)?manhuagui\.com/comic/(\d+)/(\d+)\.html'
)

# ------------------------------------------------------------------ LZString
_B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


def _b64_val(ch):
    index = _B64_ALPHABET.find(ch)
    return index if index >= 0 else 0


def lz_decompress_from_base64(data):
    """等价于 LZString.decompressFromBase64。"""
    if not data:
        return ""
    return _lz_decompress(len(data), 32, lambda i: _b64_val(data[i]) if i < len(data) else 0)


def _lz_decompress(length, reset_value, get_next_value):
    dictionary = {0: 0, 1: 1, 2: 2}
    enlarge_in, dict_size, num_bits = 4, 4, 3
    entry = ""
    result = []
    st = {"val": get_next_value(0), "pos": reset_value, "idx": 1}

    def read_bits(count):
        bits, maxpower, power = 0, 2 ** count, 1
        while power != maxpower:
            resb = st["val"] & st["pos"]
            st["pos"] >>= 1
            if st["pos"] == 0:
                st["pos"] = reset_value
                st["val"] = get_next_value(st["idx"])
                st["idx"] += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1
        return bits

    nxt = read_bits(2)
    if nxt == 0:
        current = chr(read_bits(8))
    elif nxt == 1:
        current = chr(read_bits(16))
    elif nxt == 2:
        return ""
    else:
        return None

    dictionary[3] = current
    w = current
    result.append(current)

    while True:
        if st["idx"] > length:
            return None
        cc = read_bits(num_bits)
        if cc == 0:
            dictionary[dict_size] = chr(read_bits(8))
            cc = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif cc == 1:
            dictionary[dict_size] = chr(read_bits(16))
            cc = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif cc == 2:
            return "".join(result)

        if enlarge_in == 0:
            enlarge_in = 2 ** num_bits
            num_bits += 1

        if cc in dictionary:
            entry = dictionary[cc]
        elif cc == dict_size:
            entry = w + w[0]
        else:
            return None

        result.append(entry)
        dictionary[dict_size] = w + entry[0]
        dict_size += 1
        enlarge_in -= 1
        w = entry

        if enlarge_in == 0:
            enlarge_in = 2 ** num_bits
            num_bits += 1


# ------------------------------------------------------- Dean Edwards packer
_D36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _to36(number):
    if number == 0:
        return "0"
    out = ""
    while number:
        out = _D36[number % 36] + out
        number //= 36
    return out


def _packer_token(index, base):
    """复刻 packer 的 token 编码。

    坑：index 小于基数时**仍要输出当前位**，直接返回空串会让整轮替换失效。
    """
    if index == 0 and base > 0:
        return _to36(0)
    prefix = "" if index < base else _packer_token(index // base, base)
    remainder = index % base
    return prefix + (chr(remainder + 29) if remainder > 35 else _to36(remainder))


def unpack_packer_payload(payload, base, count, words):
    out = payload
    for index in range(count - 1, -1, -1):
        if index < len(words) and words[index]:
            token = _packer_token(index, base)
            if token:
                out = re.sub(
                    r"\b" + re.escape(token) + r"\b",
                    lambda _match, word=words[index]: word,
                    out,
                )
    return out


def _find_balanced_end(source, start):
    """从 start（指向 '('）起做括号配平。"""
    depth, in_string, escaped = 0, None, False
    for position in range(start, len(source)):
        char = source[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in "\"'":
            in_string = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return position
    return -1


def extract_chapter_config(html):
    """从章节页 HTML 解出图片配置 dict。"""
    match = re.search(r'window\["\\x65\\x76\\x61\\x6c"\]\(', html)
    if not match:
        raise ValueError("漫画柜章节页缺少图片配置入口（站点结构可能已变）")
    start = match.end() - 1
    end = _find_balanced_end(html, start)
    if end < 0:
        raise ValueError("漫画柜章节页图片配置括号不配平")
    expression = html[start:end + 1]

    params = re.search(r"',(\d+),(\d+),'", expression)
    if not params:
        raise ValueError("漫画柜章节页 packer 参数缺失")
    payload = expression[expression.index("}('") + 3:params.start()]
    base, count = int(params.group(1)), int(params.group(2))
    rest = expression[params.end():]
    encoded_words = rest[:rest.rfind("'[")]

    words = lz_decompress_from_base64(encoded_words).split("|")
    unpacked = unpack_packer_payload(payload, base, count, words)

    config_match = re.search(r"\{.*\}", unpacked, re.S)
    if not config_match:
        raise ValueError("漫画柜章节页解包后未找到图片配置")
    config = json.loads(config_match.group(0))
    if not isinstance(config, dict) or not config.get("files"):
        raise ValueError("漫画柜章节图片配置为空")
    return config


# ------------------------------------------------------------------ 书籍页
_H4_SECTION_PATTERN = re.compile(r'<h4[^>]*>\s*<span>([^<]*)</span>\s*</h4>')
_CHAPTER_LINK_PATTERN = re.compile(
    r'href="/comic/(\d+)/(\d+)\.html"[^>]*title="([^"]*)"'
)
_SERIAL_SECTION_PATTERN = re.compile(r'[话回]')


def _split_sections(html):
    """按 <h4><span>标题</span></h4> 把书籍页切成若干分节。"""
    marks = [(match.group(1).strip(), match.start()) for match in _H4_SECTION_PATTERN.finditer(html)]
    if not marks:
        return [('', html)]
    bounds = [position for _name, position in marks] + [len(html)]
    return [
        (name, html[bounds[index]:bounds[index + 1]])
        for index, (name, _position) in enumerate(marks)
    ]


def _collect_section_chapters(section_html, book_id):
    """提取某一分节里属于本书的章节链接（按 cid 去重，升序排序）。

    坑：站点在分节里会混入**别的书**的链接（番外/短篇/推荐位），
    必须按 bid 过滤，否则会把别人的章节下到本书目录里。
    """
    target = str(book_id)
    chapters = {}
    for bid, cid, title in _CHAPTER_LINK_PATTERN.findall(section_html):
        if bid != target:
            continue
        chapters.setdefault(int(cid), (title or '').strip())
    return sorted(chapters.items())


def parse_book_page(html, book_id):
    """解析书籍页，返回 {title, cover_url, description, chapters, section}。"""
    soup = BeautifulSoup(html, "html.parser")

    title_node = soup.select_one('.book-title h1')
    title = sanitize_filename(title_node.get_text(strip=True) if title_node else '')
    if not title:
        page_title = soup.find('title')
        title = sanitize_filename(
            re.split(r'[_\-|]', page_title.get_text(strip=True))[0] if page_title else ''
        )
    if not title:
        raise ValueError("漫画柜未解析出漫画名")

    cover_node = soup.select_one('.hcover img')
    cover_url = None
    if cover_node and cover_node.get('src'):
        cover_url = urljoin_protocol(cover_node.get('src'))

    description = ''
    for selector in ('#intro-all', '#intro-cut', '.book-intro'):
        node = soup.select_one(selector)
        if not node:
            continue
        text = re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip()
        if text:
            description = text
            break
    description = sanitize_description(description)

    sections = []
    for name, body in _split_sections(html):
        chapters = _collect_section_chapters(body, book_id)
        if chapters:
            sections.append((name, chapters))

    if not sections:
        raise ValueError("漫画柜未解析出任何章节")

    serial_sections = [item for item in sections if _SERIAL_SECTION_PATTERN.search(item[0])]
    if serial_sections:
        chosen_name, chosen = max(serial_sections, key=lambda item: len(item[1]))
    else:
        chosen_name, chosen = max(sections, key=lambda item: len(item[1]))

    merged = {}
    for _name, chapters in sections:
        for cid, chapter_title in chapters:
            merged.setdefault(cid, chapter_title)
    if len(chosen) < 3 and len(merged) > len(chosen):
        # 连载分节异常稀疏时退回全书合并列表
        chosen_name, chosen = '全部', sorted(merged.items())

    result_chapters = []
    for order, (cid, chapter_title) in enumerate(chosen, start=1):
        display_title = chapter_title or f"第{order}话"
        result_chapters.append({
            'order': order,
            'title': display_title,
            'filename_base': f"{order:04d}_{sanitize_filename(display_title)}",
            'chapter_id': cid,
            'chapter_url': f"https://www.manhuagui.com/comic/{book_id}/{cid}.html",
        })

    return {
        'title': title,
        'cover_url': cover_url,
        'description': description,
        'chapters': result_chapters,
        'section': chosen_name or '默认',
    }


def urljoin_protocol(url):
    if not url:
        return None
    url = url.strip()
    if url.startswith('//'):
        return 'https:' + url
    return url


def sanitize_description(text):
    if not text:
        return ''
    text = text.strip()
    for junk in ('展开详情', '收起详情'):
        text = text.replace(junk, '')
    text = re.sub(r'\s+', ' ', text).strip()
    # 站点把截断版与展开版都塞在简介块里，会出现整段重复
    half = len(text) // 2
    if half > 8 and text[:half].strip() == text[half:].strip():
        text = text[:half].strip()
    return text


# ---------------------------------------------------------------- 图片直链
def extract_book_id(url):
    match = MANHUAGUI_BOOK_URL_PATTERN.match(url or '')
    if not match:
        match = MANHUAGUI_CHAPTER_URL_PATTERN.match(url or '')
    return int(match.group(1)) if match else None


def is_manhuagui_url(url):
    return bool(
        MANHUAGUI_BOOK_URL_PATTERN.match(url or '')
        or MANHUAGUI_CHAPTER_URL_PATTERN.match(url or '')
    )


def normalize_book_url(url):
    book_id = extract_book_id(url)
    if not book_id:
        return None
    return f"https://www.manhuagui.com/comic/{book_id}/"


def build_image_urls(config, host):
    signature = config.get('sl') or {}
    path = config.get('path') or ''
    suffix = f"?e={signature.get('e', '')}&m={signature.get('m', '')}"
    urls = []
    for filename in config.get('files') or []:
        if not filename:
            continue
        raw = f"https://{host}{path}{filename}{suffix}"
        urls.append(urllib.parse.quote(raw, safe=":/?=&%~"))
    return urls


def image_extension(filename, default='.jpg'):
    extension = os.path.splitext(filename or '')[1].lower()
    return extension if extension in {'.jpg', '.jpeg', '.png', '.webp', '.gif'} else default


def pick_image_host(config, referer):
    """按候选顺序探测可用图床（带一次小请求预检，避免整章下载失败）。"""
    signature = config.get('sl') or {}
    first_file = (config.get('files') or [None])[0]
    if not first_file:
        raise ValueError("漫画柜章节图片列表为空")
    last_error = None
    for host in MANHUAGUI_IMAGE_HOSTS:
        probe_url = build_image_urls({'files': [first_file], 'path': config.get('path'),
                                      'sl': signature}, host)[0]
        try:
            response = safe_http_get(
                probe_url,
                headers=default_headers(referer=referer),
                timeout=20,
                stream=True,
                verify=False,
            )
            if response.status_code == 200:
                response.close()
                return host
            response.close()
            last_error = f"{host} 返回 {response.status_code}"
        except Exception as exc:  # noqa: BLE001 - 逐个候选探测，失败继续
            last_error = f"{host}: {exc}"
    raise ValueError(f"漫画柜图床均不可用（{last_error}）")


def fetch_html(url, referer=None, timeout=30):
    response = safe_http_get(
        url,
        headers=default_headers(referer=referer),
        timeout=timeout,
        verify=False,
    )
    response.raise_for_status()
    response.encoding = 'utf-8'
    return response.text


# ---------------------------------------------------------------- provider
def load_manhuagui_source(url):
    book_url = normalize_book_url(url)
    if not book_url:
        raise ValueError("漫画柜链接格式无效")
    book_id = extract_book_id(url)
    html = fetch_html(book_url, referer='https://www.manhuagui.com/')
    parsed = parse_book_page(html, book_id)
    return {
        'provider': 'manhuagui',
        'title': parsed['title'],
        'cover_url': parsed['cover_url'],
        'cover_verify': True,
        'description': parsed['description'],
        'chapters': parsed['chapters'],
        'book_url': book_url,
        'book_id': book_id,
        'section': parsed['section'],
    }


def download_manhuagui_chapter(source, chapter, folder, comic_format, task_id):
    from mangadock.services.download import (
        download_chapter_images,
        finalize_downloaded_chapter,
        incomplete_chapter_reason,
        is_task_cancel_requested,
        update_task,
    )

    save_dir = os.path.join(COMIC_ROOT, folder, chapter['filename_base'])
    ensure_directory(save_dir)
    referer = chapter.get('chapter_url') or source.get('book_url') or MANHUAGUI_IMAGE_REFERER_FALLBACK

    try:
        if is_task_cancel_requested(task_id):
            _cleanup(save_dir)
            return False, "任务已取消"

        chapter_html = fetch_html(referer, referer=source.get('book_url'))
        config = extract_chapter_config(chapter_html)

        files = config.get('files') or []
        if not files:
            _cleanup(save_dir)
            return False, f"章节 {chapter['title']} 未找到图片"

        host = pick_image_host(config, referer)
        image_urls = build_image_urls(config, host)
        image_jobs = [
            {
                'url': image_url,
                'filename': f"{index:03d}{image_extension(files[index - 1])}",
            }
            for index, image_url in enumerate(image_urls, start=1)
        ]

        success_count, failed_items, cancelled = download_chapter_images(
            image_jobs,
            save_dir,
            referer=referer,
            verify=False,
            max_workers=5,
            cancel_checker=lambda: is_task_cancel_requested(task_id),
        )
        if cancelled:
            _cleanup(save_dir)
            return False, "任务已取消"
        # 2026-09-20 code review P2：不能只看 success_count == 0，
        # 部分成功会 finalize 出缺页 CBZ/PDF 且更新检查认为已是最新。
        incomplete_reason = incomplete_chapter_reason(success_count, len(image_jobs))
        if incomplete_reason:
            _cleanup(save_dir)
            return False, f"章节 {chapter['title']} 下载失败：{incomplete_reason}"

        if is_task_cancel_requested(task_id):
            _cleanup(save_dir)
            return False, "任务已取消"

        success, message = finalize_downloaded_chapter(save_dir, comic_format)
        _cleanup(save_dir)
        if not success:
            return False, message

        if failed_items:
            update_task(task_id, log=f"章节 {chapter['title']} 有 {len(failed_items)} 张图片下载失败")
        return True, f"章节 {chapter['title']} 处理完成"
    except Exception as exc:  # noqa: BLE001 - 与其它 provider 一致，失败转错误信息
        _cleanup(save_dir)
        return False, f"章节 {chapter['title']} 下载失败: {exc}"


def _cleanup(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)
