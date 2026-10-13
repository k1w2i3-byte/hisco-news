"""오프라인 단위 테스트 (네트워크 없이 가짜 응답으로 동작 확인).

테스트용 주소는 모두 example.com 계열의 가상 주소입니다.
실제 원문 링크 동작 테스트는 GitHub Actions의 '점검만' 실행 보고서로 확인합니다.
"""
import base64
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hisco_news import kakao, render, resolve, select, verify  # noqa: E402
from hisco_news.models import (Article, LINK_BLOCKED, LINK_GONE, LINK_MOVED, LINK_OK,  # noqa: E402
                               URL_ORIGINAL, URL_RELAY, SRC_OFFICIAL)

NOW = datetime.now(timezone.utc)
CFG = {
    "keywords": [{"q": "압력계", "weight": 3}, {"q": "유량계", "weight": 3}],
    "boost_terms": ["계측"], "exclude_terms": ["특징주"],
    "schedule": {"lookback_hours": 48, "top_n": 10, "recheck_days": 14, "send_at": "08:00"},
    "preferred_publishers": ["연합뉴스"],
    "official": [{"name": "WIKA", "url": "https://corp.example.com/press"}],
    "agency_domains": ["go.kr"],
}


class Resp:
    def __init__(self, status=200, text="", url="", ctype="text/html; charset=utf-8"):
        self.status_code, self.text, self.url = status, text, url
        self.headers = {"Content-Type": ctype}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"
        self.content = text.encode()


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.posts = []

    def get(self, url, **kw):
        for key, r in self.routes.items():
            if url.startswith(key):
                return r(url) if callable(r) else r
        raise verify.requests.ConnectionError("no route " + url)

    def post(self, url, data=None, **kw):
        self.posts.append((url, data))
        return self.get(url)


def page(title, desc="기사 설명", body="계측 산업 소식 본문입니다. " * 20):
    return (f'<html><head><title>{title}</title><meta property="og:title" content="{title}">'
            f'<meta property="og:site_name" content="테스트신문">'
            f'<meta property="og:description" content="{desc}"></head>'
            f'<body><article><p>{body}</p></article></body></html>')


def art(title, url, pub="연합뉴스", hours=2, **kw):
    a = Article(title=title, publisher=pub,
                published=(NOW - timedelta(hours=hours)).isoformat(timespec="seconds"),
                collected=NOW.isoformat(timespec="seconds"), url=url, url_type=URL_ORIGINAL, **kw)
    return a


class TestResolve(unittest.TestCase):
    def test_parse_batchexecute(self):
        inner = json.dumps(["garturlres", "https://news.example.co.kr/article/123", 1])
        body = ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner, None, None, None, "generic"],
                                         ["di", 10]])
        self.assertEqual(resolve.parse_response(body), "https://news.example.co.kr/article/123")

    def test_parse_length_prefixed(self):
        inner = json.dumps(["garturlres", "https://news.example.co.kr/a/9", 1])
        row = json.dumps([["wrb.fr", "Fbv4je", inner, None, None, None, "1"]])
        body = ")]}'\n\n" + f"{len(row)}\n{row}\n25\n" + json.dumps([["di", 30]])
        self.assertEqual(resolve.parse_response(body), "https://news.example.co.kr/a/9")

    def test_real_response_escaped_equals(self):
        """2026-10-13 실제 Google 응답: '=' 가 \\u003d 로 이스케이프됨 → 주소가 잘리면 안 됨."""
        raw = (Path(__file__).parent / "fixtures" / "batchexecute_real_20261013.txt").read_text()
        self.assertEqual(resolve.parse_response(raw),
                         "http://www.newsiesports.com/news/articleViewAmp.html?idxno=46606")

    def test_truncated_url_rejected(self):
        inner = json.dumps(["garturlres", "https://kr.example.net/news/articleView.html?idxno=", 1])
        body = ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner]])
        self.assertIsNone(resolve.parse_response(body))

    def test_rejects_google_url(self):
        inner = json.dumps(["garturlres", "https://news.google.com/something", 1])
        body = ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner]])
        self.assertIsNone(resolve.parse_response(body))

    def test_legacy_id(self):
        raw = b"\x08\x13\x22\x2bhttps://news.example.co.kr/view/777\xd2\x01\x00"
        gid = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertEqual(resolve._legacy_decode(gid), "https://news.example.co.kr/view/777")

    def test_full_resolve_flow(self):
        gid = "CBMiAU_yqLtest"
        inner = json.dumps(["garturlres", "https://news.example.co.kr/article/55", 1])
        s = FakeSession({
            "https://news.google.com/rss/articles/": Resp(200, '<c-wiz><div jscontroller="x" '
                                                          'data-n-a-sg="SIG" data-n-a-ts="1700000000"></div></c-wiz>'),
            resolve.BATCH_URL: Resp(200, ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner]])),
        })
        url = resolve.resolve_google_news(s, f"https://news.google.com/rss/articles/{gid}?hl=ko", pause=0)
        self.assertEqual(url, "https://news.example.co.kr/article/55")
        self.assertIn("garturlreq", s.posts[0][1].replace("%22", '"'))


