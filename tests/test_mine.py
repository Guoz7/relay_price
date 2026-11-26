import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import mine_extract as X  # noqa: E402
import mine_probe as MP  # noqa: E402


class ExtractTests(unittest.TestCase):
    def test_urls_and_bare_domains(self):
        text = "友链：<a href='https://API.FooRelay.com/register?aff=1'>Foo</a>，备用 www.bar-ai.top；文档 docs.foorelay.com，图床 i.imgur.com/x.png"
        hosts = X.candidates(text)
        self.assertIn("api.foorelay.com", hosts)
        self.assertIn("bar-ai.top", hosts)
        self.assertNotIn("docs.foorelay.com", hosts)
        self.assertFalse(any(h.endswith("imgur.com") for h in hosts))

    def test_noise_filtered(self):
        text = ("https://github.com/a/b https://t.me/x https://api.openai.com/v1 https://cdn.jsdelivr.net/x "
                "https://192.168.1.2:3000 http://localhost:3000 https://linux.do/t/1 https://foo.github.io/ "
                "https://chat.lobehub.com https://img.shields.io/badge")
        self.assertEqual(X.candidates(text), set())

    def test_file_names_are_not_domains(self):
        self.assertEqual(X.candidates("see README.md, config.json, index.html and logo.png"), set())

    def test_self_links_and_extra_block(self):
        text = "https://api.self.com https://other.cc https://bad.ai"
        self.assertEqual(X.candidates(text, self_roots={"self.com"}, extra_block={"bad.ai"}), {"other.cc"})

    def test_root_of(self):
        self.assertEqual(X.root_of("api.foo.com.cn"), "foo.com.cn")
        self.assertEqual(X.root_of("a.b.foo.ai"), "foo.ai")
        self.assertEqual(X.root_of("relay.vercel.app"), "relay.vercel.app")

    def test_collapse_prefers_root_and_skips_known(self):
        got = X.collapse({"api.new.com", "new.com", "api.only.ai", "x.known.com", "solo.sub.dev"}, {"known.com"})
        self.assertEqual(set(got), {"new.com", "only.ai", "solo.sub.dev"})
        self.assertEqual(got["new.com"], ["api.new.com", "new.com"])


class ProbeHelperTests(unittest.TestCase):
    def test_public_origin(self):
        self.assertEqual(MP.public_origin("https://api.x.com/"), "https://api.x.com")
        for bad in ("http://localhost:3000", "http://127.0.0.1:3000", "http://10.0.0.2", "ftp://x.com", None, ""):
            self.assertIsNone(MP.public_origin(bad))

    def test_alt_urls_dead_host(self):
        self.assertEqual(MP.alt_urls("foo.com", {}), ["https://api.foo.com/api/pricing"])
        self.assertEqual(MP.alt_urls("api.foo.com", {}), [])

    def test_alt_urls_alive_host_uses_public_hint_only(self):
        urls = MP.alt_urls("foo.com", {"status_http": 200, "status": {"server_address": "http://localhost:3000"}})
        self.assertNotIn("http://localhost:3000/api/pricing", urls)
        self.assertIn("https://api.foo.com/api/pricing", urls)
        urls = MP.alt_urls("foo.com", {"status_http": 200, "status": {"server_address": "https://gw.foo.net"}})
        self.assertEqual(urls[0], "https://gw.foo.net/api/pricing")

    def test_reason(self):
        self.assertIn("401/403", MP.reason_of({"pricing_http": 403}))
        self.assertIn("404", MP.reason_of({"pricing_http": 404}))
        self.assertIn("无法连接", MP.reason_of({"pricing_error": "URLError"}))

    def test_progress_resume(self):
        import io
        import tempfile
        old = MP.PROGRESS
        with tempfile.TemporaryDirectory() as td:
            MP.PROGRESS = Path(td) / "p.jsonl"
            try:
                buf = io.StringIO()
                MP.record(buf, "run1", "a.com", reason="超时")
                MP.record(buf, "run1", "a.com", adopted={"pricing_url": "https://a.com/api/pricing", "system": "new-api", "quotes": 5})
                MP.record(buf, "run1", "b.com", reason="HTTP 500")
                MP.record(buf, "run0", "c.com", reason="stale run")
                MP.PROGRESS.write_text(buf.getvalue() + "not json\n", encoding="utf-8")
                adopted, rejected = MP.load_progress("run1")
                self.assertEqual(set(adopted), {"a.com"})
                self.assertEqual([r["domain"] for r in rejected], ["b.com"])
            finally:
                MP.PROGRESS = old


if __name__ == "__main__":
    unittest.main()
