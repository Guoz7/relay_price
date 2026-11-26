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
    if (!loading) loading = fetch("/data/search.json").then(function (r) { return r.json(); }).then(function (d) { idx = d; return d; });
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
        // >= 9e8 means "no RMB price" (USD-only or missing): always keep at the bottom
        var ma = !(fa < 9e8), mb = !(fb < 9e8);
        if (ma !== mb) return ma ? 1 : -1;
        if (ma && mb) return fa - fb;
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
        var asc = th.getAttribute("aria-sort") !== "ascending";
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
