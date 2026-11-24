"""Điều phối cho phiên bản Meta Graph API trực tiếp — PHIÊN BẢN MỚI, TÁCH BIỆT HOÀN TOÀN khỏi
orchestrator.py/poller.py hiện có (không import 2 file đó, không sửa chúng).

ĐÃ VERIFY với dữ liệu thật 2026-08-20 (trang Định Hướng, xem lịch sử trao đổi):
  - Webhook nhận đúng "messages" + "message_echoes", chữ ký hợp lệ.
  - Gửi tin thật qua Graph API thành công, gắn tag Pancake thành công.
  - Echo của CHÍNH bot mang "app_id" khớp đúng App ID cấu hình -> KHÔNG bị coi nhầm là người khác can thiệp.
  - Echo của NGƯỜI KHÁC (nhân viên nhắn tay qua Business Suite/Pancake, app_id khác) -> bot tự dừng đúng.
  => Cơ chế nhận diện "nhân viên đã can thiệp" giờ dùng thẳng "app_id" trong echo (đơn giản, không cần tự
     theo dõi message_id đã gửi như bản nháp đầu tiên — vừa đáng tin cậy hơn vừa không cần lưu bền gì thêm
     cho phần này).

ĐA TRANG: 1 tiến trình có thể phục vụ NHIỀU trang cùng lúc (đã xác nhận thực tế — cùng 1 App/webhook nhận
sự kiện của cả 2 trang Định Hướng lẫn News). Khai báo từng trang trong meta_pages.json (Graph API Page Access
Token riêng + trỏ đúng file .env Pancake của trang đó). Trang nào CHƯA có token thật trong meta_pages.json sẽ
tự động bị bỏ qua (an toàn — không cố xử lý với token rỗng).

⚠️ GIỚI HẠN CÒN LẠI (chấp nhận được vì 2 trang Vật Lý hiện dùng CHUNG 1 bộ nội dung): SYSTEM_PROMPT/kịch bản/
category tags (qua ai_service.py) vẫn nạp 1 LẦN theo BOT_ENV_FILE lúc tiến trình khởi động (không đổi được
theo từng trang trong cùng tiến trình) — nếu sau này 1 trang trong meta_pages.json cần nội dung KHÁC hẳn
trang còn lại (khác business_name/kịch bản), cần tách lại thành tiến trình riêng cho trang đó thay vì gộp
chung ở đây.

Trạng thái "nhân viên đã can thiệp" (human_takeover) được LƯU BỀN ra file meta_human_takeover_{page_id}.json
— sống sót qua restart, tránh bot tự động trả lời lại 1 hội thoại nhân viên đã xử lý trước khi restart.

⚠️ ĐÍNH CHÍNH 2026-09-20 (kết luận ngày 2026-08-22 ở trên là SAI): 2 app_id từng bị cho vào "ignore_app_ids"
vì tưởng là nguồn tự động của Facebook, thực chất là CÔNG CỤ NHÂN VIÊN DÙNG ĐỂ NHẮN TAY — tra Graph API xác nhận
đây là Pancake v2 (pages.fm) và FChat (fchat.vn). Hậu quả: cơ chế bắt
"nhân viên can thiệp" qua echo gần như bị VÔ HIỆU, bot nhắn đè lên nhân viên (ca thật: hội thoại
trong ngày 18-19/9). Vì các công cụ này vừa cho nhân viên gõ tay vừa có tính
năng tự động trả lời (cùng 1 app_id), KHÔNG thể phân biệt ở mức app_id -> đã chuyển sang chốt chặn ở mức
TỪNG TIN qua "admin_name" đọc từ lịch sử Pancake (xem _debounce_and_reply).
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import os
import re
import time

import httpx
from dotenv import dotenv_values

from ai_service import ai, has_error_leak
from meta_client import MetaClient
from meta_config import meta_settings
from orchestrator import (
    customer_phones,
    inject_info_bundle,
    inject_schedule_image,
    BOT_TAG,
    parse_reply,
    reconcile_known_format,
    reconcile_known_grade,
    reorder_ack_before_phone_ask,
    resolve_schedule_url,
)

log = logging.getLogger("meta_orchestrator")

PANCAKE_PUBLIC_BASE = "https://pages.fm/api/public_api/v1"
PANCAKE_INTERNAL_BASE = "https://pages.fm/api/v1"

# QUY TRÌNH TIẾP QUẢN (đổi 2026-09-23 theo yêu cầu): nhân viên trả lời tay KHÔNG khoá hội thoại vĩnh viễn.
# Khi khách nhắn tin MỚI mà nhân viên không vào trả lời trong ngần này giây thì BOT TỰ TIẾP QUẢN, nhắn tiếp
# dựa trên toàn bộ lịch sử đã có (kể cả các tin nhân viên đã gõ — chúng vẫn được đưa vào lịch sử cho AI đọc).
STAFF_TAKEOVER_WAIT_SECONDS = 15 * 60


def _warn_if_token_expiring(token: str, label: str, warn_days: float = 3) -> None:
    """Token Pancake là JWT có trường "exp" — đọc ra để CẢNH BÁO TO ngay lúc khởi động khi sắp/đã hết hạn.
    Trước đây token hết hạn chỉ biểu hiện thành 'không tra được customer_id' rải rác trong log, mất hơn 1
    ngày mới phát hiện (sự cố 2026-09-24)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:  # noqa: BLE001
        return  # không giải mã được (không phải JWT) -> bỏ qua, không chặn khởi động
    if not exp:
        return
    left_days = (exp - time.time()) / 86400
    when = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")
    if left_days <= 0:
        # Từ 2026-09-25 bot tra customer_id qua API CÔNG KHAI (page_access_token không hết hạn) nên vẫn
        # chạy bình thường khi token này hết hạn — token nội bộ giờ chỉ còn là phương án dự phòng.
        log.warning("⚠️ %s ĐÃ HẾT HẠN từ %s -> chỉ mất đường dự phòng khi tra hội thoại, bot vẫn chạy bằng "
                    "API công khai. Nên lấy token mới khi tiện.", label, when)
    elif left_days <= warn_days:
        log.warning("⚠️ %s sắp hết hạn (%s, còn %.1f ngày) -> chuẩn bị lấy token mới.", label, when, left_days)
    else:
        log.info("%s còn hạn tới %s (%.0f ngày).", label, when, left_days)


