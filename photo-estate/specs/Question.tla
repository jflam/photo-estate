------------------------------ MODULE Question ------------------------------
(* A question (a row of `questions`: to_member one member or NULL for the family, status queued | asked | answered |
   closed), the `asked` rows that deal it to members (item_id, answered_at, dismissed_at: "the waiting is this row"),
   the members' question items in the inbox, and the run of the estate's agent an answer starts, written by
   stories.py (ask, deal and _deal, answer and tell, dismiss, _settle), member_inbox.dismiss (the inbox's own route)
   and listeners.py's stories listener (listeners.stories, with listeners.run moving its cursor).

   One run of the model is of one of two shapes (`part`, chosen at the start; the other's variables stay as they
   start, so the states of the two are added, not multiplied):
     "rows"      two questions asked by the estate's agent: q1 to Cleo alone, q2 to the family (Ada and Cleo), in
                 one group or two (`same`, chosen at the start); each member's question items (two rows each, since
                 an item done or put away is followed by a new one for a later question); an allowance of one item
                 a week (ALLOWANCE, or members.questions_per_week: one here is enough to reach both of deal's
                 limits)
     "followup"  one story answering a question the agent asked with follow_up: typed, spoken, or both (`kind`),
                 its journal entries story.told and story.transcribed, the stories listener's cursor, and the runs
                 it starts

   Who holds the write lock when it reads what it decides by (checked function by function; Python's `with con:`
   begins a transaction only at its first write):
     - stories.deal: BEGIN IMMEDIATE, then _for and the open items again (since specs/InboxItem.tla): one action;
     - stories.answer and stories.dismiss read _asked (item_id, shas, to_member, status) before their transaction.
       None of what they write depends on it but the item, and asked.item_id, questions.shas and to_member are never
       written after the row is made (status is read into `_` and not used): a read that cannot be stale. Their
       transaction (tell's INSERT, dismiss's UPDATE of asked) writes first, then reads for _settle: one action each;
     - member_inbox.dismiss: BEGIN IMMEDIATE before it reads the item: one action;
     - stories.ask reads the recipient and the photos (members.family, refused) before its INSERT: a member revoked
       or a photo made private meanwhile leaves a queued question nobody is dealt (deal's _for and refused), which
       is the same as a member who never reads their inbox; ask is one action here;
     - the stories listener runs in the app's one listener thread: it reads agent_tasks, and tasks.Tasks.submit
       commits the run before it returns; listeners.run then moves the cursor in a transaction of its own, so the app
       can stop between the two (Stop): the entry is handled again when it starts.
   Every write of questions.status is stories._move, a conditional UPDATE from the statuses it names, each of which
   core/states.py's QUESTION_MOVES must allow (the property StatusMoves: the model makes no other move).

   Guarded = TRUE is the code. Guarded = FALSE is the code before it was fixed (November 2026), kept so the
   invariants can be seen to fail without the guard: member_inbox.dismiss of a question item puts each question in
   it that waits on the member away, as stories.dismiss does (asked.dismissed_at, a question to them alone closed,
   question.dismissed), in its transaction (stories.put_away).

   Question.cfg checks the code; Question.before.<invariant>.cfg each find the violation there was.
   tests/test_specs.py runs them all with TLC, and tests/test_question_states.py replays each on the real code. *)
EXTENDS Naturals, FiniteSets

CONSTANT Guarded

Parts == {"rows", "followup"}
M == {"ada", "cleo"}
Q == {"q1", "q2"}
To == [q \in Q |-> IF q = "q1" THEN "cleo" ELSE "family"]   \* questions.to_member (NULL: the family)
Slots == {"i1", "i2"}               \* each member's question items (inbox_items rows of kind question)
A == 1                              \* the member's allowance: items a week, and items open at once
Statuses == {"none", "queued", "asked", "answered", "closed"}   \* "none": not asked yet
ItemStates == {"none", "open", "done", "dismissed"}
Words == {"none", "told", "dontknow"}
NoRow == [in |-> "-", ans |-> FALSE, dis |-> FALSE]

\* core/states.py's QUESTION_MOVES (with "none" -> "queued": the INSERT of ask)
Moves == {<<"none", "queued">>, <<"queued", "asked">>, <<"asked", "answered">>, <<"asked", "closed">>,
          <<"closed", "answered">>}

VARIABLES
  part,      \* which shape this run is
  same,      \* q1 and q2 have one group_key (one item, ask:<key>), or none (each its own: ask:q<id>)
  st,        \* each question's questions.status
  ak,        \* each question's asked row for each member: the item it is dealt in ("-": no row), answered_at and
             \*   dismissed_at set
  it,        \* each member's items: inbox_items.state ("none": never made)
  grp,       \* the group each item is about (its ref, ask:<group>)
  fresh,     \* each item was dealt in the last 7 days (deal's week: min(asked.at) per item_id)
  word,      \* not in the code: each member's last word on each question. "told": a story answering it (an answer
             \*   stands: a later "I don't know" does not undo it); "dontknow": "I don't know", to the question or
             \*   to its whole inbox item
  kind,      \* the story's shape: "text", "recording" (alone) or "both"
  toldE, transE,   \* the journal has story.told, and story.transcribed (transcribe.py wrote the recording out)
  cur,       \* the stories listener's cursor: how many of the two entries it has moved past
  ph,        \* "idle", or "handled": the handler has run on the entry at the cursor, which has not moved yet
  runs,      \* runs of the estate's agent started for the story (agent_tasks rows, started_by .../story:<id>)
  rd         \* whom the asking run acted for may read the story now (stories.story: none of its photos private)

rowv == <<same, st, ak, it, grp, fresh, word>>
fv == <<kind, toldE, transE, cur, ph, runs, rd>>
vars == <<part, rowv, fv>>

G(q) == IF same THEN "g" ELSE q                                   \* stories._group
OpenIn(m, g) == {s \in Slots : it[m][s] = "open" /\ grp[m][s] = g}
Opened(m) == {s \in Slots : it[m][s] = "open"}
Week(m) == {s \in Slots : fresh[m][s]}
Waits(q, m) == ak[q][m].in # "-" /\ ~ak[q][m].ans /\ ~ak[q][m].dis
\* stories._settle of item s, given the rows after the write: done (only if open) once none of its rows waits
Settled(m, s, rows) ==
  IF \E p \in Q : rows[p][m].in = s /\ ~rows[p][m].ans /\ ~rows[p][m].dis
    THEN it ELSE [it EXCEPT ![m][s] = IF @ = "open" THEN "done" ELSE @]

Init ==
  /\ part \in Parts
  /\ same \in (IF part = "rows" THEN BOOLEAN ELSE {FALSE})
  /\ st = [q \in Q |-> "none"] /\ ak = [q \in Q |-> [m \in M |-> NoRow]]
  /\ it = [m \in M |-> [s \in Slots |-> "none"]] /\ grp = [m \in M |-> [s \in Slots |-> "-"]]
  /\ fresh = [m \in M |-> [s \in Slots |-> FALSE]] /\ word = [q \in Q |-> [m \in M |-> "none"]]
  /\ kind \in (IF part = "followup" THEN {"text", "recording", "both"} ELSE {"text"})
  /\ toldE = FALSE /\ transE = FALSE /\ cur = 0 /\ ph = "idle" /\ runs = 0 /\ rd = TRUE

--------------------------------------------------------------------------
(* The part "rows" *)

\* stories.ask (the MCP tool ask_member, by the estate's agent): the question filed, queued
Ask(q) ==
  /\ part = "rows" /\ st[q] = "none"
  /\ st' = [st EXCEPT ![q] = "queued"]
  /\ UNCHANGED <<part, same, ak, it, grp, fresh, word, fv>>

\* stories._for: not closed (answered is not closed: a family question answered by one member is still dealt to the
\* others), for this member or the family, and not dealt to them yet
For(m) == {q \in Q : st[q] \notin {"none", "closed"} /\ To[q] \in {m, "family"} /\ ak[q][m].in = "-"}
Joins(m) == {q \in For(m) : OpenIn(m, G(q)) # {}}
Groups(m) == {G(q) : q \in For(m) \ Joins(m)}
Room(m) == Cardinality(Week(m)) < A /\ Cardinality(Opened(m)) < A
Into(m, q) == CHOOSE s \in OpenIn(m, G(q)) : TRUE

\* stories._deal into the items `place` names: INSERT OR IGNORE of the asked rows, and status asked from queued
DealIn(m, place) ==
  /\ ak' = [q \in Q |-> IF place[q] # "-" THEN [ak[q] EXCEPT ![m].in = place[q]] ELSE ak[q]]
  /\ st' = [q \in Q |-> IF place[q] # "-" /\ st[q] = "queued" THEN "asked" ELSE st[q]]

\* stories.deal (GET /api/inbox, GET /api/questions), with the write lock: a question whose group has an open item
\* joins it, for nothing; then, if the member was dealt fewer than A items in the last 7 days and has fewer than A
\* open, a new item (member_inbox.put) for one of the other groups (which one: deal's ranking, any here)
Deal(m) ==
  /\ part = "rows" /\ For(m) # {}
  /\ LET J == Joins(m) IN
     \/ /\ ~Room(m) \/ Groups(m) = {}
        /\ DealIn(m, [q \in Q |-> IF q \in J THEN Into(m, q) ELSE "-"])
        /\ UNCHANGED <<it, grp, fresh>>
     \/ /\ Room(m) /\ Groups(m) # {} /\ \E x \in Slots : it[m][x] = "none"
        /\ \E g \in Groups(m) :
             LET s == CHOOSE x \in Slots : it[m][x] = "none" IN
             /\ it' = [it EXCEPT ![m][s] = "open"] /\ grp' = [grp EXCEPT ![m][s] = g]
             /\ fresh' = [fresh EXCEPT ![m][s] = TRUE]
             /\ DealIn(m, [q \in Q |-> IF q \in J THEN Into(m, q) ELSE IF q \in For(m) /\ G(q) = g THEN s ELSE "-"])
  /\ UNCHANGED <<part, same, word, fv>>

\* Seven days pass since an item was dealt: it no longer counts against the member's week
Age(m, s) ==
  /\ part = "rows" /\ fresh[m][s]
  /\ fresh' = [fresh EXCEPT ![m][s] = FALSE]
  /\ UNCHANGED <<part, same, st, ak, it, grp, word, fv>>

\* stories.answer (POST /api/questions/<id>/answer), a question dealt to her (404 for one with no asked row; nothing
\* else is asked of it), and tell: a story; answered_at (if not set), status answered, _settle of its item
Answer(m, q) ==
  /\ part = "rows" /\ ak[q][m].in # "-"
  /\ LET rows == [ak EXCEPT ![q][m].ans = TRUE] IN
     /\ ak' = rows
     /\ it' = Settled(m, ak[q][m].in, rows)
  /\ st' = [st EXCEPT ![q] = "answered"]
  /\ word' = [word EXCEPT ![q][m] = "told"]
  /\ UNCHANGED <<part, same, grp, fresh, fv>>

\* stories.dismiss (POST /api/questions/<id>/dismiss), "I don't know": a row neither answered nor put away is put
\* away (dismissed_at), a question to her alone closed unless answered, question.dismissed, _settle of its item;
\* any other row is left as it is ({dismissed: false})
DismissQ(m, q) ==
  /\ part = "rows" /\ ak[q][m].in # "-"
  /\ word' = [word EXCEPT ![q][m] = IF @ = "told" THEN "told" ELSE "dontknow"]
  /\ IF Waits(q, m)
       THEN LET rows == [ak EXCEPT ![q][m].dis = TRUE] IN
            /\ ak' = rows
            /\ st' = [st EXCEPT ![q] = IF To[q] = m /\ @ # "answered" THEN "closed" ELSE @]
            /\ it' = Settled(m, ak[q][m].in, rows)
       ELSE UNCHANGED <<ak, st, it>>
  /\ UNCHANGED <<part, same, grp, fresh, fv>>

\* member_inbox.dismiss (POST /api/inbox/<id>/dismiss, the inbox page's Dismiss on a question's card): with the write
\* lock, an open item of hers is dismissed (409 for one not open). The module docstring: dismissing "its whole inbox
\* item" is "I don't know" to each question in it. Guarded: stories.put_away, in its transaction, puts each row in it
\* that waits on her away as stories.dismiss does (and the item is dismissed, not done)
DismissItem(m, s) ==
  /\ part = "rows" /\ it[m][s] = "open"
  /\ LET W == {q \in Q : ak[q][m].in = s /\ Waits(q, m)} IN
     /\ it' = [it EXCEPT ![m][s] = "dismissed"]
     /\ word' = [q \in Q |-> IF q \in W THEN [word[q] EXCEPT ![m] = "dontknow"] ELSE word[q]]
     /\ IF Guarded
          THEN /\ ak' = [q \in Q |-> IF q \in W THEN [ak[q] EXCEPT ![m].dis = TRUE] ELSE ak[q]]
               /\ st' = [q \in Q |-> IF q \in W /\ To[q] = m /\ st[q] # "answered" THEN "closed" ELSE st[q]]
          ELSE UNCHANGED <<ak, st>>
  /\ UNCHANGED <<part, same, grp, fresh, fv>>

--------------------------------------------------------------------------
(* The part "followup": a story answering a question the agent asked with follow_up *)

\* stories.tell (from answer): the story, and story.told {text: whether it has text}
Tell ==
  /\ part = "followup" /\ ~toldE
  /\ toldE' = TRUE
  /\ UNCHANGED <<part, rowv, kind, transE, cur, ph, runs, rd>>

\* stories.transcribed (transcribe.py), for a story with a recording: the transcript, once (the trigger), and
\* story.transcribed
Transcribe ==
  /\ part = "followup" /\ toldE /\ kind # "text" /\ ~transE
  /\ transE' = TRUE
  /\ UNCHANGED <<part, rowv, kind, toldE, cur, ph, runs, rd>>

\* A photo of the story made private to someone else, or released: whom the run acted for may read it, or not
Readable(v) ==
  /\ part = "followup" /\ rd # v
  /\ rd' = v
  /\ UNCHANGED <<part, rowv, kind, toldE, transE, cur, ph, runs>>

Entries == (IF toldE THEN 1 ELSE 0) + (IF transE THEN 1 ELSE 0)

\* listeners.stories on the entry at the cursor (story.told first, then story.transcribed): story.told of a recording
\* alone is passed over (woken when it is written out); a run is started (tasks.submit, committed before it returns)
\* if the reader may read the story and no run has started_by ending in /story:<id>
Handle ==
  /\ part = "followup" /\ ph = "idle" /\ cur < Entries
  /\ LET e == IF cur = 0 THEN "told" ELSE "transcribed"
         wakes == (e = "told" => kind # "recording") /\ rd /\ runs = 0 IN
     runs' = IF wakes THEN runs + 1 ELSE runs
  /\ ph' = "handled"
  /\ UNCHANGED <<part, rowv, kind, toldE, transE, cur, rd>>

\* listeners.run: the cursor moves past the entry handled (its own transaction)
Move ==
  /\ ph = "handled"
  /\ cur' = cur + 1 /\ ph' = "idle"
  /\ UNCHANGED <<part, rowv, kind, toldE, transE, runs, rd>>

\* The app stops after the handler and before the cursor moved: the entry is handled again when it starts
Stop ==
  /\ ph = "handled"
  /\ ph' = "idle"
  /\ UNCHANGED <<part, rowv, kind, toldE, transE, cur, runs, rd>>

Next ==
  \/ \E q \in Q : Ask(q)
  \/ \E m \in M : Deal(m) \/ \E s \in Slots : Age(m, s) \/ DismissItem(m, s)
  \/ \E m \in M, q \in Q : Answer(m, q) \/ DismissQ(m, q)
  \/ Tell \/ Transcribe \/ \E v \in BOOLEAN : Readable(v)
  \/ Handle \/ Move \/ Stop

Spec == Init /\ [][Next]_vars

--------------------------------------------------------------------------
(* What must always hold *)

TypeOK ==
  /\ part \in Parts /\ same \in BOOLEAN /\ st \in [Q -> Statuses]
  /\ ak \in [Q -> [M -> [in: Slots \cup {"-"}, ans: BOOLEAN, dis: BOOLEAN]]]
  /\ it \in [M -> [Slots -> ItemStates]] /\ fresh \in [M -> [Slots -> BOOLEAN]] /\ word \in [Q -> [M -> Words]]
  /\ kind \in {"text", "recording", "both"} /\ cur \in 0..2 /\ ph \in {"idle", "handled"} /\ runs \in 0..2

\* The schema: "a question dealt to a member's inbox (stories.deal): the waiting is this row"; member_inbox's question:
\* "done once each question in it is answered or put away"; stories._open shows a dealt question only in an open
\* item, and _for never deals it again. A question dealt to a member that still waits on them is in an open item.
WaitsInAnOpenItem == \A q \in Q, m \in M : Waits(q, m) => it[m][ak[q][m].in] = "open"

\* stories.py: "Saying "I don't know" (dismissing a question, or its whole inbox item) puts it away for good for that
\* member": their row is put away (dismissed_at), or answered (an answer stands)
PutAwayForGood == \A q \in Q, m \in M : word[q][m] = "dontknow" => ak[q][m].ans \/ ak[q][m].dis

\* stories.dismiss: "Put away for this member for good (a question to them alone is closed)"
ClosedWhenPutAway == \A q \in Q : To[q] # "family" /\ word[q][To[q]] = "dontknow" => st[q] = "closed"

\* tell: "answering closes their part of the question"; answer: "A second answer, theirs or anyone's, is another
\* story": a member who told a story answering a question has their row answered, and the question says answered
AnsweredAsTold == \A q \in Q, m \in M : word[q][m] = "told" => ak[q][m].ans /\ st[q] = "answered"

\* The schema's "queued | asked | answered | closed" and deal's "A question is `queued` until deal puts it in a
\* member's inbox" (the rest is this model's reading): asked is dealt and answered by nobody yet; answered, someone
\* answered it; closed, a question to one member that they put away and did not answer
StatusAsItsRows ==
  \A q \in Q :
    LET R == {m \in M : ak[q][m].in # "-"} IN
    /\ st[q] \in {"none", "queued"} => R = {}
    /\ st[q] = "asked" => R # {} /\ \A m \in R : ~ak[q][m].ans
    /\ st[q] = "answered" => \E m \in R : ak[q][m].ans
    /\ st[q] = "closed" => To[q] # "family" /\ ak[q][To[q]].dis /\ ~ak[q][To[q]].ans

\* member_inbox.put: "one open item per member, kind and ref" (InboxItem.tla's OneOpenItem)
OneOpenItem == \A m \in M : \A g \in {"g"} \cup Q : Cardinality(OpenIn(m, g)) <= 1

\* member_inbox's question item: an open one has a question waiting in it (InboxItem.tla's QuestionWhileWaiting)
OpenWhileWaiting == \A m \in M, s \in Slots : it[m][s] = "open" => \E q \in Q : ak[q][m].in = s /\ Waits(q, m)

\* stories.py: "a member is dealt at most ALLOWANCE new inbox items in any 7 days ... and never while ALLOWANCE of
\* them are open"
WithinAllowance == \A m \in M : Cardinality(Week(m)) <= A /\ Cardinality(Opened(m)) <= A

\* listeners.py: "a new run ... is started ... (started_by journal:<entry>/story:<id>; once per story)"
OneRunPerStory == runs <= 1

\* core/states.py's QUESTION_MOVES: every change of questions.status is one of its moves (a PROPERTY)
StatusMoves == [][\A q \in Q : st'[q] # st[q] => <<st[q], st'[q]>> \in Moves]_st

(* For asking narrower questions, as an ACTION_CONSTRAINT in a .cfg of your own. No writer here reads what it
   decides by before it holds the lock (the header), so nothing here needs bad timing; NoStop: the app never stops
   between the stories listener's handler and its cursor's move. *)
NoStop == ~(ph = "handled" /\ ph' = "idle" /\ cur' = cur)

(* Left out of this model, all of it in the code:
   - the photos a member may not be shown (refused): deal deals such a question to nobody, waiting() and keep_item
     leave it and its item out, answer refuses it (404). For questions about one photo that only does less (a member
     who cannot see it acts as one who does not read their inbox), so it reaches no state this model does not; but a
     hidden item still counts against the room of deal (open, and in the week), and dismiss (both) still work on it;
   - members revoked or made again (members.change): _for deals nothing to one not active, and they make no request;
     what is dealt to them waits. As above, that is a member who does nothing;
   - more than one photo, the question's text, why and priority, the item's title, and the order deal ranks groups
     in (any group here, as if any order were possible);
   - a member's own allowance changed (members.questions_per_week), and more than one item a week;
   - a family question asked by a member (deal does not deal it to its asker), and a member asking themself;
   - a story told with no question (POST /api/stories: no question's row moves), and the recording's bytes;
   - the agent being unavailable (tasks.submit answers 409: no run, and the cursor moves on: none, by design),
     a run that fails, and a follow_up question asked by a member's own agent (refused by estate_mcp.ask_member);
   - stories.ask's reads before its INSERT (the header), and answer's refused check before tell's transaction (a
     photo made private in that gap: specs/PrivatePhoto.tla's machine);
   - the inbox items of every other kind, and dismiss's races with their deciders: specs/InboxItem.tla;
   - SQLite itself: each transaction is atomic, and a write never fails. *)
=============================================================================
