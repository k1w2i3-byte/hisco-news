"""Google News 중계 링크 → 원본 언론사 URL 해석.

Google News RSS의 기사 링크(news.google.com/rss/articles/CBMi...)는 중계 페이지입니다.
1) 페이지에서 서명(data-n-a-sg)과 시각(data-n-a-ts)을 읽고
2) Google의 batchexecute 엔드포인트에 'garturlreq'를 보내 원본 URL을 받습니다.
(오픈소스 googlenewsdecoder 의 공개된 방식을 참고해 requests로 다시 구현)

원본 URL을 얻지 못하면 None 을 돌려주고, 호출 측에서 '중계 링크'로 표시합니다.
추측으로 URL을 만들어내지 않습니다.
"""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Optional
from urllib.parse import quote, urlparse, parse_qs

import requests

BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
_SG = re.compile(r'data-n-a-sg="([^"]+)"')
_TS = re.compile(r'data-n-a-ts="([^"]+)"')
_CTX = [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None,
         None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0]

GOOGLE_HOSTS = ("news.google.com", "google.com", "www.google.com", "consent.google.com")


def is_google(url: str) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return h == "google.com" or h.endswith(".google.com") or h.endswith("googleusercontent.com")


def article_id(url: str) -> Optional[str]:
    p = urlparse(url)
    if p.hostname != "news.google.com":
        return None
    parts = p.path.split("/")
    if len(parts) > 1 and parts[-2] in ("articles", "read"):
        return parts[-1] or None
    return None


def _legacy_decode(gid: str) -> Optional[str]:
    """2024년 이전 형식: id 안에 URL이 base64로 그대로 들어 있는 경우."""
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
    except Exception:
        return None
    m = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not m:
        return None
    url = m.group(0).decode("ascii", "ignore")
    # 새 형식(AU_yqL 접두)은 URL이 들어 있지 않음
    return url if not is_google(url) else None


def _params(sess: requests.Session, gid: str, src_url: str) -> Optional[tuple[str, str]]:
    qs = parse_qs(urlparse(src_url).query)
    hl = qs.get("hl", ["ko"])[0]
    gl = qs.get("gl", ["KR"])[0]
    ceid = qs.get("ceid", ["KR:ko"])[0]
    page = (f"https://news.google.com/rss/articles/{gid}"
            f"?hl={quote(hl)}&gl={quote(gl)}&ceid={quote(ceid)}")
    r = sess.get(page, timeout=15)
    if r.status_code != 200:
        return None
    sg, ts = _SG.search(r.text), _TS.search(r.text)
    if not (sg and ts):
        return None
    return sg.group(1), ts.group(1)


def build_body(gid: str, ts: str, sig: str) -> str:
    inner = ["garturlreq", _CTX, gid, int(ts) if str(ts).isdigit() else ts, sig]
    env = [["Fbv4je", json.dumps(inner, separators=(",", ":")), None, "1"]]
    return "f.req=" + quote(json.dumps([env], separators=(",", ":")))


def _rows_from(body: str):
    """batchexecute 응답 본문에서 JSON 행 목록을 차례로 꺼낸다."""
    body = body.strip()
    try:
        yield json.loads(body)
        return
    except Exception:
        pass
    # '길이 줄 + JSON' 이 반복되는 형식
    for chunk in re.split(r"(?m)^\d+\s*$", body):
        chunk = chunk.strip()
        if chunk.startswith("["):
            try:
                yield json.loads(chunk)
            except Exception:
                continue


def parse_response(text: str) -> Optional[str]:
    body = text.lstrip()
    if body.startswith(")]}'"):
        body = body[4:]
    for rows in _rows_from(body):
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, list) and len(row) >= 3 and row[0] == "wrb.fr" and row[2]:
                try:
                    payload = json.loads(row[2]) if isinstance(row[2], str) else row[2]
                except Exception:
                    continue
                if isinstance(payload, list) and len(payload) > 1 and payload[0] == "garturlres":
                    url = payload[1]
                    if isinstance(url, str) and _plausible(url):
                        return url
    # 정식 JSON 해석에 실패하면 URL을 추측하지 않는다.
    # (정규식으로 뽑으면 = 같은 이스케이프에서 주소가 잘려 엉뚱한 URL이 나올 수 있음 — 실제 테스트에서 확인)
    return None


def _plausible(url: str) -> bool:
    p = urlparse(url)
    return (p.scheme in ("http", "https") and bool(p.hostname) and not is_google(url)
            and "\\" not in url and not url.endswith(("=", "?", "&")))


def resolve_google_news(sess: requests.Session, url: str, pause: float = 0.6) -> Optional[str]:
    """중계 링크에서 원본 URL을 얻으면 반환, 실패 시 None."""
    gid = article_id(url)
    if not gid:
        return None
    legacy = _legacy_decode(gid)
    if legacy:
        return legacy
    try:
        p = _params(sess, gid, url)
        if not p:
            return None
        sig, ts = p
        r = sess.post(BATCH_URL, data=build_body(gid, ts, sig), timeout=15,
                      headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                               "Referer": "https://news.google.com/"})
        time.sleep(pause)  # 과도한 요청 방지
        if r.status_code != 200:
            return None
        return parse_response(r.text)
    except requests.RequestException:
        return None
