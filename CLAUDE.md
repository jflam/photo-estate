# Photo Estate

A family's photo library: a web app and server in `photo-estate/` (Python scripts run with `uv`, inline
dependencies, a plain HTTP server and SQLite), and an iPhone app in `ios/` (needs Xcode on a Mac).

## Working on the web app

- The page is `photo-estate/search.html`; the server is `photo-estate/embed.py`. Each module's docstring
  lists its API.
- Run it on the demo library only, never a real one:
  `cd photo-estate && HF_HUB_OFFLINE=1 uv run embed.py serve --demo --port 8767`
- Tests are self-running scripts: `uv run tests/<file>` from `photo-estate/`, every file in
  `photo-estate/tests`. The checked-in demo library (`photo-estate/demo/`) must not change.
- For a change to the page, drive the real page with Playwright and look at screenshots in light and
  dark mode.

## In a cloud session (Claude Code on the web)

- If `uv run photo-estate/embed.py --help` is slow or the search model is missing, run
  `bash photo-estate/tools/cloud-setup.sh` once.
- **When the work is ready for a person to try, or they ask to see or try the app, start a preview:**
  `bash photo-estate/tools/cloud-preview.sh`. It serves this checkout's app on the demo library to
  the owner's Mac mini, which shows it on their tailnet. Then:
  - Give them the `Open:` line it prints, and say the preview takes up to a minute to appear and lasts
    only while this session's machine is running.
  - Run it again after changing server code (it restarts the app). A change to `search.html` alone
    shows on reload.
  - If it prints `PREVIEW NOT STARTED`, tell them the reason it gives, and send screenshots instead.
  - Never print or commit the tailcat address or `PHOTO_ESTATE_PREVIEW_KEY`.
- Commit to a new branch, push it, and open a pull request. Do not merge it or touch `main` unless asked.
