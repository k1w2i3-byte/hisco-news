"""기사 수집: 네이버 뉴스 API, Google News RSS, 언론사 RSS, 기업 공식 보도자료."""
from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable
from urllib.parse import quote, urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from .models import Article, SRC_OFFICIAL, SRC_PRESS, URL_ORIGINAL, URL_RELAY
from .net import decode_html
from .resolve import is_google

log = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(dt_str: str | None) -> str | None:
    if not dt_str:
        return None
    try:
        dt = parsedate_to_datetime(dt_str)
    except Exception:
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _split_gn_title(title: str, publisher: str) -> str:
    """Google News 제목 끝의 ' - 언론사' 제거."""
    if publisher and title.endswith(" - " + publisher):
        return title[: -len(publisher) - 3].strip()
    return title


# ---------------------------------------------------------------- 네이버
def from_naver(sess: requests.Session, keywords: list[dict]) -> list[Article]:
    cid, sec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and sec):
        log.info("네이버 API 키 없음 — 건너뜀")
        return []
    out = []
    for kw in keywords:
        try:
            r = sess.get("https://openapi.naver.com/v1/search/news.json",
                         params={"query": kw["q"], "display": 30, "sort": "date"},
                         headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec},
                         timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("네이버 API 실패(%s): %s", kw["q"], e)
            continue
        for it in r.json().get("items", []):
            orig = (it.get("originallink") or "").strip()
            naver = (it.get("link") or "").strip()
            a = Article(
                title=_clean(it.get("title")),
                publisher="",  # API가 언론사명을 주지 않음 → 원문 페이지(og:site_name)에서 확인
                published=_iso(it.get("pubDate")),
                collected=now_iso(),
                url=orig or naver,
                url_type=URL_ORIGINAL if orig else URL_RELAY,
                relay_url=naver if orig and naver != orig else None,
                feed="네이버 뉴스 API", keyword=kw["q"],
            )
            a.excerpt = _clean(it.get("description"))
            a.excerpt_src = "네이버 검색 제공 발췌"
            out.append(a)
    return out


# ---------------------------------------------------------------- Google News
def from_google_news(sess: requests.Session, keywords: list[dict], lookback_h: int) -> list[Article]:
    days = max(1, round(lookback_h / 24))
    out = []
    for kw in keywords:
        url = ("https://news.google.com/rss/search?q=" + quote(f'{kw["q"]} when:{days}d')
               + "&hl=ko&gl=KR&ceid=KR:ko")
        try:
            r = sess.get(url, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("Google News 실패(%s): %s", kw["q"], e)
            continue
        feed = feedparser.parse(r.content)
        for e in feed.entries[:30]:
            src = getattr(e, "source", None) or {}
            pub = _clean(src.get("title", "")) if src else ""
            a = Article(
                title=_split_gn_title(_clean(e.get("title")), pub),
                publisher=pub,
                published=_iso(e.get("published")),
                collected=now_iso(),
                url=e.get("link", ""),
                url_type=URL_RELAY,          # 해석에 성공해야 '원문'으로 바뀜
                relay_url=e.get("link", ""),
                feed="Google News", keyword=kw["q"],
            )
            a.publisher_home = src.get("href", "") if src else ""
            out.append(a)
    return out


# ---------------------------------------------------------------- 언론사·기관 RSS
def from_rss(sess: requests.Session, feeds: list[dict]) -> list[Article]:
    out = []
    for f in feeds:
        try:
            r = sess.get(f["url"], timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("RSS 실패(%s): %s", f["name"], e)
            continue
        feed = feedparser.parse(r.content)
        pub = f.get("publisher") or _clean(feed.feed.get("title", "")) or f["name"]
        for e in feed.entries[:50]:
            link = e.get("link", "")
            if not link or is_google(link):
                continue
            a = Article(
                title=_clean(e.get("title")), publisher=pub,
                published=_iso(e.get("published") or e.get("updated")),
                collected=now_iso(), url=link, url_type=URL_ORIGINAL,
                source_type=f.get("type", SRC_PRESS), feed=f["name"],
            )
            desc = _clean(e.get("summary", ""))
            if desc:
                a.excerpt, a.excerpt_src = desc[:300], "RSS 제공 발췌"
            out.append(a)
    return out


# ---------------------------------------------------------------- 기업 공식 발표
def from_official(sess: requests.Session, entries: list[dict]) -> list[Article]:
    out = []
    for o in entries:
        try:
            if o.get("type") == "rss":
                for a in from_rss(sess, [{**o, "type": SRC_OFFICIAL, "publisher": o["name"]}]):
                    a.feed = f'{o["name"]} 공식'
                    out.append(a)
                continue
            r = sess.get(o["url"], timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("공식 보도자료 실패(%s): %s", o["name"], e)
            continue
        soup = BeautifulSoup(decode_html(r), "lxml")
        seen, n = set(), 0
        base_host = urlparse(o["url"]).hostname
        for link in soup.select(o.get("link_selector", "a")):
            href = urljoin(o["url"], link.get("href", ""))
            text = _clean(link.get_text(" "))
            if (not href.startswith("http") or href in seen or len(text) < 12
                    or urlparse(href).hostname != base_host
                    or href.rstrip("/") == o["url"].rstrip("/")):
                continue
            seen.add(href)
            out.append(Article(
                title=text, publisher=o["name"], published=None, collected=now_iso(),
                url=href, url_type=URL_ORIGINAL, source_type=SRC_OFFICIAL,
                feed=f'{o["name"]} 공식'))
            n += 1
            if n >= 10:
                break
        log.info("공식 보도자료 %s: %d건", o["name"], n)
    return out


def collect_all(sess, cfg) -> list[Article]:
    s = cfg["sources"]
    lb = cfg["schedule"]["lookback_hours"]
    arts: list[Article] = []
    if s.get("naver_api"):
        arts += from_naver(sess, cfg["keywords"])
    if s.get("google_news"):
        arts += from_google_news(sess, cfg["keywords"], lb)
    arts += from_rss(sess, s.get("rss", []))
    arts += from_official(sess, cfg.get("official", []))
    return arts
