# -*- coding: utf-8 -*-
"""HTTP client for the private Fanqie resource API.

This module intentionally contains no upstream request signing or decryption.
All Fanqie network access is performed by the configured API server.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from mangadock.settings import (
    APP_VERSION,
    FANQIE_API_ALLOW_ANONYMOUS,
    FANQIE_API_BASE_URL,
    FANQIE_API_INSTALLATION_ID,
    FANQIE_API_INSTALLATION_ID_FILE,
    FANQIE_API_MAX_ARTIFACT_BYTES,
    FANQIE_API_TOKEN,
)


INSTALLATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}")
_installation_id_lock = threading.Lock()
_installation_id_value = ""


def _fallback_installation_id(token: str) -> str:
    material = token or f"{platform.system()}:{platform.machine()}:{Path.cwd()}"
    return "fallback-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def get_installation_id(token: str = "") -> str:
    global _installation_id_value
    if _installation_id_value:
        return _installation_id_value
    with _installation_id_lock:
        if _installation_id_value:
            return _installation_id_value
        configured = FANQIE_API_INSTALLATION_ID
        if configured and INSTALLATION_ID_RE.fullmatch(configured):
            _installation_id_value = configured
            return configured
        path = Path(FANQIE_API_INSTALLATION_ID_FILE)
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if INSTALLATION_ID_RE.fullmatch(existing):
            _installation_id_value = existing
            return existing
        candidate = uuid.uuid4().hex
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(candidate + "\n")
            _installation_id_value = candidate
            return candidate
        except FileExistsError:
            try:
                existing = path.read_text(encoding="utf-8").strip()
            except OSError:
                existing = ""
            if INSTALLATION_ID_RE.fullmatch(existing):
                _installation_id_value = existing
                return existing
        except OSError:
            pass
        _installation_id_value = _fallback_installation_id(token)
        return _installation_id_value


class FanqieApiError(RuntimeError):
    def __init__(self, message: str, code: str = "API_ERROR", status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def is_configured() -> bool:
    return bool(FANQIE_API_BASE_URL and (FANQIE_API_TOKEN or FANQIE_API_ALLOW_ANONYMOUS))


class FanqieApiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or FANQIE_API_BASE_URL).strip().rstrip("/") + "/"
        self.token = FANQIE_API_TOKEN if token is None else token.strip()
        self._validate_base_url()
        self.session = requests.Session()
        installation_id = get_installation_id(self.token)
        system_info = (
            f"{platform.system()}/{platform.machine()}; "
            f"Python/{platform.python_version()}"
        )
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": f"MangaDock/{APP_VERSION} Fanqie-Client/1.1",
            "X-MangaDock-Instance-ID": installation_id,
            "X-MangaDock-Version": APP_VERSION,
            "X-MangaDock-System": system_info,
        })
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def _validate_base_url(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise FanqieApiError("番茄 API 地址格式无效", "INVALID_API_URL")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise FanqieApiError("远程番茄 API 必须使用 HTTPS", "INSECURE_API_URL")

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    @staticmethod
    def _error_from_response(response: requests.Response) -> FanqieApiError:
        code = "HTTP_ERROR"
        message = f"番茄 API 请求失败（HTTP {response.status_code}）"
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
        except ValueError:
            pass
        return FanqieApiError(message, code, response.status_code)

    def _get_stream_with_retry(self, path: str, *, timeout, unavailable_message: str):
        response = None
        last_error = None
        for attempt in range(3):
            try:
                response = self.session.get(self._url(path), timeout=timeout, stream=True)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise FanqieApiError(unavailable_message, "API_UNAVAILABLE") from exc
            if response.status_code in {502, 503, 504, 520, 522, 523, 524, 530} and attempt < 2:
                response.close()
                time.sleep(0.5 * (attempt + 1))
                continue
            return response
        raise FanqieApiError(unavailable_message, "API_UNAVAILABLE") from last_error

    def request_json(self, method: str, path: str, *, params=None, json=None, timeout=(15, 60)):
        if not is_configured() and not self.token:
            raise FanqieApiError(
                "尚未配置番茄 API Token，请设置 MANGADOCK_FANQIE_API_TOKEN",
                "API_NOT_CONFIGURED",
            )
        method = method.upper()
        retryable = method == "GET" or path == "/v1/resources/resolve"
        attempts = 3 if retryable else 1
        response = None
        last_error = None
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    self._url(path),
                    params=params,
                    json=json,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise FanqieApiError("无法连接番茄 API 服务", "API_UNAVAILABLE") from exc
            if response.status_code in {502, 503, 504, 520, 522, 523, 524, 530} and attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
                continue
            break
        if response is None:
            raise FanqieApiError("无法连接番茄 API 服务", "API_UNAVAILABLE") from last_error
        if not response.ok:
            raise self._error_from_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise FanqieApiError("番茄 API 返回了无效数据", "INVALID_RESPONSE") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise FanqieApiError("番茄 API 返回格式无效", "INVALID_RESPONSE")
        return payload.get("data")

    def capabilities(self) -> dict:
        return self.request_json("GET", "/v1/capabilities") or {}

    def search_novels(self, query: str, page: int = 1) -> list[dict]:
        data = self.request_json(
            "GET", "/v1/novels/search", params={"q": query, "page": max(1, int(page or 1))}
        ) or {}
        items = data.get("items") or []
        return items if isinstance(items, list) else []

    def resolve_resource(self, target: str, kind: str) -> dict:
        data = self.request_json(
            "POST", "/v1/resources/resolve", json={"target": target, "kind": kind}
        ) or {}
        if not isinstance(data.get("metadata"), dict) or not isinstance(data.get("chapters"), list):
            raise FanqieApiError("番茄 API 资源信息不完整", "INVALID_RESPONSE")
        return data

    def fetch_cover(self, book_id: str) -> tuple[bytes, str]:
        if not is_configured() and not self.token:
            raise FanqieApiError(
                "尚未配置番茄 API Token，请设置 MANGADOCK_FANQIE_API_TOKEN",
                "API_NOT_CONFIGURED",
            )
        response = self._get_stream_with_retry(
            f"/v1/resources/{book_id}/cover",
            timeout=(15, 45),
            unavailable_message="无法连接番茄 API 服务",
        )
        try:
            if not response.ok:
                raise self._error_from_response(response)
            declared_size = int(response.headers.get("Content-Length") or 0)
            if declared_size > 8 * 1024 * 1024:
                raise FanqieApiError("番茄封面文件过大", "COVER_TOO_LARGE")
            chunks = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 8 * 1024 * 1024:
                    raise FanqieApiError("番茄封面文件过大", "COVER_TOO_LARGE")
                chunks.append(chunk)
            content_type = (response.headers.get("Content-Type") or "image/jpeg").split(";", 1)[0]
            if not content_type.startswith("image/"):
                raise FanqieApiError("番茄 API 封面响应无效", "INVALID_COVER")
            return b"".join(chunks), content_type
        finally:
            response.close()

    def create_job(
        self,
        target: str,
        kind: str,
        output_format: str,
        chapter_ids: list[str] | None = None,
    ) -> dict:
        return self.request_json(
            "POST",
            "/v1/jobs",
            json={
                "target": target,
                "kind": kind,
                "format": output_format,
                "chapter_ids": chapter_ids or [],
            },
        ) or {}

    def get_job(self, job_id: str) -> dict:
        return self.request_json("GET", f"/v1/jobs/{job_id}") or {}

    def cancel_job(self, job_id: str) -> dict:
        return self.request_json("DELETE", f"/v1/jobs/{job_id}") or {}

    def download_artifact(self, job: dict, destination: Path) -> None:
        artifact = job.get("artifact") or {}
        path = str(artifact.get("url") or "")
        expected_sha256 = str(artifact.get("sha256") or "").lower()
        expected_size = int(artifact.get("size") or 0)
        if not path.startswith("/v1/jobs/") or not expected_sha256:
            raise FanqieApiError("番茄 API 任务产物信息不完整", "INVALID_ARTIFACT")
        if expected_size <= 0 or expected_size > FANQIE_API_MAX_ARTIFACT_BYTES:
            raise FanqieApiError("番茄 API 任务产物大小无效", "INVALID_ARTIFACT_SIZE")
        response = self._get_stream_with_retry(
            path,
            timeout=(20, 300),
            unavailable_message="下载番茄 API 任务产物失败",
        )
        try:
            if not response.ok:
                raise self._error_from_response(response)
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            try:
                with destination.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > FANQIE_API_MAX_ARTIFACT_BYTES:
                            raise FanqieApiError("番茄 API 任务产物超过大小限制", "ARTIFACT_TOO_LARGE")
                        output.write(chunk)
                        digest.update(chunk)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        finally:
            response.close()
        if size != expected_size or digest.hexdigest().lower() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise FanqieApiError("番茄 API 任务产物校验失败", "ARTIFACT_CHECKSUM_MISMATCH")


def get_client() -> FanqieApiClient:
    return FanqieApiClient()
