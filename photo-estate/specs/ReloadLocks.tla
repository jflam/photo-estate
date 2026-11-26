----------------------------- MODULE ReloadLocks -----------------------------
(* Who waits for whom among the threads of specs/Reload.tla: the catalog gate (catalog.Gate: requests share it; a swap
   takes it alone; a swap that waits makes every request that comes after it wait too, so a stream of requests cannot
   starve it), PhotoApp.fav_write, and SQLite's write lock, which a request inside the gate takes first on many routes
   now (BEGIN IMMEDIATE: members._begin, lenses._begin, private._begin, Changes.write, member_inbox, stories) and the
   ingest script holds in its own process for up to 200 photos at a time (estate.py ingest commits every 200, and
   copies each file into the store inside the transaction). Kept apart from Reload.tla because the question is
   another one (does anything wait for ever, rather than what is in memory), and because the waiting makes every step
   of Reload.tla three (ask, wait, let go): together they would be one model too big to read or to check in CI.

   The lock order the code states (catalog.Gate): the gate first, then any module's own lock; never the gate while
   holding one. And PhotoApp: fav_write, then the gate. Every lock, and where two are held at once:
     gate (read side)   every request but UNGATED, the downloads, favourites, the lens and private writes; feed_update
                        and listen_env's find and refused take it as a request does; Admin.stats_job for its numbers
     gate (swap)        Live.install (reload_catalog), Live.relook (PhotoApp.relook), Live.update (set_favorites)
     views_lock         inside the gate or a swap (Live.visible_for, shared_for, install, relook, forget_views); it is
                        let go before a View is worked out, and nothing is waited for while it is held
     fav_write          set_favorites (then SQLite's write lock, let go; then the swap), relook (then the swap), and,
                        since the fix in Reload.tla, reload_catalog's install (then the swap)
     reload_lock, settling_lock, Browse.lru_lock, IngestJob.lock, the Lifecycle's lock: held only to read or set a
                        few values; nothing else is taken inside them
     Admin.st_lock      inside the gate (stats_payload: then views_lock, through shared_for); stats_job takes the
                        gate only with it let go
     feeds_lock         inside the gate or outside it (feed_of); a Feed made inside it opens its own file
     members_lock       load_members: inside the gate (change_member, ask_in) or before any (start); then SQLite reads
     people_loading     load_people, outside the gate (threads, one of them started by reload_catalog after its swap when
                        what People is built without changed), across People's whole load (up to a minute)
     People.lock        each People's own: inside the gate (the People page's requests: then SQLite's write lock, in a
                        change), and in load_people outside it (People.replaced_by: the old one's, then the new one's,
                        then SQLite's write lock); a change passed on to the People that replaced it holds the old one's
                        then the new one's. None is held while the gate, fav_write or people_loading is asked for
                        (specs/People.tla)
     Changes.lock       Changes.write, inside the gate: then SQLite's write lock
     SQLite's write lock  inside the gate (the routes above); set_favorites, outside it with fav_write held; the
                        ingest script and the other scripts, in their own processes, holding no lock of the app's
   A read of SQLite never waits for the write lock (the catalog is in WAL mode: embed.connect), so a swap's own work
   (with_private, caught_up) waits for nothing but the gate it holds.

   Each thread runs once, in the order of its locks as the code takes them; the gated request that writes is any of
   the routes that take SQLite's write lock inside the gate. Guarded = TRUE is the code (reload_catalog's install
   under fav_write), FALSE the code before. ReloadLocks.cfg checks for deadlock: a state where something waits and
   nothing can move. One invariant at the end is broken by the code as it is and is not in ReloadLocks.cfg: any
   holder of SQLite's write lock for long still holds the app up. The ingest was the one that did (up to 200 files
   copied inside one transaction); since issue #141 it copies with no transaction open and holds the lock only for
   its inserts (estate.record). PhotoApp.mend and PhotoApp.notice (the minute timer, the listeners' pass) call
   relook and reload_catalog as the threads here do, taking the same locks in the same order: more threads of two
   kinds already here, and none new. *)
EXTENDS Naturals, Sequences

CONSTANT Guarded

\* The threads, each with its steps below:
\*   write    a request PhotoApp.handle lets into the gate (PhotoApp.gated) whose route takes SQLite's write lock
\*   get      a request in the gate that reads only (GET /api/info); get2, a second one
\*   relook   PhotoApp.relook: fav_write, read_looks (a read: no lock), Live.relook's swap
\*   fav      PhotoApp.set_favorites: fav_write, the table (the write lock, then the commit), Live.update's swap
\*   reload   PhotoApp.reload_catalog: load_catalog (a read: no lock), Live.install's swap (Guarded: under fav_write)
\*   ingest   the ingest script, in its own process: a batch under the write lock
T == {"write", "get", "get2", "relook", "fav", "reload", "ingest"}

