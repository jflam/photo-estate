---------------------------- MODULE PrivatePhoto ----------------------------
(* One photo and one of its sharers: the row of the `private` table that says the photo is private to
   them (private.py), and everything that writes it. The sharer acts through the app (POST /api/private,
   POST /api/held/review); the app acts by itself in three places, each of which reads the table, works
   for a while, and then writes: the ingest step (`private.py check`, a process of its own), the one-off
   run over the library (`private.py library`, likewise) and the release timer (a thread of the app).
   Between such a read and its write, anything else here may happen.

   Each action names the function it models. What is left out is listed at the end.

   Guarded = TRUE is the code. Guarded = FALSE is the code before it was fixed (November 2026), kept so
   the invariants can be seen to fail without the guards: each of the app's own writes now takes the write
   lock and reads again what it is about to write over (private._begin), and a sharer who makes a `new`
   photo private, or sets its release day, withdraws it (private.change).

   PrivatePhoto.cfg checks the code; PrivatePhoto.before.<invariant>.cfg each find the violation there was.
   tests/test_specs.py runs them all with TLC, and tests/test_private_states.py replays each violation on
   the real code. *)
EXTENDS Naturals

CONSTANT Guarded

VARIABLES
  sharer,     \* the member has a `sharers` row for the photo
  exists,     \* they have a `private` row for it
  why,        \* private.why: "new" | "held" | "withdrawn"
  released,   \* private.released_at IS NOT NULL
  reviewed,   \* private.reviewed_at IS NOT NULL
  until,      \* private.until: 0 for NULL, else the day (1 or 2) it is released on by itself
  today,      \* the day it is: 0, 1, 2
  chk,        \* `private.py check`: "idle", or what it decided from what it read: "hold" | "letgo"
  lib,        \* `private.py library`: "idle", or "hold"
  tmr,        \* the release timer: "idle", or "due" (it read the row as due)
  wish,       \* not in the code: the sharer's last word on the photo: "none" | "private" | "released"
  wantDay     \* not in the code: with wish "private", the day they last set (0: none)

vars == <<sharer, exists, why, released, reviewed, until, today, chk, lib, tmr, wish, wantDay>>

Open == exists /\ ~released          \* the photo is private to this sharer now

Init ==
  /\ sharer \in BOOLEAN              \* TRUE: a photo already in the library (an import); FALSE: not sent yet
  /\ exists = FALSE /\ why = "new" /\ released = FALSE /\ reviewed = FALSE /\ until = 0
  /\ today = 0
  /\ chk = "idle" /\ lib = "idle" /\ tmr = "idle"
  /\ wish = "none" /\ wantDay = 0

--------------------------------------------------------------------------
(* The app, at ingestion *)

\* estate.py's ingest, private.record_sharer: a phone upload is a `new` row; anything else is only shared
Ingest ==
  /\ ~sharer
  /\ sharer' = TRUE
  /\ \/ /\ exists' = TRUE /\ why' = "new" /\ released' = FALSE /\ reviewed' = FALSE /\ until' = 0
     \/ UNCHANGED <<exists, why, released, reviewed, until>>
  /\ UNCHANGED <<today, chk, lib, tmr, wish, wantDay>>

\* private.check, its SELECT (why='new' AND released_at IS NULL) and judge(): held or let go
CheckRead ==
  /\ chk = "idle" /\ exists /\ why = "new" /\ ~released
  /\ chk' \in {"hold", "letgo"}
  /\ UNCHANGED <<sharer, exists, why, released, reviewed, until, today, lib, tmr, wish, wantDay>>

\* private._hold's upsert: ON CONFLICT DO UPDATE SET why='held', until=NULL, released_at=NULL, reviewed_at=NULL
Hold ==
  /\ exists' = TRUE /\ why' = "held" /\ released' = FALSE /\ reviewed' = FALSE /\ until' = 0

\* private.check, its transaction: of the rows still `new` and open, DELETE what it lets go, _hold the rest
CheckWrite ==
  /\ chk # "idle"
  /\ chk' = "idle"
  /\ IF chk = "letgo"
       THEN IF exists /\ why = "new" /\ (~Guarded \/ ~released)
              THEN exists' = FALSE /\ released' = FALSE /\ reviewed' = FALSE /\ until' = 0 /\ UNCHANGED why
              ELSE UNCHANGED <<exists, why, released, reviewed, until>>
       ELSE IF ~Guarded \/ (exists /\ why = "new" /\ ~released)
              THEN Hold
              ELSE UNCHANGED <<exists, why, released, reviewed, until>>
  /\ UNCHANGED <<sharer, today, lib, tmr, wish, wantDay>>

\* private.library, its SELECT (a photo they shared with NO private row of theirs) and judge(): held
LibRead ==
  /\ lib = "idle" /\ sharer /\ ~exists
  /\ lib' = "hold"
  /\ UNCHANGED <<sharer, exists, why, released, reviewed, until, today, chk, tmr, wish, wantDay>>

\* private.library, its transaction: _hold
LibWrite ==
  /\ lib = "hold"
  /\ lib' = "idle"
  /\ IF ~Guarded \/ ~exists THEN Hold ELSE UNCHANGED <<exists, why, released, reviewed, until>>
  /\ UNCHANGED <<sharer, today, chk, tmr, wish, wantDay>>

