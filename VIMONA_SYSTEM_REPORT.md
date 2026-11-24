# Vimona Desktop System & Verification Report

**Date:** 2026-11-23  
**Platform:** Fedora Linux 43 (Workstation Edition) · 16 GB Shared RAM (14.9 GiB usable) · AMD Radeon 840M (`RADV GFX1152` / ROCm `HSA_OVERRIDE_GFX_VERSION=11.0.0`)  
**Project Root:** `/home/muthyaladaksh/.gemini/antigravity/scratch/vimona` (`6,018` lines across Python modules, markdown skills, and launch scripts)

---

## 1. Burner Firefox Profile & Burner Email Status

Inspected `/home/muthyaladaksh/firefox-burner-profile` (`signedInUser.json`, `logins.json`, `cookies.sqlite`) and verified live browser behavior via Playwright:

- **Firefox Account Email:** `jarvi3413@gmail.com`
- **Account UID:** `699f97eefdd87afd4bc39c2caf82c897` (`verified: true`, `twoFactorAuthentication: false`)
- **Saved Logins in Profile (`logins.json`):** `3` entries for `https://accounts.firefox.com` and `chrome://FirefoxAccounts`
- **Active Cookies (`cookies.sqlite`):** `.firefox.com`, `accounts.firefox.com`
- **Burner Email (Gmail) Live Check:** **Not signed in inside the burner profile yet.** Navigating to `https://mail.google.com` with `~/firefox-burner-profile` redirected to `https://accounts.google.com/v3/signin/identifier` (no Google cookies exist in `cookies.sqlite`). To use Gmail in this profile, sign into `jarvi3413@gmail.com` once in a regular Firefox launch using `--profile ~/firefox-burner-profile`.
- **Profile Compatibility Fix Applied:** The burner profile was created by a newer system `/usr/bin/firefox` than Playwright's bundled `firefox-1572`, which caused a downgrade lock error until `-allow-downgrade` was added in `vimona/search_worker.py`. With `-allow-downgrade` and stale-lock cleanup (checking `/proc` first), headless DuckDuckGo searches (`kp=-2`, SafeSearch disabled) succeeded cleanly and exited with `0` lingering Firefox processes.

---

## 2. Zero-Overlap 16 GB RAM Relay & Telemetry

Implemented in `vimona/memory_relay.py` and wired into `vimona/window.py` (`SystemStatePill` updating every `1.5s` from `/proc/meminfo`):

| Component | Binary / Entry | Flags & Environment | Observed Result |
| :--- | :--- | :--- | :--- |
| **Text Engine** | `~/llama.cpp/build/bin/llama-server` | `--model ~/models/llm/light-uncensored.gguf --device Vulkan0 --flash-attn on --load-mode none --ctx-size 4096 --n-gpu-layers 99 --reasoning-budget 96 --host 127.0.0.1 --port 18081` | Booted in `3.87s–4.23s`; RAM usage `9.7 / 14.9 GB` while active |
| **Media Engine** | `~/comfyui/venv/bin/python ~/comfyui/main.py` | `--listen 127.0.0.1 --port 18188 --lowvram --cpu-vae --cache-none --offline --disable-partner-nodes --output-directory ~/comfyui/output` + `HSA_OVERRIDE_GFX_VERSION=11.0.0`, `PYTORCH_HIP_ALLOC_CONF=expandable_segments:True`, `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` | Booted in `13.98s`; after stopping `llama-server`, RAM dropped cleanly from `9.7 GB` to `5.2 / 14.9 GB` before model load |
| **Cleanup** | Process-group termination (`start_new_session=True` + `os.killpg`) | `SIGTERM` → 6s wait → `SIGKILL` fallback, tied to `close-request`, `atexit`, `SIGINT`, `SIGTERM` | Verified `0` orphan `llama-server`, `ComfyUI`, or `firefox` processes |

### Important Note on `llama-server` Flags & Reasoning Model Behavior
- **Flag difference on disk:** This `llama.cpp` build rejected `--no-mmap` (`USE_LOAD_MODE_NONE`) and uses `--load-mode none` instead.
- **Reasoning budget fix:** `light-uncensored.gguf` is a thinking/reasoning model that emits thoughts into `message.reasoning_content`. Without `--reasoning-budget`, `max_tokens=256` produced empty `content` (`""`) and longer requests timed out at `60s`. Adding `--reasoning-budget 96`, `cache_prompt: True`, `max_tokens: 768–1024`, and returning `(content, reasoning_content)` into `ReasoningAccordion` fixed tool-call generation cleanly (`PARSED_TOOL_CALL: render_media`, `PROMPT_PREFIX_OK: True`, `LLM_REASONING_LEN: 365`, `CONTENT_LEN: 834`).

---

## 3. ComfyUI API Workflows (`vimona/workflows.py`)

Verified all **17** required node class names directly against ComfyUI's live `/object_info` endpoint (`MISSING_NODES: []`):

1. **Pony V6 XL Keyframes (`build_pony_keyframe_workflow`)**
   - `UnetLoaderGGUF` (`pony-diffusion-v6-xl-Q8_0.gguf`)
   - `DualCLIPLoader` (`clip_l.safetensors`, `clip_g.safetensors`, `type="sdxl"`)
   - `CLIPSetLastLayer` (`stop_at_clip_layer=-2`)
   - `VAELoader` (`sdxl_vae.safetensors`)
   - `KSampler` (`euler_ancestral`, `normal`, `25` steps, `cfg=6.5`) → `VAEDecode` → `SaveImage` (`~/comfyui/output`)
