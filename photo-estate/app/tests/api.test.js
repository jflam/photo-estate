// api.js against a stub fetch: every way a request can end. `npm test` in photo-estate/app.
import test from "node:test";
import assert from "node:assert/strict";

import { ApiError, createApi, isAbort } from "../api.js";

/**
 * An api whose fetch is `answer` (a Response, or a function of the request), and the requests it saw.
 * @param {Response | ((url: string, init: RequestInit) => Response | Promise<Response>)} answer
 * @param {string} [base]
 */
function stub(answer, base) {
  /** @type {{ url: string, init: RequestInit }[]} */
  const calls = [];
  const fetch = async (/** @type {any} */ url, /** @type {any} */ init) => {
    calls.push({ url, init });
    return typeof answer === "function" ? answer(url, init) : answer;
  };
  return { api: createApi({ fetch: /** @type {any} */ (fetch), base }), calls };
}
const json = (/** @type {unknown} */ body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
/** A fetch that never answers, and rejects as the browser's does when its signal is aborted. */
const hang = (/** @type {string} */ _url, /** @type {RequestInit} */ init) => new Promise((_, reject) => {
  init.signal?.addEventListener("abort", () => reject(new DOMException("This operation was aborted", "AbortError")));
});
/** @returns {Promise<ApiError>} */
async function failure(/** @type {Promise<unknown>} */ p) {
  const e = await p.then(() => assert.fail("the request should have failed"), e => e);
  assert.ok(e instanceof ApiError, `an ApiError, not ${e?.name}: ${e?.message}`);
  return e;
}

test("GET: the answer's JSON", async () => {
  const { api, calls } = stub(json({ total: 104 }));
  assert.deepEqual(await api("/api/info"), { total: 104 });
  assert.equal(calls[0].url, "/api/info");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].init.body, undefined);
  assert.deepEqual(calls[0].init.headers, {});
});

test("a body makes it a POST of JSON; {} is a body", async () => {
  const { api, calls } = stub(() => json({ ok: true }));
  await api("/api/tasks", { prompt: "hide the school photos" });
  await api("/api/changes/3/revert", {});
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, '{"prompt":"hide the school photos"}');
  assert.deepEqual(calls[0].init.headers, { "Content-Type": "application/json" });
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.body, "{}");
});

test("options: method, headers, cache and a base path", async () => {
  const { api, calls } = stub(() => json({}), "/api/intake");
  await api("/jobs/7", undefined, { method: "DELETE", headers: { "X-Why": "test" }, cache: "no-store" });
  assert.equal(calls[0].url, "/api/intake/jobs/7");
  assert.equal(calls[0].init.method, "DELETE");
  assert.deepEqual(calls[0].init.headers, { "X-Why": "test" });
  assert.equal(calls[0].init.cache, "no-store");
});

test("an empty 2xx body is null", async () => {
  assert.equal(await stub(new Response(null, { status: 204 })).api("/x"), null);
  assert.equal(await stub(new Response("", { status: 200 })).api("/x"), null);
});

test("an error with a JSON body: the server's own message, the status, and the parsed body", async () => {
  const { api } = stub(json({ error: "Content-Length must be a whole number", field: "n" }, 400));
  const e = await failure(api("/api/changes", {}));
  assert.equal(e.message, "Content-Length must be a whole number");
  assert.equal(e.status, 400);
  assert.equal(e.kind, "http");
  assert.deepEqual(e.body, { error: "Content-Length must be a whole number", field: "n" });
  assert.equal(e.url, "/api/changes");
  assert.equal(e.method, "POST");
  assert.equal(e.retryable, false);
  assert.ok(e instanceof Error && e.name === "ApiError");
});

test("an error whose JSON has no `error`: the status, and still the body", async () => {
  const e = await failure(stub(json({ state: "starting" }, 503)).api("/api/sync"));
  assert.equal(e.message, "HTTP 503");
  assert.deepEqual(e.body, { state: "starting" });
  assert.equal(e.retryable, true, "503: worth asking again");
});

test("an error that is not JSON: a short text/plain body is the message", async () => {
  const text = "Faces are still loading; try again in a moment.";
  const e = await failure(stub(new Response(text, { status: 503, headers: { "Content-Type": "text/plain" } })).api("/api/people"));
  assert.equal(e.message, text);
  assert.equal(e.status, 503);
  assert.equal(e.body, text);
});

