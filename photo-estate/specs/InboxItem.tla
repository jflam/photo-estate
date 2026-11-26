------------------------------ MODULE InboxItem ------------------------------
(* The inbox items about one thing (rows of `inbox_items`: member_id, kind, ref, state open | done | dismissed),
   and the thing they are about, written by the code that makes and decides it (member_inbox.py's put, close,
   retitle and dismiss, called by members.py, lenses.py, changes.py, private.py and stories.py, which also writes
   the table with SQL of its own).

   One run of the model is about one thing, of one of five shapes (`part`, chosen at the start; the others' variables
   stay as they start, so the states of the five are added, not multiplied, and no code makes or decides an item of
   one kind from what another kind's thing is):
     "request"   a newcomer x asking to be let in (members.request, members.change): member_request, put for every
                 active sysadmin (Ada and Dee, either of whom may stop or start being one: members.change)
     "lens"      a filter of Ben's (lenses.py): lens_proposed, put for every active sysadmin, and Cleo's lens_changed,
                 put for each live subscriber (member_inbox.lens_changed, from changes.Changes.tell)
     "question"  two questions to Cleo in one group (stories.py): question items, ask:<group>, made and joined by
                 stories.deal, done by stories._settle; three rows, since two may be open at once
     "held"      Cleo's held photos (private.py): her held item, one open per member, a later hold joining it (its ref
                 stays that of the batch that opened it: two refs here)
     "binding"   Cleo's own agent on a device (members.bind, members.decide_binding): binding
   and, in every part, the member whose item it is putting it away (member_inbox.dismiss).

   Who holds the write lock when it calls put or close (checked caller by caller; Python's `with con:` begins a
   transaction only at the first write): members.request (members._begin, before its lookup), members.bind (after
   its INSERT OR IGNORE, and only when that made the row), members.change and members.decide_binding (members._begin,
   before they read the member or the binding: since specs/Member.tla),
   lenses.propose, decide, publish, publish_album, subscribe, unsubscribe (lenses._begin), Changes.write and so
   Changes.tell and lens_changed (BEGIN IMMEDIATE), private._hold and close_held (private._begin, in every caller),
   stories.answer and stories.dismiss (their first INSERT or UPDATE, before _settle), tasks.Tasks.to_review (after
   the task's UPDATE) and listeners.series (BEGIN IMMEDIATE): each is one action here. Two are not:
     - stories.deal reads the member's questions (_for) before its transaction, and its open question items and put's
       own SELECT inside it, with no write before them: its read, then its write (DealRead, DealWrite);
     - member_inbox.dismiss reads the item (its SELECT) and then UPDATEs it by id, in a `with con:` with no write
       before: its read, then its write (DismissRead, DismissWrite).
   members.change reads the member with the write lock held (it did not before specs/Member.tla; that read decided
   nothing here: a member it moves out of pending was read pending, and who is a sysadmin after it is what it
   writes), so it is one action.

   Guarded = TRUE is the code. Guarded = FALSE is the code before it was fixed (November 2026), kept so the
   invariants can be seen to fail without the guard: stories.deal takes the write lock (BEGIN IMMEDIATE) before
   it reads, and reads the questions it deals again inside it; member_inbox.dismiss takes the write lock before it
   reads the item; members.change closes the member_request and lens_proposed items of a member who is not an
   active sysadmin after it; lenses.publish and lenses.publish_album taking a lens back close its lens_changed
   items; and (found by specs/Question.tla) member_inbox.dismiss of a question item puts its questions away. Here a
   write going by what is there when it writes is Now.

   InboxItem.cfg checks the code; InboxItem.before.<invariant>.cfg each find the violation there was.
   tests/test_specs.py runs them all with TLC, and tests/test_inbox_states.py replays each on the real code. *)
EXTENDS Naturals, FiniteSets

CONSTANT Guarded

Parts == {"request", "lens", "question", "held", "binding"}
Admins == {"ada", "dee"}            \* the members who may be sysadmins (both are at the start)
Slots == {"i1", "i2", "i3"}         \* Cleo's question items (each its own row: a question names the one it is in)
Refs == {"h1", "h2"}                \* the refs Cleo's held items may have (held:<batch>)
Ids == Admins \cup {"cleo"} \cup Slots \cup Refs
Q == {"q1", "q2"}                   \* the two questions to Cleo, one group_key
Tabs == {"t1", "t2"}                \* two dismiss requests at once
States == {"none", "open", "done", "dismissed"}

VARIABLES
  part,      \* which thing this run is about
  it,        \* each item's state ("none": never made). An admin's is their member_request or lens_proposed item; Cleo's
             \*   ("cleo") her lens_changed or binding item; a slot or a ref, her question or held item. A put that
             \*   finds none open makes a new row: here the id's state is "open" again (the old row is closed for good)
  admin,     \* each of Ada and Dee: an active sysadmin (members.role = 'sysadmin' AND status = 'active')
  xs,        \* the newcomer's members.status: "none" (not asked yet) | "pending" | "active" | "revoked"
  pub, prop, dflt,  \* Ben's filter: lenses.published, proposed_at IS NOT NULL, estate_default
  sub,       \* Cleo's subscriptions row for it: "none" | "live" | "snapshot" | "out" (opted_out, of an estate default)
  qs,        \* each question: "none" (not asked) | "queued" | "dealt" (an asked row, waiting on Cleo) | "settled"
             \*   (answered or put away by her: stories.answer, stories.dismiss)
  at,        \* the item each question is dealt in (asked.item_id): a slot, or "-"
  dl,        \* the two stories.deal requests (two tabs, the phone, GET /api/inbox and /api/questions at once)
  waits,     \* some held photo of Cleo's waits to be looked over (private.counts' waiting > 0)
  bnd,       \* Cleo's agent_bindings row: "none" | "pending" | "approved" | "revoked"
  dq,        \* two member_inbox.dismiss requests (two tabs), each between its SELECT and its UPDATE: the item read
             \*   open, and whether it has been closed since (done by its decision, or put away by the other)
  wrong      \* not in the code: a dismiss's UPDATE put away an item that was not open

