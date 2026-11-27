"""`/compare/` side-by-side matrix for ordinary users (display-only, zero downloads).

The page embeds one compact column-oriented JSON block (`<script type="application/json" id="cmp-data">`) holding only
consumer-facing display metrics: no API addresses, docs / top-up links, quota units, site exchange rates, group ratios,
billing formulas or system versions, and nothing is fetched from `/data/`. `cols` names every field once and each
station is a positional row so ~760 stations stay around 25 KB gzipped.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from html import escape as E
from typing import Any

import pricing as P
import scoring as S

SCHEMA_VERSION = 2
MAX_GZ_BYTES = 40 * 1024
EP_CODES = {"anthropic": "a", "openai": "o", "openai-response": "r", "gemini": "g", "image": "i", "video": "v", "embeddings": "e"}
COLS: list[tuple[str, str]] = [
    ("d", "域名（行序即靠谱度排名）"),
    ("n", "站点名，null=与域名相同"),
    ("score", "综合靠谱分 0–100"),
    ("grade", "等级 S/A/B/C/D"),
    ("stage", "1=仅公开报价 2=已真实调用实测 3=实测未达标"),
    ("pct", "超过全网 N% 站点"),
    ("stab", "防暗涨分 0–100"),
    ("age", "开站月数"),
    ("off", "约为官网价的百分比"),
    ("chp", "比全网 N% 同行便宜"),
    ("price", "充值 ¥ 换 $1 额度"),
    ("mode", "充值方式 discount|fx|custom|unknown"),
    ("login", "快捷登录 [GitHub,Linux.do,Telegram] 1/0"),
    ("email", "注册需邮箱验证 1/0，null=未公开"),
    ("eps", "能接的软件类型代码"),
    ("crc", "公开缓存读取价的型号占比 %"),
    ("models", "在售型号数"),
    ("fs", "热门模型日常价 [[序号, 输入, 输出, 缓存读取]]；¥/百万 token；缓存 null=未公开 \"na\"=不适用"),
]
COL_NAMES = [c for c, _ in COLS]
FORBIDDEN_KEYS = ("server_address", "docs_link", "top_up_link", "quota_per_unit", "usd_exchange_rate", "group_ratio",
                  "billing_expr", "version", "api", "docs", "topup", "qpu", "fx", "ver")


def _s(v: Any, sig: int = 4) -> Any:
    """Round to `sig` significant digits and drop a trailing .0 (keeps the JSON tiny)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    f = float(f"{float(v):.{sig}g}")
    return int(f) if f.is_integer() and abs(f) < 1e15 else f


def _b(v: Any) -> int | None:
    return None if v is None else (1 if v else 0)


def rank_order(stations: dict[str, dict[str, Any]]) -> list[str]:
    return [d for d, _ in sorted(stations.items(), key=lambda kv: (-kv[1]["score"].stage, -kv[1]["score"].total,
                                                                    -kv[1]["score"].raw_total, kv[0]))]


def _cache_cell(q: P.Quote, val: float | None) -> Any:
    if q.cache_status == "na":
        return "na"
    if val is None:
        return None
    return _s(val, 3)


def flagship_cells(qs: list[P.Quote], flagships: tuple[str, ...] | list[str]) -> list[list[Any]]:
    """Cheapest standard-lane (non-anomalous, CNY-convertible) token quote per flagship model (no group names / ratios)."""
    out: list[list[Any]] = []
    for i, canon in enumerate(flagships):
        cand = [q for q in qs if q.canonical == canon and q.kind == "token" and q.lane == "standard"
                and not (q.free or q.welfare) and q.input_cny is not None]
        if not cand:
            continue
        q = min(cand, key=lambda x: (1 if (x.anomaly or x.low_outlier) else 0, x.input_cny, str(x.group or "")))
        out.append([i, _s(q.input_cny, 3), _s(q.output_cny, 3), _cache_cell(q, q.cache_cny)])
    return out


def _stage(sc: S.StationScore) -> int:
    if sc.stage == 2:
        return 3 if sc.cap_reason else 2
    return 1


