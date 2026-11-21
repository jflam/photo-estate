// api.js: the one way a page asks the server for JSON.
//
//   api(path)                        GET; the answer's JSON
//   api(path, body)                  POST `body` as JSON (any value but undefined: `{}` posts an empty object)
//   api(path, body, options)         options: signal (AbortSignal), timeout (ms), method, headers, cache, keepalive
//   createApi({ base, fetch })       the same function for paths under `base`, or with another fetch (tests)
//   ApiError                         what a request that did not succeed throws; see below
//   isAbort(error)                   whether an error is a request (or anything else) that was cancelled
//
// A request ends in one of three ways:
//   - the server answered 2xx: its JSON (null for an empty body);
//   - it was cancelled through `signal`: the AbortError itself, untouched, so a caller that cancelled can
//     tell (isAbort) and say nothing;
//   - anything else: an ApiError. `status` is the HTTP status, or 0 when no answer came. `kind` says which:
//       "http"     the server answered, and not with 2xx. `message` is the server's own: the `error` of a
//                  JSON body (http_util.py's {"error": "..."}), else a short text/plain body, else "HTTP 503".
//                  `body` is the parsed JSON, or the text, or null.
//       "network"  no answer (offline, the server is down, the connection broke). status 0.
//       "timeout"  no answer within `timeout` ms. status 0.
//       "parse"    a 2xx whose body is not JSON. `body` is the text.
//     `retryable` is true for the ones worth asking again: network, timeout, and 408, 429, 502, 503, 504.

/** @typedef {"http" | "network" | "timeout" | "parse"} ApiErrorKind */

/**
 * @typedef {object} ApiOptions
 * @property {AbortSignal | null} [signal]  cancels the request: the promise rejects with the AbortError
 * @property {number} [timeout]             ms to wait for the whole answer; none by default
 * @property {string} [method]              default GET, or POST when there is a body
 * @property {Record<string, string>} [headers]
 * @property {RequestCache} [cache]
 * @property {boolean} [keepalive]
 */

/** @typedef {(path: string, body?: unknown, options?: ApiOptions) => Promise<any>} Api */

const RETRYABLE = new Set([408, 429, 502, 503, 504]);

export class ApiError extends Error {
  /**
   * @param {string} message
   * @param {{ kind: ApiErrorKind, status?: number, body?: unknown, url?: string, method?: string, cause?: unknown }} facts
   */
  constructor(message, { kind, status = 0, body = null, url = "", method = "GET", cause }) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "ApiError";
    /** @type {ApiErrorKind} */
    this.kind = kind;
    /** The HTTP status; 0 when no answer came. @type {number} */
    this.status = status;
    /** The answer's body: parsed JSON, else its text, else null. @type {any} */
    this.body = body;
    /** @type {string} */
    this.url = url;
    /** @type {string} */
    this.method = method;
  }

  /** Worth asking again as it is: no answer came, or the server said it is busy or starting. */
  get retryable() {
    return this.kind === "network" || this.kind === "timeout" || RETRYABLE.has(this.status);
  }
}

/**
 * Whether `e` is a cancellation (an AbortController's abort()), not a failure.
 * @param {unknown} e
 * @returns {boolean}
 */
export const isAbort = e => !!e && typeof e === "object" && /** @type {{ name?: unknown }} */ (e).name === "AbortError";

/**
 * JSON if the text is JSON, else undefined.
 * @param {string} text
 * @returns {unknown}
 */
function parse(text) {
  try { return JSON.parse(text); } catch { return undefined; }
}

/**
 * What to tell a person about an answer that was not 2xx.
 * @param {number} status
 * @param {unknown} json   the body as JSON, or undefined
 * @param {string} text    the body as text
 * @param {string} type    its Content-Type
 * @returns {string}
 */
function messageOf(status, json, text, type) {
  const said = json && typeof json === "object" ? /** @type {Record<string, unknown>} */ (json).error : undefined;
  if (typeof said === "string" && said) return said;
  const plain = text.trim();
  if (json === undefined && plain && plain.length <= 300 && /^text\/plain\b/i.test(type)) return plain;
  return `HTTP ${status}`;
}

/**
 * An `api` function. `base` goes in front of every path; `fetch` is the fetch to use (the browser's by
 * default, looked up at each call).
 * @param {{ base?: string, fetch?: typeof fetch }} [options]
 * @returns {Api}
 */
export function createApi({ base = "", fetch: fetcher } = {}) {
  return async function api(path, body, options = {}) {
    const { signal = null, timeout, method = body === undefined ? "GET" : "POST", headers = {}, cache, keepalive } = options;
    const url = base + path, facts = { url, method };
    if (signal?.aborted) throw new DOMException("The request was cancelled", "AbortError");
    // One controller of our own, so the caller's signal and the timeout both stop the request.
    const ctl = new AbortController();
    let timedOut = false;
    const stop = () => ctl.abort();
    signal?.addEventListener("abort", stop, { once: true });
    const timer = timeout ? setTimeout(() => { timedOut = true; ctl.abort(); }, timeout) : null;
    /** @type {RequestInit} */
    const init = { method, signal: ctl.signal, headers: body === undefined ? headers : { "Content-Type": "application/json", ...headers } };
    if (body !== undefined) init.body = JSON.stringify(body);
    if (cache) init.cache = cache;
    if (keepalive) init.keepalive = true;
    try {
      /** @type {Response} */ let r;
      /** @type {string} */ let text;
      try {
        r = await (fetcher || globalThis.fetch)(url, init);
        text = await r.text();
      } catch (e) {
        if (signal?.aborted) {                                           // cancelled by the caller: theirs to know
          throw isAbort(e) ? e : new DOMException("The request was cancelled", "AbortError");
        }
        if (timedOut) throw new ApiError("The server took too long to answer", { ...facts, kind: "timeout", cause: e });
        throw new ApiError("The server did not answer", { ...facts, kind: "network", cause: e });
      }
      const json = text.trim() ? parse(text) : null;
      if (!r.ok) {
        throw new ApiError(messageOf(r.status, json, text, r.headers.get("Content-Type") || ""),
          { ...facts, kind: "http", status: r.status, body: json === undefined ? text : json });
      }
      if (json === undefined) {
        throw new ApiError("The server's answer could not be read", { ...facts, kind: "parse", status: r.status, body: text });
      }
      return json;
    } finally {
      if (timer) clearTimeout(timer);
      signal?.removeEventListener("abort", stop);
    }
  };
}

/** The app's own server, from the site root: `await api("/api/info")`. */
export const api = createApi();
