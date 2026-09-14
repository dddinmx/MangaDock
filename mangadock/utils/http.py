# -*- coding: utf-8 -*-
"""Safe outbound HTTP helpers (SSRF protections)."""
import ipaddress
import os
import socket
from urllib.parse import urljoin, urlparse

import requests

from mangadock.settings import (
    CONFIG,
    MAX_HTML_RESPONSE_BYTES,
    SAFE_HTTP_ALLOWED_HOST_SUFFIXES,
    SAFE_HTTP_ALLOWED_PROXY_NETWORKS,
)


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

    try:
        resolved_addresses = {
            result[4][0]
            for result in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError('上游域名无法解析') from exc

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


def safe_http_get(url, headers=None, timeout=None, stream=False, verify=True, max_bytes=MAX_HTML_RESPONSE_BYTES, session_obj=None):
    current_url = validate_safe_upstream_url(url)
    requester = session_obj or requests
    timeout = timeout or CONFIG['request_timeout']
    verify = should_verify_upstream_tls(verify)

    for _ in range(4):
        response = requester.get(
            current_url,
            headers=headers,
            timeout=timeout,
            stream=stream,
            verify=verify,
            allow_redirects=False
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
    except Exception:
        try:
            os.remove(output_path)
        except OSError:
            pass
        raise
