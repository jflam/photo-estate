# Photo Estate

A family's photo library: a web app and server in `photo-estate/` (Python scripts run with `uv`, inline
dependencies, a plain HTTP server and SQLite), and an iPhone app in `ios/` (needs Xcode on a Mac).

## Working on the web app

- The page is `photo-estate/search.html`; the server is `photo-estate/embed.py`. Each module's docstring
  lists its API.
- Run it on the demo library only, never a real one:
  `cd photo-estate && HF_HUB_OFFLINE=1 uv run embed.py serve --demo --port 8767`
- The script the pages share is in `photo-estate/app/*.js`: native ES modules with JSDoc types (`dom.js`,
  `api.js`, `fsm.js`, `toast.js`, `format.js`; `query.js` and `layout.js` for `search.html`), imported by
  address (`import { esc } from "/app/dom.js"`), no bundler. A helper two pages need goes there, with a
  unit test in `app/tests/`. A page's script is a module: nothing in it is on `window`, so no
  `onclick="..."` in markup.
- Tests: `uv run pytest` from `photo-estate/` runs them all (`-m "not slow"` is the fast tier CI runs);
  `uv run ruff check .` lints. Each script still runs on its own: `uv run tests/<file>`, every `test_*.py` in
  `photo-estate/tests`. The checked-in demo library (`photo-estate/demo/`) must not change.
- For the script modules, from `photo-estate/app/` with Node 22: `npm ci` once, then `npm run typecheck`
  (`tsc --noEmit --allowJs --checkJs`) and `npm test` (`node --test`). CI runs both.
- For a change to the page, drive the real page with Playwright and look at screenshots in light and
  dark mode.

## Docs

- `docs/README.md` is the map of every document. The book is `docs/book/` (chapter 2 is the code map,
  appendix B the HTTP API); plans and measurements are in `docs/notes/`.
- A change that adds a file, an endpoint or a feature updates the chapter that describes it, appendix B
  for an endpoint, and chapter 2's file list.

## In a cloud session (Claude Code on the web)

- `photo-estate/deploy/CLOUD-SESSIONS.md` says what a cloud session can and cannot do: it cannot reach
  the Mac mini or the real library, and merging a pull request does not deploy.
- If `uv run photo-estate/embed.py --help` is slow or the search model is missing, run
  `bash photo-estate/tools/cloud-setup.sh` once.
- **When the work is ready for a person to try, or they ask to see or try the app, commit it to a
  branch and run** `bash photo-estate/tools/cloud-preview.sh`. It pushes the branch; the owner's Mac
  mini serves the demo app of the branch with the newest commit on its tailnet. Then:
  - Give them the `PREVIEW PUSHED` and `Open:` lines it prints, and say the preview takes about a
    minute to appear. It stays up after this session ends.
  - Run it again after each further commit they should see.
  - If it prints `PREVIEW NOT PUSHED`, tell them the reason it gives, and send screenshots instead.
- Commit to a new branch, push it, and open a pull request. Do not merge it or touch `main` unless asked.
