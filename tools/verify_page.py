"""Interactive LLM Model Authenticity & Intelligence Verifier page (/verify/).

Offers two transparent modes:
1. Online verification via the stateless Cloudflare Pages Function (/api/verify — zero key storage/logging).
   The page runs `fingerprint` first (fail-fast on dead key / URL / model), then the remaining checks in
   batches of 3 with one shared seed, and aggregates `auth_score` / `iq_score` with the same formula as the API
   (functions/api/verify.js `summarize`).
2. Local open-source CLI command generator (`python tools/verify_model.py --tier ...` — API key never leaves
   the machine; optional `--reference-url` differential testing on the same seed).
"""

from __future__ import annotations

from typing import Any

QUICK_MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "gpt-6-astra", "deepseek-v4.1-flash", "gemini-4-carbon"]

AUTH_STEPS = [
    ("fingerprint", "协议指纹与模型回显"),
    ("thinking", "Thinking 签名 / 推理遥测"),
    ("tool", "动态 Tool-Calling"),
    ("cache", "两轮 Prompt Cache"),
]
IQ_STEPS = [
    ("iq_trace", "状态机执行追踪"),
    ("iq_logic", "多约束逻辑推理"),
    ("iq_constraints", "复合指令遵从"),
    ("context_hop", "长文多跳防截断"),
]
STEPS = AUTH_STEPS + IQ_STEPS

TIERS = [
    ("l1_auth", "L1 快速真伪", "协议指纹 · 签名防篡改 · 工具 · 缓存", "约 4k tokens", "≈ ¥0.04", "4 项 · 约 6–12 秒",
     ["fingerprint", "thinking", "tool", "cache"]),
    ("l2_iq", "L2 真伪 + 智商抽检", "L1 全部 + 状态机追踪 · 逻辑推理 · 复合指令", "约 9k tokens", "≈ ¥0.09",
     "7 项 · 约 10–18 秒", ["fingerprint", "thinking", "tool", "cache", "iq_trace", "iq_logic", "iq_constraints"]),
    ("l3_deep", "L3 深度防降级", "L2 全部 + 长文 3 跳追踪防静默截断", "约 22k tokens", "≈ ¥0.22", "8 项 · 约 15–30 秒",
     [c for c, _ in STEPS]),
]
NUMS = "①②③④⑤⑥⑦⑧"


def _chips() -> str:
    return "".join(
        f'<button type="button" class="chip-btn" data-vmodel="{m}" aria-pressed="{"true" if i == 0 else "false"}">{m}</button>'
        for i, m in enumerate(QUICK_MODELS)
    )


def _tiers() -> str:
    out = []
    for tid, name, desc, tok, cost, eta, checks in TIERS:
        rec = ' <span class="v-rec">推荐</span>' if tid == "l2_iq" else ""
        out.append(
            f'<button type="button" class="v-tier" data-vtier="{tid}" data-checks="{",".join(checks)}" '
            f'aria-pressed="{"true" if tid == "l2_iq" else "false"}"><b>{name}{rec}</b><span>{desc}</span>'
            f'<small><em>{tok}</em><em>{cost}</em><em>{eta}</em></small></button>'
        )
    return "".join(out)


def _checks() -> str:
    on = set(TIERS[1][6])

    def group(title: str, steps: list[tuple[str, str]], start: int, cls: str) -> str:
        boxes = "".join(
            f'<label><input type="checkbox" name="vchk" value="{cid}"{" checked" if cid in on else ""}> {NUMS[start + i]} {label}</label>'
            for i, (cid, label) in enumerate(steps)
        )
        return f'<fieldset class="v-group {cls}"><legend>{title}</legend>{boxes}</fieldset>'

    return group("真伪特征（auth_score）", AUTH_STEPS, 0, "g-auth") + group("智商保真（iq_score）", IQ_STEPS, 4, "g-iq")


def _steps() -> str:
    return "".join(
        f'<li data-step="{cid}" class="is-idle"><i aria-hidden="true"></i><span>{label}</span><small>待执行</small></li>'
        for cid, label in STEPS
    )


BODY_HEAD = """
<section class="hero sm">
  <p class="eyebrow">模型验真 · 防套壳降智 · 动态挑战 · 智商抽检</p>
  <h1>LLM 模型真伪与智商保真鉴别工具</h1>
  <p class="lead">延迟和可用率在多数中转站上都能看到，买家真正看不见的是两件事：<b>模型是不是真的</b>（假模型、静默替换、伪造签名、剥离缓存），以及<b>智商有没有被偷偷降档</b>（换成弱模型或量化版、剥离推理档、静默截断上下文）。本工具每次用随机种子现场生成全新挑战，按 <b>真伪特征分</b> 与 <b>智商保真分</b> 两条线独立打分，任何一条不及格都直接判定异常，不会被低延迟“平均掉”。</p>
</section>

<section class="verify-box" id="verify-app">
  <div class="verify-tabs" role="tablist" aria-label="检测模式">
    <button type="button" class="btn sm primary" data-vtab="online" role="tab" aria-selected="true">在线一键验真（Cloudflare 边缘无状态转发）</button>
    <button type="button" class="btn sm" data-vtab="local" role="tab" aria-selected="false">本机开源脚本验真（Key 完全不离本机）</button>
  </div>

  <div class="v-tiers" role="group" aria-label="检测档位">"""