def _parse_pancake_ts(raw: str) -> datetime.datetime | None:
    """Pancake trả inserted_at theo giờ UTC, dạng '2026-09-22T02:15:36.201000' (không có hậu tố múi giờ)."""
    if not raw:
        return None
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "")).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def last_staff_reply_epoch(messages: list[dict], page_id: str) -> float | None:
    """Mốc thời gian (epoch giây) của tin GẦN NHẤT do NHÂN VIÊN gõ tay; None nếu chưa ai gõ tay.
    Phân biệt nhân viên / bot / tự động dựa trên "admin_name" — xem pancake_client.has_human_reply."""
    latest: float | None = None
    for m in messages:
        frm = m.get("from") or {}
        if str(frm.get("id", "")) != page_id:
            continue
        admin = frm.get("admin_name") or frm.get("admin_id")
        if not admin or admin == "Public API":
            continue
        ts = _parse_pancake_ts(m.get("inserted_at", ""))
        if ts and (latest is None or ts.timestamp() > latest):
            latest = ts.timestamp()
    return latest


_ZALO_RE = re.compile(r"\bzalo\b|\bzl\b|\bdalo\b", re.I)
ZALO_PHOTO_BEFORE_SECONDS = 10 * 60


def customer_sent_zalo_contact(messages: list[dict], page_id: str) -> bool:
    """True nếu KHÁCH đã nhắn yêu cầu trao đổi/gửi thông tin qua Zalo rồi SAU ĐÓ gửi ảnh (thường là ảnh mã QR
    hoặc danh thiếp Zalo) — coi như ĐÃ ĐỂ LẠI LIÊN HỆ, xử lý y hệt khách cho SĐT: bot im lặng, không tái chăm,
    để nhân viên kết bạn Zalo (quy tắc thêm 2026-10-05, ca thật trên News).
    Ảnh gửi TRƯỚC câu nhắc Zalo không quá ZALO_PHOTO_BEFORE_SECONDS cũng tính — ca thật trên: khách gửi ảnh
    (mã QR) rồi 12 giây sau mới nhắn "Gửi thông tin cho chị qua zalo nhé"."""
    zalo_times: list[float] = []
    photo_times: list[float] = []
    for m in messages:
        if str((m.get("from") or {}).get("id", "")) == page_id:
            continue
        ts = _parse_pancake_ts(m.get("inserted_at", ""))
        if not ts:
            continue
        if _ZALO_RE.search(m.get("original_message") or m.get("message") or ""):
            zalo_times.append(ts.timestamp())
        if any((a or {}).get("type") == "photo" for a in (m.get("attachments") or [])):
            photo_times.append(ts.timestamp())
    return any(p >= z - ZALO_PHOTO_BEFORE_SECONDS for z in zalo_times for p in photo_times)


class PancakePageReader:
    """Đọc lịch sử + gắn tag Pancake, THAM SỐ HÓA theo trang — KHÔNG dùng chung singleton
    pancake_client.pancake (singleton đó cố định 1 page/token theo BOT_ENV_FILE lúc import, không đổi được
    giữa nhiều trang trong cùng 1 tiến trình). Phạm vi y hệt pancake_client.PancakeClient, chỉ phần đọc/tag."""

    def __init__(self, page_id: str, page_access_token: str, internal_access_token: str) -> None:
        self.page_id = page_id
        self._page_access_token = page_access_token
        self._internal_access_token = internal_access_token
        self._client = httpx.AsyncClient(timeout=30)

    def _p(self, **extra) -> dict:
        return {"page_access_token": self._page_access_token, **extra}

    @staticmethod
    def _raise_if_pancake_error(data: dict, what: str) -> dict:
        """Pancake trả HTTP 200 NGAY CẢ KHI lỗi (token hết hạn, sai quyền...), chỉ báo qua body:
        {"success": false, "error_code": 103, "message": "access_token expired"}.
        Nếu không kiểm tra, code sẽ hiểu nhầm thành "không có hội thoại nào" và IM LẶNG bỏ qua tin khách —
        đã gặp thực tế: token hết hạn 18h00 ngày 2026-09-24, bot ngừng trả lời hoàn toàn hơn 1 ngày mà log
        chỉ ghi "không tra được customer_id", không ai biết nguyên nhân thật."""
        if isinstance(data, dict) and (data.get("success") is False or data.get("error_code")):
            raise RuntimeError(
                f"Pancake báo lỗi khi {what}: error_code={data.get('error_code')} "
                f"message={data.get('message')!r} (kiểm tra PANCAKE_ACCESS_TOKEN còn hạn không)"
            )
        return data

    async def get_tags(self) -> list[dict]:
        r = await self._client.get(f"{PANCAKE_PUBLIC_BASE}/pages/{self.page_id}/tags", params=self._p())
        r.raise_for_status()
        return r.json().get("tags", [])

    async def get_messages(self, conversation_id: str, customer_id: str) -> dict:
        """Đọc lịch sử hội thoại. CÓ THỬ LẠI khi Pancake lỗi tạm thời (5xx) — đã gặp thực tế: 2026-09-21
        Pancake trả 500 đúng lúc bot đọc lịch sử, khiến cả lượt trả lời bị huỷ và khách chờ 6 tiếng mới có
        nhân viên vào. Lỗi 4xx (sai token/tham số) thì KHÔNG thử lại vì thử lại cũng vô ích."""
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = await self._client.get(
                    f"{PANCAKE_PUBLIC_BASE}/pages/{self.page_id}/conversations/{conversation_id}/messages",
                    params=self._p(customer_id=customer_id),
                )
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500 or attempt == 2:
                    raise
                last_exc = e
            except httpx.RequestError as e:  # lỗi mạng/timeout — cũng đáng thử lại
                if attempt == 2:
                    raise
                last_exc = e
            await asyncio.sleep(1.5 * (attempt + 1))
        raise last_exc  # type: ignore[misc]  # không bao giờ tới đây (vòng lặp luôn return hoặc raise)

    async def list_conversations_public(self, since: int, until: int, page_number: int = 1) -> list[dict]:
        """Liệt kê hội thoại qua API CÔNG KHAI — dùng page_access_token (KHÔNG có hạn), khác với
        list_conversations_internal vốn dùng access_token 90 ngày (đã gây sự cố hết hạn 2026-09-24).
        Bắt buộc có since/until (khoảng < 1 tháng) và page_number."""
        r = await self._client.get(
            f"{PANCAKE_PUBLIC_BASE}/pages/{self.page_id}/conversations",
            params=self._p(since=since, until=until, page_number=page_number),
        )
        r.raise_for_status()
        data = self._raise_if_pancake_error(r.json(), "liệt kê hội thoại (API công khai)")
        return data.get("conversations", [])

    async def list_conversations_internal(self, current_count: int = 0) -> list[dict]:
        r = await self._client.get(
            f"{PANCAKE_INTERNAL_BASE}/pages/{self.page_id}/conversations",
            params={"access_token": self._internal_access_token, "current_count": current_count},
        )
        r.raise_for_status()
        data = self._raise_if_pancake_error(r.json(), "liệt kê hội thoại")
        return data.get("conversations", [])

    async def toggle_tag(self, conversation_id: str, tag_id: int, add: bool = True) -> dict:
        r = await self._client.post(
            f"{PANCAKE_PUBLIC_BASE}/pages/{self.page_id}/conversations/{conversation_id}/tags",
            params=self._p(),
            json={"action": "add" if add else "remove", "tag_id": tag_id},
        )
        r.raise_for_status()
        return r.json()


