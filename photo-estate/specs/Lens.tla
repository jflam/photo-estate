-------------------------------- MODULE Lens --------------------------------
(* One filter lens of Ben's (a row of `lenses`: published, proposed_at, estate_default), Cleo's subscription
   to it (her row of `subscriptions`, or none), and one album of Ben's with the series rows of `lenses` that
   name it (lenses.py). Ben is a member and owns both; Ada and Dee are the sysadmins; Cleo is a member.

   Every write here is a request of the app's, and requests run side by side, each on a connection of its
   own. Python's `with con:` begins the transaction only at the first write, so each function, as it was,
   read the row (get, _theirs, _visible, is_sysadmin, owner_of, and its own SELECTs) with no lock, decided,
   and then wrote: two actions here, a read and a write, with anything else free to happen in between.
   subscribe and unsubscribe read Cleo's row after the lens, and are three (the lens, her row, the write). A
   slot is one request at a time: Ben has two (two tabs, the phone, the API), Cleo two, each sysadmin one.

   Each action names the function it models. What is left out is listed at the end.

   Guarded = TRUE is the code. Guarded = FALSE is the code before it was fixed (November 2026), kept so the
   invariants can be seen to fail without the guard: each of these functions now takes the write lock first
   (lenses._begin, BEGIN IMMEDIATE, as private._begin) and makes every read and every check inside that
   transaction, so what it decides on is what it writes over. Here that is each write going by what is there
   when it writes (Now), and refusing, or doing nothing, exactly as the function does when it runs then; its
   read actions still happen, and only stand for the request having arrived. (Later, from InboxItem.tla: publish
   taking the lens back closes its lens_changed items.)

   Lens.cfg checks the code; Lens.before.<invariant>.cfg each find the violation there was. tests/test_specs.py
   runs them all with TLC, and tests/test_lens_states.py replays each violation on the real code. *)
EXTENDS Naturals

CONSTANT Guarded

Bens == {"b1", "b2"}
Admins == {"ada", "dee"}
Cleos == {"c1", "c2"}
Slots == Bens \cup Admins \cup Cleos

VARIABLES
  pub,        \* Ben's filter: lenses.published
  prop,       \*   lenses.proposed_at IS NOT NULL
  dflt,       \*   lenses.estate_default
  asked,      \* an open lens_proposed item about it (member_inbox.put to every sysadmin but Ben, closed for all at once)
  sub,        \* Cleo's subscriptions row for it: "none" (no row) | "live" | "snapshot" | "opted_out"
  told,       \* Cleo has an open lens_changed item about it
  srows,      \* Ben's album: how many series rows of `lenses` name it (set_id)
  spub,       \*   each one's published (1 is the first, the one publish_album reads)
  rq,         \* each request: what it is doing, and what it read
  offered,    \* not in the code: Ben's proposal stands (he proposed it, and neither took it back nor was it declined)
  word,       \* not in the code: Cleo's last word on the lens: "none" | "live" | "snapshot" | "out"
  albumWord,  \* not in the code: the album's owner's last word on it: "none" | "on" | "off"
  empty       \* not in the code: a write appended its journal entry and changed nothing

lens == <<pub, prop, dflt, asked>>
cleo == <<sub, told, word>>
album == <<srows, spub, albumWord>>
vars == <<pub, prop, dflt, asked, sub, told, srows, spub, rq, offered, word, albumWord, empty>>

Idle == [op |-> "idle"]
Busy(s) == rq[s].op # "idle"
Done(s) == rq' = [rq EXCEPT ![s] = Idle]
Start(s, r) == ~Busy(s) /\ rq' = [rq EXCEPT ![s] = r]

\* What a write goes by: what the function read before its transaction, or (Guarded) what is there now
Now(read, cur) == IF Guarded THEN cur ELSE read

\* State.on, for Cleo (not the owner): the filter is on for her
On == IF dflt /\ pub THEN sub \in {"none", "live"} ELSE pub /\ sub = "live"

