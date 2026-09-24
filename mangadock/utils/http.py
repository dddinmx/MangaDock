# -*- coding: utf-8 -*-
"""Safe outbound HTTP helpers (SSRF protections)."""
import ipaddress
import logging
import os
import socket
import time
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from urllib3.connection import HTTPConnection as _HTTPConnection
from urllib3.connection import HTTPSConnection as _HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool as _HTTPConnectionPool
from urllib3.connectionpool import HTTPSConnectionPool as _HTTPSConnectionPool

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


def _resolve_and_validate_host(host, port):
    """在真正建连时解析并校验目标 IP（DNS 重绑定防护）。返回可用安全 IP，否则抛错。

    2026-09-22 code review P2：validate_safe_upstream_url 在请求入口解析并校验过 IP，
    但随后 requests 会再次解析，二者之间存在重绑定窗口（攻击者可把域名改指向内网）。
    这里把校验下移到每次建连（_SafeHTTPConnection._new_conn），确保连接落到的 IP
    仍是经校验的安全地址；每个连接、每次重定向跳转、每次连接池复用都会重新校验。
    """
    if not host or not is_allowed_upstream_host(host):
        raise ValueError('上游地址域名不在允许列表中')
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip:
        if not is_public_ip_address(str(literal_ip)) and not any(
            literal_ip in network for network in SAFE_HTTP_ALLOWED_PROXY_NETWORKS
        ):
            raise ValueError('不允许访问内网或保留地址')
        return str(literal_ip)
    # DNS 抖动退避重试（与入口校验一致）
    for dns_attempt in range(SAFE_HTTP_DNS_RETRIES):
        try:
            addresses = {
                result[4][0]
                for result in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
            }
            break
        except socket.gaierror as exc:
            if dns_attempt >= SAFE_HTTP_DNS_RETRIES - 1:
                raise ValueError('上游域名无法解析') from exc
            delay = _retry_delay(SAFE_HTTP_DNS_BACKOFF_SECONDS, dns_attempt)
            logger.warning(
                '上游域名建连前解析失败（%s），%.1fs 后重试 %d/%d：%s',
                exc, delay, dns_attempt + 1, SAFE_HTTP_DNS_RETRIES - 1, host
            )
            time.sleep(delay)
    safe_addresses = [a for a in addresses if is_safe_resolved_upstream_address(host, a)]
    if not safe_addresses:
        raise ValueError('上游域名解析到不安全地址')
    return safe_addresses[0]


# === DNS 重绑定防护：自定义连接类，在 _new_conn 阶段固定到已校验 IP =================
# 走代理时 urllib3 会把 self.proxy 置为代理地址、self.host 指向代理，此时对端是受信任
# 代理本身、DNS 由代理负责解析，直接走默认逻辑（跳过本机校验），避免误伤本机代理。
# 注意：建连必须用 urllib3.util.connection.create_connection（支持 socket_options
# 关键字并负责逐项 setsockopt）；标准库 socket.create_connection 不接受该参数，
# 直接传会导致所有直连建连抛 TypeError（v2.10.0 发版当日事故）。
from urllib3.util.connection import create_connection as _urllib3_create_connection


class _SafeHTTPConnection(_HTTPConnection):
    def _new_conn(self):
        if getattr(self, 'proxy', None):
            return super()._new_conn()
        validated_ip = _resolve_and_validate_host(self.host, self.port)
        return _urllib3_create_connection(
            (validated_ip, self.port),
            self.timeout,
            source_address=self.source_address,
            socket_options=self.socket_options,
        )


class _SafeHTTPSConnection(_SafeHTTPConnection, _HTTPSConnection):
    # TLS 包装沿用 _HTTPSConnection.connect，仍按原始 self.host 做 SNI 与证书校验，
    # 仅把底层 socket 连到已校验 IP，证书主体不被改动。
    pass


class _SafeHTTPConnectionPool(_HTTPConnectionPool):
    ConnectionCls = _SafeHTTPConnection


class _SafeHTTPSConnectionPool(_SafeHTTPConnectionPool, _HTTPSConnectionPool):
    ConnectionCls = _SafeHTTPSConnection


class _SafePoolManager(urllib3.PoolManager):
    def connection_from_host(self, host, port, scheme, pool_kwargs=None):
        pool = super().connection_from_host(host, port, scheme, pool_kwargs=pool_kwargs)
        # 让该连接池用带 IP 校验的连接类（实例属性覆盖类属性，连接创建时生效）
        pool.ConnectionCls = _SafeHTTPSConnection if (scheme or 'http') == 'https' else _SafeHTTPConnection
        return pool


