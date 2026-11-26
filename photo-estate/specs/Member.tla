------------------------------- MODULE Member -------------------------------
(* Who is let in: the rows of `members` (role sysadmin | member, status pending | active | revoked, never deleted;
   joined_at, when first let in; a name, standing for every other field) and their member_logins, written by
   members.py (request, change; add, first_start, demo and agent only make rows), and a member's own agent on a
   device, a row of `agent_bindings` (status pending | approved | revoked, node: the device), written by members.bind
   and members.decide_binding. Just as much a part of it is where these are read to let a request through:
     - photo_app.PhotoApp.who: the member a login is, from `roster`, a copy of members.by_login kept in memory and
       read again by load_members after members.request (ask_in) and members.change (change_member) return, in the
       request's own thread; route() lets it through (PhotoApp.refusal) by http_util's Auth.allows (MEMBER:
       active; SYSADMIN: an active sysadmin) on that copy. Nothing else reads it again: a copy lives until the next
       load. members.change does not look at its actor: who may change members is the gate's alone;
     - PhotoApp.mcp_route and estate_mcp.Server.handle: a call with a member's login is their own agent if the
       roster says they are active, and members.bind reads its binding from the catalog on every call (approved:
       it writes; pending: it only looks; revoked: 403);
     - a run of the estate's agent: its token (tasks.Tasks.mint, in memory while the run lasts; Tasks.run_of) says
       whom it acts for (agent_tasks.acting_for), and nothing reads that member's status again;
     - PhotoApp.device: the node a request came from, as `tailscale whois` names its address (a StableID), or the
       address itself when whois does not answer; PhotoApp.agents_route gives it to decide_binding, which compares
       that string with the binding's node.

   The members here: Ada and Dee, active sysadmins, and Ben; and a newcomer's login, x, with no member yet. One run
   of the model is of one of three shapes (`part`, chosen at the start; the others' variables stay as they start, so
   the states of the three are added, not multiplied):
     "change"  Ben has asked in (pending). Up to three POST /api/members/<id> (Changes), two of them under way at once
               (two sysadmins, or one with two tabs), each setting one field of any member, as members.html does,
               by whoever the roster says is a sysadmin, each reading the roster again after it
     "ask"     two POST /api/request-access from x's login at once, each reading the roster again after it
     "agent"   Ben is in (active). His own agent on his laptop; two POST /api/agents/<id> by him at once (from the
               laptop or his phone, whois answering for either or not); a run of the estate's agent acting for him;
               and up to two changes of his status by Ada (revoked, let in again)

   Who held the write lock when it read what it decides by, before the fix below (Python's `with con:` begins a
   transaction only at the first write):
     - members.change reads the member (get) and then UPDATEs every field from that copy: its read (ChangeRead, also
       the route's gate), then its write (ChangeWrite). Its check for an active sysadmin is after its own UPDATE, in
       the transaction, so it reads the catalog as it is;
     - members.request looks the login up, then _add INSERTs it: RequestRead, RequestWrite;
     - members.decide_binding reads the binding, then UPDATEs it only when its status differs from what it read:
       DecideRead (also the route's gate and the device rule), DecideWrite;
     - PhotoApp.load_members reads by_login, then assigns the copy: LoadRead, LoadWrite. Every writer above but
       decide_binding loads after it returns (a refused change does not: its ValueError is answered first);
     - members.bind reads the binding outside its transaction too, but writes only seen_at and device (never the
       status nor the node), so its read decides nothing it writes: one action (Bind, AgentWrite).

   Guarded = TRUE is the code. Guarded = FALSE is the code before it was fixed (November 2026), kept so the
   invariants can be seen to fail without the guard: members.change, members.request and members.decide_binding take
   the write lock (BEGIN IMMEDIATE, members._begin) before they read, and PhotoApp.load_members holds members_lock
   from its read to its assignment. Here a read under the lock keeps nothing for its write (Kept), which reads what
   is there when it writes, and two loads are never between their read and their assignment at once.

   Member.cfg checks the code; Member.before.<invariant>.cfg each find the violation there was. tests/test_specs.py
   runs them all with TLC, and tests/test_member_states.py replays each on the real code. Four invariants at the end
   are broken by the code as it is and are not in Member.cfg: each is a question for the owner (the module's last
   comment, and the tests that show each). *)
EXTENDS Naturals, FiniteSets

CONSTANT Guarded

Parts == {"change", "ask", "agent"}
Changes == [p \in Parts |-> CASE p = "change" -> 3 [] p = "agent" -> 2 [] OTHER -> 0]   \* changes begun, at most
P == {"ada", "dee", "ben"}          \* the members
Admins == {"ada", "dee"}            \* the sysadmins at the start
X == "x"                            \* a newcomer's login
Who == P \cup {X}
C == {"c1", "c2"}                   \* two members.change at once
R == {"r1", "r2"}                   \* two members.request from x's login at once
W == C \cup R                       \* the requests that write a member, and then load the roster
T == {"t1", "t2"}                   \* two members.decide_binding at once
Roles == {"sysadmin", "member"}
Sts == {"pending", "active", "revoked"}
Fld == {"role", "st", "nm"}         \* (nm: the name, and every other field; n0 at the start, n1 renamed)
Vals(f) == CASE f = "role" -> Roles [] f = "st" -> Sts [] OTHER -> {"n1"}

Row(r, s, nm, j) == [role |-> r, st |-> s, nm |-> nm, jn |-> j]
NoRow == Row("member", "none", "n0", FALSE)         \* x before it asks: no row
Seen(row) == [role |-> row.role, st |-> row.st]   \* what the roster keeps of a member (by_login)
NoCopy == [w \in Who |-> Seen(NoRow)]
Idle == [ph |-> "idle", m |-> "ada", f |-> "st", v |-> "active", cp |-> NoRow, saw |-> FALSE, lc |-> NoCopy]
\* What a read keeps for its write: nothing, Guarded (the write reads again, under the lock it takes first)
Kept(x, none) == IF Guarded THEN none ELSE x
DcIdle == [ph |-> "idle", allow |-> FALSE, saw |-> "none"]

VARIABLES
  part,      \* which shape this run is
  n,         \* how many changes have begun
  db,        \* each member's row in the catalog (x's st "none": no row yet)
  roster,    \* PhotoApp.roster: what a request is let through by (role and status of each login's member)
  wr,        \* each writer: "idle"; "read" (between its read and its write, with its copy `cp`, or `saw`); "load"
             \*   (committed, its load_members not begun); "assign" (load_members has read `lc` and not assigned it)
  bnd,       \* Ben's binding on his laptop: "none" (no call yet) | "pending" | "approved" | "revoked"
  bnode,     \* its node: "node" (whois answered its first call: node:<StableID>) or "addr" (it did not: addr:<ip>)
  dc,        \* each decide_binding: "idle", or "read" with what it read (`saw`) and what it says (`allow`)
  run,       \* a run of the estate's agent acting for Ben is under way (its token is live)
  \* not in the code:
  ever,      \* each member has been active (let in) at some time
  told,      \* what the last decide_binding answered Ben his agent now is ("-": none yet)
  since,     \* Ben has been revoked since his binding was last decided
  lost,      \* a change wrote a field it was not asked to change, with a value that was not there
  wrongLet,  \* a request was let through as what the catalog does not say, with no load under way
  err,       \* a request to be let in was answered with an error (no answer at all: the connection is dropped)
  selfAllow, \* Ben's agent was allowed by a request from its own device
  backIn,    \* Ben's agent wrote as him, active again after he was revoked, with no word from him on it since
  runRevoked \* the estate's agent acted for Ben while he was not active

vars == <<part, n, db, roster, wr, bnd, bnode, dc, run, ever, told, since, lost, wrongLet, err, selfAllow, backIn,
          runRevoked>>
agentv == <<bnd, bnode, dc, run, told, selfAllow, backIn, runRevoked>>   \* (the part "agent"'s own)

Cat == [w \in Who |-> Seen(db[w])]
SysadminIn(s) == s.role = "sysadmin" /\ s.st = "active"
Active(s) == s.st = "active"
HasSysadmin(d) == \E p \in P : SysadminIn(Seen(d[p]))
\* a writer has committed and has not yet put what it then read into the roster: the roster may be behind until then
Outstanding == \E w \in W : wr[w].ph \in {"load", "assign"}
\* the route's gate, on the roster: let through as `p` where the catalog says otherwise is wrong, unless a load is due
Gate(p, ok(_)) == wrongLet' = (wrongLet \/ (~ok(Seen(db[p])) /\ ~Outstanding))

Init ==
  /\ part \in Parts /\ n = 0
  /\ db = [w \in Who |-> IF w \in Admins THEN Row("sysadmin", "active", "n0", TRUE)
                         ELSE IF w = "ben" /\ part = "agent" THEN Row("member", "active", "n0", TRUE)
                         ELSE IF w = "ben" THEN Row("member", "pending", "n0", FALSE) ELSE NoRow]
  /\ roster = Cat
  /\ wr = [w \in W |-> Idle]
  /\ bnd = "none" /\ bnode = "node" /\ dc = [t \in T |-> DcIdle] /\ run = FALSE
  /\ ever = [w \in Who |-> w \in Admins \/ (w = "ben" /\ part = "agent")] /\ told = "-" /\ since = FALSE
  /\ lost = FALSE /\ wrongLet = FALSE /\ err = FALSE /\ selfAllow = FALSE /\ backIn = FALSE /\ runRevoked = FALSE

--------------------------------------------------------------------------
(* Members: POST /api/members/<id> (PhotoApp.change_member, members.change) and POST /api/request-access
   (PhotoApp.ask_in, members.request), each followed by PhotoApp.load_members *)

\* change_member, let through by the roster (Auth.SYSADMIN), and members.change's read: get(con, mid), before any write
ChangeRead(c, a, m, f, v) ==
  /\ part \in {"change", "agent"} /\ n < Changes[part] /\ wr[c].ph = "idle" /\ (f = "nm" => m = "ben")
  /\ part = "agent" => c = "c1" /\ a = "ada" /\ m = "ben" /\ f = "st"
  /\ SysadminIn(roster[a])
  /\ Gate(a, SysadminIn)
  /\ wr' = [wr EXCEPT ![c] = [Idle EXCEPT !.ph = "read", !.m = m, !.f = f, !.v = v, !.cp = Kept(db[m], NoRow)]]
  /\ n' = n + 1
  /\ UNCHANGED <<part, db, roster, ever, since, lost, err, agentv>>

\* members.change's write: new = the copy with the field set. Nothing differs: it returns. Else the UPDATE of every
\* field from `new` (joined_at: the copy's, or now if `new` is active), then the check: no active sysadmin left is a
\* ValueError, rolled back and answered 400 (no load). Guarded: the copy is the row as it is now (_begin first).
ChangeWrite(c) ==
  LET w == wr[c]
      row == IF Guarded THEN db[w.m] ELSE w.cp
      new == [row EXCEPT ![w.f] = w.v]
      put == [new EXCEPT !.jn = row.jn \/ new.st = "active"]
      after == [db EXCEPT ![w.m] = put] IN
  /\ w.ph = "read"
  /\ IF new = row
       THEN /\ wr' = [wr EXCEPT ![c].ph = "load"] /\ UNCHANGED <<db, ever, since, lost>>
       ELSE IF ~HasSysadmin(after)
         THEN /\ wr' = [wr EXCEPT ![c] = Idle] /\ UNCHANGED <<db, ever, since, lost>>
         ELSE /\ db' = after
              /\ lost' = (lost \/ \E g \in Fld \ {w.f} : put[g] # db[w.m][g])
              /\ ever' = [ever EXCEPT ![w.m] = @ \/ put.st = "active"]
              /\ since' = (since \/ (w.m = "ben" /\ put.st = "revoked"))
              /\ wr' = [wr EXCEPT ![c].ph = "load"]
  /\ UNCHANGED <<part, n, roster, wrongLet, err, bnd, bnode, dc, run, told, selfAllow, backIn, runRevoked>>

\* ask_in, for a login the roster has no member for (PUBLIC), and members.request's read: lookup(con, login)
RequestRead(r) ==
  /\ part = "ask" /\ wr[r].ph = "idle" /\ roster[X].st = "none"
  /\ wr' = [wr EXCEPT ![r] = [Idle EXCEPT !.ph = "read", !.saw = Kept(db[X].st # "none", FALSE)]]
  /\ UNCHANGED <<part, n, db, roster, ever, since, lost, wrongLet, err, agentv>>

\* members.request's write: a login that is a member's is answered with that member; else _add INSERTs the member
\* and its login, pending. A login made since the read: the member_logins trigger raises (sqlite3.IntegrityError),
\* which nothing catches: the handler's thread ends with no answer. Guarded: the lookup is under the lock (_begin).
RequestWrite(r) ==
  LET there == IF Guarded THEN db[X].st # "none" ELSE wr[r].saw IN
  /\ wr[r].ph = "read"
  /\ IF there
       THEN /\ wr' = [wr EXCEPT ![r].ph = "load"] /\ UNCHANGED <<db, err>>
       ELSE IF db[X].st # "none"
         THEN /\ err' = TRUE /\ wr' = [wr EXCEPT ![r] = Idle] /\ UNCHANGED db
         ELSE /\ db' = [db EXCEPT ![X] = Row("member", "pending", "n0", FALSE)]
              /\ wr' = [wr EXCEPT ![r].ph = "load"] /\ UNCHANGED err
  /\ UNCHANGED <<part, n, roster, ever, since, lost, wrongLet, agentv>>

\* PhotoApp.load_members: members.by_login, read. Guarded: under members_lock, which no other load holds
LoadRead(w) ==
  /\ wr[w].ph = "load"
  /\ Guarded => \A u \in W : wr[u].ph # "assign"
  /\ wr' = [wr EXCEPT ![w].ph = "assign", ![w].lc = Cat]
  /\ UNCHANGED <<part, n, db, roster, ever, since, lost, wrongLet, err, agentv>>

\* and its assignment: self.roster = what it read
LoadWrite(w) ==
  /\ wr[w].ph = "assign"
  /\ roster' = wr[w].lc
  /\ wr' = [wr EXCEPT ![w] = Idle]
  /\ UNCHANGED <<part, n, db, ever, since, lost, wrongLet, err, agentv>>

--------------------------------------------------------------------------
(* Requests let through as a member *)

\* Any request on a MEMBER route (a search, a photo, /api/me's answer), let through by the roster (in the part
\* "agent", Ben's requests are the ones below)
Let(p) ==
  /\ part = "change" /\ Active(roster[p])
  /\ Gate(p, Active)
  /\ UNCHANGED <<part, n, db, roster, wr, ever, since, lost, err, agentv>>

\* POST /api/tasks by Ben (Tasks.submit, acting_for Ben), and the worker starting it: a run with its token
Submit ==
  /\ part = "agent" /\ ~run /\ Active(roster["ben"])
  /\ Gate("ben", Active)
  /\ run' = TRUE
  /\ UNCHANGED <<part, n, db, roster, wr, ever, since, lost, err, bnd, bnode, dc, told, selfAllow, backIn, runRevoked>>

\* a call of the run to /mcp with its token (Server.handle: Tasks.run_of, from this computer): it acts for Ben
RunCall ==
  /\ run
  /\ runRevoked' = (runRevoked \/ ~Active(Seen(db["ben"])))
  /\ UNCHANGED <<part, n, db, roster, wr, ever, since, lost, wrongLet, err, bnd, bnode, dc, run, told, selfAllow, backIn>>

\* the run ends (done, failed, cancelled, or its timeout): Tasks.forget of its token
RunEnd ==
  /\ run /\ run' = FALSE
  /\ UNCHANGED <<part, n, db, roster, wr, ever, since, lost, wrongLet, err, bnd, bnode, dc, told, selfAllow, backIn,
                 runRevoked>>

\* Ben's own agent's first call to /mcp from his laptop (mcp_route: the roster says he is active; members.bind): its
\* binding, pending, its node as whois named the laptop then (wh), or its address
Bind(wh) ==
  /\ part = "agent" /\ bnd = "none" /\ Active(roster["ben"])
  /\ Gate("ben", Active)
  /\ bnd' = "pending" /\ bnode' = IF wh THEN "node" ELSE "addr"
  /\ UNCHANGED <<part, n, db, roster, wr, ever, since, lost, err, dc, run, told, selfAllow, backIn, runRevoked>>

\* a later call of the agent that writes (members.bind reads the binding: approved, so the tool runs as Ben)
AgentWrite ==
  /\ bnd = "approved" /\ Active(roster["ben"])
  /\ Gate("ben", Active)
  /\ backIn' = (backIn \/ (since /\ Active(Seen(db["ben"]))))
  /\ UNCHANGED <<part, n, db, roster, wr, ever, since, lost, err, bnd, bnode, dc, run, told, selfAllow, runRevoked>>

\* POST /api/agents/<id> {allow} by Ben from device `dev`, whois answering for it or not (wh): let through by the
\* roster (MEMBER), and decide_binding's read of the binding. Allowing from the node the binding has is 403: the
\* request's node is node:<StableID> when whois answers and addr:<ip> when it does not; the binding's is one of them
DecideRead(t, allow, dev, wh) ==
  /\ dc[t].ph = "idle" /\ bnd # "none" /\ Active(roster["ben"])
  /\ Gate("ben", Active)
  /\ LET same == dev = "laptop" /\ (wh <=> bnode = "node") IN
       IF allow /\ same
         THEN UNCHANGED <<dc, selfAllow>>
         ELSE /\ dc' = [dc EXCEPT ![t] = [ph |-> "read", allow |-> allow, saw |-> Kept(bnd, "none")]]
              /\ selfAllow' = (selfAllow \/ (allow /\ dev = "laptop"))
  /\ UNCHANGED <<part, n, db, roster, wr, ever, since, lost, err, bnd, bnode, run, told, backIn, runRevoked>>

\* decide_binding's write: the UPDATE when the status it read is not the one asked for, the binding's item done, and
\* the answer: the binding with the status asked for. Guarded: read under the lock (_begin), so it is the status now
DecideWrite(t) ==
  LET s == IF dc[t].allow THEN "approved" ELSE "revoked"
      cur == IF Guarded THEN bnd ELSE dc[t].saw IN
  /\ dc[t].ph = "read"
  /\ bnd' = IF cur # s THEN s ELSE bnd
  /\ told' = s /\ since' = FALSE
  /\ dc' = [dc EXCEPT ![t] = DcIdle]
  /\ UNCHANGED <<part, n, db, roster, wr, ever, lost, wrongLet, err, bnode, run, selfAllow, backIn, runRevoked>>

Next ==
  \/ \E c \in C, a \in P, m \in P, f \in Fld : \E v \in Vals(f) : ChangeRead(c, a, m, f, v)
  \/ \E c \in C : ChangeWrite(c)
  \/ \E r \in R : RequestRead(r) \/ RequestWrite(r)
  \/ \E w \in W : LoadRead(w) \/ LoadWrite(w)
  \/ \E p \in P : Let(p)
  \/ Submit \/ RunCall \/ RunEnd
  \/ \E wh \in BOOLEAN : Bind(wh)
  \/ AgentWrite
  \/ \E t \in T, allow \in BOOLEAN, dev \in {"laptop", "phone"}, wh \in BOOLEAN : DecideRead(t, allow, dev, wh)
  \/ \E t \in T : DecideWrite(t)

Spec == Init /\ [][Next]_vars

--------------------------------------------------------------------------
(* What must always hold *)

TypeOK ==
  /\ part \in Parts /\ n \in 0..3
  /\ db \in [Who -> [role : Roles, st : Sts \cup {"none"}, nm : {"n0", "n1"}, jn : BOOLEAN]]
  /\ roster \in [Who -> [role : Roles, st : Sts \cup {"none"}]]
  /\ \A w \in W : wr[w].ph \in {"idle", "read", "load", "assign"}
  /\ bnd \in {"none", "pending", "approved", "revoked"} /\ bnode \in {"node", "addr"}
  /\ \A t \in T : dc[t].ph \in {"idle", "read"}
  /\ told \in {"-", "approved", "revoked"}

\* members.py: change raises ValueError for "a change that would leave the estate with no active sysadmin"
KeepsASysadmin == HasSysadmin(db)

\* change: "Change some of a member's FIELDS ... One member.changed entry for what changed": a change writes only
\* the fields it was asked to change; every other is left as it is
OnlyWhatWasAsked == ~lost

\* change: "Approving is status active (joined_at is set the first time)"; core/schema.py: joined_at "when first let
\* in". It is set once a member has been let in, and stays set.
JoinedWhenFirstLetIn == \A w \in Who : db[w].jn = ever[w]

\* load_members: "Members change only through this app, so they are kept in memory and read again after a change."
\* Once every change has been read again, what the app keeps is what the catalog says.
RosterAsCatalog == ~Outstanding => roster = Cat

\* photo_app.py: "only active members may do anything at all (Auth.MEMBER) ... Letting people in ... take a sysadmin
\* (Auth.SYSADMIN)": a request is let through only as what the catalog says the member is, but while a change it
\* just committed is being read again
LetAsTheCatalogSays == ~wrongLet

\* request: "A login that is already a member's, in any status, is answered with that member."
AskedOnceAnswered == ~err

\* decide_binding: "The member allows their agent on a device (approved) or turns it off (revoked)": once no decision
\* is under way, the binding is what the last one answered Ben it is
BindingAsLastAnswered == (\A t \in T : dc[t].ph = "idle") /\ told # "-" => bnd = told

(* Broken by the code as it is, and not in Member.cfg: questions for the owner (see the end of this module) *)

\* decide_binding: "Allowing must come from another device than the agent's own". Broken with no bad timing when
\* whois answers for one of the two requests and not the other: the two node strings of one laptop differ.
OwnDeviceNeverAllows == ~selfAllow

\* A member revoked and let in again has their own agents as they were: an approved one writes again, unasked.
AllowedSinceLetIn == ~backIn

\* A run of the estate's agent acting for a member goes on acting for them after they are revoked (its token is
\* checked for being live, not for whom it acts for), and one queued before starts after.
RunsOnlyForActive == ~runRevoked

\* Any status may follow any other in change: a member let in may be moved back to pending (with no member_request
\* item for anyone; /members still lists them first). The docstrings do not say whether that is meant.
PendingOnlyBeforeLetIn == \A p \in P : db[p].st = "pending" => ~ever[p]

(* For asking narrower questions, as an ACTION_CONSTRAINT in a .cfg of your own. NoRace: each request runs alone from
   its read to the end of its load. TLC checks an invariant in every state it generates, a state the constraint then
   leaves out too, so a violation found with NoRace may still have one step of bad timing at its end: check
   `Quiet => <invariant>` with it (an operator of a module that EXTENDS this one) to see what needs none at all. *)
NoRace == /\ \A w \in W : wr[w].ph # "idle" => wr'[w].ph # wr[w].ph
          /\ \A t \in T : dc[t].ph # "idle" => dc'[t].ph # dc[t].ph
Quiet == (\A w \in W : wr[w].ph = "idle") /\ \A t \in T : dc[t].ph = "idle"

(* Left out of this model, all of it in the code:
   - members.add (no caller but the tests), first_start and demo (once, as the app starts, before it serves: two
     apps starting at once on a catalog with no members could each make theirs, as both read before they write) and
     agent (the estate's agent: kind agent, which change refuses with a ValueError);
   - a member with several logins (member_logins rows are never deleted or changed: triggers), upload_policy and
     person_id (fields like the name here), and the persons check;
   - the inbox items a change closes and a request or a bind opens: specs/InboxItem.tla;
   - more than three changes, and the newcomer let in or turned away (the part "change" has Ben for that);
   - the window inside one request between the gate and the end of its work: a request let through as an active
     member that is still running when they are revoked finishes, and so does an agent's call that read its binding
     approved just before it was turned off. Here a request is let through in one step;
   - the window between a change's commit and its own load_members (members.everyone, a new connection): requests
     let through on the copy then are not counted as wrong (Outstanding). It is milliseconds, and it is there in the
     code as it is; only reading the member on every request would close it;
   - this computer (`local`: always the sysadmin, from PhotoApp.sysadmin, loaded with the roster), --demo's
     X-Demo-Member and X-Demo-Device, the demo's be= cookie;
   - what a member's revoke does not touch: their questions, inbox items, subscriptions, sync feed and tasks queued
     (a task queued for them before is started after: RunsOnlyForActive covers a run either way);
   - whois's cache of a device's name (WHOIS_FOR: ten minutes, a failure one): here each request finds whois
     answering or not, which is what the cache can give; an address that moves to another node is not modelled;
   - a second device for the agent, and a second agent; the device's name (bind updates it, never the node);
     members.bindings, the listing /agents shows, which only reads;
   - SQLite itself: each transaction is atomic, and a write never fails (one that waits for the lock longer than
     the connection's timeout fails, and nothing is written).

   The four invariants above that are not in Member.cfg, with TLC's traces (tests/test_member_states.py shows each on
   the real app):
   - OwnDeviceNeverAllows: Bind(whois not answering) then DecideRead(allow, laptop, whois answering): Ben's agent
     can allow itself from the laptop, its node then addr:<ip> and the request's node:<StableID> (or the other way);
   - AllowedSinceLetIn: his agent approved, Ben revoked and let in again: it writes as him with no word from him;
   - RunsOnlyForActive: Ben's run under way, Ben revoked: the run's calls act for him until it ends;
   - PendingOnlyBeforeLetIn: Ben let in, then set to pending (the raw API; members.html never offers it). *)
=============================================================================