Init ==
  /\ pub = FALSE /\ prop = FALSE /\ dflt = FALSE /\ asked = FALSE
  /\ sub = "none" /\ told = FALSE
  \* the album may start with its series row: changes.py's create, for an album an agent makes (lenses.add, then
  \* UPDATE lenses SET maintained_by, in the transaction that makes the album, with nothing read first)
  /\ srows \in {0, 1} /\ spub = [k \in 1..2 |-> k <= srows]
  /\ rq = [s \in Slots |-> Idle]
  /\ offered = FALSE /\ word = "none" /\ albumWord = (IF srows = 1 THEN "on" ELSE "none") /\ empty = FALSE

--------------------------------------------------------------------------
(* Ben, the owner: publish, propose, publish_album *)

\* publish(on): _theirs (get, owner_of) reads the lens; an estate default is not taken back (409); one that is
\* already as asked is left (changed: false). Neither writes, so neither is here.
PublishRead(b, on) ==
  /\ ~(dflt /\ ~on) /\ pub # on
  /\ Start(b, [op |-> "publish", on |-> on, P |-> pub, Q |-> prop, D |-> dflt])
  /\ UNCHANGED <<lens, cleo, album, offered, empty>>

\* publish's transaction: UPDATE lenses SET published, proposed_at (kept only when publishing); taken back while
\* it was proposed (as it read), member_inbox.close of the lens_proposed item. Guarded: taken back, its lens_changed
\* items are closed too (member_inbox.close: InboxItem.tla's ToldWhileFollowing)
PublishWrite(b) ==
  /\ rq[b].op = "publish"
  /\ LET on == rq[b].on
         d == Now(rq[b].D, dflt)
         p == Now(rq[b].P, pub)
         q == Now(rq[b].Q, prop) IN
     IF (d /\ ~on) \/ p = on
       THEN UNCHANGED <<pub, prop, asked, offered, empty, told>>   \* (Guarded: refused, or nothing to do now)
       ELSE /\ pub' = on
            /\ prop' = (on /\ prop)
            /\ asked' = IF ~on /\ q THEN FALSE ELSE asked
            /\ offered' = (on /\ offered)
            /\ empty' = (empty \/ (pub = on /\ prop' = prop))
            /\ told' = IF ~on /\ Guarded THEN FALSE ELSE told
  /\ Done(b)
  /\ UNCHANGED <<dflt, sub, word, album>>

\* propose: _theirs reads the lens; only a filter that is not an estate default (409); one proposed already is
\* left (changed: false)
ProposeRead(b) ==
  /\ ~dflt /\ ~prop
  /\ Start(b, [op |-> "propose", Q |-> prop, D |-> dflt])
  /\ UNCHANGED <<lens, cleo, album, offered, empty>>

\* propose's transaction: UPDATE lenses SET published=1, proposed_at; member_inbox.put of a lens_proposed item
ProposeWrite(b) ==
  /\ rq[b].op = "propose"
  /\ IF Now(rq[b].D, dflt) \/ Now(rq[b].Q, prop)
       THEN UNCHANGED <<pub, prop, asked, offered, empty>>         \* (Guarded: refused, or proposed already)
       ELSE /\ pub' = TRUE /\ prop' = TRUE /\ asked' = TRUE /\ offered' = TRUE
            /\ empty' = (empty \/ (pub /\ prop))
  /\ Done(b)
  /\ UNCHANGED <<dflt, cleo, album>>

\* publish_album(on): reads the album (Ben's, op 'view', not discarded) and its series row (the first, if any:
\* `got`); already as asked, nothing (changed: false)
AlbumAsIs(on, got) == IF got THEN spub[1] = on ELSE ~on
AlbumRead(b, on) ==
  /\ ~AlbumAsIs(on, srows > 0)
  /\ Start(b, [op |-> "album", on |-> on, got |-> (srows > 0)])
  /\ UNCHANGED <<lens, cleo, album, offered, empty>>

\* publish_album's transaction: UPDATE lenses SET published, title of the row it read; none read, lenses.add of
\* a new series row, published
AlbumWrite(b) ==
  /\ rq[b].op = "album"
  /\ LET on == rq[b].on
         got == Now(rq[b].got, srows > 0) IN
     /\ IF Guarded /\ AlbumAsIs(on, got)
          THEN UNCHANGED <<srows, spub, empty>>                    \* (Guarded: nothing to do now)
          ELSE IF got
            THEN /\ spub' = [spub EXCEPT ![1] = on] /\ UNCHANGED srows
                 /\ empty' = (empty \/ spub[1] = on)
            ELSE /\ srows' = srows + 1 /\ spub' = [spub EXCEPT ![srows + 1] = TRUE]
                 /\ UNCHANGED empty
     /\ albumWord' = IF on THEN "on" ELSE "off"
  /\ Done(b)
  /\ UNCHANGED <<lens, cleo, offered>>

