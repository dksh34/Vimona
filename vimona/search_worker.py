"""Zero-trace ephemeral web reference puller driven via headless Firefox in `/dev/shm` RAM.

Replaces the legacy persistent burner profile:
1. Purges any leftover `~/firefox-burner-profile` from older builds.
2. Allocates a single-prompt temporary directory on Linux `/dev/shm` RAM disk
   (falling back to OS temp only if `/dev/shm` is unavailable).
3. Launches Playwright headless Firefox in memory-only private mode pointed
   exclusively at that disposable directory (`browser.cache.disk.enable=False`).
4. Searches the web, inspects top pages inside the sandboxed browser, pulls
   candidate reference images strictly into Python RAM buffers (never saved to
   browser disk cache), extracts local lighting/color/palette/style tokens +
   Safe-Blur flags, and then destroys Firefox + runs `scorched_earth_wipe()`
   before returning control so offline rendering begins with zero browser trace.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import ipaddress
import json
import os
import re
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

_MAX_RESULTS_CAP = 10
_MAX_IMAGES_CAP = 8
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_PIXELS = 16_000_000
_MAX_IMAGE_DIM = 4096
_MAX_RESEARCH_BYTES = 24 * 1024 * 1024
_MAX_NAVIGATIONS = 8
_SAFESEARCH_KP_MAP = {"strict": "1", "moderate": "-1", "off": "-2"}

from .airgap import (
    _read_proc_start_ticks,
    collect_descendant_pids,
    detect_ram_persistence_risk,
    is_pid_alive_with_start,
    kill_process_tree_verified,
    purge_legacy_burner_profile,
    scorched_earth_wipe,
)
from .safety_ml import get_safety_ml_engine
from .safety_patterns import is_explicit_text

COMFY_VENV_PYTHON = Path("~/comfyui/venv/bin/python").expanduser()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHM_DIR = Path("/dev/shm")

_MEMORY_ONLY_FIREFOX_PREFS: dict[str, Any] = {
    "browser.cache.disk.enable": False,
    "browser.cache.disk.capacity": 0,
    "browser.cache.disk_cache_ssl": False,
    "browser.cache.offline.enable": False,
    "browser.cache.memory.enable": True,
    "browser.privatebrowsing.autostart": True,
    "places.history.enabled": False,
    "browser.formfill.enable": False,
    "browser.sessionstore.resume_from_crash": False,
    "browser.sessionstore.interval": 86400000,
    "dom.storage.next_gen": False,
    "privacy.sanitize.sanitizeOnShutdown": True,
    "privacy.clearOnShutdown.cache": True,
    "privacy.clearOnShutdown.cookies": True,
    "privacy.clearOnShutdown.history": True,
    "privacy.clearOnShutdown.offlineApps": True,
    "privacy.clearOnShutdown.sessions": True,
    "toolkit.telemetry.enabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "app.update.enabled": False,
}


_INJECTION_MARKER_RE = re.compile(
    r"(?i)(?:<\|[^|>]{1,40}\|>|</?tool_call>|^\s*(?:system|assistant|developer)\s*:|"
    r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions)"
)


def _clean_text(raw: str, max_len: int = 320) -> str:
    # Strip control/zero-width chars to mitigate prompt injection (`F11`, `F12`)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\ufeff]", " ", raw or "")
    collapsed = re.sub(r"\s+", " ", cleaned).strip()
    if len(collapsed) > max_len:
        return collapsed[: max_len - 1].rstrip() + "…"
    return collapsed


def sanitize_untrusted_web_text(raw: str, max_len: int = 320) -> str:
    """Neutralize prompt-injection / tool-spoofing markers in scraped web text (`F11`, `F12`)."""
    txt = _clean_text(raw, max_len=max_len)
    return _INJECTION_MARKER_RE.sub("[filtered-directive]", txt)


def _is_non_public_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        return _is_non_public_ip(ip_obj.ipv4_mapped)
    return bool(
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or not ip_obj.is_global
    )


def validate_public_http_url(
    url_str: str,
    *,
    resolve_dns: bool = True,
    pinned_hosts: dict[str, set[str]] | None = None,
) -> tuple[bool, str, set[str]]:
    """Validate public HTTP/HTTPS destinations and block SSRF / DNS rebinding (`F1`–`F4`)."""
    raw = (url_str or "").strip()
    if not raw:
        return (False, "Empty URL", set())
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return (False, "Malformed URL", set())

    if parsed.scheme.lower() not in ("http", "https"):
        return (False, f"Unsupported scheme '{parsed.scheme}' (only http/https permitted)", set())
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        return (False, "Credentials in URL authority are blocked", set())
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return (False, "Missing hostname", set())
    if host in ("localhost", "ip6-localhost", "ip6-loopback") or host.endswith(
        (".local", ".localhost", ".internal", ".localdomain", ".home", ".lan", ".corp", ".arpa")
    ):
        return (False, f"Blocked local/internal host '{host}'", set())

    try:
        port = parsed.port
        if port is not None and not (1 <= int(port) <= 65535):
            return (False, "Invalid port", set())
    except ValueError:
        return (False, "Invalid port", set())

    resolved_ips: set[str] = set()
    # Direct IP literal check
    try:
        lit = ipaddress.ip_address(host)
        if _is_non_public_ip(lit):
            return (False, f"Blocked non-public IP literal '{host}'", set())
        resolved_ips.add(str(lit))
    except ValueError:
        if resolve_dns:
            try:
                infos = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            except OSError as exc:
                return (False, f"DNS resolution failed for '{host}': {exc}", set())
            if not infos:
                return (False, f"No DNS records for '{host}'", set())
            for _fam, _st, _pr, _cn, sockaddr in infos:
                ip_txt = str(sockaddr[0]).split("%", 1)[0]
                try:
                    ip_obj = ipaddress.ip_address(ip_txt)
                except ValueError:
                    return (False, f"Unparseable resolved IP '{ip_txt}'", set())
                if _is_non_public_ip(ip_obj):
                    return (False, f"Blocked resolution of '{host}' -> private/local IP {ip_txt}", set())
                resolved_ips.add(str(ip_obj))

            # DNS rebinding defense (`F3`): ensure hostname did not rebind to completely new IP set mid-session
            if pinned_hosts is not None:
                prev = pinned_hosts.get(host)
                if prev is not None and resolved_ips and not (resolved_ips & prev):
                    return (False, f"DNS rebind detected for '{host}' ({prev} -> {resolved_ips})", set())
                if resolved_ips:
                    pinned_hosts.setdefault(host, set()).update(resolved_ips)

    return (True, "OK", resolved_ips)


def sniff_image_format(raw_bytes: bytes) -> str | None:
    """Identify raster image format strictly from binary magic bytes (`F8`)."""
    if len(raw_bytes) < 12:
        return None
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
        return "webp"
    return None


def _alloc_ephemeral_ram_dir(
    *, allow_disk_fallback: bool = False
) -> tuple[Path, Any, str, dict[str, Any]]:
    """Create a private `0700` flock-locked dir on `/dev/shm` tmpfs (never label disk `/tmp` as RAM-only)."""
    use_shm = SHM_DIR.is_dir() and os.access(SHM_DIR, os.W_OK)
    if use_shm:
        risk = detect_ram_persistence_risk(SHM_DIR)
        if not risk["is_ram_backed"] and not allow_disk_fallback:
            raise RuntimeError(
                f"{SHM_DIR} is backed by '{risk['fs_type']}' rather than tmpfs/ramfs; refusing to run without opt-in."
            )
        p = Path(tempfile.mkdtemp(prefix=f"vimona_ephem_{os.getpid()}_", dir=str(SHM_DIR)))
        profile_kind = (
            "/dev/shm tmpfs"
            if risk["is_ram_backed"]
            else f"disk-backed {SHM_DIR} ({risk['fs_type']})"
        )
    else:
        if not allow_disk_fallback:
            raise RuntimeError(
                "/dev/shm is unavailable and disk-backed /tmp fallback is disabled for privacy."
            )
        tmp_base = Path(tempfile.gettempdir())
        p = Path(tempfile.mkdtemp(prefix=f"vimona_ephem_{os.getpid()}_", dir=str(tmp_base)))
        risk = detect_ram_persistence_risk(p)
        profile_kind = (
            "OS temp (tmpfs)"
            if risk["is_ram_backed"]
            else f"DISK-BACKED OS temp ({risk['fs_type']} — NOT RAM-only)"
        )

    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    lock_fh = open(p / ".worker.lock", "a+b")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pass
    try:
        (p / ".worker_owner.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "uid": os.getuid() if hasattr(os, "getuid") else 0,
                    "start_ticks": _read_proc_start_ticks(os.getpid()),
                    "created_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    return p, lock_fh, profile_kind, risk


def _describe_rgb_color(r: float, g: float, b: float) -> str:
    """Map an RGB triple (0-255) to a concise visual palette token."""
    mx = max(r, g, b)
    mn = min(r, g, b)
    if mx < 42:
        return "obsidian black"
    if mn > 215:
        return "crisp silver white"
    if (mx - mn) < 22:
        return "charcoal slate" if mx < 120 else "cool misty gray"
    if r >= g and r >= b:
        if g > 155 and b < 95:
            return "warm golden amber"
        if g > 110 and b > 140:
            return "neon magenta pink"
        return "crimson neon red" if mx > 165 else "deeprust crimson"
    if g >= r and g >= b:
        if b > 145:
            return "electric cyan teal"
        return "emerald neon green" if mx > 150 else "deep forest green"
    # b dominant
    if r > 145:
        return "electric violet purple"
    if g > 155:
        return "electric cyan blue"
    return "deep cobalt blue" if mx > 130 else "midnight navy blue"


def analyze_image_bytes_in_ram(
    raw_bytes: bytes,
    alt_text: str = "",
    page_title: str = "",
    query: str = "",
) -> dict[str, Any] | None:
    """Decode image purely from RAM bytes, extract lighting/palette tokens, & Safe-Blur flag."""
    import numpy as np
    from PIL import Image, ImageOps

    raw_fmt = sniff_image_format(raw_bytes)
    if raw_fmt is None:
        return None

    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
            if w < 180 or h < 180 or w > _MAX_IMAGE_DIM or h > _MAX_IMAGE_DIM or (w * h) > _MAX_IMAGE_PIXELS:
                return None
            # Reject extreme banner/divider strips (e.g. 10:1 nav bars)
            aspect = max(w / max(1, h), h / max(1, w))
            if aspect > 4.2:
                return None
            im = ImageOps.exif_transpose(im)
            w, h = im.size
            if w > _MAX_IMAGE_DIM or h > _MAX_IMAGE_DIM or (w * h) > _MAX_IMAGE_PIXELS:
                return None

            rgb_im = im.convert("RGB")
            # Resize normalized reference copy in RAM (max edge 768px) for fast groundings & previews
            ref_im = rgb_im.copy()
            ref_im.thumbnail((768, 768), Image.Resampling.LANCZOS)
            ref_w, ref_h = ref_im.size
            out_buf = io.BytesIO()
            ref_im.save(out_buf, format="JPEG", quality=88)
            norm_b64 = base64.b64encode(out_buf.getvalue()).decode("ascii")

            # Fast 64x64 NumPy feature + YCbCr explicit-exposure inspection
            small = rgb_im.resize((64, 64), Image.Resampling.BILINEAR)
            arr = np.asarray(small, dtype=np.float32)
    except Exception:
        return None

    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    mean_luma = float(np.mean(luma))
    contrast = float(np.std(luma))
    max_rgb = np.maximum(np.maximum(r, g), b)
    min_rgb = np.minimum(np.minimum(r, g), b)
    sat = np.where(max_rgb > 1.0, (max_rgb - min_rgb) / np.maximum(max_rgb, 1.0), 0.0)
    mean_sat = float(np.mean(sat))

    # Dominant 4 quantized colors via spatial subsampling / Pillow median cut
    quant = small.quantize(colors=4, method=Image.Quantize.MEDIANCUT)
    pal_raw = quant.getpalette() or []
    counts = sorted(quant.getcolors() or [], reverse=True)
    palette_tags: list[str] = []
    seen_names: set[str] = set()
    for _cnt, idx in counts[:4]:
        base = idx * 3
        if base + 2 < len(pal_raw):
            cr, cg, cb = float(pal_raw[base]), float(pal_raw[base + 1]), float(pal_raw[base + 2])
            cname = _describe_rgb_color(cr, cg, cb)
            if cname not in seen_names:
                seen_names.add(cname)
                palette_tags.append(cname)

    # Lighting / mood analysis
    mood_parts: list[str] = []
    if mean_luma < 82:
        mood_parts.append("low-key nocturnal lighting")
    elif mean_luma > 175:
        mood_parts.append("bright high-key daylight")
    else:
        mood_parts.append("balanced cinematic lighting")

    if contrast > 64:
        mood_parts.append("high-contrast dramatic shadows")
    elif contrast < 36:
        mood_parts.append("soft diffused atmosphere")

    if mean_sat > 0.42:
        mood_parts.append("vivid saturated highlights")
    elif mean_sat < 0.14:
        mood_parts.append("muted desaturated tones")

    warm_bias = float(np.mean(r - b))
    if warm_bias > 22:
        mood_parts.append("warm tungsten amber cast")
    elif warm_bias < -20:
        mood_parts.append("cool cyan-blue atmosphere")

    lighting_mood = ", ".join(mood_parts[:3])

    # Dedicated SafeBlur Micro-ML Vision + Context classification BEFORE returning to UI
    combined_txt = f"{alt_text} {page_title} {query}".strip()
    is_explicit, _sc = get_safety_ml_engine().classify_image_rgb(
        np.clip(arr, 0, 255).astype(np.uint8),
        context_text=combined_txt,
    )

    style_tokens = ", ".join(
        p for p in [lighting_mood, ", ".join(palette_tags[:3]), _clean_text(alt_text, 90)] if p
    )
    return {
        "width": ref_w,
        "height": ref_h,
        "palette_tags": palette_tags,
        "lighting_mood": lighting_mood,
        "style_tokens": style_tokens,
        "explicit": is_explicit,
        "image_bytes_b64": norm_b64,
    }


def _extract_ddg_html_results(page: Any, limit: int = 5) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    cards = page.locator(".result")
    count = min(limit, cards.count())
    for i in range(count):
        card = cards.nth(i)
        title_loc = card.locator(".result__a").first
        snip_loc = card.locator(".result__snippet").first
        url_loc = card.locator(".result__url").first

        title = _clean_text(title_loc.inner_text() if title_loc.count() else "", 120)
        href = title_loc.get_attribute("href") if title_loc.count() else ""
        disp_url = _clean_text(url_loc.inner_text() if url_loc.count() else "", 120)
        snippet = _clean_text(snip_loc.inner_text() if snip_loc.count() else "", 280)

        if href and "uddg=" in href:
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if "uddg" in parsed and parsed["uddg"]:
                href = parsed["uddg"][0]

        final_url = href or disp_url
        if title and final_url:
            items.append(
                {
                    "title": title,
                    "url": final_url,
                    "takeaway": snippet or "Reference page inspected in /dev/shm sandbox.",
                }
            )
    return items


def _collect_page_candidate_images(page: Any, page_url: str, max_per_page: int = 4) -> list[dict[str, str]]:
    """Inspect a loaded page DOM for candidate visual references + alt metadata."""
    try:
        candidates = page.evaluate(
            """() => {
                const seen = new Set();
                const out = [];
                const push = (src, alt) => {
                    if (!src || src.startsWith('data:') || src.endsWith('.svg')) return;
                    const low = src.toLowerCase();
                    if (low.includes('favicon') || low.includes('logo') || low.includes('sprite') || low.includes('icon')) return;
                    if (seen.has(src)) return;
                    seen.add(src);
                    out.push({ src, alt: (alt || '').trim() });
                };
                for (const sel of [
                    "meta[property='og:image']",
                    "meta[name='twitter:image']",
                    "meta[property='og:image:secure_url']"
                ]) {
                    const el = document.querySelector(sel);
                    if (el && el.content) push(el.content, document.title || '');
                }
                const imgs = Array.from(document.querySelectorAll('img'));
                imgs.sort((a, b) => ((b.naturalWidth || 0) * (b.naturalHeight || 0)) - ((a.naturalWidth || 0) * (a.naturalHeight || 0)));
                for (const img of imgs) {
                    const src = img.currentSrc || img.getAttribute('src') || img.getAttribute('data-src') || '';
                    const alt = img.getAttribute('alt') || img.getAttribute('title') || document.title || '';
                    push(src, alt);
                }
                return out.slice(0, 12);
            }"""
        )
    except Exception:
        return []

    out: list[dict[str, str]] = []
    for item in candidates or []:
        raw_src = str(item.get("src", "")).strip()
        if not raw_src:
            continue
        abs_url = urllib.parse.urljoin(page_url, raw_src)
        if not abs_url.startswith(("http://", "https://")):
            continue
        out.append({"image_url": abs_url, "alt": _clean_text(str(item.get("alt", "")), 140)})
        if len(out) >= max_per_page:
            break
    return out


def _collect_ddg_image_results(
    page: Any,
    query: str,
    limit: int = 8,
    *,
    safesearch: str = "moderate",
) -> list[dict[str, str]]:
    """Query DuckDuckGo Images inside the isolated RAM tab with explicit SafeSearch (`F13`)."""
    encoded = urllib.parse.quote_plus(query)
    kp_val = _SAFESEARCH_KP_MAP.get(safesearch, "-1")
    url = f"https://duckduckgo.com/?q={encoded}&kp={kp_val}&iax=images&ia=images"
    candidates: list[dict[str, str]] = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1600)
        raw_items = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                for (const img of document.querySelectorAll('img')) {
                    let src = img.currentSrc || img.getAttribute('src') || img.getAttribute('data-src') || '';
                    if (!src) continue;
                    if (src.startsWith('//')) src = 'https:' + src;
                    if (!src.startsWith('http')) continue;
                    // DDG proxies thumbnails at external-content.duckduckgo.com/iu/?u=...
                    if (seen.has(src)) continue;
                    seen.add(src);
                    const alt = (img.getAttribute('alt') || '').trim();
                    out.push({ src, alt });
                }
                return out.slice(0, 16);
            }"""
        )
        for item in raw_items or []:
            src = str(item.get("src", ""))
            alt = _clean_text(str(item.get("alt", "")), 140)
            # Filter out tiny DDG UI badges
            if "duckduckgo.com/assets" in src or "duckduckgo.com/favicon" in src:
                continue
            candidates.append(
                {
                    "image_url": src,
                    "source_url": url,
                    "title": alt or f"Visual Reference: {query[:48]}",
                    "alt": alt or query,
                }
            )
            if len(candidates) >= limit:
                break
    except Exception:
        pass
    return candidates