2. **Wan 2.1 VACE 1.3B Chained 4s Sliding Windows (`build_wan_vace_window_workflow`)**
   - `UNETLoader` (`wan2.1_vace_1.3B_fp16.safetensors`) + `ModelSamplingSD3` (`shift=8.0`)
   - `CLIPLoader` (`umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `type="wan"`, `device="cpu"` to protect 8 GB iGPU VRAM)
   - `VAELoader` (`wan_2.1_vae.safetensors`)
   - `LoadImage` (Pony keyframe copied to `~/comfyui/input/` as `reference_image` anchor)
   - `WanVaceToVideo` (`832×480`, `65` frames @ `16 fps` ≈ `4s` per window; consecutive windows pass previous window's final frame via `ffmpeg` + `SolidMask(0.0)` as `control_video` / `control_masks`)
   - `KSampler` (`uni_pc`, `simple`, `20` steps, `cfg=6.0`) → `TrimVideoLatent` → `VAEDecode` → `VHS_VideoCombine` (`video/h264-mp4`)
   - Multi-window `.mp4` segments are concatenated into a single `.mp4` in `~/comfyui/output/` via `ffmpeg`.

---

## 4. Modular Markdown Skills (`vimona/skills/` & `vimona/skills_loader.py`)

Loaded dynamically into the local LLM's system prompt with zero content filters or refusals:
- `vimona/skills/pony_choreography.md` — Pony quality prefixes (`score_9, score_8_up, score_7_up, score_6_up`), dynamic framing/lighting tags, and 4-second combat/motion beats.
- `vimona/skills/firefox_research.md` — `web_search` tool-call specification for choreography references, lens terminology, and trigger tags.
- `vimona/skills/ram_relay.md` — `render_media` tool-call specification for keyframe batches (`1, 2, 4, 8`) and chained 4-second video clips (`4s, 8s, 16s, 24s`).

---

## 5. Desktop UI, Real MP4 Playback, Storage & One-Button Launch

- **Native Fedora GTK4 / Libadwaita Window (`vimona/window.py`, `vimona/widgets.py`, `vimona/tokens.py`, `vimona/icons.py`):**
  - Matte graphite palette (`#141414` / `#191919` / `#242424`), true-vector Cairo `SvgIcon` & sculpted V-ribbon mark, collapsible chat sidebar, floating pill composer, inline `1/2/4/8` image grid, `LightboxInspector` with zoom/pan/metadata, and live `SystemStatePill` RAM telemetry.
  - Real `.mp4` playback & frame-by-frame scrubbing via OpenCV (`VideoFrameDecoder` in `vimona/media.py`).
  - Local offline chat persistence in `~/.local/share/vimona/chats.json` (`vimona/storage.py`).
- **One-Button Launch & Desktop Entry:**
  - Executable script: `/home/muthyaladaksh/.gemini/antigravity/scratch/vimona/launch-vimona.sh`
  - Desktop entry: `~/.local/share/applications/dev.vimona.Desktop.desktop`
  - App icon: `~/.local/share/icons/hicolor/scalable/apps/dev.vimona.Desktop.svg`

---

## 6. Verification Summary: What Works vs. Caveats

### Working & Verified
- `python3 -m py_compile` across all modules: **Passed**
- GTK4 CSS validation & UI window startup (`CSS_ERRORS: 0`, `GTK4_UI_EXIT: 0`): **Passed**
- All model, binary, and profile paths on disk (`MISSING_PATHS: []`): **Passed**
- Modular markdown skills loading (`['firefox_research', 'pony_choreography', 'ram_relay']`): **Passed**
- `llama-server` Vulkan0 boot (`4.23s`), bounded reasoning (`--reasoning-budget 96`), and structured `render_media` tool-call emission: **Passed**
- Isolated Playwright Firefox burner search (`kp=-2`, `3` results returned, `0` lingering Firefox processes): **Passed**
- Zero-overlap RAM relay handoff (`llama-server` stopped → `ComfyUI` booted in `13.98s` with `5.2/14.9 GB` RAM used → `llama-server` rebooted in `3.87s` → `0` orphan processes): **Passed**
- ComfyUI `/object_info` validation across all `17` Pony V6 XL + Wan 2.1 VACE nodes (`MISSING_NODES: []`): **Passed**

### Caveats / Not Yet Executed End-to-End
1. **Burner Gmail (`jarvi3413@gmail.com`)** is signed into the Firefox Account (`accounts.firefox.com`), **not** into `mail.google.com` inside `~/firefox-burner-profile` (Gmail redirected to Google Sign-In).
2. **Full pixel/video sampling run in ComfyUI** (running 25 Pony diffusion steps + 65-frame Wan 2.1 VACE sampling to generate actual `.png`/`.mp4` files on disk) was **not** queued to completion during verification; node schemas, loader filenames, and process lifecycle were verified live.
3. **LLM speed note:** On the AMD Radeon 840M iGPU (`Vulkan0`), a `~1,124`-token prompt + `320`-token completion took `~49s`; `cache_prompt: True` is enabled so follow-up turns reuse the system prompt KV cache.

---

## 7. How to Launch Vimona

- **From Fedora App Grid / Launcher:** Search for **Vimona** (`~/.local/share/applications/dev.vimona.Desktop.desktop`).
- **From Terminal (one command):**
  ```bash
  /home/muthyaladaksh/.gemini/antigravity/scratch/vimona/launch-vimona.sh
  ```
