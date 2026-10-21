"""Offline tests: a fake Instagram server stands in for the real API."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from ig.api import InstagramClient, InstagramError  # noqa: E402
from ig.content import Post, Settings, caption_problems, load_queue  # noqa: E402
from ig.publisher import Publisher, select_due  # noqa: E402

TOKEN = "IGAAsecret-token-123"
NOW = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._payload


class FakeInstagram:
    """Just enough of graph.instagram.com to exercise the publisher."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.containers: dict[str, dict] = {}
        self.media: list[dict] = []
        self.next_id = 100
        self.publish_errors: list[dict] = []   # queued error payloads for media_publish
        self.status_sequence = ["IN_PROGRESS", "FINISHED"]

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        fields = dict(params or data or {})
        assert fields.pop("access_token", None) == TOKEN or "rupload" in url
        path = url.split("graph.instagram.com/")[-1].split("/", 1)[-1] if "graph" in url else url
        self.calls.append((method, path, fields))
        if path == "me":
            return Resp(200, {"user_id": "17841", "username": "karvit", "account_type": "BUSINESS"})
        if path.endswith("/content_publishing_limit"):
            return Resp(200, {"data": [{"quota_usage": 1, "config": {"quota_total": 100}}]})
        if path.endswith("/media") and method == "GET":
            return Resp(200, {"data": self.media})
        if path.endswith("/media") and method == "POST":
            cid = str(self.next_id)
            self.next_id += 1
            self.containers[cid] = {"fields": fields, "polls": 0}
            return Resp(200, {"id": cid})
        if path.endswith("/media_publish"):
            if self.publish_errors:
                return Resp(400, {"error": self.publish_errors.pop(0)})
            mid = f"m{self.next_id}"
            self.next_id += 1
            self.media.insert(0, {"id": mid, "caption": self.containers[fields["creation_id"]]["fields"].get("caption"),
                                  "timestamp": "2026-10-25T00:00:00+0000",
                                  "permalink": f"https://www.instagram.com/p/{mid}/"})
            return Resp(200, {"id": mid})
        if path.endswith("/comments"):
            return Resp(200, {"id": "c1"})
        if path in self.containers:
            c = self.containers[path]
            status = self.status_sequence[min(c["polls"], len(self.status_sequence) - 1)]
            c["polls"] += 1
            return Resp(200, {"status_code": status})
        if path.startswith("m"):
            return Resp(200, {"permalink": f"https://www.instagram.com/p/{path}/"})
        return Resp(404, {"error": {"message": f"unknown path {path}", "code": 100}})

    def count(self, suffix, method="POST"):
        return sum(1 for m, p, _ in self.calls if m == method and p.endswith(suffix))


