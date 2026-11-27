"""Public-suffix guard for shared hosting platforms and multi-level registries (stdlib only).

A naive "last two labels" root turns `jbbt.pages.dev` into `pages.dev` and `api.foo.it.com` into `it.com`,
so a fresh station silently inherits Cloudflare's or a 1990s registry's domain age and traffic rank.
This module keeps a small curated suffix list (no network, no third-party PSL package) and repairs intel entries.
"""
from __future__ import annotations

from typing import Any

CN_PROVINCES = (
    "bj", "sh", "tj", "cq", "he", "sx", "nm", "ln", "jl", "hl", "js", "zj", "ah", "fj", "jx", "sd", "ha", "hb",
    "hn", "gd", "gx", "hi", "sc", "gz", "yn", "xz", "sn", "gs", "qh", "nx", "xj", "tw", "hk", "mo",
)

MULTI_LEVEL_TLDS: frozenset[str] = frozenset({
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "co.uk", "org.uk", "me.uk", "co.jp", "ne.jp", "or.jp",
    "com.hk", "com.tw", "org.tw", "com.sg", "com.au", "co.kr", "co.nz", "co.in",
    "com.br", "com.mx", "com.tr", "com.my", "com.vn", "co.id", "my.id", "web.id", "biz.id",
    "it.com", "eu.org", "us.kg", "edu.kg", "xx.kg", "dpdns.org", "qzz.io", "indevs.in",
    "uk.com", "us.com", "cn.com", "de.com", "jp.net", "ru.com", "br.com", "sa.com", "uk.net", "gb.net", "za.com",
    "ggff.net", "de5.net", "cc.cd", "us.ci",
} | {f"{p}.cn" for p in CN_PROVINCES})

SHARED_HOST_SUFFIXES: frozenset[str] = frozenset({
    "pages.dev", "workers.dev", "vercel.app", "netlify.app", "github.io", "gitlab.io", "hf.space", "zeabur.app",
    "onrender.com", "railway.app", "up.railway.app", "fly.dev", "herokuapp.com", "azurewebsites.net", "appspot.com",
    "web.app", "firebaseapp.com", "trycloudflare.com", "ngrok-free.app", "ngrok.io", "duckdns.org", "ddns.net",
    "hopto.org", "sytes.net", "serveo.net", "cloudns.ch", "cloudns.org", "cloudns.be", "cloudns.asia", "cloudns.nz",
    "nyc.mn", "hidns.co", "dynv6.net", "is-a.dev", "js.org", "deno.dev", "replit.dev", "repl.co", "clawcloudrun.com",
    "koyeb.app", "glitch.me", "pythonanywhere.com", "streamlit.app", "dedyn.io",
})

REGISTRY_FIELDS = (
    "domain_age_days", "domain_registered_at", "domain_expires_at", "registrar",
    "tranco_rank", "umbrella_rank", "majestic_rank",
)


def _norm(domain: Any) -> str:
    d = str(domain or "").strip().lower()
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/", 1)[0].split(":", 1)[0].strip(".")
    return d[4:] if d.startswith("www.") else d


def _longest_suffix(d: str, suffixes: frozenset[str]) -> str | None:
    best = None
    for s in suffixes:
        if d.endswith("." + s) and (best is None or len(s) > len(best)):
            best = s
    return best


def shared_host_of(domain: Any) -> str | None:
    """Shared platform suffix when `domain` is a tenant subdomain (jbbt.pages.dev -> "pages.dev"), else None."""
    return _longest_suffix(_norm(domain), SHARED_HOST_SUFFIXES)


def multi_level_suffix_of(domain: Any) -> str | None:
    """Multi-level public suffix the domain sits under (api.x.it.com -> "it.com"), else None."""
    return _longest_suffix(_norm(domain), MULTI_LEVEL_TLDS)


def registrable_root(domain: Any) -> str:
    """Owner-level root: one label under a shared host or multi-level suffix, else the last two labels."""
    d = _norm(domain)
    parts = [p for p in d.split(".") if p]
    suffix = shared_host_of(d) or multi_level_suffix_of(d)
    if suffix:
        n = suffix.count(".") + 2
        return ".".join(parts[-n:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


def sanitize_intel_entry(domain: Any, entry: dict[str, Any] | None) -> dict[str, Any]:
    """Copy of an intel entry with inherited platform / registry signals removed (idempotent)."""
    out = dict(entry or {})
    d = _norm(domain)
    sh = shared_host_of(d)
    if sh:
        out["shared_host"] = sh
        out["root"] = registrable_root(d)
        for k in REGISTRY_FIELDS:
            out[k] = None
        return out
    out.pop("shared_host", None)
    ml = multi_level_suffix_of(d)
    root = out.get("root")
    if ml and isinstance(root, str) and root.strip(".").lower() in (ml, ml.split(".")[-1]):
        out["root"] = registrable_root(d)
        for k in REGISTRY_FIELDS:
            out[k] = None
        out["net_checked_at"] = None
    return out


def sanitize_intel(intel: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {d: sanitize_intel_entry(d, e) for d, e in (intel or {}).items()}