vars == <<part, it, admin, xs, pub, prop, dflt, sub, qs, at, dl, waits, bnd, dq, wrong>>
others == <<admin, xs, pub, prop, dflt, sub, qs, at, dl, waits, bnd>>

Idle == [op |-> "idle"]
NoDq == [k |-> "-", gone |-> FALSE]

\* What a write goes by: what the function read before its transaction, or (Guarded) what is there now
Now(read, cur) == IF Guarded THEN cur ELSE read

\* member_inbox.close of `ids` (every open one is done), and put of `ids` (one open: made, or brought up to date)
Closed(f, ids) == [i \in Ids |-> IF i \in ids /\ f[i] = "open" THEN "done" ELSE f[i]]
Put(f, ids) == [i \in Ids |-> IF i \in ids THEN "open" ELSE f[i]]
Open(ids) == {i \in ids : it[i] = "open"}
\* lenses.State.on, for Cleo: the filter is on for her (an estate default needs no row; opted_out is out of it)
On == pub /\ IF dflt THEN sub \in {"none", "live"} ELSE sub = "live"

\* The last conjunct of every action but dismiss's own: a dismiss under way learns that the item it read was closed
Track == dq' = [t \in Tabs |-> IF dq[t].k # "-" /\ it[dq[t].k] = "open" /\ it'[dq[t].k] # "open"
                                 THEN [dq[t] EXCEPT !.gone = TRUE] ELSE dq[t]]

Init ==
  /\ part \in Parts
  /\ it = [i \in Ids |-> "none"]
  /\ admin = [a \in Admins |-> TRUE] /\ xs = "none"
  /\ pub = FALSE /\ prop = FALSE /\ dflt = FALSE /\ sub = "none"
  /\ qs = [q \in Q |-> "none"] /\ at = [q \in Q |-> "-"] /\ dl = [d \in {"d1", "d2"} |-> Idle]
  /\ waits = FALSE /\ bnd = "none"
  /\ dq = [t \in Tabs |-> NoDq] /\ wrong = FALSE

--------------------------------------------------------------------------
(* Who is a sysadmin: members.change of Ada's or Dee's role or status (POST /api/members/<id>), refused (ValueError)
   when it would leave the estate with no active sysadmin. Guarded: one who is not an active sysadmin after it has
   none of their member_request and lens_proposed items open (nothing of theirs to decide; deciding answers 403).
   One made a sysadmin is given no items for what waits already (they see it on /members and in Albums). *)
SetAdmin(a, v) ==
  /\ part \in {"request", "lens"} /\ admin[a] # v
  /\ \E b \in Admins : (IF b = a THEN v ELSE admin[b])
  /\ admin' = [admin EXCEPT ![a] = v]
  /\ it' = IF Guarded /\ ~v THEN Closed(it, {a}) ELSE it
  /\ UNCHANGED <<part, xs, pub, prop, dflt, sub, qs, at, dl, waits, bnd, wrong>>
  /\ Track