class _SafeHTTPAdapter(requests.adapters.HTTPAdapter):
    """出站连接统一走连接级 IP 校验，消除 DNS 重绑定窗口（见 _SafeHTTPConnection）。"""
    def init_poolmanager(self, connections, maxsize, block, **pool_kwargs):
        self.poolmanager = _SafePoolManager(
            num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs
        )


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


def _read_bounded_content(response, max_bytes):
    """有界读取响应体：超过 max_bytes 立即抛错并关闭连接。

    2026-09-24 code review P2：requests 非流式请求在 get() 返回前就把整个
    响应体读进内存，事后再查 len(content) 挡不住读取阶段的内存占用。
    这里在 iter_content 流上逐块计数，超限即中止，超限部分不会进入内存。
    """
    chunks = []
    read_bytes = 0
    try:
        for chunk in response.iter_content(65536):
            if not chunk:
                continue
            read_bytes += len(chunk)
            if max_bytes and read_bytes > max_bytes:
                raise ValueError('上游响应超过大小限制')
            chunks.append(chunk)
    except Exception:
        response.close()
        raise
    response._content = b''.join(chunks)
    response._content_consumed = True


def safe_http_get(url, headers=None, timeout=None, stream=False, verify=True, max_bytes=MAX_HTML_RESPONSE_BYTES, session_obj=None, retries=None):
    current_url = validate_safe_upstream_url(url)
    timeout = timeout or CONFIG['request_timeout']
    verify = should_verify_upstream_tls(verify)
    if retries is None:
        # 非流式 = 元数据/HTML/图片列表，调用方没有别的重试兜底 → 这里重试；
        # 流式 = 图片字节，调用方 download_binary_image 已重试 → 不叠加。
        retries = 1 if stream else SAFE_HTTP_NETWORK_RETRIES

    # 2026-09-22 code review P2：所有出站连接经 _SafeHTTPAdapter，在真正建连时
    # 重新解析并校验目标 IP，消除 validate 与 connect 之间的 DNS 重绑定窗口；
    # 走代理时由连接类跳过校验（信任代理）。不改动任何调用方。
    requester = session_obj
    if requester is None or not isinstance(requester, requests.Session):
        requester = requests.Session()
    requester.mount('http://', _SafeHTTPAdapter())
    requester.mount('https://', _SafeHTTPAdapter())

    for _ in range(SAFE_HTTP_MAX_REDIRECTS):
        # 传输层始终流式发送：非流式调用随后由 _read_bounded_content 在读取
        # 阶段强制执行大小上限，异常大的响应不会先整体进入内存。
        response = _send_get_with_retries(
            requester, current_url, headers, timeout, True, verify, retries
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get('Location')
            response.close()
            if not location:
                raise ValueError('上游重定向缺少 Location')
            current_url = validate_safe_upstream_url(urljoin(current_url, location))
            continue
        # 先做 Content-Length 快速失败（流式语义：只查响应头，不触发读取）
        enforce_response_size(response, max_bytes, stream=True)
        if not stream:
            _read_bounded_content(response, max_bytes)
        return response

    raise ValueError('上游重定向次数过多')


def safe_http_post(url, json_body=None, headers=None, timeout=None, verify=True,
                   max_bytes=MAX_HTML_RESPONSE_BYTES, session_obj=None):
    """向固定的、经过校验的 HTTPS 上游发送 POST 请求。"""
    current_url = validate_safe_upstream_url(url)
    requester = session_obj
    if requester is None or not isinstance(requester, requests.Session):
        requester = requests.Session()
    requester.mount('http://', _SafeHTTPAdapter())
    requester.mount('https://', _SafeHTTPAdapter())

    response = requester.post(
        current_url,
        json=json_body,
        headers=headers,
        timeout=timeout or CONFIG['request_timeout'],
        verify=should_verify_upstream_tls(verify),
        allow_redirects=False,
        # 与 safe_http_get 一致：传输层流式 + 有界读取，大小限制在读取阶段生效
        stream=True,
    )
    if response.is_redirect or response.is_permanent_redirect:
        response.close()
        raise ValueError('上游 POST 不允许重定向')
    enforce_response_size(response, max_bytes, stream=True)
    _read_bounded_content(response, max_bytes)
    return response


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
