# WEAVER comic pipeline — operating guide for Claude Code

The author runs this project by talking to Claude Code; they do not write code.
Every operation is a `python3 -m weaver <command>` call. Run it, read its output,
and report back faithfully.

## Hard rules (never break these)

1. **Secrets.** Never print, echo, log, commit or paste `CLOUDFLARE_API_TOKEN`.
   Never `cat .env`. `doctor` reports only *whether* credentials are set.
2. **Private material.** This repository is **public**. Real photos, approved
   artwork, generated panels, pages, PDFs and production state live in
   `private/` (git-ignored). Never `git add -f` anything under `private/`, and
   never move such files elsewhere in the repo.
3. **Free tier only.** Never raise `quota.daily_neuron_cap` above
   `quota.free_allocation` (the code refuses to load such a config). Never
   upgrade a Cloudflare plan, add billing, or create extra accounts to get
   around a limit. When the quota stops a run, stop and tell the author when it
   resets (00:00 UTC).
4. **Pilot gate.** Generate only the pilot pages (`pilot_pages` in the script)
   until the author explicitly approves the 3-page pilot. Run
   `approve-pilot --confirm` only after the author says so in their own words.
5. **Locked art is final.** Never `unlock` a panel unless the author asks.
6. **No fabrication.** Only say an image exists if the command printed
   "generated and verified" or "generated <panel> vN". Show the author the real
   file (send it with SendUserFile, or Read it). Never draw placeholders or
   substitute other images for missing artwork.
7. **No local GPU inference** (author's NVIDIA MX130) unless the author
   explicitly asks for a local benchmark.

## Credentials

Needed: `CLOUDFLARE_ACCOUNT_ID` (32 hex characters) and `CLOUDFLARE_API_TOKEN`
(Cloudflare dashboard → My Profile → API Tokens → Create Token → **Workers AI**
template; optionally add *Account → Account Analytics → Read* so `doctor` can
show Cloudflare's own usage figure).

* Claude Code on the web: add both as environment variables/secrets in the
  cloud environment settings, then start a session.
* Claude Code on a computer: copy `.env.example` to `.env` and paste the two
  values, or run `python3 -m weaver setup` in a real terminal (hidden input).

Then run `python3 -m weaver doctor` and expect `Result: READY`.

## Start of every session (cloud containers start empty)

`private/` is not in git, so a fresh cloud session has no images. If
`python3 -m weaver refs list` shows MISSING sources and the author attached
images, look at each attachment, identify it, and import it:

| Attachment | Command |
|---|---|
| Chapter One cover ("Mohamed and the Spider") | `refs import <file> --as cover_chapter_one` |
| WEAVER suit sheet (front/back, Sky Stone, First Gear) | `refs import <file> --as weaver_suit_sheet` |
| Friends lineup (7 portraits) | `refs import <file> --as friends_lineup` |
| Support network (Amm Ramadan … Bondo') | `refs import <file> --as support_network` |
| Villains and monsters lineup | `refs import <file> --as villains_lineup` |
| Real photo labelled with a name | `refs import <file> --as <character id>` |

Character ids: mohamed, ali, omar, aliyy, abdulrahman, adham, ahmed, jana.
Then run `refs preview` and look at the sheet before generating anything.

## Reference photos

Real photos go in `private/references/photos/<character>.<ext>` (for example
`mohamed.jpg`). When the author attaches photos in a chat message, they are
saved under the session's `images/` folder; import each one:

```
python3 -m weaver refs import <path-to-photo> --as mohamed
python3 -m weaver refs preview --ids mohamed_photo     # then LOOK at the PNG
```

If a crop cuts the face badly, adjust that photo's `box_rel` in
`story/references.yaml`. Photos win over artwork for identity automatically.
Approved artwork goes in `private/references/source/` with the file names used
in `story/references.yaml`.

## What the author might say → what to run

| Author says | Command |
|---|---|
| "check everything" / "is it working?" | `python3 -m weaver doctor` |
| "make the Mohamed test image" | `python3 -m weaver test-mohamed --model @cf/black-forest-labs/flux-2-klein-9b` (then show the file) |
| "test <name>" | `python3 -m weaver test-character <id> --model @cf/black-forest-labs/flux-2-klein-9b` (every friend has a canon scene; `--scene "..."` overrides) |
| "what will it cost?" / "show the plan" | `python3 -m weaver plan [--pages 1-3] [--show-prompts]` |
| "make the pilot" / "continue" | `python3 -m weaver produce` (pilot pages by default) |
| "redo panel 2-3" | `python3 -m weaver regen p2-3 [--seed N]` |
| "use the first version of p2-3" | `python3 -m weaver select p2-3 1` |
| "I approve panel p1-1" / "lock page 1" | `python3 -m weaver lock p1-1` / `lock --page 1` |
| "where are we?" | `python3 -m weaver status` |
| "build the pages" | `python3 -m weaver compose [--pages 1-3]` |
| "give me the PDF" | `python3 -m weaver export-pdf [--pages 1-3]` |
| "I approve the pilot" | lock all pilot panels, then `python3 -m weaver approve-pilot --confirm` |
| "why does p1-2 look like that?" | `python3 -m weaver show p1-2` |

Exit codes: 0 ok · 1 failure · 2 needs input/confirmation · 3 stopped by quota
or budget (progress saved) · 4 credentials problem.

## Editing the story

* Scenes, layout and lettering: `story/chapter-01/script.yaml`. Changing only
  `lettering` needs **no** regeneration; just `compose` again.
* Changing `scene`, `shot` or `characters` needs `regen <panel>`.
* Character looks/outfits: `story/characters.yaml`. House style: `story/style.yaml`.
* The current pilot script is a DRAFT written from the cover's taglines. Do not
  treat it as canon. Replace it with the author's story when they provide it.

## Where things are

* Generated panels: `private/workspace/panels/<panel>/vN.png` (+ `.prompt.txt`)
* Test images: `private/workspace/tests/`
* Pages / PDF: `private/workspace/pages/`, `private/workspace/export/`
* State: `private/workspace/state/production.json`, usage ledger `usage.json`
* Audit log of every API call: `private/workspace/logs/generation.jsonl`

Run the tests after changing code: `python3 -m unittest discover -s tests -t .`