--------------------------------------------------------------------------
(* A sysadmin: decide *)

\* decide(accept): is_sysadmin, then get reads the lens; one that is not proposed is refused (409)
DecideRead(a, acc) ==
  /\ prop
  /\ Start(a, [op |-> "decide", on |-> acc, Q |-> prop])
  /\ UNCHANGED <<lens, cleo, album, offered, empty>>

\* decide's transaction: accepted, UPDATE lenses SET estate_default=1, published=1, proposed_at=NULL; declined,
\* SET proposed_at=NULL; either way member_inbox.close of the lens_proposed item
DecideWrite(a) ==
  /\ rq[a].op = "decide"
  /\ IF ~Now(rq[a].Q, prop)
       THEN UNCHANGED <<pub, prop, dflt, asked, offered, empty>>  \* (Guarded: not proposed now, refused)
       ELSE LET acc == rq[a].on IN
            /\ dflt' = (dflt \/ acc) /\ pub' = (pub \/ acc) /\ prop' = FALSE /\ asked' = FALSE
            /\ offered' = (offered /\ acc)
            /\ empty' = (empty \/ (dflt' = dflt /\ pub' = pub /\ ~prop))
  /\ Done(a)
  /\ UNCHANGED <<cleo, album>>

--------------------------------------------------------------------------
(* Cleo: subscribe, unsubscribe; and the change set telling her the lens changed *)

\* Nothing to do: live, to an estate default with no row, or to another lens with a live row (subscribe);
\* out of it already (unsubscribe)
SubAsIs(d, w) == IF d THEN w = "none" ELSE w = "live"
UnsAsIs(dp, w) == IF dp THEN w = "opted_out" ELSE w = "none"

\* subscribe(mode): _visible (get, owner_of) reads the lens: it is not Cleo's, so only a published one (404)
SubRead(c, mode) ==
  /\ pub
  /\ Start(c, [op |-> "subscribe", mode |-> mode, stage |-> "lens", P |-> pub, D |-> dflt, was |-> "?"])
  /\ UNCHANGED <<lens, cleo, album, offered, empty>>

\* subscribe, live: its SELECT of Cleo's row, in `with con:` but before anything is written, so with no lock;
\* nothing to do, it returns (changed: false)
SubWas(c) ==
  /\ rq[c].op = "subscribe" /\ rq[c].mode = "live" /\ rq[c].stage = "lens"
  /\ IF ~Guarded /\ SubAsIs(rq[c].D, sub)
       THEN Done(c) /\ word' = "live"
       ELSE rq' = [rq EXCEPT ![c].stage = "was", ![c].was = sub] /\ UNCHANGED word
  /\ UNCHANGED <<lens, sub, told, album, offered, empty>>

\* subscribe's transaction. A snapshot first copies the lens's hides into a filter of Cleo's own (lenses.add,
\* a hiding set), which begins the transaction, so its SELECT of her row comes after, with the lock held. Then
\* INSERT OR REPLACE her row; or, live to an estate default, DELETE it (no row is following it). A snapshot
\* closes her lens_changed item (member_inbox.close).
SubWrite(c) ==
  /\ rq[c].op = "subscribe" /\ (rq[c].mode = "snapshot" \/ rq[c].stage = "was")
  /\ LET live == rq[c].mode = "live"
         d == Now(rq[c].D, dflt)
         w == IF Guarded \/ ~live THEN sub ELSE rq[c].was IN
     CASE Guarded /\ ~pub -> UNCHANGED <<sub, told, word, empty>>              \* (Guarded: 404, not published now)
       [] live /\ SubAsIs(d, w) -> UNCHANGED <<sub, told, empty>> /\ word' = "live"   \* (Guarded: so already)
       [] OTHER -> /\ sub' = IF ~live THEN "snapshot" ELSE IF d THEN "none" ELSE "live"
                   /\ told' = (live /\ told)
                   /\ word' = rq[c].mode
                   /\ empty' = (empty \/ (live /\ sub' = sub))
  /\ Done(c)
  /\ UNCHANGED <<lens, album, offered>>