def _execute_in_firefox(
    query: str = "",
    url: str = "",
    max_results: int = 4,
    pull_images: bool = True,
    max_images: int = 4,
    safesearch: str = "moderate",
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    max_results = max(1, min(_MAX_RESULTS_CAP, int(max_results)))
    max_images = max(0, min(_MAX_IMAGES_CAP, int(max_images))) if pull_images else 0
    ss_mode = safesearch if safesearch in _SAFESEARCH_KP_MAP else "moderate"
    kp_val = _SAFESEARCH_KP_MAP[ss_mode]

    pinned_hosts: dict[str, set[str]] = {}
    if url:
        ok_u, err_u, _ips = validate_public_http_url(url, resolve_dns=True, pinned_hosts=pinned_hosts)
        if not ok_u:
            raise ValueError(f"Blocked URL ({err_u})")

    purge_legacy_burner_profile()
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass
    ephem_dir, lock_fh, profile_kind, ram_risk = _alloc_ephemeral_ram_dir()
    profile_dir = ephem_dir / "profile"
    ff_home = ephem_dir / "home"
    ff_cfg = ephem_dir / "xdg_config"
    ff_cache = ephem_dir / "xdg_cache"
    ff_data = ephem_dir / "xdg_data"
    ff_tmp = ephem_dir / "tmp"
    for d in (profile_dir, ff_home, ff_cfg, ff_cache, ff_data, ff_tmp):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass

    isolated_ff_env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().endswith(("_TOKEN", "_API_KEY", "_SECRET")) and not k.upper().startswith("AWS_")
    }
    isolated_ff_env.update(
        {
            "HOME": str(ff_home),
            "XDG_CONFIG_HOME": str(ff_cfg),
            "XDG_CACHE_HOME": str(ff_cache),
            "XDG_DATA_HOME": str(ff_data),
            "TMPDIR": str(ff_tmp),
            "MOZ_CRASHREPORTER_DISABLE": "1",
            "XRE_NO_CRASH_REPORTER": "1",
        }
    )

    results: list[dict[str, str]] = []
    references: list[dict[str, Any]] = []
    seen_img_urls: set[str] = set()
    seen_hashes: set[str] = set()
    browser_killed = False
    recorded_pids: list[int] = []
    wiped = False
    wipe_error = ""
    budget_state = {"bytes_used": 0, "nav_count": 0}

    def _try_pull_image(
        ctx: Any,
        img_url: str,
        source_url: str,
        title: str,
        alt_text: str,
    ) -> None:
        if len(references) >= max_images or img_url in seen_img_urls:
            return
        if budget_state["bytes_used"] >= _MAX_RESEARCH_BYTES:
            return
        ok_i, _err_i, _ips = validate_public_http_url(
            img_url, resolve_dns=True, pinned_hosts=pinned_hosts
        )
        if not ok_i:
            return
        seen_img_urls.add(img_url)
        try:
            resp = ctx.request.get(img_url, timeout=7000, max_redirects=3)
            if not resp.ok:
                return
            final_img_url = str(getattr(resp, "url", img_url) or img_url)
            ok_rf, _err_rf, _ = validate_public_http_url(
                final_img_url, resolve_dns=True, pinned_hosts=pinned_hosts
            )
            if not ok_rf:
                return
            raw_bytes = resp.body()
            if not raw_bytes or len(raw_bytes) < 4096 or len(raw_bytes) > _MAX_IMAGE_BYTES:
                return
            if (budget_state["bytes_used"] + len(raw_bytes)) > _MAX_RESEARCH_BYTES:
                return
            # Check magic bytes directly instead of trusting HTTP Content-Type header (`F8`)
            if sniff_image_format(raw_bytes) is None:
                return
            budget_state["bytes_used"] += len(raw_bytes)
            digest = hashlib.sha256(raw_bytes).hexdigest()
            if digest in seen_hashes:
                return
            feat = analyze_image_bytes_in_ram(
                raw_bytes,
                alt_text=sanitize_untrusted_web_text(alt_text, 140),
                page_title=sanitize_untrusted_web_text(title, 120),
                query=query,
            )
            if feat is None:
                return
            norm_digest = hashlib.sha256(feat["image_bytes_b64"].encode("ascii")).hexdigest()
            if norm_digest in seen_hashes:
                return
            seen_hashes.add(digest)
            seen_hashes.add(norm_digest)
            references.append(
                {
                    "id": f"ref-{len(references) + 1:02d}",
                    "title": sanitize_untrusted_web_text(title or alt_text or "Reference", 90),
                    "source_url": source_url,
                    "image_url": final_img_url,
                    "alt_text": sanitize_untrusted_web_text(alt_text, 140),
                    **feat,
                }
            )
        except Exception:
            return

    try:
        with sync_playwright() as pw:
            ctx = pw.firefox.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                firefox_user_prefs=_MEMORY_ONLY_FIREFOX_PREFS,
                args=["-private"],
                env=isolated_ff_env,
            )
            # SSRF, redirect, DNS rebind, & resource-bloat route guard (`F1`–`F5`, `F10`)
            def _route_guard(route: Any) -> None:
                req = route.request
                rtype = getattr(req, "resource_type", "")
                if rtype in ("media", "font", "websocket", "beacon", "eventsource"):
                    route.abort("blockedbyclient")
                    return
                if req.is_navigation_request():
                    if budget_state["nav_count"] >= _MAX_NAVIGATIONS:
                        route.abort("blockedbyclient")
                        return
                    budget_state["nav_count"] += 1
                ok_r, _err_r, _ = validate_public_http_url(
                    req.url, resolve_dns=True, pinned_hosts=pinned_hosts
                )
                if not ok_r:
                    route.abort("blockedbyclient")
                    return
                route.continue_()

            ctx.route("**/*", _route_guard)
            recorded_pids = [p for p in collect_descendant_pids(os.getpid()) if p != os.getpid()]
            try:
                page = ctx.new_page()
                page.set_default_timeout(14000)

                if url:
                    page.goto(url, wait_until="domcontentloaded", timeout=18000)
                    final_page_url = page.url or url
                    ok_post, err_post, _ = validate_public_http_url(
                        final_page_url, resolve_dns=True, pinned_hosts=pinned_hosts
                    )
                    if not ok_post:
                        raise ValueError(f"Blocked redirected destination ({err_post})")
                    title = sanitize_untrusted_web_text(page.title(), 120) or final_page_url
                    body_text = ""
                    try:
                        body_text = page.locator("main, article, body").first.inner_text()[:8000]
                    except Exception:
                        pass
                    results.append(
                        {
                            "title": title,
                            "url": final_page_url,
                            "takeaway": sanitize_untrusted_web_text(body_text, 420) or "Fetched page.",
                        }
                    )
                    if pull_images:
                        for cand in _collect_page_candidate_images(page, final_page_url, max_per_page=max_images * 2):
                            _try_pull_image(ctx, cand["image_url"], final_page_url, title, cand["alt"])
                            if len(references) >= max_images:
                                break

                elif query:
                    encoded = urllib.parse.quote_plus(query)
                    ddg_url = f"https://html.duckduckgo.com/html/?q={encoded}&kp={kp_val}"
                    page.goto(ddg_url, wait_until="domcontentloaded", timeout=18000)
                    results = [
                        {
                            "title": sanitize_untrusted_web_text(r.get("title", ""), 120),
                            "url": r.get("url", ""),
                            "takeaway": sanitize_untrusted_web_text(r.get("takeaway", ""), 280),
                        }
                        for r in _extract_ddg_html_results(page, limit=max_results)
                        if validate_public_http_url(r.get("url", ""), resolve_dns=False)[0]
                    ]

                    if not results:
                        page.goto(
                            f"https://duckduckgo.com/?q={encoded}&kp={kp_val}&ia=web",
                            wait_until="domcontentloaded",
                            timeout=18000,
                        )
                        page.wait_for_timeout(1500)
                        links = page.locator("article h2 a")
                        for idx in range(min(max_results, links.count())):
                            a = links.nth(idx)
                            href = a.get_attribute("href") or ""
                            title = sanitize_untrusted_web_text(a.inner_text(), 120)
                            if title and href and validate_public_http_url(href, resolve_dns=False)[0]:
                                results.append(
                                    {
                                        "title": title,
                                        "url": href,
                                        "takeaway": f"Fetched from DuckDuckGo (SafeSearch {ss_mode}).",
                                    }
                                )

                    if pull_images:
                        for tab in results[:3]:
                            if len(references) >= max_images:
                                break
                            tab_url = tab.get("url", "")
                            ok_tu, _eu, _ = validate_public_http_url(
                                tab_url, resolve_dns=True, pinned_hosts=pinned_hosts
                            )
                            if not ok_tu:
                                continue
                            try:
                                page.goto(tab_url, wait_until="domcontentloaded", timeout=10000)
                                post_u = page.url or tab_url
                                if not validate_public_http_url(post_u, resolve_dns=True, pinned_hosts=pinned_hosts)[0]:
                                    continue
                                page.wait_for_timeout(450)
                                page_title = sanitize_untrusted_web_text(page.title(), 120) or tab.get("title", "")
                                for cand in _collect_page_candidate_images(page, post_u, max_per_page=3):
                                    _try_pull_image(
                                        ctx,
                                        cand["image_url"],
                                        post_u,
                                        page_title,
                                        cand["alt"],
                                    )
                                    if len(references) >= max_images:
                                        break
                            except Exception:
                                continue

                        if len(references) < max_images:
                            for cand in _collect_ddg_image_results(
                                page, query, limit=max_images * 3, safesearch=ss_mode
                            ):
                                _try_pull_image(
                                    ctx,
                                    cand["image_url"],
                                    cand["source_url"],
                                    cand["title"],
                                    cand["alt"],
                                )
                                if len(references) >= max_images:
                                    break
            finally:
                recorded_pids.extend(
                    p for p in collect_descendant_pids(os.getpid()) if p != os.getpid() and p not in recorded_pids
                )
                try:
                    ctx.close()
                except Exception:
                    pass
                # Wait explicitly for browser/child processes to exit before wiping files (`B6`)
                exited = True
                for bpid in list(recorded_pids):
                    if not kill_process_tree_verified(bpid, term_timeout_s=2.5, kill_timeout_s=1.5):
                        exited = False
                browser_killed = exited
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                lock_fh.close()
            except OSError:
                pass
        wiped = scorched_earth_wipe(
            ephem_dir,
            owner_pid=os.getpid(),
            allowed_pids=recorded_pids,
            wait_timeout_s=4.0,
        )
        if not wiped or ephem_dir.exists():
            wipe_error = f"Cleanup verification failed: remnants remain at {ephem_dir}"
            wiped = False

    return {
        "query": query or url,
        "safesearch": f"{ss_mode} (kp={kp_val})",
        "results": results,
        "references": references,
        "trace_report": {
            "profile_kind": profile_kind,
            "storage_path": str(ephem_dir),
            "fs_type": ram_risk.get("fs_type", "unknown"),
            "ram_backed": bool(ram_risk.get("is_ram_backed", False)),
            "ram_persistence_risk": ram_risk,
            "temp_dir": str(ephem_dir),
            "browser_killed": browser_killed,
            "wiped": bool(wiped and not ephem_dir.exists()),
            "wipe_error": wipe_error,
            "bytes_budget_used": budget_state["bytes_used"],
            "safesearch": ss_mode,
            "images_extracted": len(references),
        },
    }


