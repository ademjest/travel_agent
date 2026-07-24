from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlparse

MAX_REDIRECTS = 3
DOWNLOAD_CHUNK_BYTES = 64 * 1024
Resolver = Callable[[str], Iterable[str]]


def resolve_host(hostname: str) -> tuple[str, ...]:
    addresses = {
        str(sockaddr[0])
        for unused_family, unused_type, unused_proto, unused_name, sockaddr
        in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    }
    return tuple(sorted(addresses))


def validate_public_https_url(
        url: str,
        resolver: Resolver = resolve_host) -> str:
    parsed = urlparse(url)
    if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None):
        raise ValueError("附件下载地址必须是有效的 HTTPS URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("附件下载地址端口无效") from exc
    if port not in {None, 443}:
        raise ValueError("附件下载地址只允许 HTTPS 默认端口")

    hostname = parsed.hostname.rstrip(".")
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = (str(literal),)
    except ValueError:
        try:
            addresses = tuple(resolver(hostname))
        except (OSError, socket.gaierror) as exc:
            raise ValueError("附件下载地址无法解析") from exc
    if not addresses:
        raise ValueError("附件下载地址无法解析")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("附件下载地址解析结果无效") from exc
        if not address.is_global:
            raise ValueError("附件下载地址指向受限网络")
    return url


def download_https(
        session: object,
        url: str,
        *,
        max_bytes: int,
        timeout: object,
        declared_size: int = 0,
        resolver: Resolver = resolve_host,
        max_redirects: int = MAX_REDIRECTS,
        allowed_content_types: set[str] | None = None,
        size_error: str = "文件超过大小限制",
        type_error: str = "附件格式不受支持") -> tuple[bytes, str]:
    if declared_size > max_bytes:
        raise ValueError(size_error)
    current_url = str(url or "").strip()
    for redirect_count in range(max_redirects + 1):
        validate_public_https_url(current_url, resolver)
        try:
            response = session.get(
                current_url,
                stream=True,
                timeout=timeout,
                allow_redirects=False,
            )
        except Exception as exc:
            raise ValueError("附件下载失败") from exc
        try:
            status_code = int(getattr(response, "status_code", 200) or 200)
            if status_code in {301, 302, 303, 307, 308}:
                if redirect_count >= max_redirects:
                    raise ValueError("附件下载重定向次数过多")
                location = str(response.headers.get("Location") or "").strip()
                if not location:
                    raise ValueError("附件下载重定向地址无效")
                current_url = urljoin(current_url, location)
                continue
            try:
                response.raise_for_status()
            except Exception as exc:
                raise ValueError("附件下载失败") from exc

            try:
                header_length = int(
                    response.headers.get("Content-Length") or 0
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("附件大小响应头无效") from exc
            if header_length > max_bytes:
                raise ValueError(size_error)
            content_type = str(
                response.headers.get("Content-Type") or ""
            ).split(";", 1)[0].strip().lower()
            if (
                    allowed_content_types is not None
                    and content_type not in allowed_content_types):
                raise ValueError(type_error)

            chunks = []
            total = 0
            for chunk in response.iter_content(DOWNLOAD_CHUNK_BYTES):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(size_error)
                chunks.append(chunk)
            return b"".join(chunks), content_type
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
    raise ValueError("附件下载重定向次数过多")
