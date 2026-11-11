# /// script
# dependencies = ["httpx[http2]"]
# ///
"""bench_server.py: latency of the photo app's endpoints (embed.py serve). See ../PERFORMANCE.md.

  uv run tools/bench_server.py http://127.0.0.1:8766                       # direct, HTTP/1.1
  uv run tools/bench_server.py https://HOST.TAILNET.ts.net:8766 --h2 --quick  # through tailscale serve

--quick skips the preview/large decode tests, which need --store and --cache on this machine (they pick
originals present in --store whose previews are not yet in --cache; the server then caches them).
"""
import argparse, os, sys, time, random
from concurrent.futures import ThreadPoolExecutor
import httpx

p = argparse.ArgumentParser()
p.add_argument("base")
p.add_argument("--h2", action="store_true", help="one HTTP/2 connection for the thumbnail burst, like a browser")
p.add_argument("--quick", action="store_true")
p.add_argument("--store", default=os.path.expanduser("~/estate-local/store"))
p.add_argument("--cache", default=os.path.expanduser("~/estate-local/review"), help="the server's image cache")
args = p.parse_args()
base, h2, quick = args.base.rstrip("/"), args.h2, args.quick
CACHE, STORE = args.cache, args.store
verify = False


def client():
    return httpx.Client(base_url=base, http2=h2, verify=verify, timeout=60)


C = client()


def t(path, n=5, c=C):
    xs, size = [], 0
    for _ in range(n):
        t0 = time.perf_counter()
        r = c.get(path)
        xs.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200, (path, r.status_code)
        size = r.num_bytes_downloaded
    return xs, size, r


def row(name, xs, size=None, extra=""):
    xs = sorted(xs)
    p = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]
    print(f"{name:42} n={len(xs):3} p50={p(.5):7.1f} ms  min={xs[0]:7.1f}  max={xs[-1]:7.1f}"
          + (f"  {size/1024:8.1f} KB" if size is not None else "") + f" {extra}", flush=True)