class MetaConversation:
    def __init__(self) -> None:
        self.pending_messages: list[str] = []
        self.task: asyncio.Task | None = None
        self.generation: int = 0
        # True khi phát hiện có tin PAGE gửi mà KHÔNG PHẢI do chính bot này gửi (app_id khác) -> coi như
        # nhân viên đã can thiệp -> dừng hẳn, không tự động xử lý hội thoại này nữa.
        self.human_takeover: bool = False
        # Ảnh bot ĐÃ TỰ GỬI trong tiến trình này (bổ sung cho danh sách đọc lại từ Pancake) — Pancake cần
        # thời gian đồng bộ tin bot vừa gửi (qua webhook riêng từ Facebook), nên nếu khách trả lời rất nhanh
        # (vài giây) ngay sau khi bot gửi ảnh, lượt đọc lịch sử Pancake ở lượt SAU có thể CHƯA thấy ảnh đó ->
        # tưởng chưa gửi -> gửi trùng. Set này cập nhật NGAY khi gửi thành công, không phụ thuộc độ trễ Pancake.
        self.sent_image_urls: set[str] = set()
        # Đã gặp thực tế (News, 2026-08-24, khách Lap Nguyen): khách đã xác nhận "Lớp 10" ở lượt trước, nhưng
        # lượt sau model lại trả known_grade="chưa biết" -> resolve_schedule_url() không tính được khoá ảnh
        # -> bot nói "em gửi chị tham khảo lịch học" nhưng KHÔNG có ảnh nào được gửi kèm. Nhớ lại known_grade
        # gần nhất ĐÃ XÁC ĐỊNH CHẮC CHẮN (khác "chưa biết") để dùng làm phương án dự phòng khi lượt này model
        # "quên" — cùng triết lý với reconcile_known_format (không tin model luôn nhất quán qua các lượt).
        self.known_grade_hint: str = ""
        # Thời điểm (time.monotonic()) tạo task xử lý hiện tại — dùng cho watchdog phát hiện task TREO quá
        # lâu (xem MetaOrchestrator.watchdog_loop). 0 = chưa có task nào đang chạy.
        self.task_started_at: float = 0.0
        # Thời điểm (epoch giây) webhook báo khách vừa nhắn tin mới — mốc để tính 15 phút chờ nhân viên
        # trả lời trước khi bot tiếp quản (xem STAFF_TAKEOVER_WAIT_SECONDS). Dùng giờ nhận webhook thay vì
        # giờ trong lịch sử Pancake vì Pancake đồng bộ trễ, tin vừa đến thường chưa có trong lịch sử.
        self.last_customer_msg_at: float = 0.0


class PageContext:
    def __init__(
        self, page_id: str, name: str, pancake: PancakePageReader, meta: MetaClient,
        active_hours: dict | None = None, ignore_app_ids: list[str] | None = None,
    ) -> None:
        self.page_id = page_id
        self.name = name
        self.pancake = pancake
        self.meta = meta
        self.tag_map: dict[str, int] = {}
        self.conversations: dict[str, MetaConversation] = {}
        self.customer_id_cache: dict[str, str] = {}
        # Khung giờ hoạt động riêng cho trang này — {"start": 22, "end": 6} nghĩa là CHỈ xử lý tin khách
        # trong khoảng 22h-6h sáng hôm sau (qua đêm). None = luôn hoạt động, không giới hạn giờ nào.
        self.active_hours = active_hours
        # app_id của các nguồn TỰ ĐỘNG khác (vd tính năng "Trả lời ngay"/Instant Reply có sẵn của chính
        # Facebook Page) — echo từ các app_id này KHÔNG được coi là nhân viên can thiệp (đã xác minh thực tế
        # 2026-08-22: echo xuất hiện chỉ 1 giây sau tin bot tự gửi, nội dung là câu chào mặc định của Meta,
        # không khớp bất kỳ mẫu câu nào trong kịch bản -> không phải người thật gõ tay).
        self.ignore_app_ids: set[str] = set(ignore_app_ids or [])


def in_active_hours(active_hours: dict | None, now: datetime.datetime | None = None) -> bool:
    """True nếu None (không giới hạn) hoặc giờ hiện tại (giờ hệ thống, coi như giờ VN) nằm trong khung —
    hỗ trợ khung qua đêm (vd start=22, end=6: đúng khi hour>=22 HOẶC hour<6)."""
    if not active_hours:
        return True
    start, end = active_hours.get("start", 0), active_hours.get("end", 24)
    hour = (now or datetime.datetime.now()).hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


