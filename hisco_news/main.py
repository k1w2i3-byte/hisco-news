"""실행 진입점.

python -m hisco_news run            # 수집·검증·뉴스판 생성·카카오 발송 (매일 08:00)
python -m hisco_news run --dry-run  # 카카오 발송 없이 전체 점검 (실제 원문 링크 테스트 보고서 생성)
python -m hisco_news auth-url       # 카카오 인증 주소 출력
python -m hisco_news kakao-auth --code XXXX   # 최초 1회 토큰 발급 + 테스트 메시지
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import yaml

from . import kakao
from .collect import collect_all
from .models import Article, LINK_OK, URL_ORIGINAL
from .net import session
from .render import KST, render
from .select import choose, cluster, dedupe, filter_candidates
from .summarize import summarize
from .verify import recheck_link, verify_article

ROOT = Path(__file__).resolve().parent.parent
DOCS, DATA = ROOT / "docs", ROOT / "data"
log = logging.getLogger("hisco_news")
VERIFY_LIMIT = 150   # 하루 검증 대상 상한(점수 상위)
KEEP_DAYS = 30


def load_cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def base_url(cfg) -> str:
    if cfg["site"].get("base_url"):
        return cfg["site"]["base_url"].rstrip("/")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        host = f"https://{owner.lower()}.github.io"
        return host if name.lower() == f"{owner.lower()}.github.io" else f"{host}/{name}"
    return "http://localhost:8000"


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def wait_until(send_at: str) -> None:
    hh, mm = map(int, send_at.split(":"))
    now = datetime.now(KST)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    sec = (target - now).total_seconds()
    if 0 < sec <= 40 * 60:
        log.info("발송 시각까지 %.0f초 대기", sec)
        time.sleep(sec)


def link_report(arts: list[Article], picks: list[dict], date_label: str) -> str:
    """실제 원문 링크 동작 점검 보고서(마크다운)."""
    c = Counter(a.link_status if a.url_type == URL_ORIGINAL else f"({a.url_type})" for a in arts)
    feeds = Counter(a.feed for a in arts)
    gn = [a for a in arts if a.feed == "Google News"]
    gn_ok = sum(1 for a in gn if a.url_type == URL_ORIGINAL)
    lines = [f"# 원문 링크 점검 보고서 {date_label}", "",
             f"- 검증 대상 {len(arts)}건",
             f"- Google News 원본 URL 해석 성공 {gn_ok}/{len(gn)}건" if gn else "- Google News 수집 0건",
             "", "## 링크 상태", ""]
    lines += [f"- {k}: {v}건" for k, v in c.most_common()]
    lines += ["", "## 수집 경로별 건수", ""] + [f"- {k}: {v}건" for k, v in feeds.most_common()]
    lines += ["", "## 추천 기사 원문 점검 결과", "", "| # | 언론사 | HTTP | 상태 | 제목일치 | 원문 URL |",
              "|---|---|---|---|---|---|"]
    for i, p in enumerate(picks, 1):
        a = p["rep"]
        lines.append(f"| {i} | {a.publisher} | {a.http_status} | {a.link_status} | "
                     f"{'예' if a.title_match else '확인불가' if a.title_match is None else '아니오'} | {a.url} |")
    bad = [a for a in arts if a.url_type == URL_ORIGINAL and a.link_status != LINK_OK]
    if bad:
        lines += ["", "## 열리지 않은 원문 링크", ""] + \
                 [f"- [{a.link_status} {a.http_status}] {a.title[:40]} — {a.url}" for a in bad[:30]]
    return "\n".join(lines) + "\n"


def run(dry_run: bool, no_wait: bool) -> int:
    cfg = load_cfg()
    now = datetime.now(timezone.utc)
    date_label = datetime.now(KST).strftime("%Y-%m-%d")
    sess = session()
    state = _read_json(DATA / "state.json", {})
    history: list[dict] = _read_json(DATA / "history.json", [])

    # 1) 지난 기사 원문 재점검 (삭제·이동 감지)
    cutoff = (now - timedelta(days=cfg["schedule"]["recheck_days"])).isoformat()
    for d in history:
        if d.get("collected", "") >= cutoff and d.get("picked"):
            recheck_link(sess, d)
    history = [d for d in history if d.get("collected", "") >= (now - timedelta(days=KEEP_DAYS)).isoformat()]
    dead_recent = [d for d in history if d.get("picked") and d.get("link_status") in ("삭제됨", "이동됨")]
    already_sent = {d["url"] for d in history if d.get("picked")}

    # 2) 수집 → 1차 선별 → 원문 확보·링크 점검
    raw = collect_all(sess, cfg)
    log.info("수집 %d건", len(raw))
    cands = filter_candidates(dedupe(raw), cfg, now)
    cands.sort(key=lambda a: a.score, reverse=True)
    cands = cands[:VERIFY_LIMIT]
    for i, a in enumerate(cands, 1):
        verify_article(sess, a, cfg)
        if i % 20 == 0:
            log.info("검증 %d/%d", i, len(cands))
    cands = dedupe(cands)

    # 3) 동일 사건 묶음 → 추천 선정
    groups = cluster(cands)
    picks = choose(groups, cfg, already_sent, cfg["schedule"]["top_n"], now)
    summarize([p["rep"] for p in picks], cfg)

    picked_ids = {p["rep"].id for p in picks} | {o.id for p in picks for o in p["others"]}
    rest_verified = [a for a in cands if a.verified and a.id not in picked_ids]
    unverified = [a for a in cands if not a.verified]
    stats = {"collected": len(raw), "original": sum(a.url_type == URL_ORIGINAL for a in cands),
             "verified": sum(a.verified for a in cands), "picked": len(picks),
             "unverified": len(unverified)}

    # 4) 뉴스판·데이터 저장
    archive = sorted((p.stem for p in (DOCS / "archive").glob("*.html")), reverse=True) \
        if (DOCS / "archive").exists() else []
    html = render(cfg, date_label, picks, rest_verified, unverified, dead_recent, stats,
                  [d for d in archive if d != date_label])
    (DOCS / "archive").mkdir(parents=True, exist_ok=True)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / "archive" / f"{date_label}.html").write_text(
        html.replace('href="archive/', 'href="../archive/').replace('href="./', 'href="../'), encoding="utf-8")
    _write_json(DOCS / "data" / f"{date_label}.json",
                {"date": date_label, "stats": stats,
                 "picks": [{"rep": p["rep"].to_dict(), "others": [o.to_dict() for o in p["others"]],
                            "score": p["score"]} for p in picks],
                 "unverified": [a.to_dict() for a in unverified]})
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / f"{date_label}_원문링크점검.md").write_text(
        link_report(cands, picks, date_label), encoding="utf-8")

    for p in picks:
        for a, is_rep in [(p["rep"], True)] + [(o, False) for o in p["others"]]:
            d = a.to_dict()
            d["picked"] = is_rep
            d["sent_on"] = date_label
            history.append(d)
    _write_json(DATA / "history.json", history)
    links = {d["id"]: {k: d.get(k) for k in ("url", "url_type", "title", "publisher",
                                              "link_status", "checked_at")} for d in history}
    _write_json(DOCS / "data" / "links.json", links)

    # 5) 카카오톡 발송
    if dry_run:
        log.info("점검 실행(dry-run): 카카오 발송 생략")
        _write_json(DATA / "state.json", state)
        return 0
    if not no_wait:
        wait_until(cfg["schedule"]["send_at"])
    try:
        token = kakao.access_token(state)
        for m in kakao.build_messages(picks, base_url(cfg), datetime.now(KST).strftime("%m/%d(%a)")
                                      .replace("Mon", "월").replace("Tue", "화").replace("Wed", "수")
                                      .replace("Thu", "목").replace("Fri", "금").replace("Sat", "토")
                                      .replace("Sun", "일")):
            kakao.send(token, m)
            time.sleep(0.5)
        warn = kakao.expiry_warning(state)
        if warn:
            kakao.send(token, {"object_type": "text", "text": "[계측뉴스 시스템 알림] " + warn,
                               "link": {"web_url": base_url(cfg) + "/", "mobile_web_url": base_url(cfg) + "/"}})
        state["last_sent"] = datetime.now(KST).isoformat(timespec="seconds")
    finally:
        _write_json(DATA / "state.json", state)
    log.info("카카오톡 발송 완료: %d건", len(picks))
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="hisco_news")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--no-wait", action="store_true")
    sub.add_parser("auth-url")
    k = sub.add_parser("kakao-auth")
    k.add_argument("--code", required=True)
    args = ap.parse_args(argv)
    cfg = load_cfg()
    redirect = base_url(cfg) + "/kakao_callback.html"

    if args.cmd == "run":
        return run(args.dry_run, args.no_wait)
    if args.cmd == "auth-url":
        print("https://kauth.kakao.com/oauth/authorize?response_type=code&scope=talk_message"
              f"&client_id={os.environ.get('KAKAO_REST_KEY', '<REST_API_키>')}&redirect_uri={quote(redirect, safe='')}")
        print("리다이렉트 URI(카카오 앱에 등록):", redirect)
        return 0
    if args.cmd == "kakao-auth":
        state = _read_json(DATA / "state.json", {})
        token = kakao.exchange_code(args.code.strip(), redirect, state)
        _write_json(DATA / "state.json", state)
        kakao.send(token, {"object_type": "text",
                           "text": "[협성HISCO 계측뉴스] 카카오톡 연결이 완료되었습니다. 매일 오전 8시에 뉴스 10건을 보내드립니다.",
                           "link": {"web_url": base_url(cfg) + "/", "mobile_web_url": base_url(cfg) + "/"},
                           "button_title": "뉴스판 열기"})
        print("인증 완료, 테스트 메시지 발송")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
