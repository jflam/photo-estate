// Service worker for the photo app. It does one thing: when the page itself can't be loaded (on a
// phone, usually because Tailscale is off), show a page that says so instead of the browser's error.
// Everything else goes straight to the network: the API is live data, and images are already in the
// HTTP cache (they're content-addressed and marked immutable).
const CACHE = "estate-v2";
const OFFLINE = "/offline";

const page = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0c0c0d"><title>Photo Estate</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0c0c0d; color: #f1f1f2;
    font: 15px/1.45 system-ui, -apple-system, sans-serif; padding: env(safe-area-inset-top) 24px env(safe-area-inset-bottom); }
  main { max-width: 320px; text-align: center; }
  img { width: 72px; height: 72px; border-radius: 16px; }
  h1 { margin: 18px 0 6px; font-size: 20px; font-weight: 600; letter-spacing: -.02em; }
  p { margin: 0; color: #9a9aa1; }
  button { margin-top: 24px; height: 40px; padding: 0 20px; border: 0; border-radius: 10px; background: #f1f1f2;
    color: #0c0c0d; font: 600 15px system-ui, -apple-system, sans-serif; }
</style></head><body><main>
<img src="/icon-180.png" alt="">
<h1>Can't reach your photos</h1>
<p>The library is only reachable on your tailnet. Check that Tailscale is connected, then try again.</p>
<button onclick="location.reload()">Try again</button>
</main></body></html>`;

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => Promise.all([
    c.put(OFFLINE, new Response(page, { headers: { "Content-Type": "text/html; charset=utf-8" } })),
    c.add("/icon-180.png").catch(() => {}),
  ])).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
  } else if (new URL(req.url).pathname === "/icon-180.png") {
    e.respondWith(fetch(req).catch(() => caches.match("/icon-180.png")));
  }
});
