"""Interactive TCO / Hybrid-Routing / Primary+Backup Relay Calculator (/calc/) + /data/calc.json.

Addresses the full buyer decision funnel:
1. Cross-model monthly cost comparison (Official API vs. Best Reliable Relay vs. Hybrid Routing savings)
2. Custom workload sliders (daily requests, input tokens, output tokens, prompt cache hit rate)
3. Automatic Primary + Backup station pairing (2 distinct independent domains from Standard lane)
4. Privacy & risk boundary checklist (high-sensitivity direct vs. low-risk relay small-topup trial)
"""

from __future__ import annotations

import json
from html import escape as E
from pathlib import Path
from typing import Any

import model_specs as MS
import pricing as P
import scoring as S

CALC_MODELS = [
    ("deepseek-v4.1-flash", "高性价比主力 · 轻量/批量/常规对话"),
    ("deepseek-v4-pro", "强推理性价比 · 代码与复杂分析"),
    ("claude-sonnet-5-5", "编程主力 · Claude Code / Cursor"),
    ("claude-opus-5-5", "顶级复杂编码与架构决策"),
    ("claude-fable-5-2", "旗舰深度写作与长程推理"),
    ("gpt-6.1-sol", "均衡通用与 Codex 高频任务"),
    ("gpt-6-astra", "OpenAI 顶级旗舰推理"),
    ("gemini-4-carbon", "1M 长上下文与多模态理解"),
    ("gpt-5.4", "成熟稳定通用主力"),
]


