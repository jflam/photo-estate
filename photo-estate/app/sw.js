// Service worker for the photo app. It does one thing: when the page itself can't be loaded (on a
// phone, usually because Tailscale is off), show a page that says so instead of the browser's error.
// Everything else goes straight to the network: the API is live data, and images are already in the
// HTTP cache (they're content-addressed and marked immutable).
const CACHE = "estate-v3";
const OFFLINE = "/offline";

const page = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f8f6f1" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#09090a" media="(prefers-color-scheme: dark)"><title>Photo Estate</title>
<style>
  :root { color-scheme: light dark; --paper: #f8f6f1; --ink: #1b1915; --muted: #8a8578; }
  @media (prefers-color-scheme: dark) { :root { --paper: #09090a; --ink: #f4f2ee; --muted: #86837c; } }
  body { margin: 0; min-height: 100vh; min-height: 100dvh; display: grid; place-items: center; background: var(--paper); color: var(--ink);
    font: 15px/1.45 -apple-system, system-ui, sans-serif; padding: env(safe-area-inset-top) 24px env(safe-area-inset-bottom); }
  main { max-width: 340px; text-align: center; }
  svg { width: 120px; display: block; margin: 0 auto; }
  h1 { margin: 22px 0 6px; font-size: 24px; font-weight: 700; letter-spacing: -.022em; }
  p { margin: 0; color: var(--muted); }
  button { margin-top: 22px; height: 44px; padding: 0 22px; border: 0; border-radius: 999px; background: #ffb061;
    color: #2b1904; font: 600 15px -apple-system, system-ui, sans-serif; }
</style></head><body><main>
<svg viewBox="0 0 150 100" aria-hidden="true"><circle fill="#ffb061" cx="102" cy="62" r="15"/><path fill="currentColor" d="M0 100 49.500 38 84 74 103.500 56 150 100Z"/></svg>
<h1>Can't reach your photos</h1>
<p>The library is only reachable on your tailnet. Check that Tailscale is connected, then try again.</p>
<button onclick="location.reload()">Try again</button>
</main></body></html>`;

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE)
    .then(c => c.put(OFFLINE, new Response(page, { headers: { "Content-Type": "text/html; charset=utf-8" } })))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  if (req.mode === "navigate") e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
});
