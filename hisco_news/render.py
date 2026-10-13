"""HTML 뉴스판 생성."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlparse

from .models import (Article, LINK_HELP, LINK_OK, SRC_PRESS, URL_ORIGINAL, URL_RELAY)

KST = timezone(timedelta(hours=9))


def kst(iso: str | None, fmt="%Y-%m-%d %H:%M") -> str:
    if not iso:
        return "미상"
    try:
        return datetime.fromisoformat(iso).astimezone(KST).strftime(fmt)
    except Exception:
        return "미상"


def safe_href(url: str | None) -> str:
    """http(s) 주소만 링크로 허용."""
    if url and urlparse(url).scheme in ("http", "https"):
        return escape(url, quote=True)
    return ""


def _badges(a: Article) -> str:
    b = []
    src_cls = {"기업 공식 발표": "off", "기관 발표": "agy"}.get(a.source_type, "prs")
    b.append(f'<span class="bd {src_cls}">{escape(a.source_type)}</span>')
    if a.url_type == URL_ORIGINAL:
        cls = "ok" if a.link_status == LINK_OK else "warn"
        b.append(f'<span class="bd {cls}" title="{escape(LINK_HELP.get(a.link_status, ""))}">'
                 f'원문 {escape(a.link_status)}</span>')
    elif a.url_type == URL_RELAY:
        b.append('<span class="bd warn" title="원본 언론사 URL을 확보하지 못함">중계 링크 · 원문 미확보</span>')
    else:
        b.append('<span class="bd bad">링크 없음</span>')
    if a.title_match is False:
        b.append('<span class="bd warn">원문 제목 불일치</span>')
    return " ".join(b)


def _title_link(a: Article) -> str:
    href = safe_href(a.url)
    t = escape(a.title)
    if not href:
        return f'<span class="t">{t}</span>'
    note = "" if a.url_type == URL_ORIGINAL else ' <small class="relay">(중계 페이지로 열림)</small>'
    return (f'<a class="t" href="{href}" target="_blank" rel="noopener noreferrer">{t}</a>{note}')


def _meta(a: Article) -> str:
    pub = escape(a.publisher) if a.publisher else '<b class="neg">출처 미확인</b>'
    host = escape(urlparse(a.url).hostname or "") if a.url else ""
    chk = f' · 링크점검 {kst(a.checked_at, "%m-%d %H:%M")}' if a.checked_at else ""
    return (f'<div class="meta"><span>{pub}</span> · 발행 {kst(a.published)} · 수집 {kst(a.collected)}'
            f'{chk}</div><div class="url">{"원문 URL" if a.url_type == URL_ORIGINAL else "중계 URL"}: '
            f'<a href="{safe_href(a.url)}" target="_blank" rel="noopener noreferrer">{escape(a.url or "")}</a>'
            f'{" <em>(" + host + ")</em>" if host else ""}</div>')


def _summaries(a: Article) -> str:
    out = []
    if a.excerpt:
        out.append(f'<div class="sum fact"><span class="lbl">원문 발췌 · {escape(a.excerpt_src or "원문")}</span>'
                   f'{escape(a.excerpt)}</div>')
    if a.ai_summary:
        out.append(f'<div class="sum ai"><span class="lbl">AI 요약 · 원문 본문을 바탕으로 자동 생성, '
                   f'사실 여부는 원문으로 확인</span>{escape(a.ai_summary)}</div>')
    if not out:
        out.append('<div class="sum none">요약 없음 — 원문에서 직접 확인해 주세요.</div>')
    return "".join(out)


def _card(rank: int, pick: dict) -> str:
    a = pick["rep"]
    others = "".join(
        f'<li><a href="{safe_href(o.url)}" target="_blank" rel="noopener noreferrer">'
        f'{escape(o.publisher)}</a> — {escape(o.title)} <span class="dim">({kst(o.published, "%m-%d %H:%M")})</span></li>'
        for o in pick["others"])
    other_block = (f'<details class="others" open><summary>추가 검증 출처 {len(pick["others"])}곳 '
                   f'(같은 사건, 원문 확인됨)</summary><ul>{others}</ul></details>') if others else \
        '<div class="dim single">단독 출처 — 다른 언론사 교차 보도는 확인되지 않음</div>'
    return f"""
