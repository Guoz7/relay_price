"""Domain extraction and noise filtering for tools/mine_sources.py (pure functions, unit-tested)."""
from __future__ import annotations

import ipaddress
import re
from typing import Iterable

URL_RE = re.compile(r"https?://([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)(?::\d+)?", re.I)
TLDS = ("com|cn|net|org|top|ai|io|xyz|cc|me|vip|pro|site|online|fun|icu|chat|dev|app|tech|club|cloud|ink|lol|link|run|"
        "space|store|live|work|shop|one|tw|hk|so|im|gg|to|best|plus|info|biz|moe|host|world|zone|life|asia|ltd|today|"
        "codes|tools|sh|ac|co|us|de|uk|jp|in|wang|love|kim|men|art|tv|fm|games|email|network|services|systems|center")
BARE_RE = re.compile(r"(?<![@\w.\-/])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:" + TLDS + r"))(?![\w\-.]*[\w\-])", re.I)
SECOND_LEVEL = {"com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn", "com.hk", "com.tw", "co.uk", "co.jp", "com.au"}
# Multi-tenant hosting roots: subdomains belong to different owners, so the full host is its own root.
TENANT_ROOTS = ("github.io", "vercel.app", "netlify.app", "pages.dev", "workers.dev", "onrender.com", "fly.dev",
                "railway.app", "zeabur.app", "hf.space", "deno.dev", "herokuapp.com", "us.kg", "dpdns.org", "qzz.io", "eu.org")

# Roots that are never relay stations: model vendors, code hosts, social/IM, docs, CDNs, image hosts, chat clients, search.
BLOCK_ROOTS = frozenset("""
github.com githubusercontent.com github.io gitee.com gitlab.com gitcode.com gitcode.net bitbucket.org sourceforge.net
openai.com chatgpt.com anthropic.com claude.ai claude.com x.ai grok.com google.com googleapis.com gstatic.com google.dev
deepseek.com moonshot.cn moonshot.ai kimi.com bigmodel.cn zhipuai.cn aliyun.com aliyuncs.com alibabacloud.com qwen.ai
baidu.com bdstatic.com volcengine.com byteplus.com bytedance.com doubao.com tencent.com tencentcloud.com qq.com
minimax.io minimaxi.com mistral.ai cohere.com perplexity.ai together.ai groq.com openrouter.ai siliconflow.cn siliconflow.com
azure.com microsoft.com windows.net live.com office.com bing.com apple.com amazon.com amazonaws.com
cloudflare.com cloudflareinsights.com jsdelivr.net unpkg.com cdnjs.com bootcdn.net staticfile.org loli.net elemecdn.com
bilibili.com hdslb.com zhihu.com zhimg.com weibo.com douyin.com xiaohongshu.com xhslink.com csdn.net juejin.cn cnblogs.com
jianshu.com segmentfault.com oschina.net v2ex.com linux.do nodeseek.com hostloc.com 52pojie.cn reddit.com
twitter.com x.com t.me telegram.org telegram.me discord.com discord.gg youtube.com youtu.be facebook.com instagram.com tiktok.com
wikipedia.org mozilla.org w3.org schema.org json-schema.org ietf.org npmjs.com npmjs.org pypi.org python.org nodejs.org docker.com
huggingface.co modelscope.cn kaggle.com readthedocs.io readthedocs.org gitbook.io gitbook.com mintlify.com notion.so notion.site
feishu.cn larksuite.com yuque.com shimo.im kdocs.cn wps.cn shields.io star-history.com laobi.icu
imgur.com sm.ms imgse.com postimg.cc ibb.co imgbb.com picgo.net freeimage.host
lobehub.com nextchat.dev chatboxai.app cherry-ai.com sillytavern.app openwebui.com librechat.ai
cursor.com cursor.sh windsurf.com codeium.com trae.ai kiro.dev cline.bot roocode.com
example.com example.org test.com domain.com yourdomain.com your-domain.com
beian.gov.cn miit.gov.cn 12377.cn alipay.com taobao.com tmall.com jd.com paypal.com stripe.com
ko-fi.com afdian.com afdian.net patreon.com buymeacoffee.com
tranco-list.eu umbrella.com similarweb.com icann.org rdap.org
google-analytics.com googletagmanager.com doubleclick.net clarity.ms umami.is cnzz.com
vercel.com netlify.com render.com railway.com zeabur.com fly.io new-api.pro newapi.ai one-api.com
""".split())
SKIP_PREFIXES = ("localhost", "img.", "image.", "cdn.", "static.", "assets.", "docs.", "doc.", "status.", "blog.", "wiki.",
                 "help.", "mail.", "chat.", "uptime.", "monitor.", "pic.", "file.", "files.", "s3.")


def norm(host: str) -> str:
    h = (host or "").strip().lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def root_of(host: str) -> str:
    for tr in TENANT_ROOTS:
        if host.endswith("." + tr):
            return host
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def acceptable(host: str, extra_block: frozenset[str] | set[str] = frozenset()) -> bool:
    """True when `host` could be a relay station (not an IP/localhost/known non-relay root)."""
    if not host or "." not in host or len(host) > 80 or is_ip(host) or host.endswith((".local", ".lan", ".internal")):
        return False
    if host.startswith(SKIP_PREFIXES):
        return False
    r = root_of(host)
    if r in BLOCK_ROOTS or host in BLOCK_ROOTS or r in extra_block or host in extra_block:
        return False
    if r == host and any(host.endswith("." + b) for b in BLOCK_ROOTS):  # e.g. foo.github.io
        return False
    tld = host.rsplit(".", 1)[-1]
    return tld.isalpha() and 2 <= len(tld) <= 12


def extract_hosts(text: str, bare: bool = True) -> set[str]:
    """Hosts in URLs (and optionally bare domain mentions) inside `text`."""
    out = {norm(m.group(1)) for m in URL_RE.finditer(text or "")}
    if bare:
        out |= {norm(m.group(1)) for m in BARE_RE.finditer(text or "")}
    return {h for h in out if h}


def candidates(text: str, *, self_roots: Iterable[str] = (), bare: bool = True,
               extra_block: frozenset[str] | set[str] = frozenset()) -> set[str]:
    selfs = set(self_roots)
    return {h for h in extract_hosts(text, bare) if acceptable(h, extra_block) and root_of(h) not in selfs}


def collapse(hosts: Iterable[str], known: set[str]) -> dict[str, list[str]]:
    """Group new hosts by root domain; skip roots already known. Returns {preferred_host: [all hosts seen]}.

    The bare root is preferred (probe adds api./www. itself); otherwise the shortest host seen.
    """
    known_roots = {root_of(k) for k in known}
    by_root: dict[str, set[str]] = {}
    for h in hosts:
        r = root_of(h)
        if h in known or r in known_roots:
            continue
        by_root.setdefault(r, set()).add(h)
    out: dict[str, list[str]] = {}
    for r, hs in by_root.items():
        pref = r if r in hs or all(h.startswith(("api.", "www.")) for h in hs) else sorted(hs, key=lambda x: (len(x), x))[0]
        out[pref] = sorted(hs)
    return out