--------------------------------------------------------------------------
(* The release timer (PhotoApp.release_timer, private.release_due) and the days going by *)

Tick == today < 2 /\ today' = today + 1
        /\ UNCHANGED <<sharer, exists, why, released, reviewed, until, chk, lib, tmr, wish, wantDay>>

\* release_due's SELECT: released_at IS NULL AND until IS NOT NULL AND until <= today
TimerRead ==
  /\ tmr = "idle" /\ Open /\ until # 0 /\ until <= today
  /\ tmr' = "due"
  /\ UNCHANGED <<sharer, exists, why, released, reviewed, until, today, chk, lib, wish, wantDay>>

\* release_due's transaction: what is due, read again with the write lock held, is released
TimerWrite ==
  /\ tmr = "due"
  /\ tmr' = "idle"
  /\ IF Open /\ (~Guarded \/ (until # 0 /\ until <= today))
       THEN released' = TRUE ELSE UNCHANGED released
  /\ UNCHANGED <<sharer, exists, why, reviewed, until, today, chk, lib, wish, wantDay>>

--------------------------------------------------------------------------
(* The sharer's own word *)

\* private.change(private=True, until=u). u = 3 stands for KEEP (the request names no day).
\* A held or withdrawn row that is open only has its day moved; any other (a `new` one too) becomes `withdrawn`.
Withdraw(u) ==
  /\ sharer
  /\ LET day == IF u = 3 THEN (IF Open THEN until ELSE 0) ELSE u IN
     /\ IF Open /\ ~(Guarded /\ why = "new")
          THEN UNCHANGED <<exists, why, released>>
          ELSE exists' = TRUE /\ why' = "withdrawn" /\ released' = FALSE
     /\ until' = day
     /\ wish' = "private" /\ wantDay' = day
  /\ UNCHANGED <<sharer, reviewed, today, chk, lib, tmr>>

\* private.change(private=False): an open row is released
Release ==
  /\ Open
  /\ released' = TRUE
  /\ wish' = "released" /\ wantDay' = 0
  /\ UNCHANGED <<sharer, exists, why, reviewed, until, today, chk, lib, tmr>>

\* private.review(release=[sha]): a held photo that waits to be looked over is released
ReviewRelease ==
  /\ Open /\ why = "held" /\ ~reviewed
  /\ released' = TRUE /\ reviewed' = TRUE
  /\ wish' = "released" /\ wantDay' = 0
  /\ UNCHANGED <<sharer, exists, why, until, today, chk, lib, tmr>>

\* private.review(keep=[sha]): it stays private, and leaves the review
ReviewKeep ==
  /\ Open /\ why = "held" /\ ~reviewed
  /\ reviewed' = TRUE
  /\ wish' = "private" /\ wantDay' = until
  /\ UNCHANGED <<sharer, exists, why, released, until, today, chk, lib, tmr>>

Next ==
  \/ Ingest \/ CheckRead \/ CheckWrite \/ LibRead \/ LibWrite
  \/ Tick \/ TimerRead \/ TimerWrite
  \/ \E u \in 0..3 : Withdraw(u)
  \/ Release \/ ReviewRelease \/ ReviewKeep

Spec == Init /\ [][Next]_vars

--------------------------------------------------------------------------
(* What must always hold *)

TypeOK ==
  /\ sharer \in BOOLEAN /\ exists \in BOOLEAN /\ released \in BOOLEAN /\ reviewed \in BOOLEAN
  /\ why \in {"new", "held", "withdrawn"}
  /\ until \in 0..2 /\ today \in 0..2 /\ wantDay \in 0..2
  /\ chk \in {"idle", "hold", "letgo"} /\ lib \in {"idle", "hold"} /\ tmr \in {"idle", "due"}
  /\ wish \in {"none", "private", "released"}
  /\ exists => sharer

\* "The row stays when it is released, so a photo its sharer let go is never held again" (private.py)
ReleasedStays == wish = "released" => exists /\ released

\* A photo its sharer said is private is private, until the day they set (if they set one) has come
PrivateAsAsked == (wish = "private" /\ (wantDay = 0 \/ today < wantDay)) => Open

\* While it is private at their word, the day it is released on is the day they last set
DayKept == (wish = "private" /\ Open) => until = wantDay

(* For asking narrower questions, as an ACTION_CONSTRAINT or a CONSTRAINT in a .cfg of your own.
   NoRace: nothing happens between a read of the app's own and its write, so a violation found with it
   needs no bad timing at all. OnlyTimer: the ingest check and the library run never start. *)
NoRace == (chk # "idle" \/ lib # "idle" \/ tmr # "idle") => (chk' # chk \/ lib' # lib \/ tmr' # tmr)
OnlyTimer == chk = "idle" /\ lib = "idle"

(* Left out of this model, all of it in the code:
   - a second sharer of the same photo (rule 1: who is shown a photo two members sent), and the first
     sysadmin standing in for a photo with no sharer row;
   - the journal entries and the `held` inbox item written beside each change (close_held);
   - the app's in-memory copy of this table (lenses.State.private, PhotoApp.relook) and when it catches up;
   - a photo with no vector yet, which check() leaves for its next run;
   - `estate.py purge`, tombstones, and a member being revoked;
   - SQLite itself: each transaction is taken as atomic, and a write as never failing. *)
=============================================================================
