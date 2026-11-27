"""Open station dataset (`/data/stations.json` + `/data/stations.csv`) and the `/compare/` side-by-side matrix.

The dataset is a compact column-oriented table: `cols` names every field once and each station is a
positional row, so ~760 stations with fixed specs + 6 flagship price cells stay well under 280 KB.
`/compare/` is a pure client-side page that loads this file (no extra build-time HTML per station pair).
"""

from __future__ import annotations

import csv
import io
import json
from html import escape as E
from datetime import datetime
from typing import Any

import pricing as P
import scoring as S

SCHEMA_VERSION = 1
MAX_BYTES = 280 * 1024
EP_CODES = {"anthropic": "a", "openai": "o", "openai-response": "r", "gemini": "g", "image": "i", "video": "v", "embeddings": "e"}
COLS: list[tuple[str, str]] = [
    ("d", "域名"),
    ("n", "站点名"),
    ("rank", "信誉排名"),
    ("score", "信誉分（Stage 1 封顶 74）"),
    ("grade", "等级 S/A/B/C/D"),
    ("stage", "认证阶段 1=公开校验 2=实测"),
    ("pct", "全网百分位（超过 N% 站点）"),
    ("dims", "7 维子分 [稳定,资历,透明,口碑,延迟,价稳,纯净]，null=证据不足"),
    ("stab", "价格稳定分 0–100"),
    ("age", "主域名注册天数"),
    ("price", "充值比例：¥ / $1 额度"),
    ("mode", "充值模式 discount|fx|custom|unknown"),
    ("fx", "站内汇率 usd_exchange_rate"),
    ("cur", "显示币种 quota_display_type"),
    ("qpu", "额度单位 quota_per_unit"),
    ("ver", "系统版本"),
    ("api", "API Base URL（已清洗；省略 https:// 前缀）"),
    ("docs", "文档链接（已清洗；省略 https:// 前缀）"),
    ("topup", "充值链接（已清洗；省略 https:// 前缀）"),
    ("login", "登录方式 [GitHub,Linux.do,Telegram] 1/0"),
    ("email", "邮箱验证 1/0，null=未公开"),
    ("ts", "Turnstile 人机校验 1/0，null=未公开"),
    ("eps", "协议代码 a=Anthropic o=OpenAI Chat r=OpenAI Responses g=Gemini i=生图 v=视频 e=Embedding"),
    ("crc", "缓存读公开覆盖率 %"),
    ("cwc", "缓存写公开覆盖率 %"),
    ("groups", "公开分组数"),
    ("models", "有效定价型号数"),
    ("upd", "数据更新时间 unix 秒"),
    ("cfg", "最近充值比例/汇率/分组倍率变动 [YYYY-MM-DD, 说明] 或 null"),
    ("fs", "旗舰常规价 [[旗舰序号, 输入, 输出, 缓存读, 缓存写, 分组(\"\"=default), 分组倍率]]；¥/百万 token；缓存 null=未公开 \"na\"=不适用"),
]
COL_NAMES = [c for c, _ in COLS]


def _s(v: Any, sig: int = 4) -> Any:
    """Round to `sig` significant digits and drop a trailing .0 (keeps the JSON tiny)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    f = float(f"{float(v):.{sig}g}")
    return int(f) if f.is_integer() and abs(f) < 1e15 else f


def _url(v: Any) -> str | None:
    """Drop the implicit `https://` scheme (clients add it back); plain http:// URLs keep their scheme."""
    if not v:
        return None
    s = str(v)
    return s[8:] if s.startswith("https://") else s


def full_url(v: Any) -> str:
    if not v:
        return ""
    s = str(v)
    return s if s.startswith(("http://", "https://")) else "https://" + s


def _b(v: Any) -> int | None:
    return None if v is None else (1 if v else 0)


def rank_order(stations: dict[str, dict[str, Any]]) -> list[str]:
    return [d for d, _ in sorted(stations.items(), key=lambda kv: (-kv[1]["score"].stage, -kv[1]["score"].total,
                                                                    -kv[1]["score"].raw_total, kv[0]))]


