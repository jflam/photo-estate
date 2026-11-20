#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Tasks (tasks.py), end to end on the demo library, with a stand-in for Claude Code: a small program
that takes the same arguments, uses the library's MCP tools like the real agent, and prints what it
does in the same stream-json form. Checks that a task is started with every built-in tool off and
only the library's tools allowed, that its sign-in file reaches it (and nothing else sees it), that
the change set it makes is tied to the task, that its steps can be read while it runs and after, and
that stopping, failing and queueing work. Starts the real app (`embed.py serve --demo`)."""
import json, os, socket, stat, subprocess, tempfile, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time, urllib.request
argv = sys.argv[1:]
arg = lambda flag: argv[argv.index(flag) + 1]
out = lambda o: print(json.dumps(o), flush=True)
prompt, conf = arg("-p"), json.loads(arg("--mcp-config"))["mcpServers"]["photo-estate"]
ok = (arg("--tools") == "" and "--strict-mcp-config" in argv and arg("--allowedTools") == "mcp__photo-estate"
      and "one change set" in arg("--system-prompt") and os.listdir(".") == [])
if not ok:
    sys.exit("not started the way an agent must be")
n = 0
def rpc(method, params=None):
    global n; n += 1
    req = urllib.request.Request(conf["url"], json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params or {}}).encode(),
                                 {"Content-Type": "application/json", **conf["headers"]})
    return json.load(urllib.request.urlopen(req, timeout=60))["result"]
def tool(name, args):
    out({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__photo-estate__" + name, "input": args}]}})
    got = rpc("tools/call", {"name": name, "arguments": args})
    if got["isError"]:
        out({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": got["content"]}]}})
    return got.get("structuredContent")
out({"type": "system", "subtype": "init", "mcp_servers": [{"name": "photo-estate", "status": "connected"}]})
if "sleep" in prompt:
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Thinking for a long time"}]}})
    time.sleep(120)
if "crash" in prompt:
    sys.exit("the model is not answering")
names = [t["name"] for t in rpc("tools/list")["tools"]]
seed = tool("search_photos", {"limit": 1})["photos"][0]["id"]
tool("search_photos", {"people": ["Nobody Atall"]})
made = tool("create_change_set", {"title": "From a task", "prompt": prompt})
tool("amend_change_set", {"change_set": made["change_set"], "add_search": {"like": seed, "max": 12}, "reason": "looks like the newest"})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Proposed twelve photos."}]}})
out({"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "total_cost_usd": 0.01,
     "result": f"12 photos at {made['review']}. token:{'yes' if os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') == 'sekrit-value' else 'no'} tasktools:{'submit_task' in names}"})
'''


def http(path, body=None, expect=200, headers=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code, got = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, got = e.code, e.read()
    assert code == expect, f"{path}: {code}, expected {expect}: {got[:300]!r}"
    return json.loads(got) if got else None


def tool(name, args=None, error=False, headers=None):
    got = http("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}, headers=headers)
    if "error" in got:
        return got["error"]
    assert got["result"]["isError"] == error, got
    return got["result"]["content"][0]["text"] if error else got["result"]["structuredContent"]


def wait(tid, until=("done", "failed", "cancelled")):
    for _ in range(300):
        t = http(f"/api/tasks/{tid}")
        if t["status"] in until:
            return t
        time.sleep(0.1)
    raise AssertionError(f"task {tid} is still {t['status']}")


tmp = Path(tempfile.mkdtemp(prefix="photo-estate-test-tasks-"))
fake, env_file = tmp / "claude", tmp / "agent.env"
fake.write_text(FAKE)
fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
env_file.write_text("# the agent's sign-in\nCLAUDE_CODE_OAUTH_TOKEN=sekrit-value\n")
app = subprocess.Popen(["uv", "run", "--quiet", str(ROOT / "embed.py"), "serve", "--demo", "--port", str(PORT), "--no-pregenerate",
                        "--agent-cmd", str(fake), "--agent-env", str(env_file)],
                       env={**os.environ, "HF_HUB_OFFLINE": "1"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(600):
        try:
            total = http("/api/info")["total"]
            break
        except (urllib.error.URLError, ConnectionError):
            assert app.poll() is None, app.stdout.read().decode()[-2000:]
            time.sleep(0.2)
    assert http("/api/tasks") == {"available": True, "why": "", "tasks": []}
    http("/api/tasks", {"prompt": "  "}, expect=400)

    # a request from a chat client: handed over, worked on, and ending in a change set to review
    sub = tool("submit_task", {"request": "hide the ones like the newest"})
    tid = sub["task"]
    assert sub["status"] == "queued" and sub["request"] == "hide the ones like the newest"
    t = wait(tid)
    assert t["status"] == "done" and t["turns"] == 4 and t["started_at"] and t["finished_at"], t
    assert t["result"] == "12 photos at /changes/1. token:yes tasktools:False", "its sign-in reached it; it is not offered the task tools"
    assert t["change_sets"] == [{"id": 1, "title": "From a task", "photos": 12, "review": "/changes/1"}]
    kinds = [(e["kind"], e["text"].split(" ")[0]) for e in t["events"]]
    assert kinds == [("tool", "search_photos"), ("tool", "search_photos"), ("error", "nobody"), ("tool", "create_change_set"),
                     ("tool", "amend_change_set"), ("say", "Proposed")], kinds
    assert http(f"/api/tasks/{tid}?after={t['events'][3]['id']}")["events"] == t["events"][4:]
    cs = http("/api/changes/1")
    assert (cs["created_by"], cs["n"], cs["applied"], cs["prompt"]) == (f"task:{tid}", 12, 0, "hide the ones like the newest")
    assert http("/api/info")["total"] == total, "a task proposes; it hides nothing"
    got = tool("get_task", {"task": tid})
    assert (got["status"], got["report"], got["change_sets"]) == ("done", t["result"], t["change_sets"]) and len(got["events"]) == 6
    assert tool("get_task", {"task": tid, "after": got["last_event"]})["events"] == []
    assert "no such task" in tool("get_task", {"task": 99}, error=True)
    assert "sekrit" not in json.dumps(http("/api/tasks")) + json.dumps(t), "the sign-in is in no answer"

    # the agent a task runs is told apart by its header, only from this computer, and has no task tools
    as_task = {"X-Estate-Task": "7"}
    assert tool("submit_task", {"request": "again"}, headers=as_task)["code"] == -32602
    listed = http("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=as_task)["result"]["tools"]
    assert len(listed) == 13 and not [x for x in listed if "task" in x["name"]]
    proxied = {**as_task, "X-Forwarded-For": "100.64.0.9"}   # through `tailscale serve`: an ordinary caller
    assert len(http("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=proxied)["result"]["tools"]) == 16

    # one at a time: the second waits; a running task can be stopped, and one waiting can be withdrawn
    slow = http("/api/tasks", {"prompt": "sleep on it"}, expect=201)["id"]
    queued = http("/api/tasks", {"prompt": "after the slow one"}, expect=201)["id"]
    third = http("/api/tasks", {"prompt": "never mind"}, expect=201)["id"]
    t = wait(slow, ("running",))
    for _ in range(100):
        t = http(f"/api/tasks/{slow}")
        if t["last"]:
            break
        time.sleep(0.1)
    assert t["last"]["text"] == "Thinking for a long time" and http(f"/api/tasks/{queued}")["status"] == "queued"
    assert http(f"/api/tasks/{third}/cancel", {})["status"] == "cancelled"
    assert tool("cancel_task", {"task": slow})["status"] == "cancelled"
    t = wait(queued)
    assert t["status"] == "done" and t["change_sets"][0]["id"] == 2 and wait(slow)["status"] == "cancelled"
    assert http(f"/api/tasks/{third}")["status"] == "cancelled" and not http(f"/api/tasks/{third}")["started_at"]

    # an agent that dies says why
    t = wait(http("/api/tasks", {"prompt": "crash please"}, expect=201)["id"])
    assert t["status"] == "failed" and "the model is not answering" in t["error"] and t["change_sets"] == []
    assert [x["status"] for x in http("/api/tasks")["tasks"]] == ["failed", "cancelled", "done", "cancelled", "done"]
    assert b"Say what should go" in urllib.request.urlopen(BASE + "/changes").read()
    print("tasks: ok")
finally:
    app.terminate()
    try:
        app.wait(10)
    except subprocess.TimeoutExpired:
        app.kill()
