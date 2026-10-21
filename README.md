# KARVIT Instagram autopilot

Posts reels, photos and carousels to Instagram on a schedule, hands-free, through
Instagram's official API. It runs free on GitHub Actions, so your laptop can be off
and you can be asleep when US prime time hits.

```
you (or Claude) add files + a line in content/queue.yml  →  git push
        │
        ├─ "Check queue" workflow: validates media & captions (red ✗ if something's wrong)
        │
        └─ "Publish to Instagram" workflow, every 30 min:
              due post? → Instagram fetches the file → container ready → publish
              → records it in content/posted.json (so it never posts twice)
        "Refresh Instagram token" workflow, weekly: keeps the 60-day token alive
```

**What it does:** official API only (no bots, no password, no ban risk from
automation); posts at New York time; one post per run with a 2-hour minimum gap;
won't post something 12+ hours late; never double-posts, even if a run crashes
mid-way; retries a failing post 3 times then flags it; optional Trial Reels
(shown to non-followers first) and auto first comment; GitHub emails you when
anything fails.

**What the API can't do:** trending Instagram audio, product/shopping tags,
Stories with stickers, collab invites. Add your own licensed music with
`make_reel.py --music`.

---

## One-time setup (≈30 min)

### 1. Make the Instagram account a Business account
Instagram app → Settings → **Account type and tools** → **Switch to professional
account** → **Business** → category *Clothing (Brand)*. (Creator also works.)

### 2. Put this folder on GitHub as a **public** repo
1. github.com → **New repository** → name `karvit-instagram` → **Public** → Create.
2. Push this folder:
   ```bash
   cd karvit-instagram
   git init -b main && git add . && git commit -m "KARVIT autopilot"
   git remote add origin https://github.com/<your-username>/karvit-instagram.git
   git push -u origin main
   ```
   (Uploading through the website can silently skip the `.github` folder, so use git
   or GitHub Desktop.)

**Why public:** Instagram's servers must download each photo/video from a public
link. The publisher serves your files through jsDelivr's free CDN, pinned to the
exact commit, which only works for public repos. Public repos also get unlimited
free Actions minutes. Your **token stays private** in encrypted secrets and is
masked in logs. What becomes visible: the files and captions in the queue
(including scheduled-but-not-yet-posted ones) and the posted log. If that bothers
you, see *Private repo* at the bottom.

### 3. Create a Meta app and get your token
1. Go to **developers.facebook.com** → log in → **My Apps** → **Create app**.
2. Use case: **Manage messaging & content on Instagram**. Skip the business
   portfolio. Create.
3. In the app: **Instagram → API setup with Instagram login**.
4. Under **Generate access tokens** → **Add account** → log in as your KARVIT
   Instagram account → allow everything it asks for. You need at least
   `instagram_business_basic`, `instagram_business_content_publish`, and
   `instagram_business_manage_comments` (only for the auto first comment).
5. Click **Generate token** next to the account and copy it. It's a long-lived
   token (60 days); the weekly workflow renews it.

If Meta says the account must be a tester: **App roles → Roles → Add people →
Instagram Tester** → enter your handle, then in Instagram go to **Settings →
Website permissions → Apps and websites → Tester invites → Accept**, and retry
step 4.

Leave the app in **Development** mode. No App Review is needed because only your
own account uses it.

### 4. Add two secrets to the repo
Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `IG_ACCESS_TOKEN` | the token from step 3 |
| `GH_PAT` | see 4b |

**4b. `GH_PAT`** lets the weekly job save the renewed Instagram token. GitHub →
your avatar → **Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**:
- Repository access: **Only select repositories** → `karvit-instagram`
- Permissions → Repository → **Secrets: Read and write** (nothing else)
- Expiration: 1 year (put a reminder in your calendar to regenerate it)

### 5. Test it
1. Repo → **Actions** tab → enable workflows if asked.
2. **Publish to Instagram → Run workflow** (keep *Dry run* ticked) → open the run.
   You should see `Connected as @yourhandle (BUSINESS)`.
3. **Refresh Instagram token → Run workflow** → should say the secret was updated.
4. For a real test post: add a post to the queue with `when:` a few minutes ago,
   push, then **Run workflow** with *Dry run* unticked (or just wait ≤30 min).

Done. From here it runs by itself.

### 6. Approve week 1
Your first push also starts **Build media**, which downloads your Etsy photos
and makes week 1's 5 reels and 2 carousels (about 5 minutes).
1. Actions → **Build media** → open the finished run. The summary shows a
   preview strip of every reel and carousel.
2. Want a different photo? Swap its link in `content/build.yml` (Etsy listing →
   right-click the photo → *Copy image address*) and commit. It rebuilds itself.
