"""중복 제거, 동일 사건 묶음, 점수 계산, 추천 10건 선정."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .models import Article, SRC_AGENCY, SRC_OFFICIAL
from .verify import reg_domain, title_similarity


def _dt(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def dedupe(arts: list[Article]) -> list[Article]:
    """같은 URL(또는 같은 제목+언론사) 중복 제거. 원문 URL을 가진 쪽을 남긴다."""
    best: dict[str, Article] = {}
    for a in arts:
        keys = [a.url.split("#")[0].rstrip("/")]
        if a.relay_url:
            keys.append(a.relay_url)
        keys.append((a.publisher or "?") + "|" + "".join(a.title.split()))
        hit = next((k for k in keys if k in best), None)
        if hit:
            cur = best[hit]
            if a.url_type == "원문" and cur.url_type != "원문":
                for k in keys:
                    best[k] = a
            continue
        for k in keys:
            best[k] = a
    uniq, seen = [], set()
    for a in best.values():
        if id(a) not in seen:
            seen.add(id(a))
            uniq.append(a)
    return uniq


def relevance(a: Article, cfg: dict) -> float:
    text = f"{a.title} {a.excerpt}"
    score = 0.0
    for kw in cfg["keywords"]:
        if all(w in text for w in kw["q"].split()):
            score += kw.get("weight", 1)
    score += sum(0.5 for t in cfg.get("boost_terms", []) if t in text)
    if score == 0 and a.source_type != SRC_OFFICIAL:
        return 0.0  # 검색엔진이 느슨하게 끌어온 무관 기사(실제 테스트: 항공사 팝업 기사) 제거
    if a.source_type in (SRC_OFFICIAL, SRC_AGENCY):
        score += 2
    return score


def filter_candidates(arts: list[Article], cfg: dict, now: datetime) -> list[Article]:
    lb = timedelta(hours=cfg["schedule"]["lookback_hours"])
    out = []
    for a in arts:
        if any(t in a.title for t in cfg.get("exclude_terms", [])):
            continue
        pub = _dt(a.published) if a.published else None
        if pub and now - pub > lb and a.source_type != SRC_OFFICIAL:
            continue
        a.score = relevance(a, cfg)
        if a.score <= 0:
            continue
        if a.publisher in cfg.get("exclude_publishers", []):
            a.exclude_reasons.append("홍보성 시장보고서 출처(설정에서 제외)")
        out.append(a)
    return out


GENERIC = {"계측기", "압력계", "유량계", "레벨계", "온도센서", "전송기", "스마트팩토리", "자동화",
           "글로벌", "솔루션", "코리아", "대한민국", "기업", "시장", "전망", "산업"}


def _bigrams(s: str) -> set:
    s = re.sub(r"[^0-9a-zA-Z가-힣]", "", s).lower()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _tokens(s: str) -> set:
    return {t.lower() for t in re.findall(r"[0-9A-Za-z가-힣]+", s) if len(t) >= 2}


def same_event(a: Article, b: Article) -> bool:
    """같은 사건 판정 — 2026-10-13 실제 제목으로 기준을 맞춤.
    ① 제목 글자쌍 유사도(Jaccard) 0.55 이상, 또는
    ② 숫자 없는 5자 이상 고유어(회사명 등)를 공유하고 유사도 0.12 이상
       (예: '코리아인스트루먼트 … 국제표준 동시 인증' ↔ '코리아인스트루먼트, ISO 27001 동시 획득')
    """
    da, db = _dt(a.published or ""), _dt(b.published or "")
    if da and db and abs((da - db).total_seconds()) > 3 * 86400:
        return False
    A, B = _bigrams(a.title), _bigrams(b.title)
    if not A or not B:
        return False
    jac = len(A & B) / len(A | B)
    if jac >= 0.55 or title_similarity(a.title, b.title) == 1.0:
        return True
    shared = _tokens(a.title) & _tokens(b.title)
    entity = any(len(t) >= 5 and not re.search(r"\d", t) and t not in GENERIC for t in shared)
    return entity and jac >= 0.12


def cluster(arts: list[Article]) -> list[list[Article]]:
    """같은 사건을 다룬 기사끼리 묶는다(묶음 안 어느 기사와든 같은 사건이면 합류)."""
    groups: list[list[Article]] = []
    for a in sorted(arts, key=lambda x: x.published or "", reverse=True):
        placed = False
        for g in groups:
            if any(same_event(a, x) for x in g):
                g.append(a)
                placed = True
                break
        if not placed:
            groups.append([a])
    for i, g in enumerate(groups):
        for a in g:
            a.cluster_id = f"c{i:03d}"
    return groups


def _rep_key(a: Article, cfg: dict):
    pref = cfg.get("preferred_publishers", [])
    return (
        a.verified,
        a.source_type in (SRC_OFFICIAL, SRC_AGENCY),
        a.publisher in pref,
        a.title_match is True,
        a.published or "",
    )


def choose(groups: list[list[Article]], cfg: dict, already_sent: set[str], top_n: int, now: datetime):
    """각 묶음의 대표 기사(검증된 것) + 추가 검증 출처 → 점수 순 상위 top_n."""
    picks = []
    for g in groups:
        g.sort(key=lambda a: _rep_key(a, cfg), reverse=True)
        rep = g[0]
        if not rep.verified:
            continue  # 묶음 안에 검증된 기사가 하나도 없음 → 추천 제외
        if rep.url in already_sent or any(x.url in already_sent for x in g):
            continue
        pub = _dt(rep.published) if rep.published else None
        hours = (now - pub).total_seconds() / 3600 if pub else 24
        recency = max(0.0, 2.0 - hours / 24)          # 최신일수록 가점(최대 2)
        coverage = min(3.0, 0.8 * (len({x.publisher for x in g if x.verified}) - 1))
        total = max(x.score for x in g) + recency + coverage
        others = [x for x in g[1:] if x.verified]
        picks.append({"rep": rep, "others": others, "score": round(total, 2),
                      "unverified": [x for x in g[1:] if not x.verified]})
    picks.sort(key=lambda p: p["score"], reverse=True)
    return picks[:top_n]
