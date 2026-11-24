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
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .airgap import purge_legacy_burner_profile, scorched_earth_wipe
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


def _clean_text(raw: str, max_len: int = 320) -> str:
    collapsed = re.sub(r"\s+", " ", raw or "").strip()
    if len(collapsed) > max_len:
        return collapsed[: max_len - 1].rstrip() + "…"
    return collapsed


def _alloc_ephemeral_ram_dir() -> tuple[Path, str]:
    """Create a private disposable directory in `/dev/shm` RAM disk (or OS temp fallback)."""
    if SHM_DIR.is_dir() and os.access(SHM_DIR, os.W_OK):
        p = Path(tempfile.mkdtemp(prefix="vimona_ephem_", dir=str(SHM_DIR)))
        return p, "/dev/shm RAM disk"
    p = Path(tempfile.mkdtemp(prefix="vimona_ephem_"))
    return p, "OS temp directory"


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

    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            im = ImageOps.exif_transpose(im)
            w, h = im.size
            if w < 180 or h < 180:
                return None
            # Reject extreme banner/divider strips (e.g. 10:1 nav bars)
            aspect = max(w / max(1, h), h / max(1, w))
            if aspect > 4.2:
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

    # Explicit YCbCr check on central 84% crop + alt/title text check BEFORE returning to UI
    h_a, w_a, _ = arr.shape
    core = arr[int(h_a * 0.08) : int(h_a * 0.92), int(w_a * 0.08) : int(w_a * 0.92)]
    cr_r, cr_g, cr_b = core[..., 0], core[..., 1], core[..., 2]
    cy = 0.299 * cr_r + 0.587 * cr_g + 0.114 * cr_b
    ccb = 128.0 - 0.168736 * cr_r - 0.331264 * cr_g + 0.5 * cr_b
    ccr = 128.0 + 0.5 * cr_r - 0.418688 * cr_g - 0.081312 * cr_b
    skin = (
        (cy > 68.0)
        & (cr_r > cr_g)
        & ((cr_r - cr_b) > 18.0)
        & (ccb >= 85.0)
        & (ccb <= 124.0)
        & (ccr >= 136.0)
        & (ccr <= 168.0)
    )
    skin_ratio = float(np.mean(skin)) if core.size else 0.0

    combined_txt = f"{alt_text} {page_title} {query}"
    is_explicit = is_explicit_text(combined_txt) or (skin_ratio >= 0.52)

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


