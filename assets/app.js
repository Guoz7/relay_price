(function () {
  "use strict";
  // ---------- theme
  var tb = document.getElementById("theme");
  if (tb) tb.addEventListener("click", function () {
    var cur = document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    var next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) {}
  });

  // ---------- search
  var idx = null, loading = null;
  function load() {
    if (idx) return Promise.resolve(idx);
    if (!loading) loading = fetch("/search-index.json").then(function (r) { return r.json(); }).then(function (d) { idx = d; return d; });
    return loading;
  }
  function norm(s) { return String(s || "").toLowerCase().replace(/[\s_]+/g, "-"); }
  function money(v) {
    if (v == null) return "";
    if (v >= 1) return "¥" + (+v.toFixed(2));
    if (v >= 0.01) return "¥" + (+v.toFixed(4));
    return "¥" + (+v.toFixed(6));
  }
  function score(item, q) {
    var m = item.m, i = m.indexOf(q);
    if (m === q) return 1000 + item.n;
    if (i === 0) return 600 + item.n;
    if (i > 0) return 400 + item.n;
    for (var k = 0; k < item.v.length; k++) if (norm(item.v[k]).indexOf(q) >= 0) return 200 + item.n;
    var parts = q.split("-").filter(Boolean);
    if (parts.length > 1 && parts.every(function (p) { return m.indexOf(p) >= 0; })) return 100 + item.n;
    return -1;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function bind(input, box) {
    if (!input || !box) return;
    var sel = -1, results = [];
    function render() {
      var q = norm(input.value.trim());
      if (!q) { box.hidden = true; return; }
      load().then(function (d) {
        results = d.map(function (it) { return [score(it, q), it]; })
          .filter(function (x) { return x[0] >= 0; })
          .sort(function (a, b) { return b[0] - a[0]; }).slice(0, 12).map(function (x) { return x[1]; });
        sel = results.length ? 0 : -1;
        box.innerHTML = results.length ? results.map(function (it, i) {
          var p = it.p != null ? money(it.p) + " / " + money(it.o) : (it.c != null ? money(it.c) + "/次" : "");
          return '<a href="/model/' + it.s + '/" class="' + (i === sel ? "on" : "") + '"><span><code>' + esc(it.m) +
            '</code> <small>' + it.n + ' 家</small></span><span class="p">' + p + "</span></a>";
        }).join("") : '<div class="empty">没有找到，换个关键词试试，如 "flash"、"sonnet"</div>';
        box.hidden = false;
      });
    }
    function mark() {
      var as = box.querySelectorAll("a");
      for (var i = 0; i < as.length; i++) as[i].className = i === sel ? "on" : "";
    }
    input.addEventListener("focus", function () { load(); if (input.value) render(); });
    input.addEventListener("input", render);
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { sel = Math.min(sel + 1, results.length - 1); mark(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); mark(); e.preventDefault(); }
      else if (e.key === "Enter" && results[sel]) { location.href = "/model/" + results[sel].s + "/"; }
      else if (e.key === "Escape") { box.hidden = true; }
    });
    document.addEventListener("click", function (e) { if (!box.contains(e.target) && e.target !== input) box.hidden = true; });
  }
  bind(document.getElementById("q"), document.getElementById("sug"));
  bind(document.getElementById("q2"), document.getElementById("sug2"));
  var qp = new URLSearchParams(location.search).get("q");
  if (qp) { var i2 = document.getElementById("q2") || document.getElementById("q"); if (i2) { i2.value = qp; i2.focus(); i2.dispatchEvent(new Event("input")); } }
  if (location.pathname === "/" && document.getElementById("q2") && matchMedia("(min-width: 861px)").matches) document.getElementById("q2").focus();

  // ---------- sortable tables (rows may have a trailing .sub detail row)
  function groups(tbody) {
    var out = [], rows = tbody.children;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].classList.contains("sub") && out.length) out[out.length - 1].push(rows[i]);
      else out.push([rows[i]]);
    }
    return out;
  }
  function sortTable(t, col, asc) {
    var ths = t.querySelectorAll("thead th");
    Array.prototype.forEach.call(ths, function (x) { x.removeAttribute("aria-sort"); });
    ths[col].setAttribute("aria-sort", asc ? "ascending" : "descending");
    var num = ths[col].dataset.type === "n", tb = t.tBodies[0];
    var g = groups(tb);
    g.sort(function (a, b) {
      var ca = a[0].children[col], cb = b[0].children[col];
      var va = ca ? ca.dataset.v || ca.textContent : "", vb = cb ? cb.dataset.v || cb.textContent : "";
      if (num) {
        var fa = parseFloat(va), fb = parseFloat(vb);
        // >= 9e8 (no RMB price) or non-numeric ("na" = unknown): always keep at the bottom
        var ma = !(fa < 9e8), mb = !(fb < 9e8);
        if (ma !== mb) return ma ? 1 : -1;
        if (ma && mb) return (isNaN(fa) ? 1 : 0) - (isNaN(fb) ? 1 : 0) || (fa - fb) || 0;
        return asc ? fa - fb : fb - fa;
      }
      var r = String(va).localeCompare(String(vb), "zh");
      return asc ? r : -r;
    });
    var frag = document.createDocumentFragment();
    g.forEach(function (rs) { rs.forEach(function (r) { frag.appendChild(r); }); });
    tb.appendChild(frag);
  }
  Array.prototype.forEach.call(document.querySelectorAll("table.sortable"), function (t) {
    var ths = t.querySelectorAll("thead th");
    Array.prototype.forEach.call(ths, function (th, col) {
      var btn = th.querySelector("button"); if (!btn) return;
      btn.addEventListener("click", function () {
        var cur = th.getAttribute("aria-sort");
        var asc = cur ? cur !== "ascending" : th.dataset.first !== "desc";
        sortTable(t, col, asc);
      });
    });
  });

  // ---------- filters (checkbox hide + endpoint protocol chip + cache perspective toggle)
  Array.prototype.forEach.call(document.querySelectorAll(".filters"), function (bar) {
    var boxes = bar.querySelectorAll("input[data-hide]");
    var epSel = bar.querySelector("select[data-ep]");
    var modeBtns = bar.querySelectorAll("button[data-mode]");
    var scope = bar.parentNode;

    function apply() {
      var hide = [];
      Array.prototype.forEach.call(boxes, function (b) { if (b.checked) hide.push(b.dataset.hide); });
      var wantEp = epSel ? epSel.value : "";
      Array.prototype.forEach.call(scope.querySelectorAll("table.qt tbody tr"), function (tr) {
        var h = hide.some(function (c) { return tr.classList.contains(c); });
        if (!h && wantEp) {
          var eps = (tr.dataset.eps || "").split(",");
          if (eps.indexOf(wantEp) < 0) h = true;
        }
        tr.classList.toggle("hidden-row", h);
      });
    }
    Array.prototype.forEach.call(boxes, function (b) { b.addEventListener("change", apply); });
    if (epSel) epSel.addEventListener("change", apply);
    Array.prototype.forEach.call(modeBtns, function (btn) {
      btn.addEventListener("click", function () {
        var mode = btn.dataset.mode;
        Array.prototype.forEach.call(modeBtns, function (b) {
          b.classList.toggle("on", b === btn);
          b.setAttribute("aria-pressed", b === btn ? "true" : "false");
        });
        Array.prototype.forEach.call(scope.querySelectorAll("td.mix-col"), function (td) {
          var raw = mode === "cache" ? td.dataset.c : td.dataset.m;
          var num = raw ? parseFloat(raw) : NaN;
          if (isFinite(num) && num < 9e8) {
            td.dataset.v = String(num);
            td.textContent = money(num);
          } else {
            td.dataset.v = "9e9";
            td.innerHTML = '<span class="muted">—</span>';
          }
        });
        Array.prototype.forEach.call(scope.querySelectorAll("th.mix-th button"), function (hb) {
          hb.textContent = mode === "cache" ? "7:2:1 缓存价" : "3:1 综合价";
        });
        // Re-sort by the mix column ascending when switching into cache mode for instant coding cost view
        if (mode === "cache") {
          Array.prototype.forEach.call(scope.querySelectorAll("table.sortable"), function (t) {
            var ths = t.querySelectorAll("thead th");
            for (var i = 0; i < ths.length; i++) {
              if (ths[i].classList.contains("mix-th")) { sortTable(t, i, true); break; }
            }
          });
        }
      });
    });
    apply();
  });

  // ---------- lazy multi-group expander (reads compact #m-groups JSON on click)
  var mgEl = document.getElementById("m-groups"), mgData = null;
  if (mgEl) {
    try { mgData = JSON.parse(mgEl.textContent); } catch (e) { mgData = null; }
  }
  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest ? e.target.closest("button.gbtn") : null;
    if (!btn || !mgData) return;
    var td = btn.closest("td");
    var existing = td.querySelector("ul.glist");
    if (existing) { existing.remove(); btn.classList.remove("open"); return; }
    var rows = mgData[btn.dataset.gdom];
    if (!rows || !rows.length) return;
    var ul = document.createElement("ul");
    ul.className = "glist";
    ul.innerHTML = rows.map(function (r) {
      var cavs = (r[3] || []).map(function (c) { return '<span class="badge caveat">' + esc(c) + "</span>"; }).join(" ");
      var dsc = r[4] ? '<span class="muted">' + esc(r[4]) + "</span>" : "";
      return "<li><code>" + esc(r[0]) + "</code> <small>" + esc(r[1]) + "</small> <b>" + esc(r[2]) + "</b> " + cavs + " " + dsc + "</li>";
    }).join("");
    td.appendChild(ul);
    btn.classList.add("open");
  });

  // ---------- outbound click telemetry hook (no-op unless Plausible/Umami is configured)
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a[data-out-domain]") : null;
    if (!a) return;
    var dom = a.dataset.outDomain || "", src = location.pathname;
    try {
      if (typeof window.plausible === "function") window.plausible("outbound", { props: { domain: dom, page: src } });
      if (window.umami && typeof window.umami.track === "function") window.umami.track("outbound", { domain: dom, page: src });
    } catch (err) {}
  });

  // ---------- copy buttons (submit page & scenario code boxes)
  function copyText(text, btn, label) {
    function done(ok) {
      if (!btn) return;
      btn.textContent = ok ? "已复制" : "请手动复制";
      btn.classList.toggle("done", ok);
      setTimeout(function () { btn.textContent = label; btn.classList.remove("done"); }, 1600);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    } else {
      var ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta); done(ok);
    }
  }
  Array.prototype.forEach.call(document.querySelectorAll("button[data-copy]"), function (b) {
    var label = b.textContent;
    b.addEventListener("click", function () {
      var el = document.getElementById(b.dataset.copy);
      if (el) copyText(el.textContent, b, label);
    });
  });

  // ---------- station page: 人民币价格视角 <-> 原始倍率与公式视角 (pure CSS class toggle, no DOM rebuild)
  Array.prototype.forEach.call(document.querySelectorAll(".filters .view-bar"), function (vb) {
    var scope = vb.parentNode.parentNode, vbtns = vb.querySelectorAll("button[data-view]");
    Array.prototype.forEach.call(vbtns, function (btn) {
      btn.addEventListener("click", function () {
        scope.classList.toggle("view-ratio", btn.dataset.view === "ratio");
        Array.prototype.forEach.call(vbtns, function (b) {
          b.classList.toggle("on", b === btn);
          b.setAttribute("aria-pressed", b === btn ? "true" : "false");
        });
      });
    });
  });

  // ---------- generic accessible tabs ([data-tabs] > [role=tablist] > button[role=tab] + [role=tabpanel])
  Array.prototype.forEach.call(document.querySelectorAll("[data-tabs]"), function (box) {
    var tabs = Array.prototype.slice.call(box.querySelectorAll(':scope > .tab-list > [role="tab"]'));
    function pick(t, focus) {
      tabs.forEach(function (b) {
        var on = b === t;
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
        b.classList.toggle("on", on);
        var p = document.getElementById(b.getAttribute("aria-controls"));
        if (p) { p.hidden = !on; if (on) { p.classList.remove("tab-in"); void p.offsetWidth; p.classList.add("tab-in"); } }
      });
      if (focus) t.focus();
    }
    tabs.forEach(function (t, i) {
      t.addEventListener("click", function () { pick(t, false); });
      t.addEventListener("keydown", function (e) {
        var n = tabs.length, j = -1;
        if (e.key === "ArrowRight") j = (i + 1) % n;
        else if (e.key === "ArrowLeft") j = (i - 1 + n) % n;
        else if (e.key === "Home") j = 0;
        else if (e.key === "End") j = n - 1;
        if (j < 0) return;
        e.preventDefault();
        pick(tabs[j], true);
      });
    });
  });

  // ---------- generic floating tooltip for [data-tip] (chart bars, pills, client chips)
  var tipEl = null, tipFor = null;
  function tipShow(el) {
    var text = el.getAttribute("data-tip");
    if (!text) return;
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.className = "tipf";
      tipEl.setAttribute("role", "tooltip");
      tipEl.id = "tipf";
      document.body.appendChild(tipEl);
    }
    tipFor = el;
    tipEl.textContent = text;
    tipEl.classList.add("on");
    var r = el.getBoundingClientRect(), tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
    var x = Math.max(6, Math.min(window.innerWidth - tw - 6, r.left + r.width / 2 - tw / 2));
    var y = r.top - th - 8;
    if (y < 6) y = r.bottom + 8;
    tipEl.style.left = x + "px";
    tipEl.style.top = y + "px";
    el.setAttribute("aria-describedby", "tipf");
  }
  function tipHide() {
    if (tipEl) tipEl.classList.remove("on");
    if (tipFor) tipFor.removeAttribute("aria-describedby");
    tipFor = null;
  }
  function tipTarget(e) { return e.target && e.target.closest ? e.target.closest("[data-tip]") : null; }
  document.addEventListener("mouseover", function (e) { var t = tipTarget(e); if (t && t !== tipFor) tipShow(t); else if (!t && tipFor) tipHide(); });
  document.addEventListener("focusin", function (e) { var t = tipTarget(e); if (t) tipShow(t); });
  document.addEventListener("focusout", tipHide);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") tipHide(); });
  window.addEventListener("scroll", tipHide, { passive: true });

  // ---------- /stations/ 「省钱 vs 靠谱」 bubble map (inline #bubble-data, no network fetch)
  var bubbleApi = null;
  (function () {
    var svg = document.getElementById("bubble-chart"), src = document.getElementById("bubble-data");
    var wrap = document.getElementById("bubble-wrap"), card = document.getElementById("bb-card");
    if (!svg || !src || !wrap || !card) return;
    var data;
    try { data = JSON.parse(src.textContent); } catch (e) { return; }
    var ix = {};
    (data.cols || []).forEach(function (c, i) { ix[c] = i; });
    var rows = (data.rows || []).filter(function (r) { return isFinite(r[ix.x]) && isFinite(r[ix.s]); });
    if (!rows.length) { wrap.hidden = true; return; }
    var NS = "http://www.w3.org/2000/svg", SPLIT_X = 50, SPLIT_Y = 65;
    var sMin = Math.min.apply(null, rows.map(function (r) { return r[ix.s]; })), sMax = Math.max.apply(null, rows.map(function (r) { return r[ix.s]; }));
    var Y0 = Math.max(0, Math.min(SPLIT_Y - 15, Math.floor((sMin - 3) / 10) * 10)), Y1 = sMax >= 78 ? 100 : 80;
    var want = [], dots = [], order = [], cur = -1, W = 800, H = 460, M = { l: 44, r: 14, t: 18, b: 40 };
    function g(r, c) { return r[ix[c]]; }
    function disc(p) {
      if (p == null) return "暂无统一比价基线";
      if (p < 10) return "不到官网 1 折";
      if (p < 95) return "约官网 " + (p / 10).toFixed(1) + " 折";
      if (p <= 105) return "约等于官网价";
      return "高于官网（约 " + (p / 100).toFixed(1) + " 倍）";
    }
    function age(d) { return d == null ? "开站时长未知" : (d >= 365 ? "开站 " + (d / 365).toFixed(1) + " 年" : "开站 " + d + " 天"); }
    function px(v) { return M.l + (W - M.l - M.r) * v / 100; }
    function py(v) { return M.t + (H - M.t - M.b) * (1 - (v - Y0) / (Y1 - Y0)); }
    function el(tag, attrs, text) {
      var n = document.createElementNS(NS, tag);
      for (var k in attrs) n.setAttribute(k, attrs[k]);
      if (text != null) n.textContent = text;
      return n;
    }
    function matches(r) {
      if (!want.length) return true;
      var toks = " " + (g(r, "f") || "") + " ";
      return want.every(function (t) { return toks.indexOf(" " + t + " ") >= 0; });
    }
    function draw() {
      var cw = wrap.clientWidth || 800;
      W = Math.max(320, Math.min(820, cw));
      H = W < 520 ? Math.round(W * 0.95) : Math.round(W * 0.56);
      M = W < 520 ? { l: 34, r: 8, t: 14, b: 36 } : { l: 44, r: 14, t: 18, b: 40 };
      svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      var small = W < 520, x0 = px(0), x1 = px(100), xm = px(SPLIT_X), y0 = py(Y0), y1 = py(Y1), ym = py(SPLIT_Y);
      var zones = [
        ["bq tr", xm, y1, x1 - xm, ym - y1, "既靠谱又便宜 · 优先看", x1 - 6, y1 + 16, "end"],
        ["bq tl", x0, y1, xm - x0, ym - y1, "稳但偏贵", x0 + 6, y1 + 16, "start"],
        ["bq br", xm, ym, x1 - xm, y0 - ym, "低价但需慎选", x1 - 6, y0 - 8, "end"],
        ["bq bl", x0, ym, xm - x0, y0 - ym, "又贵又不稳 · 避坑", x0 + 6, y0 - 8, "start"]
      ];
      zones.forEach(function (z) {
        svg.appendChild(el("rect", { "class": z[0], x: z[1], y: z[2], width: z[3], height: z[4] }));
        svg.appendChild(el("text", { "class": "bq-t " + z[0].split(" ")[1], x: z[6], y: z[7], "text-anchor": z[8] }, small ? z[5].split(" · ")[0] : z[5]));
      });
      [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100].filter(function (v) { return v >= Y0 && v <= Y1; }).forEach(function (v) {
        svg.appendChild(el("line", { "class": "bb-grid", x1: x0, x2: x1, y1: py(v), y2: py(v) }));
        svg.appendChild(el("text", { "class": "bb-tick", x: x0 - 6, y: py(v) + 4, "text-anchor": "end" }, String(v)));
      });
      svg.appendChild(el("line", { "class": "bb-split", x1: xm, x2: xm, y1: y1, y2: y0 }));
      svg.appendChild(el("line", { "class": "bb-split", x1: x0, x2: x1, y1: ym, y2: ym }));
      svg.appendChild(el("text", { "class": "bb-ax", x: x0, y: H - 10, "text-anchor": "start" }, "← 偏贵"));
      svg.appendChild(el("text", { "class": "bb-ax", x: (x0 + x1) / 2, y: H - 10, "text-anchor": "middle" }, small ? "便宜程度" : "便宜程度（比全网多少同行便宜）"));
      svg.appendChild(el("text", { "class": "bb-ax", x: x1, y: H - 10, "text-anchor": "end" }, "更省钱 →"));
      svg.appendChild(el("text", { "class": "bb-ax", x: 0, y: 0, "text-anchor": "middle", transform: "translate(11," + ((y0 + y1) / 2) + ") rotate(-90)" }, "靠谱程度 ↑"));
      var layer = el("g", { "class": "bb-dots" });
      dots = [];
      var drawOrder = rows.map(function (r, i) { return i; }).sort(function (a, b) { return g(rows[b], "m") - g(rows[a], "m"); });
      drawOrder.forEach(function (i, k) {
        var r = rows[i], m = +g(r, "m") || 0;
        var c = el("circle", {
          "class": "bd gr-" + (String(g(r, "g")).replace(/[^A-Za-z]/g, "") || "x"),
          cx: px(Math.max(0, Math.min(100, g(r, "x")))).toFixed(1), cy: py(Math.max(Y0, Math.min(Y1, g(r, "s")))).toFixed(1),
          r: Math.min(small ? 6.5 : 9, 2.6 + Math.sqrt(m) / 3.2).toFixed(1), "data-i": i
        });
        c.style.setProperty("--d", Math.min(600, k * 0.8).toFixed(0) + "ms");
        dots[i] = c;
        layer.appendChild(c);
      });
      svg.appendChild(layer);
      order = rows.map(function (r, i) { return i; }).sort(function (a, b) { return g(rows[a], "x") - g(rows[b], "x") || g(rows[b], "s") - g(rows[a], "s"); });
      applyWant();
      if (cur >= 0) mark(cur);
    }
    function applyWant() {
      var n = 0;
      dots.forEach(function (c, i) {
        if (!c) return;
        var ok = matches(rows[i]);
        c.classList.toggle("dim", !ok);
        c.classList.toggle("hit", ok && want.length > 0);
        if (ok) n++;
      });
      svg.classList.toggle("filtering", want.length > 0);
      return n;
    }
    function show(i) {
      var r = rows[i], c = dots[i];
      if (!r || !c) return;
      var up = g(r, "u");
      card.innerHTML = "<b>" + esc(g(r, "n")) + "</b><small>" + esc(g(r, "d")) + "</small>" +
        '<span class="bb-row"><span class="gr gr-' + esc(String(g(r, "g")).replace(/[^A-Za-z]/g, "") || "x") + '">' + esc(g(r, "g")) + "</span> 综合靠谱分 <b>" + Math.round(g(r, "s")) + "</b></span>" +
        '<span class="bb-row">' + esc(disc(g(r, "o"))) + " · 比全网 " + Math.round(g(r, "x")) + "% 同行便宜</span>" +
        '<span class="bb-row">' + esc(age(g(r, "a"))) + " · " + (up == null ? "在线率累积中" : "近 30 天在线 " + up + "%") + "</span>" +
        '<span class="bb-go">点击或回车打开站点档案 →</span>';
      card.hidden = false;
      var cb = c.getBoundingClientRect(), wb = wrap.getBoundingClientRect();
      var x = cb.left - wb.left + cb.width / 2, y = cb.top - wb.top;
      var w = card.offsetWidth, h = card.offsetHeight;
      var left = Math.max(4, Math.min(wb.width - w - 4, x - w / 2));
      var top = y - h - 10 < 0 ? y + cb.height + 10 : y - h - 10;
      card.style.left = left + "px";
      card.style.top = top + "px";
    }
    function mark(i) {
      dots.forEach(function (c) { if (c) c.classList.remove("on"); });
      cur = i;
      if (i < 0 || !dots[i]) { card.hidden = true; return; }
      dots[i].classList.add("on");
      if (dots[i].parentNode) dots[i].parentNode.appendChild(dots[i]);
      show(i);
    }
    function hitIndex(e) {
      var t = e.target;
      return t && t.getAttribute && t.getAttribute("data-i") != null ? +t.getAttribute("data-i") : -1;
    }
    svg.addEventListener("mousemove", function (e) { var i = hitIndex(e); if (i >= 0 && i !== cur) mark(i); });
    svg.addEventListener("mouseleave", function () { if (document.activeElement !== svg) mark(-1); });
    svg.addEventListener("click", function (e) {
      var i = hitIndex(e);
      if (i >= 0) location.href = "/site/" + encodeURIComponent(g(rows[i], "d")) + "/";
    });
    svg.addEventListener("keydown", function (e) {
      var vis = order.filter(function (i) { return matches(rows[i]); });
      if (!vis.length) return;
      var pos = vis.indexOf(cur), step = e.shiftKey ? 10 : 1, nx = null;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") nx = vis[pos < 0 ? 0 : Math.min(vis.length - 1, pos + step)];
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") nx = vis[pos < 0 ? vis.length - 1 : Math.max(0, pos - step)];
      else if (e.key === "Home") nx = vis[0];
      else if (e.key === "End") nx = vis[vis.length - 1];
      else if (e.key === "Enter" && cur >= 0) { location.href = "/site/" + encodeURIComponent(g(rows[cur], "d")) + "/"; return; }
      else if (e.key === "Escape") { mark(-1); return; }
      if (nx == null) return;
      e.preventDefault();
      mark(nx);
    });
    svg.addEventListener("blur", function () { mark(-1); });
    var rt = null;
    window.addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(draw, 150); });
    draw();
    wrap.classList.add("ready");
    bubbleApi = { filter: function (w) { want = w.slice(); var n = applyWant(); if (cur >= 0 && !matches(rows[cur])) mark(-1); return n; } };
  })();

  // ---------- /stations/ multi-facet filter (AND across pressed chips) + live counter
  var facets = document.getElementById("facets");
  if (facets) {
    var rankT = facets.parentNode.querySelector("table.rank");
    var fchips = facets.querySelectorAll("button.chip[data-f]");
    var fcount = document.getElementById("facet-count");
    var applyFacets = function () {
      var want = [];
      Array.prototype.forEach.call(fchips, function (c) { if (c.getAttribute("aria-pressed") === "true") want.push(c.dataset.f); });
      var shown = 0, total = 0;
      if (rankT) Array.prototype.forEach.call(rankT.tBodies[0].rows, function (tr) {
        var td = tr.querySelector("td[data-f]");
        if (!td) return;
        total++;
        var toks = " " + td.dataset.f + " ";
        var ok = want.every(function (t) { return toks.indexOf(" " + t + " ") >= 0; });
        tr.classList.toggle("hidden-row", !ok);
        if (ok) shown++;
      });
      if (fcount) fcount.textContent = "当前显示 " + shown + " / " + total + " 家站点" + (want.length && !shown ? "，没有同时满足的站点，试试减少条件" : "");
      facets.classList.toggle("active", want.length > 0);
      if (bubbleApi) bubbleApi.filter(want);
    };
    Array.prototype.forEach.call(fchips, function (c) {
      c.addEventListener("click", function () {
        c.setAttribute("aria-pressed", c.getAttribute("aria-pressed") === "true" ? "false" : "true");
        applyFacets();
      });
    });
    var freset = document.getElementById("facet-reset");
    if (freset) freset.addEventListener("click", function () {
      Array.prototype.forEach.call(fchips, function (c) { c.setAttribute("aria-pressed", "false"); });
      applyFacets();
    });
  }

  // ---------- /stations/ compare checkboxes -> sticky bar -> /compare/?s=dom1,dom2
  var cmpBar = document.getElementById("cmp-bar");
  if (cmpBar) {
    var CMP_MAX = 4, picked = [], pickedNames = {};
    var cmpGo = document.getElementById("cmp-go"), cmpN = document.getElementById("cmp-n");
    var cmpNames = document.getElementById("cmp-names"), cmpMsg = document.getElementById("cmp-msg");
    var updCmp = function () {
      cmpN.textContent = String(picked.length);
      cmpNames.textContent = picked.map(function (d) { return pickedNames[d] || d; }).join("、");
      cmpBar.hidden = picked.length === 0;
      if (picked.length >= 2) {
        cmpGo.href = "/compare/?s=" + picked.map(encodeURIComponent).join(",");
        cmpGo.removeAttribute("aria-disabled");
        cmpMsg.textContent = "";
      } else {
        cmpGo.href = "/compare/";
        cmpGo.setAttribute("aria-disabled", "true");
        cmpMsg.textContent = picked.length ? "再选 1 家即可对比" : "";
      }
    };
    var cmpBoxes = document.querySelectorAll("input[data-cmp]");
    Array.prototype.forEach.call(cmpBoxes, function (cb) {
      if (cb.checked && picked.length < CMP_MAX) { picked.push(cb.dataset.cmp); pickedNames[cb.dataset.cmp] = cb.dataset.name; }
      else cb.checked = false;
      cb.addEventListener("change", function () {
        var d = cb.dataset.cmp;
        if (cb.checked) {
          if (picked.length >= CMP_MAX) {
            cb.checked = false;
            cmpMsg.textContent = "最多并排对比 4 家，请先取消一家";
            cmpBar.classList.remove("shake"); void cmpBar.offsetWidth; cmpBar.classList.add("shake");
            return;
          }
          picked.push(d); pickedNames[d] = cb.dataset.name;
        } else {
          picked = picked.filter(function (x) { return x !== d; });
        }
        updCmp();
      });
    });
    cmpGo.addEventListener("click", function (e) { if (cmpGo.getAttribute("aria-disabled")) e.preventDefault(); });
    document.getElementById("cmp-clear").addEventListener("click", function () {
      Array.prototype.forEach.call(cmpBoxes, function (cb) { cb.checked = false; });
      picked = []; updCmp();
    });
    updCmp();
  }

  // ---------- submit form generator (local only, supports Email + GitHub Issue dual channel)
  var form = document.getElementById("sub-form");
  if (form) {
    var out = document.getElementById("sub-out"), err = document.getElementById("sub-err");
    var mail = document.getElementById("sub-mail"), gh = document.getElementById("sub-gh"), copyBtn = document.getElementById("sub-copy");
    var status = document.getElementById("sub-status");
    var copyLabel = copyBtn.textContent;
    function normDomain(s) {
      s = (s || "").trim().toLowerCase().replace(/^https?:\/\//, "").split(/[\/?#:]/)[0].replace(/^www\./, "");
      return /^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$/.test(s) ? s : "";
    }
    function fail(msg, field) {
      err.textContent = msg; err.hidden = false;
      if (field) { field.setAttribute("aria-invalid", "true"); field.focus(); }
    }
    form.addEventListener("input", function (e) {
      if (e.target.removeAttribute) e.target.removeAttribute("aria-invalid");
      err.hidden = true;
    });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var f = form.elements, d = normDomain(f.domain.value), p = f.pricing.value.trim(), r = f.ratio.value.trim();
      if (!d) return fail("请填写有效的站点域名，例如 example.com", f.domain);
      if (p && !/^https?:\/\/[^\s\/]+\.[^\s\/]+\//i.test(p + "/")) return fail("价格接口地址需以 http:// 或 https:// 开头", f.pricing);
      if (r && !(parseFloat(r) > 0 && isFinite(r))) return fail("充值比例应为正数，例如 1 或 7.2", f.ratio);
      var kind = f.kind.value;
      var lines = [
        "### 请求类型", kind, "", "### 站点域名", d, "",
        "### 价格接口地址", p || ("https://" + d + "/api/pricing（默认）"), "",
        "### 充值比例（元 / 1 美元额度）", r || "未填写（以 /api/status 公开设置为准）", "",
        "### 备注与证据", f.note.value.trim() || "无", "",
        "---", "提交时间：" + new Date().toISOString(), "来源页面：" + location.href
      ];
      var text = lines.join("\n"), subject = "[" + kind + "] " + d;
      var email = form.dataset.email || "", ghBase = form.dataset.gh || "";
      out.textContent = text; out.hidden = false;
      out.classList.remove("show"); void out.offsetWidth; out.classList.add("show");
      copyBtn.disabled = false;
      if (email) {
        mail.href = "mailto:" + email + "?subject=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(text);
        mail.removeAttribute("aria-disabled"); mail.removeAttribute("tabindex");
      }
      if (gh && ghBase) {
        var sep = ghBase.indexOf("?") >= 0 ? "&" : "?";
        gh.href = ghBase + sep + "title=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(text);
        gh.removeAttribute("aria-disabled"); gh.removeAttribute("tabindex");
      }
      status.textContent = email ? "已生成，可邮件发送、发 GitHub Issue 或复制" : "已生成，请复制模板";
    });
    mail.addEventListener("click", function (e) { if (mail.getAttribute("aria-disabled")) e.preventDefault(); });
    if (gh) gh.addEventListener("click", function (e) { if (gh.getAttribute("aria-disabled")) e.preventDefault(); });
    copyBtn.addEventListener("click", function () { copyText(out.textContent, copyBtn, copyLabel); });
  }
})();
