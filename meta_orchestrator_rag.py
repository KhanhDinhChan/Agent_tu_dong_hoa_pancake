"""Điều phối PHIÊN BẢN RAG (định tuyến kịch bản theo lớp) — TÁCH BIỆT HOÀN TOÀN khỏi meta_orchestrator.py
đang chạy (không sửa, không import lẫn 2 orchestrator vào nhau). Dùng lại các phần HẠ TẦNG THUẦN không
liên quan tới nội dung/AI — PancakePageReader, MetaClient (từ meta_client.py) — vì đây là phần đọc/ghi
Pancake + gửi Graph API đã ổn định, tách biệt hoàn toàn khỏi cách dựng prompt.

Khác biệt DUY NHẤT so với meta_orchestrator.py: gọi ai_service_rag.ai_rag thay vì ai_service.ai, và
mỗi hội thoại tự nhớ "known_grade" của lượt gần nhất (grade_hint) để ĐỊNH TUYẾN kịch bản cho lượt sau —
lượt đầu tiên (chưa có gì để nhớ) dò nhanh bằng từ khóa trong chính tin nhắn khách (rag_router.detect_
grade_hint), không đợi có known_grade mới định tuyến được.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from dotenv import dotenv_values

from ai_service_rag import ai_rag, has_error_leak
from meta_client import MetaClient
from meta_config import meta_settings
from meta_orchestrator import (
    STAFF_TAKEOVER_WAIT_SECONDS,
    PancakePageReader,
    in_active_hours,
    last_staff_reply_epoch,
)
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
from rag_router import detect_grade_hint

log = logging.getLogger("meta_orchestrator_rag")


class RagConversation:
    def __init__(self) -> None:
        self.pending_messages: list[str] = []
        self.task: asyncio.Task | None = None
        self.generation: int = 0
        self.human_takeover: bool = False
        # Ghi nhớ known_grade của lượt AI gần nhất trong hội thoại này — dùng làm gợi ý định tuyến kịch
        # bản cho lượt SAU (rag_router.build_playbook), KHÔNG phải nguồn sự thật — mỗi lượt AI vẫn tự đọc
        # lại toàn bộ lịch sử và tự quyết known_grade thật như bình thường.
        self.known_grade_hint: str = ""
        # Ảnh bot ĐÃ TỰ GỬI trong tiến trình này — bổ sung cho lịch sử đọc từ Pancake (có thể trễ đồng bộ),
        # tránh gửi trùng khi khách trả lời rất nhanh ngay sau khi bot gửi ảnh (xem meta_orchestrator.py).
        self.sent_image_urls: set[str] = set()
        # Mốc (epoch giây) webhook báo khách vừa nhắn — để tính 15 phút chờ nhân viên trả lời
        # trước khi bot tiếp quản (xem meta_orchestrator.STAFF_TAKEOVER_WAIT_SECONDS).
        self.last_customer_msg_at: float = 0.0


class RagPageContext:
    def __init__(
        self, page_id: str, name: str, pancake: PancakePageReader, meta: MetaClient,
        active_hours: dict | None = None, ignore_app_ids: list[str] | None = None,
    ) -> None:
        self.page_id = page_id
        self.name = name
        self.pancake = pancake
        self.meta = meta
        self.tag_map: dict[str, int] = {}
        self.conversations: dict[str, RagConversation] = {}
        self.customer_id_cache: dict[str, str] = {}
        self.active_hours = active_hours
        self.ignore_app_ids: set[str] = set(ignore_app_ids or [])


class RagOrchestrator:
    def __init__(self) -> None:
        self._pages: dict[str, RagPageContext] = {}

    def has_page(self, page_id: str) -> bool:
        return page_id in self._pages

    async def load_pages(self) -> None:
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
                log.warning("[RAG] Trang %s (%s): chưa có token thật -> BỎ QUA.", page_id, name)
                continue

            env_file = cfg.get("pancake_env_file", "")
            env_vals = dotenv_values(env_file) if env_file else {}
            pancake_page_id = env_vals.get("PANCAKE_PAGE_ID") or page_id
            pancake_token = env_vals.get("PANCAKE_PAGE_ACCESS_TOKEN") or ""
            internal_token = env_vals.get("PANCAKE_ACCESS_TOKEN") or ""
            if not pancake_token or not internal_token:
                log.warning("[RAG] Trang %s (%s): thiếu token Pancake trong %s -> BỎ QUA.", page_id, name, env_file)
                continue

            reader = PancakePageReader(pancake_page_id, pancake_token, internal_token)
            ctx = RagPageContext(page_id, name, reader, MetaClient(token), active_hours=cfg.get("active_hours"),
                                  ignore_app_ids=cfg.get("ignore_app_ids"))
            try:
                for tag in await reader.get_tags():
                    ctx.tag_map[tag["text"].strip()] = tag["id"]
                log.info("[RAG] Trang %s (%s): nạp %d tag.", page_id, name, len(ctx.tag_map))
            except Exception as e:  # noqa: BLE001
                log.warning("[RAG] Trang %s (%s): không nạp được tag: %s", page_id, name, e)

            self._pages[page_id] = ctx

        log.info("[RAG] Orchestrator: sẵn sàng %d trang: %s",
                  len(self._pages), [f"{p.page_id}({p.name})" for p in self._pages.values()])

    @staticmethod
    def conv_id_for(ctx: RagPageContext, psid: str) -> str:
        return f"{ctx.pancake.page_id}_{psid}"

    @staticmethod
    def _find_conv_customer_id(convs: list[dict], conv_id: str) -> str | None:
        for c in convs:
            if c.get("id") == conv_id:
                return (c.get("customers") or [{}])[0].get("id", "") or None
        return None

    async def _resolve_customer_id(self, ctx: RagPageContext, conv_id: str) -> str | None:
        if conv_id in ctx.customer_id_cache:
            return ctx.customer_id_cache[conv_id]

        # Bước 1: hội thoại MỚI luôn nổi lên đầu danh sách Pancake, nhưng Pancake có thể
        # chưa kịp đồng bộ từ Facebook. Thử lại đầu danh sách vài lần trước khi quét sâu.
        first_page: list[dict] | None = None
        for attempt in range(4):
            try:
                first_page = await ctx.pancake.list_conversations_internal(0)
            except Exception as e:  # noqa: BLE001
                log.warning("[RAG][%s] Lỗi tra customer_id (conv=%s, lần %d): %s", ctx.name, conv_id, attempt, e)
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
                log.warning("[RAG][%s] Lỗi tra customer_id (conv=%s, đã quét %d): %s", ctx.name, conv_id, cc, e)
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

    # ---------- Nhận webhook ----------

    async def handle_event(self, page_id: str, event: dict) -> None:
        ctx = self._pages.get(page_id)
        if not ctx:
            log.warning("[RAG] Webhook: sự kiện của trang %s (chưa cấu hình) -> bỏ qua.", page_id)
            return

        message = event.get("message") or {}
        is_echo = bool(message.get("is_echo"))
        if is_echo:
            await self._handle_echo(ctx, event, message)
            return

        psid = (event.get("sender") or {}).get("id", "")
        text = (message.get("text") or "").strip()
        if not psid or not text:
            log.info("[RAG][%s] Webhook: bỏ qua event không phải tin nhắn văn bản (psid=%r, keys=%s).",
                      ctx.name, psid, list(event.keys()))
            return
        log.info("[RAG][%s] Nhận tin khách (psid=%s): %r", ctx.name, psid, text[:80])
        await self.on_customer_message(ctx, psid, text)

    async def _handle_echo(self, ctx: RagPageContext, event: dict, message: dict) -> None:
        sent_app_id = str(message.get("app_id", ""))
        if sent_app_id == meta_settings.meta_app_id:
            return
        if sent_app_id in ctx.ignore_app_ids:
            log.info("[RAG][%s] Echo từ app_id=%s (nguồn tự động đã biết) -> bỏ qua.", ctx.name, sent_app_id)
            return
        psid = (event.get("recipient") or {}).get("id", "")
        if not psid:
            return
        conv_id = self.conv_id_for(ctx, psid)
        conv = ctx.conversations.setdefault(conv_id, RagConversation())
        if not conv.human_takeover:
            conv.human_takeover = True
            log.warning("[RAG][%s] Hội thoại %s: phát hiện app_id=%s khác bot -> DỪNG hẳn xử lý tự động.",
                        ctx.name, conv_id, sent_app_id)

    # ---------- Xử lý tin khách (debounce + generation, giống hệt bản gốc) ----------

    async def on_customer_message(self, ctx: RagPageContext, psid: str, text: str) -> None:
        if not in_active_hours(ctx.active_hours):
            log.info("[RAG][%s] Ngoài khung giờ hoạt động (%sh-%sh) -> bỏ qua tin khách, không xử lý gì.",
                      ctx.name, ctx.active_hours.get("start"), ctx.active_hours.get("end"))
            return
        conv_id = self.conv_id_for(ctx, psid)
        conv = ctx.conversations.setdefault(conv_id, RagConversation())
        if conv.human_takeover:
            log.info("[RAG][%s] Hội thoại %s: đã bị can thiệp trước đó -> bỏ qua.", ctx.name, conv_id)
            return
        conv.last_customer_msg_at = time.time()
        conv.pending_messages.append(text)
        conv.generation += 1
        if conv.task and not conv.task.done():
            conv.task.cancel()
        conv.task = asyncio.create_task(self._debounce_and_reply(ctx, conv_id, psid))

    async def _debounce_and_reply(self, ctx: RagPageContext, conv_id: str, psid: str) -> None:
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
                log.warning("[RAG][%s] Hội thoại %s: không tra được customer_id Pancake -> bỏ qua.", ctx.name, conv_id)
                return
            data = await ctx.pancake.get_messages(conv_id, customer_id)
            raw_messages = data.get("messages", [])
            history = self._to_plain_history(ctx, raw_messages, exclude=pending)
            sent_image_urls = self._collect_sent_image_urls(ctx, raw_messages)

            # Xem giải thích đầy đủ trong meta_orchestrator.py: nhân viên trả lời tay KHÔNG khoá vĩnh viễn —
            # nếu khách nhắn mới mà nhân viên không vào trong STAFF_TAKEOVER_WAIT_SECONDS thì bot tiếp quản.
            staff_at = last_staff_reply_epoch(raw_messages, ctx.page_id)
            if staff_at is not None:
                if not conv.human_takeover:
                    conv.human_takeover = True
                    self._save_human_takeover_state(ctx)
                customer_at = conv.last_customer_msg_at or time.time()
                if staff_at >= customer_at:
                    log.info("[RAG][%s] Hội thoại %s: nhân viên vừa trả lời sau tin khách -> bot im lặng.",
                             ctx.name, conv_id)
                    return
                remaining = customer_at + STAFF_TAKEOVER_WAIT_SECONDS - time.time()
                if remaining > 0:
                    log.info("[RAG][%s] Hội thoại %s: đợi %.0f phút xem nhân viên có trả lời không.",
                             ctx.name, conv_id, remaining / 60)
                    await asyncio.sleep(remaining)
                    if conv.generation != my_generation:
                        return
                    data = await ctx.pancake.get_messages(conv_id, customer_id)
                    raw_messages = data.get("messages", [])
                    history = self._to_plain_history(ctx, raw_messages, exclude=pending)
                    sent_image_urls = self._collect_sent_image_urls(ctx, raw_messages)
                    staff_at = last_staff_reply_epoch(raw_messages, ctx.page_id)
                    if staff_at is not None and staff_at >= customer_at:
                        log.info("[RAG][%s] Hội thoại %s: nhân viên đã trả lời trong lúc đợi -> bot im lặng.",
                                 ctx.name, conv_id)
                        return
                log.warning("[RAG][%s] Hội thoại %s: quá %d phút không ai trả lời -> BOT TIẾP QUẢN.",
                            ctx.name, conv_id, STAFF_TAKEOVER_WAIT_SECONDS // 60)

            if meta_settings.stop_when_has_phone and len(customer_phones(history)) >= meta_settings.required_phones:
                log.info("[RAG][%s] Hội thoại %s: đã đủ SĐT -> KHÔNG trả lời thêm.", ctx.name, conv_id)
                return

            # ĐỊNH TUYẾN: ưu tiên known_grade nhớ từ lượt trước; nếu chưa có, dò nhanh trong tin nhắn khách.
            grade_hint = conv.known_grade_hint or detect_grade_hint(merged)
            # Xem giải thích đầy đủ trong meta_orchestrator.py: generate() đôi lúc TREO VĨNH VIỄN dù SDK đã
            # có timeout riêng — đặt thêm trần cứng bên ngoài để không bao giờ treo quá 90s.
            result = await asyncio.wait_for(ai_rag.generate(history, merged, grade_hint=grade_hint), timeout=90)
            log.info("[RAG][%s] Hội thoại %s: prompt %d ký tự (%d tình huống) — grade_hint=%r.",
                      ctx.name, conv_id, result.get("_debug_prompt_chars", 0),
                      len(result.get("_debug_included_situations", [])), grade_hint)

            if conv.generation != my_generation:
                log.info("[RAG][%s] Hội thoại %s: đã có lượt mới hơn -> hủy gửi lượt này.", ctx.name, conv_id)
                return
        except Exception as e:  # noqa: BLE001
            log.exception("[RAG][%s] Lỗi AI %s: %s", ctx.name, conv_id, e)
            return

        # Cập nhật bộ nhớ định tuyến cho lượt sau (chỉ khi AI đã thật sự biết chắc, không ghi đè bằng rỗng).
        new_grade = (result.get("known_grade") or "").strip()
        if new_grade and new_grade != "chưa biết":
            conv.known_grade_hint = new_grade

        reply = (result.get("reply") or "").strip()
        if reply and has_error_leak(reply):
            log.warning("[RAG][%s] Hội thoại %s: reply có dấu hiệu rò rỉ lỗi kỹ thuật -> KHÔNG gửi.", ctx.name, conv_id)
            reply = ""
        if not reply:
            return

        actions = reorder_ack_before_phone_ask(parse_reply(reply))
        known_grade = result.get("known_grade", "")
        if not known_grade or known_grade == "chưa biết":
            known_grade = conv.known_grade_hint or reconcile_known_grade(known_grade, history, merged)
            if known_grade and known_grade != "chưa biết":
                conv.known_grade_hint = known_grade
        known_format = reconcile_known_format(reply, result.get("known_format", ""), history, merged)
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
                # Xem giải thích đầy đủ trong meta_orchestrator.py: Facebook KHÔNG đảm bảo giao đúng thứ tự
                # cho các tin gửi liên tiếp gần như tức thì, kể cả 2 tin CHỮ với nhau. Đợi thêm 1 chút sau
                # MỌI hành động (không chỉ ảnh) để giảm rủi ro bị đảo thứ tự hiển thị.
                if i < len(actions) - 1:
                    await asyncio.sleep(1.5)
            except Exception as e:  # noqa: BLE001
                log.exception("[RAG][%s] Lỗi gửi %s: %s", ctx.name, conv_id, e)

        if actions:
            await self._apply_tag(ctx, conv_id, BOT_TAG)
        for tag_name in result.get("tags", []):
            await self._apply_tag(ctx, conv_id, tag_name)

    async def _apply_tag(self, ctx: RagPageContext, conv_id: str, tag_name: str) -> None:
        tag_id = ctx.tag_map.get(tag_name.strip())
        if tag_id is None:
            log.warning("[RAG][%s] Chưa map được tag '%s'.", ctx.name, tag_name)
            return
        try:
            await ctx.pancake.toggle_tag(conv_id, tag_id, add=True)
        except Exception as e:  # noqa: BLE001
            log.warning("[RAG][%s] Lỗi gắn tag %s: %s", ctx.name, tag_name, e)

    @staticmethod
    def _collect_sent_image_urls(ctx: RagPageContext, messages: list[dict]) -> set[str]:
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
    def _to_plain_history(ctx: RagPageContext, messages: list[dict], exclude: list[str]) -> list[dict]:
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


rag_orchestrator = RagOrchestrator()
