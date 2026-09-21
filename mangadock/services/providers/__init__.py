# -*- coding: utf-8 -*-
"""漫画源 provider 注册表。

新增源只需两步：
1. 新建 ``providers/<name>.py``（或复用既有模块），实现：
   - ``load_source(url) -> dict``（必选）：返回 title/cover_url/description/chapters 等；
   - ``download_chapter(source, chapter, folder, comic_format, task_id)``（可选）：
     缺省时 download.py 会走通用 crawl_chapter 兜底。
2. 在下方 REGISTRY 登记一条。

识别与分发全部经注册表解析（importlib 懒加载，避免循环导入）：
- ``detect_provider_name``：host 子串或自定义 matcher，先注册先匹配；
- ``is_supported_url``：整 URL 正则或自定义 matcher（用户入口校验用）；
- ``load_source`` / ``resolve_download_chapter``：懒加载对应实现。
"""
import importlib
import re
from urllib.parse import urlparse

# 有序注册：fanqie 用自定义 matcher 判定（非 host 匹配），必须最先注册。
REGISTRY = []


def register(name, *, hosts=(), pattern=None, matches=None, module=None,
             load_source=None, download_chapter=None, is_adult=False):
    REGISTRY.append({
        'name': name,
        'hosts': tuple(hosts),
        'pattern': pattern,
        'matches': matches,
        'module': module,
        'load_source': load_source,
        'download_chapter': download_chapter,
        'is_adult': is_adult,
    })


register(
    'fanqie',
    matches='mangadock.services.fanqie_comics:is_fanqie_comic_target',
    module='mangadock.services.fanqie_comics',
    load_source='load_fanqie_comic_source',
)
register(
    'mxs',
    hosts=('mxs12.cc', 'wzd1.cc'),
    pattern=r'^https://(?:www\.)?(?:mxs12|wzd1)\.cc/(?:book/)?[^/?#]+/?$',
    module='mangadock.services.providers.mxs',
    load_source='load_mxs_source',
    download_chapter='download_mxs_chapter',
    is_adult=True,
)
register(
    'baozimh_org',
    hosts=('baozimh.org',),
    pattern=r'^https://(?:www\.)?baozimh\.org/manga/[^/?#]+/?$',
    module='mangadock.services.providers.baozimh_org',
    load_source='load_baozimh_org_source',
    download_chapter='download_baozimh_org_chapter',
)
register(
    'manhuagui',
    hosts=('manhuagui.com',),
    pattern=r'^https?://(?:www\.)?manhuagui\.com/comic/\d+(?:/\d+\.html)?/?$',
    module='mangadock.services.manhuagui',
    load_source='load_manhuagui_source',
    download_chapter='download_manhuagui_chapter',
)
register(
    'baozimhcn',
    hosts=('baozimhcn.com', 'baozimh.com'),
    pattern=r'^https://(?:cn\.baozimhcn\.com|(?:www\.)?baozimh\.com)/comic/[^/?#]+/?$',
    module='mangadock.services.providers.baozimhcn',
    load_source='load_baozimhcn_source',
)
register(
    'hipmh',
    hosts=('hipmh.com',),
    pattern=r'^https?://(?:m\.|www\.)?hipmh\.com/(?:[a-z]{2}/)?works/[^/?#]+/?$',
    module='mangadock.services.providers.hipmh',
    load_source='load_hipmh_source',
    download_chapter='download_hipmh_chapter',
)


def _resolve(ref):
    """解析 'package.module:attr' 形式的懒加载引用。"""
    module_path, _, attr = ref.partition(':')
    return getattr(importlib.import_module(module_path), attr)


def _entry(name):
    for entry in REGISTRY:
        if entry['name'] == name:
            return entry
    raise ValueError(f"未注册的 provider: {name}")


def _entry_matches(entry, url):
    if entry['matches']:
        return bool(_resolve(entry['matches'])(url))
    host = urlparse(url or '').netloc.lower()
    return any(host_suffix in host for host_suffix in entry['hosts'])


def _entry_for(url):
    for entry in REGISTRY:
        if _entry_matches(entry, url):
            return entry
    raise ValueError("暂不支持该站点")


def get_entry(name):
    """按名取注册项（供 download.py 管线读取 provider 元数据）。"""
    return _entry(name)


def detect_provider_name(url):
    """识别 URL 所属 provider，未注册站点抛 ValueError。"""
    return _entry_for(url)['name']


def is_adult_provider(name):
    return bool(_entry(name)['is_adult'])


def is_supported_url(url):
    """用户入口的整 URL 校验：正则或自定义 matcher 命中即支持。"""
    for entry in REGISTRY:
        if entry['matches']:
            if _resolve(entry['matches'])(url):
                return True
        elif entry['pattern'] and re.match(entry['pattern'], url or ''):
            return True
    return False


def load_source(url):
    """按注册表分发到对应 provider 的 load_source。"""
    entry = _entry_for(url)
    return _resolve(f"{entry['module']}:{entry['load_source']}")(url)


def resolve_download_chapter(provider_name):
    """返回 provider 的单章下载函数；未实现时返回 None（由调用方兜底）。"""
    entry = _entry(provider_name)
    if entry['download_chapter']:
        return _resolve(f"{entry['module']}:{entry['download_chapter']}")
    return None
