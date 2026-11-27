"""Batch 1: PSL shared-host truth guard, raw SHA-256 receipts, evidence-backed consumer tags, setup card and favourites."""
import argparse
import gzip
import hashlib
import inspect
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build as B  # noqa: E402
import compare_page as CP  # noqa: E402
import consumer as C  # noqa: E402
import dashboard as DB  # noqa: E402
import pricing as P  # noqa: E402
import psl  # noqa: E402
import refresh as R  # noqa: E402
import timeseries as TS  # noqa: E402
import verify_dist as VD  # noqa: E402

NOW = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
CONTAMINATED = {
    "jbbt.pages.dev": {"root": "pages.dev", "domain_age_days": 2276, "domain_registered_at": "2020-09-02",
                       "domain_expires_at": "2027-09-02", "registrar": "CloudFlare, Inc.", "tranco_rank": 162,
                       "umbrella_rank": None, "majestic_rank": 315, "net_checked_at": "2026-11-26T17:13:09+00:00", "sources": []},
    "test1122.up.railway.app": {"root": "railway.app", "domain_age_days": 2674, "tranco_rank": 2121, "majestic_rank": 9,
                                "registrar": "x", "net_checked_at": "2026-11-26T17:13:09+00:00"},
    "api.lijiajiaai.it.com": {"root": "it.com", "domain_age_days": 12452, "domain_registered_at": "1992-10-23",
                              "tranco_rank": 1197, "majestic_rank": 381, "registrar": "Intis Telecom Limited",
                              "net_checked_at": "2026-11-26T12:16:00+00:00", "sources": ["a"]},
    "api.apiyi.com": {"root": "apiyi.com", "domain_age_days": 900, "tranco_rank": 50000, "majestic_rank": None,
                      "net_checked_at": "2026-11-26T12:16:00+00:00"},
}


def _node_check(test, js: str, name: str):
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
        tf.write(js)
        p = Path(tf.name)
    try:
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        test.assertEqual(r.returncode, 0, msg=f"{name}: {r.stderr}")
    finally:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- 1. PSL truth guard