def _cache_cell(q: P.Quote, val: float | None, write: bool = False) -> Any:
    if q.cache_status == "na":
        return "na"
    if val is None:
        return None
    return _s(val)


def flagship_cells(qs: list[P.Quote], flagships: tuple[str, ...] | list[str]) -> list[list[Any]]:
    """Cheapest standard-lane (non-anomalous, CNY-convertible) token quote per flagship model."""
    out: list[list[Any]] = []
    for i, canon in enumerate(flagships):
        cand = [q for q in qs if q.canonical == canon and q.kind == "token" and q.lane == "standard"
                and not (q.free or q.welfare) and q.input_cny is not None]
        if not cand:
            continue
        q = min(cand, key=lambda x: (1 if (x.anomaly or x.low_outlier) else 0, x.input_cny, str(x.group or "")))
        out.append([i, _s(q.input_cny), _s(q.output_cny), _cache_cell(q, q.cache_cny), _cache_cell(q, q.cache_write_cny, True),
                    ("" if (q.group or "default") == "default" else str(q.group)[:16]), _s(q.group_ratio)])
    return out


def last_config_change(st: dict[str, Any]) -> list[str] | None:
    chg = st.get("config_changes") or []
    if not chg:
        return None
    c = chg[0]
    return [str(c.get("run_at") or "")[:10], str(c.get("text") or "")[:48]]


def station_row(dom: str, st: dict[str, Any], qs: list[P.Quote], rank: int, it: dict[str, Any],
                stab: dict[str, Any] | None, flagships) -> list[Any]:
    sc: S.StationScore = st["score"]
    dims = [(int(round(sc.dims[k].score)) if (k in sc.dims and sc.dims[k].known) else None) for k in S.DIM_ORDER]
    age = it.get("domain_age_days")
    f = st.get("fetched_at")
    groups = [g for g in (st.get("groups") or []) if g.get("selectable")]
    row = {
        "d": dom,
        "n": str(st.get("display_name") or dom)[:40],
        "rank": rank,
        "score": _s(sc.total, 3),
        "grade": sc.grade,
        "stage": sc.stage,
        "pct": None if sc.percentile is None else int(round(sc.percentile)),
        "dims": dims,
        "stab": int((stab or {}).get("stability_score", 100)),
        "age": int(age) if isinstance(age, (int, float)) and age >= 0 else None,
        "price": _s(st.get("topup_price")),
        "mode": st.get("topup_mode") or "unknown",
        "fx": _s(st.get("usd_exchange_rate")),
        "cur": st.get("quota_display_type"),
        "qpu": _s(st.get("quota_per_unit"), 8),
        "ver": (str(st.get("version"))[:24] if st.get("version") else None),
        "api": _url(st.get("server_address")),
        "docs": _url(st.get("docs_link")),
        "topup": _url(st.get("top_up_link")),
        "login": [_b(st.get("github_oauth")) or 0, _b(st.get("linuxdo_oauth")) or 0, _b(st.get("telegram_oauth")) or 0],
        "email": _b(st.get("email_verification")),
        "ts": _b(st.get("turnstile_check")),
        "eps": "".join(EP_CODES[e] for e in (st.get("endpoints") or []) if e in EP_CODES),
        "crc": st.get("cache_read_coverage"),
        "cwc": st.get("cache_write_coverage"),
        "groups": len(groups),
        "models": int(st.get("parsed") or len({q.model for q in qs})),
        "upd": int(f.timestamp()) if isinstance(f, datetime) else None,
        "cfg": last_config_change(st),
        "fs": flagship_cells(qs, flagships),
    }
    return [row[c] for c in COL_NAMES]


