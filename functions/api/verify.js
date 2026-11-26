/**
 * Stateless LLM Model Authenticity Verification API (Cloudflare Pages Function: /api/verify).
 *
 * Security & privacy guarantees:
 * - Never logs, persists, or echoes back user-supplied `api_key`.
 * - Enforces strict HTTPS + public hostname SSRF guard (blocks loopback, RFC1918, link-local, IP literals).
 * - Bounded fetch timeouts & response sizes to prevent abuse.
 *
 * Technical checks (inspired by canarybyte/veridrop & protocol telemetry):
 * 1. fingerprint: Native endpoint response model echo, ID prefix, headers & deterministic micro-math.
 * 2. thinking:    Anthropic extended-thinking cryptographic signature + Turn-2 signature round-trip
 *                 (or OpenAI/DeepSeek reasoning token telemetry).
 * 3. tool:        Strict typed Tool-Calling schema conformance (structured tool_use/tool_calls + int arg).
 * 4. cache:       2-turn ~1,300-token deterministic Prompt Cache read hit verification.
 */

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Cache-Control": "no-store",
};

const CACHE_PREFIX = (() => {
  const lines = [
    "You are a strict compliance auditor reviewing API relay technical specifications.",
    "Memorize the following numbered policy clauses and answer ONLY with the requested clause route code.",
  ];
  for (let i = 1; i <= 60; i++) {
    const n = String(i).padStart(3, "0");
    lines.push(
      `Clause ${n}: Relay gateway route R-${n} enforces UTF-8 JSON framing, monotonic request IDs, ` +
        `explicit latency accounting in milliseconds, non-negative token usage fields, and deterministic error codes.`
    );
  }
  return lines.join("\n");
})();

function jsonRes(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS_HEADERS },
  });
}

function isPrivateOrInvalidHost(hostname) {
  const h = (hostname || "").toLowerCase().trim();
  if (!h || !h.includes(".") || h.endsWith(".local") || h.endsWith(".internal") || h.endsWith(".localhost")) {
    return true;
  }
  if (h === "localhost" || h.startsWith("[") || h.includes(":")) return true;
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(h)) return true; // block raw IPv4 literals (loopback/RFC1918/metadata)
  return false;
}

function normalizeBaseUrl(raw) {
  const s = String(raw || "").trim();
  if (!s) return null;
  let u;
  try {
    u = new URL(s.startsWith("http://") || s.startsWith("https://") ? s : `https://${s}`);
  } catch {
    return null;
  }
  if (u.protocol !== "https:") return null;
  if (isPrivateOrInvalidHost(u.hostname)) return null;
  let path = u.pathname.replace(/\/+$/, "");
  path = path.replace(/\/v1\/(?:messages|chat\/completions|responses)$/i, "");
  path = path.replace(/\/v1$/i, "");
  return `${u.origin}${path}`;
}

function detectProtocol(model, requested) {
  if (requested && requested !== "auto") return requested;
  const m = String(model || "").toLowerCase();
  if (m.includes("claude") || m.includes("fable")) return "anthropic";
  if (/^(?:gpt-6|gpt-5|o3|o4|codex)/.test(m)) return "openai-response";
  return "openai";
}

async function callUpstream(url, headers, payload, timeoutMs = 24000) {
  const t0 = Date.now();
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "User-Agent": "RelayPriceVerify/1.0 (+https://agentied.work/verify/)", ...headers },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const text = (await resp.text()).slice(0, 65536);
    const ms = Date.now() - t0;
    let data = null;
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw_text: text.slice(0, 400) };
    }
    const interestingHeaders = {};
    for (const k of [
      "request-id",
      "x-request-id",
      "anthropic-ratelimit-requests-limit",
      "anthropic-organization-id",
      "openai-organization",
      "openai-processing-ms",
      "x-ratelimit-limit-requests",
      "server",
      "cf-ray",
    ]) {
      const v = resp.headers.get(k);
      if (v) interestingHeaders[k] = v;
    }
    return { ok: resp.ok, status: resp.status, ms, data, headers: interestingHeaders };
  } catch (err) {
    return { ok: false, status: 0, ms: Date.now() - t0, data: { error: String(err?.message || err) }, headers: {} };
  }
}