class PslTests(unittest.TestCase):
    def test_shared_host_detection_and_root(self):
        self.assertEqual(psl.shared_host_of("jbbt.pages.dev"), "pages.dev")
        self.assertEqual(psl.shared_host_of("test1122.up.railway.app"), "up.railway.app")
        self.assertEqual(psl.shared_host_of("https://Foo.Vercel.App/v1"), "vercel.app")
        self.assertIsNone(psl.shared_host_of("pages.dev"))
        self.assertIsNone(psl.shared_host_of("api.apiyi.com"))
        self.assertEqual(psl.registrable_root("jbbt.pages.dev"), "jbbt.pages.dev")
        self.assertEqual(psl.registrable_root("test1122.up.railway.app"), "test1122.up.railway.app")
        self.assertNotEqual(psl.registrable_root("a.pages.dev"), psl.registrable_root("b.pages.dev"))

    def test_multi_level_roots(self):
        self.assertEqual(psl.registrable_root("api.lijiajiaai.it.com"), "lijiajiaai.it.com")
        self.assertEqual(psl.registrable_root("a.hohai.eu.org"), "hohai.eu.org")
        self.assertEqual(psl.registrable_root("api.foo.com.cn"), "foo.com.cn")
        self.assertEqual(psl.registrable_root("bc.sn.cn"), "bc.sn.cn")
        self.assertEqual(psl.registrable_root("api.apiyi.com"), "apiyi.com")
        self.assertEqual(psl.registrable_root("apiyi.com"), "apiyi.com")
        self.assertEqual(C.root_label("jbbt.pages.dev"), "jbbt")
        self.assertEqual(C.root_label("api.lijiajiaai.it.com"), "lijiajiaai")
        self.assertNotEqual(C.brand_stem("a.hohai.eu.org"), C.brand_stem("x.other.eu.org"))

    def test_sanitize_shared_host_clears_inherited_signals(self):
        for dom, sh in (("jbbt.pages.dev", "pages.dev"), ("test1122.up.railway.app", "up.railway.app")):
            e = psl.sanitize_intel_entry(dom, CONTAMINATED[dom])
            self.assertEqual(e["shared_host"], sh)
            self.assertEqual(e["root"], dom)
            for k in psl.REGISTRY_FIELDS:
                self.assertIsNone(e[k], (dom, k))
            self.assertEqual(psl.sanitize_intel_entry(dom, e), e)
        self.assertEqual(CONTAMINATED["jbbt.pages.dev"]["domain_age_days"], 2276)

    def test_sanitize_multi_level_and_normal(self):
        e = psl.sanitize_intel_entry("api.lijiajiaai.it.com", CONTAMINATED["api.lijiajiaai.it.com"])
        self.assertEqual(e["root"], "lijiajiaai.it.com")
        for k in psl.REGISTRY_FIELDS:
            self.assertIsNone(e[k])
        self.assertIsNone(e["net_checked_at"])
        self.assertEqual(e["sources"], ["a"])
        self.assertNotIn("shared_host", e)
        fresh = dict(e, domain_age_days=120, tranco_rank=None, net_checked_at="2026-12-01T00:00:00+00:00")
        self.assertEqual(psl.sanitize_intel_entry("api.lijiajiaai.it.com", fresh), fresh)
        ok = psl.sanitize_intel_entry("api.apiyi.com", CONTAMINATED["api.apiyi.com"])
        self.assertEqual(ok, CONTAMINATED["api.apiyi.com"])
        self.assertEqual(ok["domain_age_days"], 900)
        self.assertEqual(ok["tranco_rank"], 50000)

    def test_repo_intel_json_is_clean(self):
        intel = json.loads((ROOT / "data" / "intel.json").read_text(encoding="utf-8"))
        self.assertGreater(len(intel), 100)
        bad_shared = [d for d, e in intel.items() if psl.shared_host_of(d)
                      and any(e.get(k) is not None for k in ("domain_age_days", "tranco_rank", "umbrella_rank", "majestic_rank"))]
        self.assertEqual(bad_shared, [])
        bare = [d for d, e in intel.items() if e.get("root") in psl.MULTI_LEVEL_TLDS or e.get("root") in psl.SHARED_HOST_SUFFIXES]
        self.assertEqual(bare, [])
        self.assertEqual(intel["jbbt.pages.dev"]["shared_host"], "pages.dev")
        self.assertEqual(intel["api.lijiajiaai.it.com"]["root"], "lijiajiaai.it.com")
        self.assertEqual(psl.sanitize_intel(intel), intel)

    def test_build_and_intel_use_guard(self):
        self.assertIn("psl.sanitize_intel(load_json", inspect.getsource(B.build))
        src = (ROOT / "tools" / "intel.py").read_text(encoding="utf-8")
        self.assertIn("psl.registrable_root", src)
        self.assertNotIn("relay_hub.root_dom", src)
        self.assertIn("d not in shared", src)

    def test_merge_mirrors_never_merges_unrelated_shared_hosts(self):
        stations = {d: {"display_name": "New API"} for d in ("aaa.pages.dev", "bbb.pages.dev")}
        quotes = [P.Quote(domain=d, model="m", canonical="m", family="other", kind="token", group=None, group_ratio=None,
                          group_desc=None) for d in stations]
        intel = {d: {"root": "pages.dev"} for d in stations}
        out = B.merge_mirrors(quotes, stations, intel)
        self.assertEqual(set(stations), {"aaa.pages.dev", "bbb.pages.dev"})
        self.assertEqual(len(out), 2)
        st2 = {d: {"display_name": "Same"} for d in ("a.same.com", "b.same.com")}
        q2 = [P.Quote(domain=d, model="m", canonical="m", family="other", kind="token", group=None, group_ratio=None,
                      group_desc=None) for d in st2]
        B.merge_mirrors(q2, st2, {})
        self.assertEqual(len(st2), 1)