def stations_payload(stations: dict[str, dict[str, Any]], by_station: dict[str, list[P.Quote]], intel: dict[str, Any],
                     stability_map: dict[str, Any], now: datetime, flagships, base_url: str = "") -> dict[str, Any]:
    rows = [station_row(d, stations[d], by_station.get(d, []), i, intel.get(d) or {}, stability_map.get(d), flagships)
            for i, d in enumerate(rank_order(stations), 1)]
    return {
        "v": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "source": (base_url.rstrip("/") + "/compare/") if base_url else "/compare/",
        "note": "只读公开 /api/status + /api/pricing 归一化结果；价格为人民币/百万 token（常规按量分组最低价），以站点实时页面为准。",
        "flagships": list(flagships),
        "dim_keys": list(S.DIM_ORDER),
        "cols": COL_NAMES,
        "col_desc": {c: d for c, d in COLS},
        "rows": rows,
    }


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _csv_cell(v: Any) -> Any:
    if v is None:
        return ""
    if v == "na":
        return "n/a"
    return v


def stations_csv(payload: dict[str, Any]) -> str:
    """Flat CSV mirror of stations.json (dims / login / flagships expanded into columns)."""
    cols = payload["cols"]
    ix = {c: i for i, c in enumerate(cols)}
    dim_keys = payload.get("dim_keys") or list(S.DIM_ORDER)
    flags = payload.get("flagships") or []
    head: list[str] = []
    for c in cols:
        if c == "dims":
            head += [f"dim_{k}" for k in dim_keys]
        elif c == "login":
            head += ["login_github", "login_linuxdo", "login_telegram"]
        elif c == "cfg":
            head += ["cfg_date", "cfg_change"]
        elif c == "fs":
            for m in flags:
                head += [f"{m}_in", f"{m}_out", f"{m}_cache_read", f"{m}_cache_write", f"{m}_group", f"{m}_group_ratio"]
        else:
            head.append(c)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(head)
    for r in payload["rows"]:
        out: list[Any] = []
        for c in cols:
            v = r[ix[c]]
            if c == "dims":
                out += [_csv_cell(x) for x in (v or [None] * len(dim_keys))]
            elif c == "login":
                out += [_csv_cell(x) for x in (v or [None] * 3)]
            elif c == "cfg":
                out += [_csv_cell(x) for x in (v or [None, None])]
            elif c == "fs":
                cells: dict[int, list[Any]] = {int(x[0]): x[1:] for x in (v or [])}
                for i in range(len(flags)):
                    out += [_csv_cell(x) for x in cells.get(i, [None] * 6)]
            elif c in ("api", "docs", "topup"):
                out.append(full_url(v))
            else:
                out.append(_csv_cell(v))
        w.writerow(out)
    return buf.getvalue()
