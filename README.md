<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/vimona-hero-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/brand/vimona-hero-light.svg">
  <img alt="Vimona — Local-First Air-Gapped AI Workstation" src="assets/brand/vimona-hero-dark.svg" width="100%">
</picture>

<br/><br/>

[![Powered by Hugging Face](https://img.shields.io/badge/Powered%20by-Hugging%20Face-FFD21E?style=flat-square&logo=huggingface&logoColor=black)](https://huggingface.co)
[![Hardware-Tuned Models](https://img.shields.io/badge/Flagship-Hardware--Tuned%20Model%20Picking-10B981?style=flat-square)](#hardware-tuned-model-picking--boosting)
[![Orchestration Harness](https://img.shields.io/badge/Orchestration-Zero--Overlap%20RAM%20Relay-6366F1?style=flat-square)](#orchestration--dual-booster-engines)
[![Booster Engines](https://img.shields.io/badge/Booster%20Engines-DMD2%20%2B%20CausVid%20%2B%20DSLR-F59E0B?style=flat-square)](#orchestration--dual-booster-engines)
[![Local AI Harness](https://img.shields.io/badge/Architecture-Local%20AI%20Harness-1E2430?style=flat-square)](#what-makes-vimona-a-harness-not-a-model-fork)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-1E2430?style=flat-square&logo=python&logoColor=93C5FD)](pyproject.toml)
[![GTK4 + Libadwaita](https://img.shields.io/badge/Desktop-GTK4%20%2B%20Libadwaita-1E2430?style=flat-square&logo=gnome&logoColor=93C5FD)](vimona/window.py)
[![ComfyUI Engine](https://img.shields.io/badge/Engine-ComfyUI%20%2B%20sd.cpp-1E2430?style=flat-square)](https://github.com/comfyanonymous/ComfyUI)
[![469 Skills](https://img.shields.io/badge/Skills-469%20Indexed-1E2430?style=flat-square)](vimona/skills_loader.py)
[![Linux Release](https://img.shields.io/badge/Download-Linux%20(.deb%20%7C%20.rpm%20%7C%20Arch%20%7C%20AppImage)-1E2430?style=flat-square&logo=linux&logoColor=FDE047)](https://github.com/dksh34/Vimona/releases/latest)
[![Windows Download](https://img.shields.io/badge/Download-Windows%2010%2F11%20Bundle-1E2430?style=flat-square&logo=windows&logoColor=93C5FD)](downloads/Vimona-Windows-x86_64.zip)
[![macOS Help Wanted](https://img.shields.io/badge/macOS-Packagers%20Welcome-1E2430?style=flat-square&logo=apple&logoColor=EDEDED)](#call-for-testers--macos-build)
[![License: MIT](https://img.shields.io/badge/License-MIT-1E2430?style=flat-square)](LICENSE)

<h3>
  A local AI orchestration harness <strong>powered by Hugging Face</strong> — featuring automatic <strong>Hardware-Tuned Model Picking</strong>, multi-stage <strong>Orchestration</strong>, and <strong>Dual Booster Engines</strong> tailored to your GPU, VRAM/GTT, and RAM.
</h3>

<p>
  <a href="#hardware-tuned-model-picking--boosting"><strong>Hardware-Tuned Picking</strong></a> &nbsp;·&nbsp;
  <a href="#orchestration--dual-booster-engines"><strong>Orchestration &amp; Boosters</strong></a> &nbsp;·&nbsp;
  <a href="#what-makes-vimona-a-harness-not-a-model-fork"><strong>Why a Harness</strong></a> &nbsp;·&nbsp;
  <a href="#469-skills--context-engineering"><strong>469 Skills Engine</strong></a> &nbsp;·&nbsp;
  <a href="#interactive-workspace-showcase"><strong>Live Video Demos</strong></a> &nbsp;·&nbsp;
  <a href="#downloads--installers"><strong>Downloads</strong></a> &nbsp;·&nbsp;
  <a href="#call-for-testers--macos-build"><strong>Testers &amp; macOS Wanted</strong></a> &nbsp;·&nbsp;
  <a href="#credits--acknowledgments"><strong>Credits</strong></a>
</p>

<br/>

<img src="assets/vimona-hero-showcase.png" width="96%" alt="Vimona GTK4 + Libadwaita Dark Workspace showcasing Alpine 50mm DSLR Keyframes, Controls, and Hardware-Tuned Model Header"/>

<sub><em>Vimona desktop harness running on Linux (`AMD Radeon 840M` · `8.0 GB GPU Pool` · `14.9 GB RAM`). Powered by Hugging Face open weights &amp; GTK4 + Libadwaita.</em></sub>

</div>

<br/>

> [!NOTE]
> **FLAGSHIP FEATURE — HARDWARE-TUNED MODEL PICKING (POWERED BY HUGGING FACE):**
> Most local AI apps show one static list and let laptops crash on `24 GB` weights. **Vimona is an orchestration harness** that probes your actual silicon (`/proc/meminfo`, `nvidia-smi`, and Linux DRM `mem_info_vram_total` + `mem_info_gtt_total`), classifies your tier (`Low`, `Balanced 8–16 GB`, or `Workstation 24+ GB`), **hides unsuited heavy/FP16 models automatically**, pulls verified `GGUF`/weight files directly from **Hugging Face Hub**, and tunes server launch flags (`--ctx-size`, `--vae-tiling`, `--n-gpu-layers`, `threads`) to fit your exact hardware.

<br/>

<table>
  <tr>
    <td width="33.3%" valign="top">
      <h4>1. Hardware-Tuned Model Picking</h4>
      Inspects live <code>VRAM</code>, unified <code>GTT</code> pool, CPU cores &amp; backend (<code>Vulkan</code>/<code>CUDA</code>/<code>CPU</code>). An `AMD Radeon 840M` only sees tuned <code>Q4/Q8 GGUF</code> weights—never crashing on <code>20+ GB</code> models.
    </td>
    <td width="33.3%" valign="top">
      <h4>2. Local AI Harness (Zero Lock-In)</h4>
      Essentially a pure runtime harness orchestrating <strong>Hugging Face</strong> models across <code>llama.cpp</code>, <code>stable-diffusion.cpp</code>, <code>ComfyUI</code>, <code>469</code> context-routed skills, and <code>/dev/shm</code> Firefox cleanly under one roof.
    </td>
    <td width="33.3%" valign="top">
      <h4>3. Orchestration &amp; Booster Engines</h4>
      Unloads text to <code>0.0 GB</code> before launching <code>4–6 step DMD2/Lightning</code> images, custom step counts, hardware-scaled video lengths, &amp; <code>6-step CausVid 720p</code> video inside <code>unshare -r -n</code> (<code>ENETUNREACH</code>).
    </td>
  </tr>
</table>

> [!IMPORTANT]
> **HELP TEST EVERY OS BUILD · MACOS PACKAGERS NEEDED:** Please test the **Linux** packages (<a href="https://github.com/dksh34/Vimona/releases/latest">GitHub Releases: <code>.deb</code>, <code>.rpm</code>, Arch <code>.pkg.tar.zst</code>, <code>.AppImage</code></a>) and **Windows** (<code>Vimona-Windows-x86_64.zip</code> / <code>.exe</code>) on your hardware and open an issue with your GPU/RAM behavior—and if you use **macOS (Apple Silicon / Intel)**, we would love a PR compiling a native standalone `Vimona.app` / `.dmg` desktop bundle! [**Jump to Testing &amp; macOS Callout ↓**](#call-for-testers--macos-build)

---

## Live DSLR Renders &amp; `720p` Boosted Motion

> [!NOTE]
> **Made Entirely With Lightweight `2B`-Class Models:** Every single photo and video shown across the showcase stills and demo videos below was generated locally on consumer hardware using compact **`2B`-class diffusion models** accelerated by Vimona's orchestration pipeline and booster engines—proving you do not need massive `14B+` weights to get crisp DSLR results.

Every frame below was generated locally on consumer unified-memory hardware using **Vimona's air-gapped orchestration &amp; booster pipeline** — finished through `ffmpeg lanczos` upscaling, unsharp edge toning (`5:5:0.75`), and temporal histogram validation (`0.999` frame consistency).

<div align="center">

<img src="assets/dslr-showcase-strip.png" width="96%" alt="Real local DSLR keyframes and 720p Alpine video frame rendered offline with 2B models in Vimona"/>

<br/><br/>

<img src="assets/demos/07-comfyui-video-making.webp" width="92%" alt="Vimona ComfyUI 2B Video + CausVid 720p Alpine Dawn Video Showcase"/>

<p>
  <a href="assets/demos/07-comfyui-video-making.mp4"><strong>Watch Full-Res MP4 Recording</strong></a> &nbsp;·&nbsp;
  <code>2B Diffusion Video</code> + <code>CausVid Booster LoRA (0.45)</code> &nbsp;·&nbsp;
  <code>1280×720 @ 24 fps</code>
</p>

</div>

<details>
<summary>Inspect ComfyUI Node Graph, Booster Engines &amp; DSLR Post-Processing Flags</summary>

| Pipeline Stage | Runtime Configuration | Why It Matters on 16 GB Unified RAM |
| :--- | :--- | :--- |
| **Namespace Lock** | `unshare -r -n` + `/dev/shm/*.sock` | Drops outbound LAN/internet/DNS (`ENETUNREACH`); local IPC passes strictly via host-bound `AF_UNIX`. |
| **UMT5 CPU Pinning** | `umt5_xxl_fp8_e4m3fn_scaled` offloaded to system RAM | Keeps `8 GB` GPU/GTT pool dedicated to latent denoising. |
| **Video Diffusion Boost** | `2B` Video Model + `CausVid` (`0.45`) | Generates coherent `640×368 @ 10 fps` motion in **6 steps** (`uni_pc`, `shift=5.5`, `cfg=1.1`), or custom user step count. |
| **DSLR Video Finish** | `ffmpeg` `lanczos` + `unsharp=5:5:0.75` + `eq` | Crisp `1280×720` `24 fps` (`6500k` H.264) verified by `verify_video_temporal_consistency()`. |

</details>

---

## Orchestration &amp; Dual Booster Engines

Vimona's core value is **orchestrating small-footprint local models so consumer laptops outperform naive unoptimized setups**:

<table>
  <tr>
    <td width="50%" valign="top">
      <h4>Orchestration Engine</h4>
      <ul>
        <li><strong>Zero-Overlap RAM Relay (<code>memory_relay.py</code>):</strong> Tears down <code>llama-server</code> to <code>0.0 GB</code> before image/video workers touch GPU memory, preventing unified-memory thrashing.</li>
        <li><strong>Dynamic Duration &amp; Custom Step Controls:</strong> Available video clip lengths (<code>4s</code> up to <code>32s</code>) scale automatically with both your detected GPU hardware and selected video model weight—plus a custom <code>Steps</code> input (with automatic latency warning) when you want full manual denoising control over boosted <code>Auto</code>.</li>
        <li><strong>Overnight Render Playlist + Context Routing:</strong> Queues mixed Chat/Image/Video jobs with reordering, thermal cooldowns, <code>/dev/shm</code> ephemeral research gating, and top-3 selection across <code>469</code> modular skills.</li>
      </ul>
    </td>
    <td width="50%" valign="top">
      <h4>Dual Booster Engines</h4>
      <ul>
        <li><strong>Image Booster Engine (<code>DMD2</code> + <code>Lightning</code> + <code>DSLR HiRes</code>):</strong> Runs <code>4–6 step</code> distilled denoising (<code>cfg 1.6</code>) on air-gapped <code>sd-server</code> with automatic spatial <code>--vae-tiling</code> and an optional `1.25×` DSLR detail refinement pass.</li>
        <li><strong>Video Motion Booster Engine (<code>CausVid</code> + <code>24fps Optical Flow</code>):</strong> Pairs <code>6-step CausVid</code> temporal LoRA with chained sliding-window keyframes and <code>ffmpeg lanczos + unsharp</code> finishing for crisp <code>720p @ 24 fps</code> clips even from lightweight <code>2B</code> models.</li>
        <li><strong>Hardware-Tuned Flag Injector:</strong> Scales <code>--ctx-size</code>, <code>--n-gpu-layers</code>, CPU pinning, and thread budgets automatically for Quiet/Eco or Turbo modes.</li>
      </ul>
    </td>
  </tr>
</table>

---

## Hardware-Tuned Model Picking &amp; Boosting

> **The centerpiece of Vimona ([`vimona/hf_models.py`](vimona/hf_models.py)):** **Powered directly by [Hugging Face](https://huggingface.co)**, Vimona never dumps unworkable `24 GB` workstation models onto `8–16 GB` laptops—nor clutters high-VRAM workstations with entry fallbacks.

Before showing a single download button, the **Hardware Profiler** probes:
1. **System Memory:** `MemTotal` &amp; `MemAvailable` via `/proc/meminfo` (or Windows `GlobalMemoryStatusEx`), reserving ~`2.5 GB` OS headroom on unified-memory PCs.
2. **GPU Addressable Pool:** Exact discrete VRAM via `nvidia-smi` **plus Linux DRM `mem_info_vram_total` and `mem_info_gtt_total`** (catching unified APU pools accurately—e.g. `0.5 GB` carveout + `7.5 GB` GTT = **`8.0 GB GPU Pool`** on Ryzen AI 5 / Radeon 840M).
3. **Full Multi-Tier Catalog (`43` Curated) + Live Hugging Face Hub Search:** Spans **`43` multi-tier weights (`1.5B → 70B` LLM, `SDXL Q4/Q8 → FLUX & SD 3.5 Large`, `0.9 GB Q4 Video → 14B & HunyuanVideo`)** plus a **live Hugging Face Hub search bar** inside the popup—filtering every result through `is_model_tuned_for_hardware()` so **only models matching your backend, GPU pool, and RAM tier appear**.

<div align="center">

<img src="assets/hf-models-popup.png" width="76%" alt="Vimona Hardware-Tuned Hugging Face Model Picker filtering to Vulkan GGUF weights for AMD Radeon 840M"/>

<sub><strong>Hardware-Tuned Hugging Face Picker:</strong> Detects <code>AMD Radeon 840M · 8.0 GB GPU Pool · 14.9 GB RAM · VULKAN</code>, hides heavy FP16/FLUX bloat, highlights the local <code>Active ✓</code> weight, and streams missing <code>GGUF</code> weights safely from Hugging Face.</sub>

</div>

<br/>

| Detected Hardware Tier | What Vimona Picks Automatically (Text / Image / Video) | What It Hides &amp; Why | Runtime Server Flags Auto-Tuned |
| :--- | :--- | :--- | :--- |
| **Low-RAM / CPU** (`< 11 GB` RAM or `CPU` only) | `Qwen 2.5 1.5B/3B`, `Llama 3.2 3B`, `Phi-4 Mini`, `Gemma 3 4B`, `RealVisXL Q4` (`2.6 GB`), `DreamShaper Q4` | Hides `7B–70B`, FP16 SDXL, FLUX, SD 3.5 &amp; GPU video weights cleanly so swap never thrashes | `--ctx-size 2048`, `--n-gpu-layers 0`, `--offload-to-cpu`, `--vae-tiling` |
| **Balanced Laptop / APU** (`15–16 GB` Vulkan UMA or `8 GB` dGPU) | `Qwen 2.5 7B/Coder 7B`, `DeepSeek-R1 7B`, `Qwen 3 8B`, `Mistral Nemo 12B`, `RealVisXL Q4/Q8`, `Juggernaut Q4/Q8`, `SDXL Turbo Q8`, `LTX-Video 2B`, `Cosmos 2B` | Hides `1.5B` netbook clutter plus `14B–70B`, `FP16 SDXL`, `FLUX`, `SD 3.5` &amp; `14B` video that overflow `8 GB` GPU pools | `--ctx-size 4096`, `Vulkan0`/`CUDA`, `--vae-tiling`, `DMD2 5-step (cfg 1.6)` |
| **Workstation / Server** (`24–64+ GB` RAM &amp; `16–48+ GB` VRAM) | `Qwen 2.5 14B/Coder 32B`, `Qwen 3 30B-A3B/32B`, `DeepSeek-R1 14B`, `Phi-4 14B`, `Llama 3.3 70B`, `RealVisXL/Juggernaut FP16`, `FLUX.1-dev/schnell`, `SD 3.5 Large`, `HunyuanVideo`, `CogVideoX 5B` | Hides low-RAM `1.5B–4B` &amp; `Q4` entry fallbacks so flagship full-precision models lead | `--ctx-size 8192`, `--flash-attn on`, full-precision VRAM pipeline |

<br/>

### What Makes Vimona a Harness (Not a Model Fork)

Vimona owns zero proprietary weights and locks you to zero hosted APIs—it is an **end-to-end local orchestration harness** built around open standards:

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/vimona-booster-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/brand/vimona-booster-light.svg">
  <img alt="Vimona Dual Boosting Engine and Hardware-Tuned Inference Stack" src="assets/brand/vimona-booster-dark.svg" width="100%">
</picture>

<br/><br/>

<table>
  <tr>
    <td width="50%" align="center" valign="top">
      <img src="assets/controls-dropdown.png" width="100%" alt="Unified Controls dropdown menu with Quiet/Turbo, Skills, Select Chat, and Alerts"/><br/>
      <sub><strong>Unified Controls Dropdown</strong> — Switch Quiet/Turbo thermals, open <code>469</code> Skills, select multi-turn context, or toggle SafeBlur Alerts.</sub>
    </td>
    <td width="50%" align="center" valign="top">
      <img src="assets/demos/08-boosting-hf-controls.webp" width="100%" alt="Live interactive demo of Controls dropdown, Hardware-Tuned HF Models tabs, and boosted 720p video output"/><br/>
      <sub><strong>Model Picker &amp; Boosted Flow</strong> · <a href="assets/demos/08-boosting-hf-controls.mp4">MP4 Recording</a></sub>
    </td>
  </tr>
</table>

</div>

---

## 469 Skills & Context Engineering

Rather than dumping massive static system prompts into a `4096`-token context window, Vimona indexes **`469` modular domain skills** locally via [`vimona/skills_loader.py`](vimona/skills_loader.py) and scores each prompt by **active mode**, **trigger keywords**, **name tokens**, and **context anchors**—injecting **only the top `2–3` matching skills per turn** alongside zero-bloat runtime context compaction.

<div align="center">

<img src="assets/skills-manager.png" width="94%" alt="Vimona Skills Manager showing 469 active skills across 9 wrapped category pills and clean toggle cards"/>

<br/><br/>

<img src="assets/demos/05-skills-manager.webp" width="90%" alt="Live recording of Skills Manager category filters, search, toggling, and creating a custom .vimona-skill"/>

<sub><strong>Portable <code>.vimona-skill</code> Manager, Search, Toggles &amp; In-App Authoring</strong> · <a href="assets/demos/05-skills-manager.mp4">MP4 Recording</a></sub>

</div>

<br/>

| Skill Category | Count | Focus &amp; Capabilities Injected Automatically |
| :--- | :---: | :--- |
| **Image** (`image`) | **137** | `SDXL` / `RealVisXL` score tag hierarchy (`score_9`…), skin realism, textures, fabrics, environments, styles, negative artifact suppression |
| **Video** (`video`) | **124** | `2B–14B` 5-part temporal motion formula, I2V delta prompts, period/rhythm pacing, `CausVid` LoRA calibration, physics cues |
| **Prompt Engineering** (`prompt`) | **50** | Structured reasoning patterns, constraint decomposition, tag weighting precision, anti-repetition formatting, few-shot scaffolds |
| **Context Engineering** (`context`) | **50** | Pinned `[Previous Visual Prompt Spec]` &amp; `[Zero-Trace Web Research]` anchors, rolling history compaction, signal preservation |
| **Engineering** (`engineering`) | **24** | Python asyncio, GTK4/Libadwaita idiomatic layout, systems debugging, SQL/API safety, test hardening |
| **Lighting** (`lighting`) | **24** | Golden hour grazing rim light, volumetric chiaroscuro, wet neon specular reflections, soft window diffused wrap |
| **Camera** (`camera`) | **23** | 35mm/50mm/85mm prime optics, anamorphic bokeh, aperture depth budgets (`f/1.4`–`f/8`), smooth tracking &amp; dolly paths |
| **Writing & Reasoning** (`writing`) | **22** | Structured prose flow, concise technical precision, narrative beat breakdown, multi-step critiques |
| **Web Research** (`web`) | **15** | Ephemeral `/dev/shm` query synthesis, palette extraction, authentic period signage &amp; real-world reference grounding |

> Every skill supports **one-click Export/Import** as portable `.vimona-skill` files (including drag-and-drop onto Skills Manager), optional negative prompt patches, ComfyUI/sampler overrides, and live toggle persistence in `~/.local/share/vimona/skills_state.json`.

---

## Interactive Workspace Showcase

> [!TIP]
> **All Showcase Media Generated by `2B` Models:** Every generated photo, keyframe strip, and video clip visible in the recordings below was created locally using compact **`2B`-class diffusion models** via Vimona's orchestration and dual booster engines.

Click **MP4 Recording** under any demo card to inspect full-resolution footage recorded directly from the GTK4 canvas.

<table>
  <tr>
    <td width="50%" align="center" valign="top">
      <strong>Mode Lock, Camera Presets &amp; Thinking Trace</strong><br/>
      <sub>Lock a chat to Chat, Images, or Video with transparent collapsible step reasoning</sub><br/><br/>
      <img src="assets/demos/01-studio-modes-thinking.webp" width="100%" alt="Mode lock popup, camera framing presets, and collapsible reasoning trace"/><br/>
      <sub><a href="assets/demos/01-studio-modes-thinking.mp4">MP4 Recording</a></sub>
    </td>
    <td width="50%" align="center" valign="top">
      <strong>Zero-Trace <code>/dev/shm</code> Research &amp; Lightbox</strong><br/>
      <sub>SlideCommit drag gate spawns RAM-only Firefox, extracts references, and wipes</sub><br/><br/>
      <img src="assets/demos/02-ephemeral-research-lightbox.webp" width="100%" alt="SlideCommit research gate, /dev/shm Firefox lookup, and high-DPI lightbox"/><br/>
      <sub><a href="assets/demos/02-ephemeral-research-lightbox.mp4">MP4 Recording</a></sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top">
      <strong>RAM-Relay Swap &amp; 93 KB SafeBlur ML</strong><br/>
      <sub>LLM drops to 0.0 GB before diffusion; normal photography stays unblurred cleanly</sub><br/><br/>
      <img src="assets/demos/03-media-relay-gallery-safeblur.webp" width="100%" alt="Memory relay stream and selective frosted SafeBlur ML overlay"/><br/>
      <sub><a href="assets/demos/03-media-relay-gallery-safeblur.mp4">MP4 Recording</a></sub>
    </td>
    <td width="50%" align="center" valign="top">
      <strong>Overnight Sequential Render Playlist</strong><br/>
      <sub>Queue mixed Image &amp; Video renders with reorder controls and Auto-Clean sweeps</sub><br/><br/>
      <img src="assets/demos/04-overnight-render-playlist.webp" width="100%" alt="Sequential overnight render playlist dock with live job reordering"/><br/>
      <sub><a href="assets/demos/04-overnight-render-playlist.mp4">MP4 Recording</a></sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top">
      <strong>Quiet / Eco Thermal Mode &amp; RAM Monitor</strong><br/>
      <sub>Throttle threads (50%), ionice, &amp; cooldowns; inspect/free protected PIDs safely</sub><br/><br/>
      <img src="assets/demos/06-eco-turbo-ram-monitor.webp" width="100%" alt="Quiet/Eco thermal mode toggle and Protected PID RAM Monitor popover"/><br/>
      <sub><a href="assets/demos/06-eco-turbo-ram-monitor.mp4">MP4 Recording</a></sub>
    </td>
    <td width="50%" align="center" valign="top">
      <strong>Playlist Dock &amp; Eco Banner (High-Res Still)</strong><br/>
      <sub>Compact queued batch workspace while Eco thermal throttling holds thermals cool</sub><br/><br/>
      <img src="assets/render-playlist-eco.png" width="100%" alt="High-resolution render playlist dock and Quiet/Eco mode banner"/><br/>
      <sub>Full-resolution UI snapshot</sub>
    </td>
  </tr>
</table>

---

## Architecture & Isolation

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/vimona-pipeline-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/brand/vimona-pipeline-light.svg">
  <img alt="Vimona Four-Stage Air-Gapped Memory Relay Pipeline" src="assets/brand/vimona-pipeline-dark.svg" width="100%">
</picture>

</div>

1. **Explicit Research Gate (`SlideCommitWidget`)** — External web fetches never happen implicitly. Live lookups launch headless Firefox inside RAM-backed `/dev/shm/vimona_ephem_*` strictly after you slide-confirm, validate public HTTP/HTTPS destinations against SSRF & DNS rebinding, decode candidate images strictly via verified magic bytes & pixel caps in RAM, sanitize scraped text inside untrusted data delimiters, wait for browser exit + execute `scorched_earth_wipe()` before diffusion boots. SafeSearch defaults to explicit `moderate` (`VIMONA_SAFESEARCH=strict|moderate|off`).
2. **Fail-Closed Network & Mount Isolation ([`vimona/airgap.py`](vimona/airgap.py))** — `sd-server` and `ComfyUI` run inside `unshare -r -n -m` (private user, network & `/tmp` tmpfs mount namespaces, `PR_SET_NO_NEW_PRIVS`, `RLIMIT_CORE=0`, verified zero IPv4/IPv6/DNS egress) communicating via an exclusive `0700` `AF_UNIX` bridge socket + `X-Vimona-Bridge-Token` authentication. If namespace creation fails, Vimona **fails closed** instead of silently running unisolated.
3. **Privacy Scope & Host Limits** — `unshare -r -n -m` isolates networking and mounts a private `/tmp` tmpfs, **not** a full VM or root-level sandbox against the user's own home directory, and Linux swap or hibernation can still write RAM pages to disk unless disabled (`zram`/encrypted swap recommended; Vimona reports active swap/filesystem backing honestly via `trace_report`). Chat history (`~/.local/share/vimona/chats.json`) is written atomically (`0600` inside `0700` dir) with `flock`, `.bak` recovery, and corrupt-snapshot quarantine.
4. **Verified Mutual-Exclusion RAM Relay ([`vimona/memory_relay.py`](vimona/memory_relay.py))** — Mutex-locked engine handoffs terminate `llama-server` and ALL descendant PIDs (`SIGINT`/`SIGTERM`/`SIGKILL` + zombie reap), wait for `MemAvailable` settling, verify reclaimed memory, and enforce conservative RAM headroom before booting image/video engines.
5. **Fail-Closed SafeBlur ML Gate ([`vimona/safety_ml.py`](vimona/safety_ml.py))** — Dual pure-NumPy classifier (`512`-dim hashed text n-grams + `22` color/edge/contrast image features, `safety_ml_weights.npz`) + `10`-frame video sampling. Fails **closed** (treats weights missing/corrupt as `unavailable` / blurred) rather than silently allowing unscored media.
6. **Verified Protected PID RAM Guard ([`vimona/ram_manager.py`](vimona/ram_manager.py))** — Scans live `/proc` memory hogs (`RssAnon` + `RssFile`/`RssShmem`) while validating UID, process identity, and `starttime` ticks via `pidfd_send_signal`, shielding desktop compositors and active inference engines from accidental signals.

---

## Hardware Requirements & Sizing (`MemAvailable` + Peak Runtime Budgets)

Vimona estimates **peak inference RAM** (weights + KV cache + CLIP/T5 encoders + batch/frame latents + active OS headroom), not raw `.gguf` download size alone:

| Hardware Tier | Minimum Specs | Supported Workloads | Realistic Expectations |
| :--- | :--- | :--- | :--- |
| **Entry / CPU-Only** | `8 GB` RAM (`>= 3.5 GB` `MemAvailable`), 4+ CPU cores | Small `1.5B–4B` `Q4_K_M` text models, compressed `Q4` SDXL stills | Text inference runs steadily (`10–25 tok/s`); multi-frame video is intentionally hidden to avoid OOM/swap thrash. |
| **Balanced Laptop / APU** | `16 GB` RAM (`>= 8 GB` `MemAvailable`), Vulkan iGPU (`8 GB` GPU Pool) or `8 GB` dGPU | `7B–12B` `Q4/Q8` chat/coding, `DMD2/Lightning` `5–6` step stills, `2B` `CausVid` `4–12s` video | Smooth single-user workflow via zero-overlap RAM relay; first load takes ~5–15s, cached stills take a few seconds. |
| **Workstation** | `24–64+ GB` RAM, discrete `16–48+ GB` CUDA/ROCm GPU | `14B–32B+` reasoning LLMs, `FP16`/`FLUX`/`SD 3.5`, `16–32s` video clips | Longer contexts (`8192+` tokens), larger batches, and full-precision diffusion run directly in VRAM. |

---

## Preflight Diagnostics & Troubleshooting

Before filing an issue or debugging a missing backend, run Vimona's built-in preflight diagnostic:

```bash
python3 run_vimona.py --preflight --json
# or check a standalone bundle:
./downloads/Vimona-Linux-x86_64.run --check
```

- **Environment Overrides:** Override default paths anytime via `VIMONA_STATE_DIR`, `VIMONA_MODELS_DIR`, `VIMONA_LLM_DIR`, `VIMONA_COMFY_DIR`, `VIMONA_LLAMA_BIN`, `VIMONA_SD_BIN`, `VIMONA_FFMPEG_BIN`, `VIMONA_DEFAULT_LLM`, and `VIMONA_SAFESEARCH` (`strict` | `moderate` | `off`).
- **Missing Optional Engines:** If `sd-server`, `ComfyUI`, or `playwright` are absent, Vimona launches in graceful degraded mode and reports disabled capabilities explicitly rather than crashing at startup.
- **Air-Gap Failed (`Isolation: failed`):** Ensure Linux unprivileged user namespaces are enabled (`sysctl kernel.unprivileged_userns_clone=1` on restricted distros) and `unshare` (`util-linux`) is installed. Vimona intentionally refuses unisolated execution when isolation fails.
- **Logs & State Recovery:** Rotating logs with automatic secret/token/prompt redaction live in `~/.local/share/vimona/vimona.log` (`0600`). Corrupted `chats.json` snapshots are automatically quarantined to `chats.corrupt.<ts>.json` and restored cleanly from `chats.json.bak`.
- **Resumable Hugging Face Downloads:** Interrupted downloads preserve `.part` files cleanly and resume via HTTP `Range` requests with pinned SHA-256 checksum & GGUF/safetensors header validation.

---

## Downloads &amp; Installers

Choose the direct download package for your operating system—no terminal commands required.

<table>
  <tr>
    <td width="50%" valign="top">
      <h3>Linux (Multi-Distro Packages &amp; Standalone)</h3>
      <ul>
        <li><strong>Distro Release Packages (Recommended):</strong> <a href="https://github.com/dksh34/Vimona/releases/latest"><strong>GitHub Releases — All Linux Distros</strong></a>
          <ul>
            <li><strong>Debian / Ubuntu / Mint / Pop!_OS:</strong> <code>vimona_1.0.0_amd64.deb</code></li>
            <li><strong>Fedora / RHEL / Nobara / openSUSE:</strong> <code>vimona-1.0.0-1.x86_64.rpm</code></li>
            <li><strong>Arch / Manjaro / CachyOS:</strong> <code>vimona-1.0.0-1-x86_64.pkg.tar.zst</code></li>
            <li><strong>Universal Linux:</strong> <code>Vimona-x86_64.AppImage</code></li>
          </ul>
        </li>
        <li><strong>Direct Portable Bundles:</strong> <a href="downloads/Vimona-Linux-x86_64.run"><code>Vimona-Linux-x86_64.run</code></a> (<code>524 KB</code>) &amp; <a href="downloads/Vimona-Linux-x86_64.tar.gz"><code>Vimona-Linux-x86_64.tar.gz</code></a> (<code>521 KB</code>)</li>
      </ul>
      <sub><strong>How to run:</strong> Install the matching <code>.deb</code>, <code>.rpm</code>, or <code>.pkg.tar.zst</code> package from <a href="https://github.com/dksh34/Vimona/releases/latest"><strong>Releases</strong></a>—or mark <code>Vimona-x86_64.AppImage</code> / <code>Vimona-Linux-x86_64.run</code> executable and launch directly.</sub>
    </td>
    <td width="50%" valign="top">
      <h3>Windows 10 / 11 (Experimental Preview — Linux Primary Today)</h3>
      <ul>
        <li><strong>Experimental Preview Bundle:</strong> <a href="downloads/Vimona-Windows-x86_64.zip"><strong>Download Vimona-Windows-x86_64.zip</strong></a> (<code>543 KB</code> packaging preview; requires MSYS2 UCRT64 GTK4)</li>
        <li><strong>CI Experimental Builds:</strong> <a href="https://github.com/dksh34/Vimona/releases/latest"><strong>GitHub Releases (<code>Vimona-Setup.exe</code> &amp; <code>Vimona.exe</code>)</strong></a> (unverified on end-user Windows hardware; Linux is the primary supported platform)</li>
      </ul>
      <sub><strong>Support Notice:</strong> Kernel network/mount isolation (<code>unshare -r -n -m</code>), volatile <code>/dev/shm</code> browser research, and <code>/proc</code> RAM monitoring are Linux-only today. Treat Windows builds as experimental preview until native Windows isolation &amp; runtime QA are complete.</sub>
    </td>
  </tr>
</table>

<details>
<summary>Keyboard Shortcuts Reference</summary>

| Shortcut | Action |
| :--- | :--- |
| `Ctrl` + `N` | Start new chat (opens mode selector) |
| `Ctrl` + `B` / `Ctrl` + `\` | Toggle conversation sidebar |
| `Ctrl` + `K` | Focus composer input |
| `Ctrl` + `Shift` + `A` | Multi-turn selector |
| `Enter` / `Shift` + `Enter` | Send prompt / Newline |
| `Space` / `←` / `→` / `+` / `-` / `I` | Play/pause, navigate, zoom &amp; toggle inspector inside Lightbox |
| `Esc` | Dismiss active overlay, popup, Lightbox, or modal |

</details>

---

## Project Architecture Map

```text
vimona/
├── run_vimona.py                          # Application entrypoint (Adw.Application)
├── launch-vimona.sh                       # Direct Linux launch wrapper
├── LICENSE                                # MIT License
├── downloads/                             # Direct downloadable Linux .run & portable .tar.gz bundles
├── installers/                            # Linux AppDir/AppImage & Windows .exe/Inno scripts
├── .github/workflows/                     # Automated Linux & Windows CI download workflows
├── assets/
│   ├── vimona.ico                         # Multi-resolution Windows icon (16..256px metallic V)
│   ├── brand/                             # Theme-aware hero, pipeline, booster SVGs & 256px PNG icon
│   ├── demos/                             # Animated WebP + MP4 showcases (01..08)
│   └── *.png                              # High-res framed UI & real DSLR output captures
└── vimona/
    ├── app.py, window.py & ui_popovers.py # GTK4 + Libadwaita shell, Controls & HF Models popovers
    ├── config.py, errors.py, logging_utils.py & preflight.py # Centralized VIMONA_* paths, errors, redacted logs, & startup checks
    ├── tokens.py                          # Graphite dark theme design tokens & GTK4 CSS system
    ├── widgets.py                         # Composer, Image/Video Gallery & Overnight Render Playlist
    ├── hf_models.py                       # Hardware profiler (VRAM/DRM GTT), SHA-256 verifier & curated HF downloader
    ├── controller.py                      # Mode routing, SlideCommit handling & turn orchestration
    ├── memory_relay.py                    # Mutual-exclusion RAM swap & hardware-tuned llama-server
    ├── airgap.py                          # unshare -r -n -m network/mount isolation & authenticated AF_UNIX bridge
    ├── workflows.py                       # ComfyUI 2B sliding-window video & SDXL keyframe graphs
    ├── fast_media_engine.py               # DMD2/Lightning boosted sd-server & 720p DSLR ffmpeg finish
    ├── safety_ml.py & safety_ml_weights.npz # Fail-closed offline NumPy text + vision SafeBlur gate
    ├── search_worker.py                   # Ephemeral /dev/shm headless Firefox reference extractor (SSRF/magic-byte guarded)
    ├── skills_loader.py & skills_manager.py # 469 skills, word-boundary router, schema validator & manager UI
    ├── perf_profile.py                    # Quiet/Eco (50% CPU + cooldowns) vs Turbo profile toggle
    └── ram_manager.py                     # /proc RAM inspector with pidfd + starttime protected PID safeguards
tests/                                     # Formal unittest / pytest verification suite & CI workflow
```

---

## Credits &amp; Acknowledgments

Vimona is a local AI harness powered directly by Hugging Face and extraordinary open-source infrastructure:

- **[Hugging Face Hub (`huggingface.co`)](https://huggingface.co)** — Powers Vimona's curated model catalog, live model repository metadata API (`likes`, `downloads`), and resumable-safe direct `GGUF`/`safetensors` downloads matched cleanly by hardware tier.
- **[ComfyUI (`comfyanonymous/ComfyUI` & Comfy-Org)](https://github.com/comfyanonymous/ComfyUI)** — Powers Vimona's air-gapped node graph backend (`ModelSamplingSD3`, `--lowvram` offloading, CPU UMT5 scheduling, and `VHS_VideoCombine`). Running `2B` diffusion video smoothly inside `16 GB` unified RAM would not be possible without ComfyUI.
- **[llama.cpp (`ggml-org/llama.cpp`)](https://github.com/ggml-org/llama.cpp)** — High-speed Vulkan & CUDA `GGUF` inference server (`llama-server`) for local reasoning, coding, and skill routing.
- **[stable-diffusion.cpp (`leejet/stable-diffusion.cpp`)](https://github.com/leejet/stable-diffusion.cpp)** — Fast Vulkan/GGUF diffusion server (`sd-server`) driving 4–6 step `DMD2` & `Lightning` image boosting with spatial VAE tiling.
- **[LTX-Video (`Lightricks/LTX-Video`)](https://github.com/Lightricks/LTX-Video)** & **[CausVid (`tianweiy/CausVid`)](https://github.com/tianweiy/CausVid)** — Lightweight `2B` video synthesis and 6-step distilled acceleration.
- **Community Model Authors** — Curators & quantizers behind `Qwen 2.5 / Qwen 3`, `Gemma 3`, `Llama 3.1`, `RealVisXL V5.0 Lightning`, `Juggernaut XL v9`, `DreamShaper XL`, `FLUX.1`, and GGUF quantizations (`bartowski`, `city96`, `off-grid-ai`, `RunDiffusion`, `SG161222`).
- **[GTK4](https://www.gtk.org/), [Libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/), [PyGObject](https://pygobject.gnome.org/), [NumPy](https://numpy.org/), [OpenCV](https://opencv.org/) & [FFmpeg](https://ffmpeg.org/)** — Native hardware-accelerated UI, offline ML scoring, and 720p temporal finishing.

---

## License

Released under the **[MIT License](LICENSE)** — Copyright (c) 2026 Dksh. Free to use, fork, package, modify, and redistribute.

---

## Call for Testers &amp; macOS Build

<div align="center">

[![Testers Wanted: Linux & Windows](https://img.shields.io/badge/TESTING%20NEEDED-Linux%20%26%20Windows%20OS%20Apps-2563EB?style=for-the-badge)](https://github.com/dksh34/Vimona/issues/new)
[![macOS Standalone Needed](https://img.shields.io/badge/HELP%20WANTED-macOS%20.app%20%2F%20.dmg%20Build-D97706?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/dksh34/Vimona/pulls)

</div>

> [!IMPORTANT]
> **We Need Real-World Testers Across Every Operating System!**
> Vimona is engineered for constrained consumer laptops as well as full workstations, so hardware testing on different GPUs, RAM sizes, and desktop environments makes the biggest difference. Please test the downloadable desktop apps and tell us what breaks, what looks great, and what models your machine detects.

<table>
  <tr>
    <td width="33.3%" valign="top">
      <h3>Linux Testers</h3>
      <strong>Packages:</strong> <a href="https://github.com/dksh34/Vimona/releases/latest"><code>Releases (.deb / .rpm / Arch / AppImage)</code></a> &amp; <a href="downloads/Vimona-Linux-x86_64.run"><code>.run</code></a><br/><br/>
      <strong>What to check:</strong>
      <ul>
        <li>Distro launch (<code>Fedora</code>, <code>Ubuntu</code>, <code>Debian</code>, <code>Arch</code>, Wayland &amp; X11)</li>
        <li>Hardware-Tuned HF Hub (AMD/Intel Vulkan GTT &amp; NVIDIA CUDA detection)</li>
        <li>Metallic desktop icon &amp; GNOME/KDE scaling</li>
      </ul>
    </td>
    <td width="33.3%" valign="top">
      <h3>Windows Testers</h3>
      <strong>Packages:</strong> <a href="downloads/Vimona-Windows-x86_64.zip"><code>Vimona-Windows-x86_64.zip</code></a> &amp; <a href="https://github.com/dksh34/Vimona/releases/latest"><code>Releases (.exe)</code></a><br/><br/>
      <strong>What to check:</strong>
      <ul>
        <li>Clean launch on Windows 10 &amp; Windows 11 with MSYS2 GTK4 runtime</li>
        <li>NVIDIA CUDA &amp; AMD/Intel GPU hardware profile detection</li>
        <li>Start Menu &amp; Desktop metallic shortcut icon rendering</li>
      </ul>
    </td>
    <td width="33.3%" valign="top">
      <h3>macOS Builders Wanted</h3>
      <strong>Status:</strong> <em>Help Wanted (<code>.app</code> / <code>.dmg</code>)</em><br/><br/>
      <strong>How you can help:</strong>
      <ul>
        <li>We build on Linux &amp; Windows CI, so we cannot compile Mach-O / Quartz GTK4 macOS bundles locally</li>
        <li>Compile &amp; package a standalone <strong>Apple Silicon / Intel <code>Vimona.app</code> or <code>.dmg</code></strong></li>
        <li>Wire Metal / unified memory RAM reporting in <a href="vimona/hf_models.py"><code>hf_models.py</code></a> &amp; open a PR!</li>
      </ul>
    </td>
  </tr>
</table>

> [!TIP]
> Found an edge case, UI glitch, or GPU bug—or built a macOS bundle? **[Open an Issue](https://github.com/dksh34/Vimona/issues)** or **[Submit a Pull Request](https://github.com/dksh34/Vimona/pulls)** with your OS, CPU/GPU model, and RAM size!