# ---------------------------------------------------------------- 2. receipts + cross-framework status
NEWAPI_STATUS = {
    "system_name": "Demo", "price": 1, "quota_per_unit": 500000, "email_verification": False, "linuxdo_oauth": True,
    "linuxdo_minimum_trust_level": 2, "github_oauth": True, "checkin_enabled": True, "enable_online_topup": False,
    "min_topup": 5, "quota_for_new_user": 0, "discord_oauth": False, "enable_drawing": True, "enable_task": True,
    "SidebarModulesAdmin": json.dumps({"chat": {"enabled": True, "playground": True, "chat": False}}),
    "chats": [{"Cherry Studio": "cherrystudio://providers/api-keys?v=1&data={cherryConfig}"}, {"CC Switch": "ccswitch://{key}"}],
    "turnstile_site_key": "secret-ish", "top_up_link": "https://pay.example/{key}",
}
ONEHUB_STATUS = {
    "system_name": "Hub", "CheckinEnabled": True, "CheckinQuota": 50000, "GuestChatPageEnabled": True,
    "AlipayFaceToFaceEnabled": False, "CustomAvailablePayMethods": 2, "linuxdo_oauth": False, "linuxdo_minimum_trust_level": 3,
}


class StatusReceiptTests(unittest.TestCase):
    def test_slim_status_new_api_snake_case(self):
        s = R._slim_status(NEWAPI_STATUS)
        self.assertIs(s["checkin_enabled"], True)
        self.assertIs(s["web_playground"], True)
        self.assertEqual(s["linuxdo_min_tl"], 2)
        self.assertIs(s["discord_oauth"], False)
        self.assertIs(s["enable_drawing"], True)
        self.assertIs(s["enable_task"], True)
        self.assertIs(s["pay_online"], False)
        self.assertEqual(s["min_topup"], 5)
        self.assertEqual(s["chats"], ["Cherry Studio", "CC Switch"])
        blob = json.dumps(s, ensure_ascii=False)
        for bad in ("cherryConfig", "ccswitch://", "SidebarModulesAdmin", "turnstile_site_key", "secret-ish"):
            self.assertNotIn(bad, blob)

    def test_slim_status_one_hub_pascal_case(self):
        s = R._slim_status(ONEHUB_STATUS)
        self.assertIs(s["checkin_enabled"], True)
        self.assertEqual(s["checkin_quota"], 50000)
        self.assertIs(s["web_playground"], True)
        self.assertIs(s["pay_online"], True)
        self.assertNotIn("linuxdo_min_tl", s)

    def test_slim_status_absent_fields_stay_absent(self):
        s = R._slim_status({"system_name": "x"})
        for k in ("checkin_enabled", "web_playground", "linuxdo_min_tl", "pay_online", "checkin_quota", "chats"):
            self.assertNotIn(k, s)
        off = R._slim_status({"SidebarModulesAdmin": {"chat": {"enabled": False, "playground": True}}})
        self.assertIs(off["web_playground"], False)
        self.assertIsNone(R._slim_status(None))
        self.assertIsNone(R._slim_status(["x"]))

    def test_receipts_and_fetch_pipeline(self):
        st_body = json.dumps({"success": True, "data": NEWAPI_STATUS}).encode()
        pr_body = json.dumps({"success": True, "data": [{"model_name": "gpt-6-astra", "quota_type": 0, "model_ratio": 1.25,
                                                         "completion_ratio": 8, "enable_groups": ["default"]}],
                              "group_ratio": {"default": 1}}).encode()
        self.assertEqual(R.receipt(st_body), hashlib.sha256(st_body).hexdigest()[:16])
        self.assertIsNone(R.receipt(b""))
        tmp = Path(tempfile.mkdtemp(prefix="rc-receipt-"))
        saved = {k: getattr(R, k) for k in ("_get", "DATA", "RAW", "STATE", "RUNS", "ENDPOINTS", "load_config")}
        try:
            R._get = lambda url, ua, timeout: (200, st_body if url.endswith("/api/status") else pr_body, None)
            R.DATA, R.RAW, R.STATE, R.RUNS, R.ENDPOINTS = tmp, tmp / "raw", tmp / "state.json", tmp / "runs", tmp / "ep.json"
            R.load_config = lambda: {"user_agent": "test"}
            one = R.fetch_one("x.test", "ua", 1.0)
            self.assertEqual(one["status_sha256"], R.receipt(st_body))
            self.assertEqual(one["pricing_sha256"], R.receipt(pr_body))
            self.assertRegex(one["status_sha256"], r"^[0-9a-f]{16}$")
            R.cmd_fetch(argparse.Namespace(only=["x.test"], workers=1, timeout=1.0, deadline=30.0))
            state = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["x.test"]["status_sha256"], R.receipt(st_body))
            self.assertEqual(state["x.test"]["pricing_sha256"], R.receipt(pr_body))
            with gzip.open(tmp / "raw" / "x.test.json.gz", "rt", encoding="utf-8") as fh:
                snap = json.load(fh)
            self.assertEqual(snap["status_sha256"], R.receipt(st_body))
            self.assertEqual(snap["pricing_sha256"], R.receipt(pr_body))
            self.assertIs(snap["status"]["web_playground"], True)
        finally:
            for k, v in saved.items():
                setattr(R, k, v)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normalize_station_exposes_features_safely(self):
        pricing = {"data": [{"model_name": "gpt-6-astra", "quota_type": 0, "model_ratio": 1.25, "completion_ratio": 8,
                             "enable_groups": ["default"]}], "group_ratio": {"default": 1}}
        _, meta = P.normalize_station("x.test", pricing, R._slim_status(NEWAPI_STATUS))
        self.assertIs(meta["checkin_enabled"], True)
        self.assertIs(meta["web_playground"], True)
        self.assertEqual(meta["linuxdo_min_tl"], 2)
        self.assertIs(meta["pay_online"], False)
        self.assertEqual(meta["chats"], ["Cherry Studio", "CC Switch"])
        _, empty = P.normalize_station("x.test", pricing, None)
        for k in ("checkin_enabled", "checkin_quota", "web_playground", "linuxdo_min_tl", "discord_oauth",
                  "enable_drawing", "enable_task", "pay_online", "chats"):
            self.assertIn(k, empty)
            self.assertIsNone(empty[k], k)


