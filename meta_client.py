"""Client gọi THẲNG Meta Graph API (Send API) — KHÔNG qua Pancake.

Đã verify với dữ liệu thật 2026-08-20 (trang Định Hướng): gửi tin thành công qua Graph API, chữ ký webhook
đúng, message_echoes phân biệt đúng bot/người khác qua "app_id" (xem meta_orchestrator.py).

MetaClient giờ THAM SỐ HÓA theo Page Access Token (không còn 1 token cố định toàn cục) — để 1 tiến trình
có thể phục vụ NHIỀU trang cùng lúc, mỗi trang tự có instance MetaClient riêng (xem meta_orchestrator.py).

Tài liệu tham khảo: Meta for Developers > Messenger Platform > Send API / Webhooks.
"""
from __future__ import annotations

import hashlib
import hmac

import httpx

from meta_config import meta_settings


class MetaClient:
    def __init__(self, page_access_token: str) -> None:
        self.token = page_access_token
        self._base = f"{meta_settings.meta_graph_base_url}/{meta_settings.meta_graph_api_version}"
        self._client = httpx.AsyncClient(timeout=30)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _p(self, **extra) -> dict:
        return {"access_token": self.token, **extra}

    async def _send(self, psid: str, payload: dict) -> dict:
        body = {
            "recipient": {"id": psid},
            "messaging_type": "RESPONSE",
            **payload,
        }
        r = await self._client.post(f"{self._base}/me/messages", params=self._p(), json=body)
        r.raise_for_status()
        return r.json()

    async def send_text(self, psid: str, text: str) -> dict:
        return await self._send(psid, {"message": {"text": text}})

    async def send_image(self, psid: str, image_url: str) -> dict:
        return await self._send(
            psid,
            {"message": {"attachment": {"type": "image", "payload": {"url": image_url, "is_reusable": True}}}},
        )


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Xác thực header X-Hub-Signature-256 Meta gửi kèm mỗi webhook — chống giả mạo request.
    signature_header dạng "sha256=<hex>". Trả về False nếu thiếu app_secret (an toàn: chặn hết)."""
    if not meta_settings.meta_app_secret or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(meta_settings.meta_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))
