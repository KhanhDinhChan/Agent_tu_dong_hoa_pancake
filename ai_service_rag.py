"""
AIService PHIÊN BẢN RAG (định tuyến kịch bản theo lớp) — TÁCH BIỆT HOÀN TOÀN khỏi ai_service.py đang
chạy: KHÔNG sửa ai_service.py, KHÔNG dùng SYSTEM_PROMPT tĩnh của nó. Chỉ ĐỌC LẠI (import) các hằng
số/hàm THUẦN không phụ thuộc SYSTEM_PROMPT cố định (RESPONSE_SCHEMA, ALLOWED_TAGS, has_error_leak...)
để không phải chép lại logic đã đúng, mà vẫn không đụng gì tới đường gọi Gemini của bản đang chạy.

Khác biệt DUY NHẤT so với ai_service.py: "system_instruction" được DỰNG LẠI THEO TỪNG LẦN GỌI dựa vào
known_grade/gợi ý lớp (qua rag_router.build_playbook) thay vì 1 SYSTEM_PROMPT cố định nạp 1 lần lúc
khởi động tiến trình.
"""
from __future__ import annotations

import json
import logging
import os

from google import genai
from google.genai import types

from ai_service import (
    ALLOWED_TAGS,
    KNOWN_FORMAT_VALUES,
    KNOWN_GRADE_VALUES,
    RESPONSE_SCHEMA,
    _GRADE_TAG_HINT,
    has_error_leak,
    is_ai_message_safe,
)
from config import settings
from rag_router import build_playbook

log = logging.getLogger("ai_service_rag")

_FALLBACK_TEMPLATE = '''Bạn là nhân viên tư vấn & chốt sale của Trung tâm "{business_name}".
Trả lời khách qua chat (Pancake) bằng tiếng Việt, bám SÁT kịch bản dưới đây.

===== KỊCH BẢN =====
{sales_playbook}
===== HẾT KỊCH BẢN =====

Gắn tag phù hợp trong: {allowed_tags}. {grade_tag_hint}'''


def _read_template() -> str:
    if os.path.exists(settings.system_prompt_path):
        with open(settings.system_prompt_path, encoding="utf-8") as f:
            return f.read().strip()
    log.warning("Thiếu '%s' — dùng khung tối thiểu.", settings.system_prompt_path)
    return _FALLBACK_TEMPLATE


_SYSTEM_PROMPT_TEMPLATE = _read_template()


def build_system_prompt(known_grade: str | None) -> tuple[str, list[str]]:
    """Dựng system prompt đầy đủ cho 1 lượt gọi, đã ĐỊNH TUYẾN kịch bản theo known_grade.
    Trả về (system_prompt, danh sách tình huống đã đưa vào — phục vụ log/debug/so sánh)."""
    playbook, included = build_playbook(known_grade)
    prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        business_name=settings.business_name,
        known_grade_values=KNOWN_GRADE_VALUES,
        known_format_values=KNOWN_FORMAT_VALUES,
        sales_playbook=playbook,
        allowed_tags=ALLOWED_TAGS,
        grade_tag_hint=_GRADE_TAG_HINT,
        system_prompt_path=settings.system_prompt_path,
    )
    return prompt, included


class AIServiceRag:
    def __init__(self) -> None:
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(
                timeout=45_000,
                retry_options=types.HttpRetryOptions(
                    attempts=4,
                    initial_delay=1.0,
                    max_delay=15.0,
                    exp_base=2.0,
                    http_status_codes=[408, 429, 500, 502, 503, 504],
                ),
            ),
        )

    async def generate(
        self, conversation_history: list[dict], customer_message: str, grade_hint: str | None = None
    ) -> dict:
        system_prompt, included = build_system_prompt(grade_hint)
        contents = self._to_gemini_contents(conversation_history, customer_message)
        resp = await self._client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=settings.gemini_temperature,
                top_p=settings.gemini_top_p,
            ),
        )
        result = json.loads(resp.text)
        result["_debug_prompt_chars"] = len(system_prompt)
        result["_debug_included_situations"] = included
        return result

    @staticmethod
    def _to_gemini_contents(history: list[dict], customer_message: str) -> list[types.Content]:
        contents = [
            types.Content(role=("model" if m["role"] == "assistant" else "user"), parts=[types.Part(text=m["content"])])
            for m in history
        ]
        contents.append(types.Content(role="user", parts=[types.Part(text=customer_message)]))
        return contents


ai_rag = AIServiceRag()