class TestClassify(unittest.TestCase):
    U = "https://news.example.co.kr/article/2026/10/13/abc"

    def test_ok(self):
        self.assertEqual(verify.classify(Resp(200, page("x"), self.U), self.U, page("x")), LINK_OK)

    def test_404(self):
        self.assertEqual(verify.classify(Resp(404, "", self.U), self.U), LINK_GONE)

    def test_blocked(self):
        self.assertEqual(verify.classify(Resp(403, "", self.U), self.U), LINK_BLOCKED)

    def test_redirect_home(self):
        self.assertEqual(verify.classify(Resp(200, page("홈"), "https://news.example.co.kr/"), self.U,
                                         page("홈")), LINK_MOVED)

    def test_soft_404(self):
        h = page("페이지를 찾을 수 없습니다")
        self.assertEqual(verify.classify(Resp(200, h, self.U), self.U, h), LINK_GONE)

    def test_empty_page_is_gone(self):
        """실제 사례: 없는 기사 번호에 200 + 빈 페이지를 주는 언론사."""
        h = "<html><head></head><body><script>var a=1;</script></body></html>"
        self.assertEqual(verify.classify(Resp(200, h, self.U), self.U, h), LINK_GONE)

    def test_error(self):
        self.assertEqual(verify.classify(None, self.U), verify.LINK_ERROR)


