"""Command line:  python -m ig <command>

  publish [--dry-run] [--post ID]   publish whatever is due (used by GitHub Actions)
  validate                          check queue + media against Instagram's rules
  schedule                          list posts with US and India times
  whoami                            test the access token
  refresh-token [--update-secret]   renew the 60-day token (weekly workflow)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

from .api import InstagramClient, InstagramError
from .content import QueueError, load_queue, load_state, utcnow
from .publisher import Publisher, gh_annotation, write_summary
from .validate import schedule_lines, validate


def _client(settings=None, required=True) -> InstagramClient | None:
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not token:
        if required:
            sys.exit(gh_annotation("error", "IG_ACCESS_TOKEN is not set. Add it as a "
                                            "repository secret (README, step 4)."))
        return None
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{token}")
    return InstagramClient(token, version=getattr(settings, "api_version", "v25.0"))


def _now(arg: str | None) -> datetime:
    if not arg:
        return utcnow()
    dt = datetime.fromisoformat(arg)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cmd_publish(args) -> int:
    settings, posts = load_queue()
    errors, _ = validate(settings, [p for p in posts if not p.draft])
    client = _client(settings, required=not args.dry_run)
    if client is None:
        print(gh_annotation("warning", "No IG_ACCESS_TOKEN secret, so the Instagram "
                                       "connection was not tested (README step 4)."))
    pub = Publisher(client, settings, load_state(), dry_run=args.dry_run)
    if errors:
        for e in errors:
            print(gh_annotation("warning", f"validation: {e}"))
    outcomes = pub.run(posts, _now(args.now), force_id=args.post or None)
    write_summary(outcomes)
    return 1 if any(o.status == "failed" for o in outcomes) else 0


def cmd_validate(_args) -> int:
    settings, posts = load_queue()
    errors, warns = validate(settings, posts)
    for w in warns:
        print(gh_annotation("warning", w))
    for e in errors:
        print(gh_annotation("error", e))
    print(f"{len(posts)} post(s) checked: {len(errors)} error(s), {len(warns)} warning(s)")
    return 1 if errors else 0


def cmd_schedule(_args) -> int:
    settings, posts = load_queue()
    lines = schedule_lines(settings, posts, utcnow())
    print("\n".join(lines) if lines else "Queue is empty.")
    return 0


def cmd_whoami(_args) -> int:
    settings, _ = load_queue()
    client = _client(settings)
    me = client.me()
    print(f"Token OK: @{me.get('username')}  type={me.get('account_type')}  "
          f"posts={me.get('media_count')}  id={me.get('user_id')}")
    if me.get("account_type") not in {"BUSINESS", "MEDIA_CREATOR", "CREATOR"}:
        print(gh_annotation("warning", "Account must be Business or Creator to publish."))
    return 0


def cmd_refresh(args) -> int:
    settings, _ = load_queue()
    client = _client(settings)
    out = client.refresh_token()
    new = out.get("access_token")
    if not new:
        sys.exit(gh_annotation("error", f"Refresh returned no token: {out}"))
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{new}")
    days = int(out.get("expires_in", 0)) // 86400
    print(f"Got a fresh token, valid ~{days} days.")
    if not args.update_secret:
        print("Not saving it (run with --update-secret inside the workflow).")
        return 0
    if not os.environ.get("GH_TOKEN"):
        sys.exit(gh_annotation("error", "GH_PAT secret is missing, so the new token can't "
                                        "be saved. See README step 4b."))
    # Token goes in via stdin so it never appears in a process list or log.
    res = subprocess.run(["gh", "secret", "set", "IG_ACCESS_TOKEN",
                          "--repo", os.environ["GITHUB_REPOSITORY"]],
                         input=new, text=True, capture_output=True)
    if res.returncode != 0:
        sys.exit(gh_annotation("error", "Couldn't update the secret: "
                                        + res.stderr.replace(new, "***")[:300]))
    print("IG_ACCESS_TOKEN secret updated.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ig", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--post", help="publish this post id right now, ignoring its time")
    p.add_argument("--now", help=argparse.SUPPRESS)  # testing aid
    p.set_defaults(fn=cmd_publish)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    sub.add_parser("schedule").set_defaults(fn=cmd_schedule)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    r = sub.add_parser("refresh-token")
    r.add_argument("--update-secret", action="store_true")
    r.set_defaults(fn=cmd_refresh)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except QueueError as exc:
        print(gh_annotation("error", f"queue.yml: {exc}"))
        return 1
    except InstagramError as exc:
        print(gh_annotation("error", str(exc)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