\* unsubscribe: _visible reads the lens (404 unless published)
UnsRead(c) ==
  /\ pub
  /\ Start(c, [op |-> "unsubscribe", stage |-> "lens", P |-> pub, D |-> dflt, was |-> "?"])
  /\ UNCHANGED <<lens, cleo, album, offered, empty>>

\* unsubscribe: its SELECT of Cleo's row, before `with con:`; out already, it returns (changed: false)
UnsWas(c) ==
  /\ rq[c].op = "unsubscribe" /\ rq[c].stage = "lens"
  /\ IF ~Guarded /\ UnsAsIs(rq[c].D /\ rq[c].P, sub)
       THEN Done(c) /\ word' = "out"
       ELSE rq' = [rq EXCEPT ![c].stage = "was", ![c].was = sub] /\ UNCHANGED word
  /\ UNCHANGED <<lens, sub, told, album, offered, empty>>

\* unsubscribe's transaction: a published estate default is opted out of (INSERT OR REPLACE 'opted_out'); any
\* other row is deleted; member_inbox.close of her lens_changed item
UnsWrite(c) ==
  /\ rq[c].op = "unsubscribe" /\ rq[c].stage = "was"
  /\ LET dp == Now(rq[c].D /\ rq[c].P, dflt /\ pub)
         w == Now(rq[c].was, sub) IN
     CASE Guarded /\ ~pub -> UNCHANGED <<sub, told, word, empty>>              \* (Guarded: 404, not published now)
       [] UnsAsIs(dp, w) -> UNCHANGED <<sub, told, empty>> /\ word' = "out"     \* (Guarded: out already)
       [] OTHER -> /\ sub' = IF dp THEN "opted_out" ELSE "none"
                   /\ told' = FALSE
                   /\ word' = "out"
                   /\ empty' = (empty \/ sub' = sub)
  /\ Done(c)
  /\ UNCHANGED <<lens, album, offered>>

\* member_inbox.lens_changed, from changes.py when the lens's hides change (in its own transaction, which has
\* written already, so with the lock held): Cleo, subscribed live to it while it is published, is told
Changed ==
  /\ pub /\ sub = "live" /\ ~told
  /\ told' = TRUE
  /\ UNCHANGED <<lens, sub, word, album, rq, offered, empty>>

Next ==
  \/ \E b \in Bens : \/ \E on \in BOOLEAN : PublishRead(b, on) \/ AlbumRead(b, on)
                     \/ ProposeRead(b)
                     \/ PublishWrite(b) \/ ProposeWrite(b) \/ AlbumWrite(b)
  \/ \E a \in Admins : (\E acc \in BOOLEAN : DecideRead(a, acc)) \/ DecideWrite(a)
  \/ \E c \in Cleos : \/ \E mode \in {"live", "snapshot"} : SubRead(c, mode)
                      \/ UnsRead(c)
                      \/ SubWas(c) \/ SubWrite(c) \/ UnsWas(c) \/ UnsWrite(c)
  \/ Changed

Spec == Init /\ [][Next]_vars

--------------------------------------------------------------------------
(* What must always hold *)

TypeOK ==
  /\ pub \in BOOLEAN /\ prop \in BOOLEAN /\ dflt \in BOOLEAN /\ asked \in BOOLEAN
  /\ sub \in {"none", "live", "snapshot", "opted_out"} /\ told \in BOOLEAN
  /\ srows \in 0..2 /\ spub \in [1..2 -> BOOLEAN]
  /\ \A s \in Slots : rq[s].op \in {"idle", "publish", "propose", "album", "decide", "subscribe", "unsubscribe"}
  /\ offered \in BOOLEAN /\ word \in {"none", "live", "snapshot", "out"}
  /\ albumWord \in {"none", "on", "off"} /\ empty \in BOOLEAN

