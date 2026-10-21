# WEAVER — comic production pipeline

A zero-cost pipeline for producing the *WEAVER* comic ("Mohamed and the
Spider") with AI-generated panels that keep each character's real identity and
the approved WEAVER art style.

* **Image model:** Cloudflare Workers AI, `@cf/black-forest-labs/flux-2-klein-4b`,
  official REST API, up to 4 reference images per panel (resized to ≤512 px).
* **Cost:** Workers **Free** plan only. A hard local cap (9,000 of the 10,000
  free daily neurons) is checked *before* every request. Quota exhaustion stops
  the run and saves progress; re-running after 00:00 UTC resumes.
* **Production:** resumable panel generation, per-panel regeneration, version
  selection, hash-verified locking of approved art, page composition, English
  lettering (balloons, captions, SFX) drawn by code, PDF export.
* **Gate:** only the 3-page pilot can be generated until the author approves it.
* **Privacy:** this repo is public, so photos, artwork and outputs stay in the
  git-ignored `private/` folder. Credentials come from environment variables or
  a git-ignored `.env`, and are never printed.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env            # paste CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN
python3 -m weaver doctor        # expect: Result: READY
python3 -m weaver test-mohamed  # one real identity/style test image
python3 -m weaver plan          # pilot cost estimate, no API calls
python3 -m weaver produce       # generate the pilot panels
python3 -m weaver export-pdf    # lettered pilot pages as a PDF
```

You don't need to type these yourself: ask Claude Code. `CLAUDE.md` maps plain
requests to commands and lists the rules it follows.

See `docs/DECISIONS.md` for the architecture and the reasons behind it.
