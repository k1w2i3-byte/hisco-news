"""AI 요약(선택). 원문 페이지에서 읽은 본문만 근거로 2문장 요약을 만든다.

- ANTHROPIC_API_KEY 가 없으면 아무것도 하지 않는다(원문 발췌만 표시).
- AI 요약은 항상 'AI 요약' 라벨로 원문 발췌와 분리해 표시한다.
- 본문이 부족하면 요약하지 않는다(추측 금지).
"""
from __future__ import annotations

import logging
import os

import requests

from .models import Article

log = logging.getLogger(__name__)

PROMPT = """다음은 한 기사 웹페이지에서 추출한 본문입니다. 이 본문에 실제로 적힌 내용만 근거로
계측·자동화 업계 실무자가 볼 핵심을 한국어 2문장 이내로 요약하세요.
- 본문에 없는 수치·회사명·전망을 추가하지 마세요.
- 본문이 기사 내용이 아니거나 요약할 근거가 부족하면 정확히 "요약 불가"라고만 답하세요.

[기사 제목] {title}
[본문]
{body}"""


def summarize(arts: list[Article], cfg: dict) -> None:
    conf = cfg.get("ai_summary", {})
    key = os.getenv("ANTHROPIC_API_KEY")
    if not conf.get("enabled") or not key:
        log.info("AI 요약 비활성(키 없음 또는 설정 off)")
        return
    for a in arts:
        if len(a.body_text) < 200:
            continue
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": conf.get("model", "claude-haiku-5-5"), "max_tokens": 300,
                      "messages": [{"role": "user", "content": PROMPT.format(
                          title=a.title, body=a.body_text[: conf.get("max_chars_input", 4000)])}]},
                timeout=60)
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
            if text and "요약 불가" not in text:
                a.ai_summary = text
        except Exception as e:  # 요약 실패는 발송을 막지 않는다
            log.warning("AI 요약 실패(%s): %s", a.title[:30], e)
