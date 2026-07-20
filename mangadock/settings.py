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
COVER_ROOT = os.path.join(BASE_DIR, "static", "cover")
COMIC_MAPPING_FILE = os.path.join(BASE_DIR, "comic.json")
TEMPLATE_FOLDER = os.path.join(BASE_DIR, "templates")
STATIC_FOLDER = os.path.join(BASE_DIR, "static")

BAOZIMH_ORG_IMAGE_HOST = "https://f40-1-4.g-mh.online"
BAOZIMH_ORG_DEFAULT_IMAGE_HOST = "https://t40-1-4.g-mh.online"
BAOZIMH_ORG_ENCODED_IMAGE_TRANSLATION = str.maketrans(
    "_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)

SAFE_HTTP_ALLOWED_HOST_SUFFIXES = (
    '6wm.top',
    'baozimh.com',
    'baozimhcn.com',
    'baozimh.org',
    'bzcdn.net',
    'cnbzmg.com',
    'mgsearcher.com',
    'g-mh.online',
    'jjmhw6.top',
    'mxs12.cc',
    'twbzmg.com',
    'wzd1.cc',
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