class MetaOrchestrator:
    # Task xử lý 1 hội thoại (đọc lịch sử + gọi AI + gửi) bình thường xong trong vòng vài chục giây (60s
    # debounce + tối đa 90s giới hạn cứng gọi AI + dư cho API Pancake/Facebook). Quá 5 phút coi như chắc
    # chắn đã TREO — đã gặp thực tế (News, 2026-09-08): asyncio.wait_for(90s) đôi lúc KHÔNG ngắt được nếu
    # lỗi treo sâu bên trong SDK Gemini không phản hồi cancel đúng cách. Đây là lớp bảo vệ CUỐI CÙNG bên
    # trong tiến trình — không tin tưởng hoàn toàn wait_for một mình.
    STUCK_TASK_SECONDS = 300
    HEARTBEAT_PATH = "bot_heartbeat.txt"
    HEARTBEAT_INTERVAL_SECONDS = 30

    def __init__(self) -> None:
        self._pages: dict[str, PageContext] = {}

    def has_page(self, page_id: str) -> bool:
        return page_id in self._pages

    async def watchdog_loop(self) -> None:
        """Chạy song song suốt đời tiến trình (xem main_meta.py). 2 việc:
        1. Ghi "nhịp tim" ra file mỗi HEARTBEAT_INTERVAL_SECONDS — để watchdog.py (tiến trình NGOÀI, độc
           lập) biết event loop của bot còn sống hay đã treo cứng hoàn toàn (nếu treo cứng thật, ngay cả
           vòng lặp này cũng không tự chạy được nữa -> watchdog.py sẽ thấy nhịp tim cũ và tự khởi động lại
           cả tiến trình).
        2. Tự huỷ (task.cancel()) bất kỳ task xử lý hội thoại nào đã chạy quá STUCK_TASK_SECONDS — lớp bảo
           vệ cho trường hợp NGOÀI 1 (event loop vẫn chạy bình thường, các hội thoại KHÁC vẫn được xử lý
           tốt) nhưng RIÊNG 1 task cụ thể bị treo do lỗi bên trong không tự thoát ra được.
        """
        while True:
            try:
                with open(self.HEARTBEAT_PATH, "w", encoding="utf-8") as f:
                    f.write(str(time.time()))
            except Exception as e:  # noqa: BLE001
                log.warning("Watchdog: lỗi ghi file nhịp tim: %s", e)

            now = time.monotonic()
            for ctx in self._pages.values():
                for conv_id, conv in ctx.conversations.items():
                    task = conv.task
                    if task and not task.done() and conv.task_started_at and (now - conv.task_started_at) > self.STUCK_TASK_SECONDS:
                        log.warning(
                            "[%s] Watchdog: hội thoại %s có task đã chạy quá %ds không xong -> coi như TREO, tự huỷ.",
                            ctx.name, conv_id, self.STUCK_TASK_SECONDS,
                        )
                        task.cancel()
                        conv.task_started_at = 0.0

            await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)

    async def load_pages(self) -> None:
        """Đọc meta_pages.json, dựng PageContext cho từng trang có đủ token thật. Trang thiếu token/thiếu
        cấu hình Pancake sẽ bị bỏ qua an toàn (log rõ lý do), không làm hỏng các trang khác."""
        path = meta_settings.meta_pages_config_path
        if not os.path.exists(path):
            log.error("Không tìm thấy %s -> không có trang nào được nạp.", path)
            return
        with open(path, encoding="utf-8") as f:
            pages_config = json.load(f)

        for page_id, cfg in pages_config.items():
            name = cfg.get("name", page_id)
            token = (cfg.get("meta_page_access_token") or "").strip()
            if not token or token.upper().startswith("TODO"):
                log.warning("Trang %s (%s): chưa có Graph API Page Access Token thật trong %s -> BỎ QUA.",
                            page_id, name, path)
                continue

            env_file = cfg.get("pancake_env_file", "")
            env_vals = dotenv_values(env_file) if env_file else {}
            pancake_page_id = env_vals.get("PANCAKE_PAGE_ID") or page_id
            pancake_token = env_vals.get("PANCAKE_PAGE_ACCESS_TOKEN") or ""
            internal_token = env_vals.get("PANCAKE_ACCESS_TOKEN") or ""
            if not pancake_token or not internal_token:
                log.warning("Trang %s (%s): thiếu PANCAKE_PAGE_ACCESS_TOKEN/PANCAKE_ACCESS_TOKEN trong %s -> BỎ QUA.",
                            page_id, name, env_file)
                continue

            _warn_if_token_expiring(internal_token, f"PANCAKE_ACCESS_TOKEN ({env_file})")
            reader = PancakePageReader(pancake_page_id, pancake_token, internal_token)
            ctx = PageContext(page_id, name, reader, MetaClient(token), active_hours=cfg.get("active_hours"),
                               ignore_app_ids=cfg.get("ignore_app_ids"))
            if ctx.ignore_app_ids:
                log.info("Trang %s (%s): bỏ qua %d app_id tự động (không tính là nhân viên can thiệp): %s",
                          page_id, name, len(ctx.ignore_app_ids), sorted(ctx.ignore_app_ids))
            if ctx.active_hours:
                log.info("Trang %s (%s): chỉ hoạt động trong khung %sh-%sh.",
                          page_id, name, ctx.active_hours.get("start"), ctx.active_hours.get("end"))
            try:
                for tag in await reader.get_tags():
                    ctx.tag_map[tag["text"].strip()] = tag["id"]
                log.info("Trang %s (%s): nạp %d tag.", page_id, name, len(ctx.tag_map))
            except Exception as e:  # noqa: BLE001
                log.warning("Trang %s (%s): không nạp được tag: %s", page_id, name, e)

            self._load_human_takeover_state(ctx)
            self._load_customer_index(ctx)
            self._pages[page_id] = ctx

        log.info("Meta orchestrator: sẵn sàng %d trang: %s",
                  len(self._pages), [f"{p.page_id}({p.name})" for p in self._pages.values()])

    @staticmethod
    def conv_id_for(ctx: PageContext, psid: str) -> str:
        """Ghép đúng định dạng conversation_id mà Pancake dùng ({page_id}_{psid}) — không cần tra cứu/đối
        chiếu tên, vì Pancake xây conversation_id trực tiếp từ page_id + psid."""
        return f"{ctx.pancake.page_id}_{psid}"

    @staticmethod
    def _find_conv_customer_id(convs: list[dict], conv_id: str) -> str | None:
        for c in convs:
            if c.get("id") == conv_id:
                return (c.get("customers") or [{}])[0].get("id", "") or None
        return None

    async def _lookup_customer_id_public(self, ctx: PageContext, conv_id: str, days: int = 7,
                                          max_pages: int = 5) -> str | None:
        """Tra customer_id qua API CÔNG KHAI (page_access_token KHÔNG hết hạn). Chỉ quét các hội thoại có
        hoạt động trong `days` ngày gần nhất — đủ cho luồng trả lời khách (khách đang nhắn = hội thoại vừa
        có hoạt động), và nhanh hơn hẳn quét tuần tự cả nghìn hội thoại qua API nội bộ."""
        now = int(time.time())
        for page in range(1, max_pages + 1):
            try:
                convs = await ctx.pancake.list_conversations_public(now - days * 86400, now, page)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] Lỗi tra customer_id qua API công khai (conv=%s, trang %d): %s",
                            ctx.name, conv_id, page, e)
                return None
            if not convs:
                return None
            cust_id = self._find_conv_customer_id(convs, conv_id)
            if cust_id:
                return cust_id
        return None

    # ---------- Danh bạ "hội thoại -> customer_id" (khách cũ quay lại) ----------
    # Danh sách hội thoại API công khai lọc since/until theo NGÀY TẠO hội thoại (đã kiểm chứng 2026-10-05), nên
    # khách nhắn lần đầu từ vài tháng trước rồi nay quay lại thì KHÔNG có trong "7 ngày gần nhất" -> trước đây
    # bot bỏ qua luôn (12 lượt/3 ngày). Tìm lùi từng tháng lúc khách nhắn thì chậm (40-60s) và gọi song song bị
    # Pancake chặn 429 -> dựng sẵn danh bạ ~1 năm lúc khởi động (chạy ngầm, làm mới mỗi ngày), lưu ra file.
    INDEX_WINDOWS = 13          # 13 cửa sổ x 28 ngày ~ 1 năm
    INDEX_WINDOW_DAYS = 28      # API yêu cầu since/until < 1 tháng
    INDEX_REFRESH_SECONDS = 24 * 3600

    @staticmethod
    def _customer_index_path(ctx: PageContext) -> str:
        return f"meta_customer_index_{ctx.page_id}.json"

    def _load_customer_index(self, ctx: PageContext) -> None:
        try:
            with open(self._customer_index_path(ctx), encoding="utf-8") as f:
                ctx.customer_id_cache.update(json.load(f))
            log.info("Trang %s (%s): nạp danh bạ %d hội thoại.", ctx.page_id, ctx.name, len(ctx.customer_id_cache))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("Trang %s (%s): lỗi đọc danh bạ hội thoại: %s", ctx.page_id, ctx.name, e)

    async def _list_public_with_retry(self, ctx: PageContext, since: int, until: int, page: int) -> list[dict] | None:
        """None = lỗi sau khi đã thử lại (khác [] = hết dữ liệu). Pancake chặn 429 khi gọi dồn -> nghỉ lâu dần."""
        for attempt in range(4):
            try:
                return await ctx.pancake.list_conversations_public(since, until, page)
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    log.warning("[%s] Lỗi liệt kê hội thoại (trang %d) sau 4 lần thử: %s", ctx.name, page, e)
                    return None
                await asyncio.sleep(5 * (attempt + 1))
        return None

    async def _scan_windows(self, ctx: PageContext, windows: int, stop_at: str | None = None,
                            pause: float = 0.5) -> str | None:
        """Quét TUẦN TỰ các cửa sổ 28 ngày lùi dần, nạp mọi hội thoại gặp được vào ctx.customer_id_cache.
        Có stop_at (conv_id) thì dừng ngay khi gặp và trả customer_id của nó."""
        now = int(time.time())
        for w in range(windows):
            until = now - w * self.INDEX_WINDOW_DAYS * 86400
            since = until - self.INDEX_WINDOW_DAYS * 86400
            for page in range(1, 60):
                convs = await self._list_public_with_retry(ctx, since, until, page)
                if not convs:
                    break  # hết dữ liệu (hoặc lỗi kéo dài) -> sang cửa sổ cũ hơn
                for c in convs:
                    cust = (c.get("customers") or [{}])[0].get("id")
                    if c.get("id") and cust:
                        ctx.customer_id_cache[c["id"]] = cust
                if stop_at and stop_at in ctx.customer_id_cache:
                    log.info("[%s] Hội thoại %s là khách cũ (tìm thấy trong khoảng %d-%d ngày trước).", ctx.name,
                             stop_at, w * self.INDEX_WINDOW_DAYS, (w + 1) * self.INDEX_WINDOW_DAYS)
                    return ctx.customer_id_cache[stop_at]
                await asyncio.sleep(pause)
        return None

    async def customer_index_loop(self) -> None:
        """Chạy ngầm suốt đời tiến trình (main_meta.py): dựng/làm mới danh bạ cho từng trang, lưu ra file."""
        await asyncio.sleep(30)  # nhường bot khởi động xong
        while True:
            for ctx in list(self._pages.values()):
                before = len(ctx.customer_id_cache)
                t0 = time.time()
                try:
                    await self._scan_windows(ctx, self.INDEX_WINDOWS, pause=1.0)
                    with open(self._customer_index_path(ctx), "w", encoding="utf-8") as f:
                        json.dump(ctx.customer_id_cache, f)
                    log.info("[%s] Danh bạ hội thoại: %d (thêm %d) — quét %.0fs.", ctx.name,
                             len(ctx.customer_id_cache), len(ctx.customer_id_cache) - before, time.time() - t0)
                except Exception as e:  # noqa: BLE001
                    log.warning("[%s] Lỗi dựng danh bạ hội thoại: %s", ctx.name, e)
            await asyncio.sleep(self.INDEX_REFRESH_SECONDS)

    async def _resolve_customer_id(self, ctx: PageContext, conv_id: str) -> str | None:
        if conv_id in ctx.customer_id_cache:
            return ctx.customer_id_cache[conv_id]

        # Bước 1: API CÔNG KHAI trước — token không hết hạn nên không chết theo access_token 90 ngày
        # (sự cố 2026-09-24: token nội bộ hết hạn -> bot bỏ rơi 100% tin khách hơn 1 ngày).
        # Thử lại vài lần vì hội thoại vừa tạo có thể chưa kịp đồng bộ sang Pancake.
        # Lần đầu thử nhanh; không thấy thì tìm sâu NGAY (khách cũ) — tìm sâu gồm cả 28 ngày gần nhất nên
        # cũng đủ thời gian cho hội thoại vừa tạo kịp đồng bộ, không cần chờ thêm các lượt thử lại.
        cust_id = await self._lookup_customer_id_public(ctx, conv_id)
        if cust_id:
            ctx.customer_id_cache[conv_id] = cust_id
            return cust_id

        # Bước 1b: KHÁCH CŨ chưa có trong danh bạ (vd danh bạ chưa dựng xong lúc mới khởi động) -> tìm lùi từng
        # tháng ngay lúc này (chậm hơn, 15-60s) — xem customer_index_loop.
        cust_id = await self._scan_windows(ctx, self.INDEX_WINDOWS, stop_at=conv_id)
        if cust_id:
            ctx.customer_id_cache[conv_id] = cust_id
            return cust_id

        # Hội thoại MỚI TINH có thể chưa kịp đồng bộ sang Pancake -> thử nhanh thêm vài lần.
        for _ in range(2):
            await asyncio.sleep(2)
            cust_id = await self._lookup_customer_id_public(ctx, conv_id)
            if cust_id:
                ctx.customer_id_cache[conv_id] = cust_id
                return cust_id

        # Bước 2: dự phòng — API nội bộ (access_token 90 ngày). Hội thoại MỚI luôn nổi lên đầu danh sách,
        # nên thử vài lần ở đầu trước khi quét sâu (vừa chậm vừa vô ích nếu chỉ là chưa kịp đồng bộ).
        first_page: list[dict] | None = None
        for attempt in range(4):
            try:
                first_page = await ctx.pancake.list_conversations_internal(0)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] Lỗi tra customer_id (conv=%s, lần %d): %s", ctx.name, conv_id, attempt, e)
                return None
            cust_id = self._find_conv_customer_id(first_page, conv_id)
            if cust_id:
                ctx.customer_id_cache[conv_id] = cust_id
                return cust_id
            if attempt < 3:
                await asyncio.sleep(1.5)

        # Bước 2: không thấy ở đầu -> có thể là hội thoại cũ hơn, quét sâu (có nghỉ giữa các lần gọi).
        cc = len(first_page) if first_page else 0
        while True:
            try:
                convs = await ctx.pancake.list_conversations_internal(cc)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] Lỗi tra customer_id (conv=%s, đã quét %d): %s", ctx.name, conv_id, cc, e)
                return None
            if not convs:
                return None
            cust_id = self._find_conv_customer_id(convs, conv_id)
            if cust_id:
                ctx.customer_id_cache[conv_id] = cust_id
                return cust_id
            cc += len(convs)
            if cc > 6000:
                return None
            await asyncio.sleep(0.4)  # tránh 429: quét dồn dập không nghỉ từng gây lỗi liên tục nhiều giờ (xem reengage.py)

    # ---------- Lưu bền trạng thái "nhân viên đã can thiệp" ----------

    @staticmethod
    def _human_takeover_path(ctx: PageContext) -> str:
        return f"meta_human_takeover_{ctx.page_id}.json"

    def _load_human_takeover_state(self, ctx: PageContext) -> None:
        path = self._human_takeover_path(ctx)
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            return
        conv_ids = data.get("human_takeover", [])
        for conv_id in conv_ids:
            conv = ctx.conversations.setdefault(conv_id, MetaConversation())
            conv.human_takeover = True
        if conv_ids:
            log.info("Trang %s (%s): nạp lại %d hội thoại đã bị nhân viên can thiệp trước đó (từ %s).",
                      ctx.page_id, ctx.name, len(conv_ids), path)

    def _save_human_takeover_state(self, ctx: PageContext) -> None:
        conv_ids = [cid for cid, c in ctx.conversations.items() if c.human_takeover]
        with open(self._human_takeover_path(ctx), "w", encoding="utf-8") as f:
            json.dump({"human_takeover": conv_ids}, f, ensure_ascii=False, indent=1)

    # ---------- Nhận webhook ----------

    async def handle_event(self, page_id: str, event: dict) -> None:
        ctx = self._pages.get(page_id)
        if not ctx:
            log.warning("Webhook: sự kiện của trang %s (chưa cấu hình trong meta_pages.json) -> bỏ qua.", page_id)
            return  # trang chưa cấu hình/chưa có token thật -> bỏ qua an toàn

        message = event.get("message") or {}
        is_echo = bool(message.get("is_echo"))

        if is_echo:
            await self._handle_echo(ctx, event, message)
            return

        psid = (event.get("sender") or {}).get("id", "")
        text = (message.get("text") or "").strip()
        # Khách bấm nút gợi ý của page (vd "Tôi muốn tìm chỗ học vật lý Online cho con", "Bắt đầu") -> Facebook
        # gửi dạng "postback", không phải "message". Coi chữ trên nút như khách tự gõ (trước đây bị bỏ qua,
        # mất khách — vd News 2026-10-04 21:04).
        postback = event.get("postback") or {}
        if not text and postback:
            text = (postback.get("title") or postback.get("payload") or "").strip()
            if text:
                log.info("[%s] Khách bấm nút gợi ý (postback) -> xử lý như tin nhắn: %r", ctx.name, text[:80])
        if not psid or not text:
            log.info("[%s] Webhook: bỏ qua event không phải tin nhắn văn bản (psid=%r, keys=%s).",
                      ctx.name, psid, list(event.keys()))
            return
        log.info("[%s] Nhận tin khách (psid=%s): %r", ctx.name, psid, text[:80])
        await self.on_customer_message(ctx, psid, text)

    async def _handle_echo(self, ctx: PageContext, event: dict, message: dict) -> None:
        """Tin do chính PAGE gửi (echo). So "app_id" trong echo với App ID đang cấu hình — khớp thì đúng bot
        này gửi (bình thường); khác thì GHI NHẬN là có nguồn khác (nhân viên/công cụ) tham gia hội thoại.

        LƯU Ý: từ 2026-09-23 việc ghi nhận này CHỈ mang tính đánh dấu, KHÔNG còn khoá hội thoại vĩnh viễn.
        Quyết định "im lặng nhường nhân viên" hay "bot tiếp quản" nằm ở _debounce_and_reply, dựa trên mốc
        thời gian thật đọc từ lịch sử Pancake (nhân viên im quá 15 phút thì bot tiếp quản). Lý do: echo chỉ
        có app_id của CÔNG CỤ (Pancake v2 / FChat / Page Inbox), không cho biết người thật gõ hay máy tự
        trả lời — chỉ "admin_name" trong lịch sử Pancake mới phân biệt được."""
        sent_app_id = str(message.get("app_id", ""))
        if sent_app_id == meta_settings.meta_app_id:
            return
        if sent_app_id in ctx.ignore_app_ids:
            log.info("[%s] Echo từ app_id=%s (đã biết là nguồn tự động, không phải nhân viên) -> bỏ qua.",
                      ctx.name, sent_app_id)
            return
        psid = (event.get("recipient") or {}).get("id", "")
        if not psid:
            return
        conv_id = self.conv_id_for(ctx, psid)
        conv = ctx.conversations.setdefault(conv_id, MetaConversation())
        if not conv.human_takeover:
            conv.human_takeover = True
            self._save_human_takeover_state(ctx)
            log.info(
                "[%s] Hội thoại %s: có tin PAGE gửi từ app_id=%s (không phải bot) -> ghi nhận có nguồn khác "
                "tham gia. Bot vẫn theo quy tắc: nhường nhân viên, chỉ tiếp quản nếu quá %d phút không ai trả lời.",
                ctx.name, conv_id, sent_app_id, STAFF_TAKEOVER_WAIT_SECONDS // 60,
            )

    # ---------- Xử lý tin khách (debounce + generation) ----------

    async def on_customer_message(self, ctx: PageContext, psid: str, text: str) -> None:
        if not in_active_hours(ctx.active_hours):
            log.info("[%s] Ngoài khung giờ hoạt động (%sh-%sh) -> bỏ qua tin khách, không xử lý gì.",
                      ctx.name, ctx.active_hours.get("start"), ctx.active_hours.get("end"))
            return
        conv_id = self.conv_id_for(ctx, psid)
        conv = ctx.conversations.setdefault(conv_id, MetaConversation())
        # LƯU Ý (đổi quy trình 2026-09-23): nhân viên đã tham gia KHÔNG còn khoá hội thoại vĩnh viễn nữa —
        # vẫn nhận tin bình thường, việc "nhân viên đang phụ trách hay bot tiếp quản" do _debounce_and_reply
        # quyết định dựa trên mốc thời gian (xem STAFF_TAKEOVER_WAIT_SECONDS).
        if conv.human_takeover:
            log.info("[%s] Hội thoại %s: nhân viên từng tham gia -> sẽ đợi %d phút xem nhân viên có trả lời không.",
                      ctx.name, conv_id, STAFF_TAKEOVER_WAIT_SECONDS // 60)
        conv.last_customer_msg_at = time.time()
        conv.pending_messages.append(text)
        conv.generation += 1
        if conv.task and not conv.task.done():
            conv.task.cancel()
        conv.task = asyncio.create_task(self._debounce_and_reply(ctx, conv_id, psid))
        conv.task_started_at = time.monotonic()

    async def _debounce_and_reply(self, ctx: PageContext, conv_id: str, psid: str) -> None:
        conv = ctx.conversations[conv_id]
        my_generation = conv.generation

        try:
            await asyncio.sleep(meta_settings.debounce_seconds)
        except asyncio.CancelledError:
            return

        if conv.generation != my_generation or not conv.pending_messages:
            return
        pending = conv.pending_messages
        conv.pending_messages = []
        merged = "\n".join(pending)

        try:
            customer_id = await self._resolve_customer_id(ctx, conv_id)
            if not customer_id:
                log.warning("[%s] Hội thoại %s: không tra được customer_id Pancake -> bỏ qua.", ctx.name, conv_id)
                return
            data = await ctx.pancake.get_messages(conv_id, customer_id)
            raw_messages = data.get("messages", [])
            history = self._to_plain_history(ctx, raw_messages, exclude=pending)
            sent_image_urls = self._collect_sent_image_urls(ctx, raw_messages)
            log.info("[%s] Hội thoại %s: đọc %d tin lịch sử (qua Pancake, chỉ đọc) trước khi trả lời.",
                     ctx.name, conv_id, len(history))

            # CHỐT CHẶN "NHÂN VIÊN ĐÃ VÀO" — đọc THẲNG từ lịch sử Pancake mỗi lượt, KHÔNG phụ thuộc việc bot
            # có đang chạy lúc nhân viên nhắn hay không. Cần thiết vì cơ chế cũ (bắt echo webhook + file
            # meta_human_takeover_*.json) CHỈ phát hiện được khi bot đang bật: nhân viên trả lời trong lúc bot
            # tắt/treo/hỏng token thì sau khi bật lại bot vẫn tưởng hội thoại chưa ai xử lý và nhắn đè lên.
            # Phân biệt dựa trên "admin_name" — đã kiểm chứng bằng dữ liệu thật 2026-09-20:
            #   - nhân viên gõ tay (qua Pancake/FChat): from = {admin_name:'Trung Đức', id, name, uid}
            #   - tin bot tự gửi qua Graph API:         from = {id, name}                 (không có admin_name)
            #   - tin chào tự động của công cụ:         from = {id, name} / +ai_generated (không có admin_name)
            #   - tin bot gửi qua Public API (bản cũ):  admin_name = 'Public API'         (đã loại trừ sẵn)
            staff_at = last_staff_reply_epoch(raw_messages, ctx.page_id)
            if staff_at is not None:
                if not conv.human_takeover:
                    conv.human_takeover = True
                    self._save_human_takeover_state(ctx)
                customer_at = conv.last_customer_msg_at or time.time()

                # Nhân viên đã trả lời SAU tin mới nhất của khách -> họ đang trực tiếp xử lý, bot im lặng.
                if staff_at >= customer_at:
                    log.info("[%s] Hội thoại %s: nhân viên vừa trả lời sau tin khách -> bot im lặng, nhường "
                             "nhân viên.", ctx.name, conv_id)
                    return

                # Khách vừa nhắn mà nhân viên CHƯA trả lời -> đợi đủ 15 phút rồi mới tiếp quản.
                remaining = customer_at + STAFF_TAKEOVER_WAIT_SECONDS - time.time()
                if remaining > 0:
                    log.info("[%s] Hội thoại %s: nhân viên từng phụ trách nhưng chưa trả lời tin mới -> đợi "
                             "%.0f phút xem nhân viên có vào không.", ctx.name, conv_id, remaining / 60)
                    # Đẩy mốc watchdog lên sau khoảng đợi, tránh bị coi nhầm là task TREO và huỷ giữa chừng.
                    conv.task_started_at = time.monotonic() + remaining
                    await asyncio.sleep(remaining)
                    if conv.generation != my_generation:
                        log.info("[%s] Hội thoại %s: có tin khách mới hơn trong lúc đợi -> nhường lượt sau.",
                                 ctx.name, conv_id)
                        return
                    # Đọc lại lịch sử: nhân viên có thể đã vào trả lời trong lúc bot đợi.
                    data = await ctx.pancake.get_messages(conv_id, customer_id)
                    raw_messages = data.get("messages", [])
                    history = self._to_plain_history(ctx, raw_messages, exclude=pending)
                    sent_image_urls = self._collect_sent_image_urls(ctx, raw_messages)
                    staff_at = last_staff_reply_epoch(raw_messages, ctx.page_id)
                    if staff_at is not None and staff_at >= customer_at:
                        log.info("[%s] Hội thoại %s: nhân viên đã trả lời trong lúc bot đợi -> bot im lặng.",
                                 ctx.name, conv_id)
                        return

                log.warning("[%s] Hội thoại %s: quá %d phút nhân viên không trả lời tin khách -> BOT TIẾP QUẢN, "
                            "nhắn tiếp dựa trên lịch sử đã có.", ctx.name, conv_id,
                            STAFF_TAKEOVER_WAIT_SECONDS // 60)

            if meta_settings.stop_when_has_phone and len(customer_phones(history)) >= meta_settings.required_phones:
                log.info("[%s] Hội thoại %s: đã đủ SĐT -> KHÔNG trả lời thêm.", ctx.name, conv_id)
                return
            if customer_sent_zalo_contact(raw_messages, ctx.pancake.page_id):
                log.info("[%s] Hội thoại %s: khách đã xin trao đổi qua Zalo và gửi ảnh -> coi như đã để lại liên "
                         "hệ (như cho SĐT) -> KHÔNG trả lời thêm.", ctx.name, conv_id)
                return

            # Đã gặp thực tế (nhiều lần, gồm 2 ca ngày 2026-09-05 — 1 ca khách VỪA CHO SĐT): dù AIService đã
            # có http_options.timeout=45s + retry riêng, lệnh generate() đôi lúc vẫn TREO VĨNH VIỄN (không
            # timeout, không exception, không log gì tiếp) — timeout của SDK rõ ràng không phủ hết mọi kiểu
            # treo (vd treo ở bước DNS/kết nối trước khi httpx kịp tính giờ). Đặt thêm 1 trần cứng Ở NGOÀI
            # bằng asyncio.wait_for để chắc chắn không bao giờ treo quá thời gian này, dù SDK có lỗi gì.
            result = await asyncio.wait_for(ai.generate(history, merged), timeout=90)

            if conv.generation != my_generation:
                log.info("[%s] Hội thoại %s: đã có lượt mới hơn trong lúc AI soạn -> hủy gửi lượt này.", ctx.name, conv_id)
                return
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] Lỗi AI (meta) %s: %s", ctx.name, conv_id, e)
            return

        reply = (result.get("reply") or "").strip()
        if reply and has_error_leak(reply):
            log.warning("[%s] Hội thoại %s: reply có dấu hiệu rò rỉ lỗi kỹ thuật -> KHÔNG gửi.", ctx.name, conv_id)
            reply = ""
        if not reply:
            return

        actions = reorder_ack_before_phone_ask(parse_reply(reply))
        known_grade = result.get("known_grade", "")
        if known_grade and known_grade != "chưa biết":
            conv.known_grade_hint = known_grade
        elif conv.known_grade_hint:
            known_grade = conv.known_grade_hint
        else:
            known_grade = reconcile_known_grade(known_grade, history, merged)
            if known_grade and known_grade != "chưa biết":
                conv.known_grade_hint = known_grade
        known_format = reconcile_known_format(reply, result.get("known_format", ""), history, merged)
        # Hợp cả 2 nguồn: lịch sử đọc từ Pancake (có thể trễ) + ảnh bot vừa tự gửi trong tiến trình này
        # (luôn cập nhật tức thời) — tránh gửi trùng khi khách trả lời quá nhanh, trước khi Pancake kịp
        # đồng bộ tin bot vừa gửi.
        dedup_urls = set() if result.get("resend_images") else (sent_image_urls | conv.sent_image_urls)
        schedule_url = resolve_schedule_url(known_grade, known_format)
        actions = inject_schedule_image(actions, schedule_url, dedup_urls)
        actions = inject_info_bundle(actions, known_grade, known_format, dedup_urls)

        for i, (kind, payload) in enumerate(actions):
            try:
                if kind == "text":
                    await ctx.meta.send_text(psid, payload)  # type: ignore[arg-type]
                else:
                    for url in payload:  # type: ignore[union-attr]
                        await ctx.meta.send_image(psid, url)
                        conv.sent_image_urls.add(url)
                # Facebook KHÔNG đảm bảo giao đúng thứ tự cho các tin gửi liên tiếp gần như tức thì — API
                # trả "đã nhận" ngay nhưng việc GIAO thực tế cho khách có thể bị đảo, kể cả giữa 2 tin CHỮ
                # với nhau (đã gặp thực tế: News 2026-08-25 khách Đặng Thắm — ảnh lịch học hiện SAU CÙNG dù
                # gửi ngay sau câu dẫn; News 2026-08-27 khách Loan Nguyễn — 2 tin CHỮ liên tiếp [nhận xét
                # rồi mới xin SĐT] bị đảo thành [xin SĐT rồi mới nhận xét] dù code đã gửi đúng thứ tự, đã
                # verify qua log chỉ có đúng 2 lệnh gửi cho lượt này). Đợi thêm 1 chút sau MỌI hành động
                # (không chỉ ảnh, chỉ khi còn hành động tiếp theo) để giảm khả năng bị đảo thứ tự hiển thị.
                if i < len(actions) - 1:
                    await asyncio.sleep(1.5)
            except Exception as e:  # noqa: BLE001
                log.exception("[%s] Lỗi gửi (meta) %s: %s", ctx.name, conv_id, e)

        if actions:
            await self._apply_tag(ctx, conv_id, BOT_TAG)
        for tag_name in result.get("tags", []):
            await self._apply_tag(ctx, conv_id, tag_name)

    async def _apply_tag(self, ctx: PageContext, conv_id: str, tag_name: str) -> None:
        tag_id = ctx.tag_map.get(tag_name.strip())
        if tag_id is None:
            log.warning("[%s] Chưa map được tag '%s' (kiểm tra tag này có tồn tại trên Pancake không)", ctx.name, tag_name)
            return
        try:
            await ctx.pancake.toggle_tag(conv_id, tag_id, add=True)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] Lỗi gắn tag %s: %s", ctx.name, tag_name, e)

    @staticmethod
    def _collect_sent_image_urls(ctx: PageContext, messages: list[dict]) -> set[str]:
        """Tập URL ảnh BOT đã từng gửi trong hội thoại (đọc qua Pancake) — y hệt logic trong
        orchestrator._build_history, để inject_schedule_image/inject_info_bundle không gửi trùng."""
        urls: set[str] = set()
        for m in messages:
            if str((m.get("from") or {}).get("id", "")) != ctx.pancake.page_id:
                continue
            for att in m.get("attachments") or []:
                url = att.get("url")
                if url:
                    urls.add(url)
        return urls

    @staticmethod
    def _to_plain_history(ctx: PageContext, messages: list[dict], exclude: list[str]) -> list[dict]:
        page_id = ctx.pancake.page_id
        history: list[dict] = []
        for m in messages[-meta_settings.history_messages:]:
            sender = str((m.get("from") or {}).get("id", ""))
            role = "assistant" if sender == page_id else "user"
            raw = m.get("message") or m.get("original_message") or ""
            content = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
            if content:
                history.append({"role": role, "content": content})
        pending = {p.strip() for p in exclude}
        while history and history[-1]["role"] == "user" and history[-1]["content"] in pending:
            history.pop()
        return history


meta_orchestrator = MetaOrchestrator()