test("an error that is not JSON: HTML, a long body or no body is never the message", async () => {
  const page = "<!doctype html><title>502 Bad Gateway</title>";
  const html = await failure(stub(new Response(page, { status: 502, headers: { "Content-Type": "text/html" } })).api("/x"));
  assert.equal(html.message, "HTTP 502");
  assert.equal(html.body, page);
  const long = await failure(stub(new Response("x".repeat(301), { status: 500, headers: { "Content-Type": "text/plain" } })).api("/x"));
  assert.equal(long.message, "HTTP 500");
  const none = await failure(stub(new Response(null, { status: 404 })).api("/x"));
  assert.equal(none.message, "HTTP 404");
  assert.equal(none.body, null);
  assert.equal(none.retryable, false);
});

test("a 2xx that is not JSON is an error too, not a SyntaxError", async () => {
  const e = await failure(stub(new Response("<html>hello</html>", { status: 200 })).api("/api/stats"));
  assert.equal(e.kind, "parse");
  assert.equal(e.status, 200);
  assert.equal(e.body, "<html>hello</html>");
});

test("no answer: an ApiError with status 0 and the cause", async () => {
  const down = new TypeError("fetch failed");
  const e = await failure(stub(() => { throw down; }).api("/api/info"));
  assert.equal(e.kind, "network");
  assert.equal(e.status, 0);
  assert.equal(e.message, "The server did not answer");
  assert.equal(e.cause, down);
  assert.equal(e.retryable, true);
  assert.equal(isAbort(e), false);
});

test("the connection breaking while the body is read is no answer", async () => {
  const body = new ReadableStream({ start(c) { c.error(new TypeError("terminated")); } });
  const e = await failure(stub(new Response(body, { status: 200 })).api("/api/search"));
  assert.equal(e.kind, "network");
});

test("abort: the AbortError itself, and the request was told to stop", async () => {
  const { api, calls } = stub(hang);
  const ctl = new AbortController();
  const p = api("/api/search?q=kitten", undefined, { signal: ctl.signal });
  ctl.abort();
  const e = await p.then(() => assert.fail("should have been cancelled"), e => e);
  assert.ok(isAbort(e), `an AbortError, not ${e?.name}`);
  assert.ok(!(e instanceof ApiError));
  assert.equal(calls[0].init.signal?.aborted, true);
});

test("abort with a reason of the caller's own is still an AbortError", async () => {
  const ctl = new AbortController();
  const p = stub(hang).api("/x", undefined, { signal: ctl.signal });
  ctl.abort("a newer search");
  assert.ok(isAbort(await p.catch(e => e)));
});

test("a signal aborted beforehand: nothing is sent", async () => {
  const { api, calls } = stub(json({}));
  const ctl = new AbortController();
  ctl.abort();
  assert.ok(isAbort(await api("/x", undefined, { signal: ctl.signal }).catch(e => e)));
  assert.equal(calls.length, 0);
});

test("timeout: an ApiError that says so, not an abort", async () => {
  const { api, calls } = stub(hang);
  const e = await failure(api("/api/stats?refresh=1", undefined, { timeout: 5 }));
  assert.equal(e.kind, "timeout");
  assert.equal(e.status, 0);
  assert.equal(e.retryable, true);
  assert.equal(isAbort(e), false);
  assert.equal(calls[0].init.signal?.aborted, true, "the request itself was stopped");
});

test("an answer in time is not timed out, and leaves no timer or listener behind", async () => {
  const ctl = new AbortController();
  const { api, calls } = stub(() => json({ ok: 1 }));
  assert.deepEqual(await api("/x", undefined, { timeout: 10_000, signal: ctl.signal }), { ok: 1 });
  ctl.abort();     // after the answer: nothing left to stop
  assert.equal(calls[0].init.signal?.aborted, false);
});

test("isAbort", () => {
  assert.equal(isAbort(new DOMException("x", "AbortError")), true);
  assert.equal(isAbort(new Error("x")), false);
  assert.equal(isAbort(null), false);
  assert.equal(isAbort("AbortError"), false);
});
