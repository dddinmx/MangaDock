# -*- coding: utf-8 -*-
"""Application settings, constants, and filesystem roots."""
import ipaddress
import os
from datetime import timedelta, timezone

from PIL import Image

china_tz = timezone(timedelta(hours=8))

# Optional reporting
REPORT_URL = os.environ.get("MANGADOCK_REPORT_URL", "").strip()
REPORT_SECRET = os.environ.get("MANGADOCK_REPORT_SECRET", "").strip()
REPORT_INTERVAL = int(os.environ.get("MANGADOCK_REPORT_INTERVAL", "1800"))
ENABLE_REPORT = bool(REPORT_URL and REPORT_SECRET)
APP_VERSION = "5.0"

CONFIG = {
    'max_workers': 2,
    'request_timeout': 20,
    'retry_times': 3,
    'queue_buffer': 10,
    'delay_range': (0.1, 0.5),
}

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PACKAGE_DIR)
COMIC_ROOT = os.path.join(BASE_DIR, "comic")
NOVEL_ROOT = os.path.join(BASE_DIR, "小说")
COVER_ROOT = os.path.join(BASE_DIR, "static", "cover")
NOVEL_COVER_ROOT = os.path.join(COVER_ROOT, "novels")
FANQIE_API_BASE_URL = os.environ.get(
    "MANGADOCK_FANQIE_API_URL", "https://fanqie.dddinmx.cn"
).strip().rstrip("/")
FANQIE_API_TOKEN = os.environ.get("MANGADOCK_FANQIE_API_TOKEN", "").strip()
FANQIE_API_TOKEN_FILE = os.environ.get(
    "MANGADOCK_FANQIE_API_TOKEN_FILE",
    os.path.join(BASE_DIR, "instance", "fanqie_api_token"),
).strip()
FANQIE_API_REGISTRATION_KEY_FILE = os.environ.get(
    "MANGADOCK_FANQIE_REGISTRATION_KEY_FILE",
    os.path.join(BASE_DIR, "instance", "fanqie_registration_key"),
).strip()
FANQIE_API_AUTO_REGISTER = os.environ.get(
    "MANGADOCK_FANQIE_API_AUTO_REGISTER", "true"
).strip().lower() in {"1", "true", "yes", "on"}
FANQIE_API_INSTALLATION_ID = os.environ.get(
    "MANGADOCK_FANQIE_INSTALLATION_ID", ""
).strip()
FANQIE_API_INSTALLATION_ID_FILE = os.environ.get(
    "MANGADOCK_FANQIE_INSTALLATION_ID_FILE",
    os.path.join(BASE_DIR, "instance", "fanqie_installation_id"),
).strip()
FANQIE_API_ALLOW_ANONYMOUS = os.environ.get(
    "MANGADOCK_FANQIE_API_ALLOW_ANONYMOUS", "false"
).strip().lower() in {"1", "true", "yes", "on"}
FANQIE_API_POLL_INTERVAL = max(
    1.0, float(os.environ.get("MANGADOCK_FANQIE_API_POLL_INTERVAL", "2"))
)
# 2026-09-20 code review P2：远端作业轮询的总超时上限。
# 此前只靠「远端状态变化」或「用户手动取消」退出轮询，若远端作业卡在 running
# （worker 失联 / 作业记录丢失），下载 worker 线程会被永久占住，队列其余任务饿死。
FANQIE_API_MAX_POLL_SECONDS = max(
    300, int(os.environ.get("MANGADOCK_FANQIE_API_MAX_POLL_SECONDS", "7200"))
)
FANQIE_API_MAX_ARTIFACT_BYTES = max(
    64 * 1024 * 1024,
    int(os.environ.get("MANGADOCK_FANQIE_API_MAX_ARTIFACT_MB", "2048")) * 1024 * 1024,
)
COMIC_MAPPING_FILE = os.path.join(BASE_DIR, "comic.json")
TEMPLATE_FOLDER = os.path.join(BASE_DIR, "templates")
STATIC_FOLDER = os.path.join(BASE_DIR, "static")

