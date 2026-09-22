# -*- coding: utf-8 -*-
"""Safe outbound HTTP helpers (SSRF protections)."""
import ipaddress
import logging
import os
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

from mangadock.settings import (
    CONFIG,
    MAX_HTML_RESPONSE_BYTES,
    SAFE_HTTP_ALLOWED_HOST_SUFFIXES,
    SAFE_HTTP_ALLOWED_PROXY_NETWORKS,
)

logger = logging.getLogger(__name__)

# === 上游网络层重试（2026-09-21 事故：元数据抓取阶段没有重试） =========================
# 事故现象：《恨不得吃掉妳》的更新任务在 11 秒内变成 error，日志只有
#   「更新过程出错: ('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))」
# 根因：`load_comic_source()` 走的是本函数的**非流式**分支，而旧实现里那个
#   `for _ in range(4)` 只用于「跟随重定向」，**不处理任何网络异常** —— 上游（尤其经
#   本机代理 127.0.0.1:7890 的 hipmh 系域名）偶发 ConnectionReset / SSLEOFError /
#   ReadTimeout 时，异常直接穿透到 download.py 的 `except Exception`，整个任务报废。
#   而图片下载之所以只丢几张图，是因为 `download_binary_image()` **自带外层重试**。
# 修法：非流式请求在此补网络层重试（指数退避）；流式（图片）请求保持 retries=1，
#   否则重试次数会与外层相乘（3×3=9 次/张图），真正挂掉时把失败代价放大数倍。
SAFE_HTTP_NETWORK_RETRIES = 3
SAFE_HTTP_RETRY_BACKOFF_SECONDS = (1.0, 3.0)
SAFE_HTTP_MAX_REDIRECTS = 4
SAFE_HTTP_DNS_RETRIES = 3
SAFE_HTTP_DNS_BACKOFF_SECONDS = (0.5, 1.5)

# 这几类都是「连不上/连接被打断」，属于瞬时故障，重试才有意义。
# 注意 SSLError、ProxyError 都是 ConnectionError 的子类，一并覆盖；
# 而 HTTP 4xx/5xx 由调用方按状态码处理，不在此处重试。
_RETRYABLE_NETWORK_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _retry_delay(backoff_seconds, attempt_index):
    return backoff_seconds[min(attempt_index, len(backoff_seconds) - 1)]


def _send_get_with_retries(requester, url, headers, timeout, stream, verify, retries):
    """发一次 GET（不跟随重定向），网络类异常按 retries 退避重试。"""
    last_error = None
    for attempt in range(max(1, retries)):
        try:
            return requester.get(
                url,
                headers=headers,
                timeout=timeout,
                stream=stream,
                verify=verify,
                allow_redirects=False
            )
        except Exception as exc:
            if not isinstance(exc, _RETRYABLE_NETWORK_ERRORS) or attempt >= retries - 1:
                raise
            last_error = exc
            delay = _retry_delay(SAFE_HTTP_RETRY_BACKOFF_SECONDS, attempt)
            logger.warning(
                '上游请求瞬时失败（%s: %s），%.1fs 后重试 %d/%d：%s',
                type(exc).__name__, exc, delay, attempt + 1, retries - 1, url
            )
            time.sleep(delay)
    raise last_error


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def should_verify_upstream_tls(requested_verify=True):
    if requested_verify is False and env_flag('MANGADOCK_ALLOW_INSECURE_UPSTREAM', False):
        return False
    return True


def is_allowed_upstream_host(hostname):
    normalized_host = (hostname or '').strip().lower().rstrip('.')
    return any(
        normalized_host == suffix or normalized_host.endswith(f'.{suffix}')
        for suffix in SAFE_HTTP_ALLOWED_HOST_SUFFIXES
    )


def is_public_ip_address(ip_value):
    try:
        ip_address = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    return not (
        ip_address.is_private
        or ip_address.is_loopback
        or ip_address.is_link_local
        or ip_address.is_multicast
        or ip_address.is_reserved
        or ip_address.is_unspecified
    )


def is_safe_resolved_upstream_address(hostname, ip_value):
    if is_public_ip_address(ip_value):
        return True
    try:
        ip_address = ipaddress.ip_address(ip_value)
    except ValueError:
        return False
    return (
        is_allowed_upstream_host(hostname)
        and any(ip_address in network for network in SAFE_HTTP_ALLOWED_PROXY_NETWORKS)
    )


