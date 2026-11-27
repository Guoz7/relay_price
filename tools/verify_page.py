"""Interactive LLM Model Authenticity & Protocol Verifier page (/verify/).

Offers two transparent modes:
1. Online verification via the stateless Cloudflare Pages Function (/api/verify — zero key storage/logging).
   The page calls it once per check so the step indicator reflects real progress, then aggregates the score
   with the same formula as the API (functions/api/verify.js `summarize`).
2. Local open-source CLI command generator (`python tools/verify_model.py` — API key never leaves the machine).
"""

from __future__ import annotations

from typing import Any

QUICK_MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "gpt-6-astra", "deepseek-v4.1-flash", "gemini-4-carbon"]

STEPS = [
    ("fingerprint", "协议指纹与模型回显"),
    ("thinking", "Thinking 签名 / 推理遥测"),
    ("tool", "严格 Tool-Calling"),
    ("cache", "两轮 Prompt Cache"),
]


def _chips() -> str:
    return "".join(
        f'<button type="button" class="chip-btn" data-vmodel="{m}" aria-pressed="{"true" if i == 0 else "false"}">{m}</button>'
        for i, m in enumerate(QUICK_MODELS)
    )


def _checks() -> str:
    return "".join(
        f'<label><input type="checkbox" name="vchk" value="{cid}" checked> {"①②③④"[i]} {label}</label>'
        for i, (cid, label) in enumerate(STEPS)
    )


def _steps() -> str:
    return "".join(
        f'<li data-step="{cid}" class="is-idle"><i aria-hidden="true"></i><span>{label}</span><small>待执行</small></li>'
        for cid, label in STEPS
    )


BODY_HEAD = """
<section class="hero sm">
  <p class="eyebrow">模型验真 · 防套壳降智 · 协议与缓存实测</p>
  <h1>LLM 模型真伪鉴别与协议自测工具</h1>
  <p class="lead">不要再问大模型「你是什么模型」——套壳站只需一行 system prompt 就能骗过自报身份。本工具通过 <b>4 项可证伪技术指纹</b>（协议与响应结构指纹、Anthropic Thinking 签名回传 + 篡改签名负向校验、严格 Tool-Calling 类型校验、两轮 Prompt Cache 真命中）现场核验中转站真实链路，并附带耗时与 usage 自洽性遥测。</p>
</section>

<section class="verify-box" id="verify-app">
  <div class="verify-tabs" role="tablist" aria-label="检测模式">
    <button type="button" class="btn sm primary" data-vtab="online" role="tab" aria-selected="true">在线一键验真（Cloudflare 边缘无状态转发）</button>
    <button type="button" class="btn sm" data-vtab="local" role="tab" aria-selected="false">本机开源脚本验真（Key 完全不离本机）</button>
  </div>

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

  <div id="v-pane-online">
    <div class="v-key-row">
      <label style="flex:1">测试用 API Key（建议使用小额临时 Key；边缘函数仅内存转发、绝不落盘、记录或回显）
        <input id="v-key" type="password" placeholder="sk-..." autocomplete="new-password" spellcheck="false">
      </label>
      <button type="button" class="btn primary" id="v-run">开始真伪鉴别</button>
    </div>
    <p class="muted" id="v-status" role="status" aria-live="polite">全套检测通常消耗约 3,000 tokens（中转价下一般不足 ¥0.03），逐项执行约 8–25 秒。代码完全开源：<a href="https://github.com/Guoz7/relay_price/blob/main/functions/api/verify.js" target="_blank" rel="noopener">functions/api/verify.js</a>。</p>
    <div class="v-progress" id="v-progress" hidden>
      <div class="v-prog" aria-hidden="true"><i id="v-prog-bar"></i></div>
      <ol class="v-steps" id="v-steps">"""

