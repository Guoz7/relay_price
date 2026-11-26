"""Generate an Awesome-style live ranking Markdown block and sync it into dist/GITHUB_README.md + README.md.

Designed so GitHub Actions daily refreshes keep the public repository's README.md tables
up to date automatically while preserving static project documentation outside the markers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pricing as P
import scoring as S

MARKER_START = "<!-- AUTO-RANK-START -->"
MARKER_END = "<!-- AUTO-RANK-END -->"
_MARKER_RE = re.compile(rf"{re.escape(MARKER_START)}.*?{re.escape(MARKER_END)}", re.S)

FLAGSHIPS = [
    "deepseek-v4.1-flash",
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-fable-5-2",
    "gpt-6-astra",
    "gpt-6.1-sol",
    "gemini-4-carbon",
    "gpt-image-2.6-flare",
]


def render_rank_markdown(
    site: Any,
    model_pages: dict[str, dict[str, Any]],
    stations: dict[str, dict[str, Any]],
    n_stage0: int,
    helpers: dict[str, Any],
) -> str:
    money = helpers["money"]
    fmt_cst = helpers["fmt_cst"]
    base = site.base

    lines = [
        f"- **官网（每日自动更新）**：[{base}]({base}/)",
        f"- **最近构建**：{fmt_cst(site.now)}（北京时间）｜ 收录 **{len(stations)}** 家公开报价站 + **{n_stage0}** 家 Stage 0 仅建档闭源站，覆盖 **{len(model_pages)}** 个型号",
        "- **分层封顶**：未接入真金白银实测探针的站点统一 **Stage 1 封顶 B 级（74 分）**，绝不给未实测站点发 A/S 牌；常规推荐自动隔离自称 Kiro/Cursor/逆向/特惠及未核实极端低价分组。",
        "",
        "## 按使用场景选站与算账工具",
        "",
        f"- [月度成本算账器与主备选站助手（官方直连 vs 中转 vs 80/20 混合路由）]({base}/calc/)",
        f"- [LLM 模型真伪鉴别与协议自测工具（Thinking 签名 / Tool-Calling / 缓存验真）]({base}/verify/)",
        f"- [Claude Code 编程中转站推荐]({base}/scenes/claude-code/)（Anthropic 原生协议 + 7:2:1 缓存成本）",
        f"- [DeepSeek API 中转与性价比对比]({base}/scenes/deepseek/)（缓存读价 + 分时高低峰提示）",
        f"- [Codex / GPT-6 系列中转站推荐]({base}/scenes/codex-gpt/)（OpenAI Responses 协议 + 多分组价差）",
        f"- [Gemini 与生图 / 视频 API 推荐]({base}/scenes/gemini-image/)（Gemini 原生 + 按次生图/视频）",
        f"- [English Overview & USD Comparison]({base}/en/)",
        "",
        "## 旗舰模型常规推荐速览",
        "",
        "| 模型 | 收录站数 | 常规推荐（仅常规按量组） | 推荐价（输入/输出 ¥/1M） | 全网常规中位输入 | 完整比价 |",
        "| --- | ---: | --- | ---: | ---: | --- |",
    ]
    for canon in FLAGSHIPS:
        mp = model_pages.get(canon)
        if not mp:
            continue
        r = mp.get("rec")
        med = mp.get("median_in")
        if r:
            st = stations[r.domain]
            rec_s = f"{st['display_name']} (`{S.grade(r.score)} {r.score:.0f}`)"
            p_s = f"{money(r.input_cny)} / {money(r.output_cny)}" if r.kind == "token" else f"{money(r.call_cny)}/次"
        else:
            rec_s, p_s = "—", "—"
        med_s = "按次计费" if (r and r.kind == "call") else (money(med) if med is not None else "—")
        lines.append(f"| `{canon}` | {mp['n']} | {rec_s} | {p_s} | {med_s} | [查看全部 →]({base}/model/{mp['slug']}/) |")

    lines += [
        "",
        "## 站点信誉排行 Top 20",
        "",
        "| # | 站点 | 认证阶段 | 评级 / 分数 | 充值比例 | 支持协议 | 档案 |",
        "| ---: | --- | --- | --- | ---: | --- | --- |",
    ]
    top_sts = sorted(
        stations.items(),
        key=lambda kv: (-kv[1]["score"].stage, -kv[1]["score"].total, -kv[1]["score"].raw_total, kv[0]),
    )[:20]
    for i, (dom, st) in enumerate(top_sts, 1):
        sc: S.StationScore = st["score"]
        st_lbl = "Stage 2 实测" if (sc.stage == 2 and not sc.cap_reason) else ("Stage 2 降级" if sc.stage == 2 else "Stage 1 公开校验")
        tp = f"¥{st['topup_price']:g}/$1" if st.get("topup_price") is not None else "未知"
        eps = ", ".join(P.ENDPOINT_LABELS.get(e, e) for e in (st.get("endpoints") or [])[:4]) or "—"
        lines.append(
            f"| {i} | **{st['display_name']}** (`{dom}`) | {st_lbl} | `{sc.grade}` {sc.total:.0f} | {tp} | {eps} | [靠谱吗 →]({base}/site/{dom}/) |"
        )
    return "\n".join(lines)


def sync_readme_markers(readme_path: Path, body_md: str) -> None:
    """Replace content between AUTO-RANK markers in README.md when present."""
    if not readme_path.exists():
        return
    src = readme_path.read_text(encoding="utf-8")
    if MARKER_START not in src or MARKER_END not in src:
        return
    repl = f"{MARKER_START}\n{body_md}\n{MARKER_END}"
    updated = _MARKER_RE.sub(lambda _: repl, src, count=1)
    if updated != src:
        readme_path.write_text(updated, encoding="utf-8")


def export_github_readme(
    site: Any,
    model_pages: dict[str, dict[str, Any]],
    stations: dict[str, dict[str, Any]],
    n_stage0: int,
    helpers: dict[str, Any],
    out_paths: list[Path],
    readme_path: Path | None = None,
) -> None:
    body = render_rank_markdown(site, model_pages, stations, n_stage0, helpers)
    base = site.base
    full = "\n".join([
        f"# {site.name}（{base}）",
        "",
        f"> {site.cfg['description']}",
        "",
        body,
        "",
        "## 评分方法、收录与纠错",
        "",
        f"- 完整评分算法、Stage 0/1/2 分层规则与利益披露：[评分与计价方法]({base}/about/)",
        f"- 申请新站收录、提交价格纠错或要求下架：[提交收录 / 纠错]({base}/submit/)",
        f"- 实时调价动态订阅：[调价动态]({base}/changes/) ｜ [RSS Feed]({base}/changes.xml)",
        "",
    ])
    for p in out_paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(full, encoding="utf-8")
    if readme_path is not None:
        sync_readme_markers(readme_path, body)