# ---------------------------------------------------------------- 3. tags + glossary
def _q(**kw):
    base = dict(domain="x.test", model="m", canonical="m", family="other", kind="token", group="default", group_ratio=1.0,
                group_desc=None)
    base.update(kw)
    return P.Quote(**base)


class TagTests(unittest.TestCase):
    def test_empty_inputs_yield_zero_tags(self):
        self.assertEqual(C.station_tags({}, []), [])
        _, meta = P.normalize_station("x.test", {}, None)
        self.assertEqual(C.station_tags(meta, []), [])
        _, meta2 = P.normalize_station("x.test", {"data": []}, {})
        self.assertEqual(C.station_tags(meta2, []), [])

    def test_unknown_or_missing_prices_are_not_free(self):
        self.assertEqual(C.station_tags({}, [_q(input_usd=None, input_cny=None)]), [])
        self.assertEqual(C.station_tags({}, [_q(input_usd=0.0, output_usd=2.0)]), [])
        self.assertEqual(C.station_tags({"new_user_usd": 0, "min_topup": 50, "email_verification": None,
                                         "checkin_enabled": None, "web_playground": False, "linuxdo_oauth": False}, []), [])

    def test_verified_fields_yield_exact_tags_with_provenance(self):
        st = {"checkin_enabled": True, "web_playground": True, "new_user_usd": 1.5, "min_topup": 5, "email_verification": False,
              "linuxdo_oauth": True, "linuxdo_min_tl": 2, "github_oauth": True,
              "groups": [{"name": "default", "ratio": 1, "selectable": True, "caveats": []},
                         {"name": "公益免费", "ratio": 0, "selectable": True, "caveats": []}]}
        qs = [_q(model="gpt-6-free", input_usd=0.0, output_usd=0.0, input_cny=0.0, output_cny=0.0, free=True),
              _q(model="gpt-6-astra", input_usd=2.5, output_usd=20, input_cny=2.5, output_cny=20)]
        tags = C.station_tags(st, qs)
        by = {t["key"]: t for t in tags}
        self.assertEqual(list(by), ["free_model", "free_group", "checkin", "playground", "new_user_gift", "low_topup",
                                    "no_email", "pure_std", "linuxdo", "github"])
        self.assertIn("1 个模型", by["free_model"]["tip"])
        self.assertIn("gpt-6-free", by["free_model"]["tip"])
        self.assertIn("公益免费", by["free_group"]["tip"])
        self.assertEqual(by["checkin"]["tip"], "来自站点公开接口：已开启每日签到领额度")
        self.assertEqual(by["playground"]["tip"], "来自站点公开接口：内置网页对话/测试台，免配客户端可先试")
        self.assertEqual(by["new_user_gift"]["tip"], "来自站点公开接口：新用户赠送 $1.5 额度")
        self.assertEqual(by["low_topup"]["tip"], "来自站点公开接口：最低充值 5")
        self.assertEqual(by["linuxdo"]["label"], "Linux.do TL2+")
        self.assertEqual(by["no_email"]["label"], "免邮箱秒注册")
        for t in tags:
            self.assertTrue(t["tip"].startswith("来自站点公开"), t)
            self.assertIn(t["cls"], ("ok", "info"))
        self.assertEqual(C.tag_facets(tags), ["free", "noemail", "pure"])
        self.assertEqual(C.station_tags({"linuxdo_oauth": True, "linuxdo_min_tl": 0}, [])[0]["label"], "Linux.do 登录")

    def test_pure_std_blocked_by_reverse_tool_or_caveats(self):
        g = lambda n, cav=(): {"name": n, "ratio": 1, "selectable": True, "caveats": list(cav)}  # noqa: E731
        for groups in ([g("default"), g("claude-kiro")], [g("default"), g("官逆")], [g("default"), g("cursor-pro")],
                       [g("default"), g("限时", ["特惠/限时分组"])]):
            keys = {t["key"] for t in C.station_tags({"groups": groups}, [])}
            self.assertNotIn("pure_std", keys, groups)
        keys = {t["key"] for t in C.station_tags({"groups": [g("default")]}, [_q(caveats=["自称Kiro"])])}
        self.assertNotIn("pure_std", keys)
        self.assertIn("pure_std", {t["key"] for t in C.station_tags({"groups": [g("default"), g("vip")]}, [])})
        self.assertNotIn("free_group", {t["key"] for t in C.station_tags({"groups": [g("免费 禁止使用")]}, [])})

    def test_group_glossary(self):
        self.assertEqual(C.explain_group("claude-kiro")[0], "工具特惠线")
        self.assertEqual(C.explain_group("claude-awsq")[0], "工具特惠线")
        self.assertEqual(C.explain_group("claude 满血")[0], "常规官转线")
        self.assertEqual(C.explain_group("公益免费")[0], "免费/体验组")
        self.assertEqual(C.explain_group("逆向")[0], "逆向线路")
        self.assertEqual(C.explain_group("官逆")[0], "逆向线路")
        self.assertEqual(C.explain_group("周卡")[0], "按次/套餐组")
        self.assertEqual(C.explain_group("双十一限时")[0], "特惠促销线")
        self.assertEqual(C.explain_group("default"), ("常规按量组", "按实际对话字数计费"))
        self.assertEqual(C.explain_group("vip", caveats=["自称Kiro"])[0], "工具特惠线")
        self.assertIn("限速", C.explain_group("逆向")[1])
        for name in ("claude-kiro", "逆向", "公益免费", "default", "claude 满血"):
            badge, text = C.explain_group(name)
            self.assertTrue(badge and text)
            self.assertEqual(VD.jargon_hits(text), [])
        rows = C.group_type_summary([{"name": "default", "selectable": True}, {"name": "claude-kiro", "selectable": True},
                                     {"name": "hidden-逆向", "selectable": False}])
        self.assertEqual([r["badge"] for r in rows], ["常规按量组", "工具特惠线"])
        self.assertIn("线路类型一眼看懂", DB.group_legend([{"name": "default"}, {"name": "claude-kiro"}]))
        self.assertEqual(DB.group_legend([{"name": "default"}, {"name": "vip"}]), "")

    def test_shared_host_verdict_warning(self):
        _, warns = C.verdict_tags(stage=1, failed=False, age_days=None, ups=0, sharp=0, monitored=True, cache_cov=0,
                                  attempts=0, successes=0, caveat_groups=0, shared_host="pages.dev")
        self.assertIn(("使用托管二级域名（*.pages.dev），无法核实独立建站时间，建议先小额试水", "warn"), warns)


