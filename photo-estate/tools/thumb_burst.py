# /// script
# dependencies = ["httpx[http2]"]
# ///
"""thumb_burst.py BASE DB: 150 random (so probably cold) thumbnails over one HTTP/2 connection, then the
same 150 again (warm), three times. Needs HTTPS + HTTP/2 in front, e.g. tailscale serve. See ../PERFORMANCE.md.

  uv run tools/thumb_burst.py https://HOST.TAILNET.ts.net:8766 ~/estate-local/estate.db
"""
import asyncio, random, sys, time, sqlite3
import httpx
base = sys.argv[1]
shas = [r[0] for r in sqlite3.connect(sys.argv[2]).execute(
    "SELECT sha256 FROM embeddings WHERE vec IS NOT NULL")]
random.seed(time.time())
async def burst(pick):
    async with httpx.AsyncClient(base_url=base, http2=True, timeout=60) as ac:
        await ac.get("/api/info")
        async def g(s):
            t0 = time.perf_counter(); r = await ac.get(f"/thumb/{s}"); assert r.status_code == 200
            return (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter(); xs = sorted(await asyncio.gather(*[g(s) for s in pick]))
        return xs, (time.perf_counter() - t0) * 1000
for label in ("cold", "cold", "cold"):
    pick = random.sample(shas, 150)
    xs, wall = asyncio.run(burst(pick))
    print(f"thumb burst 150 random ({label}) wall={wall:5.0f} ms p50={xs[75]:5.0f} p90={xs[135]:5.0f} max={xs[-1]:5.0f}", flush=True)
    xs, wall = asyncio.run(burst(pick))
    print(f"thumb burst 150 random (repeat) wall={wall:5.0f} ms p50={xs[75]:5.0f} p90={xs[135]:5.0f} max={xs[-1]:5.0f}", flush=True)
