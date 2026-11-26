"""Interactive LLM Model Authenticity & Protocol Verifier page (/verify/).

Offers two transparent modes:
1. Online 1-click verification via stateless Cloudflare Pages Function (/api/verify — zero key storage/logging)
2. Local open-source CLI command generator (`python tools/verify_model.py` — API key never leaves user's machine)
"""

from __future__ import annotations

from typing import Any


def build_verify(site: Any) -> None:
    body = """
<section class="hero sm">
  <p class="eyebrow">模型验真 · 防套壳降智 · 协议与缓存实测</p>
  <h1>LLM 模型真伪鉴别与协议自测工具</h1>
  <p class="lead">不要再问大模型「你是什么模型」——套壳站只需一行 system prompt 就能骗过自报身份。本工具通过 <b>4 项可证伪技术指纹</b>（协议与响应头指纹、Anthropic Thinking 加密签名两轮回传、严格 Tool-Calling 类型校验、两轮 Prompt Cache 真命中）现场核验中转站真实链路。</p>
</section>

<section class="verify-box" id="verify-app">
  <div class="verify-tabs" role="tablist" aria-label="检测模式">
    <button type="button" class="btn sm primary" data-vtab="online">在线一键验真（Cloudflare 边缘无状态转发）</button>
    <button type="button" class="btn sm" data-vtab="local">本机开源脚本验真（Key 完全不离本机）</button>
  </div>

  <div class="calc-grid" style="margin-top:12px">
    <label>中转站 Base URL<input id="v-url" type="url" placeholder="https://api.example.com" autocomplete="off"></label>
    <label>目标模型 ID<input id="v-model" type="text" value="claude-sonnet-5-5" list="v-models" autocomplete="off"></label>
    <label>底层协议<select id="v-proto">
      <option value="auto">自动匹配（Claude→Anthropic，GPT-6→Responses）</option>
      <option value="anthropic">Anthropic 原生（/v1/messages）</option>
      <option value="openai-response">OpenAI Responses（/v1/responses）</option>
      <option value="openai">OpenAI Chat（/v1/chat/completions）</option>
    </select></label>
  </div>

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

  <div class="v-checks">
    <label><input type="checkbox" name="vchk" value="fingerprint" checked> ① 协议指纹与模型回显</label>
    <label><input type="checkbox" name="vchk" value="thinking" checked> ② Thinking 加密签名回传 / 推理遥测</label>
    <label><input type="checkbox" name="vchk" value="tool" checked> ③ 严格 Tool-Calling 类型校验</label>
    <label><input type="checkbox" name="vchk" value="cache" checked> ④ 两轮 Prompt Cache 真命中</label>
  </div>

  <div id="v-pane-online">
    <div class="v-key-row">
      <label style="flex:1">测试用 API Key（建议使用小额临时 Key；边缘函数仅内存转发、绝不落盘或记录日志）
        <input id="v-key" type="password" placeholder="sk-..." autocomplete="new-password">
      </label>
      <button type="button" class="btn primary" id="v-run">开始 4 项真伪鉴别</button>
    </div>
    <p class="muted" id="v-status">全套 4 项检测约消耗 ~3,000 tokens（一般不足 ¥0.03），约 8–18 秒返回原始证据。代码完全开源：<a href="https://github.com/Guoz7/relay_price/blob/main/functions/api/verify.js" target="_blank" rel="noopener">functions/api/verify.js</a>。</p>
  </div>

  <div id="v-pane-local" hidden>
    <p class="muted">如果你不希望测试 Key 经过任何云端转发，直接复制下方命令在本机运行零依赖开源脚本（<a href="https://github.com/Guoz7/relay_price/blob/main/tools/verify_model.py" target="_blank" rel="noopener">tools/verify_model.py</a>）：</p>
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
    <li><b>Anthropic Thinking 加密签名回传（防套壳核心杀手锏）</b>：原生 Claude 启用 Extended Thinking 时会返回一段由 Anthropic 服务端签名的加密 <code>signature</code>，且在多轮对话回传该 block 时会校验签名合法性。把 DeepSeek/Qwen 伪装成 Claude 的低价套壳池几乎无法伪造或通过二轮签名回传。</li>
    <li><b>两轮 Prompt Cache 真命中</b>：连续两次发送 ~1,300 token 固定前缀，核对次轮 <code>cache_read_input_tokens</code> / <code>cached_tokens</code> 是否真正大于 0。如果一家站标榜便宜却把请求轮询发给不同池子或剥离缓存头，你的 Claude Code 实际账单会贵 3–5 倍。</li>
    <li><b>严格 Tool-Calling 类型校验</b>：要求模型强制调用结构化工具并传入严格整型 <code>code: 4096</code>，直接筛出吞工具调用、降级纯文本或 JSON schema 漂移的渠道。</li>
    <li><b>开放验真 API（POST <code>/api/verify</code>）</b>：本接口同样开放给开发者脚本或监控工具调用（<code>GET https://agentied.work/api/verify</code> 查看请求 Schema），零留存、无状态。</li>
  </ul>
</section>

<script>
(function(){
  var params = new URLSearchParams(location.search);
  var urlIn = document.getElementById('v-url');
  var modIn = document.getElementById('v-model');
  var proIn = document.getElementById('v-proto');
  var keyIn = document.getElementById('v-key');
  if(params.get('base_url')) urlIn.value = params.get('base_url');
  if(params.get('model')) modIn.value = params.get('model');
  if(params.get('protocol')) proIn.value = params.get('protocol');

  var lastReport = null;
  function esc(s){ return String(s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
  function selectedChecks(){
    return Array.prototype.slice.call(document.querySelectorAll('input[name=vchk]:checked')).map(function(x){return x.value;});
  }
  function updateCli(){
    var u = (urlIn.value || 'https://api.example.com').trim();
    var m = (modIn.value || 'claude-sonnet-5-5').trim();
    var p = proIn.value || 'auto';
    var cs = selectedChecks().join(',') || 'fingerprint,thinking,tool,cache';
    document.getElementById('v-cli').textContent =
      'python tools/verify_model.py \\\n  --base-url "'+u+'" \\\n  --api-key "$YOUR_API_KEY" \\\n  --model "'+m+'" \\\n  --protocol '+p+' \\\n  --checks '+cs;
  }
  [urlIn, modIn, proIn].forEach(function(el){ el.addEventListener('input', updateCli); el.addEventListener('change', updateCli); });
  Array.prototype.forEach.call(document.querySelectorAll('input[name=vchk]'), function(el){ el.addEventListener('change', updateCli); });
  updateCli();

  Array.prototype.forEach.call(document.querySelectorAll('button[data-vtab]'), function(btn){
    btn.addEventListener('click', function(){
      var t = btn.dataset.vtab;
      Array.prototype.forEach.call(document.querySelectorAll('button[data-vtab]'), function(b){ b.classList.toggle('primary', b===btn); });
      document.getElementById('v-pane-online').hidden = (t !== 'online');
      document.getElementById('v-pane-local').hidden = (t !== 'local');
    });
  });

  document.getElementById('v-copy-cli').addEventListener('click', function(){
    navigator.clipboard && navigator.clipboard.writeText(document.getElementById('v-cli').textContent);
    this.textContent = '已复制'; var b=this; setTimeout(function(){b.textContent='复制命令';}, 1500);
  });

  document.getElementById('v-run').addEventListener('click', function(){
    var btn = this, st = document.getElementById('v-status'), box = document.getElementById('v-result');
    var base_url = urlIn.value.trim(), api_key = keyIn.value.trim(), model = modIn.value.trim(), protocol = proIn.value;
    var checks = selectedChecks();
    if(!base_url || !api_key || !model){
      st.textContent = '请先填写 Base URL、测试 API Key 和目标模型名。';
      return;
    }
    btn.disabled = true; btn.textContent = '正在执行实测探针…';
    st.textContent = '正在依次执行 ' + checks.length + ' 项真伪鉴别（约 8–18 秒，请勿关闭页面）…';
    fetch('/api/verify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({base_url: base_url, api_key: api_key, model: model, protocol: protocol, checks: checks})
    }).then(function(r){ return r.json().then(function(d){ return {ok:r.ok, status:r.status, data:d}; }); })
      .then(function(res){
        btn.disabled = false; btn.textContent = '开始 4 项真伪鉴别';
        if(!res.ok || res.data.error){
          st.textContent = '检测未完成：' + (res.data.error || ('HTTP ' + res.status));
          return;
        }
        var d = res.data; lastReport = d; box.hidden = false;
        st.textContent = '检测完成于 ' + d.checked_at + ' · 协议：' + d.protocol;
        var cls = d.level === 'pass' ? 'ok' : (d.level === 'warn' ? 'warn' : 'danger');
        document.getElementById('v-verdict').innerHTML =
          '<span class="badge '+cls+'">真伪特征分 '+d.score+' / 100（通过 '+d.passed+'/'+d.total+'）</span> <b>'+esc(d.verdict)+'</b>';
        document.getElementById('v-cards').innerHTML = (d.results || []).map(function(x){
          var c = x.status==='pass' ? 'ok' : (x.status==='warn' ? 'warn' : 'danger');
          var lbl = x.status==='pass' ? '通过' : (x.status==='warn' ? '存疑 / 缺失' : '异常 / 失败');
          return '<div class="v-card"><div><b>'+esc(x.title)+'</b> <span class="badge '+c+'">'+lbl+'</span> <small>'+x.ms+'ms</small></div>'
            +'<p>'+esc(x.summary)+'</p></div>';
        }).join('');
        document.getElementById('v-raw').textContent = JSON.stringify(d, null, 2);
        try {
          var dom = new URL(d.base_url).hostname;
          document.getElementById('v-review-link').href =
            'https://github.com/Guoz7/relay_price/issues/new?template=station-review.yml&title='
            + encodeURIComponent('[实测评价] ' + dom + ' (' + d.model + ' · 验真 ' + d.score + '/100)');
        } catch(e){}
      })
      .catch(function(err){
        btn.disabled = false; btn.textContent = '开始 4 项真伪鉴别';
        st.textContent = '请求异常（本地静态预览不含边缘函数，请在 agentied.work 线上使用在线模式，或切到“本机开源脚本验真”）：' + err;
      });
  });

  document.getElementById('v-copy-md').addEventListener('click', function(){
    if(!lastReport) return;
    var d = lastReport;
    var lines = [
      '### LLM 模型真伪实测报告（API 比价雷达）',
      '- 站点：`'+d.base_url+'` ｜ 模型：`'+d.model+'` ｜ 协议：`'+d.protocol+'`',
      '- 综合特征分：**'+d.score+'/100**（'+d.passed+'/'+d.total+' 通过）— '+d.verdict,
    ];
    (d.results||[]).forEach(function(x){ lines.push('- ['+x.status.toUpperCase()+'] **'+x.title+'**（'+x.ms+'ms）：'+x.summary); });
    lines.push('- 工具地址：https://agentied.work/verify/');
    navigator.clipboard && navigator.clipboard.writeText(lines.join('\n'));
    this.textContent = '已复制 Markdown'; var b=this; setTimeout(function(){b.textContent='复制 Markdown 验真报告（可贴社区 / 纠错）';}, 1800);
  });
})();
</script>"""
    faq = [
        {"@type": "Question", "name": "怎么判断 AI API 中转站是不是假模型或套壳降智？",
         "acceptedAnswer": {"@type": "Answer", "text": "不要直接问模型身份。应通过原生协议与响应头指纹、Anthropic Thinking 加密签名两轮回传校验、严格 Tool-Calling 类型校验及两轮 Prompt Cache 实测命中率综合核验。"}},
        {"@type": "Question", "name": "在 agentied.work/verify/ 输入测试 API Key 安全吗？",
         "acceptedAnswer": {"@type": "Answer", "text": "在线模式由开源 Cloudflare Pages Function 在内存中无状态转发、不存日志也不落盘；更注重隐私的开发者可切换至「本机开源脚本验真」，直接在本机运行 tools/verify_model.py，Key 完全不离开本机。"}},
    ]
    site.page(
        "/verify/",
        "LLM 模型真伪鉴别与协议自测工具（防套壳 / Thinking 签名 / 缓存验真）",
        "在线或本机一键检测 AI API 中转站模型真伪：验证 Anthropic Thinking 加密签名回传、协议指纹、严格 Tool-Calling 与两轮 Prompt Cache 真命中。",
        body,
        crumbs=[("模型验真", "/verify/")],
        ld=[{"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": faq}],
        priority=0.9,
    )
