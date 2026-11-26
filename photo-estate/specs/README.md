# Models checked by a machine

Most of this code is written by an agent, faster than a person can read it. The tests say that the cases
someone thought of work. This folder is for the other question: is there any order of events, thought of or
not, that leaves the data in a state it must never be in?

A file here is a small model of one piece of state the app keeps, written in TLA+, and a list of rules that
must always hold for it. A program called TLC then visits every state the model can reach, in every order its
steps can happen, and either says no rule is ever broken or prints the shortest sequence of steps that breaks
one. It is not a proof about the Python. It is a search that is complete over the model, and the model is kept
small and close enough to the code that what it finds can be tried on the real thing in minutes.

| Model | What it is | Rules | Where the code is |
|---|---|---|---|
| `PrivatePhoto.tla` | One row of the `private` table: a photo private to one of its sharers, and everything that writes it | A released photo stays released; a photo its sharer said is private stays private until their day; the day is the one they last set | `private.py` |
| `Lens.tla` | One filter lens (published, proposed, an estate default), a member's subscription to it, and an album's series rows, written by its owner, two sysadmins and the member at once | An estate default stays published, and is one its owner proposed and did not take back; a proposal is published and an estate default is not proposed; a sysadmin's item is open exactly while there is something to decide; a write that changes nothing appends nothing; a subscription is as its member last said; one series row per album, as its owner last said | `lenses.py` (and the inbox items it writes) |
| `ChangeSet.tla` | One album or change set (status, op, kind, a search it grows from), its photos' marks and hidden rows, its series row and the two inbox items about it, written by its owner through `changes.py`, by `lenses.py`, by the series listener and by a task's end | A photo is hidden exactly while it is in the set and applied; an album, or a set put away, hides nothing; a set stored as done has every photo as marked and is as it was committed until reopened; one stored as reverted hides nothing; a published series is an album there to look at (hiding or deleting an album takes its series back); new photos wait only for a series (and wait no more once it is hidden or deleted); a review item is about a hiding set | `changes.py`, `listeners.py`'s series listener, `lenses.py`'s publish and subscribe |
| `InboxItem.tla` | The inbox items about one thing (a newcomer asking in, a filter proposed and followed, two questions in one group, held photos, an agent's binding), with every writer of them and the member putting one away | One open item per member, kind and ref; an open question item has a question waiting in it, and a done one has none; an item asking a sysadmin to decide is open only for an active sysadmin, and only while the thing waits; what waits for a sysadmin is before every active sysadmin, one made so later too; a lens_changed item is about a lens its member follows; a binding item is about a pending binding; one open held item, while a held photo waits; a dismiss puts away only an open item | `member_inbox.py`, and its callers in `members.py`, `lenses.py`, `stories.py`, `private.py` |
| `Question.tla` | A question to one member and one to the family, the `asked` rows dealing them, the members' question items within a weekly allowance, and the run of the estate's agent an answer starts (the app able to stop before the listener's cursor moves) | A question dealt to a member that waits on them is in an open item of theirs; "I don't know", to a question or its whole item, puts it away for good, and closes one to them alone; a story told answers it; the status is what its rows say, and moves only as `QUESTION_MOVES` allows; one open item per group, with a question waiting in it; the allowance holds; one run per story | `stories.py`, `member_inbox.dismiss`, `listeners.py`'s stories listener |
| `Member.tla` | A member (role, status, joined_at, a name), a newcomer's login, and a member's own agent's binding on one device, with where each is read to let a request through: the roster the app keeps in memory and reads again after each change, the route's gate on it, the binding read on every call, whois's name for a device, a run's token | The estate keeps an active sysadmin; a change writes only the fields it was given; joined_at is set once a member is let in, and stays; once every change has been read again the roster is what the catalog says, and a request is let through only as the catalog says; a second request to be let in from one login is answered with its member; a binding is what the last decision answered; an agent is never allowed from its own device, however whois named it an agent is not back with its member unasked; a run of the estate's agent acts only for an active member; nobody let in goes back to pending (the last three were questions for the owner, decided in November 2026) | `members.py`, `photo_app.PhotoApp`'s roster and gate, `estate_mcp`, `tasks.py`'s tokens |
| `Reload.tla` | What the app holds in memory from the catalog (the photos, the looks with their private rows, favourites, the generation and what is kept from one, `reload_state`, the ingest's `fresh`, `settling`), and every thread that replaces it while requests are served: an ingest run in its own process, `reload_catalog`'s loop, a relook after a member's commit, a change set taken in by its thread, a favourite | A request sees one catalog from start to finish; nothing is shown that the catalog says is private to someone else, but while their own request takes it in; a private change is in every view before the answer; once nothing is reloaded or looked at again, memory is the catalog; the reload state is idle exactly when no call runs; `fresh` is false only once every run's photos are in; `busy` while and only while a change settles; nothing from one generation is used in a later one (a relook or a reload that raises is tried again by the minute timer, `Mend`: memory may be behind until then, and is known to be) | `photo_app.PhotoApp` (`reload_catalog`, `relook`, `with_private`, `caught_up`, `set_favorites`, `on_hidden_change`), `catalog.Live`, `admin.Admin.forget_library` |
| `ReloadLocks.tla` | Who waits for whom among those threads: the catalog gate (writers first), `fav_write`, SQLite's write lock, taken inside the gate by requests and held in its own process by the ingest | No two ever wait for each other for ever (TLC's deadlock check); a swap is in alone (and, not yet decided: the whole app waits while a request in the gate waits on the ingest's write lock) | `catalog.Gate`, `photo_app.PhotoApp` |
| `People.tla` | Who a face is, as the catalog says and as the People page holds it in memory, and the groups of faces nobody has named (`ClusterState`), with every thread that writes either while requests are served: a load of People beside the one answering (its read, its own names from Google's tags and the automatic ones, its swap), a member naming a face from a People that may be replaced meanwhile, a member naming a group shown on the page, each People's clustering, and what People is built without against the app's looks (a photo made private or released, a reload of the catalog taking that in) | Only one load at a time; a member's word stays in the catalog; the People answering has every member's word the catalog has, and none it does not; a group is named as it was shown, or not at all; a group lists no face the catalog says is named; a face of a photo private to someone is shown to nobody else, but while their own request takes it in; once nothing is under way, People is built without exactly what is private and names each face as the catalog does and once quiet even after a load that failed, since it is tried again | `faces.People`, `faces.default_hidden`, `photo_app.PhotoApp` (`load_people`, `people_page`, `reload_catalog`, `relook`, `hidden_changed`) |

## When a change needs one

Not every change. A model earns its keep where three things are true together:

- **The state is in one of a few named conditions** (a status column, a flag that only makes sense with
  another, a row that exists or does not).
- **More than one thing writes it**: two requests, a request and a background thread, the app and a script in
  another process, or a step that can stop half way.
- **Being wrong matters**: who is shown a photo, what is hidden, what a member is allowed to do.

A change that adds a writer to such a state, or a new state to it, changes its model in the same pull request.
A machine with one writer and a table of legal moves (`core/states.py`'s `TASK_MOVES`, `JOB_MOVES`; `flow()` in
`app/fsm.js`) is already checked where it runs, and needs no model for that alone.

## The register, and what tells you

Nobody has to remember the rule above. `machines.toml` in this folder lists every piece of state in the app
that is in one of a few named conditions, the modules that write it, and how its moves are checked: a model
here, a table of legal moves, not needed (and why), or not modelled yet. `tests/test_machines.py` holds that
file to the code, in CI, and fails with what to do when:

- **You add state**: a column called `status`, `state`, `why`, `mode`, `decision`, `phase` or `stage`, or a
  `StrEnum` in `core/`, that no entry claims. Register it and decide how it is checked. The backlog of
  machines with no model only gets shorter: a new one is modelled, or given its table of moves, in the pull
  request that adds it.
- **You add a writer**: a module with SQL that writes a registered machine's tables and is not listed. If the
  machine has a model, the model gets an action for your writer first.
- **You change modelled code**: each model's entry names the functions it stands for, with a fingerprint of
  them (comments, docstrings and layout do not change it). When one changes, the test says which model to
  read, and prints the new fingerprint to put in `machines.toml` once the model has been looked at. That
  line of the diff is what a reviewer looks for: a fingerprint that moved beside a model that did not is a
  claim that the model still matches.

What it cannot see: state that is not a column or an enum (flags that only make sense together, a string in a
dict), and whether a model's rules are the right ones. A survey with the skill `verify-state-machines` finds
the first; a person decides the second.

## The loop

1. **Find the machine.** Write down its states, what moves it from one to another, who makes each move, and
   which states the code assumes cannot happen. Report that before changing anything.
2. **Model it as the code is.** One action per function that writes the state, named in a comment. Anything
   that reads, then works, then writes is two actions (the read and the write), so that TLC can put other
   steps between them. Add variables the code does not have for what a person *meant* (in `PrivatePhoto.tla`,
   `wish`: the sharer's last word), because the rules are about that.
3. **Write the rules as invariants,** in the words the module's own docstring uses where it makes a promise.
4. **Run TLC.** Never call a model checked that was not run.
5. **For each trace TLC prints, try it on the real code.** A trace is a recipe: do these steps in this order.
   If the real code ends up where the trace says, it is a bug; if not, the model is wrong, and is corrected.
6. **Write that as a test, see it fail, then fix the code.**
7. **Model the fix, and keep the old behaviour too.** The model has a switch (`Guarded`): TRUE is the code
   now, FALSE the code before. `<Name>.cfg` checks the code now and must pass. Each
   `<Name>.before.<invariant>.cfg` checks the code before and must still fail: a rule that can no longer be
   broken by the bug it was written for is checking nothing. One may narrow the search with a `CONSTRAINT`
   so the violation is found in seconds (`Lens.before.LeftNotTold.cfg` does), never so it is found at all.
8. **Say what the model leaves out.** Each model ends with that list. It is where the next bug is.

## Reading a model

`PrivatePhoto.tla` is under 200 lines. The pieces, in the order they appear:

- **`VARIABLES`**: the columns of the row (`exists`, `why`, `released`, `reviewed`, `until`), where each of the
  app's own jobs has got to (`chk`, `lib`, `tmr`), the day it is, and the two that are not in the code
  (`wish`, `wantDay`).
- **`Init`**: how it starts. No row; the member may or may not have shared the photo yet.
- **Actions**, one per function. `/\` is "and", `\/` is "or", and a name with a `'` is the value *after* the
  step. Lines without a `'` say when the step may happen. For example, the sharer releasing a photo:

  ```
  \* private.change(private=False): an open row is released
  Release ==
    /\ Open                                    \* only a photo private to them now
    /\ released' = TRUE                        \* it is released
    /\ wish' = "released" /\ wantDay' = 0      \* and that is their last word on it
    /\ UNCHANGED <<sharer, exists, why, ...>>  \* nothing else moves
  ```

- **`Next`**: any one action may happen next. TLC tries them all, from every state it finds.
- **Invariants**: the rules. `ReleasedStays == wish = "released" => exists /\ released` reads: if the
  sharer's last word was to release it, the row is there and says released.

What TLC printed for that rule before the fix (cut to the columns that move):

```
Error: Invariant ReleasedStays is violated.
State 2: <Ingest>      why = "new"   released = FALSE   chk = "idle"   wish = "none"
State 3: <CheckRead>   why = "new"   released = FALSE   chk = "hold"   wish = "none"
State 4: <Release>     why = "new"   released = TRUE    chk = "hold"   wish = "released"
State 5: <CheckWrite>  why = "held"  released = FALSE   chk = "idle"   wish = "released"
```

Read down: a phone upload arrives; the ingest check reads it and decides to hold it; while it is still
scoring, the sharer releases the photo; the check then writes what it decided, over the release. The test
`test_a_photo_released_while_check_scores_it_is_not_held` in `tests/test_private_states.py` is these four
steps on the real `private.py`.

## Running them

TLC is one jar and needs Java.

```
mkdir -p ~/.local/share/tlaplus
gh release download v1.7.4 --repo tlaplus/tlaplus --pattern tla2tools.jar --dir ~/.local/share/tlaplus
```

```
uv run pytest tests/test_specs.py
```

That runs every `.cfg` here: those that must pass, and those that must fail. It is skipped where there is no
Java or no jar; CI has both and runs it on every pull request. One model by hand, from this folder (on a Mac
with Homebrew's Java, which is not on the PATH, `java` is `/opt/homebrew/opt/openjdk/bin/java`):

```
java -cp ~/.local/share/tlaplus/tla2tools.jar tlc2.TLC -config PrivatePhoto.cfg PrivatePhoto.tla
```

To ask a narrower question, write a `.cfg` of your own. `PrivatePhoto.tla` defines two constraints for that:
`ACTION_CONSTRAINT NoRace` (nothing happens between one of the app's reads and its write) and `CONSTRAINT
OnlyTimer` (the check and the library run never start). An action constraint keeps TLC from going on from a step
that breaks it, but TLC still checks the invariants in the state that step reached, so a violation found with
NoRace may end in one step of bad timing. To ask whether something breaks with no bad timing at all, check the
invariant only where no read waits for its write: `Quiet => <invariant>`, an operator in a module of your own that
EXTENDS the model (`Member.tla` defines `Quiet`), with NoRace as before; what still breaks then needs none.

## What a model does not tell you

- **That the code matches it.** The comments naming each function are the only link, and they are checked by
  a person. A change to a modelled function that does not touch its model is the thing to look for in review.
- **Anything about what it leaves out.** `PrivatePhoto.tla` has one sharer; a photo two members sent is not
  in it.
- **That things eventually happen.** These models check that nothing bad is ever reached, not that something
  good always is (a question that is never closed would not show).
- **Sizes beyond the ones given.** Three days, one photo, one member. That is enough for every bug found so
  far, each of which needs two or three steps; it is a choice, not a guarantee.

## Machines with no model yet

None is left from the survey of November 2026. It found nine with no model: change sets and their photos, lenses,
subscriptions, members, agent bindings, inbox items, questions, the catalog reload, and the clustering of faces; each
was modelled in November 2026 (`ChangeSet.tla`; `Lens.tla` for lenses and subscriptions; `Member.tla` for members and
bindings; `InboxItem.tla`; `Question.tla`; `Reload.tla`, with everything else the app holds in memory from the
catalog, and its locks, `ReloadLocks.tla`; and `People.tla`, which found the clustering to be the smaller part of
who each face is, in the catalog and in memory). `tests/test_machines.py`'s backlog is empty, and stays so: a new
machine is modelled, or given its table of moves, in the pull request that adds it.

What the survey could not see, and so what may still have no model: state that is not a column or an enum (flags
that only make sense together, a string in a dict, a number kept in memory, such as People's `writes` and its
groups' numbers, which no register test finds); state kept only in memory beside the catalog, which the survey
listed by reading, not by search (`Reload.tla` and `People.tla` were both found that way, and the older copy put over
a newer one is the bug both found); writers whose SQL the register's search does not match (`estate.py purge`
deletes with table names it builds: People's entry says so in a comment); the scripts run by hand while the app serves, which tell
it nothing; and whether a model's rules are the right ones, which is a person's to decide.