def make_post(pid="p1", type_="image", when=NOW - timedelta(minutes=5), media=None, **kw):
    media = media or (["media/x.mp4"] if type_ == "reel" else ["media/x.jpg"])
    return Post(id=pid, type=type_, when=when, media=media,
                caption=kw.pop("caption", f"Caption for {pid} with enough words to match"), **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "posted.json"
        self.fake = FakeInstagram()
        self.client = InstagramClient(TOKEN, session=self.fake, sleep=lambda s: None,
                                      poll_interval=1, poll_timeout=5)
        self.settings = Settings(media_base_url="https://cdn.example/repo", min_gap_minutes=120)
        self.logs: list[str] = []
        os.environ.pop("GITHUB_ACTIONS", None)
        # media must exist on disk for the publisher's checks
        self._orig_resolve = None

    def tearDown(self):
        self.tmp.cleanup()

    def publisher(self, state=None, dry_run=False):
        pub = Publisher(self.client, self.settings, state if state is not None else {},
                        log=self.logs.append, dry_run=dry_run,
                        url_checker=lambda url: None, state_path=self.state_path)
        pub._media = lambda ref, need_url=True: (  # files "exist" for tests
            f"https://cdn.example/repo/{ref}", None)
        return pub


class SchedulingTests(Base):
    def test_future_and_draft_posts_are_not_due(self):
        posts = [make_post("future", when=NOW + timedelta(hours=1)),
                 make_post("draft", draft=True), make_post("due")]
        due = select_due(posts, {}, self.settings, NOW, self.logs.append)
        self.assertEqual([p.id for p in due], ["due"])

    def test_very_late_post_is_marked_missed_not_posted(self):
        state = {}
        due = select_due([make_post("old", when=NOW - timedelta(hours=13))], state,
                         self.settings, NOW, self.logs.append)
        self.assertEqual(due, [])
        self.assertEqual(state["old"]["status"], "missed")

    def test_rescheduling_gives_a_missed_post_a_fresh_start(self):
        state = {"p": {"status": "missed", "scheduled_for": "2026-10-20T00:00:00+00:00"}}
        due = select_due([make_post("p")], state, self.settings, NOW, self.logs.append)
        self.assertEqual([p.id for p in due], ["p"])

    def test_gives_up_after_max_attempts(self):
        post = make_post("p")
        state = {"p": {"status": "failed", "attempts": 3,
                       "scheduled_for": post.when.astimezone(timezone.utc).isoformat(timespec="seconds")}}
        self.assertEqual(select_due([post], state, self.settings, NOW, self.logs.append), [])
        self.assertEqual(state["p"]["status"], "abandoned")


class PublishingTests(Base):
    def test_image_end_to_end(self):
        state = {}
        out = self.publisher(state).run([make_post("img", alt_text="a tee")], NOW)
        self.assertEqual(out[0].status, "published")
        create = [f for m, p, f in self.fake.calls if p.endswith("/media") and m == "POST"][0]
        self.assertEqual(create["image_url"], "https://cdn.example/repo/media/x.jpg")
        self.assertEqual(create["alt_text"], "a tee")
        self.assertIn("Caption for img", create["caption"])
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["img"]["status"], "published")
        self.assertTrue(saved["img"]["permalink"].startswith("https://www.instagram.com/p/"))

    def test_reel_fields(self):
        post = make_post("r", type_="reel", trial=True, share_to_feed=False, thumb_offset=1500,
                         first_comment="Which colour?")
        self.publisher().run([post], NOW)
        create = [f for m, p, f in self.fake.calls if p.endswith("/media") and m == "POST"][0]
        self.assertEqual(create["media_type"], "REELS")
        self.assertEqual(create["video_url"], "https://cdn.example/repo/media/x.mp4")
        self.assertEqual(create["share_to_feed"], "false")
        self.assertEqual(create["thumb_offset"], 1500)
        self.assertEqual(json.loads(create["trial_params"]), {"graduation_strategy": "SS_PERFORMANCE"})
        self.assertEqual(self.fake.count("/comments"), 1)

    def test_carousel_children_then_parent(self):
        post = make_post("c", type_="carousel", media=["a.jpg", "b.jpg", "clip.mp4"])
        self.publisher().run([post], NOW)
        creates = [f for m, p, f in self.fake.calls if p.endswith("/media") and m == "POST"]
        self.assertEqual(len(creates), 4)
        self.assertTrue(all(f["is_carousel_item"] == "true" for f in creates[:3]))
        self.assertNotIn("caption", creates[0])
        self.assertEqual(creates[2]["media_type"], "VIDEO")
        self.assertEqual(creates[3]["media_type"], "CAROUSEL")
        self.assertEqual(creates[3]["children"], "100,101,102")

    def test_never_double_posts_when_already_live(self):
        post = make_post("dup")
        self.fake.media = [{"id": "m9", "caption": post.full_caption(self.settings),
                            "timestamp": "2026-10-24T23:50:00+0000",
                            "permalink": "https://www.instagram.com/p/m9/"}]
        state = {}
        out = self.publisher(state).run([post], NOW)
        self.assertEqual(self.fake.count("/media_publish"), 0)
        self.assertEqual(state["dup"]["media_id"], "m9")
        self.assertIn("recovered", out[0].detail)

    def test_publish_step_is_not_blindly_retried(self):
        self.fake.publish_errors = [{"message": "Service unavailable", "code": 2}]
        state = {}
        out = self.publisher(state).run([make_post("p")], NOW)
        self.assertEqual(out[0].status, "failed")
        self.assertEqual(self.fake.count("/media_publish"), 1)
        self.assertEqual(state["p"]["attempts"], 1)

    def test_media_not_ready_is_retried(self):
        self.fake.publish_errors = [{"message": "Media not ready", "code": 9007,
                                     "error_subcode": 2207027}]
        out = self.publisher().run([make_post("p")], NOW)
        self.assertEqual(out[0].status, "published")
        self.assertEqual(self.fake.count("/media_publish"), 2)

    def test_min_gap_between_posts(self):
        state = {"earlier": {"status": "published",
                             "published_at": (NOW - timedelta(minutes=30)).isoformat()}}
        out = self.publisher(state).run([make_post("p")], NOW)
        self.assertEqual(out, [])
        self.assertEqual(self.fake.count("/media_publish"), 0)

    def test_one_post_per_run(self):
        posts = [make_post("a", when=NOW - timedelta(minutes=50)), make_post("b")]
        out = self.publisher().run(posts, NOW)
        self.assertEqual([o.post_id for o in out], ["a"])

    def test_container_error_is_reported(self):
        self.fake.status_sequence = ["ERROR"]
        state = {}
        out = self.publisher(state).run([make_post("p")], NOW)
        self.assertEqual(out[0].status, "failed")
        self.assertIn("ERROR", state["p"]["last_error"])

    def test_dry_run_publishes_nothing_and_saves_nothing(self):
        out = self.publisher(dry_run=True).run([make_post("p")], NOW)
        self.assertEqual(out[0].status, "dry-run")
        self.assertEqual(self.fake.count("/media"), 0)
        self.assertFalse(self.state_path.exists())


class SafetyTests(unittest.TestCase):
    def test_token_never_appears_in_errors(self):
        class Boom:
            def request(self, *a, **k):
                raise requests.ConnectionError(f"failed GET ?access_token={TOKEN}")
        client = InstagramClient(TOKEN, session=Boom(), sleep=lambda s: None)
        with self.assertRaises(InstagramError) as ctx:
            client.me()
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_caption_limits(self):
        self.assertTrue(caption_problems("x" * 2201))
        self.assertTrue(caption_problems(" ".join(f"#t{i}" for i in range(31))))
        self.assertEqual(caption_problems("fine #a #b"), [])

    def test_hashtags_merged_and_deduplicated(self):
        s = Settings(caption_footer="Shop link in bio", always_hashtags=["karvit"])
        p = make_post(hashtags=["#Tee", "tee", "karvit"], caption="Hello")
        self.assertEqual(p.full_caption(s), "Hello\n\nShop link in bio\n\n#Tee #karvit")

    def test_example_queue_loads(self):
        settings, posts = load_queue()
        self.assertEqual(settings.timezone, "America/New_York")
        self.assertTrue(all(p.when.tzinfo for p in posts))


if __name__ == "__main__":
    unittest.main()