def build_calc(
    site: Any,
    model_pages: dict[str, dict[str, Any]],
    stations: dict[str, dict[str, Any]],
    specs: dict[str, dict[str, Any]],
    helpers: dict[str, Any],
    dist_dir: Path,
) -> None:
    cfg, now = site.cfg, site.now
    usd_rate = float(cfg.get("usd_cny_rate", 7.25))
    rank_key = helpers["rank_key"]
    is_reliable_rec = helpers["is_reliable_rec"]

    payload_models: list[dict[str, Any]] = []
    for canon, role in CALC_MODELS:
        mp = model_pages.get(canon)
        if not mp:
            continue
        std_rows = [q for q in mp["rows"] if q.kind == "token" and q.lane == "standard" and q.input_cny is not None]
        rec_rows = sorted(
            [q for q in std_rows if is_reliable_rec(q, stations[q.domain], cfg, now)],
            key=lambda q: rank_key(q, stations, cfg, now),
        )
        if len(rec_rows) < 2:
            rec_rows = sorted(
                [q for q in std_rows if not q.anomaly and not helpers["is_stale"](stations[q.domain], cfg, now)],
                key=lambda q: rank_key(q, stations, cfg, now),
            )
        picks = []
        seen_dom: set[str] = set()
        for q in rec_rows:
            if q.domain in seen_dom:
                continue
            seen_dom.add(q.domain)
            st = stations[q.domain]
            sc: S.StationScore = st["score"]
            picks.append({
                "dom": q.domain,
                "name": st["display_name"],
                "grade": S.grade(q.score),
                "score": q.score,
                "stage": sc.stage,
                "group": q.group or "default",
                "in": round(q.input_cny, 5),
                "out": round(q.output_cny if q.output_cny is not None else q.input_cny, 5),
                "cache": round(q.cache_cny, 5) if q.cache_cny is not None else None,
                "eps": P.endpoint_tags(q.endpoints)[:4],
            })
            if len(picks) == 5:
                break
        if not picks:
            continue
        sp = specs.get(canon) or {}
        off_in = round(sp["in_usd"] * usd_rate, 4) if isinstance(sp.get("in_usd"), (int, float)) else None
        off_out = round(sp["out_usd"] * usd_rate, 4) if isinstance(sp.get("out_usd"), (int, float)) else None
        off_cache = round(sp["cache_usd"] * usd_rate, 4) if isinstance(sp.get("cache_usd"), (int, float)) else None
        payload_models.append({
            "id": canon,
            "slug": mp["slug"],
            "role": role,
            "n": mp["n"],
            "ctx": MS.fmt_ctx(sp.get("context")),
            "off_in": off_in,
            "off_out": off_out,
            "off_cache": off_cache,
            "long_tier": bool(sp.get("long_tier")),
            "picks": picks,
        })

    calc_json = dist_dir / "data" / "calc.json"
    calc_json.parent.mkdir(parents=True, exist_ok=True)
    calc_json.write_text(
        json.dumps({"usd_cny_rate": usd_rate, "models": payload_models}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    body = f"""
<section class="hero sm">
  <p class="eyebrow">场景算账 · 跨模型对比 · 主备容灾</p>
  <h1>AI API 月度成本算账与主备选站助手</h1>
  <p class="lead">选 API 不该只看“输入每百万 token 最低价”。按你的真实调用量、上下文长度与缓存命中率，一次算清<b>官方直连 vs 常规中转 vs 大小模型混合路由</b>的月度总账，并自动给出「1 主 + 1 备」站点搭配。</p>
</section>

<section class="calc-box" id="calc-app">
  <div class="calc-presets" role="group" aria-label="常见业务场景预设">
    <button type="button" class="btn sm primary" data-preset="code">Claude Code / Agent 编程</button>
    <button type="button" class="btn sm" data-preset="cs">企业客服 / 知识库问答</button>
    <button type="button" class="btn sm" data-preset="doc">长文研报 / 文档总结</button>
    <button type="button" class="btn sm" data-preset="batch">批量翻译 / 结构化提取</button>
  </div>
  <div class="calc-grid">
    <label>每日请求数<input id="c-req" type="number" min="1" max="1000000" value="150"></label>
    <label>单次平均输入 Token<input id="c-in" type="number" min="50" max="500000" value="8000"></label>
    <label>单次平均输出 Token<input id="c-out" type="number" min="20" max="100000" value="800"></label>
    <label>Prompt Cache 命中率（%）<input id="c-cache" type="number" min="0" max="95" value="75"></label>
    <label>主模型（用于混合路由对比）<select id="c-heavy"></select></label>
    <label>低成本分流模型（80% 简单任务）<select id="c-light"></select></label>
  </div>
  <div class="kpis" id="c-kpis">
    <div><b id="k-tok">—</b><span>月均总 Token（输入 + 输出）</span></div>
    <div><b id="k-heavy-off">—</b><span>主模型官方直连估算 / 月</span></div>
    <div><b id="k-heavy-rel">—</b><span>主模型常规中转最优 / 月</span></div>
    <div><b id="k-hybrid">—</b><span>80% 轻量 + 20% 主模型混合路由 / 月</span></div>
  </div>
  <p class="muted" id="c-note">正在加载实时模型报价数据…</p>
</section>

<section>
  <h2>一、跨模型月度总账横向对比 <small>官网直连（按 1 USD ≈ ¥{usd_rate:g} 换算）vs 常规按量中转最优站 · 自动配对 2 家独立域名主备站</small></h2>
  <div class="tbl-wrap">
    <table class="qt" id="c-table">
      <thead><tr>
        <th>模型 / 定位</th><th>上下文</th><th class="n">官网直连估算 / 月</th><th class="n">常规中转最优 / 月</th><th class="n">约官网折数</th><th>首选主站</th><th>推荐备用站</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
</section>

<section class="prose">
  <h2>二、选站之前别漏掉这 4 条隐性边界</h2>
  <ul>
    <li><b>不要为了省 20% 牺牲准确率（便宜 Token 陷阱）</b>：如果低价模型导致返工、工具调用失败或人工兜底，隐性人力成本远高于省下的 API 费。建议按 <b>80% 简单任务走 DeepSeek V4.1 / GPT-6.1 Sol + 20% 复杂推理走 Claude Sonnet/Opus 5.5</b> 混合路由。</li>
    <li><b>业务数据敏感度分级</b>：涉及金融、医疗、客户隐私或严密商业机密的调用，<b>优先官方直连或签有明确零留存（ZDR）协议的云厂商</b>；个人开发、学习与非敏业务走中转时，务必控制单次充值金额。</li>
    <li><b>永远准备「1 主 + 1 备」两家独立站点</b>：中转站偶发上游限流或 429 很常见，用 LiteLLM / New API / 客户端多配置做主备切换，把单点故障影响降到最低。</li>
    <li><b>先小额实测三件事再续费</b>：① 连续发两轮同前缀请求看缓存是否真命中；② 核对后台单次扣费与公开标价是否一致；③ 用你的真实代码/业务 Prompt 测工具调用与响应速度。</li>
  </ul>
</section>
<script>
(function(){{
  var PRESETS = {{
    code:  {{req:150,  inp:8000,  out:800,  cache:75, heavy:'claude-sonnet-5-5', light:'deepseek-v4.1-flash'}},
    cs:    {{req:2000, inp:2000,  out:300,  cache:40, heavy:'gpt-5.4',           light:'deepseek-v4.1-flash'}},
    doc:   {{req:80,   inp:30000, out:2000, cache:20, heavy:'gemini-4-carbon',   light:'deepseek-v4-pro'}},
    batch: {{req:1000, inp:1500,  out:1500, cache:0,  heavy:'deepseek-v4-pro',   light:'deepseek-v4.1-flash'}}
  }};
  var DATA = null;
  function fmtY(v){{ if(v==null||!isFinite(v)) return '—'; return v>=100 ? '¥'+Math.round(v).toLocaleString() : '¥'+v.toFixed(v>=10?1:2); }}
  function monthCost(inP, outP, cacheP, req, inp, out, cr){{
    if(inP==null) return null;
    var mReq = req * 30;
    var hitIn = inp * (cr/100), missIn = inp - hitIn;
    var effCache = cacheP != null ? cacheP : inP;
    return mReq * (hitIn * effCache + missIn * inP + out * (outP!=null?outP:inP)) / 1e6;
  }}
  function esc(s){{ return String(s||'').replace(/[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}}); }}
  function render(){{
    if(!DATA) return;
    var req = Math.max(1, +document.getElementById('c-req').value || 0);
    var inp = Math.max(1, +document.getElementById('c-in').value || 0);
    var out = Math.max(1, +document.getElementById('c-out').value || 0);
    var cr  = Math.min(95, Math.max(0, +document.getElementById('c-cache').value || 0));
    var heavyId = document.getElementById('c-heavy').value;
    var lightId = document.getElementById('c-light').value;
    var totM = req * 30 * (inp + out) / 1e6;
    document.getElementById('k-tok').textContent = totM >= 100 ? Math.round(totM)+'M' : totM.toFixed(1)+'M';

    var byId = {{}};
    var rows = DATA.models.map(function(m){{
      byId[m.id] = m;
      var p1 = m.picks[0], p2 = m.picks[1] || null;
      var offC = monthCost(m.off_in, m.off_out, m.off_cache, req, inp, out, cr);
      var relC = p1 ? monthCost(p1.in, p1.out, p1.cache, req, inp, out, cr) : null;
      var disc = (offC && relC) ? (relC / offC) : null;
      var discHtml = disc != null ? '<span class="badge '+((disc<0.15||disc>1.05)?'warn':'ok')+'">'+(disc*100).toFixed(0)+'%（'+(disc*10).toFixed(1)+'折）</span>' : '<span class="muted">—</span>';
      function stCell(p){{
        if(!p) return '<span class="muted">—</span>';
        var mc = monthCost(p.in, p.out, p.cache, req, inp, out, cr);
        return '<a href="/site/'+esc(p.dom)+'/">'+esc(p.name)+'</a> <b class="gr gr-'+esc(p.grade)+'">'+esc(p.grade)+'</b><small>'+esc(p.group.slice(0,18))+' · '+fmtY(mc)+'/月</small>';
      }}
      var lt = m.long_tier ? ' <span class="badge warn" title="超过 200K/272K 长上下文官方按高档计费">长文加价</span>' : '';
      return '<tr><td class="mn"><a href="/model/'+esc(m.slug)+'/"><code>'+esc(m.id)+'</code></a><small>'+esc(m.role)+'</small></td>'
        +'<td>'+(m.ctx?'<span class="badge ep">'+esc(m.ctx)+'</span>':'—')+lt+'</td>'
        +'<td class="n">'+fmtY(offC)+'</td>'
        +'<td class="n hl">'+fmtY(relC)+'</td>'
        +'<td class="n">'+discHtml+'</td>'
        +'<td class="st">'+stCell(p1)+'</td>'
        +'<td class="st">'+stCell(p2)+'</td></tr>';
    }});
    document.querySelector('#c-table tbody').innerHTML = rows.join('');

    var hm = byId[heavyId] || DATA.models[0], lm = byId[lightId] || DATA.models[0];
    if(hm && lm){{
      var hOff = monthCost(hm.off_in, hm.off_out, hm.off_cache, req, inp, out, cr);
      var hRel = hm.picks[0] ? monthCost(hm.picks[0].in, hm.picks[0].out, hm.picks[0].cache, req, inp, out, cr) : null;
      var lRel = lm.picks[0] ? monthCost(lm.picks[0].in, lm.picks[0].out, lm.picks[0].cache, req, inp, out, cr) : null;
      var hyb  = (hRel!=null && lRel!=null) ? (0.2 * hRel + 0.8 * lRel) : null;
      document.getElementById('k-heavy-off').textContent = fmtY(hOff);
      document.getElementById('k-heavy-rel').textContent = fmtY(hRel);
      var savePct = (hRel && hyb != null) ? Math.round((1-hyb/hRel)*100) : null;
      var saveTxt = savePct == null ? '' : (savePct > 0 ? '（省 '+savePct+'%）' : (savePct === 0 ? '（持平）' : '（当前主模型中转更便宜）'));
      document.getElementById('k-hybrid').textContent = fmtY(hyb) + saveTxt;
      document.getElementById('c-note').textContent = '模型官网参考价与上下文规格同步自开源仓库 models.dev / LiteLLM；中转价取常规按量推荐区（已剔除 Kiro/逆向/特惠与未核实极端低价）。';
    }}
  }}
  fetch('/data/calc.json').then(function(r){{return r.json();}}).then(function(d){{
    DATA = d;
    var hs = document.getElementById('c-heavy'), ls = document.getElementById('c-light');
    d.models.forEach(function(m){{
      hs.add(new Option(m.id, m.id));
      ls.add(new Option(m.id, m.id));
    }});
    hs.value = d.models.some(function(m){{return m.id==='claude-sonnet-5-5';}}) ? 'claude-sonnet-5-5' : d.models[0].id;
    ls.value = d.models.some(function(m){{return m.id==='deepseek-v4.1-flash';}}) ? 'deepseek-v4.1-flash' : d.models[0].id;
    render();
  }});
  ['c-req','c-in','c-out','c-cache','c-heavy','c-light'].forEach(function(id){{
    var el = document.getElementById(id);
    if(el) el.addEventListener('input', render);
    if(el) el.addEventListener('change', render);
  }});
  Array.prototype.forEach.call(document.querySelectorAll('button[data-preset]'), function(b){{
    b.addEventListener('click', function(){{
      var p = PRESETS[b.dataset.preset]; if(!p) return;
      Array.prototype.forEach.call(document.querySelectorAll('button[data-preset]'), function(x){{ x.classList.toggle('primary', x===b); }});
      document.getElementById('c-req').value = p.req;
      document.getElementById('c-in').value = p.inp;
      document.getElementById('c-out').value = p.out;
      document.getElementById('c-cache').value = p.cache;
      if(DATA){{
        if(DATA.models.some(function(m){{return m.id===p.heavy;}})) document.getElementById('c-heavy').value = p.heavy;
        if(DATA.models.some(function(m){{return m.id===p.light;}})) document.getElementById('c-light').value = p.light;
      }}
      render();
    }});
  }});
}})();
</script>"""
    faq = [
        {"@type": "Question", "name": "如何估算 Claude Code 或 AI Agent 的真实月度 API 费用？",
         "acceptedAnswer": {"@type": "Answer", "text": "编程与 Agent 场景中 Prompt Cache 读通常占 70%–85%，按（缓存命中输入×缓存价 + 未命中输入×输入价 + 输出×输出价）× 月请求次数估算，与仅看输入单价差距可达 3–5 倍。"}},
        {"@type": "Question", "name": "什么是大模型混合路由省钱方案？",
         "acceptedAnswer": {"@type": "Answer", "text": "将 80% 简单查询、检索与格式化任务分发给 DeepSeek V4.1 Flash 等高性价比模型，仅将 20% 复杂推理与编码任务路由到 Claude Sonnet/Opus 5.5 或 GPT-6，综合成本通常可降低 60% 以上。"}},
    ]
    site.page(
        "/calc/",
        "AI API 月度成本算账器与跨模型主备选站助手",
        "按你的每日调用量、上下文长度与 Prompt Cache 命中率，对比官方直连、常规中转与大小模型混合路由的真实月成本，并推荐 1 主 + 1 备站点组合。",
        body,
        crumbs=[("月度算账器", "/calc/")],
        ld=[{"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": faq}],
        priority=0.9,
    )