3. Happy? In `content/queue.yml`, delete the `draft: true` line of each post
   (you can do this on GitHub's website: open the file → pencil icon → Commit).

---

## Day-to-day: adding posts

1. Put the file in `media/` (`media/reels/`, `media/posts/`, `media/carousels/<name>/`).
2. Add an entry to `content/queue.yml`:
   ```yaml
   - id: stay-wired-launch          # unique, never reuse
     type: reel                     # reel | image | carousel
     when: 2026-10-30 19:30         # New York time
     media: media/reels/stay-wired.mp4
     caption: |
       POV: your tee finally matches your playlist 🎧
       Which colour are you grabbing?
     hashtags: [graphictee, streetwearstyle, giftsforhim]
     first_comment: "Comment your favourite colour 👇"   # optional
     trial: true                                          # optional Trial Reel
   ```
3. `git add . && git commit -m "queue: stay wired" && git push`

The footer (`🛒 Shop KARVIT on Etsy — link in bio`) and `#karvit` are added to
every caption from `settings:` at the top of the file. Hashtags are de-duplicated,
and captions are checked against Instagram's limits (2,200 characters, 30 hashtags).

**Media straight from Etsy:** add photo links and a reel/carousel entry to
`content/build.yml`, and the **Build media** workflow makes the files for you
(hook on slide 1, your text on the following slides, call-to-action at the end).
Claude can write both the build entries and the queue entries for a whole week
from your Etsy shop link.

### Changing or cancelling
- **Move a post:** change its `when`. A post that failed or was missed gets a
  fresh start at its new time.
- **Hold a post:** add `draft: true`.
- **Already published:** nothing in the queue can delete it. Delete it in the app.
- `python -m ig schedule` lists everything with New York **and** India times.

### Making media locally
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt                        # plus ffmpeg for reels

# reel from mockups (hook on slide 1, call to action on the last)
python tools/make_reel.py front.png back.png lifestyle.png \
  --hook "Your new favourite tee is here" --text "Black or cream?" \
  --music path/to/track.mp3 --out media/reels/stay-wired.mp4

# any mockup → Instagram-ready 4:5 JPEG (never crops the product)
python tools/prep_image.py mockup.png --out media/posts/stay-wired.jpg

python -m ig validate     # same checks the workflow runs
```
Drop your brand font (`.ttf`/`.otf`, ideally with "Bold" in the name) into
`assets/fonts/` and both tools use it automatically.

---

## When to post (US audience)

Start with these two slots, then check **Instagram → Insights → Total
followers → Most active times** after 3–4 weeks and adjust.

| Slot | New York | India (IST) |
|---|---|---|
| Lunch scroll | 12:15 PM | 9:45 PM same day |
| Evening (best for reels) | 7:30 PM | 6:00 AM next day |

IST is 9.5 h ahead of New York until 1 Nov 2026, then 10.5 h ahead (US
daylight saving ends). The queue uses New York time, so you never have to think
about this.

**Cadence that works for a new account:** 4–5 reels + 2 carousels a week beats
one post a day. Reels bring new people in, and carousels convert them.

---

## Troubleshooting

Each failed run shows the reason at the top of its log, and GitHub emails you.

| Message contains | Fix |
|---|---|
| `code 190` / token invalid | Token expired or revoked (e.g. you changed your Instagram password). Redo step 3.5, update `IG_ACCESS_TOKEN`. |
| `code 10` or `200` / permission | Re-add the account in step 3.4 and allow content publishing. |
| `Media URL not reachable` | Repo isn't public, or the file isn't committed (check spelling/case). |
| `code 9004` / `2207052` | Instagram couldn't fetch the file. Retry; if it persists, the file is over 20 MB. Re-export smaller. |
| `aspect ratio` / `2207009` | Run the image through `tools/prep_image.py`. |
| `missed its slot` | GitHub was late by 12+ h. Set a new `when`. |
| `gave up after 3 failed attempts` | Fix the cause, then change `when` to retry. |
| Nothing posts at all | Actions tab → is the workflow disabled? GitHub pauses schedules in repos with no commits for 60 days. Click **Enable**. |
| `push` fails in "Save what was posted" | Settings → Actions → General → Workflow permissions → **Read and write**. |

---

## Notes

- **Daily limit:** Instagram allows up to 100 API posts per 24 h. The publisher
  checks the quota before posting.
- **Big videos (>20 MB):** the free CDN won't serve them, so the publisher tries
  Meta's direct-upload route instead (officially for Facebook-Login apps; it
  usually works). Keeping reels under 20 MB is simplest; `make_reel.py` output is
  usually 1–10 MB.
- **Private repo:** set a `MEDIA_BASE_URL` variable pointing to any public file
  host where you mirror `media/` (e.g. a Cloudflare R2 bucket), and set
  `reel_upload: auto` in the queue.
- **Tests:** `python -m unittest discover -s tests` (runs offline against a fake
  Instagram API; also runs on every push).

```
content/queue.yml          ← what to post and when (you edit this)
content/build.yml          ← which Etsy photos become which reels/slides
content/posted.json        ← what went out (the bot edits this)
media/                     ← your images and videos
ig/                        ← publisher (api.py, publisher.py, validate.py)
tools/                     ← build_media.py (Etsy → media), make_reel.py, prep_image.py
examples/                  ← demo mockups and a sample reel
.github/workflows/         ← publish (30 min), build media, refresh token (weekly), validate
```