COMPARE_JS = r"""
(function () {
  "use strict";
  var app = document.getElementById("compare-app");
  if (!app) return;
  var MAX = 4;
  var DIMS = __DIMS__;
  var EP = { a: "Anthropic", o: "OpenAI Chat", r: "OpenAI Responses", g: "Gemini", i: "生图", v: "视频", e: "Embedding" };
  var MODE = { discount: "折价额度", fx: "按汇率直充", custom: "自定义比例", unknown: "未公开" };
  var data = null, ix = {}, byDom = {}, sel = [], note = "";
  var $ = function (id) { return document.getElementById(id); };
  var head = $("cmp-head"), body = $("cmp-body"), chips = $("cmp-chips"), status = $("cmp-status");
  var q = $("cmp-q"), sug = $("cmp-sug"), diff = $("cmp-diff"), copyBtn = $("cmp-copy");

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function get(r, c) { return r[ix[c]]; }
  function money(v) {
    if (v == null) return "";
    if (v >= 1) return "¥" + (+v.toFixed(2));
    if (v >= 0.01) return "¥" + (+v.toFixed(4));
    return "¥" + (+v.toFixed(6));
  }
  function full(u) { return u ? (/^https?:\/\//.test(u) ? u : "https://" + u) : ""; }
  function muted(t) { return '<span class="muted">' + esc(t) + "</span>"; }
  function say(t) { status.textContent = t; }
  function cell(t, k, n) { return { t: t, k: k == null ? t : k, n: (typeof n === "number" && isFinite(n)) ? n : null }; }
  function yn(v, yes, no) { return v == null ? cell(muted("未公开"), "?") : cell(v ? yes : no, v ? "1" : "0"); }
  function link(u) {
    if (!u) return cell(muted("未公开"), "");
    var f = full(u);
    return cell('<a class="ext" href="' + esc(f) + '" rel="nofollow noopener" target="_blank">' + esc(u.slice(0, 48)) + "</a>", f);
  }
  function price(v, cacheish) {
    if (v === "na") return cell(muted("不适用"), "na");
    if (v == null) return cell(muted(cacheish ? "未公开" : "—"), "");
    if (v === 0 && cacheish) return cell('<span class="badge free">免费</span>', "0", 0);
    return cell(money(v), String(v), v);
  }
  function age(d) {
    if (d == null) return cell(muted("未知"), "");
    return cell(d >= 365 ? (d / 365).toFixed(1) + " 年" : d + " 天", String(d), d);
  }
  function flag(r, i) {
    var fs = get(r, "fs") || [];
    for (var k = 0; k < fs.length; k++) if (fs[k][0] === i) return fs[k];
    return null;
  }

  function sections() {
    var S = [];
    var base = [
      { l: "信誉分 / 等级", best: "max", f: function (r) { return cell("<b>" + get(r, "score") + "</b> " + esc(get(r, "grade")) + " 级 · Stage " + get(r, "stage"), String(get(r, "score")), get(r, "score")); } },
      { l: "信誉排名", best: "min", f: function (r) { return cell("#" + get(r, "rank"), String(get(r, "rank")), get(r, "rank")); } },
      { l: "全网百分位", best: "max", f: function (r) { var p = get(r, "pct"); return p == null ? cell(muted("—"), "") : cell("超过 " + p + "% 站点", String(p), p); } }
    ];
    (data.dim_keys || []).forEach(function (k, i) {
      base.push({ l: "子分 · " + (DIMS[k] || k), best: "max", f: function (r) { var v = (get(r, "dims") || [])[i]; return v == null ? cell(muted("证据不足"), "") : cell(String(v), String(v), v); } });
    });
    base.push({ l: "价格稳定分", best: "max", f: function (r) { var v = get(r, "stab"); return cell(String(v), String(v), v); } });
    base.push({ l: "域名时长", best: "max", f: function (r) { return age(get(r, "age")); } });
    S.push(["基础信誉与分位", base]);
    S.push(["计费与汇率口径", [
      { l: "充值比例", best: "min", f: function (r) { var p = get(r, "price"); return p == null ? cell(muted("未公开"), "") : cell("¥" + p + " / $1", String(p), p); } },
      { l: "充值模式", f: function (r) { var m = get(r, "mode") || "unknown"; return cell(esc(MODE[m] || m), m); } },
      { l: "站内汇率", f: function (r) { var v = get(r, "fx"); return v == null ? cell(muted("未公开"), "") : cell(String(v), String(v)); } },
      { l: "显示币种", f: function (r) { var v = get(r, "cur"); return v ? cell("<code>" + esc(v) + "</code>", v) : cell(muted("未公开"), ""); } },
      { l: "额度单位", f: function (r) { var v = get(r, "qpu"); return v ? cell(Number(v).toLocaleString() + " 额度 = $1", String(v)) : cell(muted("未公开"), ""); } },
      { l: "最近汇率 / 分组倍率变动", f: function (r) { var c = get(r, "cfg"); return c ? cell(esc(c[0]) + " " + esc(c[1]), c.join(" ")) : cell(muted("近 30 天无"), ""); } }
    ]]);
    S.push(["接入与账号门槛", [
      { l: "系统版本", f: function (r) { var v = get(r, "ver"); return v ? cell("<code>" + esc(v) + "</code>", v) : cell(muted("未公开"), ""); } },
      { l: "API 地址", f: function (r) { var v = get(r, "api"); return v ? cell("<code>" + esc(full(v)) + "</code>", full(v)) : cell(muted("未公开"), ""); } },
      { l: "文档链接", f: function (r) { return link(get(r, "docs")); } },
      { l: "充值入口", f: function (r) { return link(get(r, "topup")); } },
      { l: "登录方式", f: function (r) {
        var g = get(r, "login") || [0, 0, 0], names = ["GitHub", "Linux.do", "Telegram"], on = [];
        names.forEach(function (n, i) { if (g[i]) on.push(n); });
        return on.length ? cell(esc(on.join(" · ")), on.join(",")) : cell(muted("仅账号密码"), "");
      } },
      { l: "邮箱验证", f: function (r) { return yn(get(r, "email"), "需要", "不需要"); } },
      { l: "Turnstile 人机校验", f: function (r) { return yn(get(r, "ts"), "开启", "未开启"); } }
    ]]);
    S.push(["协议与缓存覆盖", [
      { l: "支持协议", f: function (r) { var e = get(r, "eps") || ""; var t = e.split("").map(function (c) { return EP[c] || c; }); return t.length ? cell(esc(t.join(" · ")), e) : cell(muted("未声明"), ""); } },
      { l: "缓存读覆盖率", best: "max", f: function (r) { var v = get(r, "crc"); return v == null ? cell(muted("不适用"), "") : cell(v + "%", String(v), v); } },
      { l: "缓存写覆盖率", best: "max", f: function (r) { var v = get(r, "cwc"); return v == null ? cell(muted("不适用"), "") : cell(v + "%", String(v), v); } },
      { l: "公开分组数", f: function (r) { var v = get(r, "groups"); return cell(String(v), String(v)); } },
      { l: "有效型号数", best: "max", f: function (r) { var v = get(r, "models"); return cell(String(v), String(v), v); } },
      { l: "数据更新", f: function (r) { var u = get(r, "upd"); return u ? cell(new Date(u * 1000).toLocaleString("zh-CN", { hour12: false }), String(u)) : cell(muted("—"), ""); } }
    ]]);
    (data.flagships || []).forEach(function (m, i) {
      var cols = [["常规输入", 1, false], ["输出", 2, false], ["缓存读", 3, true], ["缓存写", 4, true]];
      var rows = cols.map(function (c) {
        return { l: c[0], best: "min", f: function (r) { var x = flag(r, i); return x ? price(x[c[1]], c[2]) : cell(muted("未上架"), "-"); } };
      });
      rows.push({ l: "常规组名与倍率", f: function (r) {
        var x = flag(r, i); if (!x) return cell(muted("—"), "-");
        var g = x[5] || "default";
        return cell("<code>" + esc(g) + "</code>" + (x[6] != null ? " ×" + x[6] : ""), g + "|" + x[6]);
      } });
      S.push(["旗舰 · " + m + "（¥ / 百万 token）", rows]);
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
      return '<span class="cmp-chip">' + esc(get(r, "n")) + ' <small>#' + get(r, "rank") + '</small><button type="button" class="ic" data-rm="' + esc(d) + '" aria-label="移除 ' + esc(get(r, "n")) + '">×</button></span>';
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
    body.innerHTML = '<tr class="empty"><td colspan="2">正在加载 /data/stations.json …</td></tr>';
    fetch(app.dataset.src || "/data/stations.json").then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (d) {
      data = d;
      d.cols.forEach(function (c, i) { ix[c] = i; });
      d.rows.forEach(function (r) { byDom[get(r, "d")] = r; });
      var want = (new URLSearchParams(location.search).get("s") || "").split(",").map(function (s) { return s.trim().toLowerCase(); }).filter(function (s) { return byDom[s]; });
      sel = want.filter(function (s, i) { return want.indexOf(s) === i; }).slice(0, MAX);
      if (!sel.length) {
        sel = d.rows.slice(0, 3).map(function (r) { return get(r, "d"); });
        note = "未指定站点，已预载信誉排名前 3 的站点";
      }
      app.classList.add("ready");
      renderChips();
      render();
    }).catch(function (err) {
      body.innerHTML = '<tr class="empty"><td colspan="2">数据加载失败（' + esc(err.message) + '）。<button type="button" class="btn sm" id="cmp-retry">重试</button></td></tr>';
      var rb = $("cmp-retry"); if (rb) rb.addEventListener("click", boot);
    });
  }
  boot();
})();
"""