# ---------------------------------------------------------------- 4. setup card
class SetupCardTests(unittest.TestCase):
    def test_base_url_sanitized(self):
        self.assertEqual(DB.setup_base_url("foo.com", {"server_address": "https://api.foo.com/v1/"}), ("https://api.foo.com", True))
        self.assertEqual(DB.setup_base_url("foo.com", {"server_address": "https://foo.com/"}), ("https://foo.com", True))
        for bad in ("http://localhost:3000", "http://127.0.0.1", "http://192.168.1.2:3000", "https://your-domain.com",
                    "https://docs.newapi.pro", "https://evil.com", "{address}", "", None):
            self.assertEqual(DB.setup_base_url("foo.com", {"server_address": bad}), ("https://foo.com", False), bad)
        self.assertEqual(DB.setup_base_url("jbbt.pages.dev", {"server_address": "https://other.pages.dev"})[0],
                         "https://jbbt.pages.dev")

    def test_client_urls_and_copy_buttons(self):
        html = DB.setup_card("foo.com", {"server_address": "https://api.foo.com/v1", "endpoints": ["openai"],
                                         "chats": ["Cherry Studio", "CC Switch", "OpenCat", "DeepChat", "Lobe"]})
        for client in ("Cherry Studio", "ChatBox", "沉浸式翻译", "Claude Code", "Cursor"):
            self.assertIn(f">{client}</button>", html)
        self.assertIn('id="su-u0">https://api.foo.com</code>', html)
        self.assertIn('id="su-u1">https://api.foo.com</code>', html)
        self.assertIn('id="su-u2">https://api.foo.com/v1/chat/completions</code>', html)
        self.assertIn('ANTHROPIC_BASE_URL=&quot;https://api.foo.com&quot;', html)
        self.assertIn('id="su-u4">https://api.foo.com/v1</code>', html)
        self.assertIn("复制地址", html)
        self.assertIn("复制配置", html)
        self.assertIn('data-done="已复制 ✓"', html)
        self.assertIn("该站后台还自带一键导入：Cherry Studio、CC Switch、OpenCat、DeepChat", html)
        self.assertNotIn("Lobe", html)
        self.assertIn("未声明 Claude 原生接口", html)
        self.assertNotIn("/v1/v1", html)
        self.assertEqual(VD.jargon_hits(html), [])
        leak = DB.setup_card("foo.com", {"server_address": "http://localhost:3000"})
        self.assertNotIn("localhost", leak)
        self.assertNotIn("该站后台还自带一键导入", leak)