class TestVerify(unittest.TestCase):
    def test_relay_unresolved_stays_relay(self):
        s = FakeSession({"https://news.google.com/": Resp(500, "")})
        a = Article(title="압력계 신제품", publisher="테스트신문", published=None, collected="",
                    url="https://news.google.com/rss/articles/CBMiAU_yqLzzz", url_type=URL_RELAY,
                    relay_url="https://news.google.com/rss/articles/CBMiAU_yqLzzz", feed="Google News")
        verify.verify_article(s, a, CFG)
        self.assertEqual(a.url_type, URL_RELAY)
        self.assertTrue(a.url.startswith("https://news.google.com"))
        self.assertFalse(a.verified)
        self.assertIn("원문 URL 미확보(중계 링크)", a.exclusion_reasons())

    def test_original_verified(self):
        u = "https://news.example.co.kr/article/1"
        s = FakeSession({u: Resp(200, page("압력계 국산화 성공"), u)})
        a = art("압력계 국산화 성공", u, pub="")
        verify.verify_article(s, a, CFG)
        self.assertEqual(a.link_status, LINK_OK)
        self.assertTrue(a.title_match)
        self.assertEqual(a.publisher, "테스트신문")
        self.assertEqual(a.excerpt_src, "원문 페이지")
        self.assertTrue(a.verified)

    def test_title_mismatch_excluded(self):
        u = "https://news.example.co.kr/article/2"
        s = FakeSession({u: Resp(200, page("전혀 다른 연예 기사 제목입니다"), u)})
        a = art("유량계 수출 확대", u)
        verify.verify_article(s, a, CFG)
        self.assertFalse(a.verified)
        self.assertIn("원문 페이지 제목 불일치", a.exclusion_reasons())

    def test_amp_to_canonical(self):
        amp = "https://kr.example.net/news/articleViewAmp.html?idxno=1815297"
        can = "https://kr.example.net/news/articleView.html?idxno=1815297"
        h = page("반도체 가스 초고순도 유지 솔루션 공개").replace(
            "</head>", f'<link rel="canonical" href="{can}"></head>')
        s = FakeSession({amp: Resp(200, h, amp), can: Resp(200, page("반도체 가스 초고순도 유지 솔루션 공개"), can)})
        a = art("반도체 가스 초고순도 유지 솔루션 공개", amp)
        verify.verify_article(s, a, CFG)
        self.assertEqual(a.url, can)
        self.assertTrue(a.verified)

    def test_official_domain(self):
        u = "https://corp.example.com/press/2026/new-gauge"
        s = FakeSession({u: Resp(200, page("WIKA launches new pressure gauge"), u)})
        a = art("WIKA launches new pressure gauge", u, pub="WIKA")
        verify.verify_article(s, a, CFG)
        self.assertEqual(a.source_type, SRC_OFFICIAL)

    def test_no_publisher_excluded(self):
        u = "https://unknown-site.example.net/a/1"
        s = FakeSession({u: Resp(200, "<html><title>압력계 소식</title><p>짧음</p></html>", u)})
        a = art("압력계 소식", u, pub="")
        verify.verify_article(s, a, CFG)
        self.assertIn("출처(언론사·기관) 미확인", a.exclusion_reasons())


class TestSelect(unittest.TestCase):
    def _ok(self, a):
        a.link_status, a.title_match = LINK_OK, True
        return a

    def test_cluster_and_choose(self):
        a1 = self._ok(art("한국 기업 압력계 국산화 성공… 수입 대체 기대", "https://a.example.com/1"))
        a2 = self._ok(art("한국 기업, 압력계 국산화 성공 수입대체 기대", "https://b.example.com/1", pub="전자신문"))
        a3 = self._ok(art("유량계 시장 성장 전망", "https://c.example.com/1", pub="매일경제"))
        a4 = art("압력계 업계 루머", "https://news.google.com/rss/articles/x", pub="")
        a4.url_type = URL_RELAY
        cands = select.filter_candidates([a1, a2, a3, a4], CFG, NOW)
        groups = select.cluster(cands)
        picks = select.choose(groups, CFG, set(), 10, NOW)
        reps = [p["rep"].url for p in picks]
        self.assertEqual(len(picks), 2)                       # 같은 사건 2건 → 1건으로 묶임
        self.assertNotIn(a4.url, reps)                         # 미검증 기사는 추천 제외
        merged = next(p for p in picks if len(p["others"]) == 1)
        self.assertEqual(merged["rep"].publisher, "연합뉴스")  # 선호 언론사가 대표
        self.assertEqual(merged["others"][0].publisher, "전자신문")

    def test_real_titles_clustering(self):
        """2026-10-13 실제 수집 제목: 같은 사건 3건은 묶이고, 다른 기사는 분리."""
        t = ["코리아인스트루먼트, 교정기관 최초로 정보보호·개인정보보호 국제표준 두 개 동시 인증",
             "코리아인스트루먼트, ISO/IEC 27001·27701 동시 획득...보안 내재화 추진",
             "코리아인스트루먼트, KOLAS 교정기관 최초 ISO/IEC 27001·27701 동시 인증",
             "눈에 보이지 않는 불순물까지 잡아 반도체 제조 가스 초고순도 유지... 알에스코리아, SEDEX 2026서 정밀 가스 분석 솔루션 공개",
             "리프웨이브, KES 2026서 초고주파 계측 모듈 'TX500/RX500' 선보여",
             "2035년 기본 에너지 계측기 시장 전망: 전력망 현대화와 효율 규제가 성장 견인",
             "센서 개발 키트 시장 2035년 전망: 엣지 AI와 IoT 프로토타이핑 수요가 성장 견인"]
        arts = [art(x, f"https://s{i}.example.com/a") for i, x in enumerate(t)]
        sizes = sorted(len(g) for g in select.cluster(arts))
        self.assertEqual(sizes, [1, 1, 1, 1, 3])

    def test_exclude_terms_and_old(self):
        old = art("압력계 기사", "https://a.example.com/old", hours=100)
        ex = art("[특징주] 압력계 업체 급등", "https://a.example.com/ex")
        self.assertEqual(select.filter_candidates([old, ex], CFG, NOW), [])

    def test_irrelevant_search_hit_dropped(self):
        a = art("싱가포르항공 여의도 팝업, 한정판 굿즈", "https://x.example.com/1", keyword="압력전송기")
        b = art("IndexBox 계측기 시장 전망", "https://y.example.com/1", pub="IndexBox")
        out = select.filter_candidates([a, b], {**CFG, "exclude_publishers": ["IndexBox"],
                                                "keywords": CFG["keywords"] + [{"q": "계측기", "weight": 3}]}, NOW)
        self.assertEqual([x.url for x in out], [b.url])
        self.assertIn("홍보성 시장보고서 출처(설정에서 제외)", out[0].exclusion_reasons())

    def test_already_sent_skipped(self):
        a = self._ok(art("압력계 국산화", "https://a.example.com/1"))
        picks = select.choose(select.cluster([a]), CFG, {"https://a.example.com/1"}, 10, NOW)
        self.assertEqual(picks, [])


