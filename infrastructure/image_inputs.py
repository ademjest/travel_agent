"""Size-limited HTTPS/cache image inputs; no reservation or model workflow."""
from pathlib import Path

from infrastructure.attachment_cache import read_cached_attachment
from infrastructure.secure_download import download_https, resolve_host

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TIMEOUT = (10, 20)
CONTENT_TYPE_EXTENSIONS = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}


def validate_image(data, content_type):
    signatures = {'image/png': data.startswith(b'\x89PNG\r\n\x1a\n'),
                  'image/jpeg': data.startswith(b'\xff\xd8\xff'),
                  'image/webp': data[:4] == b'RIFF' and data[8:12] == b'WEBP'}
    if len(data) > MAX_IMAGE_BYTES or not signatures.get(content_type):
        raise ValueError('图片内容不是受支持的 JPEG、PNG 或 WebP，或超过 5 MB。')


def is_supported_attachment(attachment: object) -> bool:
    content_type = str(
        getattr(attachment, "content_type", "") or ""
    ).split(";", 1)[0].lower()
    suffix = Path(
        str(getattr(attachment, "filename", "") or "")
    ).suffix.lower()
    return (
        content_type in CONTENT_TYPE_EXTENSIONS
        or suffix in {".jpg", ".jpeg", ".png", ".webp"}
    )

def download_image(attachment: object, *, session, database_path, resolver=resolve_host) -> tuple[bytes, str]:
    if getattr(attachment, 'local_path', ''):
        cached = read_cached_attachment(attachment, database_path, max_bytes=MAX_IMAGE_BYTES)
        content_type = str(getattr(attachment, 'content_type', ''))
        if content_type not in CONTENT_TYPE_EXTENSIONS:
            raise ValueError('图片格式必须是 JPEG、PNG 或 WebP。')
        return cached, content_type
    url = str(getattr(attachment, "url", "") or "")
    declared_size = int(getattr(attachment, "size", 0) or 0)
    return download_https(
        session,
        url,
        timeout=IMAGE_TIMEOUT,
        max_bytes=MAX_IMAGE_BYTES,
        declared_size=declared_size,
        resolver=resolver,
        allowed_content_types=set(CONTENT_TYPE_EXTENSIONS),
        size_error="图片超过 5 MB 限制",
        type_error="图片格式必须是 JPEG、PNG 或 WebP",
    )
