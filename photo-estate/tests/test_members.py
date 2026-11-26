"""members.py on a scratch catalog: looking a login up, adding, asking to be let in, changing a role or
status, linking to a person, the first start from --upload-user, the demo's members, and the journal
entry each change appends in the same transaction (none for a change that is refused).

  uv run pytest tests/test_members.py
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import members  # noqa: E402
from core import schema  # noqa: E402


@pytest.fixture
def con(tmp_path):
    con = sqlite3.connect(tmp_path / "estate.db")
    schema.ensure(con, "members", "faces")
    yield con
    con.close()


def entries(con):
    return [(actor, kind, subject, json.loads(data)) for actor, kind, subject, data in
            con.execute("SELECT actor, kind, subject, data FROM journal ORDER BY id")]


def test_first_start_makes_the_upload_users_members_and_the_first_the_sysadmin(con):
    made = members.first_start(con, ["Owner@Example.com", "kid@example.com", "owner@example.com"])
    assert [(m.name, m.role, m.status, m.upload_policy, m.login) for m in made] == [
        ("owner", "sysadmin", "active", "everything", "owner@example.com"),
        ("kid", "member", "active", "everything", "kid@example.com")]
    assert all(m.joined_at for m in made)
    assert members.lookup(con, " OWNER@example.com ").id == made[0].id      # logins are matched lower-case
    assert members.lookup(con, "nobody@example.com") is None and members.lookup(con, "") is None
    assert members.first_start(con, ["third@example.com"]) == []          # only while there are no members
    assert [e[1] for e in entries(con)] == ["member.added", "member.added"]
    assert members.sysadmin(con).id == made[0].id
    assert set(members.by_login(con)) == {"owner@example.com", "kid@example.com"}


def test_first_start_with_sysadmin_named(con):
    made = members.first_start(con, ["a@example.com", "b@example.com"], sysadmin="b@example.com")
    assert [(m.login, m.role) for m in made] == [("b@example.com", "sysadmin"), ("a@example.com", "member")]


def test_first_start_with_nobody_makes_nobody(con):
    assert members.first_start(con, []) == []
    assert members.everyone(con) == [] and members.sysadmin(con) is None


def test_asking_to_be_let_in_makes_a_pending_member_once(con):
    members.first_start(con, ["owner@example.com"])
    m = members.request(con, "Dana@example.com", "  Dana   Smith ")
    assert (m.name, m.status, m.role, m.upload_policy, m.joined_at) == ("Dana Smith", "pending", "member", "review", None)
    assert members.request(con, "dana@example.com", "Someone else") == m   # asking again changes nothing
    assert members.request(con, "eve@example.com").name == "eve"           # no name: the login's own
    with pytest.raises(ValueError):
        members.request(con, "  ")
    assert entries(con)[1] == (m.id, "member.requested", f"member:{m.id}",
                               {"login": "dana@example.com", "name": "Dana Smith", "role": "member", "status": "pending"})
    listed = members.everyone(con)
    assert [x["name"] for x in listed] == ["Dana Smith", "eve", "owner"]   # waiting first
    assert listed[0]["logins"][0]["login"] == "dana@example.com" and listed[0]["person"] is None


def test_approving_revoking_and_changing_a_role_are_journaled(con):
    owner = members.first_start(con, ["owner@example.com"])[0]
    dana = members.request(con, "dana@example.com", "Dana")
    let_in = members.change(con, dana.id, owner.id, status="active")
    assert let_in.status == "active" and let_in.joined_at
    up = members.change(con, dana.id, owner.id, role="sysadmin", upload_policy="review_flagged", name="Dana S.")
    assert (up.role, up.upload_policy, up.name, up.joined_at) == ("sysadmin", "review_flagged", "Dana S.", let_in.joined_at)
    assert members.change(con, dana.id, owner.id, role="sysadmin") == up      # nothing changed: no entry
    out = members.change(con, dana.id, owner.id, status="revoked")
    assert out.status == "revoked" and not out.active and not out.is_sysadmin
    assert members.lookup(con, "dana@example.com").status == "revoked"   # revoked, never deleted
    changed = [e for e in entries(con) if e[1] == "member.changed"]
    assert changed == [
        (owner.id, "member.changed", f"member:{dana.id}", {"status": ["pending", "active"]}),
        (owner.id, "member.changed", f"member:{dana.id}", {"name": ["Dana", "Dana S."], "role": ["member", "sysadmin"],
                                                           "upload_policy": ["review", "review_flagged"]}),
        (owner.id, "member.changed", f"member:{dana.id}", {"status": ["active", "revoked"]})]


def test_refused_changes_change_nothing_and_journal_nothing(con):
    owner = members.first_start(con, ["owner@example.com"])[0]
    before = entries(con)
    for fields in ({"role": "viewer"}, {"role": "king"}, {"status": "gone"}, {"upload_policy": "all"},
                   {"person_id": 99}, {"email": "x"}):
        with pytest.raises(ValueError):
            members.change(con, owner.id, owner.id, **fields)
    with pytest.raises(ValueError, match="no such member"):
        members.change(con, 999, owner.id, status="active")
    # the last active sysadmin can be neither revoked nor made a member
    for fields in ({"status": "revoked"}, {"role": "member"}):
        with pytest.raises(ValueError, match="active sysadmin"):
            members.change(con, owner.id, owner.id, **fields)
    assert members.get(con, owner.id).is_sysadmin and entries(con) == before


def test_a_second_sysadmin_lets_the_first_step_down(con):
    owner = members.first_start(con, ["owner@example.com"])[0]
    other = members.add(con, "Other", "other@example.com", role="sysadmin", status="active", actor=owner.id)
    assert members.change(con, owner.id, other.id, role="member").role == "member"
    assert members.sysadmin(con).id == other.id
    with pytest.raises(ValueError, match="already"):
        members.add(con, "Dup", "OTHER@example.com")


def test_linking_a_member_to_a_person(con):
    owner = members.first_start(con, ["owner@example.com"])[0]
    con.execute("INSERT INTO persons(id, name) VALUES (7, 'John Lam')")
    con.commit()
    assert members.change(con, owner.id, owner.id, person_id=7).person_id == 7
    assert members.everyone(con)[0]["person"] == "John Lam"
    assert members.change(con, owner.id, owner.id, person_id=None).person_id is None
    with pytest.raises(ValueError, match="no such person"):
        members.change(con, owner.id, owner.id, person_id="7")


def test_demo_members(con):
    made = members.demo(con)
    assert [(m.name, m.login, m.role, m.status) for m in made] == [
        ("Ada", "ada", "sysadmin", "active"), ("Ben", "ben", "member", "active"), ("Cleo", "cleo", "member", "active")]
    assert members.demo(con) == []


def test_the_tables_are_in_the_catalog_areas():
    assert "members" in schema.CATALOG_AREAS
    assert ("acting_for", "INT") in schema.ADDED["tasks"]["agent_tasks"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
