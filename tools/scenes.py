"""Scenario / client landing pages (/scenes/...) targeting high-intent buyer searches.

Built from empirical buyer corpus + SERP research (Experiment 3):
1. Claude Code (/scenes/claude-code/) - Anthropic protocol + Prompt Cache 7:2:1 cost
2. DeepSeek (/scenes/deepseek/) - V4.1 / V4 / R1 caching & peak/off-peak tiers
3. Codex / GPT (/scenes/codex-gpt/) - OpenAI Responses API & GPT-6 series
4. Gemini / Image & Video (/scenes/gemini-image/) - Gemini native + image/video per-call & token
"""

from __future__ import annotations

from html import escape as E
from typing import Any

import pricing as P
import scoring as S

SCENES: list[dict[str, Any]] = [
    {
        "slug": "claude-code",
        "name": "Claude Code 编程中转站推荐",
        "short": "Claude Code",
        "tagline": "聚焦支持原生 Anthropic 协议与 Prompt Cache 的 Claude 常规线路，默认剥离低价 Kiro/逆向分组。",
        "desc": "Claude Code 用哪家 API 中转站靠谱？按 Anthropic 原生协议、7:2:1 编程缓存成本与站点分层信誉对比 Claude Sonnet 5.5 / Opus 5.5 / Fable 5.2。",
        "req_endpoints": ["anthropic"],
        "models": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2", "claude-haiku-5-5", "claude-sonnet-5", "claude-opus-5"],
        "prefer_cache": True,
        "snippet_title": "Claude Code 接入环境变量（3 行示例）",
        "snippet": (
            'export ANTHROPIC_BASE_URL="https://<站点域名>"\n'
            'export ANTHROPIC_AUTH_TOKEN="sk-你的密钥"\n'
            'claude  # 建议先小额充值并观察首轮与第二轮缓存命中扣费'
        ),
        "tips": [
            "Claude Code 高频依赖 <b>Prompt Cache（缓存读通常占 70%+）</b>，务必点表格上方切换为「编程缓存 7:2:1」视角对比真实成本。",
            "务必确认所选站点与模型带有 <code>Anthropic</code> 协议标签；只支持 OpenAI 转发的线路常出现工具调用或 thinking 签名丢失。",
            "同一站点往往同时挂有高稳常规组与 0.1× Kiro/逆向特惠组（中位价差达 6×），请点开行内「全部分组」核对具体分组名后再建 Key。",
        ],
    },
    {
        "slug": "deepseek",
        "name": "DeepSeek API 中转站推荐与价格对比",
        "short": "DeepSeek",
        "tagline": "对比 DeepSeek V4.1 / V4 / Chat / Reasoner 的人民币实付价、缓存读价与分时高低峰提醒。",
        "desc": "DeepSeek API 中转站哪家便宜又稳定？对比各站 DeepSeek V4.1 Flash / V4 Pro / R1 人民币输入输出价、缓存命中价与站点信誉评分。",
        "req_endpoints": [],
        "models": ["deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner", "deepseek-r1", "deepseek-v3.2"],
        "prefer_cache": False,
        "snippet_title": "OpenAI SDK / Cherry Studio 接入示例",
        "snippet": (
            'OPENAI_BASE_URL="https://<站点域名>/v1"\n'
            'OPENAI_API_KEY="sk-你的密钥"\n'
            '# 模型名建议填 deepseek-v4.1-flash 或 deepseek-chat'
        ),
        "tips": [
            "部分站点对 DeepSeek 启用 <b>分时高低峰计价</b>（夜间低价、白天高峰涨价），表中带「分时计价 / 高峰档」标签的行请按白天高峰价核算预算。",
            "低至中位价 0.25× 以下且未实测的极低价分组，可能存在并发限流或不稳定风险，常规推荐已自动排除此类异常值。",
            "若是纯 Chat 带工具调用场景，遇到「返回空内容但 status=stop」可优先选支持 <code>OpenAI Responses</code> 协议的线路。",
        ],
    },
    {
        "slug": "codex-gpt",
        "name": "Codex / GPT-6 API 中转站推荐",
        "short": "Codex / GPT",
        "tagline": "聚焦支持 OpenAI Responses 与 GPT-6 / Codex 系列的常规按量中转站，区分常规组与特惠组。",
        "card": "聚焦能直接接 Codex 等新版客户端、在卖 GPT-6 系列的常规按量中转站，区分常规组与特惠组。",
        "desc": "Codex CLI 与 GPT-6 API 中转站哪家稳定？按 OpenAI Responses 协议支持、缓存价与分层信誉对比 GPT-6 Astra / GPT-6.1 Sol / Codex。",
        "req_endpoints": ["openai-response", "openai"],
        "models": ["gpt-6-astra", "gpt-6.1-sol", "gpt-6.2-astra", "gpt-6.2-sol", "gpt-5.4", "o3"],
        "prefer_cache": True,
        "snippet_title": "Codex CLI / OpenAI 兼容配置示例",
        "snippet": (
            'export OPENAI_BASE_URL="https://<站点域名>/v1"\n'
            'export OPENAI_API_KEY="sk-你的密钥"\n'
            '# Codex CLI 建议确认站点该型号具备 OpenAI Responses 协议标签'
        ),
        "tips": [
            "Codex CLI 新版默认走 <b>Responses API（<code>/v1/responses</code>）</b>，选站时请优先勾选 <code>OpenAI Responses</code> 协议标签。",
            "GPT-6 系列同站不同分组价差中位数高达 <b>5×</b>，购买前请确认所用 Key 绑定的分组倍率与站点原文说明一致。",
            "未接入真金白银探针实测前，所有中转站最高封顶 B 级（74 分）；大额充值前请先小额验证并发与工具调用。",
        ],
    },
    {
        "slug": "gemini-image",
        "name": "Gemini 与生图 / 视频 API 中转推荐",
        "short": "Gemini / 生图视频",
        "tagline": "聚合支持 Gemini 原生协议及按次/按 Token 生图、视频模型的站点与公开透明单价。",
        "desc": "Gemini 4 Carbon、GPT Image 生图与视频 API 中转哪家透明？对比原生 Gemini 协议、按次生图人民币单价与站点分层评分。",
        "req_endpoints": ["gemini", "image", "video"],
        "models": ["gemini-4-carbon", "gemini-3-pro-preview", "gemini-3-flash", "gpt-image-2.6-flare", "gpt-image-2.6-sunburst", "gpt-image-2", "minimax-h3"],
        "prefer_cache": False,
        "snippet_title": "Gemini / 图像接口调用提示",
        "snippet": (
            '# Gemini 原生或 OpenAI 兼容端点视客户端而定：\n'
            'BASE_URL="https://<站点域名>/v1"\n'
            'API_KEY="sk-你的密钥"'
        ),
        "tips": [
            "生图与视频模型常按 <b>每次调用（¥/张 或 ¥/次）</b> 计费，也会随分辨率或秒数分档；下单前请看行内「多档计价」提示。",
            "使用 Gemini 原生 SDK（含长上下文缓存或搜索接地）时，请优先看带有 <code>Gemini</code> 协议标签的站点。",
        ],
    },
]


