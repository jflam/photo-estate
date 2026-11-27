------------------------------ MODULE PhoneReplica ------------------------------
(* A member's phone keeps its own copy of what that member sees (a SQLite replica), and follows a change feed of it
   that the server keeps: the server is the only writer of the catalog, the phone a replica that works offline. Being
   wrong decides what a member is shown on their phone, after a photo was made private to someone else or deleted.
   specs/Reload.tla is the catalog in memory and the views, and leaves feed_update out: this model starts there.

   THE MACHINE (step 1 of specs/README.md's loop)

   The member here is Ada; a photo is Y (Ben's, which he makes private to himself and lets go again: a relook; Ada's
   favourite) or W (whose catalog row changes, or which leaves the catalog and comes back: a reload).

     The member's view (catalog.Live.visible_for(Ada)): per photo, shown or not, and its row (View.feed_row: its facts,
     `fav`, feed_extra). Moved, each in one swap with no request reading, by:
       - PhotoApp.relook (Ben's POST /api/private, a lens, a change set, a release), which then starts the thread of
         PhotoApp.hidden_changed, whose feed_update reads after the relook;
       - PhotoApp.reload_catalog's install, which then calls feed_update itself, in the same thread, before its loop
         may run again;
       - PhotoApp.set_favorites' show (Live.update), which then calls Feed.touch, after the swap and outside fav_write,
         and answers (PhotoApp.favorite: POST /api/favorite).

     The feed (sync.Feed, one per member: sync-feed.db for the first sysadmin, sync-feed-<id>.db for anyone else): it is
     not there (none of their phones has asked yet, or its file was lost), or it is: `epoch`, `version`, `ready` (false
     after a start until its first whole update), and per photo none | a row (digest, version) | gone (version).
     Written, each under Feed.lock, by:
       - Feed.update(rows) (whole): PhotoApp.feed_update reads each member's rows inside the gate (PhotoApp.feed_rows)
         and writes them outside it: a read and a write, two actions; and the /api/sync route on a member's first phone
         (PhotoApp.feed_of(make=True)), inside the gate;
       - Feed.touch(rows) (some rows): set_favorites, with the rows it worked out in its swap;
       - Guarded, Feed.page itself (Feed._agree), from the route, inside the gate: see below.

     The phone (SyncEngine.run, one run at a time: sync()): idle | asked (the cursor read: epoch, since, after; the GET
     sent) | answered (a page, not yet applied). A page is applied with its cursor in one transaction (Catalog.applyPage:
     Catalog.apply, and Catalog.dropOtherEpochs when there is no more), or the request fails and nothing is written. The
     replica's rows, each with the epoch it came in, are written by that transaction and by Favourites.keep (the
     phone's copy of a favourite, once the server agreed: Catalog.keep, a transaction of its own). SQLite.swift's Store
     has one connection and one queue for writing, so the two are one after the other, never interleaved.

   What the code assumes cannot happen: a page carries a row of a photo its member does not see (the route asks only
   whether the catalog has it, `s in v.cat.meta`); a row sent is other than the feed's (rows are read from the request's
   view, which may be ahead of the feed); an older view is written after a newer one (nothing orders feed_update's write,
   nor a touch, against another); a favourite is set as the member's first phone makes their feed, or before a feed is
   ready, and no touch follows; a page answered before a favourite the phone kept is applied over it; the phone keeps a
   favourite another of the member's requests has already changed.

   Guarded = TRUE is the code after the fixes of November 2026 (FALSE: as it was):
     - Feed.page(..., now=) first makes what it is about to send agree with the request's view (Feed._agree): the rows
       after the cursor, in order, until `limit` of them are as the view has them now, each one that is not is put
       right (gone, if the view does not show it; the view's row, if it differs), and every deletion after the cursor of
       a photo the view shows is taken back (its row again); each at the next version. So no page carries a row its
       member does not see, every row sent is the one the feed has, and an older write is put right by the next page;
     - set_favorites asks for the member's feed in its swap, and touches it whether it is ready or not;
     - POST /api/favorite answers `fav` as the photo is when it answers, and Favourites.set keeps that;
     - SyncEngine.run does not apply a page asked before a favourite the phone kept: Catalog.applyPage finds the kv row
       `edits` moved (Catalog.keep moves it), writes nothing, and the run asks again.
   Restarts = TRUE lets the app restart (its feed's file kept, or lost). *)
EXTENDS Integers, FiniteSets, Sequences, TLC

CONSTANTS Guarded, Restarts, Budget, Limit, FU, FS

Ph == {1, 2}                  \* Y (1) and W (2), as shas: "" is 0, and "~" (after every sha) is 3
Y == 1
W == 2
Tilde == 3
\* Slots within FU (hidden_changed/start feed_updates) and FS (set_favorites calls) are interchangeable; all
\* invariants here are safety invariants, so TLC's symmetry reduction is exact.
Sym == {f @@ g : f \in Permutations(FU), g \in Permutations(FS)}

Lt(a, b) == a[1] < b[1] \/ (a[1] = b[1] /\ a[2] < b[2])     \* (version, sha) order
Le(a, b) == ~Lt(b, a)

VARIABLES
  \* the member's view (catalog.Live)
  vis,      \* Y is not private to Ben, nor hidden by Ada's filters (W: TRUE)
  fav,      \* Ada's favourite (W: FALSE)
  meta,     \* the photo's row in the catalog, a version: 0, 1 (Y: 0)
  incat,    \* in the catalog (Y: TRUE)
  \* the feed (sync.Feed)
  fex,      \* the member has a feed (PhotoApp.feeds)
  ready,    \* Feed.ready
  ep,       \* Feed.epoch, as a number
  fv,       \* Feed.version
  fst,      \* per photo: "none" | "row" (in Feed.rows) | "gone" (in Feed.gone)
  fdg,      \* the digest of its row (the row, as a number), -1 for none
  fver,     \* the version it last changed at
  \* the server's threads
  fu,       \* feed_update threads: [st: "idle" | "read" | "write", has: the feed was in PhotoApp.feeds, rows]
  rl,       \* reload_catalog's thread, its feed_update after an install: the same
  fs,       \* set_favorites calls: [st: "idle" | "begun" | "touch", on, cap: the feed it found, rows, phone, at]
  \* the phone (SyncEngine, Catalog)
  ph,       \* "idle" | "asked" | "answered"
  ask,      \* the cursor it read when it asked: [e, v, a]
  resp,     \* the page: [rows, ver, del, cur, more, ep, q, at]
  rep,      \* the replica: per photo its row (a number), -1 for none
  repe,     \* the epoch each row came in
  repv,     \* the feed's version of the row when it was sent
  ce, cv, ca,   \* the cursor: epoch (0: none yet), since, after
  pf,       \* a favourite from the phone (Favourites.set): "none" | "post" (its POST /api/favorite not yet answered)
  kept,     \* the photos Favourites.keep wrote since the page in flight was asked (`edits` moved)
  \* not in the code
  keptAt,   \* chg when the server agreed to the favourite the phone last kept; -1 none
  chg,      \* how many changes there have been to the view
  n,        \* how many changes (relook, reload, favourite) and restarts have been made, against Budget
  told,     \* the phone's last page said no more, was answered with nothing under way, and nothing changed since
  sentHidden,   \* a page carried a row of a photo the member's view did not show
  undone        \* a page was applied over a favourite the phone kept, to say otherwise

vars == <<vis, fav, meta, incat, fex, ready, ep, fv, fst, fdg, fver, fu, rl, fs, ph, ask, resp, rep, repe, repv, ce, cv,
          ca, pf, kept, keptAt, chg, n, told, sentHidden, undone>>
view == <<vis, fav, meta, incat>>
feed == <<fex, ready, ep, fv, fst, fdg, fver>>
phone == <<ph, ask, resp, rep, repe, repv, ce, cv, ca, pf, kept, keptAt>>

Shown(p) == incat[p] /\ vis[p]
Content(p) == meta[p] * 2 + (IF fav[p] THEN 1 ELSE 0)          \* View.feed_row(p)
Row(p) == IF Shown(p) THEN Content(p) ELSE -1
View == [p \in Ph |-> Row(p)]                                   \* PhotoApp.feed_rows(v)
None == [p \in Ph |-> -1]
Idle == [st |-> "idle", has |-> FALSE, rows |-> None]
NoFav == [st |-> "idle", on |-> FALSE, cap |-> FALSE, rows |-> None, phone |-> FALSE, at |-> 0]
NoPage == [rows |-> None, ver |-> [p \in Ph |-> 0], del |-> {}, cur |-> <<0, 0>>, more |-> FALSE, ep |-> 0,
           q |-> FALSE, at |-> 0]

Init ==
  /\ vis = [p \in Ph |-> TRUE] /\ fav = [p \in Ph |-> FALSE] /\ meta = [p \in Ph |-> 0] /\ incat = [p \in Ph |-> TRUE]
  \* Ada's phone has asked before (her feed is there and up to date: everything at version 1), or never
  /\ fex \in BOOLEAN /\ ready = fex /\ ep = 1
  /\ fv = IF fex THEN 1 ELSE 0
  /\ fst = [p \in Ph |-> IF fex THEN "row" ELSE "none"]
  /\ fdg = [p \in Ph |-> IF fex THEN 0 ELSE -1] /\ fver = [p \in Ph |-> fv]
  /\ fu = [s \in FU |-> Idle] /\ rl = Idle /\ fs = [s \in FS |-> NoFav]
  /\ ph = "idle" /\ ask = [e |-> 0, v |-> 0, a |-> 0] /\ resp = NoPage
  /\ rep = None /\ repe = [p \in Ph |-> 0] /\ repv = [p \in Ph |-> 0] /\ ce = 0 /\ cv = 0 /\ ca = 0
  /\ pf = "none" /\ kept = {} /\ keptAt = -1
  /\ chg = 0 /\ n = 0 /\ told = FALSE /\ sentHidden = FALSE /\ undone = FALSE

ServerQuiet == (\A s \in FU : fu[s].st = "idle") /\ rl.st = "idle" /\ \A s \in FS : fs[s].st = "idle"
PhoneQuiet == ph = "idle" /\ pf = "none"

--------------------------------------------------------------------------
(* sync.Feed, under Feed.lock. R: the rows (-1: none). *)

Changed(R) == {p \in Ph : R[p] # -1 /\ (fst[p] # "row" \/ fdg[p] # R[p])}
Removed(R) == {p \in Ph : fst[p] = "row" /\ R[p] = -1}
NewSt(st, C, Rm) == [p \in Ph |-> IF p \in C THEN "row" ELSE IF p \in Rm THEN "gone" ELSE st[p]]
NewDg(dg, C, Rm, R) == [p \in Ph |-> IF p \in C THEN R[p] ELSE IF p \in Rm THEN -1 ELSE dg[p]]
NewVer(ver, C, Rm, v) == [p \in Ph |-> IF p \in C \cup Rm THEN v + 1 ELSE ver[p]]
NewV(v, C, Rm) == IF C \cup Rm = {} THEN v ELSE v + 1

\* Feed.update(rows): whatever is new, different or gone gets the next version (Feed._write, one transaction, then
\* memory); ready from then on
Whole(R) ==
  LET C == Changed(R)
      Rm == Removed(R) IN
  /\ fst' = NewSt(fst, C, Rm) /\ fdg' = NewDg(fdg, C, Rm, R) /\ fver' = NewVer(fver, C, Rm, fv) /\ fv' = NewV(fv, C, Rm)
  /\ ready' = TRUE

\* Feed.touch(rows): those that differ get the next version; nothing is taken as gone
Touch(R) ==
  LET C == Changed(R) IN
  /\ fst' = NewSt(fst, C, {}) /\ fdg' = NewDg(fdg, C, {}, R) /\ fver' = NewVer(fver, C, {}, fv) /\ fv' = NewV(fv, C, {})
  /\ UNCHANGED ready

--------------------------------------------------------------------------
(* What changes the member's view: each one swap (Live.relook, Live.install, Live.update), with no request reading *)

\* PhotoApp.relook (Ben's POST /api/private: Y private to him, or let go; or Ada's own filter), then on_hidden_change:
\* the thread of hidden_changed, whose feed_update reads after the relook
Relook(s) ==
  /\ n < Budget /\ fu[s].st = "idle"
  /\ vis' = [vis EXCEPT ![Y] = ~vis[Y]]
  /\ chg' = chg + 1 /\ n' = n + 1 /\ told' = FALSE
  /\ fu' = [fu EXCEPT ![s].st = "read"]
  /\ UNCHANGED <<fav, meta, incat, feed, rl, fs, phone, sentHidden, undone>>

\* PhotoApp.reload_catalog: Live.install (W's row changed: a place found, a face named; or W gone from the catalog, or
\* back), then feed_update in the same thread: its loop runs again only after that
ReloadInstall ==
  /\ n < Budget /\ rl.st = "idle"
  /\ \/ meta' = [meta EXCEPT ![W] = 1 - meta[W]] /\ UNCHANGED incat
     \/ incat' = [incat EXCEPT ![W] = ~incat[W]] /\ UNCHANGED meta
  /\ chg' = chg + 1 /\ n' = n + 1 /\ told' = FALSE
  /\ rl' = [rl EXCEPT !.st = "read"]
  /\ UNCHANGED <<vis, fav, feed, fu, fs, phone, sentHidden, undone>>

\* PhotoApp.feed_update, for this member: `list(self.feeds.items())`, then `with self.live.gate: rows = feed_rows(...)`
Read == [st |-> "write", has |-> fex, rows |-> View]
\* and `feed.update(rows)`, outside the gate (a feed made since the list was taken is not in it)
Write(t) == IF t.has /\ fex THEN Whole(t.rows) ELSE UNCHANGED <<fst, fdg, fver, fv, ready>>

HcRead(s) ==
  /\ fu[s].st = "read" /\ fu' = [fu EXCEPT ![s] = Read]
  /\ UNCHANGED <<view, feed, rl, fs, phone, chg, n, told, sentHidden, undone>>
HcWrite(s) ==
  /\ fu[s].st = "write" /\ Write(fu[s]) /\ fu' = [fu EXCEPT ![s] = Idle]
  /\ UNCHANGED <<view, fex, ep, rl, fs, phone, chg, n, told, sentHidden, undone>>
RlRead ==
  /\ rl.st = "read" /\ rl' = Read
  /\ UNCHANGED <<view, feed, fu, fs, phone, chg, n, told, sentHidden, undone>>
RlWrite ==
  /\ rl.st = "write" /\ Write(rl) /\ rl' = Idle
  /\ UNCHANGED <<view, fex, ep, fu, fs, phone, chg, n, told, sentHidden, undone>>

\* PhotoApp.set_favorites(shas=[Y], on), by Ada on the web or from her phone (Favourites.set's POST /api/favorite):
\* `feed = self.feed_of(member)`, before fav_write. Guarded: asked for in the swap instead (FavShow)
FavBegin(s, on, mine) ==
  /\ n < Budget /\ fs[s].st = "idle"
  /\ mine => pf = "none"
  /\ fs' = [fs EXCEPT ![s] = [st |-> "begun", on |-> on, cap |-> fex, rows |-> None, phone |-> mine, at |-> 0]]
  /\ n' = n + 1
  /\ pf' = IF mine THEN "post" ELSE pf
  /\ UNCHANGED <<view, feed, fu, rl, ph, ask, resp, rep, repe, repv, ce, cv, ca, kept, keptAt, chg, told, sentHidden,
                 undone>>

\* under fav_write, the table (lenses.set_favorites, the journal), then Live.update(show): in a swap, the member's view
\* told in place, and the rows to touch worked out: those it changed that the member sees, `if feed and feed.ready`
\* (Guarded: the feed asked for here, and touched ready or not)
FavShow(s) ==
  LET f == fs[s]
      feedNow == IF Guarded THEN fex ELSE f.cap
      changed == fav[Y] # f.on
      touch == feedNow /\ (Guarded \/ ready) /\ changed /\ Shown(Y) IN
  /\ f.st = "begun"
  /\ fav' = [fav EXCEPT ![Y] = f.on]
  /\ chg' = IF changed THEN chg + 1 ELSE chg
  /\ told' = IF changed THEN FALSE ELSE told
  /\ fs' = [fs EXCEPT ![s].st = "touch", ![s].cap = feedNow, ![s].at = chg',
                      ![s].rows = [p \in Ph |-> IF p = Y /\ touch THEN 2 * meta[Y] + (IF f.on THEN 1 ELSE 0) ELSE -1]]
  /\ UNCHANGED <<vis, meta, incat, feed, fu, rl, phone, n, sentHidden, undone>>

\* `if rows and feed: feed.touch(rows)`, after the swap and with fav_write let go; then PhotoApp.favorite answers
\* `fav` (Guarded: as the photo is now; before, what was asked). A phone's Favourites.set then keeps that
\* (Favourites.keep: Catalog.keep, one transaction on the replica's write queue), in this same step: it is queued as soon
\* as the answer is in, before the phone can apply a page the server answered later (see the end of this module)
FavTouch(s) ==
  LET f == fs[s]
      said == IF Guarded THEN fav[Y] ELSE f.on IN
  /\ f.st = "touch"
  /\ IF f.cap /\ fex /\ \E p \in Ph : f.rows[p] # -1 THEN Touch(f.rows) ELSE UNCHANGED <<fst, fdg, fver, fv, ready>>
  /\ fs' = [fs EXCEPT ![s] = NoFav]
  /\ IF f.phone
       THEN /\ pf' = "none"
            /\ rep' = [rep EXCEPT ![Y] = IF rep[Y] = -1 THEN -1 ELSE rep[Y] - (rep[Y] % 2) + (IF said THEN 1 ELSE 0)]
            /\ kept' = (IF ph = "idle" THEN {} ELSE kept \cup {Y})      \* (no page in flight: nothing to tell)
            /\ keptAt' = (IF ph = "idle" THEN -1 ELSE f.at)
       ELSE UNCHANGED <<pf, rep, kept, keptAt>>
  /\ UNCHANGED <<view, fex, ep, fu, rl, ph, ask, resp, repe, repv, ce, cv, ca, chg, n, told, sentHidden, undone>>

\* The app restarts (embed.py serve again): what was in memory is gone, and so is every thread; the catalog is as it
\* was, so the view is the same. Its feeds are read again from their files (`ready` false until start()'s feed_update),
\* or a feed's file was lost: a member's (sync-feed-<id>.db) is not found by the glob, and is made again by their next
\* phone, with a new epoch. (A favourite's table and its show are one step here: a restart between them comes up with
\* the table's word, which is the show's.)
Restart ==
  /\ Restarts /\ n < Budget
  /\ \E lose \in BOOLEAN :
       /\ fex' = (fex /\ ~lose) /\ ready' = FALSE
       /\ IF lose THEN /\ ep' = ep + 1 /\ fv' = 0 /\ fst' = [p \in Ph |-> "none"] /\ fdg' = None
                       /\ fver' = [p \in Ph |-> 0]
                  ELSE UNCHANGED <<ep, fv, fst, fdg, fver>>
  /\ n' = n + 1
  /\ \E s0 \in FU : fu' = [s \in FU |-> IF s = s0 THEN [Idle EXCEPT !.st = "read"] ELSE Idle]   \* start()'s feed_update
  /\ rl' = Idle /\ fs' = [s \in FS |-> NoFav]
  /\ pf' = IF pf = "post" THEN "none" ELSE pf      \* a POST not yet answered fails: nothing is kept
  /\ UNCHANGED <<vis, fav, meta, incat, ph, ask, resp, rep, repe, repv, ce, cv, ca, kept, keptAt, chg, told,
                 sentHidden, undone>>

--------------------------------------------------------------------------
(* GET /api/sync (PhotoApp.sync_route), inside the gate: one step, as no swap can happen while it is in there *)

\* Feed.page(epoch, since, after, limit) on a feed (st, ver, v): the shas to send, the deletions, the cursor, more
Key(ver, p) == <<ver[p], p>>
Todo(st, ver, start) == {p \in Ph : st[p] = "row" /\ Lt(start, Key(ver, p))}
Rank(ver, p, S) == Cardinality({q \in S : Lt(Key(ver, q), Key(ver, p))})
Ids(st, ver, start) == {p \in Todo(st, ver, start) : Rank(ver, p, Todo(st, ver, start)) < Limit}
More(st, ver, start) == Cardinality(Todo(st, ver, start)) > Limit
Cursor(st, ver, v, start) ==
  IF More(st, ver, start)
    THEN Key(ver, CHOOSE p \in Ids(st, ver, start) : Rank(ver, p, Todo(st, ver, start)) = Limit - 1)
    ELSE <<v, Tilde>>
Deleted(st, ver, v, start) ==
  {p \in Ph : st[p] = "gone" /\ Lt(start, Key(ver, p)) /\ Le(Key(ver, p), Cursor(st, ver, v, start))}

\* Guarded, Feed._agree: the rows after the cursor, in order, until `limit` of them are as the view has them now; the
\* ones on the way that are not (not shown, or another row) are returned, to be put right
Order(ver, S) == [i \in 1..Cardinality(S) |-> CHOOSE p \in S : Rank(ver, p, S) = i - 1]
RECURSIVE Walk(_, _, _)
Walk(sq, k, dg) ==
  IF sq = <<>> \/ k = Limit THEN {}
  ELSE LET p == Head(sq) IN
       IF Shown(p) /\ dg[p] = Content(p) THEN Walk(Tail(sq), k + 1, dg) ELSE {p} \cup Walk(Tail(sq), k, dg)

\* The route: `feed = self.feed_of(viewer)`; none yet: made now and feed.update(self.feed_rows(v)); not ready: 503 (the
\* phone waits and asks again: no answer here). Then the page, and `rows = [... for s in ids if s in v.cat.meta]`, each
\* View.feed_row(s) of the request's own view. Guarded: Feed.page(..., now=) puts right first what it is about to send
\* (Feed._agree): the rows the walk found, and the deletions after the cursor of photos the view shows; one write, at
\* the next version.
Answer ==
  LET make == ~fex
      R == View
      C0 == IF make THEN {p \in Ph : R[p] # -1} ELSE {}    \* (a feed just made is empty: every row is new)
      st1 == IF make THEN NewSt(fst, C0, {}) ELSE fst
      dg1 == IF make THEN NewDg(fdg, C0, {}, R) ELSE fdg
      ver1 == IF make THEN NewVer(fver, C0, {}, fv) ELSE fver
      v1 == IF make THEN NewV(fv, C0, {}) ELSE fv
      start == IF ask.e = ep THEN <<ask.v, ask.a>> ELSE <<0, 0>>
      bad == IF Guarded THEN Walk(Order(ver1, Todo(st1, ver1, start)), 0, dg1) ELSE {}
      back == IF Guarded THEN {p \in Ph : st1[p] = "gone" /\ Lt(start, Key(ver1, p)) /\ Shown(p)} ELSE {}
      hid == {p \in bad : ~Shown(p)}
      redo == {p \in bad : Shown(p)} \cup back
      st2 == NewSt(st1, redo, hid)
      dg2 == NewDg(dg1, redo, hid, R)
      ver2 == NewVer(ver1, redo, hid, v1)
      v2 == NewV(v1, redo, hid)
      ids == Ids(st2, ver2, start)
      rows == [p \in Ph |-> IF p \in ids /\ incat[p] THEN Content(p) ELSE -1] IN
  /\ ph = "asked" /\ (ready \/ make)
  /\ fex' = TRUE /\ ready' = TRUE
  /\ fst' = st2 /\ fdg' = dg2 /\ fver' = ver2 /\ fv' = v2
  /\ ph' = "answered"
  /\ resp' = [rows |-> rows, ver |-> ver2, del |-> Deleted(st2, ver2, v2, start), cur |-> Cursor(st2, ver2, v2, start),
              more |-> More(st2, ver2, start), ep |-> ep, q |-> ServerQuiet, at |-> chg]
  /\ sentHidden' = (sentHidden \/ \E p \in Ph : rows[p] # -1 /\ ~Shown(p))
  /\ UNCHANGED <<view, ep, fu, rl, fs, ask, rep, repe, repv, ce, cv, ca, pf, kept, keptAt, chg, n, told, undone>>

--------------------------------------------------------------------------
(* The phone: SyncEngine.run, Catalog.applyPage (Catalog.apply, Catalog.dropOtherEpochs) *)

\* read the cursor (Catalog.cursor: epoch, since, after; Guarded, `edits` with them), and GET /api/sync with it
PhoneAsk ==
  /\ ph = "idle"
  /\ ph' = "asked" /\ ask' = [e |-> ce, v |-> cv, a |-> ca] /\ kept' = {}
  /\ UNCHANGED <<view, feed, fu, rl, fs, resp, rep, repe, repv, ce, cv, ca, pf, keptAt, chg, n, told, sentHidden,
                 undone>>

\* (what the model keeps of a page in flight, let go once the phone is idle: nothing reads it then)
Done == ask' = [e |-> 0, v |-> 0, a |-> 0] /\ resp' = NoPage /\ kept' = {} /\ keptAt' = -1

\* the request fails, or the app is stopped, before the page is applied: nothing is written
PhoneFail ==
  /\ ph \in {"asked", "answered"}
  /\ ph' = "idle" /\ Done
  /\ UNCHANGED <<view, feed, fu, rl, fs, rep, repe, repv, ce, cv, ca, pf, chg, n, told, sentHidden, undone>>

\* Catalog.applyPage, one transaction: Catalog.apply (deleted, then the rows, each with the page's epoch), the cursor,
\* and when there is no more, Catalog.dropOtherEpochs. Guarded: not if Favourites.keep wrote since the page was asked
\* (`edits` moved): nothing is written, and the run asks again
PhoneApply ==
  LET r == resp
      rep1 == [p \in Ph |-> IF r.rows[p] # -1 THEN r.rows[p] ELSE IF p \in r.del THEN -1 ELSE rep[p]]
      repe1 == [p \in Ph |-> IF r.rows[p] # -1 THEN r.ep ELSE repe[p]] IN
  /\ ph = "answered"
  /\ ph' = "idle" /\ Done
  /\ IF Guarded /\ kept # {}
       THEN UNCHANGED <<rep, repe, repv, ce, cv, ca, told, undone>>
       ELSE /\ rep' = [p \in Ph |-> IF ~r.more /\ rep1[p] # -1 /\ repe1[p] # r.ep THEN -1 ELSE rep1[p]]
            /\ repe' = repe1
            /\ repv' = [p \in Ph |-> IF r.rows[p] # -1 THEN r.ver[p] ELSE repv[p]]
            /\ ce' = r.ep /\ cv' = r.cur[1] /\ ca' = r.cur[2]
            /\ told' = (~r.more /\ r.q /\ r.at = chg)
            /\ undone' = (undone \/ \E p \in kept : rep[p] # -1 /\ r.rows[p] # -1 /\ r.at < keptAt
                                                     /\ r.rows[p] % 2 # rep[p] % 2)
  /\ UNCHANGED <<view, feed, fu, rl, fs, pf, chg, n, sentHidden>>

Next ==
  \/ \E s \in FU : Relook(s) \/ HcRead(s) \/ HcWrite(s)
  \/ ReloadInstall \/ RlRead \/ RlWrite
  \/ \E s \in FS : FavShow(s) \/ FavTouch(s) \/ \E on, mine \in BOOLEAN : FavBegin(s, on, mine)
  \/ Restart
  \/ Answer
  \/ PhoneAsk \/ PhoneFail \/ PhoneApply

Spec == Init /\ [][Next]_vars

--------------------------------------------------------------------------
(* What must always hold *)

TypeOK ==
  /\ fst \in [Ph -> {"none", "row", "gone"}] /\ fdg \in [Ph -> -1..3] /\ rep \in [Ph -> -1..3]
  /\ ph \in {"idle", "asked", "answered"} /\ pf \in {"none", "post"} /\ kept \subseteq Ph
  /\ {fex, ready, told, sentHidden, undone} \subseteq BOOLEAN /\ n \in 0..Budget

\* sync.py: the feed is of "what that member sees"; photo_app.py: a photo one member's filters hide "leaves only that
\* member's phones". A page never carries a row of a photo the member's view does not show.
NeverSentUnseen == ~sentHidden

\* Once nothing is reloading, looking again, being favourited or syncing, and the phone has been told there is no more
\* by a page answered then: the replica is exactly what the member sees, row for row (favourites included), and so every
\* deletion has reached it.
AsSeenOnceQuiet == (ServerQuiet /\ PhoneQuiet /\ told) => rep = View

\* sync.Feed.page: the cursor means "I have every row, and every deletion, up to this (version, sha)". A cursor never
\* passes a row of the feed the phone has not got.
CursorNeverPasses ==
  (ph = "idle" /\ fex /\ ce = ep) =>
    \A p \in Ph : (fst[p] = "row" /\ Le(Key(fver, p), <<cv, ca>>)) => (rep[p] # -1 /\ repe[p] = ep /\ repv[p] >= fver[p])

\* SyncEngine: "A page that fails leaves the cursor where it was": while a page is in flight, nothing but its own
\* transaction moves the cursor it was asked with.
CursorAsRead == ph # "idle" => <<ce, cv, ca>> = <<ask.e, ask.v, ask.a>>

\* API.swift: once the server has agreed, "the phone's copy is changed to match, at once rather than at the next sync": a
\* page the server answered before it agreed is not applied over it.
KeptStays == ~undone

(* For narrower questions, as a CONSTRAINT in a .cfg of your own. OnlyW: Y is never made private nor a favourite, so
   what is left is W's reloads (its row changed, or W out of the catalog and back), the restarts and the phone: does a
   cursor passed over W ever leave it off the phone for good? *)
OnlyW == vis[Y] /\ ~fav[Y]

(* Left out of this model, all of it in the code:
   - other members and their feeds (each is its own: PhotoApp.feeds, one Feed and one lock each), and a member's two
     phones (each has its own cursor; the feed they share is made once, under feeds_lock, inside the gate: a second
     phone asking at once is answered 503, and asks again, or from the same feed and view:
     tests/test_phone_replica_states.py has the two at once);
   - more than two photos, and pages longer than Limit (1 here); the trips list (sent whole on the last page) and
     `total`; two reloads' feed_updates are one after the other (the reload's loop), as here;
   - the views themselves and when they are worked out (specs/Reload.tla): here the view is what the catalog says, and
     each change to it is one swap. A relook's read and its install, a favourite's table and its show, a reload's load
     outside the gate, are Reload.tla's;
   - hidden_changed's other work (load_people: specs/People.tla), and its own relook when it is the one that relooks
     (Ada's change set): its feed_update reads after the relook either way;
   - who waits for whom (specs/ReloadLocks.tla): Feed.lock is taken inside the gate by the route and outside it by
     feed_update and touch, and nothing is asked for while holding it;
   - a feed_update or a touch that raises (disk full): the feed stays as it was, and the next page, or the next change's
     feed_update, puts it right; a first phone's update that raises leaves a feed that is never ready until a
     feed_update (the phone is told 503, and asks again every 3 s until then);
   - the phone's keep as a step of its own: here it is done as the answer comes in. A page the server answered after
     the favourite, applied before its keep, would be overwritten by it (the phone would hold a favourite the feed does
     not know it holds): that needs the answers of two requests to arrive in the other order than the server gave them,
     or the keep, queued at once, to wait longer than a request to the server takes;
   - a feed's file and its memory parting (a write is one SQLite transaction, then memory: a crash between is a start
     that reads the file); the sysadmin's feed (sync-feed.db) is never lost to the glob, and a change of who the first
     sysadmin is makes sync-feed.db another member's (a new epoch to their phone: their own file is not read);
   - the phone's other copies: the album files kept "as last seen" (Albums.swift), the image cache (ImageStore: thumbs.db
     and the full-screen files, which Downloader.run forgets only for photos that have a thumbnail row, when it is
     restarted), and what the grid holds in memory until `revision` moves;
   - an album's favourite (Albums.swift keeps `on` for every photo the album showed, as it was before the fix for one
     photo), and two of the phone's favourites at once (the viewer waits for one before the next on the same photo);
   - a revoked member: their phone is refused every request (403) and keeps its replica, and their feed stays on the
     server (specs/Member.tla's list of what a revoke does not touch). *)
=============================================================================
