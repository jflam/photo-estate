----------------------------- MODULE PhoneGate -----------------------------
(* The phone gate of the iPhone app (ios/Shared/PhoneGate.swift): a member whose upload policy is `review` or
   `review_flagged` looks over new photos on the phone before any is sent, and a photo they mark is kept back for
   good, as a tombstone (the SHA-256 of its file) told to the server. "A photo sent is for good."

   THE MACHINE (step 1 of specs/README.md's loop)

   The state is GateState, one JSON file in the app group (GateStore: each change a locked read-change-write).
   A photo, as the gate sees it, is in one of these places (Gate.known is the union of the first five):
     candidate      the extension saw it arrive and made no job                          (candidates)
     in a batch     in an Inbox item made on the phone, marked (kept back) or not         (batches, marked)
     approved       to be sent, by the app at once or by the extension's next run          (approved)
     shared         sent, or made a job: never offered again                               (shared)
     kept back      opted out, with the hashes of its files                                (optedOut)
     (nowhere)      not known: offered with its year's chunk of older photos, or new to the extension
   and its hashes may be in three lists: serverTombstones (as GET /api/tombstones last said), unsentAdd and
   unsentLift (what the server has not been told yet). Outside the file: the extension's own `pending` and change
   token (its UserDefaults), the upload jobs iOS holds, and the server's tombstones and the files it has.

   The moves, and who makes each:
     the extension (UploadExtension.run; a process iOS starts and may end at any point)
       new -> candidate (a review policy, or none known yet); new -> job (everything); approved -> job.
       Gate.route in one locked write, then its `pending` in its UserDefaults, the token saved, the jobs made,
       `pending` saved: four writes, and nothing holds them together.
     the app (GateModel, @MainActor: at each `await` another of its functions may run)
       readMe: the policy (Gate.me; under everything the candidates are approved)
       gather: candidates or a chunk -> a batch, or approved without asking (Gate.gather)
       toggle: a mark in a batch (Gate.toggle)
       apply: batch -> approved (unmarked) and kept back (marked, hashes -> unsentAdd) (Gate.apply)
       shareAfterAll: kept back -> approved, its hashes -> unsentLift, or forgotten if unsent (Gate.shareAfterAll)
       tellServer: POST /api/tombstones with the unsent lists, then Gate.told; refresh: GET /api/tombstones
     the in-app uploader (ForegroundUploader): approved -> shared (Gate.sent) or kept back (Gate.keptBack), the
       hash checked first on the phone (Gate.refused) and again by the server; under everything, "Upload recent
       photos" sends every photo of the last days
     the system uploader: a job's bytes reach the server, which drops them if a tombstone refuses them
     the server (private.change_tombstones, refused_upload; Admin.upload), and the member's other phone, which adds
       tombstones of its own to the same rows

   What the code assumes cannot happen (each is a rule at the end): a photo approved and in no list that will send
   it; a photo kept back whose hash the server does not refuse; a hash in both unsent lists (the server answers 400
   to that, every time); a photo shared after all that is kept back again; bytes kept back that reach the server.

   THE MODEL

   Two photos, which may have the same bytes (`dup`: a duplicate in Photos, one hash); one file each. Each action
   names the function it models. A function that reads, awaits and then writes is two actions or more.

   Guarded = TRUE is the code. Guarded = FALSE is the code before it was fixed (November 2026), kept so the
   invariants can be seen to fail without the fixes:
     - the extension's photos to make jobs for are GateState.pending, written in the same locked write in which
       Gate.route takes them, and Gate.queued leaves there what got no job; before, they were in its own
       UserDefaults, written after the token, and an extension ended in between lost them (ExtRoute, ExtQueue);
     - Gate.apply leaves in the batch a marked photo with no hash (its original could not be read, or it was
       marked while the others were hashed), instead of keeping it back with no hash ever told (ApplyBody);
     - "share after all" lifts every hash of the photo that no other photo kept back holds (a hash in unsentAdd
       too: a POST may be carrying it), and the photo waits in GateState.lifting until the server has answered
       the lift (Gate.told then approves it); tellServer's POSTs, and refresh's GET /api/tombstones, run one at a
       time (ServerTurns) (ShareAfterAll, TellRead, TellWrite, GetRead);
     - Gate.keptBack leaves a photo approved while a lift of its hash is on its way; a photo kept back or in a
       batch on this phone (Gate.held, by its identifier) is no upload of the in-app uploader's, nor a job of the
       extension's, under `everything` too (KeptBack, UpOne, ExtRoute, ExtQueue).

   PhoneGate.cfg checks the code; PhoneGate.before.<invariant>.cfg each find the violation there was.
   tests/test_specs.py runs them all with TLC, and ios/Tests/main.swift replays each on the real Swift. The
   invariant at the end that is not in PhoneGate.cfg is broken by the code as it is: a question for the owner. *)
EXTENDS Naturals, FiniteSets

CONSTANTS Guarded, MaxSaid    \* MaxSaid: how many times the member acts, at most (a size, as the two photos are)

P == {"p1", "p2"}
Hs == {"h1", "h2"}
B == {1, 2}                                     \* batch ids
T == {1, 2}                                     \* tellServer calls under way at once
Policies == {"everything", "review", "flagged"} \* flagged: review_flagged
Gated(pol) == pol # "everything"                \* (route's `policy?.gated ?? true`: "none" is nil)

VARIABLES
  dup,        \* the two photos have the same bytes (fixed for a run)
  \* -- gate.json: GateState --
  policy,     \* "none" (nil: not known yet) or one of Policies
  cand,       \* candidates
  batch,      \* [P -> 0..2]: the batch a photo is in, 0 for none
  marked,     \* photos marked (kept back) in their batch
  approved,
  shared,
  kept,       \* DOMAIN optedOut
  keptH,      \* the photos kept back whose optedOut entry has their hash (the rest have [])
  lifting,    \* (Guarded) GateState.lifting: shared after all, waiting for the server's answer to the lift
  srvT,       \* serverTombstones
  uAdd,       \* unsentAdd
  uLift,      \* unsentLift
  gPend,      \* (Guarded) GateState.pending: the extension's photos to make jobs for
  \* -- the extension --
  seen,       \* the photos behind the change token it saved
  defPend,    \* its UserDefaults pendingIdentifiers (the code before)
  ext,        \* "idle" | "routed" | "tokened": where a run has got to
  extMem,     \* the run's `pending`, in memory
  jobs,       \* upload jobs iOS holds, not yet sent
  \* -- the app's functions under way --
  ga,         \* gather, between its read of the policy and its write: [pc, ids]
  ap,         \* apply, hashing the marked photos: [pc, b, todo, hashed]
  tl,         \* [T -> [pc, add, lift]]: tellServer, between its read of the unsent lists and Gate.told
  gt,         \* refresh's GET /api/tombstones, between the server's answer and its write: [pc, val]
  up,         \* the in-app uploader's run: [pc, q, cur]
  \* -- the server --
  tomb,       \* this member's tombstones, not lifted (one row per hash and member: both phones share it)
  got,        \* the hashes whose bytes the server has
  other,      \* the member's other phone opted out of h1
  \* -- not in the code: what the member said --
  wish,       \* [P -> "none" | "keep" | "share"]: their last word on each photo
  byPol,      \* let through with no word of theirs, under `everything` (their standing word)
  recent,     \* in an "Upload recent photos" run they started (under `everything`): sent unless kept back
  after,      \* they said "share after all"
  leak,       \* bytes were received while the member's word on them was keep, and none was share
  unasked,    \* a photo was received that the member never said to share, nor their policy
  said        \* how many times the member has acted (Me, a gather, a toggle, an apply, ...): at most MaxSaid

gateVars == <<policy, cand, batch, marked, approved, shared, kept, keptH, lifting, srvT, uAdd, uLift, gPend>>
extVars == <<seen, defPend, ext, extMem, jobs>>
appVars == <<ga, ap, tl, gt, up>>
srvVars == <<tomb, got, other>>
ghostVars == <<wish, byPol, recent, after, leak, unasked, said>>
vars == <<dup, gateVars, extVars, appVars, srvVars, ghostVars>>

H(p) == IF dup \/ p = "p1" THEN "h1" ELSE "h2"
HashesOf(S) == {H(p) : p \in S}
InBatch == {p \in P : batch[p] # 0}
Known == cand \cup InBatch \cup approved \cup shared \cup kept \cup lifting \cup gPend   \* Gate.known
Held == kept \cup InBatch                        \* (Guarded) Gate.held: by identifier, kept back or in a batch

\* Gate.refused(sha, s): unsentLift first; then the server's, the unsent, and every optedOut entry's
Refused(h) == h \notin uLift /\ (h \in srvT \/ h \in uAdd \/ h \in HashesOf(keptH))

\* What the member said, for the rules
Cleared(p) == wish[p] = "share" \/ (p \in byPol /\ wish[p] # "keep")
Asked(p) == Cleared(p) \/ (p \in recent /\ wish[p] # "keep")
KeepWord(h) == (\E p \in P : H(p) = h /\ wish[p] = "keep") \/ (h = "h1" /\ other)
ShareWord(h) == \E p \in P : H(p) = h /\ Asked(p)
Conflict(h) == KeepWord(h) /\ ShareWord(h)      \* two words about the same bytes: no rule says which wins

\* The server stores the bytes of p: the two ghosts note whether it should have
Received(p) ==
  /\ got' = got \cup {H(p)}
  /\ leak' = (leak \/ (KeepWord(H(p)) /\ ~ShareWord(H(p))))
  /\ unasked' = (unasked \/ ~Asked(p))

GaIdle == [pc |-> "idle", ids |-> {}]
ApIdle == [pc |-> "idle", b |-> 0, todo |-> {}, hashed |-> {}]
TlIdle == [pc |-> "idle", add |-> {}, lift |-> {}]
GtIdle == [pc |-> "idle", val |-> {}]
UpIdle == [pc |-> "idle", q |-> {}]

Init ==
  /\ dup \in BOOLEAN
  /\ policy = "none" /\ cand = {} /\ batch = [p \in P |-> 0] /\ marked = {}
  /\ approved = {} /\ shared = {} /\ kept = {} /\ keptH = {} /\ lifting = {}
  /\ srvT = {} /\ uAdd = {} /\ uLift = {} /\ gPend = {}
  /\ seen = {} /\ defPend = {} /\ ext = "idle" /\ extMem = {} /\ jobs = {}
  /\ ga = GaIdle /\ ap = ApIdle /\ tl = [t \in T |-> TlIdle] /\ gt = GtIdle /\ up = UpIdle
  /\ tomb = {} /\ got = {} /\ other = FALSE
  /\ wish = [p \in P |-> "none"] /\ byPol = {} /\ recent = {} /\ after = {} /\ leak = FALSE /\ unasked = FALSE
  /\ said = 0

Say == said < MaxSaid /\ said' = said + 1

--------------------------------------------------------------------------
(* The app: who I am, batches, marks *)

\* GateModel.readMe, Gate.me: the policy /api/me says (as a sysadmin set it on /members). Under everything the
\* candidates are approved.
Me(pol) ==
  /\ pol # policy /\ (policy = "none" \/ pol = "everything")    \* (a policy moved back from everything: left out)
  /\ policy' = pol
  /\ IF pol = "everything"
       THEN /\ approved' = approved \cup cand /\ cand' = {}
            /\ byPol' = byPol \cup {p \in cand : wish[p] = "none"}
       ELSE UNCHANGED <<approved, cand, byPol>>
  /\ UNCHANGED <<dup, batch, marked, shared, kept, keptH, lifting, srvT, uAdd, uLift, gPend>>
  /\ UNCHANGED <<extVars, appVars, srvVars, wish, recent, after, leak, unasked>>
  /\ Say

\* Gate.gather(ids, flagged): under review_flagged only the flagged photos are asked about (and every photo is,
\* the first time, before the line about sharing has been shown). Every photo of this model is one Vision flags,
\* for its text, and one Flags.quick does not (no screenshot): under review_flagged all are asked about whether or
\* not the line was shown, so GateState.told is left out; with the flags of a gather that did not run Vision
\* (`vision` FALSE), none would be.
GatherBody(ids, vision) ==
  LET ids2 == ids \ (Known \ cand)
      ask == CASE policy = "everything" -> {}
               [] policy = "flagged" -> (IF vision THEN ids2 ELSE {})
               [] OTHER -> ids2
      nb == CHOOSE b \in B : \A p \in P : batch[p] # b
  IN /\ cand' = cand \ ids2
     /\ approved' = approved \cup (ids2 \ ask)
     /\ byPol' = byPol \cup (IF policy = "everything" THEN {p \in ids2 : wish[p] = "none"} ELSE {})
     /\ batch' = IF ask = {} THEN batch ELSE [p \in P |-> IF p \in ask THEN nb ELSE batch[p]]

\* GateModel.gather, from refresh (the candidates) or share(chunk) (photos the gate had no place for when the chunks
\* were made), only while gated. Under review its flags are Flags.quick and there is no await before Gate.gather:
\* one step.
GatherNow(ids) ==
  /\ ga.pc = "idle" /\ policy = "review"
  /\ ids # {} /\ (ids = cand \/ ids \cap Known = {})
  /\ GatherBody(ids, FALSE)
  /\ UNCHANGED <<dup, marked, shared, kept, keptH, lifting, srvT, uAdd, uLift, gPend, policy>>
  /\ UNCHANGED <<extVars, appVars, srvVars, wish, recent, after, leak, unasked>>
  /\ Say

\* GateModel.gather under review_flagged: `let policy = state.policy`, then `await Flags.why` for each photo
GatherRead(ids) ==
  /\ ga.pc = "idle" /\ policy = "flagged"
  /\ ids # {} /\ (ids = cand \/ ids \cap Known = {})
  /\ ga' = [pc |-> "flagging", ids |-> ids]
  /\ UNCHANGED <<dup, gateVars, extVars, ap, tl, gt, up, srvVars, wish, byPol, recent, after, leak, unasked>>
  /\ Say

\* ... and its GateStore.update: Gate.gather with the flags Vision gave, under the file's policy as it is now
GatherWrite ==
  /\ ga.pc = "flagging"
  /\ GatherBody(ga.ids, TRUE)
  /\ ga' = GaIdle
  /\ UNCHANGED <<dup, marked, shared, kept, keptH, lifting, srvT, uAdd, uLift, gPend, policy>>
  /\ UNCHANGED <<extVars, ap, tl, gt, up, srvVars, wish, recent, after, leak, unasked, said>>

\* GateModel.toggle, Gate.toggle: a tap on a photo of a batch. A mark is the member's word to keep it back; a mark
\* taken off while its batch is being applied is their word to share it.
Toggle(p) ==
  /\ batch[p] # 0
  /\ marked' = IF p \in marked THEN marked \ {p} ELSE marked \cup {p}
  /\ wish' = [wish EXCEPT ![p] = IF p \notin marked THEN "keep"
                                 ELSE IF ap.pc # "idle" /\ ap.b = batch[p] THEN "share" ELSE "none"]
  /\ UNCHANGED <<dup, policy, cand, batch, approved, shared, kept, keptH, lifting, srvT, uAdd, uLift, gPend>>
  /\ UNCHANGED <<extVars, appVars, srvVars, byPol, recent, after, leak, unasked>>
  /\ Say

\* Gate.apply(batch, hashes): the unmarked approved, the marked kept back with the hashes worked out for them
\* (`hashed`), which wait in unsentAdd unless serverTombstones has them. Guarded: a marked photo with no hash stays
\* in the batch; and every hash waits in unsentAdd (serverTombstones may be a lift behind, or an old answer).
ApplyBody(b, hashed) ==
  LET inB == {p \in P : batch[p] = b}
      keep == inB \cap marked
      stay == IF Guarded THEN keep \ hashed ELSE {}
      done == inB \ stay
      keptNow == keep \ stay
      hs == HashesOf(keptNow \cap hashed)
  IN /\ batch' = [p \in P |-> IF p \in done THEN 0 ELSE batch[p]]
     /\ marked' = marked \ done
     /\ kept' = kept \cup keptNow
     /\ keptH' = (keptH \ keptNow) \cup (keptNow \cap hashed)
     /\ uLift' = uLift \ hs
     /\ uAdd' = uAdd \cup (IF Guarded THEN hs ELSE hs \ srvT)
     /\ approved' = approved \cup (inB \ marked)

\* GateModel.apply: the member presses "Share 1, keep 1 back" with these marks (the batch value the view holds).
\* With nothing marked there is no await before Gate.apply: one step.
ApplyRead(b) ==
  /\ ap.pc = "idle" /\ \E p \in P : batch[p] = b
  /\ LET inB == {p \in P : batch[p] = b} IN
     /\ wish' = [p \in P |-> IF p \in inB THEN (IF p \in marked THEN "keep" ELSE "share") ELSE wish[p]]
     /\ IF inB \cap marked = {}
          THEN ApplyBody(b, {}) /\ UNCHANGED ap
          ELSE /\ ap' = [pc |-> "hashing", b |-> b, todo |-> inB \cap marked, hashed |-> {}]
               /\ UNCHANGED <<batch, marked, kept, keptH, uLift, uAdd, approved>>
  /\ UNCHANGED <<dup, policy, cand, shared, lifting, srvT, gPend>>
  /\ UNCHANGED <<extVars, ga, tl, gt, up, srvVars, byPol, recent, after, leak, unasked>>
  /\ Say

\* GateModel.hashes(of:), awaited for each marked photo: [] when its original cannot be read (in iCloud, offline)
Hash(p, ok) ==
  /\ ap.pc = "hashing" /\ p \in ap.todo
  /\ ap' = [ap EXCEPT !.todo = ap.todo \ {p}, !.hashed = IF ok THEN ap.hashed \cup {p} ELSE ap.hashed]
  /\ UNCHANGED <<dup, gateVars, extVars, ga, tl, gt, up, srvVars, ghostVars>>

\* GateModel.apply's GateStore.update: Gate.apply with the stored batch's marks, as they are now
ApplyWrite ==
  /\ ap.pc = "hashing" /\ ap.todo = {}
  /\ ApplyBody(ap.b, ap.hashed)
  /\ ap' = ApIdle
  /\ UNCHANGED <<dup, policy, cand, shared, lifting, srvT, gPend>>
  /\ UNCHANGED <<extVars, ga, tl, gt, up, srvVars, ghostVars>>

\* GateModel.shareAfterAll, Gate.shareAfterAll: on Kept back, "Share after all". Before: a hash still in unsentAdd
\* is forgotten, as never heard of, and the photo approved at once. Guarded: its hashes are lifted, every one (one
\* in unsentAdd too), and so are the other photos kept back with the same bytes (its copies: kept back, they would
\* refuse it); they wait in `lifting` for the server's answer.
ShareAfterAll(p) ==
  /\ p \in kept
  /\ LET hs == IF p \in keptH THEN {H(p)} ELSE {}
         copies == IF Guarded THEN {p} \cup {q \in keptH : H(q) \in hs} ELSE {p}
         lifts == IF Guarded THEN hs ELSE hs \ uAdd
     IN /\ kept' = kept \ copies /\ keptH' = keptH \ copies
        /\ uAdd' = uAdd \ hs
        /\ uLift' = uLift \cup lifts
        /\ srvT' = srvT \ hs
        /\ shared' = shared \ copies
        /\ IF Guarded /\ lifts # {}
             THEN lifting' = lifting \cup copies /\ UNCHANGED approved
             ELSE approved' = approved \cup copies /\ UNCHANGED lifting
        /\ wish' = [q \in P |-> IF q \in copies THEN "share" ELSE wish[q]]   \* (the bytes are what is shared)
  /\ after' = after \cup {p}
  /\ UNCHANGED <<dup, policy, cand, batch, marked, gPend>>
  /\ UNCHANGED <<extVars, appVars, srvVars, byPol, recent, leak, unasked>>
  /\ Say

--------------------------------------------------------------------------
(* The app and the server: tombstones *)

\* GateModel.tellServer: GateStore.read() of the unsent lists (it returns if both are empty), then `await` the POST.
\* Called by refresh, apply and shareAfterAll, each of which may start one while another waits. Guarded: ServerTurns
\* runs them one at a time, and refresh's GET in turn with them (a call while one runs leaves it to go round again:
\* ServerTurns.begin says no, and ServerTurns.end yes, to the one under way, which starts its next ServerTurns.round).
Alone == (\A u \in T : tl[u].pc = "idle") /\ gt.pc = "idle"
TellRead(t) ==
  /\ tl[t].pc = "idle" /\ (uAdd # {} \/ uLift # {})
  /\ Guarded => Alone
  /\ tl' = [tl EXCEPT ![t] = [pc |-> "read", add |-> uAdd, lift |-> uLift]]
  /\ UNCHANGED <<dup, gateVars, extVars, ga, ap, gt, up, srvVars, ghostVars>>

\* private.change_tombstones (POST /api/tombstones); or a 400 for a hash in both lists, or no network (both: the
\* lists are kept for next time). Two POSTs under way may reach the server in either order.
TellPost(t) ==
  /\ tl[t].pc = "read"
  /\ \/ /\ tl[t].add \cap tl[t].lift = {}
        /\ tomb' = (tomb \cup tl[t].add) \ tl[t].lift
        /\ tl' = [tl EXCEPT ![t].pc = "posted"]
     \/ tl' = [tl EXCEPT ![t] = TlIdle] /\ UNCHANGED tomb
  /\ UNCHANGED <<dup, gateVars, extVars, ga, ap, gt, up, got, other, ghostVars>>

\* GateModel.tellServer's GateStore.update: Gate.told(added, lifted) with the lists it read. Guarded: a photo shared
\* after all is approved once none of its hashes waits to be lifted.
TellWrite(t) ==
  /\ tl[t].pc = "posted"
  /\ uAdd' = uAdd \ tl[t].add
  /\ uLift' = uLift \ tl[t].lift
  /\ srvT' = (srvT \cup tl[t].add) \ tl[t].lift
  /\ LET free == IF Guarded THEN {p \in lifting : H(p) \notin uLift'} ELSE {} IN
     /\ lifting' = lifting \ free
     /\ approved' = approved \cup free
  /\ tl' = [tl EXCEPT ![t] = TlIdle]
  /\ UNCHANGED <<dup, policy, cand, batch, marked, shared, kept, keptH, gPend>>
  /\ UNCHANGED <<extVars, ga, ap, gt, up, srvVars, ghostVars>>

\* GateModel.refresh, gated: GET /api/tombstones (private.tombstones), the server answering. Guarded: in turn with
\* the POSTs (ServerTurns)
GetRead ==
  /\ gt.pc = "idle" /\ policy \in {"review", "flagged"}
  /\ Guarded => Alone
  /\ gt' = [pc |-> "answered", val |-> tomb]
  /\ UNCHANGED <<dup, gateVars, extVars, ga, ap, tl, up, srvVars, ghostVars>>

\* ... and, when its `await` resumes, serverTombstones = Set(the answer)
GetWrite ==
  /\ gt.pc = "answered"
  /\ srvT' = gt.val
  /\ gt' = GtIdle
  /\ UNCHANGED <<dup, policy, cand, batch, marked, approved, shared, kept, keptH, lifting, uAdd, uLift, gPend>>
  /\ UNCHANGED <<extVars, ga, ap, tl, up, srvVars, ghostVars>>

\* The member's other phone keeps back a photo with p1's bytes: its POST /api/tombstones
OtherKeep ==
  /\ ~other
  /\ other' = TRUE /\ tomb' = tomb \cup {"h1"}
  /\ UNCHANGED <<dup, gateVars, extVars, appVars, got, wish, byPol, recent, after, leak, unasked>>
  /\ Say

--------------------------------------------------------------------------
(* The in-app uploader (ForegroundUploader) *)

\* ForegroundUploader.send(state.approved) (after refresh, gather, apply, shareAfterAll), or, under everything,
\* ForegroundUploader.start(days): "Upload recent photos" on Uploads, every photo of the last days. One run at a time.
UpStart(isRecent) ==
  /\ up.pc = "idle"
  /\ IF isRecent THEN policy = "everything" ELSE approved # {}
  /\ up' = [pc |-> "run", q |-> IF isRecent THEN P ELSE approved]
  /\ recent' = IF isRecent THEN P ELSE recent
  /\ UNCHANGED <<dup, gateVars, extVars, ga, ap, tl, gt, srvVars, wish, byPol, after, leak, unasked>>
  /\ IF isRecent THEN Say ELSE UNCHANGED said

\* Gate.keptBack(asset, sha): kept back here from now on. Guarded: not while a lift of that hash is on its way.
KeptBack(p) ==
  IF Guarded /\ H(p) \in uLift
    THEN UNCHANGED <<kept, keptH, approved>>
    ELSE kept' = kept \cup {p} /\ keptH' = keptH \cup {p} /\ approved' = approved \ {p}

\* ForegroundUploader.upload, one photo: the file written and hashed, then Gate.refused(sha, GateStore.read()): kept
\* back without a request (Guarded: a photo kept back or in a batch on this phone is skipped first, by its
\* identifier: Gate.held). Otherwise Admin.upload with X-Content-SHA256: refused before the body (Admin.refused,
\* private.refused_upload), stored, or no network; and ForegroundUploader.run after the answer: Gate.keptBack,
\* Gate.sent, or nothing (it stays approved).
\* (One step: what else may happen between the check and the server's answer is left out.)
UpOne(p) ==
  /\ up.pc = "run" /\ p \in up.q
  /\ up' = IF up.q = {p} THEN UpIdle ELSE [up EXCEPT !.q = up.q \ {p}]
  /\ CASE Guarded /\ p \in Held ->
            UNCHANGED <<kept, keptH, approved, shared, got, leak, unasked>>
       [] Refused(H(p)) \/ H(p) \in tomb ->
            KeptBack(p) /\ UNCHANGED <<shared, got, leak, unasked>>
       [] OTHER ->
            \/ /\ Received(p)
               /\ approved' = approved \ {p}
               /\ shared' = IF Gated(policy) THEN shared \cup {p} ELSE shared
               /\ UNCHANGED <<kept, keptH>>
            \/ UNCHANGED <<kept, keptH, approved, shared, got, leak, unasked>>
  /\ UNCHANGED <<dup, policy, cand, batch, marked, lifting, srvT, uAdd, uLift, gPend>>
  /\ UNCHANGED <<extVars, ga, ap, tl, gt, tomb, other, wish, byPol, recent, after, said>>

--------------------------------------------------------------------------
(* The upload extension (UploadExtension.run), a process of its own, and the system uploader *)

\* run: `pending` from its UserDefaults, the changes since its token, and Gate.route in one GateStore.update: the
\* approved are taken (under everything, the new photos too) and marked shared (gated or not known); otherwise new
\* photos are candidates. Guarded: the photos to make jobs for are GateState.pending, in that same write, and a
\* photo held back here (Gate.held: kept back, or in a batch waiting for the member's word) is no job.
ExtRoute ==
  /\ ext = "idle"
  /\ LET ins == P \ seen
         newJobs == IF policy = "everything" THEN ins ELSE {}
         out == IF Guarded THEN (gPend \cup approved \cup newJobs) \ Held ELSE (approved \cup newJobs) \ kept
     IN /\ approved' = {}
        /\ cand' = IF policy # "everything" THEN cand \cup (ins \ Known) ELSE cand
        /\ shared' = IF Gated(policy) THEN shared \cup out ELSE shared
        /\ byPol' = byPol \cup {p \in newJobs \cap out : wish[p] = "none"}
        /\ IF Guarded THEN gPend' = out /\ extMem' = out ELSE extMem' = defPend \cup out /\ UNCHANGED gPend
  /\ ext' = "routed"
  /\ UNCHANGED <<dup, policy, batch, marked, kept, keptH, lifting, srvT, uAdd, uLift>>
  /\ UNCHANGED <<seen, defPend, jobs, appVars, srvVars, wish, recent, after, leak, unasked, said>>

\* save(token: last): the new photos are behind it now
ExtToken ==
  /\ ext = "routed"
  /\ seen' = P /\ ext' = "tokened"
  /\ UNCHANGED <<dup, gateVars, defPend, extMem, jobs, appVars, srvVars, ghostVars>>

\* GateStore.read().optedOut taken out, UploadExtension.queue (a job per photo, until one fails: the job limit), then
\* defaults.set(pending, ...) with what is left. Guarded: Gate.queued, in a GateStore.update. (Ended between the
\* jobs and that write, the next run makes the same jobs again: the server answers 200 to bytes it has.)
ExtQueue ==
  /\ ext = "tokened"
  /\ LET mem == extMem \ (IF Guarded THEN Held ELSE kept) IN
     \E made \in SUBSET mem :
       /\ jobs' = jobs \cup made
       /\ IF Guarded THEN gPend' = mem \ made /\ UNCHANGED defPend ELSE defPend' = mem \ made /\ UNCHANGED gPend
  /\ ext' = "idle" /\ extMem' = {}
  /\ UNCHANGED <<dup, policy, cand, batch, marked, approved, shared, kept, keptH, lifting, srvT, uAdd, uLift>>
  /\ UNCHANGED <<seen, appVars, srvVars, ghostVars>>

\* iOS ends the extension (it may, at any point: its time is up, or memory)
ExtEnded ==
  /\ ext # "idle"
  /\ ext' = "idle" /\ extMem' = {}
  /\ UNCHANGED <<dup, gateVars, seen, defPend, jobs, appVars, srvVars, ghostVars>>

\* The system sends a job: Admin.upload with no hash, so the bytes are read, hashed and dropped if a tombstone
\* refuses them. The job is done either way (a 200).
JobSend(p) ==
  /\ p \in jobs
  /\ jobs' = jobs \ {p}
  /\ IF H(p) \in tomb THEN UNCHANGED <<got, leak, unasked>> ELSE Received(p)
  /\ UNCHANGED <<dup, gateVars, seen, defPend, ext, extMem, appVars, tomb, other, wish, byPol, recent, after, said>>

\* iOS ends the app: every function of GateModel's under way, and the uploader's run, stop where they are
AppEnded ==
  /\ \/ ga.pc # "idle" \/ ap.pc # "idle" \/ gt.pc # "idle" \/ up.pc # "idle" \/ \E t \in T : tl[t].pc # "idle"
  /\ ga' = GaIdle /\ ap' = ApIdle /\ tl' = [t \in T |-> TlIdle] /\ gt' = GtIdle /\ up' = UpIdle
  /\ UNCHANGED <<dup, gateVars, extVars, srvVars, ghostVars>>

Next ==
  \/ \E pol \in Policies : Me(pol)
  \/ \E ids \in SUBSET P : GatherNow(ids) \/ GatherRead(ids)
  \/ GatherWrite
  \/ \E p \in P : Toggle(p) \/ ShareAfterAll(p) \/ UpOne(p) \/ JobSend(p)
  \/ \E b \in B : ApplyRead(b)
  \/ \E p \in P, ok \in BOOLEAN : Hash(p, ok)
  \/ ApplyWrite
  \/ \E t \in T : TellRead(t) \/ TellPost(t) \/ TellWrite(t)
  \/ GetRead \/ GetWrite \/ OtherKeep
  \/ \E r \in BOOLEAN : UpStart(r)
  \/ ExtRoute \/ ExtToken \/ ExtQueue \/ ExtEnded
  \/ AppEnded

Spec == Init /\ [][Next]_vars

--------------------------------------------------------------------------
(* What must always hold *)

TypeOK ==
  /\ dup \in BOOLEAN /\ policy \in {"none"} \cup Policies
  /\ batch \in [P -> {0} \cup B] /\ marked \subseteq InBatch
  /\ \A S \in {cand, approved, shared, kept, keptH, lifting, gPend, seen, defPend, extMem, jobs, byPol, recent, after} :
       S \subseteq P
  /\ keptH \subseteq kept
  /\ \A S \in {srvT, uAdd, uLift, tomb, got} : S \subseteq Hs
  /\ ext \in {"idle", "routed", "tokened"} /\ other \in BOOLEAN
  /\ wish \in [P -> {"none", "keep", "share"}]
  /\ leak \in BOOLEAN /\ unasked \in BOOLEAN /\ said \in 0..MaxSaid

\* "What you keep back stays on this iPhone and is never sent" (InboxView): no file is received while the member's
\* word on its bytes is to keep them back, here or on their other phone, and none is to share them
KeptBackNeverSent == ~leak

\* "Nothing is sent until the member has looked" (chapter 24): a photo is received only if they said share, or
\* their policy is everything
SentOnlyAsSaid == ~unasked

\* "What it could not send stays approved, and the extension makes jobs for it on its next run" (PhoneGate.swift):
\* a photo the member approved (or their policy let through) is, until the server has its bytes, in a list that
\* will send it: approved, waiting for a lift, the extension's pending (in its run's memory while it runs), a job;
\* or still to look over (a candidate, in a batch), or new to the extension still (its next run finds it again,
\* the token not having moved). Not when its bytes are also kept back: no rule says which wins.
OnItsWay(p) ==
  \/ p \in approved \cup lifting \cup jobs \cup defPend \cup gPend \cup cand \cup InBatch
  \/ (ext # "idle" /\ p \in extMem)
  \/ p \notin seen
\* (The server keys a file by its bytes: a copy on its way is the photo on its way.)
ApprovedIsSent ==
  \A p \in P : (Cleared(p) /\ ~Conflict(H(p))) => (H(p) \in got \/ \E q \in P : H(q) = H(p) /\ OnItsWay(q))

\* "A photo kept back is never sent from this iPhone, and your server refuses it from any phone of yours"
\* (KeptBackView): every photo kept back has its hash, and the server holds it or it waits in unsentAdd
KeptBackIsTold == \A p \in kept : p \in keptH /\ (H(p) \in tomb \/ H(p) \in uAdd)

\* "Share after all: an opt-out taken back" (Gate.shareAfterAll): it stays taken back while that is the member's last
\* word on it, unless they also kept back these bytes, in another photo or on their other phone
ShareAfterAllStays == \A p \in after : (wish[p] = "share" /\ ~Conflict(H(p))) => p \notin kept

\* The server answers 400 to a hash in both lists, and would to every POST after (private.change_tombstones)
NeverBothLists == uAdd \cap uLift = {}

(* Broken by the code as it is, and not in PhoneGate.cfg: a question for the owner (see the end of this module) *)

\* A batch waits only where the member can see it: the Inbox shows the phone's batches only while gated
BatchesShown == policy = "everything" => InBatch = {}

(* What TLC found, the code before (PhoneGate.before.<invariant>.cfg; ios/Tests/main.swift replays each):
   - KeptBackNeverSent, SentOnlyAsSaid: a photo in a batch, marked kept back, its policy moved to everything: "Upload
     recent photos" sends it (Gate.refused knows hashes only), and so does the extension's next run if the photo is
     new to it (Gate.route under everything took every new photo).
   - ApprovedIsSent: under everything, Gate.route hands the new photos back; the token is saved; iOS ends the
     extension before `defaults.set(pending, ...)`: the photos are behind the token and in no list. Under a review
     policy the same with the approved photos, which Gate.route took out of the file. No bad timing: an end.
   - KeptBackIsTold: a photo marked, its original not readable (hashes(of:) is []): kept back with no hash, so no
     tombstone is ever sent, and nothing tries again. No bad timing. Also a photo marked while the others were hashed.
   - ShareAfterAllStays: "share after all" while apply's POST of the hash is under way: the hash is forgotten as never
     heard of, the POST reaches the server, and the uploader is refused and keeps the photo back again.
   - Once those were fixed, each other fix was taken out of the model in turn, and each time TLC found what it is for:
     the lift POSTed beside the add and reaching the server first; refresh's GET answered before a lift and written
     after it; the photo approved at once on "share after all" and made a job the server dropped, its add arriving
     first; a copy with the same bytes kept back as the lift went out; Gate.keptBack while that lift was on its way;
     the other photo kept back with the same bytes, whose tombstone the lift took away; and an apply whose hash
     serverTombstones said the server had, a lift behind.
   NeverBothLists held before too: apply and shareAfterAll each take a hash out of the other list as they add it.

   BatchesShown, not in PhoneGate.cfg, a question for the owner: a policy moved from review to everything leaves the
   batches made under review in GateState, where nothing shows them (InboxView shows batches only while gated) and
   their photos, known, are in no chunk; Gate.me approves the candidates, not them. TLC: Me(review), GatherNow,
   Me(everything). Whether they are approved then, dropped, or shown still is the owner's to say; until then they are
   held (Gate.held): never sent, by either uploader.

   Left out of this model, all of it in the code:
   - more than two photos, a photo with more than one file (a Live Photo: hashes(of:) now gives all or none), more
     than two batches, and a second gather or apply under way at once (one of each here);
   - the member acting more than MaxSaid times (eight was checked by hand once, as seven is in PhoneGate.cfg);
   - GateState.told and the first review_flagged batch that asks about everything: every photo here is one Vision
     flags for its text, so under review_flagged all are asked about either way. Under review gather does not await
     before Gate.gather, so a policy read and then changed under it is not possible there; under review_flagged it
     is: to everything (here) approves them all, as everything does; to review (left out: Me moves only from none,
     or to everything) asks about all of them, whatever the flags;
   - a policy moved back from everything to a review policy, a member on this phone changed (Gate.me starts again),
     Skip on a chunk, the Hidden album, and a photo deleted from Photos;
   - the other phone taking its own opt-out back (a lift there of bytes kept back here), and a purge's tombstone;
   - between the in-app uploader's check on the phone and the server's answer (one step here), a Live Photo sent
     half (one file stored, the other refused), and the system retrying a failed job;
   - the server's 400 and the phone's network failing on a GET (taken as no answer), the extension's first run (it
     saves a token and routes nothing), the job limit beyond "some jobs are made", and `estate.py`'s side of an
     upload once stored (ingest);
   - the app's own memory (GateModel.state, reloaded after each write it makes) and the chunks it lists: a chunk
     made before a photo found a place may be offered still, and Gate.gather drops what has a place since. *)
=============================================================================