def run_ephemeral_research(
    query: str = "",
    url: str = "",
    max_results: int = 4,
    pull_images: bool = True,
    max_images: int = 4,
    timeout_s: float = 55.0,
    safesearch: str = "moderate",
    cancel_event: Any = None,
) -> dict[str, Any]:
    """Run ephemeral Firefox research with SSRF protection, budgets, & ownership-safe wipe (`F1`–`F15`)."""
    max_results = max(1, min(_MAX_RESULTS_CAP, int(max_results)))
    max_images = max(0, min(_MAX_IMAGES_CAP, int(max_images)))
    ss_mode = safesearch if safesearch in _SAFESEARCH_KP_MAP else "moderate"

    if url:
        ok_u, err_u, _ = validate_public_http_url(url, resolve_dns=True)
        if not ok_u:
            return {
                "query": url,
                "error": f"Blocked destination URL: {err_u}",
                "results": [],
                "references": [],
                "trace_report": {"wiped": True, "browser_killed": True, "images_extracted": 0, "safesearch": ss_mode},
            }

    purge_legacy_burner_profile()
    if not COMFY_VENV_PYTHON.is_file():
        return {
            "query": query or url,
            "error": f"Python interpreter not found at {COMFY_VENV_PYTHON}",
            "results": [],
            "references": [],
            "trace_report": {"wiped": True, "browser_killed": True, "images_extracted": 0, "safesearch": ss_mode},
        }

    cmd = [
        str(COMFY_VENV_PYTHON),
        "-m",
        "vimona.search_worker",
        "--max-results",
        str(max_results),
        "--max-images",
        str(max_images),
        "--safesearch",
        ss_mode,
    ]
    if not pull_images:
        cmd.append("--no-images")
    if url:
        cmd.extend(["--url", url])
    else:
        cmd.extend(["--query", query])

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + max(5.0, float(timeout_s))
    cancelled = False
    timed_out = False
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.1)

    if cancelled or timed_out:
        killed_ok = kill_process_tree_verified(proc.pid, term_timeout_s=2.0, kill_timeout_s=1.5)
        try:
            proc.wait(timeout=1.5)
        except Exception:
            pass
        sweep_res = _sweep_orphan_shm_profiles()
        return {
            "query": query or url,
            "error": "Research cancelled." if cancelled else "Ephemeral Firefox worker timed out.",
            "results": [],
            "references": [],
            "trace_report": {
                "wiped": bool(sweep_res.get("ok", False)),
                "wipe_error": "; ".join(sweep_res.get("errors", [])),
                "browser_killed": killed_ok,
                "safesearch": ss_mode,
                "images_extracted": 0,
            },
        }
    stdout, stderr = proc.communicate()

    sweep_res = _sweep_orphan_shm_profiles()
    if proc.returncode != 0:
        return {
            "query": query or url,
            "error": _clean_text(stderr, 260) or f"Worker exited with code {proc.returncode}",
            "results": [],
            "references": [],
            "trace_report": {
                "wiped": bool(sweep_res.get("ok", False)),
                "wipe_error": "; ".join(sweep_res.get("errors", [])),
                "browser_killed": True,
                "images_extracted": 0,
            },
        }

    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError:
        return {
            "query": query or url,
            "error": "Invalid JSON returned from ephemeral worker.",
            "results": [],
            "references": [],
            "trace_report": {
                "wiped": bool(sweep_res.get("ok", False)),
                "wipe_error": "; ".join(sweep_res.get("errors", [])),
                "browser_killed": True,
                "images_extracted": 0,
            },
        }


