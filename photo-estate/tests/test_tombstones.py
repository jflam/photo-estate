"""Tombstones, the phone gate's opt-outs (multiplayer phase 6: private.py, admin.py), on the demo library in process
as its members Ada (the sysadmin), Ben and Cleo: the endpoints (a member's own only, journaled once a call, lifted
and opted out again); an upload refused both ways (with X-Content-SHA256 before its body is read, over a real
socket that never sends the body; without it, hashed and dropped from staging, so nothing reaches the inbox); an
opt-out refusing only its member's copy; a purge refusing everyone's and never lifted; the phone's screenshot mark
reaching the sidecar and the hold.

  uv run pytest tests/test_tombstones.py
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import socket
import sqlite3
import sys
import threading
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed  # noqa: E402
import estate  # noqa: E402
import private  # noqa: E402
from photo_app import PhotoApp  # noqa: E402
from test_multiplayer import ADA, BEN, CLEO, call, mid  # noqa: E402

MODEL = embed.DEFAULT_MODEL


@pytest.fixture
def app(tmp_path):
    a = embed.parser().parse_args(["serve", "--demo", "--no-pregenerate"])
    app = PhotoApp(a)             # start() is not called: no model, no threads; its own scratch catalog
    app.admin.inbox = tmp_path / "iphone"
    return app


def jpeg(seed: int, size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, ((seed * 70) % 256, (seed * 30) % 256, (seed * 110) % 256)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def journal(app, kind: str) -> list[tuple]:
    con = sqlite3.connect(app.a.db)
    try:
        return [(a, s, json.loads(d)) for a, s, d in con.execute("SELECT actor, subject, data FROM journal WHERE kind=? ORDER BY id", (kind,))]
    finally:
        con.close()


def files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.exists() else []


def upload(app, who, name: str, data: bytes, **headers):
    return app.request("PUT", f"/api/upload/2026/11/14/{name}", headers={**who, **headers}, body=data)


class Unread(io.RawIOBase):
    """A body that must not be read."""
    def read(self, n=-1):
        raise AssertionError("the body was read")


def test_the_endpoints_are_each_members_own(app):
    a, b, c = (sha(jpeg(i)) for i in (1, 2, 3))
    assert call(app, "GET", "/api/tombstones", BEN) == {"tombstones": []}
    assert call(app, "POST", "/api/tombstones", BEN, {"add": [a, b, a]}) == {"opted_out": 2, "lifted": 0, "changed": True}
    assert call(app, "GET", "/api/tombstones", BEN)["tombstones"] == sorted([a, b])
    assert call(app, "GET", "/api/tombstones", CLEO)["tombstones"] == [], "Ben's opt-outs are his"
    assert call(app, "POST", "/api/tombstones", CLEO, {"lift": [a]})["changed"] is False, "only their member lifts them"
    assert call(app, "POST", "/api/tombstones", BEN, {"add": [a]})["changed"] is False, "already standing"
    assert call(app, "POST", "/api/tombstones", BEN, {"lift": [a], "add": [c]}) == {"opted_out": 1, "lifted": 1, "changed": True}
    assert call(app, "GET", "/api/tombstones", BEN)["tombstones"] == sorted([b, c])
    assert call(app, "POST", "/api/tombstones", BEN, {"add": [a]})["opted_out"] == 1, "opted out again, after a lift"
    ben = mid(app, BEN)
    assert journal(app, "tombstones.changed") == [(ben, "photos", {"opted_out": 2, "lifted": 0}),
                                                  (ben, "photos", {"opted_out": 1, "lifted": 1}), (ben, a, {"opted_out": 1, "lifted": 0})]
    con = sqlite3.connect(app.a.db)
    assert con.execute("SELECT count(*) FROM tombstones WHERE member_id=?", (ben,)).fetchone()[0] == 3, "a lift is a column, never a delete"
    con.close()
    for bad in ({"add": ["NOTAHASH"]}, {"add": a}, {"add": [a], "lift": [a]}, {"lift": [a.upper()]}):
        call(app, "POST", "/api/tombstones", BEN, bad, expect=400)
    call(app, "POST", "/api/tombstones", BEN, [a], expect=400)


def test_without_the_header_the_bytes_are_hashed_and_dropped_from_staging(app):
    """The system uploader's way (its extension never reads the file): Ben's opted-out bytes are read, hashed and
    thrown away; Cleo's copy of the same bytes is hers to share."""
    inbox = app.admin.inbox
    data = jpeg(7)
    call(app, "POST", "/api/tombstones", BEN, {"add": [sha(data)]})
    r = upload(app, BEN, "IMG_7.JPG", data, **{"X-Batch": "b1/1", "X-Taken": "2026-11-14T10:00:00Z"})
    assert (r.status, r.json(), r.headers.get("X-Upload-Refused")) == (200, {"status": "refused: opted_out"}, "opted_out")
    assert files(inbox) == [] and files(private_staging(inbox)) == [], "nothing in the inbox, nor left in staging"
    assert app.admin.arrivals.empty(), "nothing is prepared (thumbnail, embedding) from it"
    r = upload(app, CLEO, "IMG_7.JPG", data)
    assert r.status == 201, "an opt-out is Ben's: Cleo's copy is not refused"
    assert files(inbox) == [f"m{mid(app, CLEO)}/2026/11/14/IMG_7.JPG"]
    assert upload(app, BEN, "IMG_8.JPG", jpeg(8)).status == 201, "his other photos go through"