BODY_FORM = """</div>

  <div class="calc-grid" style="margin-top:12px">
    <label>中转站 Base URL<input id="v-url" type="url" placeholder="https://api.example.com" autocomplete="off" inputmode="url"></label>
    <label>目标模型 ID<input id="v-model" type="text" value="claude-sonnet-5-5" list="v-models" autocomplete="off" spellcheck="false"></label>
    <label>底层协议<select id="v-proto">
      <option value="auto">自动匹配（Claude→Anthropic，GPT-6→Responses，Gemini→原生）</option>
      <option value="anthropic">Anthropic 原生（/v1/messages）</option>
      <option value="openai-response">OpenAI Responses（/v1/responses）</option>
      <option value="openai">OpenAI Chat（/v1/chat/completions）</option>
      <option value="gemini">Gemini 原生（/v1beta/models/…:generateContent）</option>
    </select></label>
  </div>
  <div class="v-chips" role="group" aria-label="常用模型快捷选择"><span class="muted">常用：</span>"""

BODY_MID = """<small class="muted" id="v-proto-hint" aria-live="polite"></small></div>

  <datalist id="v-models">
    <option value="claude-sonnet-5-5">
    <option value="claude-opus-5-5">
    <option value="claude-fable-5-2">
    <option value="gpt-6-astra">
    <option value="gpt-6.1-sol">
    <option value="gpt-5.4">
    <option value="deepseek-v4.1-flash">
    <option value="deepseek-v4-pro">
    <option value="gemini-4-carbon">
  </datalist>

  <div class="v-checks">"""

BODY_PANES = """</div>
  <div class="v-opts">
    <label>随机种子（留空则每次随机；填写后可复现同一套挑战）<input id="v-seed" type="text" inputmode="numeric" placeholder="随机" autocomplete="off" spellcheck="false"></label>
    <label id="v-ctx-wrap">长文多跳上下文 token（在线 4,000–28,000）<input id="v-ctx" type="number" min="4000" max="28000" step="1000" value="12000"></label>
  </div>

  <div id="v-pane-online">
    <div class="v-key-row">
      <label style="flex:1">测试用 API Key（建议使用小额临时 Key；边缘函数仅内存转发、绝不落盘、记录或回显）
        <input id="v-key" type="password" placeholder="sk-..." autocomplete="new-password" spellcheck="false">
      </label>
      <button type="button" class="btn primary" id="v-run">开始鉴别</button>
    </div>
    <p class="muted" id="v-status" role="status" aria-live="polite">先跑协议指纹（Key / URL / 模型不可用时立即停止，不浪费 token），其余项每批 3 个并行。token 与费用为粗估（按约 ¥10 / 百万 token），实际以中转站账单为准。代码完全开源：<a href="https://github.com/Guoz7/relay_price/blob/main/functions/api/verify.js" target="_blank" rel="noopener">functions/api/verify.js</a>。</p>
    <div class="v-progress" id="v-progress" hidden>
      <div class="v-prog" aria-hidden="true"><i id="v-prog-bar"></i></div>
      <ol class="v-steps" id="v-steps">"""