class TestKakao(unittest.TestCase):
    def test_ten_items_four_messages(self):
        picks = [{"rep": art(f"압력계 기사 {i}", f"https://n{i}.example.com/a"), "others": []}
                 for i in range(10)]
        msgs = kakao.build_messages(picks, "https://hisco.github.io/hisco-news", "10/14(수)")
        self.assertEqual([len(m["contents"]) for m in msgs], [3, 3, 2, 2])
        for m in msgs:
            for c in m["contents"]:
                self.assertTrue(c["link"]["web_url"].startswith("https://hisco.github.io/hisco-news/go.html?id="))
        ids = [c["link"]["web_url"].split("id=")[1] for m in msgs for c in m["contents"]]
        self.assertEqual(ids, [p["rep"].id for p in picks])   # 순서·기사 일치

    def test_counts(self):
        for n, expect in [(1, [None]), (2, [2]), (4, [2, 2]), (5, [3, 2]), (7, [3, 2, 2])]:
            picks = [{"rep": art(f"t{i}", f"https://x{i}.example.com/"), "others": []} for i in range(n)]
            msgs = kakao.build_messages(picks, "https://h.github.io/r", "d")
            self.assertEqual([len(m["contents"]) if "contents" in m else None for m in msgs], expect)

    def test_empty(self):
        msgs = kakao.build_messages([], "https://h.github.io/r", "d")
        self.assertEqual(msgs[0]["object_type"], "text")


class TestRender(unittest.TestCase):
    def test_links_and_labels(self):
        a = art("압력계 <script>alert(1)</script>", "https://news.example.co.kr/1")
        a.link_status, a.title_match = LINK_OK, True
        a.excerpt, a.excerpt_src, a.ai_summary = "원문 문장", "원문 페이지", "AI가 만든 문장"
        bad = art("자바스크립트 링크", "javascript:alert(1)")
        bad.url_type = URL_RELAY
        html = render.render({"site": {"title": "T"}}, "2026-10-14",
                             [{"rep": a, "others": [], "score": 1}], [], [bad], [],
                             {"collected": 2, "original": 1, "verified": 1, "picked": 1, "unverified": 1}, [])
        self.assertIn('href="https://news.example.co.kr/1" target="_blank" rel="noopener noreferrer"', html)
        self.assertNotIn("<script>alert", html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn("원문 발췌 · 원문 페이지", html)
        self.assertIn("AI 요약 · 원문 본문을 바탕으로 자동 생성", html)
        self.assertIn("중계 링크 · 원문 미확보", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