def station_row(dom: str, st: dict[str, Any], qs: list[P.Quote], it: dict[str, Any],
                stab: dict[str, Any] | None, flagships) -> list[Any]:
    sc: S.StationScore = st["score"]
    age = it.get("domain_age_days")
    cm = st.get("cm") or {}
    name = str(st.get("display_name") or dom)[:24]
    row = {
        "d": dom,
        "n": None if name == dom else name,
        "score": _s(sc.total, 3),
        "grade": sc.grade,
        "stage": _stage(sc),
        "pct": None if sc.percentile is None else int(round(sc.percentile)),
        "stab": int((stab or {}).get("stability_score", 100)),
        "age": int(age) // 30 if isinstance(age, (int, float)) and age >= 0 else None,
        "off": _s(cm.get("off_pct"), 3),
        "chp": None if cm.get("cheaper_pct") is None else int(round(cm["cheaper_pct"])),
        "price": _s(st.get("topup_price"), 3),
        "mode": st.get("topup_mode") or "unknown",
        "login": [_b(st.get("github_oauth")) or 0, _b(st.get("linuxdo_oauth")) or 0, _b(st.get("telegram_oauth")) or 0],
        "email": _b(st.get("email_verification")),
        "eps": "".join(EP_CODES[e] for e in (st.get("endpoints") or []) if e in EP_CODES),
        "crc": _s(st.get("cache_read_coverage"), 2),
        "models": int(st.get("parsed") or len({q.model for q in qs})),
        "fs": flagship_cells(qs, flagships),
    }
    return [row[c] for c in COL_NAMES]


def stations_payload(stations: dict[str, dict[str, Any]], by_station: dict[str, list[P.Quote]], intel: dict[str, Any],
                     stability_map: dict[str, Any], now: datetime, flagships) -> dict[str, Any]:
    rows = [station_row(d, stations[d], by_station.get(d, []), intel.get(d) or {}, stability_map.get(d), flagships)
            for d in rank_order(stations)]
    return {
        "v": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "flagships": list(flagships),
        "cols": COL_NAMES,
        "rows": rows,
    }