def private_staging(inbox: Path) -> Path:
    return inbox.with_name(inbox.name + ".partial")


def test_with_the_header_the_body_is_never_read(app):
    """The in-app uploader's way: X-Content-SHA256 names opted-out bytes, and the answer comes before the body."""
    data, ben = jpeg(9), mid(app, BEN)
    call(app, "POST", "/api/tombstones", BEN, {"add": [sha(data)]})
    got, close = app.admin.upload("PUT", "/api/upload/2026/11/14/IMG_9.JPG",
                                  {"Content-Length": str(len(data)), "X-Content-SHA256": sha(data).upper()}, Unread(),
                                  "ben", True, app.folder_of(app.roster["ben"]), ben)
    assert got[0] == 200 and json.loads(got[1]) == {"status": "refused: opted_out"} and got[4] == {"X-Upload-Refused": "opted_out"}
    assert close, "the body is still on the wire: the connection closes"
    assert files(app.admin.inbox) == [] and files(private_staging(app.admin.inbox)) == []
    # Cleo's, with the same header, is read and kept
    got, close = app.admin.upload("PUT", "/api/upload/2026/11/14/IMG_9.JPG",
                                  {"Content-Length": str(len(data)), "X-Content-SHA256": sha(data)}, io.BytesIO(data),
                                  "cleo", True, app.folder_of(app.roster["cleo"]), mid(app, CLEO))
    assert got[0] == 201 and not close
    # over a real socket: the headers alone, and the answer comes back with no body sent at all
    srv = app.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=10)
        s.sendall((f"PUT /api/upload/2026/11/14/IMG_9b.JPG HTTP/1.1\r\nHost: x\r\nX-Demo-Member: ben\r\n"
                   f"Content-Length: {len(data)}\r\nX-Content-SHA256: {sha(data)}\r\n\r\n").encode())
        answer = b""
        while b"\r\n\r\n" not in answer or not answer.rstrip().endswith(b"}"):
            chunk = s.recv(4096)
            if not chunk:
                break
            answer += chunk
        s.close()
    finally:
        srv.shutdown()
    head = answer.split(b"\r\n\r\n")[0].decode()
    assert head.startswith("HTTP/1.1 200") and "X-Upload-Refused: opted_out" in head, head
    assert not (app.admin.inbox / f"m{ben}").exists()