def _collect_ddg_image_results(page: Any, query: str, limit: int = 8) -> list[dict[str, str]]:
    """Query DuckDuckGo Images inside the isolated RAM tab as direct visual fallback."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://duckduckgo.com/?q={encoded}&kp=-2&iax=images&ia=images"
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
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    purge_legacy_burner_profile()
    ephem_dir, profile_kind = _alloc_ephemeral_ram_dir()
    results: list[dict[str, str]] = []
    references: list[dict[str, Any]] = []
    seen_img_urls: set[str] = set()
    seen_hashes: set[str] = set()
    browser_killed = False

    def _try_pull_image(
        ctx: Any,
        img_url: str,
        source_url: str,
        title: str,
        alt_text: str,
    ) -> None:
        if len(references) >= max_images or img_url in seen_img_urls:
            return
        seen_img_urls.add(img_url)
        try:
            resp = ctx.request.get(img_url, timeout=7000)
            if not resp.ok:
                return
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype and ("image/" not in ctype or "svg" in ctype):
                return
            raw_bytes = resp.body()
            if not raw_bytes or len(raw_bytes) < 4096 or len(raw_bytes) > 8 * 1024 * 1024:
                return
            digest = hashlib.sha256(raw_bytes).hexdigest()
            if digest in seen_hashes:
                return
            feat = analyze_image_bytes_in_ram(
                raw_bytes,
                alt_text=alt_text,
                page_title=title,
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
                    "title": _clean_text(title or alt_text or "Reference", 90),
                    "source_url": source_url,
                    "image_url": img_url,
                    "alt_text": _clean_text(alt_text, 140),
                    **feat,
                }
            )
        except Exception:
            return

    try:
        with sync_playwright() as pw:
            ctx = pw.firefox.launch_persistent_context(
                user_data_dir=str(ephem_dir),
                headless=True,
                firefox_user_prefs=_MEMORY_ONLY_FIREFOX_PREFS,
                args=["-private"],
            )
            try:
                page = ctx.new_page()
                page.set_default_timeout(14000)

                if url:
                    page.goto(url, wait_until="domcontentloaded", timeout=18000)
                    title = _clean_text(page.title(), 120) or url
                    body_text = ""
                    try:
                        body_text = page.locator("main, article, body").first.inner_text()
                    except Exception:
                        pass
                    final_page_url = page.url or url
                    results.append(
                        {
                            "title": title,
                            "url": final_page_url,
                            "takeaway": _clean_text(body_text, 420) or "Fetched page.",
                        }
                    )
                    if pull_images:
                        for cand in _collect_page_candidate_images(page, final_page_url, max_per_page=max_images * 2):
                            _try_pull_image(ctx, cand["image_url"], final_page_url, title, cand["alt"])
                            if len(references) >= max_images:
                                break

                elif query:
                    encoded = urllib.parse.quote_plus(query)
                    ddg_url = f"https://html.duckduckgo.com/html/?q={encoded}&kp=-2"
                    page.goto(ddg_url, wait_until="domcontentloaded", timeout=18000)
                    results = _extract_ddg_html_results(page, limit=max_results)

                    if not results:
                        page.goto(
                            f"https://duckduckgo.com/?q={encoded}&kp=-2&ia=web",
                            wait_until="domcontentloaded",
                            timeout=18000,
                        )
                        page.wait_for_timeout(1500)
                        links = page.locator("article h2 a")
                        for idx in range(min(max_results, links.count())):
                            a = links.nth(idx)
                            href = a.get_attribute("href") or ""
                            title = _clean_text(a.inner_text(), 120)
                            if title and href:
                                results.append(
                                    {
                                        "title": title,
                                        "url": href,
                                        "takeaway": "Fetched from DuckDuckGo (SafeSearch off).",
                                    }
                                )

                    if pull_images:
                        # 1. Inspect top search pages inside the sandboxed tab for authentic visual refs
                        for tab in results[:3]:
                            if len(references) >= max_images:
                                break
                            tab_url = tab.get("url", "")
                            if not tab_url.startswith(("http://", "https://")):
                                continue
                            try:
                                page.goto(tab_url, wait_until="domcontentloaded", timeout=10000)
                                page.wait_for_timeout(450)
                                page_title = _clean_text(page.title(), 120) or tab.get("title", "")
                                for cand in _collect_page_candidate_images(page, page.url or tab_url, max_per_page=3):
                                    _try_pull_image(
                                        ctx,
                                        cand["image_url"],
                                        page.url or tab_url,
                                        page_title,
                                        cand["alt"],
                                    )
                                    if len(references) >= max_images:
                                        break
                            except Exception:
                                continue

                        # 2. Fill remaining slots from DuckDuckGo Images in RAM tab
                        if len(references) < max_images:
                            for cand in _collect_ddg_image_results(page, query, limit=max_images * 3):
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
                try:
                    ctx.close()
                except Exception:
                    pass
                browser_killed = True
    finally:
        # Scorched-Earth wipe: ensure Firefox child processes dead and temp profile dir erased cleanly
        wiped = scorched_earth_wipe(ephem_dir)

    return {
        "query": query or url,
        "safesearch": "off (kp=-2)",
        "results": results,
        "references": references,
        "trace_report": {
            "profile_kind": profile_kind,
            "temp_dir": str(ephem_dir),
            "browser_killed": browser_killed,
            "wiped": bool(wiped and not ephem_dir.exists()),
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
) -> dict[str, Any]:
    """Run ephemeral zero-trace Firefox research in `/dev/shm` and destroy all trace afterward."""
    purge_legacy_burner_profile()
    if not COMFY_VENV_PYTHON.is_file():
        return {
            "query": query or url,
            "error": f"Python interpreter not found at {COMFY_VENV_PYTHON}",
            "results": [],
            "references": [],
            "trace_report": {"wiped": True, "browser_killed": True, "images_extracted": 0},
        }

    cmd = [
        str(COMFY_VENV_PYTHON),
        "-m",
        "vimona.search_worker",
        "--max-results",
        str(max_results),
        "--max-images",
        str(max_images),
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
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        # Sweep any orphaned vimona_ephem_* dirs on /dev/shm owned by current UID
        _sweep_orphan_shm_profiles()
        return {
            "query": query or url,
            "error": "Ephemeral Firefox sandbox timed out (wiped).",
            "results": [],
            "references": [],
            "trace_report": {"wiped": True, "browser_killed": True, "images_extracted": 0},
        }

    _sweep_orphan_shm_profiles()
    if proc.returncode != 0:
        return {
            "query": query or url,
            "error": _clean_text(stderr, 260) or f"Worker exited with code {proc.returncode}",
            "results": [],
            "references": [],
            "trace_report": {"wiped": True, "browser_killed": True, "images_extracted": 0},
        }

    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError:
        return {
            "query": query or url,
            "error": "Invalid JSON returned from ephemeral worker.",
            "results": [],
            "references": [],
            "trace_report": {"wiped": True, "browser_killed": True, "images_extracted": 0},
        }


def _sweep_orphan_shm_profiles() -> None:
    """Best-effort sweep ensuring no `vimona_ephem_*` directory remains on `/dev/shm`."""
    uid = os.getuid()
    for base in (SHM_DIR, Path(tempfile.gettempdir())):
        if not base.is_dir():
            continue
        try:
            for item in base.glob("vimona_ephem_*"):
                try:
                    if item.stat().st_uid == uid:
                        scorched_earth_wipe(item)
                except OSError:
                    pass
        except OSError:
            pass


# Compatibility alias for existing callers
run_firefox_search = run_ephemeral_research


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vimona zero-trace /dev/shm Firefox reference worker")
    parser.add_argument("--query", default="", help="Search query (DuckDuckGo kp=-2)")
    parser.add_argument("--url", default="", help="Direct URL to inspect")
    parser.add_argument("--max-results", type=int, default=4)
    parser.add_argument("--max-images", type=int, default=4)
    parser.add_argument("--no-images", action="store_true")
    args = parser.parse_args(argv)

    try:
        payload = _execute_in_firefox(
            query=args.query,
            url=args.url,
            max_results=args.max_results,
            pull_images=not args.no_images,
            max_images=args.max_images,
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