def _pick_scene_models(scene: dict[str, Any], model_pages: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for m in scene["models"]:
        if m in model_pages and m not in seen:
            found.append((m, model_pages[m]))
            seen.add(m)
    return found


def build_scenes(site: Any, model_pages: dict[str, dict[str, Any]], stations: dict[str, dict[str, Any]], helpers: dict[str, Any]) -> None:
    """Generate /scenes/ overview and all 4 scenario landing pages."""
    money = helpers["money"]
    grade_badge = helpers["grade_badge"]
    rank_key = helpers["rank_key"]
    short_price = helpers["short_price"]
    cache_mix_cny = helpers["cache_mix_cny"]
    is_reliable_rec = helpers["is_reliable_rec"]
    cfg, now = site.cfg, site.now

    scene_cards = []
    for sc in SCENES:
        ms = _pick_scene_models(sc, model_pages)
        req_eps = set(sc["req_endpoints"])

        # Collect flagship model sections for this scene
        sections = []
        matched_stations: set[str] = set()
        for canon, mp in ms:
            std_rows = [q for q in mp["rows"] if q.lane == "standard"]
            if req_eps:
                ep_rows = [q for q in std_rows if req_eps & set(P.endpoint_tags(q.endpoints))]
            else:
                ep_rows = std_rows
            use_rows = ep_rows if len(ep_rows) >= 3 else std_rows
            use_rows = sorted(use_rows, key=lambda q: rank_key(q, stations, cfg, now))
            rec_rows = [q for q in use_rows if is_reliable_rec(q, stations[q.domain], cfg, now)]
            top_rows = (rec_rows or use_rows)[:8]
            for q in top_rows:
                matched_stations.add(q.domain)

            trs = []
            for q in top_rows:
                st = stations[q.domain]
                sc_obj: S.StationScore = st["score"]
                eps = [P.ENDPOINT_LABELS.get(t, t) for t in P.endpoint_tags(q.endpoints)[:3]]
                ep_html = "".join(f'<span class="badge ep">{E(x)}</span>' for x in eps)
                cmix = cache_mix_cny(q, cfg) if q.kind == "token" else None
                if q.kind == "token":
                    p_html = f"{money(q.input_cny)} / {money(q.output_cny)}"
                    c_html = money(q.cache_cny) if q.cache_cny is not None else '<span class="muted">—</span>'
                    mix_html = money(cmix) if cmix is not None else '<span class="muted">—</span>'
                else:
                    p_html = f"{money(q.call_cny)}/次"
                    c_html = '<span class="muted">—</span>'
                    mix_html = p_html
                st_tag = '<span class="badge ok">Stage 2 实测</span>' if sc_obj.stage == 2 else '<span class="badge">Stage 1</span>'
                trs.append(
                    f'<tr><td class="st"><a href="/site/{E(q.domain)}/">{E(st["display_name"])}</a>'
                    f'<small>{E(q.domain)} · {st_tag}</small></td>'
                    f'<td class="sc">{grade_badge(q.score)} <b>{q.score:.0f}</b><small>站 {sc_obj.total:.0f}</small></td>'
                    f'<td class="n hl">{p_html}</td>'
                    f'<td class="n">{c_html}</td>'
                    f'<td class="n">{mix_html}</td>'
                    f'<td class="g"><span>{E((q.group or "default")[:24])}</span> <small>{"" if q.group_ratio is None else f"×{q.group_ratio:g}"}</small>'
                    f'{"<div class=bd>" + ep_html + "</div>" if ep_html else ""}</td></tr>'
                )
            if not trs:
                continue
            ep_note = (f"已筛选支持 {' / '.join(P.ENDPOINT_LABELS.get(e, e) for e in sc['req_endpoints'])} 协议" if (req_eps and len(ep_rows) >= 3) else "展示常规按量分组")
            sections.append(
                f'<section class="scene-block">'
                f'<h2><code>{E(canon)}</code> <small>{ep_note} · <a href="/model/{mp["slug"]}/">查看全部 {mp["n"]} 家站与全部分组 →</a></small></h2>'
                f'<div class="tbl-wrap"><table class="qt"><thead><tr>'
                f'<th>站点</th><th>综合评分</th><th class="n">输入 / 输出（¥/1M）</th><th class="n">缓存读</th><th class="n">7:2:1 缓存混合</th><th>推荐分组 / 协议</th>'
                f'</tr></thead><tbody>{"".join(trs)}</tbody></table></div></section>'
            )

        tips_html = "".join(f"<li>{t}</li>" for t in sc["tips"])
        snip_id = f"snip-{sc['slug']}"
        faq = [
            {
                "@type": "Question",
                "name": f"{sc['short']} 用哪家 API 中转站比较稳？",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": f"本页只从支持对应协议的常规按量分组中筛选，已剥离自称 Kiro/Cursor/逆向/特惠及未核实极端低价分组。未实测站点统一封顶 B 级（74 分），大额充值前请先小额测试。",
                },
            },
            {
                "@type": "Question",
                "name": "7:2:1 缓存混合价是什么意思？",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": "编程与 Agent 场景下缓存读通常占 70% 左右，因此按 70% 缓存读 + 20% 未缓存输入 + 10% 输出估算每百万 token 综合成本。",
                },
            },
        ]
        body = f"""
<section class="hero sm">
  <p class="eyebrow">场景选站 · {E(sc['short'])}</p>
  <h1>{E(sc['name'])}</h1>
  <p class="lead">{E(sc['tagline'])}</p>
  <div class="kpis">
    <div><b>{len(ms)}</b><span>覆盖主力型号</span></div>
    <div><b>{len(matched_stations)}</b><span>精选候选站点</span></div>
    <div><b>Stage 1 / 2</b><span>未实测最高封顶 B 级</span></div>
  </div>
</section>
<section class="note">
  <h2>本场景避坑要点</h2>
  <ul>{tips_html}</ul>
  <h3>{E(sc['snippet_title'])}</h3>
  <div class="codebox"><button class="btn sm copy" type="button" data-copy="{snip_id}">复制</button><pre id="{snip_id}"><code>{E(sc['snippet'])}</code></pre></div>
</section>
{''.join(sections)}
<section class="note">
  <p class="muted">所有价格来自站点公开只读接口并按公开充值比例换算，点击具体型号可展开每个站点的全部分组与特惠/逆向对照区。发现错误请到 <a href="/submit/">提交纠错</a>。</p>
</section>"""
        site.page(
            f"/scenes/{sc['slug']}/",
            sc["name"],
            sc["desc"],
            body,
            crumbs=[("场景选站", "/scenes/"), (sc["short"], f"/scenes/{sc['slug']}/")],
            ld=[{"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": faq}],
            priority=0.9,
        )
        m_tags = " ".join(f'<code>{E(c)}</code>' for c, _ in ms[:3])
        scene_cards.append(
            f'<a class="card scene-card" href="/scenes/{sc["slug"]}/">'
            f'<b>{E(sc["name"])}</b><p class="muted">{E(sc["tagline"])}</p>'
            f'<div class="meta">{m_tags}</div></a>'
        )

    idx_body = f"""
<section class="hero sm">
  <p class="eyebrow">按使用场景选站</p>
  <h1>AI API 中转站场景导购</h1>
  <p class="lead">不同客户端对协议（Anthropic / OpenAI Responses / Gemini）与 Prompt Cache 的要求截然不同。按你的工具直接看对应场景的常规推荐与真实缓存成本。</p>
</section>
<div class="cards scene-cards">{''.join(scene_cards)}</div>"""
    site.page(
        "/scenes/",
        "AI API 中转站场景选站：Claude Code / DeepSeek / Codex / Gemini",
        "按使用场景筛选 AI API 中转站：Claude Code 原生协议与缓存、DeepSeek 高性价比与分时档、Codex Responses 接口、Gemini 与生图视频。",
        idx_body,
        crumbs=[("场景选站", "/scenes/")],
        priority=0.9,
    )