BODY_TAIL = r"""</ol>
    </div>
  </div>

  <div id="v-pane-local" hidden>
    <p class="muted">如果你不希望测试 Key 经过任何云端转发，直接复制下方命令在本机运行零依赖开源脚本（<a href="https://github.com/Guoz7/relay_price/blob/main/tools/verify_model.py" target="_blank" rel="noopener">tools/verify_model.py</a>，检测逻辑与在线版逐项同步）：</p>
    <div class="codebox">
      <pre id="v-cli"></pre>
      <button type="button" class="btn sm copy" id="v-copy-cli">复制命令</button>
    </div>
  </div>

  <div id="v-result" hidden>
    <div class="v-verdict" id="v-verdict"></div>
    <div class="v-cards" id="v-cards"></div>
    <div class="v-actions">
      <button type="button" class="btn sm" id="v-copy-md">复制 Markdown 验真报告（可贴社区 / 纠错）</button>
      <a class="btn sm" id="v-review-link" href="https://github.com/Guoz7/relay_price/issues/new/choose" target="_blank" rel="noopener">把本次实测提交为站点评价 →</a>
    </div>
    <details class="groups"><summary>查看完整原始 JSON 证据</summary><div class="codebox"><pre id="v-raw"></pre></div></details>
  </div>
</section>

<section class="prose">
  <h2>为什么这 4 项检测比“问它是什么模型”靠谱得多？</h2>
  <ul>
    <li><b>Anthropic Thinking 签名回传 + 篡改负向校验（防套壳核心）</b>：原生 Claude 开启 Extended Thinking 时会返回一段由 Anthropic 服务端签名的 <code>signature</code>，多轮回传时上游会校验签名。我们先回传真签名（应正常续答 460），再并行回传一份<b>只改动 8 个字符的篡改签名</b>（原生上游必然以 400 拒绝）。篡改签名也被接受，说明签名根本没人校验——基本可判定是伪造 thinking 或转译链路。</li>
    <li><b>usage 与耗时自洽</b>：二轮输入 token 必须随上下文增长、reasoning_tokens 不能超过输出总数；同时记录 TTFB、总耗时与 tok/s，便于横向比较不同站点的真实吞吐。</li>
    <li><b>两轮 Prompt Cache 真命中</b>：连续两次发送 ~1,300 token 固定前缀，核对次轮 <code>cache_read_input_tokens</code> / <code>cached_tokens</code> / <code>cachedContentTokenCount</code> 是否大于 0。剥离缓存头或多池轮询的站点，Claude Code 实际账单会贵 3–5 倍。</li>
    <li><b>严格 Tool-Calling 类型校验</b>：强制调用结构化工具并传入整型 <code>code: 4096</code>，筛出吞工具调用、降级纯文本或 JSON schema 漂移的渠道。</li>
    <li><b>Gemini 原生协议</b>：Gemini 模型默认走 <code>/v1beta/models/{model}:generateContent</code>，校验 <code>thoughtsTokenCount</code> 与 <code>thoughtSignature</code>；站点不支持原生端点（404/405）时自动回退 OpenAI 兼容协议并在报告中注明。</li>
    <li><b>开放验真 API（POST <code>/api/verify</code>）</b>：同样开放给脚本或监控调用（<code>GET https://agentied.work/api/verify</code> 查看 Schema）。仅接受公网 HTTPS 域名的 443 端口，禁止裸 IP、内网域名与 30x 跳转，零留存、无状态。</li>
  </ul>
</section>

<script>
(function(){
  var params = new URLSearchParams(location.search);
  var urlIn = document.getElementById('v-url');
  var modIn = document.getElementById('v-model');
  var proIn = document.getElementById('v-proto');
  var keyIn = document.getElementById('v-key');
  var runBtn = document.getElementById('v-run');
  var stEl = document.getElementById('v-status');
  var hint = document.getElementById('v-proto-hint');
  if(params.get('base_url')) urlIn.value = params.get('base_url');
  if(params.get('model')) modIn.value = params.get('model');
  if(params.get('protocol')) proIn.value = params.get('protocol');

  var LABELS = {anthropic:'Anthropic 原生', 'openai-response':'OpenAI Responses', openai:'OpenAI Chat', gemini:'Gemini 原生'};
  var STATUS_TXT = {pass:'通过', warn:'存疑 / 缺失', fail:'异常 / 失败'};
  var EXPLAIN = {
    fingerprint: {
      pass: '响应结构、ID 格式、模型回显与确定性算术全部符合原生协议，请求确实到达了声明的协议端点。',
      warn: '能正常回答，但模型回显、ID 或结构有一项不符。常见于中转改写 model 字段或做了协议转换，需结合其他检测判断。',
      fail: '调用失败或多项指纹不符，疑似转译、降级或接口不兼容。'
    },
    thinking: {
      pass: 'Claude：真签名可二轮回传、篡改签名被上游拒绝，签名确实被校验，套壳很难伪造。其他协议：返回了原生推理遥测且 usage 自洽。',
      warn: '部分特征缺失：二轮回传异常、篡改校验未完成、usage 不自洽，或非推理档模型本身不返回推理字段。建议换推理档模型复测。',
      fail: '缺少有效签名、篡改签名仍被接受或推理答错。Claude 渠道出现此项，基本可判定不是原生链路。'
    },
    tool: {
      pass: '强制工具调用返回了结构化调用，整数参数类型精确，适合 Claude Code / Agent 工作流。',
      warn: '调用了工具但参数类型漂移（例如整数变成字符串），严格 schema 的 Agent 可能出错。',
      fail: '没有返回结构化工具调用，Agent / 编程类工作流会直接失败。'
    },
    cache: {
      pass: '第二轮命中了缓存读 token，编程场景的缓存折扣真实生效。',
      warn: '未观察到缓存命中：可能剥离了缓存头、多账号轮询，或未达到上游最小缓存阈值。长上下文场景账单可能偏高。',
      fail: '缓存测试请求失败，无法评估缓存命中。'
    }
  };

  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
  function detect(m){
    m = String(m||'').toLowerCase().split('/').pop();
    if(m.indexOf('claude')>=0 || m.indexOf('fable')>=0) return 'anthropic';
    if(m.indexOf('gemini')>=0) return 'gemini';
    if(/^(?:gpt-6|gpt-5|o3|o4|codex)/.test(m)) return 'openai-response';
    return 'openai';
  }
  function selectedChecks(){
    return Array.prototype.slice.call(document.querySelectorAll('input[name=vchk]:checked')).map(function(x){return x.value;});
  }
  function syncUi(){
    var m = modIn.value.trim();
    Array.prototype.forEach.call(document.querySelectorAll('button[data-vmodel]'), function(b){
      b.setAttribute('aria-pressed', b.dataset.vmodel === m ? 'true' : 'false');
    });
    hint.textContent = proIn.value === 'auto' && m ? ('将使用 ' + LABELS[detect(m)] + ' 协议') : '';
    var u = (urlIn.value || 'https://api.example.com').trim();
    var cs = selectedChecks().join(',') || 'fingerprint,thinking,tool,cache';
    document.getElementById('v-cli').textContent =
      'python tools/verify_model.py \\\n  --base-url "'+u+'" \\\n  --api-key "$YOUR_API_KEY" \\\n  --model "'+(m||'claude-sonnet-5-5')+'" \\\n  --protocol '+(proIn.value||'auto')+' \\\n  --checks '+cs;
  }
  [urlIn, modIn, proIn].forEach(function(el){ el.addEventListener('input', syncUi); el.addEventListener('change', syncUi); });
  Array.prototype.forEach.call(document.querySelectorAll('input[name=vchk]'), function(el){ el.addEventListener('change', syncUi); });
  Array.prototype.forEach.call(document.querySelectorAll('button[data-vmodel]'), function(b){
    b.addEventListener('click', function(){ modIn.value = b.dataset.vmodel; syncUi(); modIn.focus(); });
  });
  syncUi();

  Array.prototype.forEach.call(document.querySelectorAll('button[data-vtab]'), function(btn){
    btn.addEventListener('click', function(){
      var t = btn.dataset.vtab;
      Array.prototype.forEach.call(document.querySelectorAll('button[data-vtab]'), function(b){
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

  function summarize(results){
    var n = results.length || 1, pass = 0, fail = 0, acc = 0;
    results.forEach(function(x){ if(x.status==='pass'){pass++; acc+=100;} else if(x.status==='warn'){acc+=55;} else {fail++;} });
    var score = Math.round(acc / n), level = 'pass', verdict = '高度符合原生协议与真机特征';
    if(fail > 0 || score < 55){ level = 'fail'; verdict = '存在明显异常（疑似转译、接口不兼容、降级或套壳，建议核对下方原始证据）'; }
    else if(pass < results.length){ level = 'warn'; verdict = '基础调用正常，但部分高级特征缺失（如缓存未命中或缺少原生遥测字段，建议小额实测）'; }
    return {score: score, level: level, verdict: verdict, passed: pass, total: results.length};
  }
  function cls(s){ return s==='pass' ? 'ok' : (s==='warn' ? 'warn' : 'danger'); }
  function pill(label, value, tone){
    return '<span class="pill'+(tone?' '+tone:'')+'"><em>'+esc(label)+'</em>'+esc(value)+'</span>';
  }
  function pills(x, model){
    var e = x.evidence || {}, t = x.telemetry || {}, out = [];
    if(x.id === 'fingerprint' && 'returned_model' in e){
      out.push(pill('请求', model));
      out.push(pill('返回', e.returned_model || '空', e.echo_ok ? 'ok' : 'danger'));
      out.push(pill('ID', e.id_ok ? '原生格式' : String(e.response_id||'空').slice(0,14), e.id_ok ? 'ok' : 'warn'));
      out.push(pill('算术', e.math_ok ? '700 ✓' : '错误', e.math_ok ? 'ok' : 'danger'));
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
      out.push(pill('函数', e.function_name || '无', e.function_name ? 'ok' : 'danger'));
      if(e.arguments != null) out.push(pill('参数', JSON.stringify(e.arguments).slice(0, 40), x.status === 'pass' ? 'ok' : 'warn'));
    }
    if(x.id === 'cache' && 'cache_read_t2' in e){
      out.push(pill('缓存读', e.cache_read_t2 + ' tokens', e.cache_read_t2 > 0 ? 'ok' : 'warn'));
      out.push(pill('首轮写入', e.cache_creation_t1));
    }
    if(e.http_status && x.status === 'fail' && !('returned_model' in e)) out.push(pill('HTTP', e.http_status, 'danger'));
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
    document.getElementById('v-verdict').innerHTML =
      '<span class="badge '+cls(d.level)+'">真伪特征分 '+d.score+' / 100（通过 '+d.passed+'/'+d.total+'）</span> <b>'+esc(d.verdict)+'</b>'
      + (d.protocol_fallback ? ' <small class="muted">'+esc(d.protocol_fallback.reason)+'</small>' : '');
    document.getElementById('v-cards').innerHTML = (d.results || []).map(function(x){ return card(x, d.model); }).join('');
    document.getElementById('v-raw').textContent = JSON.stringify(d, null, 2);
    try {
      var dom = new URL(d.base_url).hostname;
      document.getElementById('v-review-link').href =
        'https://github.com/Guoz7/relay_price/issues/new?template=station-review.yml&title='
        + encodeURIComponent('[实测评价] ' + dom + ' (' + d.model + ' · 验真 ' + d.score + '/100)');
    } catch(e){}
  }

  runBtn.addEventListener('click', function(){
    var base_url = urlIn.value.trim(), api_key = keyIn.value.trim(), model = modIn.value.trim();
    var checks = selectedChecks();
    if(!base_url || !api_key || !model){ stEl.textContent = '请先填写 Base URL、测试 API Key 和目标模型名。'; return; }
    if(!checks.length){ stEl.textContent = '请至少勾选一项检测。'; return; }
    var protocol = proIn.value, requested = protocol, results = [], meta = null, i = 0;
    runBtn.disabled = true; runBtn.setAttribute('aria-busy', 'true'); runBtn.textContent = '检测中…';
    stepsBox.hidden = false; bar.style.width = '0%';
    Array.prototype.forEach.call(document.querySelectorAll('#v-steps li'), function(li){
      var on = checks.indexOf(li.dataset.step) >= 0;
      li.className = on ? 'is-pending' : 'is-skip';
      li.querySelector('small').textContent = on ? '排队中' : '未勾选';
    });
    function finish(msg){
      runBtn.disabled = false; runBtn.removeAttribute('aria-busy'); runBtn.textContent = '重新鉴别';
      stEl.textContent = msg;
    }
    function next(){
      if(i >= checks.length){
        var s = summarize(results);
        render({checked_at: meta.checked_at, base_url: meta.base_url, model: model, requested_protocol: requested,
                protocol: protocol, protocol_fallback: meta.protocol_fallback, score: s.score, level: s.level,
                verdict: s.verdict, passed: s.passed, total: s.total, results: results, disclaimer: meta.disclaimer});
        finish('检测完成于 ' + meta.checked_at + ' · 协议：' + (LABELS[protocol] || protocol));
        return;
      }
      var id = checks[i];
      setStep(id, 'running', '执行中…');
      stEl.textContent = '正在执行第 ' + (i+1) + ' / ' + checks.length + ' 项（请勿关闭页面）…';
      fetch('/api/verify', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({base_url: base_url, api_key: api_key, model: model, protocol: protocol, checks: [id]})
      }).then(function(r){ return r.json().then(function(d){ return {ok: r.ok, status: r.status, data: d}; }); })
        .then(function(res){
          if(!res.ok || res.data.error){
            setStep(id, 'fail', '未完成');
            finish('检测未完成：' + (res.data.error || ('HTTP ' + res.status)));
            return;
          }
          var d = res.data, x = (d.results || [])[0];
          if(!meta){ meta = d; if(protocol === 'auto') protocol = d.protocol; }
          if(x){ results.push(x); setStep(id, x.status, STATUS_TXT[x.status] + ' · ' + x.ms + 'ms'); }
          i++; bar.style.width = Math.round(i / checks.length * 100) + '%';
          next();
        })
        .catch(function(err){
          setStep(id, 'fail', '请求异常');
          finish('请求异常（本地静态预览不含边缘函数，请在 agentied.work 线上使用在线模式，或切到“本机开源脚本验真”）：' + err);
        });
    }
    next();
  });

  document.getElementById('v-copy-md').addEventListener('click', function(){
    if(!lastReport) return;
    var d = lastReport;
    var lines = [
      '### LLM 模型真伪实测报告（API 比价雷达）',
      '- 站点：`'+d.base_url+'` ｜ 模型：`'+d.model+'` ｜ 协议：`'+d.protocol+'`' + (d.protocol_fallback ? '（'+d.protocol_fallback.reason+'）' : ''),
      '- 综合特征分：**'+d.score+'/100**（'+d.passed+'/'+d.total+' 通过）— '+d.verdict
    ];
    (d.results||[]).forEach(function(x){
      var t = x.telemetry || {};
      lines.push('- ['+x.status.toUpperCase()+'] **'+x.title+'**（'+x.ms+'ms'+(t.tok_per_sec!=null?' · '+t.tok_per_sec+' tok/s':'')+'）：'+x.summary);
    });
    lines.push('- 检测时间：' + d.checked_at + ' ｜ 工具地址：https://agentied.work/verify/');
    copy(lines.join('\n'), this, '复制 Markdown 验真报告（可贴社区 / 纠错）', '已复制 Markdown');
  });
})();
</script>"""


