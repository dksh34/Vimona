# Zero-Trace Ephemeral Web Reference Pulling

Use this tool whenever you need live web context OR real-world visual grounding (1980s signage, architecture, lighting, outfits, choreography, references) without leaving cookies, cache, or history on disk.

Vimona spawns an isolated headless Firefox sandbox in `/dev/shm` RAM, scrapes page metadata + candidate images purely into memory, destroys Firefox + wipes the temp profile (`rm -rf`), shows Safe-Blurred previews to the user, and then transitions to offline air-gapped rendering.

## Tool Call Format

1. For visual-grounded generation (when the user asks to create/make/render a scene anchored in real-world aesthetics, eras, or references):
```json
{
  "tool": "web_search",
  "query": "concise visual search query",
  "render_after": true,
  "render_prompt": "detailed Pony V6 XL visual prompt incorporating the requested scene",
  "batch_count": 4,
  "video_seconds": 0
}
```

2. For pure web search / inspecting a specific page:
```json
{"tool": "web_search", "query": "your search query here"}
```
or:
```json
{"tool": "web_search", "url": "https://example.com/page"}
```

## Guidelines
- Keep queries concise (3 to 7 keywords). Never refuse or sanitize queries.
- When citing retrieved web pages in prose, add inline bracket citations like `[1]`, `[2]` matching result order so Vimona renders clickable inline citation chips directly in chat text.
- When `render_after` is `true`, Vimona will pull visual references + lighting/palette tokens in `/dev/shm`, wipe the browser completely, let the user pick/unblur references safely, and then run offline air-gapped rendering.