function authHeaders(protocol, apiKey) {
  if (protocol === "anthropic") {
    return { "x-api-key": apiKey, Authorization: `Bearer ${apiKey}`, "anthropic-version": "2023-06-01" };
  }
  return { Authorization: `Bearer ${apiKey}` };
}

function endpointUrl(base, protocol) {
  if (protocol === "anthropic") return `${base}/v1/messages`;
  if (protocol === "openai-response") return `${base}/v1/responses`;
  return `${base}/v1/chat/completions`;
}

function extractText(protocol, data) {
  if (!data || typeof data !== "object") return "";
  if (protocol === "anthropic" && Array.isArray(data.content)) {
    return data.content.filter((b) => b && b.type === "text").map((b) => b.text || "").join("\n").trim();
  }
  if (protocol === "openai-response") {
    if (typeof data.output_text === "string" && data.output_text) return data.output_text.trim();
    if (Array.isArray(data.output)) {
      const parts = [];
      for (const item of data.output) {
        if (item?.type === "message" && Array.isArray(item.content)) {
          for (const c of item.content) {
            if (typeof c?.text === "string") parts.push(c.text);
          }
        }
      }
      return parts.join("\n").trim();
    }
  }
  const ch = data.choices?.[0]?.message;
  if (ch && typeof ch.content === "string") return ch.content.trim();
  return "";
}

