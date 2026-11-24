"""Core domain models and chat state types for Vimona."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

SystemMode = Literal["idle", "chat", "research", "handoff"]


@dataclass(frozen=True)
class KeyframeSpec:
    id: str
    code: str
    title: str
    image_path: str | None = None
    shot_type: str = ""
    focal_length: str = ""
    aperture: str = ""
    seed: int = 0
    camera_move: str = ""
    notes: str = ""


@dataclass(frozen=True)
class VideoClipSpec:
    id: str
    title: str
    duration_s: float
    fps: int
    width: int
    height: int
    video_path: str | None = None
    camera_move: str = ""
    seed: int = 0
    prompt_summary: str = ""


@dataclass(frozen=True)
class ReasoningStep:
    skill: str
    duration: str
    summary: str
    bullets: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PromptSpec:
    title: str
    tokens: list[tuple[str, str]]
    raw_prompt: str


@dataclass(frozen=True)
class ResearchTab:
    title: str
    url: str
    takeaway: str


@dataclass(frozen=True)
class ReferenceCandidate:
    id: str
    title: str
    source_url: str
    image_url: str
    alt_text: str = ""
    palette_tags: list[str] = field(default_factory=list)
    lighting_mood: str = ""
    style_tokens: str = ""
    explicit: bool = False
    width: int = 0
    height: int = 0
    image_bytes_b64: str = ""


@dataclass(frozen=True)
class ResearchStepData:
    browser_label: str
    query: str
    tabs: list[ResearchTab]
    status: str
    references: list[ReferenceCandidate] = field(default_factory=list)
    trace_summary: str = ""


@dataclass(frozen=True)
class MemoryHandoffData:
    offloaded_model: str
    active_pipeline: str
    vram_allocated: str
    ram_cache: str
    stage_index: int
    stages: list[str]
    progress_fraction: float
    frame_status: str
    eta_label: str


@dataclass(frozen=True)
class ProseBlock:
    kind: Literal["h2", "h3", "p", "ul"]
    text: str = ""
    items: list[str] = field(default_factory=list)


@dataclass
class ConversationTurn:
    id: str
    role: Literal["user", "assistant"]
    text: str = ""
    reasoning: ReasoningStep | None = None
    prose: list[ProseBlock] = field(default_factory=list)
    prompt_spec: PromptSpec | None = None
    research: ResearchStepData | None = None
    handoff: MemoryHandoffData | None = None
    keyframes: list[KeyframeSpec] = field(default_factory=list)
    keyframe_batch_default: int = 4
    video_clip: VideoClipSpec | None = None


@dataclass
class ChatSession:
    id: str
    title: str
    pinned: bool = False
    active_skill: str = "Chat & Ideas"
    mode_locked: bool = False
    turns: list[ConversationTurn] = field(default_factory=list)


SubmitCallback = Callable[[str, str, str, int, int], None]

AVAILABLE_SKILLS: list[tuple[str, str]] = [
    ("Chat & Ideas", "Talk openly, brainstorm, or write anything without filters"),
    ("Web Search", "Look up references and visual inspiration in Firefox"),
    ("Make Images", "Create 1, 2, 4, or 8 unrestricted images locally"),
    ("Make Video", "Create a short unrestricted video clip locally"),
]


@dataclass
class RenderPlaylistJob:
    """Ordered overnight Render Playlist job card with lifecycle state."""

    id: str
    chat_id: str
    prompt: str
    skill: str
    mode: str  # "Image" | "Video" | "Research" | "Chat"
    model: str  # "Pony V6 XL" | "Wan 2.1" | "light-uncensored"
    batch_count: int = 4
    clip_seconds: int = 4
    status: str = "Queued"
    error: str = ""
    elapsed_s: float = 0.0