def main():
    xs, size, r = t("/", 5); row("GET /", xs, size, r.headers.get("cache-control", ""))
    r2 = C.get("/", headers={"If-None-Match": r.headers.get("etag", "x")}); print("  page revalidation ->", r2.status_code, r.headers.get("etag"))
    xs, size, _ = t("/api/info", 5); row("/api/info", xs, size)
    xs, size, _ = t("/api/facets?", 5); row("/api/facets (no filter)", xs, size)
    xs, size, _ = t("/api/facets?country=Canada&from=2015&to=2019", 5); row("/api/facets country+date", xs, size)
    xs, size, _ = t("/api/facets?q=beach", 5); row("/api/facets q=beach", xs, size)
    info = C.get("/api/info").json()
    person = (info["facets"].get("person") or [["nobody", 0]])[0][0]   # the most-photographed person
    xs, size, _ = t(f"/api/facets?person={person}&q=place:oregon", 5); row("/api/facets person+place:", xs, size)
    xs, size, r = t("/api/search?limit=150", 5); row("/api/search no text (newest)", xs, size)
    first = r.json()["results"]
    xs, size, _ = t("/api/search?limit=150&country=Canada&from=2015&to=2019", 5); row("/api/search filters only", xs, size)
    xs, size, _ = t("/api/search?limit=150&q=year:2012+place:california", 5); row("/api/search typed filters", xs, size)
    # text: cold = new query each time (text encoder), warm = same query (LRU)
    qs = ["dog on a beach", "birthday cake", "snow mountains", "kids at the lake", "sunset over water",
          "red car", "christmas tree", "wedding dress", "city skyline at night", "people hiking"]
    cold = []
    for q in qs:
        x, size, rr = t(f"/api/search?limit=150&q={q}&nonce={random.random()}", 1)
        cold.append(x[0])
    row("/api/search text cold (encode+rank)", cold, size, f"server ms={rr.json()['ms']}")
    xs, size, rr = t(f"/api/search?limit=150&q={qs[0]}", 5); row("/api/search text warm (LRU)", xs, size)
    xs, size, rr = t(f"/api/search?limit=200&q={qs[0]}&cursor=150", 5); row("/api/search text page 2", xs, size)
    nonce = [t(f"/api/search?limit=150&q={qs[0]}&country=Canada&n={random.random()}", 1)[0][0] for _ in range(5)]
    row("/api/search cached text, new filter", nonce, None)
    xs, size, _ = t(f"/api/similar?limit=150&sha={first[3]['sha']}", 5); row("/api/similar", xs, size)

    # thumbnail burst: one screenful, all at once. New connection per request (= tailscale serve per stream)
    shas = [x["sha"] for x in first]
    for conc in (150,):
        lat = []
        def one(s):
            with httpx.Client(base_url=base, http2=False, verify=verify, timeout=60) as cc:
                t0 = time.perf_counter(); r = cc.get(f"/thumb/{s}"); dt = (time.perf_counter() - t0) * 1000
                assert r.status_code == 200, r.status_code
                return dt, len(r.content)
        if h2:  # one HTTP/2 connection, many streams, the browser's view
            import asyncio
            async def burst():
                async with httpx.AsyncClient(base_url=base, http2=True, verify=verify, timeout=60,
                                             limits=httpx.Limits(max_connections=1)) as ac:
                    await ac.get("/api/info")
                    async def g(s):
                        t0 = time.perf_counter(); r = await ac.get(f"/thumb/{s}")
                        assert r.status_code == 200, r.status_code
                        return (time.perf_counter() - t0) * 1000, len(r.content)
                    t0 = time.perf_counter()
                    res = await asyncio.gather(*[g(s) for s in shas])
                    return res, (time.perf_counter() - t0) * 1000
            for rep in range(3):
                res, wall = asyncio.run(burst())
                row(f"thumb burst {len(shas)} over one h2 conn", [r[0] for r in res], None, f"wall={wall:.0f} ms")
        else:
            for rep in range(3):
                t0 = time.perf_counter()
                with ThreadPoolExecutor(conc) as ex:
                    res = list(ex.map(one, shas))
                wall = (time.perf_counter() - t0) * 1000
                row(f"thumb burst {len(shas)} x new conn", [r[0] for r in res], None, f"wall={wall:.0f} ms")
            # keep-alive reuse: 6 connections like a browser on HTTP/1.1
            lat = []
            def worker(chunk):
                with httpx.Client(base_url=base, verify=verify, timeout=60) as cc:
                    out = []
                    for s in chunk:
                        t0 = time.perf_counter(); cc.get(f"/thumb/{s}"); out.append((time.perf_counter() - t0) * 1000)
                    return out
            t0 = time.perf_counter()
            with ThreadPoolExecutor(6) as ex:
                res = sum(ex.map(worker, [shas[i::6] for i in range(6)]), [])
            row("thumb 150 over 6 keep-alive conns", res, None, f"wall={(time.perf_counter() - t0) * 1000:.0f} ms")
        r = C.get(f"/thumb/{shas[0]}"); print("  thumb headers:", {k: v for k, v in r.headers.items() if k.lower() in ("cache-control", "etag", "last-modified", "connection", "content-length")})
        r2 = C.get(f"/thumb/{shas[0]}", headers={"If-None-Match": r.headers.get("etag", "x"), "If-Modified-Since": r.headers.get("last-modified", "Thu, 01 Jan 2099 00:00:00 GMT")})
        print("  conditional GET ->", r2.status_code)

    if quick:
        return
    # HEIC preview/large, uncached then cached
    have = set()
    for root, _, files in os.walk(STORE):
        have.update(files)
    heic = [f[:64] for f in have if f.endswith(".heic")]
    jpg = [f[:64] for f in have if f.endswith(".jpg")]
    random.seed(int(time.time()))
    for kind, prefix, sample in (("previews", "/preview/", heic), ("large", "/large/", heic), ("previews", "/preview/", jpg)):
        pick = [s for s in random.sample(sample, 40) if not os.path.exists(f"{CACHE}/{kind}/{s}.jpg")][:8]
        cold = [t(f"{prefix}{s}", 1)[0][0] for s in pick]
        warm = [t(f"{prefix}{s}", 1)[0][0] for s in pick]
        ext = "heic" if sample is heic else "jpg"
        row(f"{prefix} {ext} uncached", cold); row(f"{prefix} {ext} cached", warm)
        # 4 concurrent uncached (viewer prefetch + scroll)
        pick = [s for s in random.sample(sample, 40) if not os.path.exists(f"{CACHE}/{kind}/{s}.jpg")][:4]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(4) as ex:
            lat = list(ex.map(lambda s: t(f"{prefix}{s}", 1, client())[0][0], pick))
        row(f"{prefix} {ext} 4 concurrent uncached", lat, None, f"wall={(time.perf_counter() - t0) * 1000:.0f} ms")


main()