(* The part "request" *)

\* members.request: a login nobody has let in is a pending member (_add), and member_inbox.put of member_request for
\* every active sysadmin (member_inbox.sysadmins, read after _add's write)
Request ==
  /\ part = "request" /\ xs = "none"
  /\ xs' = "pending"
  /\ it' = Put(it, {a \in Admins : admin[a]})
  /\ UNCHANGED <<part, admin, pub, prop, dflt, sub, qs, at, dl, waits, bnd, wrong>>
  /\ Track

\* members.change of the newcomer's status (any status may follow any other); one who was pending and is not now was
\* let in or turned away: member_inbox.close of member_request, for every sysadmin
ChangeX(s) ==
  /\ part = "request" /\ xs # "none" /\ s # xs
  /\ xs' = s
  /\ it' = IF xs = "pending" THEN Closed(it, Admins) ELSE it
  /\ UNCHANGED <<part, admin, pub, prop, dflt, sub, qs, at, dl, waits, bnd, wrong>>
  /\ Track

(* The part "lens": Ben owns the filter, and is no sysadmin here; Cleo may follow it *)

\* lenses.propose: published, proposed, and member_inbox.put of lens_proposed for every active sysadmin but its owner
Propose ==
  /\ part = "lens" /\ ~prop /\ ~dflt
  /\ prop' = TRUE /\ pub' = TRUE
  /\ it' = Put(it, {a \in Admins : admin[a]})
  /\ UNCHANGED <<part, admin, xs, dflt, sub, qs, at, dl, waits, bnd, wrong>>
  /\ Track

\* lenses.decide(accept) by an active sysadmin (403 for anyone else): no longer proposed (an estate default, if
\* accepted); member_inbox.close of lens_proposed, for every sysadmin
Decide(a, acc) ==
  /\ part = "lens" /\ admin[a] /\ prop
  /\ prop' = FALSE /\ dflt' = (dflt \/ acc)
  /\ it' = Closed(it, Admins)
  /\ UNCHANGED <<part, admin, xs, pub, sub, qs, at, dl, waits, bnd, wrong>>
  /\ Track

\* lenses.publish(on) by Ben (and lenses.publish_album for a series: the same for its lens_changed items). Taking
\* it back takes a proposal with it (member_inbox.close of lens_proposed); an estate default stays published (409).
\* Guarded: taking it back closes every lens_changed item about it (member_inbox.close of its ref, lenses._ref).
Publish(on) ==
  /\ part = "lens" /\ pub # on /\ (dflt => on)
  /\ pub' = on /\ prop' = (on /\ prop)
  /\ it' = IF on THEN it ELSE Closed(it, (IF prop THEN Admins ELSE {}) \cup (IF Guarded THEN {"cleo"} ELSE {}))
  /\ UNCHANGED <<part, admin, xs, dflt, sub, qs, at, dl, waits, bnd, wrong>>
  /\ Track

\* lenses.subscribe(mode) by Cleo, of a published lens (404 for one that is not: _visible). Live to an estate default
\* deletes her row (it needs none); a snapshot instead closes her lens_changed item (member_inbox.close)
Subscribe(mode) ==
  /\ part = "lens" /\ pub
  /\ IF mode = "live"
       THEN /\ IF dflt THEN sub' = "none" ELSE sub # "live" /\ sub' = "live"
            /\ UNCHANGED it
       ELSE /\ sub' = "snapshot" /\ it' = Closed(it, {"cleo"})
  /\ UNCHANGED <<part, admin, xs, pub, prop, dflt, qs, at, dl, waits, bnd, wrong>>
  /\ Track

\* lenses.unsubscribe by Cleo, of a published lens (404 for one that is not): her row ends (an estate default's
\* becomes opted_out); member_inbox.close of her lens_changed item
Unsubscribe ==
  /\ part = "lens" /\ pub /\ (IF dflt THEN sub # "out" ELSE sub # "none")
  /\ sub' = IF dflt THEN "out" ELSE "none"
  /\ it' = Closed(it, {"cleo"})
  /\ UNCHANGED <<part, admin, xs, pub, prop, dflt, qs, at, dl, waits, bnd, wrong>>
  /\ Track

\* member_inbox.lens_changed, from Changes.tell when the filter's hides change (in Changes.write's transaction, which
\* holds the lock): a published lens's live subscribers (here Cleo) have one open lens_changed item (put)
Changed ==
  /\ part = "lens" /\ pub /\ sub = "live"
  /\ it' = Put(it, {"cleo"})
  /\ UNCHANGED <<part, admin, xs, pub, prop, dflt, sub, qs, at, dl, waits, bnd, wrong>>
  /\ Track

(* The part "question" *)

Rows == {q \in Q : qs[q] = "queued"}                \* stories._for: not closed, and not dealt to her yet
OpenQ == IF Open(Slots) = {} THEN "-" ELSE CHOOSE s \in Open(Slots) : TRUE   \* (deal keeps one per ref: the last)
Waiting(s) == {q \in Q : at[q] = s /\ qs[q] = "dealt"}

\* stories.ask: a question to Cleo is filed, queued
Ask(q) ==
  /\ part = "question" /\ qs[q] = "none"
  /\ qs' = [qs EXCEPT ![q] = "queued"]
  /\ UNCHANGED <<part, it, admin, xs, pub, prop, dflt, sub, at, dl, waits, bnd, wrong>>
  /\ Track

\* stories.deal (GET /api/inbox, GET /api/questions): _for, before its transaction (none: nothing is written); then,
\* inside `with con:` and before any write, her open question items and put's own SELECT
DealRead(d) ==
  /\ part = "question" /\ dl[d].op = "idle" /\ Rows # {}
  /\ dl' = [dl EXCEPT ![d] = [op |-> "deal", rows |-> Rows, open |-> OpenQ]]
  /\ UNCHANGED <<part, it, admin, xs, pub, prop, dflt, sub, qs, at, waits, bnd, wrong>>
  /\ Track

\* deal's writes: a group with an open item joins it (_deal: INSERT OR IGNORE of the asked rows, which a question
\* dealt already keeps, and UPDATE of the title); else member_inbox.put finds none and INSERTs one, and _deal deals
\* the questions into it. (Its room is taken to be enough.) Guarded: the lock first, and both read again inside it.
DealWrite(d) ==
  /\ part = "question" /\ dl[d].op = "deal"
  /\ LET rows == Now(dl[d].rows, Rows)
         o == Now(dl[d].open, OpenQ)
         new == CHOOSE s \in Slots : it[s] = "none"
         s == IF o # "-" THEN o ELSE new IN
     /\ (o = "-" => \E x \in Slots : it[x] = "none")         \* (three rows are all this model has)
     /\ IF rows = {}
          THEN UNCHANGED <<it, qs, at>>                      \* (Guarded: nothing left to deal)
          ELSE /\ it' = IF o # "-" THEN it ELSE Put(it, {s})
               /\ at' = [q \in Q |-> IF q \in rows /\ at[q] = "-" THEN s ELSE at[q]]
               /\ qs' = [q \in Q |-> IF q \in rows /\ at[q] = "-" THEN "dealt" ELSE qs[q]]
  /\ dl' = [dl EXCEPT ![d] = Idle]
  /\ UNCHANGED <<part, admin, xs, pub, prop, dflt, sub, waits, bnd, wrong>>
  /\ Track

