"""members.py — who the estate knows: its members, the Tailscale logins that are them, their roles and
upload policies (standard library only). Every function takes an open catalog connection; every change
appends its journal entry in the same transaction, and commits.

  Member                                    one member, as a request is told it (frozen)
  lookup(con, login) -> Member | None       the member a login is, whatever its status
  by_login(con) -> {login: Member}          every login, for the app to keep in memory
  sysadmin(con) -> Member | None            the first active sysadmin: whom a newcomer is told to ask
  everyone(con) -> [dict]                   every member with its logins, waiting first (/members)
  persons(con) -> [{id, name}]              the people in photos (faces.py) a member can be linked to
  add(con, name, login=, role=, status=, upload_policy=, actor=) -> Member
  request(con, login, name) -> Member       a login nobody has let in asks to be: a pending member, and an
                                            inbox item for every sysadmin (member_inbox.py: member_request)
  change(con, id, actor, **fields) -> Member   name, role, status, upload_policy, person_id; ValueError
                                            when a value is not one of these, it would leave no sysadmin, or
                                            the member is the estate's agent;
                                            a pending member let in or turned away closes their request's items;
                                            one who is no active sysadmin after it has their member_request and
                                            lens_proposed items done (one made a sysadmin is given none for what
                                            waits already: /members and Albums show it)
  first_start(con, logins, sysadmin=None) -> [Member]   no members yet: these logins become active members
  demo(con) -> [Member]                     the demo's three members (in its scratch catalog)
  agent(con) -> Member                      the estate's agent (kind agent), made the first time it is needed
  family(con) -> [Member]                   the active members who are people (whom a question to the family is for)
  bind(con, member, node, device) -> dict   a member's own agent calling from a device: its binding, made pending
                                            (and a `binding` inbox item) the first time
  bindings(con, member) -> [dict]           the member's own agents, one per device
  decide_binding(con, member, id, allow, node) -> dict   the member allows one (from another device) or turns it off
  node_kind(node) -> str                    what named a device: node (whois), addr, demo, or unknown
  ROLES, STATUSES, POLICIES, AGENT_NAME, BindingError

Words (docs/notes/MULTIPLAYER-PLAN.md). A member is not a `persons` row, which is someone *in* photos
(faces.py); a member may be linked to one. A member is never deleted: revoked is a status (and triggers
refuse a DELETE of members or member_logins: core/schema.py). Logins are kept lower-case. Roles are
sysadmin and member (the plan's viewer is for devices, later). The journal kinds written here
(journal.append), each with the subject member:<id>: member.added and member.requested (data: the
login), member.changed (data: {field: [before, after]}).

The estate's agent (multiplayer phase 7) is a member too: kind `agent`, role member, active, with no login (it
never comes through `tailscale serve`; a run of it is known by the token tasks.py gives it). agent() makes it
the first time something needs it (a run, a question) and it is never deleted. Acting for no member it is the
family archivist; acting for a member it sees what they see (estate_mcp.py). first_start and demo count only
people: a catalog with nothing but the agent has no members yet.

A member's **own agent** (multiplayer phase 8) runs in their own harness and calls /mcp through `tailscale serve`
with their login, so it is not a member row of its own: it is that member's agent on one device, an
`agent_bindings` row. The first call from a device makes it `pending`; the member then allows it (`approved`) or
turns it off (`revoked`), and may turn an approved one off, or allow a revoked one again. Only the member decides,
on the web (POST /api/agents/<id>), never through /mcp, and allowing must come from another device than the
agent's own (decide_binding). What a device is comes from photo_app.PhotoApp.device: the tailnet node the request
came from (tailscale whois on the address `tailscale serve` forwards it from).
Journal: binding.requested {device} (no actor, acting for the member), binding.approved and binding.revoked
{device, was} (by the member), subject binding:<id>.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

import journal
import member_inbox
from core.states import MEMBER_MOVES

ROLES = ("sysadmin", "member")
STATUSES = ("pending", "active", "revoked")
POLICIES = ("everything", "review", "review_flagged")
COLS = "m.id, m.name, m.role, m.status, m.upload_policy, m.person_id, m.kind, m.joined_at"
DEMO = (("Ada", "sysadmin", "everything"), ("Ben", "member", "review"), ("Cleo", "member", "review_flagged"))
AGENT_NAME = "The estate's agent"
PEOPLE = "kind IS NOT 'agent'"      # (SQL) the members who are people


@dataclass(frozen=True)
class Member:
    id: int | None
    name: str
    role: str = "member"
    status: str = "active"
    upload_policy: str = "review"
    person_id: int | None = None
    kind: str = "person"
    joined_at: str | None = None
    login: str = ""          # the login the request came with ("" for this computer)
    local: bool = False      # a request from this computer itself, which is the sysadmin

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def is_sysadmin(self) -> bool:
        return self.active and self.role == "sysadmin"

    def public(self) -> dict:
        """What GET /api/me answers."""
        return asdict(self)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(login: str | None) -> str:
    return (login or "").strip().lower()


def _member(row, login: str = "") -> Member:
    return Member(*row, login=login)


def get(con: sqlite3.Connection, mid: int) -> Member | None:
    row = con.execute(f"SELECT {COLS} FROM members m WHERE id=?", (mid,)).fetchone()
    return _member(row) if row else None


def lookup(con: sqlite3.Connection, login: str) -> Member | None:
    login = norm(login)
    row = con.execute(f"SELECT {COLS} FROM members m JOIN member_logins l ON l.member_id=m.id WHERE l.login=?",
                      (login,)).fetchone() if login else None
    return _member(row, login) if row else None


def by_login(con: sqlite3.Connection) -> dict[str, Member]:
    return {r[-1]: _member(r[:-1], r[-1]) for r in con.execute(
        f"SELECT {COLS}, l.login FROM members m JOIN member_logins l ON l.member_id=m.id")}


def sysadmin(con: sqlite3.Connection) -> Member | None:
    row = con.execute(f"SELECT {COLS} FROM members m WHERE role='sysadmin' AND status='active' ORDER BY id LIMIT 1").fetchone()
    return _member(row) if row else None


def everyone(con: sqlite3.Connection) -> list[dict]:
    logins: dict[int, list[dict]] = {}
    for login, mid, at in con.execute("SELECT login, member_id, added_at FROM member_logins ORDER BY added_at, login"):
        logins.setdefault(mid, []).append({"login": login, "added_at": at})
    persons = dict(con.execute("SELECT id, name FROM persons")) if _has(con, "persons") else {}
    out = []
    for row in con.execute(f"SELECT {COLS} FROM members m ORDER BY CASE status WHEN 'pending' THEN 0"
                           " WHEN 'active' THEN 1 ELSE 2 END, id"):
        m = asdict(_member(row))
        del m["login"], m["local"]
        out.append({**m, "person": persons.get(m["person_id"]), "logins": logins.get(m["id"], [])})
    return out


def persons(con: sqlite3.Connection) -> list[dict]:
    return [{"id": i, "name": n} for i, n in con.execute("SELECT id, name FROM persons ORDER BY name")] if _has(con, "persons") else []


def _has(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _add(con, name: str, login: str | None, role: str, status: str, upload_policy: str, actor: int | None,
         entry: str = "member.added") -> Member:
    t = now()
    mid = con.execute("INSERT INTO members(name, kind, role, upload_policy, joined_at, status) VALUES (?,?,?,?,?,?)",
                      (name, "person", role, upload_policy, t if status == "active" else None, status)).lastrowid
    assert mid is not None
    if login:
        con.execute("INSERT INTO member_logins(login, member_id, added_at) VALUES (?,?,?)", (login, mid, t))
    journal.append(con, mid if entry == "member.requested" else actor, entry, f"member:{mid}",
            {"login": login, "name": name, "role": role, "status": status})
    got = get(con, mid)
    assert got is not None
    return replace(got, login=login or "")


def _begin(con) -> None:
    """Take the write lock now, at the head of a `with con:` block, so what the block reads next is what it writes
    over: no other request changes the member, the login or the binding in between. Python's own BEGIN waits for the
    first write, and a read before it sees a row that may have moved on by then (specs/Member.tla is the model of
    what that let happen: a revoke undone by a rename, a second request from one login answered with an error)."""
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")


def _check(role: str, status: str, upload_policy: str) -> None:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if upload_policy not in POLICIES:
        raise ValueError(f"upload_policy must be one of {', '.join(POLICIES)}")


def add(con: sqlite3.Connection, name: str, login: str | None = None, *, role: str = "member", status: str = "pending",
        upload_policy: str = "review", actor: int | None = None) -> Member:
    """A new member, with a login if given (ValueError if the login is already someone's)."""
    _check(role, status, upload_policy)
    login = norm(login) or None
    with con:
        if login and lookup(con, login):
            raise ValueError(f"{login} is already a member's login")
        return _add(con, str(name).strip()[:80] or (login or "").split("@")[0] or "?", login, role, status, upload_policy, actor)


def request(con: sqlite3.Connection, login: str, name: str | None = None) -> Member:
    """A login nobody has let in asks to be: a pending member, journaled as member.requested (its own
    actor). A login that is already a member's, in any status, is answered with that member (two requests from one
    login at once too: the lookup is made with the write lock held)."""
    login = norm(login)
    if not login:
        raise ValueError("no login: a request comes through `tailscale serve`, which says who it is")
    with con:
        _begin(con)
        if got := lookup(con, login):
            return got
        name = " ".join(str(name or "").split())[:80] or login.split("@")[0]
        m = _add(con, name, login, "member", "pending", "review", None, entry="member.requested")
        member_inbox.put(con, member_inbox.sysadmins(con), "member_request", f"member:{m.id}", f"{name} asks to be let in ({login})")
        return m


FIELDS = ("name", "role", "status", "upload_policy", "person_id")


class Forbidden(ValueError):
    """members.change by someone who is not an active sysadmin (the route answers 403)."""


def change(con: sqlite3.Connection, mid: int, actor: int | None, **fields) -> Member:
    """Change some of a member's FIELDS. Approving is status active (joined_at is set the first time),
    revoking is status revoked. One member.changed entry for what changed (none when nothing did).
    ValueError for an unknown member, field or value, a person_id that is no person, or a change that
    would leave the estate with no active sysadmin, or a status that may not follow the one they have (MEMBER_MOVES:
    nobody goes back to pending). The member is read with the write lock held, so the fields not given are written as
    they are, never as another change at the same moment found them.
    `actor` (members.id) must be an active sysadmin, read under the same lock: Forbidden otherwise, whatever the route
    checked. None is this computer itself before there is any member, and a script.
    Revoking sets the member's approved agents to pending (binding.pending, and a `binding` item for them): let in
    again, they allow each one again, from another device (decide_binding). The route also stops their runs of the
    estate's agent (Tasks.cancel_for)."""
    if bad := set(fields) - set(FIELDS):
        raise ValueError(f"not a member field: {', '.join(sorted(bad))}")
    with con:
        _begin(con)          # (the member as it is when this writes: specs/Member.tla)
        if actor is not None and not ((by := get(con, actor)) and by.is_sysadmin):
            raise Forbidden("only a sysadmin changes a member")
        m = get(con, mid)
        if m is None:
            raise ValueError("no such member")
        if m.kind == "agent":
            raise ValueError("the estate's agent is not changed on /members: it acts for whoever its run is for")
        new = {**asdict(m), **fields}
        new["name"] = " ".join(str(new["name"] or "").split())[:80] or m.name
        _check(new["role"], new["status"], new["upload_policy"])
        if new["status"] != m.status and new["status"] not in MEMBER_MOVES[m.status]:
            raise ValueError(f"a member who is {m.status} is not made {new['status']}")
        if new["person_id"] is not None:
            if not isinstance(new["person_id"], int) or isinstance(new["person_id"], bool) or not _has(con, "persons") \
                    or not con.execute("SELECT 1 FROM persons WHERE id=?", (new["person_id"],)).fetchone():
                raise ValueError("no such person")
        diff = {k: [getattr(m, k), new[k]] for k in FIELDS if getattr(m, k) != new[k]}
        if not diff:
            return m
        joined = m.joined_at or (now() if new["status"] == "active" else None)
        con.execute("UPDATE members SET name=?, role=?, status=?, upload_policy=?, person_id=?, joined_at=? WHERE id=?",
                    (new["name"], new["role"], new["status"], new["upload_policy"], new["person_id"], joined, mid))
        if not con.execute("SELECT 1 FROM members WHERE role='sysadmin' AND status='active'").fetchone():
            raise ValueError("the estate must keep an active sysadmin")   # (the `with` rolls the update back)
        journal.append(con, actor, "member.changed", f"member:{mid}", diff)
        if m.status == "pending" and new["status"] != "pending":     # let in or turned away: the request is decided
            member_inbox.close(con, "member_request", f"member:{mid}")
        if new["status"] == "revoked" and m.status != "revoked" and _has(con, "agent_bindings"):
            # their own agents wait to be allowed again: nobody is asked when the member is let back in (issue #130)
            for bid, device in con.execute("SELECT id, device FROM agent_bindings WHERE member_id=? AND status='approved'",
                                           (mid,)).fetchall():
                con.execute("UPDATE agent_bindings SET status='pending', decided_at=? WHERE id=?", (now(), bid))
                journal.append(con, actor, "binding.pending", f"binding:{bid}", {"device": device, "was": "approved",
                                                                                  "why": "member revoked"}, mid)
                member_inbox.put(con, [mid], "binding", f"binding:{bid}",
                                 f"Your agent on {device} was turned off while you were not let in. Allow it again?")
        if (new["role"], new["status"]) != ("sysadmin", "active"):   # no sysadmin now: nothing is theirs to decide
            for kind, ref in con.execute("SELECT kind, ref FROM inbox_items WHERE member_id=? AND state='open'"
                                         " AND kind IN ('member_request', 'lens_proposed')", (mid,)).fetchall():
                member_inbox.close(con, kind, ref, mid)
        elif (m.role, m.status) != ("sysadmin", "active"):           # a sysadmin from now: what waits is theirs too
            _waiting(con, mid)                                       # (issue #127)
    got = get(con, mid)
    assert got is not None
    return got


def _waiting(con, mid: int) -> None:
    """Someone was made a sysadmin: an item of theirs for each thing already waiting for a sysadmin, worded as the
    others' are: everyone asking to be let in (request), and every filter proposed as an estate default but their
    own (lenses.propose). In change's transaction."""
    for other, name, login in con.execute(
            f"SELECT m.id, m.name, (SELECT login FROM member_logins WHERE member_id=m.id ORDER BY added_at LIMIT 1)"
            f" FROM members m WHERE m.status='pending' AND {PEOPLE} AND m.id != ? ORDER BY m.id", (mid,)).fetchall():
        member_inbox.put(con, [mid], "member_request", f"member:{other}", f"{name} asks to be let in ({login})")
    if not _has(con, "lenses"):
        return
    first = (member_inbox.sysadmins(con) or [None])[0]               # whose a lens with no owner is
    for lid, owner, title in con.execute("SELECT id, owner, title FROM lenses WHERE proposed_at IS NOT NULL"
                                         " AND estate_default=0 ORDER BY id").fetchall():
        owner = owner if owner is not None else first
        if owner != mid:
            name = (con.execute("SELECT name FROM members WHERE id=?", (owner,)).fetchone() or ["Someone"])[0]
            member_inbox.put(con, [mid], "lens_proposed", f"lens:{lid}", f"{name} proposes the filter “{title}” as an estate default")


def first_start(con: sqlite3.Connection, logins, sysadmin: str | None = None) -> list[Member]:
    """The first start of a catalog with no members: `logins` (--upload-user) become active members who
    may upload everything, as they could before; `sysadmin` (--sysadmin), or else the first login, is the
    sysadmin. Nothing happens once there is any member."""
    wanted = list(dict.fromkeys(norm(x) for x in ([sysadmin] if sysadmin else []) + list(logins) if norm(x)))
    with con:
        _begin(con)          # (two apps starting at once on an empty catalog made two first sysadmins: issue #137)
        if not wanted or con.execute(f"SELECT 1 FROM members WHERE {PEOPLE} LIMIT 1").fetchone():
            return []
        return [_add(con, login.split("@")[0], login, "sysadmin" if i == 0 else "member", "active", "everything", None)
                for i, login in enumerate(wanted)]


def demo(con: sqlite3.Connection) -> list[Member]:
    """The demo's members, made in its scratch catalog at start: a sysadmin and two members, each with
    the lower-case name as its login (X-Demo-Member: ben)."""
    with con:
        _begin(con)
        if con.execute(f"SELECT 1 FROM members WHERE {PEOPLE} LIMIT 1").fetchone():
            return []
        return [_add(con, name, name.lower(), role, "active", policy, None) for name, role, policy in DEMO]


def agent(con: sqlite3.Connection) -> Member:
    """The estate's agent: the oldest member of kind agent, made (member.added, by no one) if there is none. Commits."""
    row = con.execute(f"SELECT {COLS} FROM members m WHERE kind='agent' ORDER BY id LIMIT 1").fetchone()
    if row:
        return _member(row)
    with con:
        mid = con.execute("INSERT INTO members(name, kind, role, upload_policy, joined_at, status) VALUES (?,?,?,?,?,?)",
                          (AGENT_NAME, "agent", "member", "everything", now(), "active")).lastrowid
        journal.append(con, None, "member.added", f"member:{mid}", {"name": AGENT_NAME, "kind": "agent"})
    got = get(con, mid) if mid is not None else None
    assert got is not None
    return got


# -- a member's own agent (multiplayer phase 8) -------------------------------------------------------------

BINDING_COLS = "id, member_id, node, device, status, requested_at, decided_at, seen_at"


class BindingError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _binding(row) -> dict:
    return dict(zip(("id", "member", "node", "device", "status", "requested_at", "decided_at", "seen_at"), row))


def bind(con: sqlite3.Connection, member: int, node: str, device: str) -> dict:
    """The binding of `member`'s own agent on this device (`node`): made pending the first time a call comes from
    it, with a binding.requested entry (no actor, acting for the member) and a `binding` inbox item for them; after
    that its last call (seen_at, to the minute) and the device's name as it is now. Commits."""
    t, q = now(), f"SELECT {BINDING_COLS} FROM agent_bindings WHERE member_id=? AND node=?"
    row = con.execute(q, (member, node)).fetchone()
    if row is not None and (row[7] or "")[:16] == t[:16] and row[3] == device[:80]:
        return _binding(row)                 # (seen this minute: nothing to write)
    with con:
        made = row is None and con.execute("INSERT OR IGNORE INTO agent_bindings(member_id, node, device, status, requested_at,"
                                           " seen_at) VALUES (?,?,?,'pending',?,?)", (member, node, device[:80], t, t)).rowcount
        b = _binding(con.execute(q, (member, node)).fetchone())
        if made:
            journal.append(con, None, "binding.requested", f"binding:{b['id']}", {"device": b["device"]}, member)
            member_inbox.put(con, [member], "binding", f"binding:{b['id']}",
                             f"An agent connected as you from {b['device']}. Allow it?")
        elif (b["seen_at"] or "")[:16] != t[:16] or b["device"] != device[:80]:
            con.execute("UPDATE agent_bindings SET seen_at=?, device=? WHERE id=?", (t, device[:80], b["id"]))
            b.update(seen_at=t, device=device[:80])
    return b


def bindings(con: sqlite3.Connection, member: int | None) -> list[dict]:
    """The member's own agents, one per device, the most recently seen first."""
    return [_binding(r) for r in con.execute(f"SELECT {BINDING_COLS} FROM agent_bindings WHERE member_id IS ?"
                                             " ORDER BY seen_at DESC, id DESC", (member,))]


NAMED = ("node", "demo")   # the kinds of node that name a device for certain (node_kind)


def node_kind(node: str) -> str:
    """What named a device, from the node PhotoApp.device gave: "node" (tailscale whois: node:<StableID>), "addr"
    (whois did not answer: addr:<address>, and one device may come from two, its IPv4 and its IPv6), "demo" (--demo:
    demo:<name>, or `local`, this computer), else "unknown" (nothing said which device it was)."""
    kind = node.split(":", 1)[0] if ":" in node else node
    return "demo" if kind in ("demo", "local") else kind if kind in ("node", "addr") else "unknown"


def decide_binding(con: sqlite3.Connection, member: int | None, bid: int, allow: bool, node: str) -> dict:
    """The member allows their agent on a device (approved) or turns it off (revoked), from a request that came from
    `node`. Allowing must come from another device than the agent's own: whatever the agent could send from there
    (it holds the member's login on that device) is no proof that the member said yes. So it is allowed only when
    both the request's device and the agent's were named for certain, the same way (node_kind: by whois, or by the
    demo), and are not the same: a device whois could not name is an address, and one laptop is one node but may be
    an address, another address, or a node, so nothing unnamed can be told apart from the agent's own device.
    Turning off is allowed from anywhere. One binding.approved or binding.revoked entry by the member, and the
    binding's inbox item is done. The binding is read with the write lock held, so two decisions at once end as the
    later one answered. BindingError: 404 for a binding that is not theirs, 403 to allow it from its own device or
    from one that could not be told apart from it."""
    with con:
        _begin(con)          # (the binding as it is when this writes: specs/Member.tla)
        row = con.execute(f"SELECT {BINDING_COLS} FROM agent_bindings WHERE id=? AND member_id IS ?", (bid, member)).fetchone()
        if row is None or member is None:
            raise BindingError(404, "no such agent")
        b = _binding(row)
        if allow and b["node"] == node:
            raise BindingError(403, f"allow it from another device than {b['device']}, the one the agent connected from"
                                    " (your phone, say): anything there could be the agent itself")
        if allow and not (node_kind(node) == node_kind(b["node"]) and node_kind(node) in NAMED):
            raise BindingError(403, "this device, or the one the agent connected from, could not be named (tailscale whois"
                                    " did not answer), so this cannot be told apart from the agent's own device. Try"
                                    " again in a minute. If the agent connected while its device could not be named, let"
                                    " it connect again and allow the new entry")
        status = "approved" if allow else "revoked"
        if b["status"] != status:
            t = now()
            con.execute("UPDATE agent_bindings SET status=?, decided_at=? WHERE id=?", (status, t, bid))
            journal.append(con, member, f"binding.{status}", f"binding:{bid}", {"device": b["device"], "was": b["status"]})
            b.update(status=status, decided_at=t)
        member_inbox.close(con, "binding", f"binding:{bid}")
    return b


def family(con: sqlite3.Connection) -> list[Member]:
    """The active members who are people, oldest first: whom a question to the whole family is for."""
    return [_member(r) for r in con.execute(f"SELECT {COLS} FROM members m WHERE status='active' AND {PEOPLE} ORDER BY id")]