def _sweep_orphan_shm_profiles() -> dict[str, Any]:
    """Safely sweep ONLY dead/unlocked `vimona_ephem_*` dirs owned by current UID (`B3`, `B4`)."""
    my_uid = os.getuid() if hasattr(os, "getuid") else 0
    errors: list[str] = []
    removed: list[str] = []
    for base in (SHM_DIR, Path(tempfile.gettempdir())):
        if not base.is_dir():
            continue
        try:
            for item in base.glob("vimona_ephem_*"):
                try:
                    st = item.lstat()
                    if st.st_uid != my_uid or item.is_symlink() or not item.is_dir():
                        continue
                    # Never interfere with active concurrent worker holding .worker.lock
                    lock_p = item / ".worker.lock"
                    if lock_p.exists():
                        with open(lock_p, "r+b") as fh:
                            try:
                                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                            except BlockingIOError:
                                continue
                    owner_p = item / ".worker_owner.json"
                    owner_pid = None
                    if owner_p.is_file():
                        try:
                            meta = json.loads(owner_p.read_text(encoding="utf-8"))
                            opid = int(meta.get("pid") or 0)
                            ost = meta.get("start_ticks")
                            if opid > 1 and is_pid_alive_with_start(opid, ost):
                                continue  # Owner process still running!
                            owner_pid = opid
                        except Exception:
                            pass
                    elif (time.time() - st.st_mtime) < 15.0:
                        continue  # Avoid racing a worker mid-startup before metadata exists
                    if scorched_earth_wipe(item, owner_pid=owner_pid):
                        removed.append(str(item))
                    else:
                        errors.append(f"Failed orphan cleanup: {item}")
                except OSError as exc:
                    errors.append(f"{item}: {exc}")
        except OSError:
            pass
    return {"ok": len(errors) == 0, "removed": removed, "errors": errors}


# Compatibility alias for existing callers
run_firefox_search = run_ephemeral_research


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vimona ephemeral Firefox reference worker")
    parser.add_argument("--query", default="", help="Search query")
    parser.add_argument("--url", default="", help="Direct public HTTP/HTTPS URL to inspect")
    parser.add_argument("--max-results", type=int, default=4)
    parser.add_argument("--max-images", type=int, default=4)
    parser.add_argument("--safesearch", choices=("strict", "moderate", "off"), default="moderate")
    parser.add_argument("--no-images", action="store_true")
    args = parser.parse_args(argv)

    max_res = max(1, min(_MAX_RESULTS_CAP, int(args.max_results)))
    max_img = max(0, min(_MAX_IMAGES_CAP, int(args.max_images)))
    try:
        payload = _execute_in_firefox(
            query=args.query,
            url=args.url,
            max_results=max_res,
            pull_images=not args.no_images,
            max_images=max_img,
            safesearch=args.safesearch,
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
