"""
MIME detection and WeChat media-type routing.
Ported from openclaw-weixin/src/media/mime.ts and api/types.ts.
"""

import os

# Extension -> MIME (from mime.ts:3-30)
EXTENSION_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# UploadMediaType (types.ts:24-30) — used in getUploadUrl.media_type
UPLOAD_TYPE_IMAGE = 1
UPLOAD_TYPE_VIDEO = 2
UPLOAD_TYPE_FILE = 3

# MessageItemType (types.ts:70-79) — used in item_list[].type
ITEM_TYPE_TEXT = 1
ITEM_TYPE_IMAGE = 2
ITEM_TYPE_FILE = 4
ITEM_TYPE_VIDEO = 5


def guess_mime(path: str) -> str:
    """MIME from filename extension; 'application/octet-stream' if unknown."""
    ext = os.path.splitext(path)[1].lower()
    return EXTENSION_TO_MIME.get(ext, "application/octet-stream")


def upload_media_type(mime: str) -> int:
    """Map MIME to UploadMediaType (getUploadUrl.media_type)."""
    if mime.startswith("image/"):
        return UPLOAD_TYPE_IMAGE
    if mime.startswith("video/"):
        return UPLOAD_TYPE_VIDEO
    return UPLOAD_TYPE_FILE


def send_item_type(mime: str) -> int:
    """Map MIME to MessageItemType (item_list[].type)."""
    if mime.startswith("image/"):
        return ITEM_TYPE_IMAGE
    if mime.startswith("video/"):
        return ITEM_TYPE_VIDEO
    return ITEM_TYPE_FILE
