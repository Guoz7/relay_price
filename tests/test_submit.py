import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import adapters as A  # noqa: E402
import build as B  # noqa: E402
import export_readme as ER  # noqa: E402
import pricing as P  # noqa: E402
import submit_page as SP  # noqa: E402


class FakeSite:
    def __init__(self):
        self.pages = {}

    def page(self, path, title, desc, body, **kw):
        self.pages[path] = body


class SubmitPageTests(unittest.TestCase):
    def test_example_parses_with_adapter(self):
        pr, st, name = A.adapt(SP.PUBLIC_SCHEMA_EXAMPLE)
        self.assertEqual(name, "public-schema-cny")
        qs, _ = P.normalize_station("x.com", pr, st)
        ds = next(q for q in qs if q.model == "deepseek-v4.1-flash")
        self.assertAlmostEqual(ds.input_cny, 1.0)
        self.assertAlmostEqual(ds.output_cny, 4.0)

    def test_email_from_config(self):
        self.assertEqual(SP.submit_email({"submit_url": "mailto:a@b.com?subject=x"}), "a@b.com")
        self.assertEqual(SP.submit_email({"submit_url": "https://github.com/x/issues"}), "")

    def test_page_body(self):
        site = FakeSite()
        SP.build_submit(site, {"submit_url": "mailto:a@b.com", "user_agent": "Bot/1 <x>"}, 12, {"candidates": 9, "adopted": 2})
        body = site.pages["/submit/"]
        for needle in ('id="sub-form"', 'data-email="a@b.com"', "Bot/1 &lt;x&gt;", "per_1m_tokens", "VoAPI", "下架"):
            self.assertIn(needle, body)
        self.assertEqual(body.count("<table"), body.count("</table>"))


class SwapDistTests(unittest.TestCase):
    def test_established_seed_wins_mirror_tie(self):
        tmp = Path(tempfile.mkdtemp())
        old_root, old_raw = B.ROOT, B.RAW
        try:
            (tmp / "raw").mkdir()
            for d in ("all.mirror.top", "api.established.com", "zzz.unlisted.io"):
                (tmp / "raw" / f"{d}.json.gz").write_bytes(b"")
            (tmp / "seeds.txt").write_text("# c\napi.established.com\nall.mirror.top  # later\n", encoding="utf-8")
            B.ROOT, B.RAW = tmp, tmp / "raw"
            names = [f.name[:-8] for f in B.snapshot_files()]
            self.assertEqual(names, ["api.established.com", "all.mirror.top", "zzz.unlisted.io"])
        finally:
            B.ROOT, B.RAW = old_root, old_raw
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sync_tree_makes_identical(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            src, dst = tmp / "src", tmp / "dst"
            (src / "a").mkdir(parents=True)
            (src / "a" / "x.html").write_text("new", encoding="utf-8")
            (dst / "old").mkdir(parents=True)
            (dst / "old" / "y.html").write_text("stale", encoding="utf-8")
            (dst / "a").mkdir()
            (dst / "a" / "x.html").write_text("old", encoding="utf-8")
            B.sync_tree(src, dst)
            self.assertEqual((dst / "a" / "x.html").read_text(encoding="utf-8"), "new")
            self.assertFalse((dst / "old").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cf_beacon_and_readme_sync(self):
        from datetime import datetime, timezone

        site = B.Site({"base_url": "https://agentied.work", "site_name": "T", "cf_beacon_token": "tok123"}, datetime.now(timezone.utc), "v1")
        tag = site._analytics_tag()
        self.assertIn("static.cloudflareinsights.com/beacon.min.js", tag)
        self.assertIn('"token":"tok123"', tag)

        tmp = Path(tempfile.mkdtemp())
        try:
            rm = tmp / "README.md"
            rm.write_text(f"top\n{ER.MARKER_START}\nold\n{ER.MARKER_END}\nbottom\n", encoding="utf-8")
            ER.sync_readme_markers(rm, "fresh-rank")
            self.assertEqual(rm.read_text(encoding="utf-8"), f"top\n{ER.MARKER_START}\nfresh-rank\n{ER.MARKER_END}\nbottom\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
