-------------------------------- MODULE People --------------------------------
(* Who each face is, as the catalog says (faces, persons, face_not, face_later) and as the People page holds it in
   memory (faces.People: built once by PhotoApp.load_people, answering every /people request), and every thread that
   writes either while requests are served:
     - a load of People (PhotoApp.load_people, one at a time under people_loading, a core.lifecycle.Lifecycle): the
       catalog read (People.__init__, People.load: face_rows, without every photo default_hidden says, and
       People.decisions), then People.seed and People.rescore, which write the names they work out (Google's, the
       automatic ones) to the catalog while the People before keeps answering, then the clustering thread started,
       then People.replaced_by and Lifecycle.succeed, which swap it in;
     - a member's write (People.label, People.label_many, People.edit_person, through People.handle): under People.lock,
       a draft of memory (People.decide), the catalog's transaction, the draft swapped in (People.commit), then
       People.rescore, which writes the automatic names it changed. A request takes the People that answers
       (PhotoApp.people_page: Lifecycle.snapshot) before it waits for its lock;
     - the clustering thread of each People (People.cluster, People.find_clusters): the candidates picked under the
       lock, the groups worked out outside it, then installed (ready) or failed, under the lock;
     - a group named on the page (POST /api/people/label {cluster: n}), n being a group of the answer to GET
       /api/people/unknown (People.unknown), which lists the groups' faces that memory says are unnamed;
     - what People is built without (default_hidden: photos the estate defaults hide and every photo private to
       anyone, lenses.State.people_out in the looks), against what the app's looks say (Live.looks, which every
       request's `keep` and `see` are worked out from): a member makes a photo private or releases it (POST
       /api/private, PhotoApp.private_write: private.change, then PhotoApp.relook, which says whether what People is
       built without changed, then on_hidden_change, whose thread, PhotoApp.hidden_changed, loads People if it did); a
       reload of the catalog (PhotoApp.reload_catalog: Live.install with with_private, which reads the private rows
       again into the looks, and PhotoApp.people_out before and after).

   The catalog has one face, f, in one photo, which its sharer Ben may make private and release. Ada names f. Cleo
   looks at the groups of unnamed faces and names one. f's name is "none", "auto" (rescore's), "google" (seed's) or
   "you" (a member's word). A People is known by the load that made it (ep): the one answering, and the one being
   loaded beside it.

   Guarded = TRUE is the code. Guarded = FALSE is the code before it was fixed (November 2026), kept so the invariants
   can be seen to fail without the guards:
     - People.seed and People.rescore write a name only where the catalog still has none of a member's or Google's
       (source NULL or 'auto'), and seed none to a person the face was said not to be (face_not): a load that read
       before a member named a face, or said who it is not, could otherwise write its own name over their word;
     - PhotoApp.load_people notes the changes the People answering has taken (People.writes, under its lock) before the
       new one reads, and before it swaps the new one in, People.replaced_by, under the old one's lock, has the new
       one read the names again from the catalog (People.refresh) if any were taken meanwhile: else a name given during
       the load was in the catalog and not in memory until the next load;
     - from then on, a People replaced passes a change to the one that replaced it (People.successor): a request that
       took the old one before the swap and its lock after would otherwise write it where nothing reads it;
     - a group's number is that People's own (People.first, from a random place in each run of the app, plus its
       place), and a label naming a number that is not one of its groups is refused (409, faces.Regrouped): a page
       showing the groups of one People named the same place in the next one, a group of other faces, or nothing at
       all while it said it had;
     - a reload of the catalog that changes what People is built without loads People again (reload_catalog): the
       member's relook then finds the looks already changed and says nothing changed, and People was not loaded.
   Fails = TRUE lets a load of People, or a clustering, raise.

   People.cfg checks the code; People.before.<invariant>.cfg each find the violation there was. tests/test_specs.py
   runs them all with TLC, and tests/test_people_states.py replays each on the real code. One invariant at the end is
   broken by the code as it is and is not in People.cfg: a question for the owner. *)
EXTENDS Integers

CONSTANTS Guarded, Fails

Names == {"none", "auto", "google", "you"}
Settled == {"google", "you"}         \* source >= GOOGLE: what seed and rescore leave alone
CS == {"waiting", "working", "ready", "failed"}

VARIABLES
  dbSrc,     \* f's row of `faces`: who says who it is
  dbPriv,    \* f's photo has an open `private` row (private to Ben)
  lPriv,     \* the app's looks (Live.looks.private) have it
  ep,        \* the People answering (Lifecycle.value): the load that made it
  mSrc,      \* its memory of f's name
  mOut,      \* it was built without f's photo (default_hidden): f is not in it at all
  mW,        \* the writes it has taken (People.writes)
  cs,        \* its clusters_state
  grp,       \* f is in one of its groups
  ld,        \* PhotoApp.load_people: "none" | "begun" | "read" | "built"
  bSrc, bOut, bSeen, bCs, bGrp,   \* the People being loaded: its memory, and what load_people noted before it read
  ph,        \* the Lifecycle's phase: "loading" | "ready" | "failed"
  asks,      \* calls of load_people waiting for people_loading
  ada,       \* Ada naming f: "idle" | "took" (the People answering, ada.at; whether it had f) | "done"
  ben,       \* Ben: "idle" | "relook" (committed) | "answered" | "done"
  flips,     \* how many times Ben has changed f's photo
  hc,        \* his hidden_changed threads still to ask for a load
  ig,        \* a reload of the catalog: "wait" | "done"
  cleo,      \* Cleo: "idle" | "shown" (the groups of People cleo.shown) | "took" (the People her label took) | "done"
  \* not in the code:
  said,      \* a member's word on f went into the catalog
  wrong,     \* a group label named another group than the one Cleo was shown, or nothing while it answered it had
  failed     \* a load raised

vars == <<dbSrc, dbPriv, lPriv, ep, mSrc, mOut, mW, cs, grp, ld, bSrc, bOut, bSeen, bCs, bGrp, ph, asks, ada, ben,
          flips, hc, ig, cleo, said, wrong, failed>>
cur == <<ep, mSrc, mOut, mW, cs, grp>>
new == <<bSrc, bOut, bSeen, bCs, bGrp>>

Init ==
  /\ dbSrc = "none" /\ dbPriv = FALSE /\ lPriv = FALSE
  /\ ep = 1 /\ mSrc = "none" /\ mOut = FALSE /\ mW = 0 /\ cs = "waiting" /\ grp = FALSE
  /\ ld = "none" /\ bSrc = "none" /\ bOut = FALSE /\ bSeen = 0 /\ bCs = "waiting" /\ bGrp = FALSE
  /\ ph = "ready" /\ asks = 0
  /\ ada = [at |-> "idle", ep |-> 0, has |-> FALSE] /\ ben = "idle" /\ flips = 0 /\ hc = 0 /\ ig = "wait"
  /\ cleo = [at |-> "idle", shown |-> 0, ep |-> 0] /\ said = FALSE /\ wrong = FALSE /\ failed = FALSE

\* the catalog write of a name seed or rescore worked out: before the fix, whatever the catalog has
Overwrite(v) == IF Guarded /\ dbSrc \in Settled THEN dbSrc ELSE v

--------------------------------------------------------------------------
(* PhotoApp.load_people, one at a time (people_loading) *)

\* people_loading taken, Lifecycle.begin; Guarded: the changes the People answering has taken (People.writes, under its
\* lock) noted. (Before the fix nothing was noted, and nothing reads bSeen then.)
LoadBegin ==
  /\ ld = "none" /\ asks > 0
  /\ ld' = "begun" /\ asks' = asks - 1 /\ ph' = "loading"
  /\ bSeen' = mW
  /\ UNCHANGED <<dbSrc, dbPriv, lPriv, cur, bSrc, bOut, bCs, bGrp, ada, ben, flips, hc, ig, cleo, said, wrong, failed>>

\* People.__init__, People.load: face_rows (the faces, less every photo default_hidden says), then People.decisions
\* (persons, face_not, face_later)
LoadRead ==
  /\ ld = "begun" /\ ld' = "read"
  /\ bSrc' = (IF dbPriv THEN "none" ELSE dbSrc) /\ bOut' = dbPriv
  /\ UNCHANGED <<dbSrc, dbPriv, lPriv, cur, bSeen, bCs, bGrp, ph, asks, ada, ben, flips, hc, ig, cleo, said, wrong,
                 failed>>

\* People.seed (at the end of People.load), then People.rescore: f, if it is in this People and nobody has settled it as
\* far as its read says, named as it works out (Google's name, an automatic one, or none), and written where it changed:
\* before the fix over whatever the catalog has, Guarded only where it has no settled name (Overwrite); then the
\* clustering thread started (waiting)
LoadBuild ==
  /\ ld = "read" /\ ld' = "built"
  /\ \E v \in (IF ~bOut /\ bSrc \notin Settled THEN {"none", "auto", "google"} ELSE {bSrc}) :
       /\ bSrc' = v
       /\ dbSrc' = IF v # bSrc THEN Overwrite(v) ELSE dbSrc
  /\ bCs' = "waiting" /\ bGrp' = FALSE
  /\ UNCHANGED <<dbPriv, lPriv, cur, bOut, bSeen, ph, asks, ada, ben, flips, hc, ig, cleo, said, wrong, failed>>

\* People() (or, Guarded, People.refresh in replaced_by) raised (a locked or broken table, out of memory):
\* Lifecycle.fail; the People answering keeps answering
LoadFail ==
  /\ Fails /\ ld \in {"begun", "read", "built"}
  /\ ld' = "none" /\ ph' = "failed" /\ failed' = TRUE
  /\ UNCHANGED <<dbSrc, dbPriv, lPriv, cur, new, asks, ada, ben, flips, hc, ig, cleo, said, wrong>>

\* Guarded: People.replaced_by, under the old one's lock: if it took a change since LoadBegin, the new one reads the names
\* again from the catalog (People.refresh) and works the automatic ones out again (rescore, written where they changed);
\* and the old one's successor is the new one (AdaWrite, CleoWrite). Then Lifecycle.succeed(p): the new People answers.
\* (A request between replaced_by and succeed takes the old one, and is passed on: one step here.)
LoadInstall ==
  /\ ld = "built" /\ ld' = "none" /\ ph' = "ready"
  /\ IF Guarded /\ mW # bSeen /\ ~bOut
       THEN \E v \in (IF dbSrc \notin Settled THEN {"none", "auto"} ELSE {dbSrc}) :
              /\ mSrc' = v /\ dbSrc' = v
       ELSE /\ mSrc' = bSrc /\ UNCHANGED dbSrc
  /\ ep' = ep + 1 /\ mOut' = bOut /\ mW' = 0 /\ cs' = bCs /\ grp' = bGrp
  /\ UNCHANGED <<dbPriv, lPriv, new, asks, ada, ben, flips, hc, ig, cleo, said, wrong, failed>>

--------------------------------------------------------------------------
(* The clustering thread of each People: People.cluster. The People answering ("cur") or the one being loaded ("new") *)

\* under the lock: working, and the candidates picked (People.find_clusters: unnamed, as memory says)
ClusterPick(o) ==
  \/ /\ o = "cur" /\ cs = "waiting" /\ cs' = "working" /\ grp' = (~mOut /\ mSrc = "none")
     /\ UNCHANGED <<dbSrc, dbPriv, lPriv, ep, mSrc, mOut, mW, ld, new, ph, asks, ada, ben, flips, hc, ig, cleo, said,
                    wrong, failed>>
  \/ /\ o = "new" /\ ld = "built" /\ bCs = "waiting" /\ bCs' = "working" /\ bGrp' = (~bOut /\ bSrc = "none")
     /\ UNCHANGED <<dbSrc, dbPriv, lPriv, cur, ld, bSrc, bOut, bSeen, ph, asks, ada, ben, flips, hc, ig, cleo, said,
                    wrong, failed>>

\* worked out outside the lock; then, under it, the groups swapped in and ready (or failed, if Fails)
ClusterDone(o) ==
  \E to \in (IF Fails THEN {"ready", "failed"} ELSE {"ready"}) :
    \/ /\ o = "cur" /\ cs = "working" /\ cs' = to
       /\ UNCHANGED <<dbSrc, dbPriv, lPriv, ep, mSrc, mOut, mW, grp, ld, new, ph, asks, ada, ben, flips, hc, ig, cleo,
                      said, wrong, failed>>
    \/ /\ o = "new" /\ ld = "built" /\ bCs = "working" /\ bCs' = to
       /\ UNCHANGED <<dbSrc, dbPriv, lPriv, cur, ld, bSrc, bOut, bSeen, bGrp, ph, asks, ada, ben, flips, hc, ig, cleo,
                      said, wrong, failed>>

--------------------------------------------------------------------------
(* Ada: POST /api/people/label {faces: [f], person: P} *)

\* PhotoApp.people_page: the People answering (Lifecycle.snapshot), before its lock
AdaTake ==
  /\ ada.at = "idle" /\ ada' = [at |-> "took", ep |-> ep, has |-> ~mOut]
  /\ UNCHANGED <<dbSrc, dbPriv, lPriv, cur, ld, new, ph, asks, ben, flips, hc, ig, cleo, said, wrong, failed>>

\* People.label, under the lock of the People it took: f named in the catalog's transaction and in memory, then rescore
\* (which leaves f alone: settled). A People since replaced: before the fix, its own memory, which nothing reads now;
\* Guarded, it passes the write on to the one that replaced it (People.successor). A People without f names nothing.
AdaWrite ==
  /\ ada.at = "took" /\ ada' = [ada EXCEPT !.at = "done"]
  /\ LET here == ada.ep = ep \/ Guarded
         has == IF here THEN ~mOut ELSE ada.has
     IN /\ dbSrc' = IF has THEN "you" ELSE dbSrc
        /\ said' = (said \/ has)
        /\ mSrc' = IF here /\ has THEN "you" ELSE mSrc
        /\ mW' = IF here THEN mW + 1 ELSE mW
  /\ UNCHANGED <<dbPriv, lPriv, ep, mOut, cs, grp, ld, new, ph, asks, ben, flips, hc, ig, cleo, wrong, failed>>

--------------------------------------------------------------------------
(* Cleo: GET /api/people/unknown, then POST /api/people/label {cluster: n, person: Q} *)

\* People.unknown: the groups, once the People answering has them (ready), each with its number
CleoLook ==
  /\ cleo.at = "idle" /\ cs = "ready"
  /\ cleo' = [cleo EXCEPT !.at = "shown", !.shown = ep]
  /\ UNCHANGED <<dbSrc, dbPriv, lPriv, cur, ld, new, ph, asks, ada, ben, flips, hc, ig, said, wrong, failed>>

CleoTake ==
  /\ cleo.at = "shown" /\ cleo' = [cleo EXCEPT !.at = "took", !.ep = ep]
  /\ UNCHANGED <<dbSrc, dbPriv, lPriv, cur, ld, new, ph, asks, ada, ben, flips, hc, ig, said, wrong, failed>>

\* People.label (People.decide), under the lock of the People it took (Guarded: the one that replaced it): group n is that
\* People's n-th. Before the fix the number is a place in whichever People takes it: the group shown only if it is the one
\* that showed it; else another group, or none (not ready: nothing named, and the answer ok). Guarded: a number of another
\* People's is refused (409). A label that names its group then rescores, which may name f anew where nobody has.
CleoWrite ==
  /\ cleo.at = "took" /\ cleo' = [cleo EXCEPT !.at = "done"]
  /\ LET at == IF Guarded THEN ep ELSE cleo.ep
         refused == Guarded /\ at # cleo.shown
     IN /\ wrong' = (wrong \/ (~refused /\ at # cleo.shown))
        /\ IF at = ep /\ ~refused
             THEN /\ mW' = mW + 1
                  /\ \E v \in (IF ~mOut /\ mSrc \notin Settled THEN {"none", "auto"} ELSE {mSrc}) :
                       /\ mSrc' = v
                       /\ dbSrc' = IF v # mSrc THEN Overwrite(v) ELSE dbSrc
             ELSE UNCHANGED <<mW, mSrc, dbSrc>>
  /\ UNCHANGED <<dbPriv, lPriv, ep, mOut, cs, grp, ld, new, ph, asks, ada, ben, flips, hc, ig, said, failed>>

--------------------------------------------------------------------------
(* Ben: POST /api/private, twice (withdrawn, then released), and a reload of the catalog *)

\* PhotoApp.private_write: private.change commits
BenCommit ==
  /\ ben \in {"idle", "answered"} /\ flips < 2
  /\ dbPriv' = ~dbPriv /\ flips' = flips + 1 /\ ben' = "relook"
  /\ UNCHANGED <<dbSrc, lPriv, cur, ld, new, ph, asks, ada, hc, ig, cleo, said, wrong, failed>>

\* PhotoApp.relook: the looks read again and installed, and whether what People is built without changed (the looks
\* before against the looks after); then on_hidden_change starts hidden_changed, and the request is answered
BenRelook ==
  /\ ben = "relook" /\ ben' = IF flips = 2 THEN "done" ELSE "answered"
  /\ lPriv' = dbPriv /\ hc' = IF lPriv # dbPriv THEN hc + 1 ELSE hc
  /\ UNCHANGED <<dbSrc, dbPriv, cur, ld, new, ph, asks, ada, flips, ig, cleo, said, wrong, failed>>

\* PhotoApp.hidden_changed: load_people, if the relook said so (it waits for people_loading)
BenLoad ==
  /\ hc > 0 /\ hc' = hc - 1 /\ asks' = asks + 1
  /\ UNCHANGED <<dbSrc, dbPriv, lPriv, cur, ld, new, ph, ada, ben, flips, ig, cleo, said, wrong, failed>>

\* PhotoApp.reload_catalog (an ingest's photos are in): Live.install, with with_private, reads the private rows again into
\* the looks. Guarded: if that changed what People is built without (PhotoApp.people_out, before and after, under
\* fav_write), load_people in a thread of its own
ReloadInstall ==
  /\ ig = "wait" /\ ig' = "done"
  /\ lPriv' = dbPriv
  /\ asks' = IF Guarded /\ lPriv # dbPriv THEN asks + 1 ELSE asks
  /\ UNCHANGED <<dbSrc, dbPriv, cur, ld, new, ph, ada, ben, flips, hc, cleo, said, wrong, failed>>

Next ==
  \/ LoadBegin \/ LoadRead \/ LoadBuild \/ LoadFail \/ LoadInstall
  \/ \E o \in {"cur", "new"} : ClusterPick(o) \/ ClusterDone(o)
  \/ AdaTake \/ AdaWrite \/ CleoLook \/ CleoTake \/ CleoWrite
  \/ BenCommit \/ BenRelook \/ BenLoad \/ ReloadInstall

Spec == Init /\ [][Next]_vars

--------------------------------------------------------------------------
(* What must always hold *)

TypeOK ==
  /\ {dbSrc, mSrc, bSrc} \subseteq Names /\ {cs, bCs} \subseteq CS
  /\ {dbPriv, lPriv, mOut, grp, bOut, bGrp, said, wrong, failed} \subseteq BOOLEAN /\ hc \in 0..2
  /\ ld \in {"none", "begun", "read", "built"} /\ ph \in {"loading", "ready", "failed"} /\ asks \in 0..3
  /\ ada.at \in {"idle", "took", "done"} /\ ben \in {"idle", "relook", "answered", "done"} /\ flips \in 0..2
  /\ cleo.at \in {"idle", "shown", "took", "done"} /\ ig \in {"wait", "done"}

\* Lifecycle: "two loads of one thing at once are a bug that shows": begin() is never called while one is loading
OneLoadAtATime == ld = "none" => ph # "loading"

\* faces.py: "Everything you decide" is kept: a member's word on a face stays in the catalog until a member says otherwise
NameKept == said => dbSrc = "you"

\* faces.py: changes "reach SQLite in the order they reach memory and the two never disagree": the People answering has
\* every member's word the catalog has, and none it does not
WordsAsTheCatalog == ~mOut => ((mSrc = "you") <=> (dbSrc = "you"))

\* POST /api/people/label {cluster: n} names "the whole group" Cleo was shown, and nothing else
GroupAsShown == ~wrong

\* GET /api/people/unknown lists a face of a group only while memory says nobody has named it, and so the catalog too
ShownUnnamed == (cs = "ready" /\ grp /\ ~mOut /\ mSrc = "none") => dbSrc # "you"

\* Ben's request has committed and its relook has not yet put it in the looks
Outstanding == ben = "relook"

\* default_hidden: a private photo's faces "are in People for no one ... until it is released": a face of a photo private
\* to Ben is shown to nobody else (People.handle's keep and see, from the looks), but while his own request takes it in
NoPrivateFaceShown == (~mOut /\ ~lPriv) => (~dbPriv \/ Outstanding)

\* Nothing is under way: no load of People asked for or running, nobody between a commit and its relook, no
\* hidden_changed still to ask for one
Quiet == ld = "none" /\ asks = 0 /\ hc = 0 /\ ben # "relook" /\ ada.at # "took" /\ cleo.at # "took"

\* Once nothing is under way, the People answering is what the catalog says: built without exactly the photos that are
\* private now (a released photo's faces are in it), with f's name as the catalog has it (but after a load that raised:
\* AsTheCatalogEvenAfterAFailure, for the owner)
AsTheCatalogOnceQuiet == Quiet /\ ~failed => (mOut = dbPriv /\ (~mOut => mSrc = dbSrc))

(* Broken by the code as it is, and not in People.cfg: a question for the owner (see the end of this module) *)

\* A load of People that raised is not tried again: until the next ingest's faces, private change or restart, People is
\* what it was (a photo released meanwhile is not in it; one made private is, but shown to nobody: NoPrivateFaceShown)
AsTheCatalogEvenAfterAFailure == Quiet => (mOut = dbPriv /\ (~mOut => mSrc = dbSrc))

(* For asking narrower questions, as an ACTION_CONSTRAINT in a .cfg of your own. NoRace: nothing happens between a
   load's read and its install, between a request taking a People and writing to it, or between Ben's commit and his
   relook. Quiet (above) is where no read waits for its write. *)
NoRace == /\ ld = "begun" => ld' \in {"read", "none"}
          /\ ld = "read" => ld' \in {"built", "none"}
          /\ ld = "built" => ld' = "none"
          /\ ada.at = "took" => ada'.at = "done"
          /\ cleo.at = "took" => cleo'.at = "done"
          /\ ben = "relook" => ben' # "relook"

(* Left out of this model, all of it in the code:
   - every face but f, and every person: a name is who says so ("you", "google", "auto", none), not whom. A merge or a
     rename (edit_person) and a birthday are changes like Ada's (under the lock, the draft, the transaction, the swap,
     passed on when replaced). "Not this person" (face_not) and "later" are words like hers: seed's guarded write
     names no face to a person it is not (Google's name is never worked out again), and rescore's automatic names are
     worked out again from the face_not rows by refresh. Two members changing names at once inside one People wait for
     its lock, which is held across read, write and swap, so they are not a race (tests/test_faces_consistency.py);
   - the other faces of a group, what a group label names of f, and "everything waiting for P" (label's `pending`),
     which is what the People that takes it says is waiting, not what the page showed: by design, one People or two;
   - the scan (faces.py run, a process of its own), which adds faces and never changes one: a People misses those
     added after its read until the next load, which the ingest's on_faces asks for; and estate.py purge, which deletes
     a photo's faces, by hand, with a restart after it (MAC-MINI.md): until then the People answering still has them,
     and a face id the purge freed (the largest) is given to the next scan's new face, which a label of the purged
     face by id then names (tests/test_people_states.py shows it; a question for the owner);
   - the estate defaults' hides, which change what People is built without as a private row does, through the lens
     routes' relook and a change set's hidden_changed (Ben's shape);
   - the ingest's on_faces and intake's on_faces (more calls of load_people), the release timer (Ben's shape), and the
     seed's new persons (a person a member made of the same name during the load is the tag's: before the fix SQLite
     refused it, persons.name being UNIQUE, and the load failed);
   - the counts People answers (named, pending, a group's n) and a group's guess at a name, which are over every face
     it holds, a stale one's included: no face is shown by them;
   - a rescore's own SQLite error after a label's transaction committed (the request is answered 500, the name is in);
   - who waits for whom: the locks are People.lock (the old one's, then the new one's, in replaced_by and in a change
     passed on; never the other way), people_loading outside them, and SQLite's write lock inside them. *)
=============================================================================