def dumps(payload: dict[str, Any]) -> str:
    """JSON safe to embed inside <script type="application/json"> (no `</script>` break-out)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def gz_size(payload: dict[str, Any]) -> int:
    return len(gzip.compress(dumps(payload).encode("utf-8"), mtime=0))


COMPARE_JS = r"""
(function () {
  "use strict";
  var app = document.getElementById("compare-app");
  if (!app) return;
  var MAX = 4;
  var EP = { a: "Claude Code / Cursor", o: "ChatBox / Cherry Studio", r: "Codex 等新版客户端", g: "Gemini 客户端", i: "AI 生图", v: "AI 视频", e: "知识库检索" };
  var MODE = { discount: "折价额度（充得越多越便宜）", fx: "按汇率直充", custom: "站点自定义比例", unknown: "未公开" };
  var STAGE = { 1: "仅公开报价（未人工实测，最高 B 级）", 2: "已通过真实调用实测", 3: "实测未达标（最高 C 级）" };
  var data = null, ix = {}, byDom = {}, rankOf = {}, sel = [], note = "";
  var $ = function (id) { return document.getElementById(id); };
  var head = $("cmp-head"), body = $("cmp-body"), chips = $("cmp-chips"), status = $("cmp-status");
  var q = $("cmp-q"), sug = $("cmp-sug"), diff = $("cmp-diff"), copyBtn = $("cmp-copy");

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function get(r, c) {
    if (c === "rank") return rankOf[r[ix.d]];
    var v = r[ix[c]];
    return (c === "n" && v == null) ? r[ix.d] : v;
  }
  function money(v) {
    if (v == null) return "";
    if (v >= 1) return "¥" + (+v.toFixed(2));
    if (v >= 0.01) return "¥" + (+v.toFixed(4));
    return "¥" + (+v.toFixed(6));
  }
  function muted(t) { return '<span class="muted">' + esc(t) + "</span>"; }
  function say(t) { status.textContent = t; }
  function cell(t, k, n) { return { t: t, k: k == null ? t : k, n: (typeof n === "number" && isFinite(n)) ? n : null }; }
  function yn(v, yes, no) { return v == null ? cell(muted("未公开"), "?") : cell(v ? yes : no, v ? "1" : "0"); }
  function disc(p) {
    if (p == null) return "暂无统一比价基线";
    if (p < 10) return "不到官网 1 折";
    if (p < 95) return "约官网 " + (p / 10).toFixed(1) + " 折";
    if (p <= 105) return "约等于官网价";
    return "高于官网（约 " + (p / 100).toFixed(1) + " 倍）";
  }
  function price(v, cacheish) {
    if (v === "na") return cell(muted("不适用"), "na");
    if (v == null) return cell(muted(cacheish ? "未公开" : "—"), "");
    if (v === 0 && cacheish) return cell('<span class="badge free">免费</span>', "0", 0);
    return cell(money(v), String(v), v);
  }
  function age(m) {
    if (m == null) return cell(muted("未知"), "");
    return cell(m >= 12 ? (m / 12).toFixed(1) + " 年" : (m < 1 ? "不到 1 个月" : m + " 个月"), String(m), m);
  }
  function flag(r, i) {
    var fs = get(r, "fs") || [];
    for (var k = 0; k < fs.length; k++) if (fs[k][0] === i) return fs[k];
    return null;
  }

  function sections() {
    var S = [];
    var base = [
      { l: "综合靠谱分 / 等级", best: "max", f: function (r) { return cell("<b>" + get(r, "score") + "</b> " + esc(get(r, "grade")) + " 级", String(get(r, "score")), get(r, "score")); } },
      { l: "体检方式", f: function (r) { var s = get(r, "stage"); return cell(esc(STAGE[s] || "—"), String(s)); } },
      { l: "靠谱度排名", best: "min", f: function (r) { return cell("第 " + get(r, "rank") + " 名", String(get(r, "rank")), get(r, "rank")); } },
      { l: "在全网的位置", best: "max", f: function (r) { var p = get(r, "pct"); return p == null ? cell(muted("—"), "") : cell("超过 " + p + "% 站点", String(p), p); } }
    ];
    base.push({ l: "防暗涨记录", best: "max", f: function (r) { var v = get(r, "stab"); return cell(String(v) + " 分", String(v), v); } });
    base.push({ l: "开站时长", best: "max", f: function (r) { return age(get(r, "age")); } });
    S.push(["靠谱程度", base]);
    S.push(["价格水平", [
      { l: "相对官网价", best: "min", f: function (r) { var p = get(r, "off"); return p == null ? cell(muted(disc(null)), "") : cell(esc(disc(p)), String(p), p); } },
      { l: "比全网同行便宜", best: "max", f: function (r) { var p = get(r, "chp"); return p == null ? cell(muted("—"), "") : cell("比 " + p + "% 同行便宜", String(p), p); } },
      { l: "充值比例", best: "min", f: function (r) { var p = get(r, "price"); return p == null ? cell(muted("未公开"), "") : cell("¥" + p + " 换 $1 额度", String(p), p); } },
      { l: "充值方式", f: function (r) { var m = get(r, "mode") || "unknown"; return cell(esc(MODE[m] || m), m); } }
    ]]);
    S.push(["注册与使用", [
      { l: "快捷登录", f: function (r) {
        var g = get(r, "login") || [0, 0, 0], names = ["GitHub", "Linux.do", "Telegram"], on = [];
        names.forEach(function (n, i) { if (g[i]) on.push(n); });
        return on.length ? cell(esc(on.join(" · ")), on.join(",")) : cell(muted("仅账号密码"), "");
      } },
      { l: "注册要邮箱验证", f: function (r) { return yn(get(r, "email"), "需要", "不需要"); } },
      { l: "能直接用在", f: function (r) { var e = get(r, "eps") || ""; var t = e.split("").map(function (c) { return EP[c] || c; }); return t.length ? cell(esc(t.join(" · ")), e) : cell(muted("未声明"), ""); } }
    ]]);
    S.push(["长对话缓存与规模", [
      { l: "公开缓存读取价的型号", best: "max", f: function (r) { var v = get(r, "crc"); return v == null ? cell(muted("不适用"), "") : cell(v + "%", String(v), v); } },
      { l: "在售型号数", best: "max", f: function (r) { var v = get(r, "models"); return cell(String(v), String(v), v); } }
    ]]);
    (data.flagships || []).forEach(function (m, i) {
      var cols = [["日常输入价", 1, false], ["输出价", 2, false], ["长对话缓存读取价", 3, true]];
      var rows = cols.map(function (c) {
        return { l: c[0], best: "min", f: function (r) { var x = flag(r, i); return x ? price(x[c[1]], c[2]) : cell(muted("未上架"), "-"); } };
      });
      S.push(["热门模型 · " + m + "（¥ / 百万 token）", rows]);
    });
    return S;
  }
  function render() {
    var n = sel.length, rs = sel.map(function (d) { return byDom[d]; });
    head.innerHTML = '<tr class="hd"><th scope="col" class="lbl">对比项</th>' + rs.map(function (r, i) {
      return '<th scope="col"><a href="/site/' + esc(get(r, "d")) + '/">' + esc(get(r, "n")) + "</a><small>" + esc(get(r, "d")) + "</small>" +
        '<span class="cmp-ops"><button type="button" class="ic" data-mv="' + i + '" aria-label="左移 ' + esc(get(r, "n")) + '"' + (i === 0 ? " disabled" : "") + ">←</button>" +
        '<button type="button" class="ic" data-rm="' + esc(get(r, "d")) + '" aria-label="移除 ' + esc(get(r, "n")) + '">×</button></span></th>';
    }).join("") + "</tr>";
    if (!n) {
      body.innerHTML = '<tr class="empty"><td colspan="2">还没有选择站点，在上方搜索框输入站点名或域名添加 2–4 家。</td></tr>';
      return;
    }
    var html = [], onlyDiff = diff.checked, shown = 0, total = 0;
    sections().forEach(function (sec) {
      var part = [];
      sec[1].forEach(function (row) {
        var cs = rs.map(function (r) { return row.f(r); });
        var same = cs.every(function (c) { return c.k === cs[0].k; });
        total++;
        if (onlyDiff && n > 1 && same) return;
        shown++;
        var best = null;
        if (row.best && !same) {
          var nums = cs.filter(function (c) { return c.n != null; }).map(function (c) { return c.n; });
          if (nums.length >= 2) best = row.best === "min" ? Math.min.apply(null, nums) : Math.max.apply(null, nums);
        }
        part.push('<tr class="' + (same && n > 1 ? "same" : "dif") + '"><th scope="row" class="lbl">' + esc(row.l) + "</th>" + cs.map(function (c) {
          return '<td class="' + (best != null && c.n === best ? "best" : "") + '">' + c.t + "</td>";
        }).join("") + "</tr>");
      });
      if (part.length) html.push('<tr class="sec"><th scope="colgroup" colspan="' + (n + 1) + '">' + esc(sec[0]) + "</th></tr>" + part.join(""));
    });
    if (!shown) html.push('<tr class="empty"><td colspan="' + (n + 1) + '">所选站点在全部固定项上取值相同。</td></tr>');
    body.innerHTML = html.join("");
    say((note ? note + "；" : "") + (onlyDiff ? "只看差异项：显示 " + shown + " / " + total + " 行" : "共 " + total + " 行，标 ✓ 的格子为该行最优值"));
  }

  function renderChips() {
    chips.innerHTML = sel.map(function (d) {
      var r = byDom[d];
      return '<span class="cmp-chip">' + esc(get(r, "n")) + ' <small>第 ' + get(r, "rank") + ' 名</small><button type="button" class="ic" data-rm="' + esc(d) + '" aria-label="移除 ' + esc(get(r, "n")) + '">×</button></span>';
    }).join("") + (sel.length < MAX ? '<span class="muted cmp-left">还可添加 ' + (MAX - sel.length) + " 家</span>" : "");
  }
  function sync() {
    var url = location.pathname + (sel.length ? "?s=" + sel.map(encodeURIComponent).join(",") : "");
    note = "";
    try { history.replaceState(null, "", url); } catch (e) {}
    renderChips();
    render();
  }
  function add(d) {
    if (!byDom[d]) return;
    if (sel.indexOf(d) >= 0) { say("已在对比中"); return; }
    if (sel.length >= MAX) { say("最多并排对比 " + MAX + " 家，请先移除一家"); return; }
    sel.push(d); q.value = ""; sug.hidden = true; sync();
  }
  function remove(d) { sel = sel.filter(function (x) { return x !== d; }); sync(); }
  function moveLeft(i) { if (i > 0 && i < sel.length) { var t = sel[i - 1]; sel[i - 1] = sel[i]; sel[i] = t; sync(); } }

  app.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest("button") : null;
    if (!b) return;
    if (b.dataset.rm) remove(b.dataset.rm);
    else if (b.dataset.mv) moveLeft(parseInt(b.dataset.mv, 10));
    else if (b.dataset.add) add(b.dataset.add);
  });
  diff.addEventListener("change", render);

  var hits = [], cur = -1;
  function search() {
    var t = q.value.trim().toLowerCase();
    if (!t || !data) { sug.hidden = true; return; }
    hits = data.rows.filter(function (r) {
      return String(get(r, "d")).indexOf(t) >= 0 || String(get(r, "n")).toLowerCase().indexOf(t) >= 0;
    }).slice(0, 8);
    cur = hits.length ? 0 : -1;
    sug.innerHTML = hits.length ? hits.map(function (r, i) {
      var on = sel.indexOf(get(r, "d")) >= 0;
      return '<button type="button" role="option" class="' + (i === cur ? "on" : "") + '" data-add="' + esc(get(r, "d")) + '"' + (on ? ' aria-disabled="true"' : "") + ">" +
        "<span>" + esc(get(r, "n")) + " <small>" + esc(get(r, "d")) + "</small></span><span class=\"p\">#" + get(r, "rank") + " · " + get(r, "score") + (on ? " · 已选" : "") + "</span></button>";
    }).join("") : '<div class="empty">没有匹配的站点</div>';
    sug.hidden = false;
  }
  q.addEventListener("input", search);
  q.addEventListener("keydown", function (e) {
    var bs = sug.querySelectorAll("button");
    if (e.key === "ArrowDown" && bs.length) { cur = Math.min(cur + 1, bs.length - 1); e.preventDefault(); }
    else if (e.key === "ArrowUp" && bs.length) { cur = Math.max(cur - 1, 0); e.preventDefault(); }
    else if (e.key === "Enter" && hits[cur]) { e.preventDefault(); add(get(hits[cur], "d")); return; }
    else if (e.key === "Escape") { sug.hidden = true; return; }
    else return;
    for (var i = 0; i < bs.length; i++) bs[i].className = i === cur ? "on" : "";
  });
  document.addEventListener("click", function (e) { if (!sug.contains(e.target) && e.target !== q) sug.hidden = true; });

  copyBtn.addEventListener("click", function () {
    var text = location.origin + "/compare/" + (sel.length ? "?s=" + sel.join(",") : "");
    var label = "复制对比链接";
    function done(ok) {
      copyBtn.textContent = ok ? "已复制" : "请手动复制地址栏";
      copyBtn.classList.toggle("done", ok);
      setTimeout(function () { copyBtn.textContent = label; copyBtn.classList.remove("done"); }, 1600);
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    else done(false);
  });

  function boot() {
    var el = $("cmp-data");
    try {
      data = JSON.parse(el ? el.textContent : "null");
      if (!data || !data.cols) throw new Error("empty");
    } catch (err) {
      body.innerHTML = '<tr class="empty"><td colspan="2">对比数据读取失败，请刷新页面重试。</td></tr>';
      return;
    }
    data.cols.forEach(function (c, i) { ix[c] = i; });
    data.rows.forEach(function (r, i) { byDom[r[ix.d]] = r; rankOf[r[ix.d]] = i + 1; });
    var want = (new URLSearchParams(location.search).get("s") || "").split(",").map(function (s) { return s.trim().toLowerCase(); }).filter(function (s) { return byDom[s]; });
    sel = want.filter(function (s, i) { return want.indexOf(s) === i; }).slice(0, MAX);
    if (!sel.length) {
      sel = data.rows.slice(0, 3).map(function (r) { return get(r, "d"); });
      note = "未指定站点，已预载靠谱度排名前 3 的站点";
    }
    app.classList.add("ready");
    renderChips();
    render();
  }
  boot();
})();
"""


def compare_js() -> str:
    return COMPARE_JS


def build_compare(site: Any, payload: dict[str, Any] | None = None) -> None:
    payload = payload or {"cols": COL_NAMES, "rows": [], "flagships": []}
    n = len(payload.get("rows") or [])
    flags = "、".join(payload.get("flagships") or [])
    n_txt = f"{n} 家在榜站点" if n else "全部在榜站点"
    body = (
        '<section class="hero sm"><p class="eyebrow">并排对比 · 帮你在几家之间做决定</p><h1>中转站并排对比</h1>'
        f'<p class="lead">从 {n_txt}中挑 2–4 家，逐行对齐靠谱分、相对官网的价格、注册门槛、能用的软件、长对话缓存，以及热门模型的日常价'
        f'{("（" + E(flags) + "）") if flags else ""}。未指定站点时自动预载靠谱度排名前 3。</p></section>'
        '<div id="compare-app" class="cmp-app">'
        '<div class="cmp-pick"><div class="search cmp-search" role="search">'
        '<input id="cmp-q" type="search" placeholder="搜索站点名或域名，回车加入对比" autocomplete="off" aria-label="搜索并添加站点" aria-controls="cmp-sug">'
        '<div id="cmp-sug" class="sug" hidden role="listbox"></div></div>'
        '<div id="cmp-chips" class="cmp-chips" aria-live="polite"></div></div>'
        '<div class="cmp-tools"><label class="sw"><input type="checkbox" id="cmp-diff"> 只看差异项</label>'
        '<button type="button" class="btn sm" id="cmp-copy">复制对比链接</button>'
        '<span id="cmp-status" class="muted" role="status"></span></div>'
        '<div class="tbl-wrap"><table class="cmp-mx" id="cmp-mx"><thead id="cmp-head"></thead>'
        '<tbody id="cmp-body"><tr class="empty"><td>正在整理对比数据…</td></tr></tbody></table></div>'
        '<noscript><p class="muted">对比表需要启用 JavaScript；也可以逐家打开 <a href="/stations/">站点排行</a> 里的站点档案查看。</p></noscript>'
        '</div>'
        '<p class="muted">口径：价格为常规按量分组的最低价（¥ / 百万 token），异常低价、免费与福利分组不参与；缓存价「未公开」不等于免费，「不适用」表示该项不涉及。'
        '评分方法见 <a href="/about/">关于本站</a>；数据有误请 <a href="/submit/">提交纠错</a>。</p>'
        f'<script type="application/json" id="cmp-data">{dumps(payload)}</script>'
        f"<script>{compare_js()}</script>"
    )
    site.page(
        "/compare/", "AI API 中转站并排对比：靠谱分、相对官网价格、注册门槛与热门模型价格",
        "并排对比 2–4 家 AI API 中转站：综合靠谱分、约官网几折、比多少同行便宜、快捷登录与能用的软件、长对话缓存与热门模型价格，支持只看差异项与分享链接。",
        body, crumbs=[("站点排行", "/stations/"), ("并排对比", "/compare/")], priority=0.7,
    )