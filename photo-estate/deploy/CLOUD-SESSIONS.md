# Working on the app from a phone

The web app can be changed, looked at and merged without a laptop: the work is done by a cloud session
(Claude Code on the web, started from the Claude app on a phone), and the Mac mini shows the result
on the tailnet. This page says how the pieces fit, what a cloud session can and cannot do, and how a
change gets to the real library. The mini's side is in [MAC-MINI.md](MAC-MINI.md).

```
phone: the Claude app  -->  cloud session: a container with a clone of this repo
                                 |  git push (the only way out)
                                 v
                              GitHub
                                 |  git fetch, every 30 s
                                 v
phone: the browser  --tailnet-->  Mac mini :8768, the demo app of the branch with the newest commit
```

## The three apps on the mini

| Port | What it shows | Code it runs | Changes when |
|---|---|---|---|
| 8766 | the real library | `~/src/photo-estate` | someone pulls and restarts it by hand (below) |
| 8767 | the demo library | `~/src/photo-estate-demo`, a worktree | someone moves the worktree by hand |
| 8768 | the demo library, as a preview | a clone the preview keeps for itself | a newer commit is pushed to any branch |

Only 8768 follows GitHub by itself. Nothing a cloud session does can reach 8766 or the photos.

## The cloud environment

Sessions for this repository run in the environment named `photo-estate` (the cloud environment menu
in a session's title bar, then Edit). It holds:

- **Setup script:** runs [`tools/cloud-setup.sh`](../tools/cloud-setup.sh) when a session's machine is
  built: uv, a CPU-only PyTorch, the app's dependencies, the search model and exiftool. Its last
  lines list what is ready and what is MISSING.
- **Network:** Limited, with package registries, plus `download.pytorch.org`,
  `download-r2.pytorch.org`, `huggingface.co` and `us.aws.cdn.hf.co` for that script.
- **Variables and secrets:** none. The preview needs none.

**Start each session with this repository selected.** A session's repositories are chosen when it
starts, and a session started without one has no code and cannot push: it can read this repository,
because it is public, and nothing more.

## A change, from start to merged

1. On the phone, start a session in the `photo-estate` environment with `jflam/photo-estate`
   selected, and say what is wanted.
2. The session works in its own clone, on a new branch. It runs the app on the demo library and the
   tests there, and for a change to the page it looks at its own screenshots.
3. When there is something to look at, it commits and runs
   [`tools/cloud-preview.sh`](../tools/cloud-preview.sh), which pushes the branch and prints
   `PREVIEW PUSHED: <branch> at <commit>`.
4. Within about a minute the mini is showing that commit at
   `https://johns-mac-mini.<tailnet>.ts.net:8768`. Open it on the phone, which must be on the tailnet.
   `~/Library/Logs/photo-preview.log` on the mini names the branch and commit being shown.
5. Ask for changes; each further commit that is pushed replaces the one shown.
6. The session opens a pull request. Merge it from the GitHub app, or ask the session to. The preview
   then shows `main`, because the merge is the newest commit.

The preview does not need the session: it stays up when the session's machine stops, which it does
between turns.

## What a cloud session cannot do

- **Reach the mini, the tailnet or the real library.** It is not on the tailnet and has no SSH. It
  cannot restart an agent, read a log on the mini or see `/Volumes/estate`. It works on the demo
  library only.
- **Be reached.** Its machine takes no connections from outside, and its network carries no tunnel
  out: the proxy passes HTTPS and WebSocket only, and no UDP, which is why a tailcat pipe from the
  session to the mini was tried and given up.
- **Keep anything that is not pushed.** The machine is thrown away after a time; what was pushed is
  what is left.
- **Delete a branch on GitHub.** Use the pull request's "Delete branch" button.
- **Build the iPhone app.** That needs Xcode on a Mac.

## Putting a change on the real library

Merging does not deploy. The real app runs the code in `~/src/photo-estate` on the mini, and changes
only when that checkout is pulled and the agent restarted, on the mini or over SSH from a machine the
tailnet's policy lets in:

```bash
cd ~/src/photo-estate && git pull --ff-only && launchctl kickstart -k gui/$(id -u)/com.jflam.photo-search
```

A cloud session cannot run that, so today a deploy needs the laptop. To deploy from the phone, the
mini would have to fetch the release itself, as the preview does: a script that follows one branch
kept for the purpose, pulls and restarts when it moves, checks that the app answers and goes back to
the commit before if it does not. That is not built.