\* stories.answer (a story, by tell) or stories.dismiss ("I don't know") of a question dealt to her: settled; then
\* _settle of its item: done (UPDATE ... AND state='open') once none of its questions waits
Settle(q) ==
  /\ part = "question" /\ qs[q] = "dealt"
  /\ qs' = [qs EXCEPT ![q] = "settled"]
  /\ it' = IF \E p \in Q \ {q} : at[p] = at[q] /\ qs[p] = "dealt" THEN it ELSE Closed(it, {at[q]})
  /\ UNCHANGED <<part, admin, xs, pub, prop, dflt, sub, at, dl, waits, bnd, wrong>>
  /\ Track

(* The part "held" *)

\* private._hold (a batch checked, or the library run): photos held, waiting; member_inbox.put of the held item with
\* the ref of the one open already (_open_held), else of this batch; then close_held(keep) closes any other
Hold ==
  /\ part = "held"
  /\ LET ref == IF Open(Refs) # {} THEN CHOOSE r \in Open(Refs) : TRUE ELSE CHOOSE r \in Refs : it[r] # "open" IN
     it' = Closed(Put(it, {ref}), Refs \ {ref})
  /\ waits' = TRUE
  /\ UNCHANGED <<part, admin, xs, pub, prop, dflt, sub, qs, at, dl, bnd, wrong>>
  /\ Track