def fit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Guarantee the open dataset stays within MAX_BYTES (drops the field glossary first, then config texts)."""
    if len(dumps(payload).encode("utf-8")) <= MAX_BYTES:
        return payload
    payload = dict(payload, col_desc={"_": "字段说明见 /about/ 第七节"})
    if len(dumps(payload).encode("utf-8")) > MAX_BYTES:
        ci = payload["cols"].index("cfg")
        payload["rows"] = [r[:ci] + [None] + r[ci + 1:] for r in payload["rows"]]
    return payload


def compare_js() -> str:
    dims = {k: S.DIM_LABELS.get(k, k) for k in S.DIM_ORDER}
    return COMPARE_JS.replace("__DIMS__", json.dumps(dims, ensure_ascii=False))


def build_compare(site: Any, payload: dict[str, Any] | None = None) -> None:
    n = len((payload or {}).get("rows") or [])
    flags = "、".join((payload or {}).get("flagships") or [])
    n_txt = f"{n} 家在榜站点" if n else "全部在榜站点"
    body = (
        '<section class="hero sm"><p class="eyebrow">并排对比 · 纯前端读取开放数据</p><h1>中转站并排对比</h1>'
        f'<p class="lead">从 {n_txt}中挑 2–4 家，逐行对齐信誉分位、充值与汇率口径、接入与账号门槛、协议与缓存覆盖，以及 6 大旗舰模型的常规价'
        f'{("（" + E(flags) + "）") if flags else ""}。未指定站点时自动预载信誉排名前 3。</p></section>'
        '<div id="compare-app" class="cmp-app" data-src="/data/stations.json">'
        '<div class="cmp-pick"><div class="search cmp-search" role="search">'
        '<input id="cmp-q" type="search" placeholder="搜索站点名或域名，回车加入对比" autocomplete="off" aria-label="搜索并添加站点" aria-controls="cmp-sug">'
        '<div id="cmp-sug" class="sug" hidden role="listbox"></div></div>'
        '<div id="cmp-chips" class="cmp-chips" aria-live="polite"></div></div>'
        '<div class="cmp-tools"><label class="sw"><input type="checkbox" id="cmp-diff"> 只看差异项</label>'
        '<button type="button" class="btn sm" id="cmp-copy">复制对比链接</button>'
        '<a class="btn sm" href="/data/stations.json" download>下载 JSON</a>'
        '<a class="btn sm" href="/data/stations.csv" download>导出 CSV</a>'
        '<span id="cmp-status" class="muted" role="status"></span></div>'
        '<div class="tbl-wrap"><table class="cmp-mx" id="cmp-mx"><thead id="cmp-head"></thead>'
        '<tbody id="cmp-body"><tr class="empty"><td>正在加载对比数据…</td></tr></tbody></table></div>'
        '<noscript><p class="muted">对比矩阵需要启用 JavaScript；也可直接下载 <a href="/data/stations.json">stations.json</a> 或 <a href="/data/stations.csv">stations.csv</a>。</p></noscript>'
        '</div>'
        '<p class="muted">口径：价格为常规按量分组最低价（¥ / 百万 token），异常低价、免费与福利分组不参与；缓存价「未公开」不等于免费，「不适用」表示该项不涉及。'
        '字段说明见 <a href="/about/">评分方法第七节</a>；数据有误请 <a href="/submit/">提交纠错</a>。</p>'
        f"<script>{compare_js()}</script>"
    )
    site.page(
        "/compare/", "AI API 中转站并排对比：充值比例、登录门槛、缓存与旗舰价格",
        "并排对比 2–4 家 AI API 中转站：信誉分位、充值模式与汇率、API 地址与登录门槛、缓存读写覆盖率与 6 大旗舰模型价格，支持只看差异项与分享链接。",
        body, crumbs=[("站点排行", "/stations/"), ("并排对比", "/compare/")], priority=0.7,
    )