BODY_TAIL = r"""</ol>
    </div>
  </div>

  <div id="v-pane-local" hidden>
    <p class="muted">如果你不希望测试 Key 经过任何云端转发，直接复制下方命令在本机运行零依赖开源脚本（<a href="https://github.com/Guoz7/relay_price/blob/main/tools/verify_model.py" target="_blank" rel="noopener">tools/verify_model.py</a>，挑战生成器、判分器与在线版逐项同步）。本机版长文多跳最高支持 64,000 token；加上 <code>--reference-url</code> 与 <code>--reference-key</code> 后，会用<b>同一个种子</b>对官方端点做差分对照。</p>
    <div class="codebox">
      <pre id="v-cli"></pre>
      <button type="button" class="btn sm copy" id="v-copy-cli">复制命令</button>
    </div>
  </div>

  <div id="v-result" hidden>
    <div class="v-gauges" id="v-gauges"></div>
    <div class="v-verdict" id="v-verdict"></div>
    <div id="v-cards"></div>
    <div class="v-actions">
      <button type="button" class="btn sm" id="v-copy-md">复制 Markdown 验真报告（可贴社区 / 纠错）</button>
      <a class="btn sm" id="v-review-link" href="https://github.com/Guoz7/relay_price/issues/new/choose" target="_blank" rel="noopener">把本次实测提交为站点评价 →</a>
    </div>
    <details class="groups"><summary>查看完整原始 JSON 证据</summary><div class="codebox"><pre id="v-raw"></pre></div></details>
  </div>
</section>

<section class="prose">
  <h2>为什么要把“真伪”和“智商”分开打分？</h2>
  <p>真伪特征分看<b>链路是不是原生的</b>，智商保真分看<b>回答你的是不是声明的那一档模型</b>。两者互不补偿：签名篡改被接受，或任一分数低于 55，结论直接判为异常。所有挑战都由随机种子（Mulberry32 伪随机数）现场生成，判分全部是精确比对，<b>不用大模型当裁判</b>。仓库开源也无妨：中转站无法用正则识别固定题目、只把探测请求转给官方上游。</p>
  <h2>真伪特征（auth_score）</h2>
  <ul>
    <li><b>Anthropic Thinking 签名回传 + 篡改负向校验</b>：先回传真签名，应按动态算术正确续答；再并行回传一份只改动 8 个字符的篡改签名，原生上游必然以 400 拒绝。篡改签名也被接受，说明签名根本没人校验，基本可判定是伪造 thinking 或转译链路。</li>
    <li><b>协议指纹与 usage 自洽</b>：响应结构、ID 格式、模型回显与动态算术；二轮输入 token 必须随上下文增长，同时记录 TTFB、总耗时与 tok/s。</li>
    <li><b>动态 Tool-Calling</b>：工具名、参数键、枚举值与整型编号每次都不同，筛出吞工具调用、降级纯文本或 schema 漂移的渠道。</li>
    <li><b>两轮 Prompt Cache</b>：约 1,350 token 的动态条款库（含会话随机串与随机目标条款），次轮必须命中缓存读 token 且答对条款编号。</li>
  </ul>
  <h2>智商保真（iq_score）</h2>
  <ul>
    <li><b>状态机执行追踪</b>：随机生成 6 步整数程序（条件分支、取模、按位异或），要求给出精确终态与 checksum。剥离推理档或换成小模型，最容易在这里露馅。</li>
    <li><b>多约束逻辑推理</b>：4 人 × 3 属性的推理题，每道题都由内置精确求解器证明唯一解，绝不出现有歧义的题。</li>
    <li><b>复合指令遵从</b>：4 条可程序判定的约束同时成立（动态键名、精确数量、禁用字母、长度单调），报告具体哪一条被破坏。</li>
    <li><b>长文多跳追踪</b>（仅 L3）：在约 12,000 token 的日志里，于约 15% / 52% / 87% 深度埋入 3 跳指针链，并混入诱饵链；同时比对上游返回的 prompt_tokens 与估算值，识别静默截断或 token 虚增。</li>
  </ul>
  <h2>开放验真 API</h2>
  <p>POST <code>/api/verify</code> 支持 <code>tier</code>（<code>l1_auth</code> / <code>l2_iq</code> / <code>l3_deep</code>）、<code>checks</code>、<code>seed</code> 与 <code>context_tokens</code>，<code>GET https://agentied.work/api/verify</code> 查看 Schema。仅接受公网 HTTPS 域名的 443 端口，禁止裸 IP、内网域名与 30x 跳转，零留存、无状态。</p>
</section>
<script>
(function(){
  var params = new URLSearchParams(location.search);
  var urlIn = document.getElementById('v-url');
  var modIn = document.getElementById('v-model');
  var proIn = document.getElementById('v-proto');
  var keyIn = document.getElementById('v-key');
  var seedIn = document.getElementById('v-seed');
  var ctxIn = document.getElementById('v-ctx');
  var ctxWrap = document.getElementById('v-ctx-wrap');
  var runBtn = document.getElementById('v-run');
  var stEl = document.getElementById('v-status');
  var hint = document.getElementById('v-proto-hint');
  if(params.get('base_url')) urlIn.value = params.get('base_url');
  if(params.get('model')) modIn.value = params.get('model');
  if(params.get('protocol')) proIn.value = params.get('protocol');

  var IQ = ['iq_trace', 'iq_logic', 'iq_constraints', 'context_hop'];
  var FAIL_FAST_HTTP = [0, 401, 402, 403, 404];
  var LABELS = {anthropic:'Anthropic 原生', 'openai-response':'OpenAI Responses', openai:'OpenAI Chat', gemini:'Gemini 原生'};
  var STATUS_TXT = {pass:'通过', warn:'存疑 / 缺失', fail:'异常 / 失败'};
  var EXPLAIN = {
    fingerprint: {
      pass: '响应结构、ID 格式、模型回显与动态算术全部符合原生协议，请求确实到达了声明的协议端点。',
      warn: '能正常回答，但模型回显、ID 或结构有一项不符。常见于中转改写 model 字段或做了协议转换，需结合其他检测判断。',
      fail: '调用失败或多项指纹不符，疑似转译、降级或接口不兼容。'
    },
    thinking: {
      pass: 'Claude：真签名可二轮回传、篡改签名被上游拒绝，签名确实被校验，套壳很难伪造。其他协议：返回了原生推理遥测且 usage 自洽。',
      warn: '部分特征缺失：二轮回传异常、篡改校验未完成、usage 不自洽，或非推理档模型本身不返回推理字段。建议换推理档模型复测。',
      fail: '缺少有效签名、篡改签名仍被接受或推理答错。Claude 渠道出现此项，基本可判定不是原生链路。'
    },
    tool: {
      pass: '强制调用动态生成的工具，返回了结构化调用且整数参数类型精确，适合 Claude Code / Agent 工作流。',
      warn: '调用了工具但参数类型漂移（例如整数变成字符串），严格 schema 的 Agent 可能出错。',
      fail: '没有按动态 schema 返回结构化工具调用，Agent / 编程类工作流会直接失败。'
    },
    cache: {
      pass: '第二轮命中了缓存读 token 且答对了随机条款，编程场景的缓存折扣真实生效。',
      warn: '未观察到缓存命中或条款召回错误：可能剥离了缓存头、多账号轮询，或未达到上游最小缓存阈值。',
      fail: '缓存测试请求失败，无法评估缓存命中。'
    },
    iq_trace: {
      pass: '精确执行了随机生成的 6 步整数程序，推理能力与旗舰档一致。',
      warn: '只有 3/4 个终态字段正确，常见于推理档被削减或量化模型的计算漂移。',
      fail: '终态大面积错误或没有给出 JSON，疑似静默换成弱模型或剥离了推理。'
    },
    iq_logic: {
      pass: '完整推出唯一解（唯一性已由内置求解器证明），多步演绎能力正常。',
      warn: '接近唯一解但有个别属性错误，建议换一个种子复测。',
      fail: '多数属性推错或没有给出 JSON，疑似静默降级为弱模型。'
    },
    iq_constraints: {
      pass: '4 条约束同时满足，复合指令遵从能力正常。',
      warn: '有 1 条约束被破坏，见下方明细。',
      fail: '2 条及以上约束被破坏，指令遵从明显弱于旗舰档。'
    },
    context_hop: {
      pass: '在长日志的前段、中段、后段完成 3 跳追踪，且上游 prompt_tokens 与估算一致，未发现截断。',
      warn: '答案正确，但上游 prompt_tokens 与估算偏差异常，疑似截断输入或虚增计费。',
      fail: '没有追到正确的 FINAL_CODE 或命中诱饵链，疑似静默截断、丢失中段上下文或回退弱模型。'
    }
  };

  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
  function all(sel){ return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  function isIq(id){ return IQ.indexOf(id) >= 0; }
  function detect(m){
    m = String(m||'').toLowerCase().split('/').pop();
    if(m.indexOf('claude')>=0 || m.indexOf('fable')>=0) return 'anthropic';
    if(m.indexOf('gemini')>=0) return 'gemini';
    if(/^(?:gpt-6|gpt-5|o3|o4|codex)/.test(m)) return 'openai-response';
    return 'openai';
  }
  function selectedChecks(){ return all('input[name=vchk]:checked').map(function(x){ return x.value; }); }
  function activeTier(){
    var cs = selectedChecks().join(','), hit = null;
    all('button[data-vtier]').forEach(function(b){ if(b.dataset.checks === cs) hit = b.dataset.vtier; });
    return hit;
  }
  function seedValue(){
    var s = seedIn.value.trim();
    if(!s) return null;
    if(!/^\d{1,10}$/.test(s) || Number(s) > 4294967295) return NaN;
    return Number(s);
  }
  function ctxValue(){
    var v = Math.floor(Number(ctxIn.value));
    if(!isFinite(v)) v = 12000;
    return Math.min(28000, Math.max(4000, v));
  }
  function syncUi(){
    var m = modIn.value.trim();
    all('button[data-vmodel]').forEach(function(b){ b.setAttribute('aria-pressed', b.dataset.vmodel === m ? 'true' : 'false'); });
    var tier = activeTier();
    all('button[data-vtier]').forEach(function(b){ b.setAttribute('aria-pressed', b.dataset.vtier === tier ? 'true' : 'false'); });
    var hop = selectedChecks().indexOf('context_hop') >= 0;
    ctxWrap.hidden = !hop;
    hint.textContent = proIn.value === 'auto' && m ? ('将使用 ' + LABELS[detect(m)] + ' 协议') : '';
    var u = (urlIn.value || 'https://api.example.com').trim();
    var sel = tier ? ('--tier ' + tier) : ('--checks ' + (selectedChecks().join(',') || 'fingerprint'));
    var seed = seedValue();
    var lines = ['python tools/verify_model.py', '  --base-url "'+u+'"', '  --api-key "$YOUR_API_KEY"',
                 '  --model "'+(m||'claude-sonnet-5-5')+'"', '  --protocol '+(proIn.value||'auto'), '  '+sel];
    if(typeof seed === 'number' && !isNaN(seed)) lines.push('  --seed ' + seed);
    if(hop) lines.push('  --context-tokens ' + ctxValue());
    document.getElementById('v-cli').textContent = lines.join(' \\\n')
      + '\n\n# 可选差分对照（同一种子对比官方端点）：\n#   --reference-url https://api.anthropic.com --reference-key "$OFFICIAL_KEY"';
  }
  [urlIn, modIn, proIn, seedIn, ctxIn].forEach(function(el){ el.addEventListener('input', syncUi); el.addEventListener('change', syncUi); });
  all('input[name=vchk]').forEach(function(el){ el.addEventListener('change', syncUi); });
  all('button[data-vmodel]').forEach(function(b){
    b.addEventListener('click', function(){ modIn.value = b.dataset.vmodel; syncUi(); modIn.focus(); });
  });
  all('button[data-vtier]').forEach(function(b){
    b.addEventListener('click', function(){
      var want = b.dataset.checks.split(',');
      all('input[name=vchk]').forEach(function(el){ el.checked = want.indexOf(el.value) >= 0; });
      syncUi();
    });
  });
  syncUi();

  all('button[data-vtab]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var t = btn.dataset.vtab;
      all('button[data-vtab]').forEach(function(b){
        b.classList.toggle('primary', b===btn); b.setAttribute('aria-selected', b===btn ? 'true' : 'false');
      });
      document.getElementById('v-pane-online').hidden = (t !== 'online');
      document.getElementById('v-pane-local').hidden = (t !== 'local');
    });
  });

  function copy(text, btn, label, done){
    var ok = function(){ btn.textContent = done; btn.classList.add('done'); setTimeout(function(){ btn.textContent = label; btn.classList.remove('done'); }, 1600); };
    if(navigator.clipboard && window.isSecureContext){ navigator.clipboard.writeText(text).then(ok, function(){ btn.textContent = '请手动复制'; }); }
    else { btn.textContent = '请手动复制'; }
  }
  document.getElementById('v-copy-cli').addEventListener('click', function(){
    copy(document.getElementById('v-cli').textContent, this, '复制命令', '已复制');
  });

  function points(s){ return s==='pass' ? 100 : (s==='warn' ? 55 : 0); }
  function groupScore(xs){
    if(!xs.length) return null;
    return Math.round(xs.reduce(function(a, x){ return a + points(x.status); }, 0) / xs.length);
  }
  function summarize(results){
    var n = results.length || 1, pass = 0, fail = 0;
    results.forEach(function(x){ if(x.status==='pass') pass++; else if(x.status==='fail') fail++; });
    var score = Math.round(results.reduce(function(a, x){ return a + points(x.status); }, 0) / n);
    var auth = groupScore(results.filter(function(x){ return !isIq(x.id); }));
    var iq = groupScore(results.filter(function(x){ return isIq(x.id); }));
    var tamper = results.some(function(x){ return x.evidence && x.evidence.tamper_result === 'accepted'; });
    var veto = [];
    if(tamper) veto.push('tampered_signature_accepted');
    if(auth !== null && auth < 55) veto.push('auth_score_below_55');
    if(iq !== null && iq < 55) veto.push('iq_score_below_55');
    var level = 'pass', verdict = iq !== null ? '真伪与智商抽检均符合原生旗舰模型特征' : '高度符合原生协议与真机特征';
    if(tamper){ level = 'fail'; verdict = '篡改签名仍被接受：签名未被校验，疑似伪造 thinking 或套壳链路（非补偿否决，直接判定异常）'; }
    else if(auth !== null && auth < 55){ level = 'fail'; verdict = '真伪特征分低于 55：疑似转译、套壳或伪造协议（非补偿否决，直接判定异常）'; }
    else if(iq !== null && iq < 55){ level = 'fail'; verdict = '智商保真分低于 55：疑似静默降级到弱模型、剥离推理档或截断上下文（非补偿否决，直接判定异常）'; }
    else if(fail > 0 || score < 55){ level = 'fail'; verdict = '存在明显异常（疑似转译、接口不兼容、降级或套壳，建议核对下方原始证据）'; }
    else if(pass < results.length){ level = 'warn'; verdict = '基础调用正常，但部分高级特征缺失（如缓存未命中或缺少原生遥测字段，建议小额实测）'; }
    return {score: score, auth_score: auth, iq_score: iq, level: level, verdict: verdict, veto: veto,
            tamper_accepted: tamper, passed: pass, total: results.length};
  }
  function cls(s){ return s==='pass' ? 'ok' : (s==='warn' ? 'warn' : 'danger'); }
  function scoreTone(v){ return v == null ? '' : (v >= 85 ? 'ok' : (v >= 55 ? 'warn' : 'danger')); }
  function pill(label, value, tone){
    return '<span class="pill'+(tone?' '+tone:'')+'"><em>'+esc(label)+'</em>'+esc(value)+'</span>';
  }
  function pills(x, model){
    var e = x.evidence || {}, t = x.telemetry || {}, out = [];
    if(x.id === 'fingerprint' && 'returned_model' in e){
      out.push(pill('请求', model));
      out.push(pill('返回', e.returned_model || '空', e.echo_ok ? 'ok' : 'danger'));
      out.push(pill('ID', e.id_ok ? '原生格式' : String(e.response_id||'空').slice(0,14), e.id_ok ? 'ok' : 'warn'));
      out.push(pill('动态算术', e.math_ok ? e.expected + ' ✓' : '期望 ' + e.expected, e.math_ok ? 'ok' : 'danger'));
    }
    if(x.id === 'thinking'){
      if('signature_len' in e) out.push(pill('签名', e.signature_len + ' 字符', e.signature_len >= 40 ? 'ok' : 'danger'));
      if('turn2_status' in e) out.push(pill('Turn-2', 'HTTP ' + e.turn2_status + (e.turn2_ok ? ' ✓' : ''), e.turn2_ok ? 'ok' : 'warn'));
      if('tamper_result' in e){
        var tm = {rejected:['被拒 ✓','ok'], accepted:['被接受 ✗','danger'], rejected_other:['HTTP '+e.tamper_status,'warn'], inconclusive:['未完成','warn']}[e.tamper_result] || ['—',''];
        out.push(pill('篡改签名', tm[0], tm[1]));
      }
      if('usage_ok' in e) out.push(pill('usage', e.usage_ok ? '自洽' : '异常', e.usage_ok ? 'ok' : 'warn'));
      if(e.cache_read_t2 != null) out.push(pill('缓存读', e.cache_read_t2));
      if('reasoning_tokens' in e) out.push(pill('reasoning_tokens', e.reasoning_tokens == null ? '无' : e.reasoning_tokens, e.strong ? 'ok' : 'warn'));
      if(e.thought_signature_len) out.push(pill('thoughtSignature', e.thought_signature_len + ' 字符', 'ok'));
    }
    if(x.id === 'tool' && 'function_name' in e){
      out.push(pill('函数', e.function_name || '无', e.function_name && e.function_name === e.expected_name ? 'ok' : 'danger'));
      if(e.arguments != null) out.push(pill('参数', JSON.stringify(e.arguments).slice(0, 40), x.status === 'pass' ? 'ok' : 'warn'));
    }
    if(x.id === 'cache' && 'cache_read_t2' in e){
      out.push(pill('缓存读', e.cache_read_t2 + ' tokens', e.cache_read_t2 > 0 ? 'ok' : 'warn'));
      out.push(pill('首轮写入', e.cache_creation_t1));
      if('answer_ok' in e) out.push(pill('条款召回', e.answer_ok ? '正确' : '错误', e.answer_ok ? 'ok' : 'warn'));
    }
    if(x.id === 'iq_trace' && 'correct' in e){
      out.push(pill('终态字段', e.correct + '/4', cls(x.status)));
      out.push(pill('期望 checksum', e.expected && e.expected.checksum));
    }
    if(x.id === 'iq_logic' && 'correct' in e){
      out.push(pill('属性正确', e.correct + '/' + e.total, cls(x.status)));
      out.push(pill('约束条数', e.clues));
      if(e.wrong && e.wrong.length) out.push(pill('错误', e.wrong.slice(0, 3).join(' '), 'warn'));
    }
    if(x.id === 'iq_constraints' && 'passed' in e){
      out.push(pill('约束满足', e.passed + '/4', cls(x.status)));
      if(e.broken && e.broken.length) out.push(pill('未满足', e.broken.join(' '), 'danger'));
    }
    if(x.id === 'context_hop' && 'found' in e){
      out.push(pill('3 跳追踪', e.found && !e.decoy_hit ? '正确' : (e.decoy_hit ? '命中诱饵' : '未找到'), e.found && !e.decoy_hit ? 'ok' : 'danger'));
      out.push(pill('prompt_tokens', (e.prompt_tokens == null ? '未返回' : e.prompt_tokens) + ' / 估算 ' + e.est_tokens, e.token_flag ? 'warn' : ''));
      if(e.depth_pct) out.push(pill('埋点深度', e.depth_pct.join('% / ') + '%'));
    }
    if(e.http_status && x.status === 'fail' && e.error) out.push(pill('HTTP', e.http_status, 'danger'));
    if(t.total_ms != null) out.push(pill('耗时', t.total_ms + ' ms'));
    if(t.ttfb_ms != null) out.push(pill('TTFB', t.ttfb_ms + ' ms'));
    if(t.tok_per_sec != null) out.push(pill('吞吐', t.tok_per_sec + ' tok/s'));
    return out.join('');
  }
  function card(x, model){
    var ex = (EXPLAIN[x.id] || {})[x.status] || '';
    return '<article class="v-card s-'+esc(x.status)+'"><header><b>'+esc(x.title)+'</b> <span class="badge '+cls(x.status)+'">'+STATUS_TXT[x.status]+'</span></header>'
      + '<div class="pills">'+pills(x, model)+'</div>'
      + '<p>'+esc(x.summary)+'</p>'
      + (ex ? '<p class="v-explain">'+esc(ex)+'</p>' : '') + '</article>';
  }
  function gauge(label, sub, v){
    var pct = v == null ? 0 : v, tone = scoreTone(v);
    return '<div class="v-gauge '+tone+'" role="meter" aria-label="'+esc(label)+'" aria-valuemin="0" aria-valuemax="100" aria-valuenow="'+pct+'" style="--v:'+pct+'">'
      + '<div class="v-ring"><b>'+(v == null ? '—' : v)+'</b><small>'+(v == null ? '未检测' : '/ 100')+'</small></div>'
      + '<div><h3>'+esc(label)+'</h3><p>'+esc(sub)+'</p></div></div>';
  }

  var stepsBox = document.getElementById('v-progress'), bar = document.getElementById('v-prog-bar');
  function setStep(id, state, note){
    var li = document.querySelector('#v-steps li[data-step="'+id+'"]');
    if(!li) return;
    li.className = 'is-' + state;
    li.querySelector('small').textContent = note;
  }

  var lastReport = null;
  function render(d){
    lastReport = d;
    var box = document.getElementById('v-result');
    box.hidden = false;
    document.getElementById('v-gauges').innerHTML =
      gauge('真伪特征分', '协议、签名、防篡改、工具、缓存', d.auth_score)
      + gauge('智商保真分', '状态机追踪、逻辑推理、复合指令、长文多跳', d.iq_score);
    var veto = d.veto && d.veto.length ? ' <span class="badge danger">非补偿否决：'+esc(d.veto.join(' · '))+'</span>' : '';
    document.getElementById('v-verdict').innerHTML =
      '<span class="badge '+cls(d.level)+'">综合 '+d.score+' / 100（通过 '+d.passed+'/'+d.total+'）</span>' + veto + ' <b>'+esc(d.verdict)+'</b>'
      + ' <small class="muted">档位 '+esc(d.tier)+' · 种子 '+esc(d.seed)+'</small>'
      + (d.fail_fast ? ' <small class="muted">'+esc(d.fail_fast)+'</small>' : '')
      + (d.protocol_fallback ? ' <small class="muted">'+esc(d.protocol_fallback.reason)+'</small>' : '');
    var rs = d.results || [];
    var auth = rs.filter(function(x){ return !isIq(x.id); }), iq = rs.filter(function(x){ return isIq(x.id); });
    function grp(title, xs){ return xs.length ? '<h3 class="v-gh">'+title+'</h3><div class="v-cards">'+xs.map(function(x){ return card(x, d.model); }).join('')+'</div>' : ''; }
    document.getElementById('v-cards').innerHTML = grp('真伪特征证据', auth) + grp('智商保真证据', iq);
    document.getElementById('v-raw').textContent = JSON.stringify(d, null, 2);
    try {
      var dom = new URL(d.base_url).hostname;
      document.getElementById('v-review-link').href =
        'https://github.com/Guoz7/relay_price/issues/new?template=station-review.yml&title='
        + encodeURIComponent('[实测评价] ' + dom + ' (' + d.model + ' · 真伪 ' + (d.auth_score == null ? '—' : d.auth_score)
          + ' · 智商 ' + (d.iq_score == null ? '—' : d.iq_score) + ')');
    } catch(e){}
  }
  function failFast(x){
    var e = x && x.evidence;
    return Boolean(x) && x.status === 'fail' && Boolean(e) && ('error' in e) && FAIL_FAST_HTTP.indexOf(e.http_status) >= 0;
  }
  function randomSeed(){
    var a = new Uint32Array(1);
    if(window.crypto && crypto.getRandomValues) crypto.getRandomValues(a); else a[0] = Math.floor(Math.random() * 4294967296);
    return a[0];
  }

  runBtn.addEventListener('click', function(){
    var base_url = urlIn.value.trim(), api_key = keyIn.value.trim(), model = modIn.value.trim();
    var checks = selectedChecks();
    if(!base_url || !api_key || !model){ stEl.textContent = '请先填写 Base URL、测试 API Key 和目标模型名。'; return; }
    if(!checks.length){ stEl.textContent = '请至少勾选一项检测。'; return; }
    var seed = seedValue();
    if(typeof seed === 'number' && isNaN(seed)){ stEl.textContent = '随机种子必须是 0–4294967295 之间的整数，或留空。'; seedIn.focus(); return; }
    if(seed === null) seed = randomSeed();
    var tier = activeTier() || 'custom', ctx = ctxValue();
    var protocol = proIn.value, requested = protocol, results = [], meta = null, done = 0;
    runBtn.disabled = true; runBtn.setAttribute('aria-busy', 'true'); runBtn.textContent = '检测中…';
    stepsBox.hidden = false; bar.style.width = '0%';
    all('#v-steps li').forEach(function(li){
      var on = checks.indexOf(li.dataset.step) >= 0;
      li.className = on ? 'is-pending' : 'is-skip';
      li.querySelector('small').textContent = on ? '排队中' : '未勾选';
    });
    function finish(msg){
      runBtn.disabled = false; runBtn.removeAttribute('aria-busy'); runBtn.textContent = '重新鉴别';
      stEl.textContent = msg;
    }
    function one(id){
      setStep(id, 'running', '执行中…');
      return fetch('/api/verify', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({base_url: base_url, api_key: api_key, model: model, protocol: protocol, checks: [id], seed: seed, context_tokens: ctx})
      }).then(function(r){ return r.json().then(function(d){ return {ok: r.ok, status: r.status, data: d}; }); })
        .then(function(res){
          done++; bar.style.width = Math.round(done / checks.length * 100) + '%';
          if(!res.ok || res.data.error){ setStep(id, 'fail', '未完成'); return {id: id, error: res.data.error || ('HTTP ' + res.status)}; }
          var d = res.data, x = (d.results || [])[0];
          if(!meta){ meta = d; if(protocol === 'auto') protocol = d.protocol; }
          if(x) setStep(id, x.status, STATUS_TXT[x.status] + ' · ' + x.ms + 'ms');
          return {id: id, result: x || null};
        }, function(err){
          done++; setStep(id, 'fail', '请求异常');
          return {id: id, error: '请求异常（本地静态预览不含边缘函数，请在 agentied.work 线上使用在线模式，或切到“本机开源脚本验真”）：' + err};
        });
    }
    function report(skipped, ff){
      var s = summarize(results);
      render({checked_at: meta.checked_at, base_url: meta.base_url, model: model, requested_protocol: requested,
              protocol: protocol, protocol_fallback: meta.protocol_fallback, tier: tier, checks: checks, seed: seed,
              context_tokens: ctx, skipped: skipped, fail_fast: ff, score: s.score, auth_score: s.auth_score,
              iq_score: s.iq_score, level: s.level, verdict: s.verdict, veto: s.veto, tamper_accepted: s.tamper_accepted,
              passed: s.passed, total: s.total, results: results, disclaimer: meta.disclaimer});
      finish('检测完成于 ' + meta.checked_at + ' · 协议：' + (LABELS[protocol] || protocol) + ' · 种子 ' + seed);
    }
    function batches(rest){
      if(!rest.length){ report([], null); return; }
      var batch = rest.slice(0, 3);
      stEl.textContent = '并行执行 ' + batch.length + ' 项（已完成 ' + done + ' / ' + checks.length + '，请勿关闭页面）…';
      Promise.all(batch.map(one)).then(function(outs){
        var err = null;
        outs.forEach(function(o){ if(o.result) results.push(o.result); if(o.error && !err) err = o.error; });
        if(err){ finish('检测未完成：' + err); return; }
        batches(rest.slice(3));
      });
    }
    var head = checks[0], rest = checks.slice(1);
    stEl.textContent = '正在执行第 1 / ' + checks.length + ' 项（' + (head === 'fingerprint' ? '协议指纹，失败即停止' : head) + '）…';
    one(head).then(function(o){
      if(o.error){ finish('检测未完成：' + o.error); return; }
      if(o.result) results.push(o.result);
      if(head === 'fingerprint' && rest.length && failFast(o.result)){
        rest.forEach(function(id){ setStep(id, 'skip', '已跳过'); });
        bar.style.width = '100%';
        report(rest, '指纹探测 HTTP ' + (o.result.evidence.http_status || '超时') + '：Key / URL / 模型不可用，已跳过其余 ' + rest.length + ' 项以免浪费 token');
        return;
      }
      batches(rest);
    });
  });

  document.getElementById('v-copy-md').addEventListener('click', function(){
    if(!lastReport) return;
    var d = lastReport;
    var na = function(v){ return v == null ? '未检测' : v + '/100'; };
    var lines = [
      '### LLM 模型真伪与智商实测报告（API 比价雷达）',
      '- 站点：`'+d.base_url+'` ｜ 模型：`'+d.model+'` ｜ 协议：`'+d.protocol+'`' + (d.protocol_fallback ? '（'+d.protocol_fallback.reason+'）' : ''),
      '- 档位：`'+d.tier+'` ｜ 随机种子：`'+d.seed+'`（同种子可复现同一套挑战）',
      '- 真伪特征分：**'+na(d.auth_score)+'** ｜ 智商保真分：**'+na(d.iq_score)+'** ｜ 综合：'+d.score+'/100（'+d.passed+'/'+d.total+' 通过）',
      '- 结论：' + d.verdict + (d.veto && d.veto.length ? '（否决：' + d.veto.join('、') + '）' : '')
    ];
    if(d.fail_fast) lines.push('- ' + d.fail_fast);
    (d.results||[]).forEach(function(x){
      var t = x.telemetry || {};
      lines.push('- ['+x.status.toUpperCase()+'] '+(isIq(x.id) ? '智商' : '真伪')+' · **'+x.title+'**（'+x.ms+'ms'+(t.tok_per_sec!=null?' · '+t.tok_per_sec+' tok/s':'')+'）：'+x.summary);
    });
    lines.push('- 检测时间：' + d.checked_at + ' ｜ 工具地址：https://agentied.work/verify/');
    copy(lines.join('\n'), this, '复制 Markdown 验真报告（可贴社区 / 纠错）', '已复制 Markdown');
  });
})();
</script>"""