<article class="card" id="a-{a.id}">
  <div class="rank">{rank}</div>
  <div class="body">
    <div class="badges">{_badges(a)}</div>
    <h3>{_title_link(a)}</h3>
    {_meta(a)}
    {_summaries(a)}
    {other_block}
    <a class="btn" href="{safe_href(a.url)}" target="_blank" rel="noopener noreferrer">원문 보기 ↗</a>
  </div>
</article>"""


def _row(a: Article, show_reason: bool) -> str:
    reasons = "".join(f'<span class="chip">{escape(r)}</span>' for r in a.exclusion_reasons())
    return (f'<tr><td>{_title_link(a)}<div class="dim">{escape(a.feed)}</div></td>'
            f'<td>{escape(a.publisher) or "<b class=neg>미확인</b>"}</td>'
            f'<td class="nw">{kst(a.published, "%m-%d %H:%M")}</td><td>{_badges(a)}</td>'
            f'{"<td>" + reasons + "</td>" if show_reason else ""}</tr>')


CSS = """
:root{--navy:#0b2545;--blue:#13315c;--acc:#1f6feb;--bg:#f4f6fa;--card:#fff;--ink:#14213d;
--dim:#5b6b82;--line:#dde3ec;--ok:#1a7f37;--warn:#b35900;--bad:#c62828;--offi:#6f42c1}
@media (prefers-color-scheme:dark){:root{--bg:#0d1420;--card:#152033;--ink:#e6edf6;--dim:#9fb0c6;
--line:#26344c;--acc:#5aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:"맑은 고딕","Malgun Gothic","Apple SD Gothic Neo","Noto Sans KR",sans-serif;line-height:1.55}
header{background:linear-gradient(120deg,var(--navy),var(--blue));color:#fff;padding:22px 16px}
.wrap{max-width:980px;margin:0 auto;padding:0 16px}header .wrap{padding:0}
h1{margin:0;font-size:22px}header p{margin:4px 0 0;opacity:.85;font-size:14px}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.stat{background:rgba(255,255,255,.12);border-radius:8px;padding:6px 12px;font-size:13px}
.stat b{font-size:17px;margin-right:4px}
h2{font-size:18px;margin:28px 0 10px;border-left:4px solid var(--acc);padding-left:10px}
.note{font-size:13px;color:var(--dim);background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:10px 12px}
.card{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px;margin:12px 0}
.rank{flex:0 0 34px;height:34px;border-radius:50%;background:var(--navy);color:#fff;display:grid;
place-items:center;font-weight:700}
@media (prefers-color-scheme:dark){.rank{background:var(--acc)}}
.body{min-width:0;flex:1}h3{margin:6px 0 4px;font-size:17px}
a{color:var(--acc)}a.t{color:var(--ink);text-decoration:none}a.t:hover{color:var(--acc);text-decoration:underline}
.meta{font-size:13px;color:var(--dim)}.url{font-size:12px;color:var(--dim);word-break:break-all;margin-top:2px}
.url em{font-style:normal}
.sum{font-size:14px;border-radius:8px;padding:8px 10px;margin-top:8px}
.sum .lbl{display:block;font-size:11.5px;font-weight:700;margin-bottom:2px}
.fact{background:rgba(31,111,235,.07);border-left:3px solid var(--acc)}.fact .lbl{color:var(--acc)}
.ai{background:rgba(111,66,193,.07);border-left:3px dashed var(--offi)}.ai .lbl{color:var(--offi)}
.none{color:var(--dim);background:transparent;border:1px dashed var(--line)}
.bd{display:inline-block;font-size:11.5px;border-radius:999px;padding:1px 8px;border:1px solid}
.prs{color:var(--dim);border-color:var(--line)}.off{color:var(--offi);border-color:var(--offi)}
.agy{color:#0a7a8a;border-color:#0a7a8a}.ok{color:var(--ok);border-color:var(--ok)}
.warn{color:var(--warn);border-color:var(--warn)}.bad{color:var(--bad);border-color:var(--bad)}
.others{margin-top:8px;font-size:13px}.others ul{margin:6px 0 0;padding-left:18px}
.single{font-size:12.5px;margin-top:8px}
.btn{display:inline-block;margin-top:10px;background:var(--navy);color:#fff;text-decoration:none;
border-radius:6px;padding:6px 12px;font-size:13px}
@media (prefers-color-scheme:dark){.btn{background:var(--acc)}}
.dim{color:var(--dim);font-size:12px}.neg{color:var(--bad)}.relay{color:var(--warn)}
.tbl{width:100%;border-collapse:collapse;background:var(--card);font-size:13px}
.tbl th,.tbl td{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}
.tbl th{background:var(--navy);color:#fff;font-weight:600}.nw{white-space:nowrap}
.chip{display:inline-block;font-size:11.5px;background:rgba(198,40,40,.08);color:var(--bad);
border-radius:4px;padding:1px 6px;margin:1px}
.scroll{overflow-x:auto}details>summary{cursor:pointer}
footer{margin:36px 0 50px;font-size:12px;color:var(--dim)}
@media (max-width:600px){.card{padding:12px;gap:10px}.rank{flex-basis:28px;height:28px;font-size:13px}
.tbl td:nth-child(3),.tbl th:nth-child(3){display:none}}
"""


def render(cfg: dict, date_label: str, picks: list[dict], rest_verified: list[Article],
           unverified: list[Article], dead_recent: list[dict], stats: dict, archive: list[str]) -> str:
    cards = "".join(_card(i, p) for i, p in enumerate(picks, 1)) or \
        '<p class="note">오늘은 원문 링크까지 확인된 추천 기사가 없습니다.</p>'
    rest = "".join(_row(a, False) for a in rest_verified)
    unv = "".join(_row(a, True) for a in unverified)
    dead = "".join(
        f'<tr><td>{escape(d["title"])}<div class="url">{escape(d["url"])}</div></td>'
        f'<td>{escape(d.get("publisher") or "")}</td><td class="nw">{kst(d.get("published"), "%m-%d")}</td>'
        f'<td><span class="bd bad">{escape(d["link_status"])}</span>'
        f'<div class="dim">{kst(d.get("checked_at"), "%m-%d %H:%M")} 점검</div></td></tr>'
        for d in dead_recent)
    arch = " · ".join(f'<a href="archive/{d}.html">{d}</a>' for d in archive[:14])
    s = stats
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{escape(cfg['site']['title'])} {date_label}</title><style>{CSS}</style></head><body>
<header><div class="wrap"><h1>{escape(cfg['site']['title'])}</h1>
<p>{date_label} 오전 브리핑 · 기사 제목을 누르면 원문(언론사·발표기관) 페이지가 새 탭에서 열립니다</p>
<div class="stats"><span class="stat"><b>{s['collected']}</b>건 수집</span>
<span class="stat"><b>{s['original']}</b>건 원문 URL 확보</span>
<span class="stat"><b>{s['verified']}</b>건 원문 확인</span>
<span class="stat"><b>{s['picked']}</b>건 추천</span>
<span class="stat"><b>{s['unverified']}</b>건 확인 필요·제외</span></div></div></header>
<main class="wrap">
<h2>오늘의 추천 뉴스 {len(picks)}건</h2>
<p class="note">추천 기준: 원본 URL 확보 + 링크 정상 접속 + 원문 페이지 제목 일치 + 출처 확인.
<b>원문 발췌</b>(파란 칸)는 원문 페이지·언론사가 제공한 문장이고, <b>AI 요약</b>(보라 점선 칸)은 자동 생성 문장입니다.
뉴스·URL은 실제 수집·점검 결과만 표시하며 임의로 만들지 않습니다.</p>
{cards}
<h2>최근 원문 삭제·이동 감지</h2>
{('<div class="scroll"><table class="tbl"><tr><th>기사</th><th>출처</th><th>발행</th><th>상태</th></tr>' + dead + '</table></div>') if dead else '<p class="note">지난 기사 재점검에서 삭제·이동된 원문이 없습니다.</p>'}
<details><summary><h2 style="display:inline-block">그 밖의 원문 확인 기사 {len(rest_verified)}건</h2></summary>
<div class="scroll"><table class="tbl"><tr><th>기사</th><th>출처</th><th>발행</th><th>상태</th></tr>{rest}</table></div></details>
<details><summary><h2 style="display:inline-block">확인 필요 · 추천 제외 {len(unverified)}건</h2></summary>
<p class="note">원본 URL을 확보하지 못했거나(중계 링크), 링크가 열리지 않거나, 출처가 확인되지 않은 기사입니다. 중요 뉴스 추천에서 제외했습니다.</p>
<div class="scroll"><table class="tbl"><tr><th>기사</th><th>출처</th><th>발행</th><th>상태</th><th>제외 사유</th></tr>{unv}</table></div></details>
<footer>지난 브리핑: {arch or '없음'}<br>생성 {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST · 협성히스코 경영지원부</footer>
</main></body></html>"""
