"""기사 데이터 구조와 상태 코드."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional

# 출처 구분
SRC_PRESS = "언론 보도"
SRC_OFFICIAL = "기업 공식 발표"
SRC_AGENCY = "기관 발표"

# URL 구분
URL_ORIGINAL = "원문"   # 언론사·발표기관의 실제 페이지
URL_RELAY = "중계"      # Google News 등 중계 페이지 (원본 URL 확보 실패)
URL_NONE = "없음"

# 링크 상태
LINK_OK = "정상"
LINK_MOVED = "이동됨"        # 다른 주소(대개 홈페이지)로 넘어감 → 삭제 의심
LINK_GONE = "삭제됨"         # 404/410
LINK_BLOCKED = "자동확인불가"  # 403/429 등 봇 차단 — 브라우저에선 열릴 수 있음
LINK_ERROR = "접속오류"       # 타임아웃·DNS·SSL 오류
LINK_UNCHECKED = "미점검"

LINK_HELP = {
    LINK_OK: "원문 페이지 정상 확인",
    LINK_MOVED: "원문 주소가 다른 페이지로 이동됨(삭제 가능성)",
    LINK_GONE: "원문이 삭제되었거나 주소가 잘못됨",
    LINK_BLOCKED: "언론사가 자동 점검을 차단 — 직접 열어 확인 필요",
    LINK_ERROR: "원문 사이트 접속 오류",
    LINK_UNCHECKED: "아직 점검하지 않음",
}


@dataclass
class Article:
    title: str                      # 수집 시점 제목(RSS·API 제공)
    publisher: str                  # 언론사명 / 발표기관명 ("" = 미확인)
    published: Optional[str]        # ISO8601, 발행일 (없으면 None)
    collected: str                  # ISO8601, 수집일시
    url: str                        # 사용자에게 연결할 URL (원문 또는 중계)
    url_type: str = URL_NONE
    relay_url: Optional[str] = None # 중계 링크(있었다면 기록)
    source_type: str = SRC_PRESS
    feed: str = ""                  # 어느 수집 경로에서 왔는지
    keyword: str = ""

    # 검증 결과
    link_status: str = LINK_UNCHECKED
    http_status: Optional[int] = None
    final_url: Optional[str] = None
    checked_at: Optional[str] = None
    page_title: Optional[str] = None   # 원문 페이지에서 확인한 제목
    title_match: Optional[bool] = None
    excerpt: str = ""                  # 원문 페이지의 메타 설명/첫 문단 (원문 발췌)
    excerpt_src: str = ""              # 발췌 출처: "원문 페이지" / "검색 API 제공" 등
    publisher_home: str = ""           # 수집 경로가 알려 준 언론사 홈페이지 (도메인 대조용)
    body_text: str = field(default="", repr=False)  # AI 요약 입력용 (저장하지 않음)
    ai_summary: str = ""               # AI 생성 요약 (라벨 구분)

    # 선정 관련
    score: float = 0.0
    cluster_id: Optional[str] = None
    exclude_reasons: list = field(default_factory=list)

    @property
    def id(self) -> str:
        key = (self.url or "") + "|" + (self.title or "")
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

    @property
    def verified(self) -> bool:
        """추천 가능한 '검증된' 기사인가."""
        return not self.exclusion_reasons()

    def exclusion_reasons(self) -> list[str]:
        r = []
        if self.url_type != URL_ORIGINAL:
            r.append("원문 URL 미확보(중계 링크)" if self.url_type == URL_RELAY else "원문 링크 없음")
        if not self.publisher:
            r.append("출처(언론사·기관) 미확인")
        if self.url_type == URL_ORIGINAL and self.link_status != LINK_OK:
            r.append(f"원문 링크 {self.link_status}")
        if self.link_status == LINK_OK and self.title_match is False:
            r.append("원문 페이지 제목 불일치")
        return r + list(self.exclude_reasons)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("body_text", None)
        d["id"] = self.id
        d["verified"] = self.verified
        d["reasons"] = self.exclusion_reasons()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)