\* private.review, change or release_due, then close_held: some photos looked over or released (w: some still wait).
\* None waits: every open held item is done; else the newest open stays (retitle) and any other is done
Look(w) ==
  /\ part = "held" /\ waits
  /\ waits' = w
  /\ it' = IF ~w \/ Open(Refs) = {} THEN Closed(it, Refs) ELSE Closed(it, Refs \ {CHOOSE r \in Open(Refs) : TRUE})
  /\ UNCHANGED <<part, admin, xs, pub, prop, dflt, sub, qs, at, dl, bnd, wrong>>
  /\ Track

(* The part "binding" *)

\* members.bind, the first call from a device: the binding made pending (INSERT OR IGNORE), and its item (put)
Bind ==
  /\ part = "binding" /\ bnd = "none"
  /\ bnd' = "pending" /\ it' = Put(it, {"cleo"})
  /\ UNCHANGED <<part, admin, xs, pub, prop, dflt, sub, qs, at, dl, waits, wrong>>
  /\ Track

\* members.decide_binding: allowed or turned off (from any status); member_inbox.close of its item either way
DecideBinding(allow) ==
  /\ part = "binding" /\ bnd # "none"
  /\ bnd' = IF allow THEN "approved" ELSE "revoked"
  /\ it' = Closed(it, {"cleo"})
  /\ UNCHANGED <<part, admin, xs, pub, prop, dflt, sub, qs, at, dl, waits, wrong>>
  /\ Track

--------------------------------------------------------------------------
(* Every part: the member whose item it is puts it away (POST /api/inbox/<id>/dismiss) *)

\* member_inbox.dismiss: its SELECT of the item (one not open is answered 409, and nothing is written)
DismissRead(t, k) ==
  /\ dq[t].k = "-" /\ it[k] = "open"
  /\ dq' = [dq EXCEPT ![t] = [k |-> k, gone |-> FALSE]]
  /\ UNCHANGED <<part, it, admin, xs, pub, prop, dflt, sub, qs, at, dl, waits, bnd, wrong>>

\* its UPDATE ... SET state='dismissed' WHERE id=? and inbox.dismissed in the journal: of the row it read, whatever
\* that row is now. Guarded: read with the write lock held, an item closed since is answered 409; and a question item
\* takes its questions with it (stories.put_away: each that waits in it is put away, settled; specs/Question.tla)
DismissWrite(t) ==
  /\ dq[t].k # "-"
  /\ IF dq[t].gone
       THEN /\ wrong' = (wrong \/ ~Guarded) /\ UNCHANGED <<it, qs>>   \* (a row closed already: a new open one is untouched)
       ELSE /\ it' = [it EXCEPT ![dq[t].k] = "dismissed"] /\ UNCHANGED wrong
            /\ qs' = IF Guarded /\ dq[t].k \in Slots THEN [q \in Q |-> IF q \in Waiting(dq[t].k) THEN "settled" ELSE qs[q]]
                     ELSE qs
  /\ dq' = [u \in Tabs |-> IF u = t THEN NoDq
                           ELSE IF dq[u].k = dq[t].k /\ ~dq[t].gone THEN [dq[u] EXCEPT !.gone = TRUE] ELSE dq[u]]
  /\ UNCHANGED <<part, admin, xs, pub, prop, dflt, sub, at, dl, waits, bnd>>

Next ==
  \/ \E a \in Admins, v \in BOOLEAN : SetAdmin(a, v)
  \/ Request \/ \E s \in {"pending", "active", "revoked"} : ChangeX(s)
  \/ Propose \/ \E a \in Admins, acc \in BOOLEAN : Decide(a, acc)
  \/ \E on \in BOOLEAN : Publish(on)
  \/ \E mode \in {"live", "snapshot"} : Subscribe(mode)
  \/ Unsubscribe \/ Changed
  \/ \E q \in Q : Ask(q) \/ Settle(q)
  \/ \E d \in {"d1", "d2"} : DealRead(d) \/ DealWrite(d)
  \/ Hold \/ \E w \in BOOLEAN : Look(w)
  \/ Bind \/ \E allow \in BOOLEAN : DecideBinding(allow)
  \/ \E t \in Tabs : DismissWrite(t) \/ \E k \in Ids : DismissRead(t, k)

Spec == Init /\ [][Next]_vars

--------------------------------------------------------------------------
(* What must always hold *)