\* "an estate default stays published" (publish's 409); "An estate default is a published filter" (lenses.py)
DefaultStaysPublished == dflt => pub

\* "any member may propose a published filter of theirs as one, which a sysadmin accepts or declines" (lenses.py);
\* "A sysadmin accepts a proposed filter as an estate default" (decide): only a proposal that still stands
DefaultAsProposed == dflt => offered

\* "it is published, and waits for a sysadmin" (propose); taking it back takes the proposal with it (publish)
ProposedIsPublished == prop => pub

\* "only a filter that is not an estate default yet can be proposed" (propose); accepting it ends the proposal
DefaultNotProposed == dflt => ~prop

\* "proposing puts a lens_proposed item in every other sysadmin's inbox, and deciding it (or taking the filter
\* back) closes it" (lenses.py): an open item about it while, and only while, there is something to decide
AskedWhileProposed == asked <=> prop

\* "A write that changes nothing appends nothing." (lenses.py)
NoEmptyEntry == ~empty

\* subscriptions.mode "opted_out (an estate default)"; MODES: "'opted_out' is an estate default left"
OptedOutOfADefault == sub = "opted_out" => dflt

\* "every other published filter M subscribes to live" is on for M; subscribe also takes an estate default back
FollowsAsAsked == (word = "live" /\ pub) => On

\* unsubscribe: "a live or snapshot subscription ends"; a snapshot's "later changes to the lens do not follow"
LeftAsAsked == (word \in {"out", "snapshot"} /\ ~dflt) => ~On

\* "leaving a lens (unsubscribing, or a snapshot instead) closes the member's lens_changed item about it"
LeftNotTold == word \in {"out", "snapshot"} => ~told

\* publish_album: "Publish an album of the member's as a series (its lenses row, made the first time)"
OneSeriesRow == srows <= 1

\* and "or take it back": every series row of the album is as its owner last said
SeriesAsAsked == albumWord # "none" => \A k \in 1..srows : spub[k] = (albumWord = "on")

(* For asking narrower questions, as an ACTION_CONSTRAINT or a CONSTRAINT in a .cfg of your own.
   NoRace: nothing happens while a request is between its read and its write but that request's own next
   step, so a violation found with it needs no bad timing at all. OneEach: no member, Ben or Cleo, has two
   requests in flight at once (what is left is a race between people). OneAdmin: Dee never acts. NoAlbum:
   the album is left as it is (a question about the filter alone). *)
NoRace == \A s \in Slots : Busy(s) => rq'[s] # rq[s]
OneEach == ~(Busy("b1") /\ Busy("b2")) /\ ~(Busy("c1") /\ Busy("c2"))
OneAdmin == ~Busy("dee")
NoAlbum == \A b \in Bens : rq[b].op # "album"
(* Left out of this model, all of it in the code:
   - every other lens: the Estate defaults lens and a member's own "Hidden photos" (lenses.mine, estate_default:
     read-then-insert, in changes.py's transaction under its lock), and lenses.create, which writes a new row only;
   - what a filter hides (its hiding sets, changes.py), and so what a snapshot copies, and `shown`;
   - Ben's own subscription to his filter once it is an estate default, and Cleo's to the series; subscribe and
     unsubscribe are the same code for both;
   - publish() on the series row: refused for every series since issue #126 (a series is published and taken back
     from its album, publish_album); the album being discarded or put up for hiding, which takes its series back
     (lenses.series_taken_back, the UPDATE publish_album(off) makes, in Changes.write's transaction: issue #124):
     ChangeSet.tla, the change set's machine; and lenses.maintained_by after the row is made;
   - the inbox beyond the two items: one lens_proposed item stands for every sysadmin's, put and closed together;
     put() reading before it inserts; a member dismissing an item (member_inbox.dismiss); the item's title;
   - who is a sysadmin changing while a decision is being made (members.py), and a member revoked;
   - the journal beyond whether an entry was appended for a write that changed nothing;
   - the app's in-memory copy (lenses.State, PhotoApp.relook) and when it catches up;
   - SQLite itself: each transaction is taken as atomic, and a write as never failing (a write that waits for
     the lock longer than the connection's timeout fails; the request answers with an error, and nothing is
     written). *)
=============================================================================