def build_verify(site: Any) -> None:
    body = BODY_HEAD + _tiers() + BODY_FORM + _chips() + BODY_MID + _checks() + BODY_PANES + _steps() + BODY_TAIL
    faq = [
        {"@type": "Question", "name": "怎么判断 AI API 中转站是不是假模型或套壳降智？",
         "acceptedAnswer": {"@type": "Answer", "text": "不要直接问模型身份。应分两条线核验：真伪特征（原生协议指纹、Anthropic Thinking 签名两轮回传与篡改签名负向校验、动态 Tool-Calling、两轮 Prompt Cache 实测命中）与智商保真（随机生成的状态机执行追踪、唯一解逻辑推理、复合指令遵从和长上下文多跳追踪），两条线任一不及格即判定异常。"}},
        {"@type": "Question", "name": "什么是篡改签名负向校验？",
         "acceptedAnswer": {"@type": "Answer", "text": "把 Claude 返回的 thinking signature 改动 8 个字符后回传。原生 Anthropic 上游会以 400 拒绝；若仍正常回答，说明签名没有被校验，链路大概率是伪造或转译的。"}},
        {"@type": "Question", "name": "检测题目开源了，中转站会不会只对检测题走官方通道？",
         "acceptedAnswer": {"@type": "Answer", "text": "每次检测都由随机种子现场生成全新题目（算术、工具名、条款库、状态机程序、逻辑题与长文指针链），没有固定提示词可供识别；判分全部是程序化精确比对，不依赖大模型当裁判。"}},
        {"@type": "Question", "name": "在 agentied.work/verify/ 输入测试 API Key 安全吗？",
         "acceptedAnswer": {"@type": "Answer", "text": "在线模式由开源 Cloudflare Pages Function 在内存中无状态转发、不存日志、不落盘，上游报错中的 Key 也会被抹除；更注重隐私的开发者可切换至「本机开源脚本验真」，直接在本机运行 tools/verify_model.py，Key 完全不离开本机。"}},
    ]
    site.page(
        "/verify/",
        "LLM 模型真伪与智商保真鉴别工具（防套壳 / 防降智 / Thinking 签名 / 长文防截断）",
        "在线或本机一键检测 AI API 中转站：真伪特征分（Thinking 签名防篡改、协议指纹、动态工具调用、缓存真命中）与智商保真分（状态机追踪、唯一解逻辑题、复合指令、长文多跳），随机种子动态出题，任一不及格直接判异常。",
        body,
        crumbs=[("模型验真", "/verify/")],
        ld=[{"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": faq}],
        priority=0.9,
    )