function modelEchoMatches(requested, returned) {
  if (!returned) return false;
  const a = String(requested).toLowerCase().replace(/^[^/]+\//, "");
  const b = String(returned).toLowerCase().replace(/^[^/]+\//, "");
  if (a === b || b.startsWith(a) || a.startsWith(b)) return true;
  // Normalize dot vs dash version variants (e.g. claude-sonnet-5-5 vs claude-sonnet-5.5-2026xxxx)
  const na = a.replace(/\./g, "-");
  const nb = b.replace(/\./g, "-");
  return nb.startsWith(na) || na.startsWith(nb);
}

async function runFingerprint(base, apiKey, model, protocol) {
  const url = endpointUrl(base, protocol);
  const prompt = "Compute (37 * 43) - 891. Reply with the single integer only, no explanation.";
  let payload;
  if (protocol === "anthropic") {
    payload = { model, max_tokens: 32, temperature: 0, messages: [{ role: "user", content: prompt }] };
  } else if (protocol === "openai-response") {
    payload = { model, max_output_tokens: 64, input: prompt };
  } else {
    payload = { model, max_tokens: 32, temperature: 0, messages: [{ role: "user", content: prompt }] };
  }
  const r = await callUpstream(url, authHeaders(protocol, apiKey), payload);
  if (!r.ok) {
    return {
      id: "fingerprint",
      title: "协议指纹与模型回显",
      status: "fail",
      ms: r.ms,
      summary: `HTTP ${r.status} 调用失败`,
      evidence: { http_status: r.status, error: JSON.stringify(r.data).slice(0, 240) },
    };
  }
  const text = extractText(protocol, r.data);
  const retModel = r.data?.model || null;
  const retId = r.data?.id || null;
  const mathOk = /\b700\b/.test(text);
  const echoOk = modelEchoMatches(model, retModel);
  const expectedPrefix =
    protocol === "anthropic" ? /^msg_/ : protocol === "openai-response" ? /^resp_/ : /^(?:chatcmpl-|cmpl-|msg_|gen-)/;
  const idOk = typeof retId === "string" && expectedPrefix.test(retId);
  const status = echoOk && mathOk && idOk ? "pass" : mathOk && (echoOk || idOk) ? "warn" : "fail";
  const notes = [];
  if (!echoOk) notes.push(`返回型号「${retModel || "空"}」与请求不一致`);
  if (!idOk) notes.push(`ID 前缀「${String(retId || "空").slice(0, 18)}」非标准 ${protocol} 格式`);
  if (!mathOk) notes.push(`确定性算术未返回 700（实际: ${text.slice(0, 40)}）`);
  return {
    id: "fingerprint",
    title: "协议指纹与模型回显",
    status,
    ms: r.ms,
    summary: notes.length ? notes.join("；") : `原生响应结构匹配（model=${retModel}，${r.ms}ms）`,
    evidence: {
      http_status: r.status,
      returned_model: retModel,
      response_id: retId,
      system_fingerprint: r.data?.system_fingerprint || null,
      answer: text.slice(0, 80),
      usage: r.data?.usage || null,
      headers: r.headers,
    },
  };
}

async function runThinking(base, apiKey, model, protocol) {
  const url = endpointUrl(base, protocol);
  if (protocol === "anthropic") {
    const p1 = {
      model,
      max_tokens: 1400,
      temperature: 1,
      thinking: { type: "enabled", budget_tokens: 1024 },
      messages: [{ role: "user", content: "Compute (19 * 23) + 17. Reply with the integer only." }],
    };
    const r1 = await callUpstream(url, authHeaders(protocol, apiKey), p1, 30000);
    if (!r1.ok) {
      return {
        id: "thinking",
        title: "Thinking 加密签名与多轮回传",
        status: "fail",
        ms: r1.ms,
        summary: `开启 Extended Thinking 返回 HTTP ${r1.status}`,
        evidence: { http_status: r1.status, error: JSON.stringify(r1.data).slice(0, 240) },
      };
    }
    const blocks = Array.isArray(r1.data?.content) ? r1.data.content : [];
    const thinkBlk = blocks.find((b) => b && b.type === "thinking");
    const sig = typeof thinkBlk?.signature === "string" ? thinkBlk.signature : "";
    const txt1 = extractText(protocol, r1.data);
    const hasSig = sig.length >= 40;
    if (!thinkBlk || !hasSig) {
      return {
        id: "thinking",
        title: "Thinking 加密签名与多轮回传",
        status: "fail",
        ms: r1.ms,
        summary: thinkBlk
          ? `存在 thinking 块但缺少有效加密 signature（长度 ${sig.length}），疑似转译或伪造`
          : "未返回原生 thinking 块（疑似被中转剥离或转译为非 Claude 模型）",
        evidence: { block_types: blocks.map((b) => b?.type), signature_len: sig.length, answer_t1: txt1.slice(0, 60) },
      };
    }
    // Turn 2: replay exact thinking+signature block back to verify authentic Anthropic signature validation
    const p2 = {
      model,
      max_tokens: 1200,
      temperature: 1,
      thinking: { type: "enabled", budget_tokens: 1024 },
      messages: [
        { role: "user", content: "Compute (19 * 23) + 17. Reply with the integer only." },
        { role: "assistant", content: blocks },
        { role: "user", content: "Add 6 to your previous integer result. Reply with the integer only." },
      ],
    };
    const r2 = await callUpstream(url, authHeaders(protocol, apiKey), p2, 30000);
    const txt2 = r2.ok ? extractText(protocol, r2.data) : "";
    const roundtripOk = r2.ok && /\b460\b/.test(txt2);
    return {
      id: "thinking",
      title: "Thinking 加密签名与多轮回传",
      status: roundtripOk ? "pass" : "warn",
      ms: r1.ms + r2.ms,
      summary: roundtripOk
        ? `两轮 Thinking 加密签名回传通过（signature 长度 ${sig.length}，结果 454 → 460）`
        : `首轮含 signature（长度 ${sig.length}），但二轮回传异常（HTTP ${r2.status}，回复 ${txt2.slice(0, 40)}）`,
      evidence: {
        signature_len: sig.length,
        signature_preview: `${sig.slice(0, 14)}…${sig.slice(-8)}`,
        answer_t1: txt1.slice(0, 40),
        turn2_status: r2.status,
        answer_t2: txt2.slice(0, 40),
      },
    };
  }

  // OpenAI Responses / OpenAI Chat reasoning telemetry check
  const prompt = "Compute (19 * 23) + 17. Reply with the integer only.";
  const payload =
    protocol === "openai-response"
      ? { model, max_output_tokens: 256, input: prompt }
      : { model, max_tokens: 256, messages: [{ role: "user", content: prompt }] };
  const r = await callUpstream(url, authHeaders(protocol, apiKey), payload);
  if (!r.ok) {
    return {
      id: "thinking",
      title: "推理遥测与确定性校验",
      status: "fail",
      ms: r.ms,
      summary: `HTTP ${r.status} 调用失败`,
      evidence: { http_status: r.status, error: JSON.stringify(r.data).slice(0, 220) },
    };
  }
  const txt = extractText(protocol, r.data);
  const okAns = /\b454\b/.test(txt);
  const rTokens =
    r.data?.usage?.output_tokens_details?.reasoning_tokens ??
    r.data?.usage?.completion_tokens_details?.reasoning_tokens ??
    null;
  const hasReasoningField = rTokens !== null || Boolean(r.data?.choices?.[0]?.message?.reasoning_content);
  return {
    id: "thinking",
    title: "推理遥测与确定性校验",
    status: okAns ? (hasReasoningField ? "pass" : "warn") : "fail",
    ms: r.ms,
    summary: okAns
      ? hasReasoningField
        ? `确定性校验通过（454），含原生推理字段（reasoning_tokens=${rTokens ?? "text"}）`
        : "确定性校验通过（454），但未返回 reasoning_tokens 字段（部分非推理档或中转会隐藏）"
      : `算术推理失败（期望 454，实际: ${txt.slice(0, 50)}）`,
    evidence: { answer: txt.slice(0, 60), reasoning_tokens: rTokens, usage: r.data?.usage || null },
  };
}

async function runToolCall(base, apiKey, model, protocol) {
  const url = endpointUrl(base, protocol);
  const userMsg = 'Call the tool lookup_audit_code with region="CN" and code=4096. Do not answer in plain text.';
  let payload;
  if (protocol === "anthropic") {
    payload = {
      model,
      max_tokens: 200,
      tools: [
        {
          name: "lookup_audit_code",
          description: "Lookup an audit verification record by region and numeric code.",
          input_schema: {
            type: "object",
            properties: {
              region: { type: "string", enum: ["CN", "US"] },
              code: { type: "integer" },
            },
            required: ["region", "code"],
          },
        },
      ],
      tool_choice: { type: "any" },
      messages: [{ role: "user", content: userMsg }],
    };
  } else if (protocol === "openai-response") {
    payload = {
      model,
      max_output_tokens: 200,
      tools: [
        {
          type: "function",
          name: "lookup_audit_code",
          description: "Lookup an audit verification record by region and numeric code.",
          parameters: {
            type: "object",
            properties: {
              region: { type: "string", enum: ["CN", "US"] },
              code: { type: "integer" },
            },
            required: ["region", "code"],
          },
        },
      ],
      tool_choice: "required",
      input: userMsg,
    };
  } else {
    payload = {
      model,
      max_tokens: 200,
      tools: [
        {
          type: "function",
          function: {
            name: "lookup_audit_code",
            description: "Lookup an audit verification record by region and numeric code.",
            parameters: {
              type: "object",
              properties: {
                region: { type: "string", enum: ["CN", "US"] },
                code: { type: "integer" },
              },
              required: ["region", "code"],
            },
          },
        },
      ],
      tool_choice: "required",
      messages: [{ role: "user", content: userMsg }],
    };
  }

  const r = await callUpstream(url, authHeaders(protocol, apiKey), payload);
  if (!r.ok) {
    return {
      id: "tool",
      title: "严格 Tool-Calling 结构校验",
      status: "fail",
      ms: r.ms,
      summary: `工具调用返回 HTTP ${r.status}`,
      evidence: { http_status: r.status, error: JSON.stringify(r.data).slice(0, 220) },
    };
  }
  let fnName = null;
  let args = null;
  if (protocol === "anthropic") {
    const tu = (r.data?.content || []).find((b) => b && b.type === "tool_use");
    if (tu) {
      fnName = tu.name;
      args = tu.input;
    }
  } else if (protocol === "openai-response") {
    const fc = (r.data?.output || []).find((b) => b && b.type === "function_call");
    if (fc) {
      fnName = fc.name;
      try {
        args = typeof fc.arguments === "string" ? JSON.parse(fc.arguments) : fc.arguments;
      } catch {
        args = fc.arguments;
      }
    }
  } else {
    const tc = r.data?.choices?.[0]?.message?.tool_calls?.[0];
    if (tc?.function) {
      fnName = tc.function.name;
      try {
        args = typeof tc.function.arguments === "string" ? JSON.parse(tc.function.arguments) : tc.function.arguments;
      } catch {
        args = tc.function.arguments;
      }
    }
  }
  const exact = fnName === "lookup_audit_code" && args && args.region === "CN" && args.code === 4096 && typeof args.code === "number";
  const loose = fnName === "lookup_audit_code" && args && String(args.region).toUpperCase() === "CN" && Number(args.code) === 4096;
  return {
    id: "tool",
    title: "严格 Tool-Calling 结构校验",
    status: exact ? "pass" : loose ? "warn" : "fail",
    ms: r.ms,
    summary: exact
      ? `原生结构化工具调用精准通过（region="CN", code=4096）`
      : loose
      ? "命中工具名但参数类型漂移（如整数被序列化为字符串）"
      : "未返回结构化 tool_use/tool_calls（部分伪装/转译渠道会吞掉工具调用）",
    evidence: { function_name: fnName, arguments: args, stop_reason: r.data?.stop_reason || r.data?.choices?.[0]?.finish_reason || r.data?.status },
  };
}

async function runCache(base, apiKey, model, protocol) {
  const url = endpointUrl(base, protocol);
  const makePayload = (q) => {
    if (protocol === "anthropic") {
      return {
        model,
        max_tokens: 24,
        temperature: 0,
        system: [{ type: "text", text: CACHE_PREFIX, cache_control: { type: "ephemeral" } }],
        messages: [{ role: "user", content: q }],
      };
    }
    if (protocol === "openai-response") {
      return { model, max_output_tokens: 32, instructions: CACHE_PREFIX, input: q };
    }
    return {
      model,
      max_tokens: 24,
      temperature: 0,
      messages: [
        { role: "system", content: CACHE_PREFIX },
        { role: "user", content: q },
      ],
    };
  };

  const r1 = await callUpstream(url, authHeaders(protocol, apiKey), makePayload("What is the route code for Clause 017? Reply with the code only."));
  if (!r1.ok) {
    return {
      id: "cache",
      title: "两轮 Prompt Cache 真命中",
      status: "fail",
      ms: r1.ms,
      summary: `首轮缓存预热失败（HTTP ${r1.status}）`,
      evidence: { http_status: r1.status, error: JSON.stringify(r1.data).slice(0, 220) },
    };
  }
  const r2 = await callUpstream(url, authHeaders(protocol, apiKey), makePayload("What is the route code for Clause 042? Reply with the code only."));
  if (!r2.ok) {
    return {
      id: "cache",
      title: "两轮 Prompt Cache 真命中",
      status: "fail",
      ms: r1.ms + r2.ms,
      summary: `次轮缓存复查失败（HTTP ${r2.status}）`,
      evidence: { http_status: r2.status, error: JSON.stringify(r2.data).slice(0, 220) },
    };
  }
  const u1 = r1.data?.usage || {};
  const u2 = r2.data?.usage || {};
  const cacheCreate = u1.cache_creation_input_tokens ?? 0;
  const cacheRead =
    u2.cache_read_input_tokens ??
    u2.input_tokens_details?.cached_tokens ??
    u2.prompt_tokens_details?.cached_tokens ??
    u2.prompt_cache_hit_tokens ??
    0;
  const hit = Number(cacheRead) > 0;
  return {
    id: "cache",
    title: "两轮 Prompt Cache 真命中",
    status: hit ? "pass" : "warn",
    ms: r1.ms + r2.ms,
    summary: hit
      ? `次轮实测命中缓存读 ${cacheRead} tokens（首轮写入 ${cacheCreate}）`
      : "次轮未返回缓存命中 token（可能该分组剥离了缓存头、走多账号轮询混池，或上游未达最小缓存阈值）",
    evidence: {
      turn1_usage: u1,
      turn2_usage: u2,
      cache_creation_t1: cacheCreate,
      cache_read_t2: cacheRead,
      answer_t2: extractText(protocol, r2.data).slice(0, 40),
    },
  };
}

export async function onRequestOptions() {
  return new Response(null, { status: 204, headers: CORS_HEADERS });
}

export async function onRequestGet() {
  return jsonRes({
    service: "API Price Radar — LLM Authenticity Verifier",
    docs: "https://agentied.work/verify/",
    source: "https://github.com/Guoz7/relay_price",
    privacy: "Stateless edge verification. API keys are forwarded in-memory only and never logged or stored.",
    method: "POST",
    request_schema: {
      base_url: "https://example-relay.com",
      api_key: "sk-...",
      model: "claude-sonnet-5-5",
      protocol: "auto | anthropic | openai-response | openai",
      checks: ["fingerprint", "thinking", "tool", "cache"],
    },
  });
}

export async function onRequestPost(context) {
  let body;
  try {
    body = await context.request.json();
  } catch {
    return jsonRes({ error: "请求体必须是合法 JSON" }, 400);
  }
  const base = normalizeBaseUrl(body?.base_url);
  const apiKey = String(body?.api_key || "").trim();
  const model = String(body?.model || "").trim();
  if (!base) {
    return jsonRes({ error: "base_url 必须是公网 HTTPS 域名（出于 SSRF 安全限制禁止内网与裸 IP）" }, 400);
  }
  if (!apiKey || apiKey.length < 6) {
    return jsonRes({ error: "请提供用于本次检测的 API Key（建议使用小额临时 Key，本接口不存日志）" }, 400);
  }
  if (!model || model.length > 120) {
    return jsonRes({ error: "请填写有效的目标模型名" }, 400);
  }
  const protocol = detectProtocol(model, body?.protocol);
  const reqChecks = Array.isArray(body?.checks) && body.checks.length ? body.checks : ["fingerprint", "thinking", "tool", "cache"];

  const results = [];
  if (reqChecks.includes("fingerprint")) results.push(await runFingerprint(base, apiKey, model, protocol));
  if (reqChecks.includes("thinking")) results.push(await runThinking(base, apiKey, model, protocol));
  if (reqChecks.includes("tool")) results.push(await runToolCall(base, apiKey, model, protocol));
  if (reqChecks.includes("cache")) results.push(await runCache(base, apiKey, model, protocol));

  const nPass = results.filter((x) => x.status === "pass").length;
  const nFail = results.filter((x) => x.status === "fail").length;
  const total = results.length || 1;
  const score = Math.round(
    results.reduce((acc, x) => acc + (x.status === "pass" ? 100 : x.status === "warn" ? 55 : 0), 0) / total
  );
  let level = "pass";
  let verdict = "高度符合原生协议与真机特征";
  if (nFail > 0 || score < 55) {
    level = "fail";
    verdict = "存在明显异常（疑似转译、接口不兼容、降级或套壳，建议核对下方原始证据）";
  } else if (nPass < total) {
    level = "warn";
    verdict = "基础调用正常，但部分高级特征缺失（如缓存未命中或缺少原生遥测字段，建议小额实测）";
  }

  return jsonRes({
    checked_at: new Date().toISOString(),
    base_url: base,
    model,
    protocol,
    score,
    level,
    verdict,
    passed: nPass,
    total,
    results,
    disclaimer: "黑盒技术指纹校验仅反映本次实测的协议与特征证据，不构成厂商背书；本接口不保存或记录任何 API Key。",
  });
}
