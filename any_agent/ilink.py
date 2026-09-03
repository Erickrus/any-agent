"""
iLink API client — speaks the Tencent iLink protocol for WeChat bot communication.
Ported from @tencent-weixin/openclaw-weixin plugin.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import struct
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

from .aes_ecb import aes_ecb_padded_size, encrypt_aes_ecb
from . import media as media_types

logger = logging.getLogger("wx_bridge.ilink")

ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.4.8"
DEFAULT_BOT_AGENT = "WxBridge/1.0"

def _client_version(version: str = CHANNEL_VERSION) -> int:
    parts = [int(p) for p in version.split(".")]
    major = parts[0] if len(parts) > 0 else 0
    minor = parts[1] if len(parts) > 1 else 0
    patch = parts[2] if len(parts) > 2 else 0
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _random_wechat_uin() -> str:
    val = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(val).encode()).decode()


def _build_common_headers() -> dict[str, str]:
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(_client_version()),
    }


def _build_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
        **_build_common_headers(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _build_base_info() -> dict[str, str]:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": DEFAULT_BOT_AGENT}


@dataclass
class QRLoginResult:
    connected: bool = False
    bot_token: str = ""
    account_id: str = ""
    base_url: str = ""
    user_id: str = ""
    message: str = ""


FIXED_QR_BASE_URL = "https://ilinkai.weixin.qq.com"
QR_LONG_POLL_TIMEOUT = 35


async def start_qr_login(
    session: aiohttp.ClientSession,
    bot_type: str = "3",
    existing_tokens: list[str] | None = None,
) -> dict[str, Any]:
    url = f"{FIXED_QR_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type={bot_type}"
    body = {"local_token_list": existing_tokens or []}
    headers = _build_headers()
    async with session.post(url, json=body, headers=headers) as resp:
        raw = await resp.text()
    return json.loads(raw)


async def poll_qr_status(
    session: aiohttp.ClientSession,
    qrcode: str,
    base_url: str = FIXED_QR_BASE_URL,
    verify_code: str | None = None,
) -> dict[str, Any]:
    from urllib.parse import quote
    endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode)}"
    if verify_code:
        endpoint += f"&verify_code={quote(verify_code)}"
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = _build_common_headers()
    timeout = aiohttp.ClientTimeout(total=QR_LONG_POLL_TIMEOUT + 5)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        raw = await resp.text()
    return json.loads(raw)


async def wait_for_qr_login(
    session: aiohttp.ClientSession,
    qrcode: str,
    timeout_s: float = 300,
    on_status: Any = None,
) -> QRLoginResult:
    deadline = time.time() + timeout_s
    base_url = FIXED_QR_BASE_URL
    pending_verify_code: str | None = None

    while time.time() < deadline:
        try:
            data = await poll_qr_status(session, qrcode, base_url, pending_verify_code)
        except Exception as e:
            logger.error(f"QR poll error: {e}")
            await asyncio.sleep(2)
            continue

        status = data.get("status", "wait")
        if on_status:
            on_status(status)

        if status == "need_verifycode":
            prompt = "Enter the number shown on your phone WeChat: "
            loop = asyncio.get_event_loop()
            code = await loop.run_in_executor(None, lambda: input(prompt).strip())
            pending_verify_code = code
            continue

        if status == "scaned":
            if pending_verify_code:
                pending_verify_code = None

        if status == "scaned_but_redirect":
            new_host = data.get("redirect_host", "")
            if new_host:
                base_url = f"https://{new_host}" if not new_host.startswith("http") else new_host
                logger.info(f"QR IDC redirect -> {base_url}")
            await asyncio.sleep(1)
            continue

        if status == "binded_redirect":
            return QRLoginResult(connected=False, message="Already bound to this bot. No new credentials issued.")

        if status == "confirmed":
            return QRLoginResult(
                connected=True,
                bot_token=data.get("bot_token", ""),
                account_id=data.get("ilink_bot_id", ""),
                base_url=data.get("baseurl", ""),
                user_id=data.get("ilink_user_id", ""),
                message="Login confirmed",
            )

        if status == "expired":
            return QRLoginResult(message="QR code expired")

        if status == "verify_code_blocked":
            pending_verify_code = None
            return QRLoginResult(message="Too many wrong codes, try again later")

        await asyncio.sleep(1)

    return QRLoginResult(message="Login timed out")


@dataclass
class WeixinMessage:
    seq: int = 0
    message_id: int = 0
    from_user_id: str = ""
    to_user_id: str = ""
    client_id: str = ""
    create_time_ms: int = 0
    session_id: str = ""
    group_id: str = ""
    message_type: int = 0
    message_state: int = 0
    item_list: list[dict[str, Any]] = field(default_factory=list)
    context_token: str = ""
    run_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "WeixinMessage":
        return WeixinMessage(
            seq=d.get("seq", 0),
            message_id=d.get("message_id", 0),
            from_user_id=d.get("from_user_id", ""),
            to_user_id=d.get("to_user_id", ""),
            client_id=d.get("client_id", ""),
            create_time_ms=d.get("create_time_ms", 0),
            session_id=d.get("session_id", ""),
            group_id=d.get("group_id", ""),
            message_type=d.get("message_type", 0),
            message_state=d.get("message_state", 0),
            item_list=d.get("item_list", []),
            context_token=d.get("context_token", ""),
            run_id=d.get("run_id", ""),
            raw=d,
        )

    def text_body(self) -> str:
        for item in self.item_list:
            if item.get("type") == 1 and item.get("text_item"):
                return item["text_item"].get("text", "")
        return ""


class ILinkClient:
    """Async client for Tencent iLink bot API."""

    def __init__(self, base_url: str, token: str, cdn_base_url: str = "", state_dir: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.cdn_base_url = cdn_base_url
        self.get_updates_buf = ""
        self._session: aiohttp.ClientSession | None = None
        self._state_dir = state_dir or os.path.join(os.path.expanduser("~"), ".any_agent")
        self._context_tokens: dict[str, str] = {}
        self._load_state()

    def _state_file(self, name: str) -> str:
        return os.path.join(self._state_dir, name)

    def _load_state(self):
        os.makedirs(self._state_dir, exist_ok=True)
        try:
            with open(self._state_file("sync.json")) as f:
                data = json.load(f)
                self.get_updates_buf = data.get("get_updates_buf", "")
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        try:
            with open(self._state_file("context_tokens.json")) as f:
                self._context_tokens = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save_state(self):
        os.makedirs(self._state_dir, exist_ok=True)
        with open(self._state_file("sync.json"), "w") as f:
            json.dump({"get_updates_buf": self.get_updates_buf}, f)
        with open(self._state_file("context_tokens.json"), "w") as f:
            json.dump(self._context_tokens, f)

    def set_context_token(self, user_id: str, token: str):
        self._context_tokens[user_id] = token
        self.save_state()

    def get_context_token(self, user_id: str) -> str:
        return self._context_tokens.get(user_id, "")

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _url(self, endpoint: str) -> str:
        sep = "" if self.base_url.endswith("/") else "/"
        return f"{self.base_url}{sep}{endpoint}"

    async def _post(self, endpoint: str, body: dict, timeout_s: float = 15) -> dict:
        session = await self._ensure_session()
        headers = _build_headers(self.token)
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        url = self._url(endpoint)
        logger.debug(f"POST {url}")
        async with session.post(url, json=body, headers=headers, timeout=timeout) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"iLink {endpoint} HTTP {resp.status}: {text[:200]}")
            return json.loads(text)

    async def get_updates(self, long_poll_timeout_ms: int = 35000) -> dict:
        body: dict[str, Any] = {
            "get_updates_buf": self.get_updates_buf,
            "base_info": _build_base_info(),
        }
        timeout_s = (long_poll_timeout_ms / 1000) + 10
        data = await self._post("ilink/bot/getupdates", body, timeout_s=timeout_s)
        if data.get("get_updates_buf"):
            self.get_updates_buf = data["get_updates_buf"]
            self.save_state()
        return data

    async def send_message(
        self,
        to: str,
        text: str,
        context_token: str = "",
        run_id: str = "",
    ) -> dict:
        client_id = f"any-agent-{int(time.time()*1000)}-{random.randint(0,9999):04d}"
        item_list = []
        if text:
            item_list.append({"type": 1, "text_item": {"text": text}})
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "item_list": item_list if item_list else None,
                "context_token": context_token or None,
                "run_id": run_id or None,
            },
            "base_info": _build_base_info(),
        }
        return await self._post("ilink/bot/sendmessage", body)

    # ── Media (CDN upload + send) ─────────────────────────────────────

    async def get_upload_url(
        self,
        filekey: str,
        media_type: int,
        to_user_id: str,
        rawsize: int,
        rawfilemd5: str,
        filesize: int,
        aeskey_hex: str,
    ) -> dict:
        """Request a pre-signed CDN upload URL. Ref: openclaw-weixin api.ts:474-500."""
        body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aeskey_hex,
            "base_info": _build_base_info(),
        }
        return await self._post("ilink/bot/getuploadurl", body, timeout_s=20)

    async def _upload_to_cdn(
        self,
        ciphertext: bytes,
        upload_full_url: str,
        upload_param: str,
        filekey: str,
        max_retries: int = 3,
    ) -> str:
        """
        POST ciphertext to the CDN. Returns the x-encrypted-param download param.
        Ref: openclaw-weixin cdn-upload.ts:14-93. 4xx aborts immediately; 5xx retries.
        """
        if upload_full_url:
            url = upload_full_url.strip()
        elif upload_param:
            url = (
                f"{self.cdn_base_url}/upload"
                f"?encrypted_query_param={quote(upload_param, safe='')}"
                f"&filekey={quote(filekey, safe='')}"
            )
        else:
            raise RuntimeError("CDN upload URL missing (need upload_full_url or upload_param)")

        session = await self._ensure_session()
        headers = {"Content-Type": "application/octet-stream"}
        timeout = aiohttp.ClientTimeout(total=60)
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                async with session.post(url, data=ciphertext, headers=headers, timeout=timeout) as resp:
                    if 400 <= resp.status < 500:
                        errmsg = resp.headers.get("x-error-message") or (await resp.text())[:200]
                        raise RuntimeError(f"CDN upload client error {resp.status}: {errmsg}")
                    if resp.status != 200:
                        errmsg = resp.headers.get("x-error-message") or f"status {resp.status}"
                        raise RuntimeError(f"CDN upload server error: {errmsg}")
                    download_param = resp.headers.get("x-encrypted-param")
                    if not download_param:
                        raise RuntimeError("CDN upload response missing x-encrypted-param header")
                    logger.info(f"CDN upload success (attempt {attempt}) filekey={filekey}")
                    return download_param
            except RuntimeError as e:
                last_error = e
                if "client error" in str(e):
                    raise
                logger.warning(f"CDN upload attempt {attempt} failed: {e}")
            except Exception as e:
                last_error = e
                logger.warning(f"CDN upload attempt {attempt} network error: {e}")

        raise last_error or RuntimeError(f"CDN upload failed after {max_retries} attempts")

    async def upload_file(self, path: str, to_user_id: str, media_type: int) -> dict:
        """
        Upload a local file to the WeChat CDN with AES-128-ECB encryption.
        Returns dict with filekey, download_param, aeskey_hex, raw_size, cipher_size.
        Ref: openclaw-weixin upload.ts:63-122.
        """
        with open(path, "rb") as f:
            plaintext = f.read()

        raw_size = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        cipher_size = aes_ecb_padded_size(raw_size)
        filekey = os.urandom(16).hex()
        aeskey = os.urandom(16)
        aeskey_hex = aeskey.hex()

        logger.info(
            f"upload_file: {path} raw={raw_size} cipher={cipher_size} "
            f"md5={rawfilemd5} filekey={filekey} media_type={media_type}"
        )

        resp = await self.get_upload_url(
            filekey=filekey,
            media_type=media_type,
            to_user_id=to_user_id,
            rawsize=raw_size,
            rawfilemd5=rawfilemd5,
            filesize=cipher_size,
            aeskey_hex=aeskey_hex,
        )
        upload_full_url = (resp.get("upload_full_url") or "").strip()
        upload_param = resp.get("upload_param") or ""
        if not upload_full_url and not upload_param:
            raise RuntimeError(f"getUploadUrl returned no upload URL: {resp}")

        ciphertext = encrypt_aes_ecb(plaintext, aeskey)
        download_param = await self._upload_to_cdn(
            ciphertext=ciphertext,
            upload_full_url=upload_full_url,
            upload_param=upload_param,
            filekey=filekey,
        )

        return {
            "filekey": filekey,
            "download_param": download_param,
            "aeskey_hex": aeskey_hex,
            "raw_size": raw_size,
            "cipher_size": cipher_size,
        }

    def _build_media_ref(self, uploaded: dict) -> dict:
        """CDNMedia block. aes_key = base64 of the aeskey HEX STRING's bytes (matches TS)."""
        aes_key_b64 = base64.b64encode(uploaded["aeskey_hex"].encode()).decode()
        return {
            "encrypt_query_param": uploaded["download_param"],
            "aes_key": aes_key_b64,
            "encrypt_type": 1,
        }

    async def _send_item(self, to: str, item: dict, context_token: str = "") -> dict:
        """Send a single item in its own item_list (matches TS one-item-per-request)."""
        client_id = f"any-agent-{int(time.time()*1000)}-{random.randint(0,9999):04d}"
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "item_list": [item],
                "context_token": context_token or None,
                "run_id": None,
            },
            "base_info": _build_base_info(),
        }
        return await self._post("ilink/bot/sendmessage", body)

    async def send_media(
        self,
        to: str,
        path: str,
        caption: str = "",
        context_token: str = "",
    ) -> dict:
        """
        Upload a local file and send it to the user as image/video/file.
        Caption (if any) is sent first as a separate text item.
        Ref: openclaw-weixin send.ts:145-294.
        """
        mime = media_types.guess_mime(path)
        up_type = media_types.upload_media_type(mime)
        item_type = media_types.send_item_type(mime)

        uploaded = await self.upload_file(path, to_user_id=to, media_type=up_type)
        ref = self._build_media_ref(uploaded)

        if item_type == media_types.ITEM_TYPE_IMAGE:
            item = {"type": item_type, "image_item": {"media": ref, "mid_size": uploaded["cipher_size"]}}
        elif item_type == media_types.ITEM_TYPE_VIDEO:
            item = {"type": item_type, "video_item": {"media": ref, "video_size": uploaded["cipher_size"]}}
        else:
            item = {
                "type": item_type,
                "file_item": {
                    "media": ref,
                    "file_name": os.path.basename(path),
                    "len": str(uploaded["raw_size"]),
                },
            }

        if caption:
            await self._send_item(to, {"type": 1, "text_item": {"text": caption}}, context_token)
        result = await self._send_item(to, item, context_token)
        logger.info(f"send_media: sent {path} ({mime}) to {to}")
        return result

    async def send_typing(self, user_id: str, typing_ticket: str = "", status: int = 1) -> dict:
        body = {
            "ilink_user_id": user_id,
            "typing_ticket": typing_ticket,
            "status": status,
            "base_info": _build_base_info(),
        }
        return await self._post("ilink/bot/sendtyping", body, timeout_s=10)

    async def get_config(self) -> dict:
        body = {"base_info": _build_base_info()}
        return await self._post("ilink/bot/getconfig", body, timeout_s=10)

    async def notify_start(self) -> dict:
        body = {"base_info": _build_base_info()}
        return await self._post("ilink/bot/msg/notifystart", body, timeout_s=10)

    async def notify_stop(self) -> dict:
        body = {"base_info": _build_base_info()}
        return await self._post("ilink/bot/msg/notifystop", body, timeout_s=10)