def build_verify(site: Any) -> None:
    body = BODY_HEAD + _chips() + BODY_MID + _checks() + BODY_PANES + _steps() + BODY_TAIL
    faq = [
        {"@type": "Question", "name": "怎么判断 AI API 中转站是不是假模型或套壳降智？",
         "acceptedAnswer": {"@type": "Answer", "text": "不要直接问模型身份。应通过原生协议与响应结构指纹、Anthropic Thinking 签名两轮回传与篡改签名负向校验、严格 Tool-Calling 类型校验及两轮 Prompt Cache 实测命中率综合核验。"}},
        {"@type": "Question", "name": "什么是篡改签名负向校验？",
         "acceptedAnswer": {"@type": "Answer", "text": "把 Claude 返回的 thinking signature 改动 8 个字符后回传。原生 Anthropic 上游会以 400 拒绝；若仍正常回答，说明签名没有被校验，链路大概率是伪造或转译的。"}},
        {"@type": "Question", "name": "在 agentied.work/verify/ 输入测试 API Key 安全吗？",
         "acceptedAnswer": {"@type": "Answer", "text": "在线模式由开源 Cloudflare Pages Function 在内存中无状态转发、不存日志、不落盘，上游报错中的 Key 也会被抹除；更注重隐私的开发者可切换至「本机开源脚本验真」，直接在本机运行 tools/verify_model.py，Key 完全不离开本机。"}},
    ]
    site.page(
        "/verify/",
        "LLM 模型真伪鉴别与协议自测工具（防套壳 / Thinking 签名 / 缓存验真）",
        "在线或本机一键检测 AI API 中转站模型真伪：Anthropic Thinking 签名回传与篡改负向校验、协议指纹、Gemini 原生协议、严格 Tool-Calling 与两轮 Prompt Cache 真命中。",
        body,
        crumbs=[("模型验真", "/verify/")],
        ld=[{"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": faq}],
        priority=0.9,
    )