# baozimh.org 当前公开阅读页使用的章节 API 与图片线路。
# 图片路径由 API 返回相对路径；line=2 走 nd2，其余走 nd3。
BAOZIMH_ORG_API_BASE_URL = "https://v2.apikk.top"
BAOZIMH_ORG_IMAGE_HOST = "https://c-nd2-1.6wm.top"
BAOZIMH_ORG_DEFAULT_IMAGE_HOST = "https://c-nd3-1.6wm.top"
BAOZIMH_ORG_ENCODED_IMAGE_TRANSLATION = str.maketrans(
    "_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

# 嬉皮漫畫 (hipmh.com)：Astro 前端 + S3 静态图床。
# 作品页只给 mid；章节列表在 v1 API；阅读页给「章节 API hid」与图床基址；
# 章节图片在 v2 API，images 字段是混淆串（解码实现见 providers/hipmh.py）。
HIPMH_API_BASE_URL = "https://hipapi1.s3file.top"
HIPMH_READER_BASE_URL = "https://reader.hipmh.top"
HIPMH_DEFAULT_IMAGE_HOST = "https://hip-tx-1.s3imgs.top"
HIPMH_SITE_REFERER = "https://m.hipmh.com/"
HIPMH_READER_REFERER = "https://reader.hipmh.top/"
HIPMH_IMAGE_PAYLOAD_PREFIX = "qM9"
HIPMH_IMAGE_PAYLOAD_SUFFIX = "Z7"
HIPMH_IMAGE_PAYLOAD_INNER_MARKER = "Vx"
HIPMH_IMAGE_PAYLOAD_SPLIT_MARKER = "pL0"
HIPMH_ENCODED_IMAGE_TRANSLATION = str.maketrans(
    "_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

# 漫画柜 (manhuagui.com)：章节页内联 Dean Edwards packer 混淆的图片配置（纯静态）。
# 图片直链 https://{图床}{path}{file}?e=&m= 的签名由服务端下发，必须带 Referer。
# 注意：图床 hamreus.com 直连不可达，必须走代理（由 start-macos.sh 的代理自检保证）。
MANHUAGUI_IMAGE_HOSTS = ('us.hamreus.com', 'us2.hamreus.com', 'us3.hamreus.com')
MANHUAGUI_IMAGE_REFERER_FALLBACK = 'https://www.manhuagui.com/'

SAFE_HTTP_ALLOWED_HOST_SUFFIXES = (
    # Horizontal artwork metadata/image endpoints used by homepage banner lookup.
    'anilist.co',
    'kitsu.io',
    'webtoons.com',
    'webtoon-phinf.pstatic.net',
    '6wm.top',
    'baozimh.com',
    'baozimhcn.com',
    'baozimh.org',
    'apikk.top',
    'bzcdn.net',
    'cnbzmg.com',
    'mgsearcher.com',
    'g-mh.online',
    'jjmhw6.top',
    'jjmhw8.top',
    'mxs12.cc',
    'twbzmg.com',
    'wzd1.cc',
    # 嬉皮漫畫：主站 hipmh.com、阅读器 reader.hipmh.top、API hipapi1.s3file.top、图床 *.s3imgs.top
    'hipmh.com',
    'hipmh.top',
    's3file.top',
    's3imgs.top',
    # 漫画柜：主站 manhuagui.com、封面 CDN cf.mhgui.com、图床 *.hamreus.com
    'manhuagui.com',
    'mhgui.com',
    'hamreus.com',
    # 瓜子漫画：主站 guazimanhua.com、图床 img.guazicdn.com（无防盗链）
    'guazimanhua.com',
    'guazicdn.com',
)
SAFE_HTTP_ALLOWED_PROXY_NETWORKS = (
    ipaddress.ip_network('198.18.0.0/15'),
)

MAX_HTML_RESPONSE_BYTES = int(os.environ.get('MANGADOCK_MAX_HTML_MB', '8')) * 1024 * 1024
MAX_IMAGE_RESPONSE_BYTES = int(os.environ.get('MANGADOCK_MAX_IMAGE_MB', '30')) * 1024 * 1024
MAX_ARCHIVE_ENTRY_BYTES = int(os.environ.get('MANGADOCK_MAX_ARCHIVE_ENTRY_MB', '50')) * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = int(os.environ.get('MANGADOCK_MAX_ARCHIVE_TOTAL_MB', '500')) * 1024 * 1024
MAX_ARCHIVE_PAGES = int(os.environ.get('MANGADOCK_MAX_ARCHIVE_PAGES', '1200'))
MAX_PDF_PAGES = int(os.environ.get('MANGADOCK_MAX_PDF_PAGES', '1200'))
PDF_TOOL_TIMEOUT_SECONDS = int(os.environ.get('MANGADOCK_PDF_TOOL_TIMEOUT', '30'))
Image.MAX_IMAGE_PIXELS = int(os.environ.get('MANGADOCK_MAX_IMAGE_PIXELS', '36000000'))

LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_DURATION = timedelta(minutes=15)

UPDATE_CHECK_INTERVAL = timedelta(days=1)
WORKER_POLL_INTERVAL_SECONDS = 2
WORKER_SCHEDULE_HEARTBEAT_SECONDS = 600
TASK_WORKER_COUNT = 2
COMIC_UPDATE_MODE_SETTING_KEY = 'comic_update_mode'
COMIC_UPDATE_MODE_MANUAL = 'manual'
COMIC_UPDATE_MODE_AUTO = 'auto'
COMIC_UPDATE_MODES = {COMIC_UPDATE_MODE_MANUAL, COMIC_UPDATE_MODE_AUTO}

# 18+ 内容来源（漫小肆韩漫 / mxs12.cc）全局开关，默认关闭。
# 关闭时：下载页不显示漫小肆入口，且后端拒绝一切 mxs 来源的下载/更新。
ADULT_CONTENT_SETTING_KEY = 'adult_content_enabled'
ADULT_CONTENT_DISABLED_MESSAGE = '漫小肆韩漫属于 18+ 来源，请先在「设置」页开启 18+ 开关'
