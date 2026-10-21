"""Loading the content queue (content/queue.yml) and the posted-state file."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
QUEUE_FILE = ROOT / "content" / "queue.yml"
STATE_FILE = ROOT / "content" / "posted.json"

POST_TYPES = {"image", "reel", "carousel"}
CAPTION_LIMIT = 2200
HASHTAG_LIMIT = 30
MENTION_LIMIT = 20
HASHTAG_RE = re.compile(r"(?<!\w)#\w+", re.UNICODE)
MENTION_RE = re.compile(r"(?<!\w)@[\w.]+", re.UNICODE)


class QueueError(ValueError):
    pass


@dataclass
class Settings:
    timezone: str = "America/New_York"
    max_posts_per_run: int = 1
    min_gap_minutes: int = 120
    max_lateness_hours: float = 12
    max_attempts: int = 3
    reel_upload: str = "url"            # url | resumable | auto
    media_base_url: str | None = None
    caption_footer: str = ""
    always_hashtags: list[str] = field(default_factory=list)
    api_version: str = "v25.0"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass
class Post:
    id: str
    type: str
    when: datetime
    media: list[str]
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    first_comment: str | None = None
    alt_text: str | None = None
    share_to_feed: bool = True
    thumb_offset: int | None = None
    cover: str | None = None
    trial: bool = False
    draft: bool = False

    def full_caption(self, settings: Settings) -> str:
        parts = [self.caption.strip()]
        if settings.caption_footer.strip():
            parts.append(settings.caption_footer.strip())
        tags = _normalise_tags(self.hashtags + settings.always_hashtags)
        if tags:
            parts.append(" ".join(tags))
        return "\n\n".join(p for p in parts if p)

    def fingerprint(self) -> str:
        """Used to recognise this post on Instagram if state was lost."""
        return normalise_for_match(self.caption)


def _normalise_tags(tags: list[str]) -> list[str]:
    seen, out = set(), []
    for t in tags:
        t = str(t).strip()
        if not t:
            continue
        t = t if t.startswith("#") else f"#{t}"
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def normalise_for_match(text: str | None) -> str:
    text = (text or "").lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120]


def _parse_when(raw, tz: ZoneInfo, post_id: str) -> datetime:
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).strip().replace("T", " "))
        except ValueError:
            raise QueueError(f"[{post_id}] 'when' must look like 2026-10-24 19:30 "
                             f"(got {raw!r})") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def load_queue(path: Path = QUEUE_FILE) -> tuple[Settings, list[Post]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw_settings = data.get("settings") or {}
    known = set(Settings.__dataclass_fields__)
    unknown = set(raw_settings) - known
    if unknown:
        raise QueueError(f"Unknown settings: {sorted(unknown)}")
    settings = Settings(**raw_settings)
    if settings.reel_upload not in {"url", "resumable", "auto"}:
        raise QueueError("settings.reel_upload must be url, resumable or auto")

    posts: list[Post] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(data.get("posts") or [], start=1):
        if not isinstance(item, dict):
            raise QueueError(f"Post #{i} is not a mapping")
        pid = str(item.get("id") or "").strip()
        if not pid:
            raise QueueError(f"Post #{i} has no id")
        if pid in seen_ids:
            raise QueueError(f"Duplicate post id: {pid}")
        seen_ids.add(pid)
        ptype = str(item.get("type", "")).strip().lower()
        if ptype not in POST_TYPES:
            raise QueueError(f"[{pid}] type must be one of {sorted(POST_TYPES)}")
        media = item.get("media")
        media = [media] if isinstance(media, str) else list(media or [])
        if not media:
            raise QueueError(f"[{pid}] needs 'media'")
        if ptype in {"image", "reel"} and len(media) != 1:
            raise QueueError(f"[{pid}] a {ptype} takes exactly one media file")
        if ptype == "carousel" and not 2 <= len(media) <= 20:
            raise QueueError(f"[{pid}] a carousel takes 2-20 media files")
        hashtags = item.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = hashtags.split()
        posts.append(Post(
            id=pid, type=ptype,
            when=_parse_when(item.get("when"), settings.tz, pid),
            media=[str(m) for m in media],
            caption=str(item.get("caption") or ""),
            hashtags=[str(h) for h in hashtags],
            first_comment=item.get("first_comment"),
            alt_text=item.get("alt_text"),
            share_to_feed=bool(item.get("share_to_feed", True)),
            thumb_offset=item.get("thumb_offset"),
            cover=item.get("cover"),
            trial=bool(item.get("trial", False)),
            draft=bool(item.get("draft", False)),
        ))
    posts.sort(key=lambda p: p.when)
    return settings, posts


def caption_problems(caption: str) -> list[str]:
    problems = []
    if len(caption) > CAPTION_LIMIT:
        problems.append(f"caption is {len(caption)} chars (max {CAPTION_LIMIT})")
    n_tags = len(HASHTAG_RE.findall(caption))
    if n_tags > HASHTAG_LIMIT:
        problems.append(f"{n_tags} hashtags (max {HASHTAG_LIMIT})")
    n_mentions = len(MENTION_RE.findall(caption))
    if n_mentions > MENTION_LIMIT:
        problems.append(f"{n_mentions} @mentions (max {MENTION_LIMIT})")
    return problems


# ----------------------------------------------------------------- media URLs
def is_url(ref: str) -> bool:
    return ref.startswith("http://") or ref.startswith("https://")


def media_base_url(settings: Settings) -> str | None:
    """Where Instagram's servers can download our files from.

    Default: jsDelivr's free CDN in front of this (public) GitHub repo, pinned to
    the exact commit being published, so the URL never changes under Meta.
    """
    base = os.environ.get("MEDIA_BASE_URL") or settings.media_base_url
    if base:
        return base.rstrip("/") + "/"
    repo, sha = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_SHA")
    if repo and sha:
        return f"https://cdn.jsdelivr.net/gh/{repo}@{sha}/"
    return None


def resolve_media(ref: str, settings: Settings) -> tuple[str | None, Path | None]:
    """Return (public_url, local_path) for a media reference."""
    if is_url(ref):
        return ref, None
    local = (ROOT / ref).resolve()
    base = media_base_url(settings)
    url = base + Path(ref).as_posix().lstrip("/") if base else None
    return url, local


# ---------------------------------------------------------------------- state
def load_state(path: Path = STATE_FILE) -> dict:
    if not Path(path).exists():
        return {}
    text = Path(path).read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
