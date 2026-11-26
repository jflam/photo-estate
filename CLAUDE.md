# Photo Estate

A family's photo library: a web app and server in `photo-estate/` (Python scripts run with `uv`, inline
dependencies, a plain HTTP server and SQLite), and an iPhone app in `ios/` (needs Xcode on a Mac).

## Working on the web app

- The page is `photo-estate/search.html`; the server is `photo-estate/embed.py`. Each module's docstring
  lists its API.
- Run it on the demo library only, never a real one:
  `cd photo-estate && HF_HUB_OFFLINE=1 uv run embed.py serve --demo --port 8767`
- The script the pages share is in `photo-estate/app/*.js`: native ES modules with JSDoc types (`dom.js`,
  `api.js`, `fsm.js`, `toast.js`, `format.js`; `query.js`, `layout.js` and the state machines `*-flow.js`
  and `favourite.js` for `search.html`), imported by address (`import { esc } from "/app/dom.js"`), no
  bundler. A page's state machines are strict (`flow()` in `fsm.js`: a step not in the table throws) and
  own their data; they live in a module with a unit test, not in the page. A helper two pages need goes there, with a
  unit test in `app/tests/`. A page's script is a module: nothing in it is on `window`, so no
  `onclick="..."` in markup.
- Tests: `uv run pytest` from `photo-estate/` runs them all (`-m "not slow"` is the fast tier CI runs);
  `uv run ruff check .` lints. Each script still runs on its own: `uv run tests/<file>`, every `test_*.py` in
  `photo-estate/tests`. The checked-in demo library (`photo-estate/demo/`) must not change.
- For the script modules, from `photo-estate/app/` with Node 22: `npm ci` once, then `npm run typecheck`
  (`tsc --noEmit --allowJs --checkJs`) and `npm test` (`node --test`). CI runs both.
- For a change to the page, drive the real page with Playwright and look at screenshots in light and
  dark mode.
- State that more than one thing writes (two requests, a request and a background job, the app and a
  script) and that decides what someone is shown or allowed has a TLA+ model in `photo-estate/specs/`,
  checked by TLC (`uv run pytest tests/test_specs.py`; CI runs it). `specs/README.md` is how: model the
  code as it is, run TLC (never call a model checked that was not run), replay each trace on the real
  code as a failing test, fix, then model the fix. A change that adds a writer or a state to a modelled
  machine changes its model in the same pull request.
- `specs/machines.toml` registers every such state, its writers and how it is checked;
  `tests/test_machines.py` fails when a status-like column or a state enum is unregistered, when a new
  module writes a registered table, or when a modelled function changes. Do what its message says: never
  renew a model's `stamp` without reading the model's action for what changed, and never add a new
  machine as `unmodelled`.

## Docs

- `docs/README.md` is the map of every document. The book is `docs/book/` (chapter 2 is the code map,
  appendix B the HTTP API); plans and measurements are in `docs/notes/`.
- A change that adds a file, an endpoint or a feature updates the chapter that describes it, appendix B
  for an endpoint, and chapter 2's file list.

## Deploying to the Mac mini (from the laptop)

**Since 2026-11-24 the real app runs on `home-compute` (a Linux PC), not the mini:** `ssh home-compute`, checkout
`~/src/photo-estate` (it pulls over HTTPS), catalog `~/estate/estate.db`, store `/mnt/estate/store`, restart with
`systemctl --user restart photo-search`, log `~/.local/state/photo-search.log`; the runbook is
`photo-estate/deploy/HOME-COMPUTE.md`. Deploy there, with the same care as below (look first, back up before a
schema change, check `/api/info` and the log). The mini's app is stopped and disabled; its demo (8767) and cloud
preview (8768) still run there. The rest of this section describes the mini and is kept for those two and for going back.

The real app ran on the owner's Mac mini from the checkout `~/src/photo-estate`; the runbook is
`photo-estate/deploy/MAC-MINI.md`. Merging a pull request does not deploy. **When work is finished and
its pull request is open, deploy its branch to the mini without being asked** (and say so), unless the
owner said not to; when the pull request merges, put the mini back on `main`.

- Reach it by its Tailscale address; the short name often does not resolve:
  `ssh -o ConnectTimeout=15 -o HostKeyAlias=johns-mac-mini -o HostName=100.100.132.37 johns-mac-mini`.
  A non-interactive shell there needs `export PATH=/opt/homebrew/bin:$PATH` for `uv`.
- Look first: `git status -sb` in the checkout (it must be clean), the end of
  `~/Library/Logs/photo-search.log`, and whether a background job is still running.
- Get the code: `git fetch origin`, then `git checkout <branch> && git pull --ff-only` (or
  `git checkout main && git pull --ff-only`).
- A change to the catalog's schema, or a step that writes to it, takes a backup first:
  `sqlite3 ~/estate/estate.db ".backup $HOME/photo-estate-lab/estate.db.before-<what>-$(date +%Y%m%d-%H%M)"`,
  then `pragma quick_check` on the copy. The paths for scripts run by hand are
  `DB=~/estate/estate.db STORE=/Volumes/estate/store`.
- A change to a page or `app/*.js` only is live once the checkout moves. Anything in Python needs
  `launchctl kickstart -k gui/$(id -u)/com.jflam.photo-search`. A change to the plist needs
  `bootout`/`bootstrap` (MAC-MINI.md).
- Check it: `curl -s http://127.0.0.1:8766/api/info` on the mini, the main routes answer 200, and no
  traceback in the log since the restart. It is up in about 20 seconds; `/people` takes up to a minute more.
- Leave alone unless asked: the demo worktree `~/src/photo-estate-demo` (port 8767), the cloud preview
  (port 8768, another user), and old files and backups on the mini, which are deleted only on the owner's word.
- The iPhone: `ios/deploy.sh` from the laptop builds the next build number and installs it on the
  phone, which must be unlocked and on the network.

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
