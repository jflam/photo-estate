# Photo Estate's documents

Every document in the repository, by what you want to do. The [README](../README.md) is the
introduction; everything else is here.

## Use it

| Document | What it covers |
|---|---|
| [README](../README.md) | What it is, the two-minute demo, bringing your photos in, where it runs |
| [guides/DISK-SETUP.md](guides/DISK-SETUP.md) | Choosing and preparing storage, the overnight pull, checking the archive |
| [guides/COMPARE-A-MODEL.md](guides/COMPARE-A-MODEL.md) | Putting a new image model through `/compare`: export, embed, run, read the report |
| [../photo-estate/deploy/MAC-MINI.md](../photo-estate/deploy/MAC-MINI.md) | Running it all the time on a Mac mini, reaching it over Tailscale, the cloud preview's side |

## Understand it

| Document | What it covers |
|---|---|
| [The book](book/README.md) ([as a website](https://jflam.github.io/photo-estate/)) | How every piece works, 27 chapters with diagrams |
| [book/02-code-tour.md](book/02-code-tour.md) | The map of the code: every file, with line counts |
| [book/appendix-a-commands.md](book/appendix-a-commands.md) | Every command and option |
| [book/appendix-b-http-api.md](book/appendix-b-http-api.md) | Every HTTP endpoint, including `/mcp`, who may call it, and its status codes |
| [book/appendix-c-glossary.md](book/appendix-c-glossary.md) | The words the code and the book use |

Each Python module's docstring also lists its commands or its API, and each Swift file starts with what
it is for.

## Work on it

| Document | What it covers |
|---|---|
| [../CLAUDE.md](../CLAUDE.md) | What Claude Code is told at the start of every session: how to run the app and the tests |
| [book/27-built-with-claude.md](book/27-built-with-claude.md) | How the project is built with Claude Code, from a laptop and from a phone |
| [../photo-estate/deploy/CLOUD-SESSIONS.md](../photo-estate/deploy/CLOUD-SESSIONS.md) | Working on the app from a phone: cloud sessions, the preview, what they cannot do |
| [book/17-testing-and-demo.md](book/17-testing-and-demo.md) | The tests, the type checks (pyright; TypeScript for the pages' script modules) and the demo library |
| [GitHub issues](https://github.com/jflam/photo-estate/issues) | The backlog (`backlog`) and the bugs (`bug`) |

## Plans, research and measurements

Working notes, kept as they were written; the book is the current description.

| Document | What it covers |
|---|---|
| [notes/PLAN.md](notes/PLAN.md) | The original plan: why Takeout, rolling ingest, the tagging tiers |
| [notes/INTAKE-PLAN.md](notes/INTAKE-PLAN.md) | Bringing photos in from the app, by phases |
| [notes/TAGGING-RESEARCH.md](notes/TAGGING-RESEARCH.md) | The tiered tagging pipeline and the model comparison |
| [notes/PERFORMANCE.md](notes/PERFORMANCE.md) | Where the app's time and memory go, measured, and the open performance tasks |
| [notes/BENCHMARK.md](notes/BENCHMARK.md) | Library statistics and the benchmark album used to compare models |
| [notes/VIDEO-PLAN.md](notes/VIDEO-PLAN.md) | Videos and Live Photos: what the library holds, how clips are told, best frame against mean, which frames to keep, what is left |
| [notes/MULTIPLAYER-PLAN.md](notes/MULTIPLAYER-PLAN.md) | The family as users: members from the tailnet, lenses, private photos, the phone gate, append-only, the journal, the inbox, agents as members (phase 1, members and identity, is built) |

## Where documents live

```
README.md                    the introduction
CLAUDE.md                    instructions for Claude Code
docs/
  README.md                  this page
  book/                      the book (published by .github/workflows/book.yml)
  guides/                    how-tos for running it
  notes/                     plans, research and measurements
  screenshots/               the pictures in the README and the book
photo-estate/deploy/*.md     runbooks for the Mac mini, next to the scripts they describe
photo-estate/demo/CREDITS.md the demo photos' authors and sources
```

Keep the line counts in [chapter 2](book/02-code-tour.md) current when the code changes a lot: they
are measured with `tokei`, leaving out the demo library and Markdown.