def validate_safe_upstream_url(url):
    parsed = urlparse(url or '')
    if parsed.scheme != 'https':
        raise ValueError('仅允许访问 HTTPS 上游地址')
    if not parsed.hostname or not is_allowed_upstream_host(parsed.hostname):
        raise ValueError('上游地址域名不在允许列表中')

    try:
        literal_ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_ip = None
    if literal_ip and not is_public_ip_address(str(literal_ip)):
        raise ValueError('不允许访问内网或保留地址')

    # DNS 抖动与连接抖动是同一类瞬时故障：一次解析失败同样会毁掉整个任务，
    # 所以这里也退避重试，耗尽后才按原语义抛「上游域名无法解析」。
    resolved_addresses = set()
    for dns_attempt in range(SAFE_HTTP_DNS_RETRIES):
        try:
            resolved_addresses = {
                result[4][0]
                for result in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            }
            break
        except socket.gaierror as exc:
            if dns_attempt >= SAFE_HTTP_DNS_RETRIES - 1:
                raise ValueError('上游域名无法解析') from exc
            delay = _retry_delay(SAFE_HTTP_DNS_BACKOFF_SECONDS, dns_attempt)
            logger.warning(
                '上游域名解析失败（%s），%.1fs 后重试 %d/%d：%s',
                exc, delay, dns_attempt + 1, SAFE_HTTP_DNS_RETRIES - 1, parsed.hostname
            )
            time.sleep(delay)

    if not resolved_addresses or any(
        not is_safe_resolved_upstream_address(parsed.hostname, address)
        for address in resolved_addresses
    ):
        raise ValueError('上游域名解析到不安全地址')

    return url


def enforce_response_size(response, max_bytes, stream=False):
    if not max_bytes:
        return
    content_length = response.headers.get('Content-Length')
    if content_length:
        try:
            parsed_content_length = int(content_length)
        except ValueError:
            raise ValueError('上游响应大小无效')
        if parsed_content_length > max_bytes:
            raise ValueError('上游响应超过大小限制')
    if not stream and len(response.content) > max_bytes:
        raise ValueError('上游响应超过大小限制')


def safe_http_get(url, headers=None, timeout=None, stream=False, verify=True, max_bytes=MAX_HTML_RESPONSE_BYTES, session_obj=None, retries=None):
    current_url = validate_safe_upstream_url(url)
    requester = session_obj or requests
    timeout = timeout or CONFIG['request_timeout']
    verify = should_verify_upstream_tls(verify)
    if retries is None:
        # 非流式 = 元数据/HTML/图片列表，调用方没有别的重试兜底 → 这里重试；
        # 流式 = 图片字节，调用方 download_binary_image 已重试 → 不叠加。
        retries = 1 if stream else SAFE_HTTP_NETWORK_RETRIES

    for _ in range(SAFE_HTTP_MAX_REDIRECTS):
        response = _send_get_with_retries(
            requester, current_url, headers, timeout, stream, verify, retries
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get('Location')
            response.close()
            if not location:
                raise ValueError('上游重定向缺少 Location')
            current_url = validate_safe_upstream_url(urljoin(current_url, location))
            continue
        enforce_response_size(response, max_bytes, stream=stream)
        return response

    raise ValueError('上游重定向次数过多')


def write_limited_response_to_file(response, output_path, max_bytes):
    written = 0
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    try:
        with open(output_path, 'wb') as output_file:
            for chunk in response.iter_content(8192):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError('下载内容超过大小限制')
                output_file.write(chunk)
        # 2026-09-20 code review P2：之前写 0 字节也当成功返回，调用方按「已成功」
        # 计数，于是空响应 / 风控页 / SMB 截断都会被静默当成有效图片。
        if written <= 0:
            raise ValueError('上游返回了空响应体')
        # 落盘字节数复核：SMB 上 stat 读回可能有短暂滞后，所以给几次重试机会，
        # 只有始终不符才判定写坏（避免把 SMB 抖动误判成下载失败）。
        for attempt in range(3):
            try:
                output_file_size = os.path.getsize(output_path)
            except OSError:
                output_file_size = -1
            if output_file_size == written:
                break
            if attempt == 2:
                raise ValueError(
                    f'落盘字节数不符（写入 {written}，磁盘上 {output_file_size}）'
                )
            time.sleep(0.2)
    except Exception:
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise
