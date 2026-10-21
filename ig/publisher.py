"""Decides what is due and publishes it, safely and at most once."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

from .api import InstagramClient, InstagramError
from .content import (Post, Settings, caption_problems, normalise_for_match,
                      resolve_media, save_state)

Log = Callable[[str], None]

VIDEO_EXT = {".mp4", ".mov"}
JSDELIVR_MAX_BYTES = 20 * 1024 * 1024   # jsDelivr won't serve GitHub files over 20 MB


def gh_annotation(kind: str, msg: str) -> str:
    """GitHub Actions log annotation when running in Actions, plain text otherwise."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return f"::{kind}::{msg}"
    return f"{kind.upper()}: {msg}"


@dataclass
class Outcome:
    post_id: str
    status: str            # published | failed | skipped | missed | dry-run
    detail: str = ""
    permalink: str | None = None


# --------------------------------------------------------------- scheduling
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def entry_for(post: Post, state: dict) -> dict:
    """State entry for this post, reset if the post was re-scheduled."""
    entry = state.get(post.id) or {}
    if entry and entry.get("status") != "published" and entry.get("scheduled_for") != _iso(post.when):
        entry = {}  # user moved the post to a new time: give it a fresh start
    return entry


def select_due(posts: list[Post], state: dict, settings: Settings, now: datetime,
               log: Log) -> list[Post]:
    due: list[Post] = []
    for post in posts:
        if post.draft or post.when > now:
            continue
        entry = entry_for(post, state)
        status = entry.get("status")
        if status in {"published", "missed", "abandoned"}:
            continue
        if entry.get("attempts", 0) >= settings.max_attempts:
            state[post.id] = {**entry, "status": "abandoned", "scheduled_for": _iso(post.when)}
            log(gh_annotation("error", f"{post.id}: gave up after {settings.max_attempts} "
                                       f"failed attempts. Fix it and change its 'when' to retry."))
            continue
        if now - post.when > timedelta(hours=settings.max_lateness_hours):
            state[post.id] = {"status": "missed", "scheduled_for": _iso(post.when),
                              "note": f"more than {settings.max_lateness_hours}h late; "
                                      f"change 'when' to post it"}
            log(gh_annotation("warning", f"{post.id}: missed its slot by more than "
                                         f"{settings.max_lateness_hours}h, not posting late. "
                                         f"Give it a new 'when' if you still want it."))
            continue
        due.append(post)
    return due


def last_published_at(state: dict) -> datetime | None:
    stamps = [v.get("published_at") for v in state.values()
              if v.get("status") == "published" and v.get("published_at")]
    return max((datetime.fromisoformat(s) for s in stamps), default=None)


# ----------------------------------------------------------------- media prep
def check_url(url: str, *, session: requests.Session | None = None, tries: int = 3,
              sleep: Callable[[float], None] | None = None) -> None:
    """Make sure Meta will be able to download the file (and warm the CDN)."""
    import time
    http = session or requests
    sleep = sleep or time.sleep
    last = ""
    for attempt in range(tries):
        try:
            r = http.get(url, stream=True, timeout=60)
            ok = r.status_code == 200
            ctype = r.headers.get("Content-Type", "")
            r.close()
            if ok:
                if "text/html" in ctype:
                    raise InstagramError(f"{url} returned a web page, not a media file")
                return
            last = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            last = str(exc)
        if attempt < tries - 1:
            sleep(10)
    raise InstagramError(f"Media URL not reachable ({last}): {url}. Is the repo public "
                         f"and the file committed?")


