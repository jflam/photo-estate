#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""The aesthetics pilot's pairwise judge (tools/aesthetics/judge.py): a deterministic, balanced queue whose strata
obey their rules, a judgment log that replays through undo and resume, the report's agreement numbers and
Wilson intervals, and an image route that refuses anything but a known sha. Round two (round2_pairs.py + the
pairs.json mode): disjoint good-photo pairs with their stratum rules, repeats, and the self-consistency report.
Rejecting a photo ends its pair with no pick, keeps the photo out of every later pair, and is undone like an answer;
a later round's pairs leave out every excluded photo.
Synthetic data only."""
import csv, http.client, json, sys, tempfile, threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "aesthetics"))
import judge  # noqa: E402
import round2_pairs as r2  # noqa: E402

N, PER = 300, 20


def synth(seed=1):
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(10, 16))
    vec = centres[rng.integers(0, 10, N)] + 0.9 * rng.normal(size=(N, 16))
    vec = (vec / np.linalg.norm(vec, axis=1, keepdims=True)).astype(np.float32)
    teacher = rng.uniform(1.7, 6.9, N)
    ridge = teacher + rng.normal(0, 0.8, N)
    sha = [f"{i:064x}" for i in range(N)]
    return sha, vec, teacher, ridge


def test_queue():
    sha, vec, teacher, ridge = synth()
    q, notes = judge.build_queue(sha, vec, teacher, ridge, per=PER)
    q2, _ = judge.build_queue(sha, vec, teacher, ridge, per=PER)
    assert q == q2, "deterministic"
    assert len(q) == 4 * PER and not notes, notes
    assert [x["index"] for x in q] == list(range(len(q)))
    # balanced: every prefix of 4k holds k of each stratum, and any prefix is within one of balanced
    for k in range(1, PER + 1):
        assert {s: sum(x["stratum"] == s for x in q[: 4 * k]) for s in judge.STRATA} == dict.fromkeys(judge.STRATA, k)
    keys = [frozenset((x["sha_a"], x["sha_b"])) for x in q]
    assert len(set(keys)) == len(q) and all(len(k) == 2 for k in keys), "no repeats, a != b"
    assert {x["left"] for x in q} == {"a", "b"}
    other, _ = judge.build_queue(sha, vec, teacher, ridge, seed=45, per=PER)
    assert other != q


def test_strata_rules():
    sha, vec, teacher, ridge = synth()
    q, _ = judge.build_queue(sha, vec, teacher, ridge, per=PER)
    ix = {s: i for i, s in enumerate(sha)}
    seen = {}
    for x in q:
        i, j = ix[x["sha_a"]], ix[x["sha_b"]]
        seen.setdefault(x["stratum"], []).append((i, j))
    for i, j in seen["similar"]:
        assert judge.SIM_LO <= float(vec[i] @ vec[j]) <= judge.SIM_HI
    for i, j in seen["disagree"]:
        dt, ds = teacher[i] - teacher[j], ridge[i] - ridge[j]
        assert dt * ds < 0 and abs(dt) >= 0.3 and abs(ds) >= 0.3
    for i, j in seen["good"]:
        assert teacher[i] >= 4.0 and teacher[j] >= 4.0
    # a stratum that cannot supply its share says so
    q, notes = judge.build_queue(sha[:20], vec[:20], teacher[:20], ridge[:20], per=100)
    assert any("only" in n for n in notes) and 0 < len(q) < 400


def answer(path, pair, choice, stratum="random", a="a" * 64, b="b" * 64):
    judge.append_record(path, {"event": "answer", "pair": pair, "stratum": stratum, "sha_a": a, "sha_b": b,
                               "left_sha": a, "choice": choice, "ms": 5})


def test_replay():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / judge.LOG
        assert judge.replay(log) == {}
        answer(log, 0, "a"); answer(log, 1, "b"); answer(log, 2, "same")
        judge.append_record(log, {"event": "undo", "pair": 2})
        assert list(judge.replay(log)) == [0, 1]
        answer(log, 2, "skip")          # answered again after undo
        judge.append_record(log, {"event": "undo", "pair": 1})
        assert {k: v["choice"] for k, v in judge.replay(log).items()} == {0: "a", 2: "skip"}
        with open(log, "a") as f:
            f.write('{"event": "answer", "pa')  # a torn last line is ignored
        assert list(judge.replay(log)) == [0, 2]
        lines = log.read_text().splitlines()
        assert len(lines) == 7, "append-only"


def test_report_and_wilson():
    lo, hi = judge.wilson(8, 10)
    assert abs(lo - 0.4902) < 1e-3 and abs(hi - 0.9433) < 1e-3, (lo, hi)
    assert judge.wilson(0, 0) == (0.0, 1.0)
    A, B, C, D = "a" * 64, "b" * 64, "c" * 64, "d" * 64
    teacher = {A: 5.0, B: 3.0, C: 4.0, D: 4.5}
    ridge = {A: 5.0, B: 6.0, C: 4.0, D: 1.0}   # student prefers B over A; D below C
    ans = [
        dict(stratum="random", sha_a=A, sha_b=B, choice="a"),     # teacher yes, student no
        dict(stratum="random", sha_a=A, sha_b=B, choice="b"),     # teacher no, student yes
        dict(stratum="random", sha_a=C, sha_b=D, choice="b"),     # teacher yes (D 4.5 > C 4.0), student no
        dict(stratum="good", sha_a=A, sha_b=C, choice="a"),       # both yes
        dict(stratum="good", sha_a=A, sha_b=B, choice="same"),    # gap 2.0
        dict(stratum="good", sha_a=C, sha_b=D, choice="same"),    # gap 0.5
        dict(stratum="good", sha_a=C, sha_b=D, choice="same"),    # gap 0.5
        dict(stratum="good", sha_a=A, sha_b=B, choice="skip"),
    ]
    rows = {r["stratum"]: r for r in judge.report_rows(ans, teacher, ridge)}
    assert (rows["random"]["n"], rows["random"]["teacher"], rows["random"]["student"]) == (3, 2, 1)
    assert (rows["good"]["n"], rows["good"]["teacher"], rows["good"]["student"]) == (1, 1, 1)
    assert (rows["overall"]["n"], rows["overall"]["teacher"], rows["overall"]["student"]) == (4, 3, 2)
    assert rows["overall"]["same"] == 3 and rows["overall"]["skip"] == 1 and rows["overall"]["same_gap"] == 0.5
    assert rows["overall"]["teacher_ci"] == judge.wilson(3, 4)
    text = judge.format_report(list(rows.values()))
    assert "<30 pairs" in text and "overall" in text


def test_server_and_routes():
    sha, vec, teacher, ridge = synth()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "img").mkdir()
        (d / "img" / f"{sha[0]}.jpg").write_bytes(b"\xff\xd8fake")
        (d / "secret.jpg").write_bytes(b"nope")
        app = judge.Judge(d, pilot=(sha, vec, teacher, ridge))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), judge.make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def req(method, path, body=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
            c.request(method, path, json.dumps(body) if body is not None else None)
            r = c.getresponse(); data = r.read(); c.close()
            return r.status, data

        assert req("GET", f"/img/{sha[0]}.jpg") == (200, b"\xff\xd8fake")
        for bad in ("/img/../secret.jpg", "/img/..%2Fsecret.jpg", "/img/secret.jpg", f"/img/{'f' * 64}.jpg",
                    f"/img/{'A' * 64}.jpg",f"/img/{sha[1]}.jpg", f"/img/{sha[0]}"):
            assert req("GET", bad)[0] == 404, bad
        st = json.loads(req("POST", "/api/answer", {"index": 0, "choice": "left", "ms": 12})[1])
        assert st["answered"] == 1 and st["pair"]["index"] == 1
        assert req("POST", "/api/answer", {"index": 0, "choice": "left"})[0] == 409, "already answered"
        assert req("POST", "/api/answer", {"index": 1, "choice": "bogus"})[0] == 409
        rec = judge.replay(d / judge.LOG)[0]
        q0 = app.queue[0]
        assert rec["choice"] == q0["left"] and rec["left_sha"] == q0["sha_" + q0["left"]]
        st = json.loads(req("POST", "/api/undo")[1])
        assert st["answered"] == 0 and st["pair"]["index"] == 0
        assert req("GET", "/report")[0] == 200
        assert b"No answers yet" in req("GET", "/report")[1]
        srv.shutdown()


def test_load_pilot_alignment():
    sha, vec, teacher, ridge = synth()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        extra = [f"{N + i:064x}" for i in range(3)]       # in the sample but without a teacher score
        allsha = sha[:5] + extra + sha[5:]
        allvec = np.concatenate([vec[:5], vec[:3], vec[5:]])
        np.savez(d / "sample.npz", sha=np.array(allsha), vec=allvec)
        with open(d / "teacher.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["sha", "score"])
            for s, t in zip(sha, teacher):
                w.writerow([s, t])
        json.dump({"y": teacher.tolist(), "ridge": ridge.tolist(), "mlp": ridge.tolist()}, open(d / "oof.json", "w"))
        s2, v2, t2, r2 = judge.load_pilot(d)
        assert s2 == sha and np.allclose(t2, teacher) and np.allclose(r2, ridge) and v2.shape == (N, 16)
        json.dump({"y": teacher[::-1].tolist(), "ridge": ridge.tolist()}, open(d / "oof.json", "w"))
        try:
            judge.load_pilot(d)
        except AssertionError:
            pass
        else:
            raise SystemExit("misaligned oof.json must be rejected")


# ------------------------------------------------------------------ round two

def synth2(seed=3, n=4000, ndays=150):
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(8, 16))
    vec = centres[rng.integers(0, 8, n)] + 0.7 * rng.normal(size=(n, 16))
    vec = (vec / np.linalg.norm(vec, axis=1, keepdims=True)).astype(np.float32)
    sha = [f"{i:064x}" for i in range(n)]
    score = rng.uniform(2.0, 7.0, n)
    make = np.where(rng.random(n) < 0.15, "", "Apple")
    day = rng.integers(0, ndays, n)
    taken = np.array([f"2020-{1 + d // 28:02d}-{1 + d % 28:02d}T10:00:00+00:00" for d in day])
    taken[rng.random(n) < 0.05] = ""
    return sha, vec, make, taken, score


def test_round2_pair_rules():
    sha, vec, make, taken, score = synth2()
    entries, notes, pool = r2.build_pairs(sha, vec, make, taken, score)
    again, _, _ = r2.build_pairs(sha, vec, make, taken, score)
    assert entries == again, "deterministic"
    assert r2.build_pairs(sha, vec, make, taken, score, seed=47)[0] != entries
    assert not [n for n in notes if "only" in n], notes   # (a note about undated photos is fine)
    assert pool == sum(1 for i in range(len(sha)) if score[i] >= 5.0 and make[i]), "pool: score >= 5 and a make"
    assert [e["index"] for e in entries] == list(range(len(entries))) and len(entries) == 550
    orig = [e for e in entries if e["repeat_of"] is None]
    reps = [e for e in entries if e["repeat_of"] is not None]
    assert (len(orig), len(reps)) == (500, 50)
    assert {s: sum(e["stratum"] == s for e in orig) for s in r2.STRATA} == r2.COUNTS
    ix = {s: i for i, s in enumerate(sha)}
    photos = [s for e in orig for s in (e["sha_a"], e["sha_b"])]
    assert len(photos) == len(set(photos)) == 1000, "no photo in more than one pair"
    perday = {}
    for e in orig:
        i, j = ix[e["sha_a"]], ix[e["sha_b"]]
        assert score[i] >= 5.0 and score[j] >= 5.0 and make[i] and make[j]
        di, dj, sim = taken[i][:10], taken[j][:10], float(vec[i] @ vec[j])
        assert di and dj and e["left_sha"] in (e["sha_a"], e["sha_b"])
        if e["stratum"] == "same_day":
            assert di == dj and sim < 0.92
            perday[di] = perday.get(di, 0) + 1
        elif e["stratum"] == "similar":
            assert di != dj and 0.60 <= sim < 0.92
        else:
            assert di != dj
    assert max(perday.values()) <= 3
    # round-robin interleave of the strata
    assert [e["stratum"] for e in orig[:6]] == ["same_day", "similar", "good_random"] * 2
    assert {e["left_sha"] == e["sha_a"] for e in orig} == {True, False}
    # repeats: same pair, sides swapped, at least 100 later, stratified, one repeat per original
    assert len({e["repeat_of"] for e in reps}) == 50
    for e in reps:
        o = entries[e["repeat_of"]]
        assert o["repeat_of"] is None and e["index"] - o["index"] >= 100
        assert (e["sha_a"], e["sha_b"], e["stratum"]) == (o["sha_a"], o["sha_b"], o["stratum"])
        assert e["left_sha"] != o["left_sha"]
    assert {s: sum(e["stratum"] == s for e in reps) for s in r2.STRATA} == {"same_day": 20, "similar": 15, "good_random": 15}
    # the originals keep their relative order
    assert [e["sha_a"] for e in orig] == [e["sha_a"] for e in r2.build_pairs(sha, vec, make, taken, score, n_repeats=0)[0]]


def test_round2_shortfall_and_helpers():
    sha, vec, make, taken, score = synth2(n=300, ndays=10)
    entries, notes, _ = r2.build_pairs(sha, vec, make, taken, score)
    assert any("same_day: only" in n for n in notes), "10 dates x cap 3 cannot fill 200 same-day pairs"
    same = [e for e in entries if e["stratum"] == "same_day" and e["repeat_of"] is None]
    assert 0 < len(same) <= 30
    assert r2.allocate({"a": 200, "b": 150, "c": 150}, 50) == {"a": 20, "b": 15, "c": 15}
    assert r2.allocate({"a": 3, "b": 0}, 50) == {"a": 3, "b": 0}
    assert r2.day_of("2021-03-04T05:06:07+00:00") == "2021-03-04" and r2.day_of("") is None and r2.day_of("junk") is None


def test_round2_files_and_guard():
    sha, vec, make, taken, score = synth2(n=1500, ndays=100)
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        np.savez(d / "sample.npz", sha=np.array(sha), vec=vec, make=make, taken_at=taken)
        with open(d / "teacher.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["sha", "score"])
            for s, t in zip(sha, score):
                w.writerow([s, t])
            f.write(sha[0][:20])                       # a torn last row, as while the scorer still runs
        entries, _, _ = r2.run(d)
        body = (d / "pairs.json").read_bytes()
        needed = (d / "needed.txt").read_text().split()
        assert needed == sorted({f"{e[k]}.jpg" for e in entries for k in ("sha_a", "sha_b")})
        r2.run(d)
        assert (d / "pairs.json").read_bytes() == body, "idempotent"
        (d / "judgments.jsonl").write_text("{}\n")
        r2.run(d)                                       # identical content is fine
        try:
            r2.run(d, min_score=5.5)                    # different pairs while answers exist: refused
        except SystemExit:
            pass
        else:
            raise AssertionError("must refuse to regenerate pairs.json under existing judgments")
        assert (d / "pairs.json").read_bytes() == body


def r2_entries(rows):
    """rows: (stratum, a, b, left_sha, repeat_of)"""
    return [{"index": k, "stratum": s, "sha_a": a, "sha_b": b, "left_sha": left, "repeat_of": rep}
            for k, (s, a, b, left, rep) in enumerate(rows)]


def test_round2_mode_and_self_consistency():
    P = {c: f"{i:064x}" for i, c in enumerate("abcdefgh")}
    A, B, C, D, E, F, G, H = (P[c] for c in "abcdefgh")
    teacher = {A: 6.0, B: 5.0, C: 5.5, D: 5.2, E: 5.1, F: 5.9, G: 5.3, H: 5.4}
    rows = [("same_day", A, B, A, None), ("similar", C, D, D, None), ("good_random", E, F, E, None),
            ("same_day", G, H, G, None),
            ("same_day", A, B, B, 0), ("similar", C, D, C, 1), ("good_random", E, F, F, 2), ("same_day", G, H, H, 3)]
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "img").mkdir()
        (d / "img" / f"{A}.jpg").write_bytes(b"\xff\xd8fake")
        with open(d / "teacher.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["sha", "score"])
            for s, t in teacher.items():
                w.writerow([s, t])
        json.dump(r2_entries(rows), open(d / "pairs.json", "w"))
        app = judge.Judge(d)                            # no oof.json, no sample.npz needed
        assert len(app.queue) == 8 and app.strata == judge.R2_STRATA and not app.notes
        assert [q["left"] for q in app.queue] == ["a", "b", "a", "a", "b", "a", "b", "b"]
        # the page and the state reveal nothing about strata, scores or repeats
        st = app.state()
        assert set(st) == {"done", "answered", "total", "rejected", "pair", "upcoming"} and set(st["pair"]) == {"index", "left", "right"}
        assert st["total"] == 8
        for word in ("repeat", "stratum", "score", "same_day", "good_random"):
            assert word not in judge.PAGE.lower(), word
        # answers via the app: original 0 picks the left photo (A... left is A), its repeat (left B) picks the right (A): same photo
        assert app.answer(0, "left", 1) and app.answer(4, "right", 1)
        rec = judge.replay(app.log)
        assert rec[0]["choice"] == "a" and rec[4]["choice"] == "a" and rec[4]["repeat_of"] == 0 and "repeat_of" not in rec[0]
        assert app.answer(1, "left", 1)                 # pair 1: left is D -> picks D (b)
        assert app.answer(5, "left", 1)                 # its repeat: left is C -> picks C (a): inconsistent
        assert app.answer(2, "right", 1)                # pair 2: left E -> picks F (b)
        assert app.answer(6, "skip", 1)                 # repeat skipped: excluded
        assert app.answer(3, "same", 1)                 # original "same": excluded
        assert app.answer(7, "left", 1)                 # repeat decided but original was "same": excluded
        ans = judge.replay(app.log)
        assert judge.consistency(ans, app.queue) == (1, 2, 4)
        text = app.report()
        assert "student" not in text
        assert "50.0% (1 of 2; Wilson 95% [" in text and "4 repeats in the queue" in text
        lo, hi = judge.wilson(1, 2)
        assert f"[{lo:.1%}, {hi:.1%}]" in text
        rows_ = {r["stratum"]: r for r in judge.report_rows([r for k, r in ans.items() if k < 4], teacher, None, judge.R2_STRATA)}
        # agreement counts originals only: pairs 0 (A>B yes), 1 (D<C: no), 2 (F>E yes); pair 3 is 'same'
        assert (rows_["overall"]["n"], rows_["overall"]["teacher"], rows_["overall"]["same"]) == (3, 2, 1)
        overall = [l for l in text.splitlines() if l.startswith("overall")][0]
        assert overall.split()[1] == "3" and "66.7%" in overall, overall
        # undo removes the latest answer from the consistency maths (undo repeat 7 then repeat 6, original 3...)
        assert app.undo() and app.undo() and app.undo() and app.undo()   # drops 7, 3, 6, 2 in answer order
        assert judge.consistency(judge.replay(app.log), app.queue) == (1, 2, 4)
        assert app.undo() and app.undo()               # drops 5 then 1
        assert judge.consistency(judge.replay(app.log), app.queue) == (1, 1, 4)
        # image route knows pairs.json's photos only; the server and /report work in this mode
        srv = ThreadingHTTPServer(("127.0.0.1", 0), judge.make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
        c.request("GET", f"/img/{A}.jpg"); assert c.getresponse().status == 200
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
        c.request("GET", f"/img/{'9' * 64}.jpg"); assert c.getresponse().status == 404
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
        c.request("GET", "/report"); r = c.getresponse(); body = r.read()
        assert r.status == 200 and b"Self-consistency" in body
        srv.shutdown()
        # resume: a new app on the same dir sees the standing answers
        assert judge.Judge(d).state()["answered"] == len(judge.replay(app.log))
        # a repeat that is not the swapped original is rejected
        bad = r2_entries(rows[:4] + [("same_day", A, B, A, 0)])
        json.dump(bad, open(d / "pairs.json", "w"))
        try:
            judge.load_pairs(d)
        except AssertionError:
            pass
        else:
            raise AssertionError("an unswapped repeat must be rejected")


def test_round2_with_student_column():
    sha, vec, teacher, ridge = synth()
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        np.savez(d / "sample.npz", sha=np.array(sha), vec=vec)
        with open(d / "teacher.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["sha", "score"])
            for s, t in zip(sha, teacher):
                w.writerow([s, t])
        json.dump({"y": teacher.tolist(), "ridge": ridge.tolist()}, open(d / "oof.json", "w"))
        json.dump(r2_entries([("similar", sha[0], sha[1], sha[0], None), ("similar", sha[0], sha[1], sha[1], 0)]),
                  open(d / "pairs.json", "w"))
        app = judge.Judge(d)
        assert app.answer(0, "left", 1) and app.answer(1, "right", 1)
        text = app.report()
        assert "student agrees" in text and "100.0% (1 of 1;" in text


def test_reject_a_photo():
    P = {c: f"{i:064x}" for i, c in enumerate("abcdef")}
    A, B, C, D, E, F = (P[c] for c in "abcdef")
    teacher = {A: 6.0, B: 5.0, C: 5.5, D: 5.2, E: 5.1, F: 5.9}
    rows = [("same_day", A, B, A, None), ("similar", C, D, D, None), ("good_random", E, F, E, None),
            ("same_day", A, B, B, 0), ("similar", C, D, C, 1)]
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        with open(d / "teacher.csv", "w", newline="") as f:
            csv.writer(f).writerows([["sha", "score"], *teacher.items()])
        json.dump(r2_entries(rows), open(d / "pairs.json", "w"))
        app = judge.Judge(d)
        assert app.state()["rejected"] == 0 and not (d / "rejected.txt").exists()
        # pair 0 shows A on the left: rejecting the right photo rejects B, and pair 0's repeat (3) is never shown
        assert app.answer(0, "reject_right", 5)
        rec = judge.replay(app.log)[0]
        assert rec["choice"] == "reject" and rec["rejected"] == B
        assert (d / "rejected.txt").read_text() == B + "\n"
        st = app.state()
        assert (st["answered"], st["total"], st["rejected"], st["pair"]["index"]) == (1, 4, 1, 1)
        assert not app.answer(0, "left", 1), "a rejected pair is answered"
        # pair 1 shows D on the left: reject it; its repeat (4) goes too, leaving only pair 2
        assert app.answer(1, "reject_left", 5)
        assert (d / "rejected.txt").read_text().split() == sorted([B, D])
        st = app.state()
        assert (st["total"], st["rejected"], st["pair"]["index"], st["upcoming"]) == (3, 2, 2, None)
        assert app.answer(2, "left", 5) and app.state()["done"]
        # a reject is no answer in the report: one decided pair, no consistency pair, and the count is said
        text = app.report()
        overall = [l for l in text.splitlines() if l.startswith("overall")][0]
        assert overall.split()[1] == "1", overall
        assert judge.consistency(judge.replay(app.log), app.queue) == (0, 0, 2)
        assert "Rejected photos" in text and text.rstrip().endswith(": 2")
        # undo takes back the answer, then the reject of D: pair 1 and its repeat are in the queue again
        assert app.undo() and app.undo()
        st = app.state()
        assert (st["answered"], st["total"], st["rejected"], st["pair"]["index"]) == (1, 4, 1, 1)
        assert (d / "rejected.txt").read_text() == B + "\n"
        assert judge.Judge(d).state() == st, "resume"
        assert not app.answer(1, "reject_both", 1)
        # over http, as the page sends it
        srv = ThreadingHTTPServer(("127.0.0.1", 0), judge.make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
        c.request("POST", "/api/answer", json.dumps({"index": 1, "choice": "reject_right", "ms": 3}), {"Content-Type": "application/json"})
        r = c.getresponse(); st = json.loads(r.read())
        assert r.status == 200 and st["rejected"] == 2 and st["pair"]["index"] == 2
        srv.shutdown()
        assert (d / "rejected.txt").read_text().split() == sorted([B, C])
        assert "reject" in judge.PAGE.lower()


def test_round_excludes_photos():
    sha, vec, make, taken, score = synth2()
    base, _, pool = r2.build_pairs(sha, vec, make, taken, score)
    shown = {e[k] for e in base for k in ("sha_a", "sha_b")}
    hidden = set(sha[::7])
    entries, notes, pool2 = r2.build_pairs(sha, vec, make, taken, score, seed=47, exclude=shown | hidden)
    used = {e[k] for e in entries for k in ("sha_a", "sha_b")}
    assert used and not used & (shown | hidden), "no excluded photo in a later round"
    assert pool2 < pool and any("excluded" in n for n in notes)
    assert r2.build_pairs(sha, vec, make, taken, score)[0] == base, "no exclusion: unchanged"
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "x.txt"
        f.write_text(f"{sha[0]}\n{sha[1]}.jpg\n\n")
        assert r2.read_shas(f) == {sha[0], sha[1]}
        f.write_text("not a sha\n")
        try:
            r2.read_shas(f)
        except SystemExit:
            pass
        else:
            raise AssertionError("a file of anything but shas must be refused")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("aesthetics judge: ok")
