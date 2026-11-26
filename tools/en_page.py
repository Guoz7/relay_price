"""English landing page (/en/) for overseas developers arriving from GitHub, Hacker News, Reddit & Dev.to."""

from __future__ import annotations

from html import escape as E
from typing import Any

import model_specs as MS
import pricing as P
import scoring as S

EN_FLAGSHIPS = [
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-fable-5-2",
    "gpt-6-astra",
    "gpt-6.1-sol",
    "deepseek-v4.1-flash",
    "deepseek-v4-pro",
    "gemini-4-carbon",
]


def build_en(
    site: Any,
    model_pages: dict[str, dict[str, Any]],
    stations: dict[str, dict[str, Any]],
    specs: dict[str, dict[str, Any]],
    n_stage0: int,
    helpers: dict[str, Any],
) -> None:
    usd_rate = float(site.cfg.get("usd_cny_rate", 7.25))
    money = helpers["money"]
    grade_badge = helpers["grade_badge"]

    trs = []
    for canon in EN_FLAGSHIPS:
        mp = model_pages.get(canon)
        if not mp:
            continue
        r = mp.get("rec")
        sp = specs.get(canon) or {}
        ctx = MS.fmt_ctx(sp.get("context")) or "—"
        off_s = f"${sp['in_usd']:g} / ${sp['out_usd']:g}" if (sp.get("in_usd") is not None and sp.get("out_usd") is not None) else "—"
        if r and r.input_cny is not None:
            st = stations[r.domain]
            in_usd_eff = r.input_cny / usd_rate
            out_usd_eff = (r.output_cny or r.input_cny) / usd_rate
            rel_s = f"${in_usd_eff:.3g} / ${out_usd_eff:.3g} <small>({money(r.input_cny)} / {money(r.output_cny)})</small>"
            disc = (in_usd_eff / sp["in_usd"]) if (sp.get("in_usd") and sp["in_usd"] > 0) else None
            disc_cls = "warn" if (disc is not None and (disc < 0.15 or disc > 1.05)) else "ok"
            disc_s = f'<span class="badge {disc_cls}">{disc * 100:.0f}% of official</span>' if disc is not None else "—"
            rec_s = f'<a href="/site/{E(r.domain)}/">{E(st["display_name"])}</a> {grade_badge(r.score)}'
        else:
            rel_s, disc_s, rec_s = "—", "—", "—"
        trs.append(
            f'<tr><td class="mn"><a href="/model/{mp["slug"]}/"><code>{E(canon)}</code></a></td>'
            f'<td><span class="badge ep">{E(ctx)}</span></td>'
            f'<td class="n">{off_s}</td>'
            f'<td class="n hl">{rel_s}</td>'
            f'<td class="n">{disc_s}</td>'
            f'<td class="st">{rec_s}</td></tr>'
        )

    body = f"""
<section class="hero sm">
  <p class="eyebrow">Open-Source AI API Relay Directory &amp; Honest Benchmark · English Overview</p>
  <h1>API Price Radar (<code>agentied.work</code>)</h1>
  <p class="lead">Daily read-only pricing &amp; trust tracker across <b>{len(stations)}</b> public AI API relays (+ {n_stage0} closed Stage-0 archives) and <b>{len(model_pages)}</b> models. Built to stop bait-and-switch rankings: unprobed relays are hard-capped at <b>Stage 1 (Grade B / 74)</b>, multi-group spreads are expanded in full, and coding workloads can toggle <b>7:2:1 Prompt Cache TCO</b>.</p>
  <div class="chips">
    <a class="chip" href="/verify/">LLM Authenticity Verifier (Thinking Sig / Cache) →</a>
    <a class="chip" href="/calc/">Monthly TCO &amp; Routing Calculator →</a>
    <a class="chip" href="/scenes/claude-code/">Claude Code (Anthropic + Cache)</a>
    <a class="chip" href="/scenes/codex-gpt/">Codex / GPT-6 (Responses API)</a>
    <a class="chip" href="/scenes/deepseek/">DeepSeek Comparison</a>
    <a class="chip" href="https://github.com/Guoz7/relay_price" target="_blank" rel="noopener">GitHub Open Source →</a>
  </div>
</section>

<section>
  <h2>Flagship Models: Official API vs. Standard-Lane Relays <small>Per 1M tokens (USD equivalent @ 1 USD = ¥{usd_rate:g})</small></h2>
  <div class="tbl-wrap"><table class="qt">
    <thead><tr><th>Model</th><th>Context</th><th class="n">Official API (In/Out)</th><th class="n">Best Standard Relay</th><th class="n">vs. Official</th><th>Recommended Station</th></tr></thead>
    <tbody>{"".join(trs)}</tbody>
  </table></div>
</section>

<section class="prose">
  <h2>Why We Built This (Data Audit Findings Across 850 Relays)</h2>
  <ul>
    <li><b>Cheapest-first rankings mislead 83% of the time</b>: Across 6 flagship models, naive price-first rankings promoted self-declared reverse/Kiro/test groups or extreme &lt;0.25× outliers into Top 3.</li>
    <li><b>Same-station multi-group spreads reach 3×–6.25×</b>: 87.2% of relays expose multiple paid groups per model. Collapsing to the cheapest group hides whether a station is mixing reverse pools with official channels. We expand every group transparently.</li>
    <li><b>No S/A grades without paid real-call probing</b>: Passive metadata (uptime + domain age) cannot distinguish a 95% reliable relay from a 50% one. Unprobed stations stay capped at <b>Stage 1 (Grade B / 74)</b>. Only relays passing continuous paid micro-probes (<code>tools/probes.py</code>: TTFT/TPOT, tool-calling, 2-turn prompt cache hit, and real billing reconciliation) unlock <b>Stage 2 (A/S)</b>.</li>
    <li><b>Open-source model metadata</b>: Context windows, capabilities, and official baseline USD prices are synced from open-source registries (<code>anomalyco/models.dev</code> &amp; <code>BerriAI/litellm</code>).</li>
  </ul>
  <p>Full source code, scoring formulas, and submission/correction workflow: <a href="https://github.com/Guoz7/relay_price" target="_blank" rel="noopener">github.com/Guoz7/relay_price</a> · <a href="/about/">Methodology &amp; Affiliation Disclosure</a> · <a href="/">Chinese Main Site</a>.</p>
</section>"""
    site.page(
        "/en/",
        "API Price Radar — Open-Source AI API Relay Pricing, Cache TCO & Trust Benchmark",
        f"Compare {len(stations)} AI API relays across Claude, GPT-6, DeepSeek & Gemini with official discount ratios (models.dev/LiteLLM), protocol filters, 7:2:1 prompt-cache TCO, and Stage 0/1/2 honest grading.",
        body,
        crumbs=[("English", "/en/")],
        priority=0.8,
        lang="en",
    )