TypeOK ==
  /\ part \in Parts /\ it \in [Ids -> States] /\ admin \in [Admins -> BOOLEAN]
  /\ xs \in {"none", "pending", "active", "revoked"} /\ pub \in BOOLEAN /\ prop \in BOOLEAN /\ dflt \in BOOLEAN
  /\ sub \in {"none", "live", "snapshot", "out"} /\ qs \in [Q -> {"none", "queued", "dealt", "settled"}]
  /\ at \in [Q -> Slots \cup {"-"}] /\ waits \in BOOLEAN /\ bnd \in {"none", "pending", "approved", "revoked"}
  /\ \A t \in Tabs : dq[t].k \in Ids \cup {"-"} /\ wrong \in BOOLEAN

\* put: "One open item of `kind` about `ref` for each of `members`: made, or, when one is open already, given this
\* title"; the module: "one open item per member, kind and ref". (Only question rows can be two open at once.)
OneOpenItem == Cardinality(Open(Slots)) <= 1

\* question: "done once each question in it is answered or put away"; "Not shown while none of its questions may be
\* shown to them" (stories.keep_item): an open question item has a question dealt in it that waits on her. One with
\* none is shown to nobody and is never done.
QuestionWhileWaiting == \A s \in Slots : it[s] = "open" => Waiting(s) # {}

\* and the other half: a question item is done only once each question in it is answered or put away
DoneWhenAnswered == \A s \in Slots : it[s] = "done" => Waiting(s) = {}

\* member_request and lens_proposed: "every active sysadmin"; sysadmins: "the active sysadmins, whom a request or a
\* proposal is for"; "done (decided, or no longer waiting on anyone)": an open one is held only by an active sysadmin
AskedOfASysadmin == part \in {"request", "lens"} => \A a \in Admins : it[a] = "open" => admin[a]

\* member_request: "closed when that member is let in or turned away"; lens_proposed: "closed when a sysadmin decides
\* (lenses.decide) or the owner takes the filter back"
AskedWhileUndecided ==
  /\ part = "request" => \A a \in Admins : it[a] = "open" => xs = "pending"
  /\ part = "lens" => \A a \in Admins : it[a] = "open" => prop

\* lens_changed: "each live subscriber"; lens_changed(): "If it is published, each member subscribed to it live";
\* "Nothing waits on it: it is closed when they leave the lens". An open one is about a lens they follow now
\* (lenses.py: "every other published filter M subscribes to live", and an estate default they did not opt out of).
ToldWhileFollowing == part = "lens" /\ it["cleo"] = "open" => On

\* binding: "done when they allow it or turn it off (members.decide_binding)"
BindingWhilePending == part = "binding" /\ it["cleo"] = "open" => bnd = "pending"

\* held: "one open item per member, which a later hold brings up to date"
OneHeld == Cardinality(Open(Refs)) <= 1

\* held: "done once none waits (looked over on /held, or released by any way)"
HeldWhileWaiting == Open(Refs) # {} => waits

\* dismiss: "the member puts one of their own open items away ... 409 for one not open"
DismissOnlyOpen == ~wrong

(* For asking narrower questions, as an ACTION_CONSTRAINT in a .cfg of your own. NoRace: nothing happens while a deal
   or a dismiss is between its read and its write but that one's own write, so a violation found with it needs no
   bad timing at all. *)
NoRace == /\ \A d \in {"d1", "d2"} : dl[d].op # "idle" => dl'[d].op = "idle"
          /\ \A t \in Tabs : dq[t].k # "-" => dq'[t].k = "-"

(* Left out of this model, all of it in the code:
   - the kinds review and series_update, and the change set or album they are about: ChangeSet.tla (put, close and
     dismiss of both, with every writer of the set);
   - every thing but one at a time, and kinds about different things: no code makes or decides an item of one kind
     from another kind's thing, and close() never crosses kinds or refs;
   - the item's title, updated_at and the order mine() lists them in; retitle, here part of close_held's keep;
   - a question's own life (the questions and asked tables, deal's weekly allowance and room, the order groups are
     dealt in, the photos one may not be shown, which keep_item hides): specs/Question.tla; here deal's room is enough;
   - the member and the binding themselves (members.py): specs/Member.tla (members.change, members.request and
     decide_binding read with the write lock held now, so a second request from one login is answered with the
     first's member); a member revoked (their items stay as they are: see InboxItem's report); the owner of a lens
     being a sysadmin;
   - estate defaults beyond staying published, and their opted_out rows (Lens.tla);
   - more than two dismisses under way at once;
   - estate.py purge, which deletes a purged photo's `private` rows with the app stopped and closes no held item;
   - SQLite itself: each transaction is atomic, and a write never fails (one that waits for the lock longer than
     the connection's timeout fails, and nothing is written). *)
=============================================================================