# ---------------------------------------------------------------- 5. mini end-to-end build
BASE = {"claude-sonnet-5-5": (1.5, 5, 0.1), "gpt-6-astra": (1.25, 8, 0.1), "deepseek-v4.1-flash": (0.15, 4, 0.1),
        "gemini-3.8-flash": (0.15, 4, 0.25)}
FEAT = {"price": 1, "usd_exchange_rate": 7.3, "quota_per_unit": 500000, "email_verification": False, "linuxdo_oauth": True,
        "linuxdo_min_tl": 2, "checkin_enabled": True, "web_playground": True, "quota_for_new_user": 500000, "min_topup": 5,
        "chats": ["Cherry Studio", "CC Switch"], "server_address": "http://localhost:3000"}
PLAIN = {"price": 1, "usd_exchange_rate": 7.3, "quota_per_unit": 500000, "email_verification": True}
MINI = {
    "jbbt.pages.dev": (PLAIN, {"default": 1, "claude-kiro": 0.4}),
    "gift.example-a.com": (FEAT, {"default": 0.5, "公益免费": 0}),
    "plain.example-b.net": (PLAIN, {"default": 0.8}),
    "api.lijiajiaai.it.com": (PLAIN, {"default": 0.6}),
}


def _pricing(groups):
    return {"data": [{"model_name": m, "quota_type": 0, "model_ratio": r[0], "completion_ratio": r[1], "cache_ratio": r[2],
                      "enable_groups": list(groups)} for m, r in BASE.items()],
            "group_ratio": groups, "usable_group": {g: g for g in groups}}


class MiniBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = B.load_json(ROOT / "config.json", {})
        stations, by_station = {}, defaultdict(list)
        for i, (d, (status, groups)) in enumerate(MINI.items()):
            qs, meta = P.normalize_station(d, _pricing(groups), status)
            st = dict(meta)
            st.update({"display_name": d.split(".")[0].title(), "fetched_at": NOW, "parsed": len(BASE), "http_status": 200,
                       "endpoints": ["anthropic", "openai"] if i % 2 == 0 else ["openai"]})
            stations[d] = st
            by_station[d].extend(qs)
        intel = psl.sanitize_intel({d: dict(CONTAMINATED.get(d) or {"domain_age_days": 400}) for d in MINI})
        cls.intel = intel
        by_model = defaultdict(list)
        for qs in by_station.values():
            for q in qs:
                by_model[q.canonical].append(q)
        B.mark_anomalies(by_model, cfg)
        md = B.compute_station_scores(stations, by_station, intel, {}, cfg, {}, stability_map={}, now=NOW, monitored=True)
        B.compute_quote_scores(by_model, stations, cfg)
        specs = B.MS.load_model_specs(B.DATA)
        cx = B.consumer_context(stations, by_station, specs, cfg, [], ledger=TS.empty_ledger(), intel=intel, run_stats={},
                                stability_map={}, monitored=True)
        cls.tmp = Path(tempfile.mkdtemp(prefix="rc-b1-"))
        old = B.DIST
        B.DIST = cls.tmp
        try:
            site = B.Site(cfg, NOW, "t1")
            B.build_station_pages(site, stations, by_station, intel, [], cfg, run_stats={}, stability_map={}, reviews_map={},
                                  market_dims=md, market_px=B.flagship_market(by_model, specs, cfg), cx=cx)
            B.build_indexes(site, {}, stations, by_station, {}, cfg, 3, intel=intel, stability_map={}, cx=cx)
            CP.build_compare(site, CP.stations_payload(stations, by_station, intel, {}, NOW, B.FLAGSHIP_MODELS))
            B.build_home(site, cfg, {}, stations, {}, [], {}, [])
            site.flush()
            B.build_seo_files(site, {}, stations, 0)
        finally:
            B.DIST = old
        cls.stations = stations

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def read(self, rel):
        return (self.tmp / rel).read_text(encoding="utf-8")

    def test_zero_download_and_plain_language(self):
        self.assertEqual(VD.check_zero_download(self.tmp), [])
        self.assertEqual(VD.check_plain_language(self.tmp), [])
        layer1 = [self.tmp / "stations" / "index.html", self.tmp / "compare" / "index.html", *self.tmp.glob("site/*/index.html")]
        self.assertEqual(len(layer1), 2 + len(MINI))
        for f in layer1:
            self.assertEqual(VD.jargon_hits(f.read_text(encoding="utf-8")), [], f.parent.name)
        self.assertFalse((self.tmp / "data").exists() and any((self.tmp / "data").rglob("*")))

    def test_shared_host_page_is_honest(self):
        html = self.read("site/jbbt.pages.dev/index.html")
        self.assertIn("使用托管二级域名（*.pages.dev），无法核实独立建站时间，建议先小额试水", html)
        self.assertIn("托管二级域名（*.pages.dev）", html)
        self.assertIn("无法通过域名核实建站年限", html)
        for bad in ("Tranco #162", "Majestic #315", "老站", "2020-09-02", "CloudFlare, Inc."):
            self.assertNotIn(bad, html)
        self.assertIn("线路类型一眼看懂", html)
        self.assertIn("工具特惠线", html)
        self.assertIn("<th>通俗解释</th>", html)
        it = self.read("site/api.lijiajiaai.it.com/index.html")
        for bad in ("34.1 年", "Tranco #1,197", "1992-10-23"):
            self.assertNotIn(bad, it)

    def test_practical_tags_and_setup_card(self):
        html = self.read("site/gift.example-a.com/index.html")
        self.assertIn('class="vd-ptags"', html)
        for lbl in ("有免费/体验分组", "支持每日签到", "支持网页试测", "注册送额度", "低门槛起充", "免邮箱秒注册",
                    "无逆向纯常规", "Linux.do TL2+"):
            self.assertIn(lbl, html)
        self.assertIn('data-tip="来自站点公开接口：已开启每日签到领额度"', html)
        self.assertIn("30 秒接进我的软件", html)
        self.assertIn("https://gift.example-a.com/v1/chat/completions", html)
        self.assertNotIn("localhost", VD.first_screen_text(html))
        self.assertIn("该站后台还自带一键导入：Cherry Studio、CC Switch", html)
        plain = self.read("site/plain.example-b.net/index.html")
        for lbl in ("支持每日签到", "支持网页试测", "注册送额度", "免邮箱秒注册", "有免费"):
            self.assertNotIn(lbl, plain)
        self.assertNotIn("线路类型一眼看懂", plain)

    def test_favourite_markup_and_facets(self):
        site = self.read("site/gift.example-a.com/index.html")
        self.assertIn('data-fav-btn="gift.example-a.com"', site)
        self.assertIn("☆ 收藏常用站", site)
        for rel in ("stations/index.html", "index.html"):
            html = self.read(rel)
            self.assertIn('<section class="fav-bar" id="fav-bar" hidden aria-label="我收藏的常用站"></section>', html)
        st = self.read("stations/index.html")
        for t in ('data-f="free"', 'data-f="noemail"', 'data-f="pure"', "有免费/可试测", "免邮箱秒注册", "无逆向纯常规"):
            self.assertIn(t, st)
        self.assertIn('data-dom="gift.example-a.com"', st)
        self.assertRegex(st, r'data-f="[^"]*\bfree\b[^"]*\bnoemail\b[^"]*\bpure\b[^"]*">')
        self.assertIn('class="rtag free"', st)
        self.assertEqual(st.count("<tr>"), st.count("</tr>"))
        pay = json.loads(re.search(r'<script type="application/json" id="bubble-data">(.*?)</script>', st, re.S).group(1))
        facets = {r[0]: r[-1].split() for r in pay["rows"]}
        if "gift.example-a.com" in facets:
            self.assertIn("free", facets["gift.example-a.com"])

    def test_scores_do_not_inherit_platform_age(self):
        cfg = B.load_json(ROOT / "config.json", {})
        st = {d: dict(s, score=None) for d, s in self.stations.items()}
        qs = defaultdict(list)
        raw = {d: dict(CONTAMINATED.get(d) or {"domain_age_days": 400}) for d in MINI}
        B.compute_station_scores(st, qs, raw, {}, cfg, {}, stability_map={}, now=NOW, monitored=True)
        dirty = st["jbbt.pages.dev"]["score"].dims["longevity"].score
        self.assertGreater(dirty, self.stations["jbbt.pages.dev"]["score"].dims["longevity"].score)

    def test_node_check_assets_and_inline_scripts(self):
        for rel in ("assets/app.js", "functions/api/verify.js"):
            r = subprocess.run(["node", "--check", str(ROOT / rel)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, msg=f"{rel}: {r.stderr}")
        app = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
        for needle in ('"relay_favs_v1"', "MAX = 8", "data-fav-btn", "一键对比收藏", "去排行查看状态", "★ 已收藏", "data-done"):
            self.assertIn(needle, app)
        for f in self.tmp.rglob("*.html"):
            for attrs, js in re.findall(r"<script\b([^>]*)>(.*?)</script>", f.read_text(encoding="utf-8"), re.S):
                if "application/json" in attrs or "application/ld+json" in attrs or "src=" in attrs or not js.strip():
                    continue
                _node_check(self, js, f.name)


if __name__ == "__main__":
    unittest.main()