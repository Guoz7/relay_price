"""Static /submit/ page: inclusion criteria, supported public price formats, WAF guidance and dual-channel (Email + GitHub Issue) form generator."""
from __future__ import annotations

import json
from html import escape as E
from typing import Any
from urllib.parse import unquote, urlsplit

PUBLIC_SCHEMA_EXAMPLE = {
    "success": True,
    "data": {
        "currency": "CNY",
        "price_unit": "per_1m_tokens",
        "updated_at": "2026-11-27T00:00:00+08:00",
        "models": [
            {"group_name": "default", "model_name": "deepseek-v4.1-flash", "input_price": 1.0, "output_price": 4.0, "cache_input_price": 0.2},
            {"group_name": "default", "model_name": "claude-sonnet-5", "input_price": 18.0, "output_price": 90.0},
        ],
    },
}


def submit_email(cfg: dict[str, Any]) -> str:
    """Mailbox from config submit_url (mailto:…); empty when the config uses a non-mail URL."""
    u = str(cfg.get("submit_url") or "")
    if not u.startswith("mailto:"):
        return ""
    return unquote(urlsplit(u).path)


def build_submit(site: Any, cfg: dict[str, Any], n_stations: int, mine_stats: dict[str, Any] | None = None, n_stage0: int = 0) -> None:
    email = submit_email(cfg)
    gh_issues = str(cfg.get("github_issues_url") or "").strip()
    ua = cfg.get("user_agent") or ""
    example = json.dumps(PUBLIC_SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)
    mined = ""
    if mine_stats and mine_stats.get("candidates"):
        mined = (f"<p class=\"muted\">最近一次扩源：从公开导航与榜单挖出 {mine_stats['candidates']} 个候选域名，"
                 f"其中 {mine_stats.get('adopted', 0)} 个有可读的公开价格接口并已收录。其余多为接口被拦截、关闭匿名价格或非中转站。</p>")
    stage0_txt = f"（另含 {n_stage0} 家 401/403 闭源站按 Stage 0 仅建档案、不参与比价）" if n_stage0 else ""
    channel_parts = []
    if email:
        channel_parts.append(f'邮箱 <code>{E(email)}</code>')
    if gh_issues:
        channel_parts.append(f'<a href="{E(gh_issues)}" rel="noopener" target="_blank">GitHub Issue</a>')
    else:
        channel_parts.append("GitHub Issue 模板（复制后直接发帖）")
    channel_note = "支持双通道提交：" + " 或 ".join(channel_parts) + "。"
    gh_btn = (
        f'<a class="btn" id="sub-gh" href="{E(gh_issues)}" target="_blank" rel="noopener" aria-disabled="true" tabindex="-1">打开 GitHub Issue</a>'
        if gh_issues else ""
    )
    body = f"""
<section class="hero sm"><h1>提交收录 / 纠错</h1>
<p class="lead">本站目前收录 {n_stations} 家公开报价站点{stage0_txt}。收录不收费，也不接受付费改分；所有公开数据来自站点只读接口，实测认证均由独立匿名小号跑通真金白银探针后自动授予。</p></section>
<section class="prose">
<h2>一、收录与分层标准</h2>
<ul>
<li><b>Stage 1（公开校验·可比价，评级封顶 B 级）</b>：对外提供 AI 模型 API 中转服务；价格接口无需登录即可只读 GET 访问，计价单位明确且至少有 3 个已定价型号。</li>
<li><b>Stage 0（仅建档案·不比价不评分）</b>：返回 401/403（需登录或 WAF 拦截）的已知站点，仅保留基础档案页并注明未公开报价，站长放行只读接口后自动升级为 Stage 1。</li>
<li><b>Stage 2（实测认证·解锁 A/S 级）</b>：接入连续 7 天真金白银探针（可用率 ≥ 90% 且账单实扣偏差 ≤ ±15%）。我们只展示客观接口快照、原始实测数值与时间戳，不做主观攻击。</li>
<li>我们绝不登录、不破解、不绕过任何验证码或防火墙；公益/免费/体验/测试分组默认与常规付费分组隔离。</li>
</ul>
{mined}
<h2>二、支持的公开价格格式</h2>
<table class="plain">
<thead><tr><th>系统</th><th>默认地址</th><th>说明</th></tr></thead>
<tbody>
<tr><td>New API</td><td><code>/api/pricing</code></td><td>按「模型倍率 × 分组倍率 × 充值比例」换算；充值比例读 <code>/api/status</code> 的公开设置。支持新版嵌套 <code>data.data</code> 结构、多分组展开与分档表达式。</td></tr>
<tr><td>One API（旧版）</td><td><code>/api/pricing</code></td><td><code>data</code> 内含 <code>ModelRatio</code>、<code>CompletionRatio</code>、<code>ModelFixedPrice</code>、<code>GroupRatio</code>。</td></tr>
<tr><td>VoAPI</td><td><code>/api/pricing</code></td><td><code>data.group_info</code> 与 <code>data.model_info[].price_info</code>。</td></tr>
<tr><td>自报价格表 public-schema</td><td>任意公开地址，常用 <code>/api/public/pricing</code></td><td>适合自研系统。直接写每百万 token 的价格，币种为 <code>CNY</code> 或 <code>USD</code>。</td></tr>
</tbody></table>
<p>public-schema 最小示例（<code>price_unit</code> 必须为 <code>per_1m_tokens</code>；<code>cache_input_price</code> 可省略；<code>"enabled": false</code> 的条目会被忽略）：</p>
<div class="codebox"><button class="btn sm copy" type="button" data-copy="schema-ex">复制</button><pre id="schema-ex"><code>{E(example)}</code></pre></div>

<h2>三、开了 Cloudflare / WAF，或关闭了匿名价格接口</h2>
<p>我们遇到 401、403、验证码或挑战页时，只记为 Stage 0 未公开报价，不会绕过。想从 Stage 0 升级进比价表，可以任选一种做法：</p>
<ol>
<li><b>放行我们的 User-Agent</b>：在 WAF 规则里对下面的 UA 放行 <code>/api/status</code> 与 <code>/api/pricing</code> 两个 GET 路径。我们每天刷新一到两次，每次每站两三个请求。
<div class="codebox"><button class="btn sm copy" type="button" data-copy="ua-str">复制</button><pre id="ua-str"><code>{E(ua)}</code></pre></div></li>
<li><b>提供独立的只读报价地址</b>：例如 <code>https://api.example.com/api/pricing</code> 或按 public-schema 生成的静态 JSON，不需要对整个站点放行。</li>
<li><b>New API 站长</b>：确认后台未关闭匿名访问价格页；若前台与 API 分在不同域名，请告诉我们 API 域名。</li>
</ol>

<h2>四、生成提交内容（双通道）</h2>
<p>下面的表单只在你的浏览器里拼出 Markdown 文本，不会上传任何数据。{channel_note}</p>
<form id="sub-form" class="subform" novalidate data-email="{E(email)}" data-gh="{E(gh_issues)}">
<div class="row2">
<label>请求类型<select name="kind"><option value="收录">申请收录 / 升级公开报价</option><option value="纠错">数据纠错 / 异动复测</option><option value="实测申请">申请进入 V2 实测队列</option><option value="下架">要求下架</option></select></label>
<label>站点域名 <em>*</em><input name="domain" required placeholder="example.com" autocomplete="off" spellcheck="false"></label>
</div>
<div class="row2">
<label>价格接口地址<input name="pricing" placeholder="https://example.com/api/pricing" autocomplete="off" spellcheck="false"></label>
<label>充值比例（元 / 1 美元额度）<input name="ratio" inputmode="decimal" placeholder="例如 1 或 7.2" autocomplete="off"></label>
</div>
<label>备注与客观证据<textarea name="note" rows="4" placeholder="系统类型、分组说明、纠错页面链接、接口返回原文或时间戳证据等"></textarea></label>
<p class="err" id="sub-err" role="alert" hidden></p>
<div class="actions">
<button class="btn primary" type="submit">生成提交内容</button>
<a class="btn" id="sub-mail" href="#" aria-disabled="true" tabindex="-1">用邮件发送</a>
{gh_btn}
<button class="btn" type="button" id="sub-copy" disabled>复制 Markdown 模板</button>
<span class="status" id="sub-status" aria-live="polite"></span>
</div>
<pre id="sub-out" class="out" hidden></pre>
</form>

<h2>五、纠错、复测与下架</h2>
<ul>
<li><b>价格、标签或实测结果有异议</b>：选择「数据纠错 / 异动复测」，附上型号、分组名与可复核的客观证据。我们核对公开接口或安排 24 小时内复测，并在下一次构建同步更正；评分规则见 <a href="/about/">评分方法</a>。</li>
<li><b>要求下架</b>：站长可选择「要求下架」，请从站点域名的邮箱发送，或在站点公告里放一段我们能公开核对的说明。核实后会加入排除名单，不再抓取。</li>
<li>价格以站点实时页面为准。本站展示的是公开接口的快照，最长约一天更新一次。</li>
</ul>
</section>"""
    site.page("/submit/", "提交收录 / 纠错", "申请收录 AI API 中转站、提交价格纠错、申请实测复核或下架请求：收录标准、支持的公开价格接口格式、WAF 放行说明与邮箱/GitHub Issue 双通道提交模板。",
              body, crumbs=[("提交收录 / 纠错", "/submit/")], priority=0.4)
