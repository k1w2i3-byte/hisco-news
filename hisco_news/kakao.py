"""카카오톡 '나에게 보내기' 발송.

카카오 메시지 링크는 카카오 개발자 앱에 등록한 도메인만 열 수 있습니다.
그래서 각 기사 항목은 우리 뉴스판의 go.html?id=... 로 연결하고,
go.html 이 원문 상태를 확인한 뒤 즉시 원본 언론사 URL로 이동시킵니다.
(Google News 같은 외부 중계 페이지를 거치지 않습니다.)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"


class KakaoError(RuntimeError):
    pass


def _client_params() -> dict:
    p = {"client_id": os.environ["KAKAO_REST_KEY"]}
    if os.getenv("KAKAO_CLIENT_SECRET"):
        p["client_secret"] = os.environ["KAKAO_CLIENT_SECRET"]
    return p


def _mask(v: str) -> None:
    if os.getenv("GITHUB_ACTIONS") and v:
        print(f"::add-mask::{v}")


def save_refresh_token(token: str) -> bool:
    """새 refresh token 을 GitHub Secret 에 저장(GH_PAT 필요)."""
    _mask(token)
    if not (os.getenv("GH_PAT") and os.getenv("GITHUB_REPOSITORY")):
        log.warning("GH_PAT 없음 — 새 refresh token 을 저장하지 못함")
        return False
    r = subprocess.run(["gh", "secret", "set", "KAKAO_REFRESH_TOKEN", "--body", token,
                        "--repo", os.environ["GITHUB_REPOSITORY"]],
                       env={**os.environ, "GH_TOKEN": os.environ["GH_PAT"]},
                       capture_output=True, text=True)
    if r.returncode != 0:
        log.error("Secret 저장 실패: %s", r.stderr.strip())
        return False
    return True


def exchange_code(code: str, redirect_uri: str, state: dict) -> str:
    """최초 1회: 인가 코드 → 토큰."""
    r = requests.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                                       "redirect_uri": redirect_uri, **_client_params()}, timeout=15)
    if r.status_code != 200:
        raise KakaoError(f"토큰 발급 실패: {r.status_code} {r.text}")
    t = r.json()
    _mask(t["access_token"])
    if not save_refresh_token(t["refresh_token"]):
        raise KakaoError("refresh token 저장 실패 — GH_PAT 권한(Secrets: Read and write)을 확인하세요")
    _record_expiry(state, t.get("refresh_token_expires_in"))
    return t["access_token"]


def _record_expiry(state: dict, seconds) -> None:
    if seconds:
        exp = datetime.now(timezone.utc) + timedelta(seconds=int(seconds))
        state["kakao_refresh_expires"] = exp.isoformat(timespec="seconds")


def access_token(state: dict) -> str:
    rt = os.getenv("KAKAO_REFRESH_TOKEN")
    if not rt:
        raise KakaoError("KAKAO_REFRESH_TOKEN 없음 — '카카오 최초 인증' 워크플로를 먼저 실행하세요")
    r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": rt,
                                       **_client_params()}, timeout=15)
    if r.status_code != 200:
        raise KakaoError(f"토큰 갱신 실패: {r.status_code} {r.text} — 최초 인증을 다시 하세요")
    t = r.json()
    _mask(t["access_token"])
    if t.get("refresh_token"):  # 만료 1개월 이내일 때만 새로 발급됨
        if save_refresh_token(t["refresh_token"]):
            _record_expiry(state, t.get("refresh_token_expires_in"))
    return t["access_token"]


def expiry_warning(state: dict) -> str | None:
    exp = state.get("kakao_refresh_expires")
    if not exp:
        return None
    left = datetime.fromisoformat(exp) - datetime.now(timezone.utc)
    if left < timedelta(days=10):
        return f"카카오 인증이 {max(0, left.days)}일 뒤 만료됩니다. '카카오 최초 인증'을 다시 실행해 주세요."
    return None


def send(token: str, template: dict) -> None:
    r = requests.post(MEMO_URL, headers={"Authorization": f"Bearer {token}"},
                      data={"template_object": json.dumps(template, ensure_ascii=False)}, timeout=15)
    if r.status_code != 200 or r.json().get("result_code") != 0:
        raise KakaoError(f"발송 실패: {r.status_code} {r.text}")


# ------------------------------------------------------------ 메시지 구성
def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _kst(iso: str | None) -> str:
    if not iso:
        return "발행일 미상"
    dt = datetime.fromisoformat(iso).astimezone(timezone(timedelta(hours=9)))
    return dt.strftime("%m/%d %H:%M")


def build_messages(picks: list[dict], base_url: str, date_label: str) -> list[dict]:
    """추천 기사 → 리스트 템플릿 여러 개 (템플릿당 2~3건)."""
    n = len(picks)
    if n == 0:
        return [{
            "object_type": "text",
            "text": f"[협성HISCO 계측뉴스 {date_label}]\n오늘은 원문 링크까지 확인된 추천 기사가 없습니다. "
                    "수집 결과와 제외 사유는 뉴스판에서 확인하세요.",
            "link": {"web_url": base_url + "/", "mobile_web_url": base_url + "/"},
            "button_title": "뉴스판 열기",
        }]
    sizes, left = [], n
    while left > 0:  # 리스트 템플릿은 2~3건 → 1건이 남지 않게 분배 (10건 = 3+3+2+2)
        take = 1 if left == 1 else 2 if left in (2, 4) else 3
        sizes.append(take)
        left -= take
    msgs, idx = [], 0
    pages = len(sizes)
    for p, size in enumerate(sizes, 1):
        contents = []
        for k in range(size):
            pick = picks[idx]
            a, others = pick["rep"], pick["others"]
            idx += 1
            extra = f" · 교차확인 {len(others)}곳" if others else ""
            tag = "[공식발표] " if a.source_type != "언론 보도" else ""
            link = f"{base_url}/go.html?id={a.id}"
            contents.append({
                "title": _short(f"{idx}. {tag}{a.title}", 70),
                "description": _short(f"{a.publisher} · {_kst(a.published)}{extra} · 원문확인", 60),
                "link": {"web_url": link, "mobile_web_url": link},
            })
        if len(contents) == 1:  # 안전장치: 리스트 최소 2건
            msgs.append({"object_type": "text",
                         "text": _short(f"{contents[0]['title']}\n{contents[0]['description']}", 200),
                         "link": contents[0]["link"], "button_title": "원문 보기"})
            continue
        msgs.append({
            "object_type": "list",
            "header_title": f"계측 산업뉴스 {date_label} ({p}/{pages})",
            "header_link": {"web_url": base_url + "/", "mobile_web_url": base_url + "/"},
            "contents": contents,
            "buttons": [{"title": "뉴스판 전체 보기",
                         "link": {"web_url": base_url + "/", "mobile_web_url": base_url + "/"}}],
        })
    return msgs
