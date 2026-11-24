"""
Client gọi Pancake Public API — ĐÃ VERIFY với token thật (trang "Vật Lý Siêu Nhẩm").

Base:  https://pages.fm/api/public_api/v1
Auth:  page_access_token (lấy từ Pancake, dùng lâu dài — không hết hạn)

Endpoint đã kiểm chứng:
  GET  /pages/{pid}/tags?page_access_token=                              -> {"tags":[{id,text,color}]}
  GET  /pages/{pid}/conversations?page_access_token=&since=&until=       -> danh sách hội thoại
  GET  /pages/{pid}/conversations/{conv_id}/messages?...&customer_id=UUID-> lịch sử tin nhắn

Endpoint GHI (gửi tin, gắn tag) theo tài liệu Public API — GIỮ NGUYÊN, chưa bắn thử để
tránh nhắn nhầm khách thật. Khi chạy thật lần đầu nên test trên 1 hội thoại nội bộ.

Lưu ý dữ liệu thật:
  - conversation_id có dạ"{page_id}_{psid}"
  - customer_id dùng để đọc tin là UUID của khách (field customers[].id), KHÔNG phải psid
  - tag là ID số (vd 20="Tiềm năng"); lấy map id<->text từ get_tags()
"""
from __future__ import annotations

import time

import httpx

from config import settings


class PancakeClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(base_url=settings.pancake_base_url, timeout=30)
        self.page_id = settings.pancake_page_id
        self.page_token = settings.pancake_page_access_token

    async def aclose(self) -> None:
        await self._client.aclose()

    def _p(self, **extra) -> dict:
        return {"page_access_token": self.page_token, **extra}

    # ---------- Đọc ----------

    async def get_tags(self) -> list[dict]:
        r = await self._client.get(f"/pages/{self.page_id}/tags", params=self._p())
        r.raise_for_status()
        return r.json().get("tags", [])

    async def get_conversations(self, since: int | None = None, until: int | None = None) -> dict:
        until = until or int(time.time())
        since = since or (until - 7 * 86400)
        r = await self._client.get(
            f"/pages/{self.page_id}/conversations", params=self._p(since=since, until=until)
        )
        r.raise_for_status()
        return r.json()

    async def list_conversations_internal(self, current_count: int = 0) -> list[dict]:
        """Liệt kê hội thoại qua Internal API (sắp theo updated_at giảm dần). Dùng cho tái chăm."""
        r = await self._client.get(
            f"{settings.pancake_internal_url}/pages/{self.page_id}/conversations",
            params={"access_token": settings.pancake_access_token, "current_count": current_count},
        )
        r.raise_for_status()
        return r.json().get("conversations", [])

    async def get_messages(self, conversation_id: str, customer_id: str) -> dict:
        """customer_id là UUID của khách (customers[].id)."""
        r = await self._client.get(
            f"/pages/{self.page_id}/conversations/{conversation_id}/messages",
            params=self._p(customer_id=customer_id),
        )
        r.raise_for_status()
        return r.json()

    # ---------- Ghi (theo tài liệu Public API) ----------

    async def send_text(self, conversation_id: str, message: str) -> dict:
        return await self._send(conversation_id, message=message)

    async def send_image(self, conversation_id: str, image_urls: list[str], message: str = "") -> list[dict]:
        """Gửi ảnh — Pancake nhận content_url là 1 URL/tin, nên gửi mỗi ảnh 1 tin.
        Caption (message) chỉ kèm ảnh đầu tiên."""
        results = []
        for i, url in enumerate(image_urls):
            results.append(await self._send(conversation_id, message=message if i == 0 else "", content_url=url))
        return results

    async def _send(self, conversation_id: str, message: str = "", content_url: str | None = None) -> dict:
        payload: dict = {"action": "reply_inbox", "message": message}
        if content_url:
            payload["content_url"] = content_url  # 1 URL dạng chuỗi
        r = await self._client.post(
            f"/pages/{self.page_id}/conversations/{conversation_id}/messages",
            params=self._p(),
            json=payload,
        )
        r.raise_for_status()
        return r.json()

    async def toggle_tag(self, conversation_id: str, tag_id: int, add: bool = True) -> dict:
        """Gắn/gỡ tag (ID số) cho hội thoại."""
        r = await self._client.post(
            f"/pages/{self.page_id}/conversations/{conversation_id}/tags",
            params=self._p(),
            json={"action": "add" if add else "remove", "tag_id": tag_id},
        )
        r.raise_for_status()
        return r.json()


def has_human_reply(messages: list[dict], page_id: str) -> bool:
    """
    True nếu có tin do NHÂN VIÊN THẬT gõ tay qua Pancake (from.id=page_id + admin_name là tên người,
    khác "Public API"). Tin bot tự gửi qua page_access_token có admin_name="Public API" -> loại trừ.

    LƯU Ý: field thật của Pancake là "admin_name" (vd "Trung Đức"), KHÔNG PHẢI "admin_id" như tưởng
    trước đây — kiểm tra thực tế trên 15 hội thoại thật cho thấy "admin_id" không bao giờ xuất hiện,
    còn "admin_name" xuất hiện ở MỌI tin (bot tự gửi mang admin_name="Public API", nhân viên gõ tay
    mang admin_name=tên thật). Bug cũ khiến tính năng "dừng khi nhân viên nhắn tay" không bao giờ hoạt
    động — đã sửa lại đúng field + loại trừ "Public API". Giữ thêm check admin_id để phòng hờ tương thích.
    """
    for m in messages:
        frm = m.get("from") or {}
        if str(frm.get("id", "")) != page_id:
            continue
        admin = frm.get("admin_name") or frm.get("admin_id")
        if admin and admin != "Public API":
            return True
    return False


pancake = PancakeClient()