class Publisher:
    def __init__(self, client: InstagramClient | None, settings: Settings, state: dict,
                 *, log: Log = print, dry_run: bool = False,
                 url_checker: Callable[[str], None] = check_url,
                 state_path: Path | None = None):
        self.client = client
        self.settings = settings
        self.state = state
        self.log = log
        self.dry_run = dry_run
        self.check_url = url_checker
        self.state_path = state_path
        self.user_id: str | None = None

    # -------------------------------------------------------------- helpers
    def _persist(self) -> None:
        if self.dry_run:
            return
        if self.state_path:
            save_state(self.state, self.state_path)
        else:
            save_state(self.state)

    def _media(self, ref: str, *, need_url: bool = True) -> tuple[str | None, Path | None]:
        url, local = resolve_media(ref, self.settings)
        if local is not None and not local.exists():
            raise InstagramError(f"Media file not found in repo: {ref}")
        if need_url and not url:
            raise InstagramError("No public media URL. Run this inside GitHub Actions on a "
                                 "public repo, or set MEDIA_BASE_URL.")
        return url, local

    def _already_on_instagram(self, caption: str) -> dict | None:
        """Guard against double-posting if a previous run published but crashed
        before saving state."""
        key = normalise_for_match(caption)
        if len(key) < 20:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(days=4)
        for m in self.client.recent_media(self.user_id, limit=25):
            ts = m.get("timestamp")
            try:
                when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z") if ts else None
            except ValueError:
                when = None
            if when and when < cutoff:
                continue
            if normalise_for_match(m.get("caption")) == key:
                return m
        return None

    # ------------------------------------------------------ container build
    def _video_container(self, ref: str, base: dict, *, carousel_item: bool = False) -> str:
        mode = self.settings.reel_upload
        url, local = self._media(ref, need_url=False)
        size = local.stat().st_size if local else 0
        use_resumable = local is not None and not carousel_item and (
            mode == "resumable" or (mode == "auto" and size > JSDELIVR_MAX_BYTES)
            or (mode == "url" and size > JSDELIVR_MAX_BYTES and not os.environ.get("MEDIA_BASE_URL")))
        if use_resumable:
            self.log(f"  uploading {ref} directly ({size / 1e6:.1f} MB, resumable)")
            try:
                out = self.client.create_container(self.user_id, upload_type="resumable", **base)
                self.client.upload_video(out["id"], str(local), out.get("uri"))
                return out["id"]
            except InstagramError as exc:
                if not url or size > JSDELIVR_MAX_BYTES:
                    raise
                self.log(gh_annotation("warning", f"resumable upload refused ({exc}); "
                                                  f"falling back to URL"))
        if not url:
            raise InstagramError("No public media URL for video (see README: MEDIA_BASE_URL).")
        self.check_url(url)
        fields = {**base, "video_url": url}
        if carousel_item:
            fields.update(is_carousel_item="true")
        return self.client.create_container(self.user_id, **fields)["id"]

    def _image_container(self, ref: str, *, caption: str | None = None,
                         alt_text: str | None = None, carousel_item: bool = False) -> str:
        url, _ = self._media(ref)
        self.check_url(url)
        fields = {"image_url": url, "alt_text": alt_text}
        if carousel_item:
            fields["is_carousel_item"] = "true"
        else:
            fields["caption"] = caption
        return self.client.create_container(self.user_id, **fields)["id"]

    def build_container(self, post: Post, caption: str) -> str:
        if post.type == "image":
            cid = self._image_container(post.media[0], caption=caption, alt_text=post.alt_text)
        elif post.type == "reel":
            base = {"media_type": "REELS", "caption": caption,
                    "share_to_feed": "true" if post.share_to_feed else "false",
                    "thumb_offset": post.thumb_offset}
            if post.cover:
                base["cover_url"] = self._media(post.cover)[0]
            if post.trial:
                base["trial_params"] = json.dumps({"graduation_strategy": "SS_PERFORMANCE"})
            cid = self._video_container(post.media[0], base)
        else:  # carousel
            children = []
            for ref in post.media:
                if Path(ref.split("?")[0]).suffix.lower() in VIDEO_EXT:
                    child = self._video_container(ref, {"media_type": "VIDEO"}, carousel_item=True)
                else:
                    child = self._image_container(ref, alt_text=post.alt_text, carousel_item=True)
                children.append(child)
            for child in children:
                self.client.wait_until_ready(child)
            cid = self.client.create_container(self.user_id, media_type="CAROUSEL",
                                               children=",".join(children),
                                               caption=caption)["id"]
        self.client.wait_until_ready(cid)
        return cid

    # ------------------------------------------------------------- publish
    def publish_post(self, post: Post) -> Outcome:
        caption = post.full_caption(self.settings)
        problems = caption_problems(caption)
        if problems:
            raise InstagramError(f"caption: {'; '.join(problems)}")

        if self.dry_run:
            self.log(f"  [dry-run] would publish {post.type} '{post.id}'")
            for ref in post.media:
                url, _ = self._media(ref, need_url=False)
                where = url or "(URL is set inside GitHub Actions)"
                self.log(f"    media: {ref} -> {where}")
                if url and os.environ.get("GITHUB_ACTIONS") == "true":
                    self.check_url(url)
            self.log("    caption:\n      " + caption.replace("\n", "\n      "))
            return Outcome(post.id, "dry-run")

        existing = self._already_on_instagram(caption)
        if existing:
            self.log(gh_annotation("warning", f"{post.id}: already on Instagram "
                                              f"({existing.get('permalink')}); recording it, not reposting"))
            self.state[post.id] = {"status": "published", "media_id": existing["id"],
                                   "permalink": existing.get("permalink"),
                                   "published_at": existing.get("timestamp") and _iso(
                                       datetime.strptime(existing["timestamp"], "%Y-%m-%dT%H:%M:%S%z")),
                                   "scheduled_for": _iso(post.when), "recovered": True}
            self._persist()
            return Outcome(post.id, "published", "recovered (was already live)",
                           existing.get("permalink"))

        container = self.build_container(post, caption)
        media_id = self.client.publish(self.user_id, container)
        link = self.client.permalink(media_id)
        self.state[post.id] = {"status": "published", "media_id": media_id, "permalink": link,
                               "published_at": _iso(datetime.now(timezone.utc)),
                               "scheduled_for": _iso(post.when)}
        self._persist()  # save immediately: the post is live now

        if post.first_comment:
            try:
                self.client.comment(media_id, str(post.first_comment))
            except InstagramError as exc:
                self.log(gh_annotation("warning", f"{post.id}: posted, but first comment "
                                                  f"failed: {exc}"))
        return Outcome(post.id, "published", f"media {media_id}", link)

    def run(self, posts: list[Post], now: datetime, *, force_id: str | None = None) -> list[Outcome]:
        outcomes: list[Outcome] = []

        if force_id:
            matches = [p for p in posts if p.id == force_id]
            if not matches:
                raise SystemExit(f"No post with id '{force_id}' in the queue")
            due = matches
        else:
            due = select_due(posts, self.state, self.settings, now, self.log)
            self._persist()

        upcoming = [p for p in posts if p.when > now and not p.draft][:3]
        if not due:
            self.log("Nothing due right now.")
            for p in upcoming:
                self.log(f"  next: {p.id} at {p.when.astimezone(self.settings.tz):%a %d %b %H:%M %Z}")
            return outcomes

        if self.client:
            me = self.client.me()
            self.user_id = str(me.get("user_id") or me.get("id"))
            self.log(gh_annotation("notice", f"Connected as @{me.get('username')} "
                                             f"({me.get('account_type')})"))
            try:
                limit = self.client.publishing_limit(self.user_id)
                used = limit.get("quota_usage")
                total = (limit.get("config") or {}).get("quota_total")
                if used is not None and total and used >= total:
                    self.log(gh_annotation("warning", f"Daily API publishing limit reached "
                                                      f"({used}/{total}); will retry next run"))
                    return outcomes
            except InstagramError:
                pass  # informational only

        if not force_id and not self.dry_run:
            last = last_published_at(self.state)
            gap = timedelta(minutes=self.settings.min_gap_minutes)
            if last and now - last < gap:
                wait = int((gap - (now - last)).total_seconds() // 60)
                self.log(f"{len(due)} post(s) due, but the last post went out less than "
                         f"{self.settings.min_gap_minutes} min ago. Waiting ~{wait} min.")
                return outcomes

        limit_n = 1 if force_id else max(1, self.settings.max_posts_per_run)
        for post in due[:limit_n]:
            self.log(f"Publishing {post.type} '{post.id}' (scheduled "
                     f"{post.when.astimezone(self.settings.tz):%a %d %b %H:%M %Z})")
            entry = entry_for(post, self.state)
            try:
                outcome = self.publish_post(post)
            except InstagramError as exc:
                hint = (" Your access token is invalid or expired: generate a new one and "
                        "update the IG_ACCESS_TOKEN secret (README, step 3-4)."
                        if exc.token_problem else "")
                self.log(gh_annotation("error", f"{post.id}: {exc}{hint}"))
                if not self.dry_run:
                    self.state[post.id] = {
                        **entry, "status": "failed", "attempts": entry.get("attempts", 0) + 1,
                        "last_error": str(exc)[:500], "scheduled_for": _iso(post.when),
                        "last_attempt_at": _iso(datetime.now(timezone.utc))}
                    self._persist()
                outcome = Outcome(post.id, "failed", str(exc))
                if exc.token_problem:
                    outcomes.append(outcome)
                    break
            outcomes.append(outcome)
            if outcome.permalink:
                self.log(f"  live: {outcome.permalink}")
        return outcomes


def write_summary(outcomes: list[Outcome]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not outcomes:
        return
    lines = ["| Post | Result | Link |", "|---|---|---|"]
    for o in outcomes:
        link = f"[open]({o.permalink})" if o.permalink else ""
        detail = o.detail.replace("|", "/")[:150]
        lines.append(f"| {o.post_id} | {o.status} {detail} | {link} |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