def test_a_purge_refuses_everyone_and_is_never_lifted(app):
    data = jpeg(11)
    con = sqlite3.connect(app.a.db)
    con.execute("INSERT INTO tombstones(sha256, member_id, why, at) VALUES (?, 0, 'purged', 't')", (sha(data),))
    con.commit()
    con.close()
    for i, who in enumerate((ADA, BEN, CLEO)):
        r = upload(app, who, f"P{i}.JPG", data)
        assert (r.status, r.headers.get("X-Upload-Refused")) == (200, "purged"), who
        r = upload(app, who, f"Q{i}.JPG", data, **{"X-Content-SHA256": sha(data)})
        assert (r.status, r.headers.get("X-Upload-Refused")) == (200, "purged"), who
    assert files(app.admin.inbox) == []
    for who in (ADA, BEN):
        assert call(app, "POST", "/api/tombstones", who, {"lift": [sha(data)]})["changed"] is False
        assert call(app, "GET", "/api/tombstones", who)["tombstones"] == [], "a purge is nobody's opt-out"
    assert upload(app, BEN, "P9.JPG", data).headers.get("X-Upload-Refused") == "purged"
    # an opt-out of a purged photo is a row of its own, and lifting it leaves the purge standing
    call(app, "POST", "/api/tombstones", BEN, {"add": [sha(data)]})
    call(app, "POST", "/api/tombstones", BEN, {"lift": [sha(data)]})
    assert upload(app, BEN, "P10.JPG", data).headers.get("X-Upload-Refused") == "purged"


def test_an_old_phones_upload_with_no_new_header_is_as_before(app):
    data = jpeg(12)
    r = upload(app, ADA, "IMG_12.JPG", data, **{"X-Taken": "2026-11-14T10:00:00Z", "X-Favorite": "true"})
    assert r.status == 201 and "X-Upload-Refused" not in r.headers
    side = next(app.admin.inbox.rglob("*.xmp")).read_text()
    assert "<xmp:Rating>5<" in side and "screenshot" not in side
    assert upload(app, ADA, "IMG_12.JPG", data).status == 200, "sent again: exists"


def test_the_phones_screenshot_mark_reaches_the_sidecar_and_the_hold(app, tmp_path):
    """X-Screenshot (PhotoKit's screenshot subtype) is a keyword in the sidecar the server writes; ingest reads it
    and `private.py check` holds the photo for it, though its vector looks like a landscape and its size and name
    are a camera's. The same kind of photo without the mark is let go."""
    inbox, store = app.admin.inbox, tmp_path / "store"
    shot, plain = jpeg(21, (640, 480)), jpeg(22, (640, 480))
    assert upload(app, BEN, "IMG_21.JPG", shot, **{"X-Screenshot": "1", "X-Taken": "2026-11-14T10:00:00Z"}).status == 201
    assert upload(app, BEN, "IMG_22.JPG", plain, **{"X-Taken": "2026-11-14T10:00:01Z"}).status == 201
    side = (inbox / f"m{mid(app, BEN)}/2026/11/14/IMG_21.JPG.xmp").read_text()
    assert "<rdf:li>screenshot</rdf:li>" in side and 'xmlns:dc="http://purl.org/dc/elements/1.1/"' in side
    estate.cmd_ingest(argparse.Namespace(db=app.a.db, store=str(store), sources=[str(inbox)], force=True, facts=None,
                                         delete_after=False, progress=False, uploads=True))
    con = sqlite3.connect(app.a.db)
    landscape = app.live.cat.M[0]
    for data in (shot, plain):
        v = np.asarray(landscape, np.float32)
        con.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?,NULL,'2026-11-22')",
                    (sha(data), MODEL, v.shape[0], (v / np.linalg.norm(v)).astype(np.float16).tobytes()))
    con.commit()
    assert private.judge(con, MODEL, [sha(shot), sha(plain)])[sha(shot)][0] == ["phone_screenshot"]
    assert private.check(con, MODEL, out=lambda *a: None) == {"checked": 2, "held": 1, "waiting": 0}
    assert con.execute("SELECT sha256, why FROM private").fetchall() == [(sha(shot), "held")]
    con.close()
