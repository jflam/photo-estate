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
                                            when a value is not one of these, or it would leave no sysadmin;
                                            a pending member let in or turned away closes their request's items
  first_start(con, logins, sysadmin=None) -> [Member]   no members yet: these logins become active members
  demo(con) -> [Member]                     the demo's three members (in its scratch catalog)
  ROLES, STATUSES, POLICIES

Words (docs/notes/MULTIPLAYER-PLAN.md). A member is not a `persons` row, which is someone *in* photos
(faces.py); a member may be linked to one. A member is never deleted: revoked is a status (and triggers
refuse a DELETE of members or member_logins: core/schema.py). Logins are kept lower-case. Roles are
sysadmin and member (the plan's viewer is for devices, later). The journal kinds written here
(journal.append), each with the subject member:<id>: member.added and member.requested (data: the
login), member.changed (data: {field: [before, after]}).
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

import journal
import member_inbox

ROLES = ("sysadmin", "member")
STATUSES = ("pending", "active", "revoked")
POLICIES = ("everything", "review", "review_flagged")
COLS = "m.id, m.name, m.role, m.status, m.upload_policy, m.person_id, m.kind, m.joined_at"
DEMO = (("Ada", "sysadmin", "everything"), ("Ben", "member", "review"), ("Cleo", "member", "review_flagged"))


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
    actor). A login that is already a member's, in any status, is answered with that member."""
    login = norm(login)
    if not login:
        raise ValueError("no login: a request comes through `tailscale serve`, which says who it is")
    with con:
        if got := lookup(con, login):
            return got
        name = " ".join(str(name or "").split())[:80] or login.split("@")[0]
        m = _add(con, name, login, "member", "pending", "review", None, entry="member.requested")
        member_inbox.put(con, member_inbox.sysadmins(con), "member_request", f"member:{m.id}", f"{name} asks to be let in ({login})")
        return m


FIELDS = ("name", "role", "status", "upload_policy", "person_id")


def change(con: sqlite3.Connection, mid: int, actor: int | None, **fields) -> Member:
    """Change some of a member's FIELDS. Approving is status active (joined_at is set the first time),
    revoking is status revoked. One member.changed entry for what changed (none when nothing did).
    ValueError for an unknown member, field or value, a person_id that is no person, or a change that
    would leave the estate with no active sysadmin."""
    if bad := set(fields) - set(FIELDS):
        raise ValueError(f"not a member field: {', '.join(sorted(bad))}")
    with con:
        m = get(con, mid)
        if m is None:
            raise ValueError("no such member")
        new = {**asdict(m), **fields}
        new["name"] = " ".join(str(new["name"] or "").split())[:80] or m.name
        _check(new["role"], new["status"], new["upload_policy"])
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
    got = get(con, mid)
    assert got is not None
    return got


def first_start(con: sqlite3.Connection, logins, sysadmin: str | None = None) -> list[Member]:
    """The first start of a catalog with no members: `logins` (--upload-user) become active members who
    may upload everything, as they could before; `sysadmin` (--sysadmin), or else the first login, is the
    sysadmin. Nothing happens once there is any member."""
    wanted = list(dict.fromkeys(norm(x) for x in ([sysadmin] if sysadmin else []) + list(logins) if norm(x)))
    with con:
        if not wanted or con.execute("SELECT 1 FROM members LIMIT 1").fetchone():
            return []
        return [_add(con, login.split("@")[0], login, "sysadmin" if i == 0 else "member", "active", "everything", None)
                for i, login in enumerate(wanted)]


def demo(con: sqlite3.Connection) -> list[Member]:
    """The demo's members, made in its scratch catalog at start: a sysadmin and two members, each with
    the lower-case name as its login (X-Demo-Member: ben)."""
    with con:
        if con.execute("SELECT 1 FROM members LIMIT 1").fetchone():
            return []
        return [_add(con, name, name.lower(), role, "active", policy, None) for name, role, policy in DEMO]
