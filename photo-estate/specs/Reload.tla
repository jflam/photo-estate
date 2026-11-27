-------------------------------- MODULE Reload --------------------------------
(* What the running app holds in memory that is worked out from the catalog, and how it is replaced while requests
   are being served (catalog.Live, and photo_app.PhotoApp around it):
     - the catalog itself (Live.cat: the photos), replaced by PhotoApp.reload_catalog: load_catalog, read outside the
       gate while the old one keeps answering, then Live.install, which swaps it in with no request reading
       (Gate.swap), and in the same swap reads the private rows again (PhotoApp.with_private) over the looks as they
       are, and lets go of what was worked out from the old one (PhotoApp.caught_up: Browse.forget,
       Admin.forget_library);
     - the looks (Live.looks, a lenses.State: what members hide, their favourites, the private rows), replaced by
       PhotoApp.relook: lenses.read (read_looks), then Live.relook in a swap, both under fav_write ("not while a
       favourite is being shown"), with Browse.forget and Admin.forget_library (caught_up) in the same swap;
     - a favourite (PhotoApp.set_favorites): under fav_write, the table, then Live.update in a swap, which changes
       the looks and the Views in place;
     - Live.generation, which every swap moves on, and what is kept from one: Browse's cache (keyed by its
       generation: never used in a later one) and Admin's library numbers (Admin.st["lib"], which is not keyed:
       it is let go by forget_library, and /api/stats answers from it when it is there);
     - reload_state (idle | running | rerun, under reload_lock), its error, and upload.IngestJob.fresh, which
       reload_catalog clears when its catalog is in (the /stats page's "restart needed");
     - `settling`, under settling_lock, which on_hidden_change counts up before it starts the thread that runs
       hidden_changed, and hidden_changed counts down when it is done: Changes' `busy`, with reload_state.
   The views (Live.views, Live.shares) are let go in every swap and worked out again from cat and looks, so they are
   what those are and are not variables here.

   The catalog is a few versions: X, Ben's phone upload, which the ingest script adds with its `new` row in one
   transaction (private.record_sharer) and `private.py check` then holds or lets go, both in a process of their own
   that tells the app nothing but, at catalog_ready, to reload (IngestJob.on_added); W, a second run's photo, shared
   at once; Y, a photo Ben shares, which he makes private (POST /api/private: private.change, then relook, then
   on_hidden_change, then the answer); what members hide, which Ada changes (a change set applied: Changes.write
   commits, then Changes.on_change is on_hidden_change, whose thread relooks); and Cleo's favourite. A request is in
   the gate or not; it may ask /api/stats.

   Guarded = TRUE is the code. Guarded = FALSE is the code before it was fixed (November 2026), kept so the invariants
   can be seen to fail without the guards:
     - reload_catalog installs under fav_write, as a relook does from its read to its install: a relook that read the
       looks before an ingest's commit could otherwise install them after the reload, private rows older than the
       catalog's photos, and the new photo was shown to everyone;
     - a relook lets go of Browse's cache and Admin's library numbers inside its swap (Live.relook's `then`), not after
       it, so no request is answered from numbers of the looks before;
     - a run of reload_catalog that fails before its catalog is in sets IngestJob.fresh again: a call clears it after
       its loop if reload_state's error is not set, and that is the last run's of any call (one done meanwhile could
       clear it as another began, which then failed);
     - on_hidden_change counts `settling` down again when the thread cannot be started.
   Fails = TRUE lets a reload, a relook, or starting a thread raise.

   Reload.cfg checks the code; Reload.before.<invariant>.cfg each find the violation there was. tests/test_specs.py
   runs them all with TLC, and tests/test_reload_states.py replays each on the real app. specs/ReloadLocks.tla is the
   locks of the same threads, for whether any two ever wait for each other for ever. Two invariants at the end are
   broken by the code as it is and are not in Reload.cfg: each is a question for the owner. *)
EXTENDS Integers

CONSTANTS Guarded, Fails

I == {1, 2}                       \* the calls of reload_catalog: one by each ingest run (IngestJob.on_added)
A == {"ben", "ada", "cleo"}       \* Ben's POST /api/private; Ada's change set applied; Cleo's favourite
Relooks == {"ben", "ada"}         \* the two of them that relook (PhotoApp.relook)

VARIABLES
  dbN,       \* the photos ingest runs have added to the catalog: 0; 1 (X); 2 (X and W)
  dbX,       \* X has its open `private` row, private to Ben (new, then held); FALSE before it is in, or let go
  dbY,       \* Y is private to Ben (withdrawn)
  dbL,       \* what members hide (lens and hidden rows), a version: 0, 1
  dbF,       \* the favourites (member_favorites), a version: 0, 1
  memN, memX, memY, memL, memF,   \* the same, as the app holds them: Live.cat (memN), Live.looks (the rest)
  gen,       \* Live.generation
  lib,       \* Admin.st["lib"]: the generation it was worked out in, or -1 (None)
  ig,        \* the ingest script and IngestJob: "add1" "check" "ready1" "ask1" "add2" "ready2" "ask2" "done"
  fresh,     \* IngestJob.fresh
  rs,        \* reload_state["state"]: "idle" | "running" | "rerun"
  err,       \* reload_state["error"] is set
  inv,       \* each call of reload_catalog: "none" | "load" | "install" | "next" | "after"
  got,       \* what the run under way read: photos (load_catalog)
  pc,        \* where each of Ben, Ada and Cleo has got to
  fw,        \* who holds fav_write: "none", or one of A
  rd,        \* what the relook holding fav_write read (read_looks)
  settling,  \* PhotoApp.settling
  hc,        \* Ben's hidden_changed thread: "none" | "run" | "done"
  q,         \* a request: "out", or "in" the gate
  seen,      \* what it found in memory when it came in
  \* not in the code:
  stale,     \* a request was answered from something worked out in an earlier generation
  failed     \* a reload or a relook raised

vars == <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, fresh, rs, err, inv, got, pc, fw,
          rd, settling, hc, q, seen, stale, failed>>
db == <<dbN, dbX, dbY, dbL, dbF>>
mem == <<memN, memX, memY, memL, memF, gen>>
reload == <<fresh, rs, err, inv, got>>

Init ==
  /\ dbN = 0 /\ dbX = FALSE /\ dbY = FALSE /\ dbL = 0 /\ dbF = 0
  /\ memN = 0 /\ memX = FALSE /\ memY = FALSE /\ memL = 0 /\ memF = 0 /\ gen = 0 /\ lib = -1
  /\ ig = "add1" /\ fresh = FALSE /\ rs = "idle" /\ err = FALSE /\ inv = [i \in I |-> "none"] /\ got = 0
  /\ pc = [a \in A |-> "idle"] /\ fw = "none" /\ rd = [x |-> FALSE, y |-> FALSE, l |-> 0, f |-> 0]
  /\ settling = 0 /\ hc = "none" /\ q = "out" /\ seen = mem /\ stale = FALSE /\ failed = FALSE

--------------------------------------------------------------------------
(* The ingest script (ingest-iphone.sh, a process of its own) and upload.IngestJob._follow *)

\* estate.py ingest: X, with its `new` row (private.record_sharer), in one transaction
IngestAdd1 ==
  /\ ig = "add1" /\ ig' = "check"
  /\ dbN' = 1 /\ dbX' = TRUE
  /\ UNCHANGED <<dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, fresh, rs, err, inv, got, pc, fw, rd,
                 settling, hc, q, seen, stale, failed>>

\* private.py check, before catalog_ready: held (the row stays, why held) or let go (no row)
IngestCheck ==
  /\ ig = "check" /\ ig' = "ready1"
  /\ dbX' \in BOOLEAN
  /\ UNCHANGED <<dbN, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, fresh, rs, err, inv, got, pc, fw,
                 rd, settling, hc, q, seen, stale, failed>>

\* a second run: W, shared at once
IngestAdd2 ==
  /\ ig = "add2" /\ ig' = "ready2"
  /\ dbN' = 2
  /\ UNCHANGED <<dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, fresh, rs, err, inv, got, pc, fw,
                 rd, settling, hc, q, seen, stale, failed>>

\* catalog_ready: IngestJob._follow sets fresh, then starts the thread that calls on_added
Ready(k) ==
  /\ ig = IF k = 1 THEN "ready1" ELSE "ready2"
  /\ ig' = IF k = 1 THEN "ask1" ELSE "ask2"
  /\ fresh' = TRUE
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, rs, err, inv, got, pc, fw, rd,
                 settling, hc, q, seen, stale, failed>>

\* on_added is reload_catalog, in that thread: under reload_lock, it runs (from idle) or asks the run under way for
\* another (rerun)
Ask(k) ==
  /\ ig = IF k = 1 THEN "ask1" ELSE "ask2"
  /\ ig' = IF k = 1 THEN "add2" ELSE "done"
  /\ IF rs = "idle"
       THEN rs' = "running" /\ inv' = [inv EXCEPT ![k] = "load"]
       ELSE rs' = "rerun" /\ UNCHANGED inv
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, fresh, err, got, pc, fw, rd,
                 settling, hc, q, seen, stale, failed>>

--------------------------------------------------------------------------
(* PhotoApp.reload_catalog: its loop, a run at a time *)

\* load_catalog(self.live.cat.raw), outside the gate, the old catalog answering meanwhile
ReloadRead(i) ==
  /\ inv[i] = "load" /\ inv' = [inv EXCEPT ![i] = "install"]
  /\ got' = dbN
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, fresh, rs, err, pc, fw, rd,
                 settling, hc, q, seen, stale, failed>>

\* Live.install(fresh, then=caught_up, looks=with_private): in one swap, with no request reading, the new catalog,
\* the looks as they are with the private rows read again, the caches let go; then loaded_through and the error
\* cleared. Guarded: under fav_write, which no relook or favourite then holds. (If the private rows it read changed what
\* People is built without, a thread is started that loads People: none of this model's variables, specs/People.tla.)
ReloadInstall(i) ==
  /\ inv[i] = "install" /\ q = "out"
  /\ Guarded => fw = "none"
  /\ memN' = got /\ memX' = dbX /\ memY' = dbY
  /\ gen' = gen + 1 /\ lib' = -1
  /\ err' = FALSE
  /\ inv' = [inv EXCEPT ![i] = "next"]
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memL, memF, ig, fresh, rs, got, pc, fw, rd, settling, hc, q, seen, stale,
                 failed>>

\* the run raises (a locked or broken catalog, out of memory): the old catalog keeps answering, and the error is set.
\* Guarded: fresh is set again (what the ingest catalogued is not in)
ReloadFail(i) ==
  /\ Fails /\ inv[i] \in {"load", "install"}
  /\ err' = TRUE /\ failed' = TRUE
  /\ fresh' = IF Guarded THEN TRUE ELSE fresh
  /\ inv' = [inv EXCEPT ![i] = "next"]
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, rs, got, pc, fw, rd, settling,
                 hc, q, seen, stale>>

\* under reload_lock: asked again meanwhile (rerun), it runs again; else idle
ReloadNext(i) ==
  /\ inv[i] = "next"
  /\ IF rs = "rerun"
       THEN rs' = "running" /\ inv' = [inv EXCEPT ![i] = "load"]
       ELSE rs' = "idle" /\ inv' = [inv EXCEPT ![i] = "after"]
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, fresh, err, got, pc, fw, rd,
                 settling, hc, q, seen, stale, failed>>

\* after the loop, outside the lock: `if self.ingest and not rs["error"]: self.ingest.fresh = False` (the error is the
\* last run's of any call: another may have started, or failed, since this one went idle)
ReloadAfter(i) ==
  /\ inv[i] = "after" /\ inv' = [inv EXCEPT ![i] = "none"]
  /\ fresh' = IF err THEN fresh ELSE FALSE
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, rs, err, got, pc, fw, rd,
                 settling, hc, q, seen, stale, failed>>

\* PhotoApp.mend, on the minute timer (Guarded: the owner's word on issues #138 and #139): a relook that raised, or
\* a thread that could not be started, set looks_stale, and a reload that raised set reload_failed; mend reads the
\* looks again (on_hidden_change: relook, the feeds, People) and reloads the catalog (reload_catalog, when no reload
\* is running). Here both in one step, once nobody holds fav_write and no request reads: a relook holds fav_write from
\* its read to its install and a reload's install takes it, so no other relook, favourite or install comes between
\* (the races of a read against a commit are RelookRead's and ReloadRead's, above, and a commit's own relook or the
\* ingest's own reload follows it as before). It may raise again (Fails): then nothing changes, and the next minute
\* tries again, which is this action not taken. PhotoApp.notice (the listeners' pass) calls the same two for what a
\* script run by hand wrote: more callers of the actions above, and no new one.
Mend ==
  /\ Guarded /\ failed /\ fw = "none" /\ q = "out" /\ rs = "idle" /\ \A i \in I : inv[i] = "none"
  /\ \A a \in Relooks : pc[a] \notin {"relook", "install", "forget", "spawn"}
  /\ memN' = dbN /\ memX' = dbX /\ memY' = dbY /\ memL' = dbL /\ memF' = dbF
  /\ gen' = gen + 1 /\ lib' = -1
  /\ err' = FALSE /\ fresh' = FALSE /\ failed' = FALSE
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, ig, rs, inv, got, pc, fw, rd, settling, hc, q, seen, stale>>

--------------------------------------------------------------------------
(* Ben, Ada and Cleo *)

\* PhotoApp.private_write: private.change commits (Y withdrawn); then this request's relook
BenCommit ==
  /\ pc["ben"] = "idle" /\ pc' = [pc EXCEPT !["ben"] = "relook"]
  /\ dbY' = TRUE
  /\ UNCHANGED <<dbN, dbX, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, reload, fw, rd, settling, hc, q,
                 seen, stale, failed>>

\* Changes.write (apply) commits the hidden rows, and returns; Changes.on_change is on_hidden_change(None): settling
\* counted up, and the thread that runs hidden_changed, which relooks first
AdaCommit ==
  /\ pc["ada"] = "idle" /\ pc' = [pc EXCEPT !["ada"] = "spawn"]
  /\ dbL' = 1
  /\ UNCHANGED <<dbN, dbX, dbY, dbF, memN, memX, memY, memL, memF, gen, lib, ig, reload, fw, rd, settling, hc, q,
                 seen, stale, failed>>

\* PhotoApp.relook: under fav_write, PhotoApp.read_looks (lenses.read: every lens, hidden, favourite and private row)
RelookRead(a) ==
  /\ pc[a] = "relook" /\ fw = "none"
  /\ fw' = a /\ rd' = [x |-> dbX, y |-> dbY, l |-> dbL, f |-> dbF]
  /\ pc' = [pc EXCEPT ![a] = "install"]
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, reload, settling, hc, q, seen,
                 stale, failed>>

\* the relook raises (lenses.read: a locked catalog, a bug) and lets fav_write go: Ben's request is answered an error,
\* with no on_hidden_change; hidden_changed's `finally` counts Ada's thread down
RelookFail(a) ==
  /\ Fails /\ pc[a] = "relook"
  /\ pc' = [pc EXCEPT ![a] = "failed"]
  /\ settling' = IF a = "ada" THEN settling - 1 ELSE settling
  /\ failed' = TRUE
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, reload, fw, rd, hc, q, seen,
                 stale>>

\* Live.relook(looks): in a swap, the looks it read, every view let go (Live.forget_views). Guarded: then=caught_up,
\* so Browse's cache and Admin's numbers are let go in the swap too; and fav_write let go
RelookInstall(a) ==
  /\ pc[a] = "install" /\ fw = a /\ q = "out"
  /\ memX' = rd.x /\ memY' = rd.y /\ memL' = rd.l /\ memF' = rd.f
  /\ gen' = gen + 1
  /\ lib' = IF Guarded THEN -1 ELSE lib
  /\ fw' = IF Guarded THEN "none" ELSE fw
  /\ pc' = [pc EXCEPT ![a] = IF Guarded THEN (IF a = "ben" THEN "spawn" ELSE "feed") ELSE "forget"]
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, ig, reload, rd, settling, hc, q, seen, stale, failed>>

\* before the fix, after the swap: self.browse.forget(); self.admin.forget_library(); then fav_write let go
RelookForget(a) ==
  /\ pc[a] = "forget"
  /\ lib' = -1 /\ fw' = "none"
  /\ pc' = [pc EXCEPT ![a] = IF a = "ben" THEN "spawn" ELSE "feed"]
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, ig, reload, rd, settling, hc, q, seen,
                 stale, failed>>

\* PhotoApp.on_hidden_change: settling counted up, and a thread started: for Ben, hidden_changed(people) after his
\* relook (then the answer); for Ada, hidden_changed(None), which relooks
Spawn(a) ==
  /\ a \in Relooks /\ pc[a] = "spawn"
  /\ settling' = settling + 1
  /\ IF a = "ben" THEN pc' = [pc EXCEPT !["ben"] = "answered"] /\ hc' = "run"
                  ELSE pc' = [pc EXCEPT !["ada"] = "relook"] /\ UNCHANGED hc
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, reload, fw, rd, q, seen, stale,
                 failed>>

\* the thread cannot be started (Thread.start raises: no more threads): the request is answered an error. Guarded:
\* settling counted down again
SpawnFail(a) ==
  /\ Fails /\ a \in Relooks /\ pc[a] = "spawn"
  /\ settling' = IF Guarded THEN settling ELSE settling + 1
  /\ pc' = [pc EXCEPT ![a] = "failed"]
  /\ failed' = TRUE
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, reload, fw, rd, hc, q, seen,
                 stale>>

\* PhotoApp.hidden_changed's end: feed_update (in the gate), load_people, then (finally) settling counted down
BenSettled ==
  /\ hc = "run" /\ hc' = "done"
  /\ settling' = settling - 1
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, reload, pc, fw, rd, q, seen,
                 stale, failed>>

AdaSettled ==
  /\ pc["ada"] = "feed" /\ pc' = [pc EXCEPT !["ada"] = "done"]
  /\ settling' = settling - 1
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, reload, fw, rd, hc, q, seen,
                 stale, failed>>

\* PhotoApp.set_favorites: under fav_write, lenses.set_favorites and the journal, committed
CleoCommit ==
  /\ pc["cleo"] = "idle" /\ fw = "none"
  /\ fw' = "cleo" /\ dbF' = 1
  /\ pc' = [pc EXCEPT !["cleo"] = "show"]
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, memN, memX, memY, memL, memF, gen, lib, ig, reload, rd, settling, hc, q, seen,
                 stale, failed>>

\* and Live.update(show): in a swap, the looks and the views told (in place), Browse and Admin let go; fav_write let go
CleoShow ==
  /\ pc["cleo"] = "show" /\ q = "out"
  /\ memF' = 1 /\ gen' = gen + 1 /\ lib' = -1 /\ fw' = "none"
  /\ pc' = [pc EXCEPT !["cleo"] = "done"]
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, ig, reload, rd, settling, hc, q, seen, stale, failed>>

--------------------------------------------------------------------------
(* A request (PhotoApp.handle: `with self.live.gate`) *)

Enter ==
  /\ q = "out" /\ q' = "in" /\ seen' = mem
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, reload, pc, fw, rd, settling,
                 hc, stale, failed>>

\* GET /api/stats (Admin.stats_payload, under st_lock): the library numbers kept, or worked out now and kept
Stats ==
  /\ q = "in"
  /\ IF lib = -1 THEN lib' = gen /\ UNCHANGED stale
                 ELSE stale' = (stale \/ lib # gen) /\ UNCHANGED lib
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, ig, reload, pc, fw, rd, settling, hc, q,
                 seen, failed>>

Leave ==
  /\ q = "in" /\ q' = "out"
  /\ UNCHANGED <<dbN, dbX, dbY, dbL, dbF, memN, memX, memY, memL, memF, gen, lib, ig, reload, pc, fw, rd, settling,
                 hc, seen, stale, failed>>

Next ==
  \/ IngestAdd1 \/ IngestCheck \/ IngestAdd2 \/ \E k \in I : Ready(k) \/ Ask(k)
  \/ \E i \in I : ReloadRead(i) \/ ReloadInstall(i) \/ ReloadFail(i) \/ ReloadNext(i) \/ ReloadAfter(i)
  \/ BenCommit \/ AdaCommit \/ CleoCommit \/ CleoShow \/ BenSettled \/ AdaSettled
  \/ \E a \in Relooks : RelookRead(a) \/ RelookFail(a) \/ RelookInstall(a) \/ RelookForget(a) \/ Spawn(a) \/ SpawnFail(a)
  \/ Enter \/ Stats \/ Leave \/ Mend

Spec == Init /\ [][Next]_vars

--------------------------------------------------------------------------
(* What must always hold *)

TypeOK ==
  /\ dbN \in 0..2 /\ memN \in 0..2 /\ got \in 0..2 /\ {dbX, dbY, memX, memY, fresh, err, stale, failed} \subseteq BOOLEAN
  /\ {dbL, dbF, memL, memF} \subseteq 0..1 /\ lib \in -1..gen
  /\ rs \in {"idle", "running", "rerun"} /\ \A i \in I : inv[i] \in {"none", "load", "install", "next", "after"}
  /\ fw \in {"none"} \cup A /\ settling \in 0..2 /\ hc \in {"none", "run", "done"} /\ q \in {"out", "in"}

\* Live: "A request reads `cat` inside the gate ..., so it sees one catalog from start to finish", and the looks too
OneCatalog == q = "in" => seen = mem

\* Ben's request has committed Y and its relook has not yet put it in (private_write: "the change is in every view
\* (relook) before this answers"), or the relook raised (a question for the owner: PrivateEvenWhenARelookFails)
Outstanding == pc["ben"] \in {"relook", "install", "failed"}

\* private.py: X is private to its sharer "from the moment ingest records it" (ingest-iphone.sh), and with_private:
\* "no member is shown a new upload before it has been checked"; Y, from the moment Ben's answer says so. What the app
\* shows anyone else is never a photo the catalog says is private to Ben, but while his own request takes it in.
ShownAsTheCatalogSays ==
  /\ ~(memN >= 1 /\ ~memX /\ dbX)
  /\ (~memY /\ dbY) => Outstanding

\* photo_app.py: a change to what someone holds private "is in every view before the answer"
InEveryViewBeforeTheAnswer == pc["ben"] = "answered" => memY = dbY

\* Nothing is under way: the ingest's runs have each told the app, no reload is running, nobody is between a commit
\* and its relook or its showing.
Quiet ==
  /\ ig \in {"add1", "add2", "done"} /\ \A i \in I : inv[i] = "none"
  /\ \A a \in A : pc[a] \in {"idle", "answered", "done", "failed"}

\* Once nothing is being reloaded or looked at again, what is in memory is what the catalog says (but after a reload or
\* a relook that raised: AsTheCatalogEvenAfterAFailure, for the owner)
AsTheCatalogOnceQuiet == Quiet /\ ~failed => <<memN, memX, memY, memL, memF>> = db

\* reload_catalog: "Asked again mid-run, it runs again"; the state is idle exactly when no call is in its loop
ReloadStateMatches == (rs = "idle") <=> \A i \in I : inv[i] \in {"none", "after"}

\* IngestJob.fresh: "a run added photos, so the app has not loaded them yet": once no reload is under way, it is
\* false only if every photo a run told the app about is in the catalog it serves
FreshTold == (Quiet /\ rs = "idle") => (fresh \/ memN = dbN)

\* Changes' busy: true while a change is still being taken in (Ada's thread, Ben's), and false again once all is done
BusyWhileSettling == (hc = "run" \/ pc["ada"] \in {"relook", "install", "forget", "feed"}) => settling > 0
NotBusyOnceSettled == (hc # "run" /\ pc["ada"] \in {"idle", "done", "failed"} /\ pc["ben"] # "spawn") => settling = 0

\* Live: "anything worked out from the catalog ... belongs to the generation it was made in, and is never used in a
\* later one"
NoStaleWork == ~stale

(* Not in Reload.cfg: each is broken from a failure until Mend has run (the minute timer; the owner's word on issues
   #138 and #139). Before that, nothing tried again: they stayed broken until the next ingest, change or restart. An
   invariant cannot say "within a minute"; AsTheCatalogOnceQuiet says the rest: whatever memory has that the catalog
   does not say, once quiet, is known to have failed, and Mend is enabled. *)

\* A reload that raised, or a relook: until Mend, the photos it was to bring in are not searchable, and the listeners
\* wait for it (loaded_through).
AsTheCatalogEvenAfterAFailure == Quiet => <<memN, memX, memY, memL, memF>> = db

\* Ben's withdrawal is committed; his relook raises, and his request is answered an error. Y is shown to everyone,
\* whatever it now says, until Mend (or anything else that relooks or reloads).
PrivateEvenWhenARelookFails == ~(pc["ben"] = "failed" /\ dbY /\ ~memY)

(* For asking narrower questions, as an ACTION_CONSTRAINT in a .cfg of your own. NoRace: nothing happens between a
   relook's read and its install, nor between a reload's read and its install. Quiet (above) is where no read waits
   for its write. *)
NoRace == /\ \A a \in Relooks : pc[a] = "install" => pc'[a] # "install"
          /\ \A i \in I : inv[i] = "install" => inv'[i] # "install"

(* Left out of this model, all of it in the code:
   - the scripts run by hand on the catalog: `private.py library` (holds photos private) and `estate.py purge`
     (takes photos out) are noticed by their journal entries on the listeners' next pass (PhotoApp.notice, every 30
     seconds: a relook for an entry after looks_through, a reload of the whole catalog for a purge after
     loaded_through; issue #140), which are the relook and the reload modelled here with one more caller;
     `estate.py migrate` still tells the app nothing;
   - the release timer (release_due, then relook, as Ben's request here), the lens routes (relook, then
     on_hidden_change(people): Ben's shape), a members change (on_hidden_change(None): Ada's shape), held_review
     (Ben's), and intake's on_ready and on_faces (more calls of reload_catalog);
   - the views as such (Live.views and shares: let go in every swap, kept only for the cat and looks they were worked
     out from) and Browse's cache (keyed by its generation, and so never used in a later one);
   - several photos per run, and more than one photo held: X is every photo an ingest run adds, W a run's photos
     once check has let them go;
   - loaded_through (the journal mark taken before the load, set only when the catalog is in), and the listeners
     waiting on it: after a failed reload they wait until the next good one (AsTheCatalogEvenAfterAFailure);
   - feed_update and load_people as such (hidden_changed's work; People reads the catalog itself): the feeds, and the
     phones that follow them, are specs/PhoneReplica.tla, and People specs/People.tla; the pregenerate thread, and what
     a run's error message says when feed_update raises after the catalog is in (reported as a failed reload, though
     the catalog is in);
   - who waits for whom: a swap waits for the requests in the gate and makes new ones wait; that, fav_write and
     SQLite's write lock are specs/ReloadLocks.tla. Here a swap is one step;
   - the window between a change set's commit and on_change (Changes.write lets its lock go first): `busy` is false
     there though the change is not yet in. It is the time a function call takes. *)
=============================================================================