VARIABLES
  pc,        \* each thread's place in its steps
  readers,   \* Gate.readers
  writing,   \* Gate.writing
  waiting,   \* Gate.waiting: swaps waiting for the requests in the gate to leave
  fw,        \* who holds fav_write ("none")
  sql        \* who holds SQLite's write lock ("none")

vars == <<pc, readers, writing, waiting, fw, sql>>

Steps(t) ==
  CASE t = "write"  -> <<"enter", "sql", "unsql", "leave">>
    [] t \in {"get", "get2"} -> <<"enter", "leave">>
    [] t = "relook" -> <<"fw", "ask", "swap", "unswap", "unfw">>
    [] t = "fav"    -> <<"fw", "sql", "unsql", "ask", "swap", "unswap", "unfw">>
    [] t = "reload" -> IF Guarded THEN <<"fw", "ask", "swap", "unswap", "unfw">> ELSE <<"ask", "swap", "unswap">>
    [] t = "ingest" -> <<"sql", "unsql">>

At(t) == IF pc[t] <= Len(Steps(t)) THEN Steps(t)[pc[t]] ELSE "end"
Move(t) == pc' = [pc EXCEPT ![t] = @ + 1]

Init == pc = [t \in T |-> 1] /\ readers = 0 /\ writing = FALSE /\ waiting = 0 /\ fw = "none" /\ sql = "none"

\* Gate.__enter__: waits while a swap is in or waiting
Enter(t) == At(t) = "enter" /\ ~writing /\ waiting = 0 /\ readers' = readers + 1 /\ Move(t)
              /\ UNCHANGED <<writing, waiting, fw, sql>>
\* Gate.__exit__
Leave(t) == At(t) = "leave" /\ readers' = readers - 1 /\ Move(t) /\ UNCHANGED <<writing, waiting, fw, sql>>
\* Gate.swap: counted as waiting at once, then in alone once no request reads and no swap is in
Ask(t) == At(t) = "ask" /\ waiting' = waiting + 1 /\ Move(t) /\ UNCHANGED <<readers, writing, fw, sql>>
Swap(t) == At(t) = "swap" /\ readers = 0 /\ ~writing /\ waiting' = waiting - 1 /\ writing' = TRUE /\ Move(t)
             /\ UNCHANGED <<readers, fw, sql>>
Unswap(t) == At(t) = "unswap" /\ writing' = FALSE /\ Move(t) /\ UNCHANGED <<readers, waiting, fw, sql>>
\* `with self.fav_write`
Fw(t) == At(t) = "fw" /\ fw = "none" /\ fw' = t /\ Move(t) /\ UNCHANGED <<readers, writing, waiting, sql>>
Unfw(t) == At(t) = "unfw" /\ fw' = "none" /\ Move(t) /\ UNCHANGED <<readers, writing, waiting, sql>>
\* BEGIN IMMEDIATE (or a first write), and the commit; a busy timeout is not modelled: the lock is waited for
Sql(t) == At(t) = "sql" /\ sql = "none" /\ sql' = t /\ Move(t) /\ UNCHANGED <<readers, writing, waiting, fw>>
Unsql(t) == At(t) = "unsql" /\ sql' = "none" /\ Move(t) /\ UNCHANGED <<readers, writing, waiting, fw>>

\* every thread has run: nothing more happens (not a deadlock)
Done == (\A t \in T : At(t) = "end") /\ UNCHANGED vars

Next == Done \/ \E t \in T : Enter(t) \/ Leave(t) \/ Ask(t) \/ Swap(t) \/ Unswap(t) \/ Fw(t) \/ Unfw(t) \/ Sql(t) \/ Unsql(t)

Spec == Init /\ [][Next]_vars

TypeOK == readers \in 0..3 /\ waiting \in 0..3 /\ writing \in BOOLEAN /\ fw \in {"none"} \cup T /\ sql \in {"none"} \cup T

\* Gate: a swap is in alone
Alone == writing => readers = 0

(* Broken by the code as it is, and not in ReloadLocks.cfg: a question for the owner. While the ingest script holds
   SQLite's write lock, a request inside the gate that waits for it keeps the gate, a swap waits for that request,
   and every request that comes after the swap waits for the swap: the whole app waits on the ingest's batch (up to
   200 files copied into the store), or until the request's busy timeout (60 s, embed.connect) fails it. *)
NothingWaitsOnTheIngest ==
  ~(sql = "ingest" /\ At("write") = "sql" /\ waiting > 0 /\ At("get") = "enter" /\ ~writing)

(* Left out: the leaf locks above (nothing is waited for while one is held); the gate entered twice by one thread
   (Gate.__enter__ does not refuse it as swap() does: a request that entered it again while a swap waited would wait
   for itself; nothing does now); SQLite's busy timeout, which ends a wait with an error; the listeners, the feeds and
   People, which take the gate as a request does or not at all. *)
=============================================================================
