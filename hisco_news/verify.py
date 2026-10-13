"""원문 링크 동작 점검과 원문 페이지 확인."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .models import (Article, LINK_BLOCKED, LINK_ERROR, LINK_GONE, LINK_MOVED, LINK_OK,
                     SRC_AGENCY, SRC_OFFICIAL, URL_ORIGINAL, URL_RELAY)
from .net import decode_html
from .resolve import is_google, resolve_google_news

log = logging.getLogger(__name__)

SOFT_404 = re.compile(r"(페이지를 찾을 수 없|존재하지 않는 (기사|페이지)|삭제된 기사|기사가 삭제|"
                      r"page not found|404 not found|not be found|no longer available)", re.I)

# og:site_name 이 없을 때 쓰는 최소한의 도메인 → 언론사명 표
DOMAIN_PUBLISHER = {
    "yna.co.kr": "연합뉴스", "etnews.com": "전자신문", "mk.co.kr": "매일경제",
    "hankyung.com": "한국경제", "sedaily.com": "서울경제", "mt.co.kr": "머니투데이",
    "edaily.co.kr": "이데일리", "kidd.co.kr": "산업일보", "hellot.net": "헬로티",
    "ekn.kr": "에너지경제", "chosun.com": "조선일보", "joongang.co.kr": "중앙일보",
    "donga.com": "동아일보", "hani.co.kr": "한겨레", "khan.co.kr": "경향신문",
    "newsis.com": "뉴시스", "news1.kr": "뉴스1", "korea.kr": "대한민국 정책브리핑",
}


def reg_domain(url_or_host: str) -> str:
    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").lower().removeprefix("www.").removeprefix("m.")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "or", "go", "ac", "ne", "re", "com"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _norm(t: str) -> str:
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|<[^>]*>", " ", t or "")
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", t).lower()


def title_similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    ga = {na[i:i + 2] for i in range(len(na) - 1)}
    gb = {nb[i:i + 2] for i in range(len(nb) - 1)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


def classify(resp: Optional[requests.Response], orig_url: str, html_text: str = "") -> str:
    if resp is None:
        return LINK_ERROR
    code = resp.status_code
    if code in (404, 410):
        return LINK_GONE
    if code in (401, 403, 429, 451) or code == 999:
        return LINK_BLOCKED
    if code >= 400:
        return LINK_ERROR
    final = resp.url or orig_url
    o, f = urlparse(orig_url), urlparse(final)
    if (reg_domain(o.hostname or "") != reg_domain(f.hostname or "")
            or (len(o.path.strip("/")) > 3 and f.path.strip("/") in ("", "index.html", "main",
                                                                     "index.php", "news"))):
        return LINK_MOVED
    head = html_text[:6000]
    title_m = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    if title_m and SOFT_404.search(title_m.group(1)):
        return LINK_GONE
    if len(html_text) < 3000 and SOFT_404.search(html_text):
        return LINK_GONE
    # 일부 언론사는 없는 기사에 '정상 응답 + 빈 페이지'를 준다 (실제 테스트에서 확인)
    visible = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", html_text)
    has_title = bool(title_m and title_m.group(1).strip()) or "og:title" in head
    if not has_title and len(re.sub(r"\s+", "", visible)) < 150:
        return LINK_GONE
    return LINK_OK


def fetch(sess: requests.Session, url: str):
    try:
        r = sess.get(url, timeout=15, allow_redirects=True)
        text = ""
        ctype = r.headers.get("Content-Type", "")
        if r.status_code < 400 and ("html" in ctype or not ctype):
            text = decode_html(r)[:600_000]
        return r, text
    except requests.RequestException as e:
        log.info("접속 오류 %s: %s", url, e)
        return None, ""


def _meta(soup: BeautifulSoup, *names) -> str:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return re.sub(r"\s+", " ", tag["content"]).strip()
    return ""


def page_info(html_text: str) -> dict:
    soup = BeautifulSoup(html_text, "lxml")
    title = _meta(soup, "og:title", "twitter:title")
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    paras = [p.get_text(" ", strip=True) for p in soup.select("article p, #articleBody p, "
             "#article_body p, .article_body p, .news_cnt_detail_wrap p, #newsct_article, p")]
    body = " ".join(x for x in paras if len(x) > 30)[:8000]
    return {
        "title": title,
        "site_name": _meta(soup, "og:site_name", "application-name"),
        "description": _meta(soup, "og:description", "description", "twitter:description"),
        "published": _meta(soup, "article:published_time", "og:article:published_time",
                           "pubdate", "publish-date", "date"),
        "body": body,
        "canonical": (soup.find("link", rel="canonical") or {}).get("href", ""),
    }


def verify_article(sess: requests.Session, a: Article, cfg: dict) -> Article:
    """원문 URL 확보 → 링크 점검 → 원문 제목 대조 → 출처 확인."""
    # 1) Google News 중계 링크면 원본 URL 해석
    if a.url_type == URL_RELAY and a.relay_url and "news.google.com" in a.relay_url:
        orig = resolve_google_news(sess, a.relay_url)
        if orig:
            a.url, a.url_type = orig, URL_ORIGINAL
        else:
            a.url = a.relay_url  # 그대로 중계 링크로 둔다 (원문인 척하지 않음)

    a.checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if a.url_type != URL_ORIGINAL or is_google(a.url):
        a.url_type = URL_RELAY if a.url else a.url_type
        return a

    # 2) 원문 링크 점검
    resp, text = fetch(sess, a.url)
    a.http_status = resp.status_code if resp is not None else None
    a.final_url = resp.url if resp is not None else None
    a.link_status = classify(resp, a.url, text)

    # 3) 원문 페이지 정보
    if a.link_status == LINK_OK and text:
        info = page_info(text)
        a.page_title = info["title"] or None
        if a.page_title:
            a.title_match = title_similarity(a.title, a.page_title) >= 0.45
        if not a.publisher and info["site_name"]:
            a.publisher = info["site_name"]
        if info["description"]:
            a.excerpt, a.excerpt_src = info["description"][:300], "원문 페이지"
        if not a.published and info["published"]:
            from .collect import _iso
            a.published = _iso(info["published"])
        a.body_text = info["body"]
        # AMP 주소면 같은 언론사의 정식(canonical) 주소로 교체 — 교체 전 열리는지 다시 확인
        can = info["canonical"]
        if (can and can != a.url and "amp" in a.url.lower() and can.startswith("http")
                and reg_domain(can) == reg_domain(a.url)):
            r2, t2 = fetch(sess, can)
            if classify(r2, can, t2) == LINK_OK:
                a.url, a.final_url = can, r2.url

    if not a.publisher:
        a.publisher = DOMAIN_PUBLISHER.get(reg_domain(a.url), "")

    # 4) 출처 구분
    dom = reg_domain(a.url)
    official_domains = {reg_domain(o["url"]) for o in cfg.get("official", [])}
    if dom in official_domains:
        a.source_type = SRC_OFFICIAL
    elif any(dom.endswith(d) for d in cfg.get("agency_domains", [])) and dom.endswith(("go.kr", "korea.kr")):
        a.source_type = SRC_AGENCY
    return a


def recheck_link(sess: requests.Session, d: dict) -> dict:
    """지난 기사 링크 재점검(삭제 감지)."""
    if d.get("url_type") != URL_ORIGINAL:
        return d
    resp, text = fetch(sess, d["url"])
    d["link_status"] = classify(resp, d["url"], text)
    d["http_status"] = resp.status_code if resp is not None else None
    d["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return d
