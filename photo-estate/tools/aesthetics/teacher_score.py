#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["torch", "transformers>=4.49", "pillow>=10.0", "aesthetic-predictor-v2-5", "numpy", "sentencepiece", "protobuf"]
# ///
"""
teacher_score.py — aesthetics pilot step 3: score the pilot JPEGs with Aesthetic Predictor V2.5 (AGPL, used only as a
local tool; nothing from it is copied into this repo). Writes teacher.csv (sha,score) into the pilot cache dir.

  uv run teacher_score.py --dir ~/aesthetic-20k   (resumable: appends to teacher.csv, skips scored shas)
"""
import argparse, csv, time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path.home() / "Library/Caches/photo-estate-aesthetics/pilot"))
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--dtype", default="float32", choices=["float32", "float16", "bfloat16"])
    ap.add_argument("--device", default="mps")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="teacher.csv")
    a = ap.parse_args()
    import torch
    from PIL import Image
    from aesthetic_predictor_v2_5 import convert_v2_5_from_siglip
    d = Path(a.dir).expanduser()
    shas = sorted(p.stem for p in (d / "img").glob("*.jpg"))
    if a.limit:
        shas = shas[: a.limit]
    dtype = getattr(torch, a.dtype)
    model, pre = convert_v2_5_from_siglip(low_cpu_mem_usage=True, trust_remote_code=True)
    model = model.to(a.device, dtype).eval()
    print(f"backbone={model.config._name_or_path} image_size={model.config.image_size} patch={model.config.patch_size} "
          f"dtype={a.dtype} device={a.device} proc_size={pre.size} n={len(shas)}", flush=True)

    csv_path = d / a.out
    done = set()
    if csv_path.exists():
        with open(csv_path, newline="") as f:
            done = {r[0] for r in csv.reader(f) if r and r[0] != "sha"}
    todo = [s for s in shas if s not in done]
    print(f"already scored {len(done)}, todo {len(todo)}", flush=True)
    logf = open(d / "teacher.log", "a")

    def log(msg):
        print(msg, flush=True); logf.write(f"{time.strftime('%F %T')} {msg}\n"); logf.flush()

    def load(s):
        try:
            with Image.open(d / "img" / f"{s}.jpg") as im:
                return im.convert("RGB")
        except Exception as e:
            log(f"UNREADABLE {s} {type(e).__name__}: {e}")
            return None

    def score(batch_shas):
        pairs = [(s, load(s)) for s in batch_shas]
        pairs = [(s, im) for s, im in pairs if im is not None]
        if not pairs:
            return []
        px = pre(images=[im for _, im in pairs], return_tensors="pt").pixel_values.to(a.device, dtype)
        with torch.inference_mode():
            return list(zip([s for s, _ in pairs], model(px).logits.squeeze(-1).float().cpu().tolist()))

    if not todo:
        log("nothing to do"); return
    score(todo[: a.batch])  # warm-up (kernel compile), not timed, not written
    new_file = not csv_path.exists() or csv_path.stat().st_size == 0
    n, t0, last = 0, time.time(), time.time()
    with open(csv_path, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["sha", "score"])
        for i in range(0, len(todo), a.batch):
            res = score(todo[i:i + a.batch])
            w.writerows(res); f.flush()
            n += min(a.batch, len(todo) - i)
            if time.time() - last >= 60 or n >= len(todo):
                last = time.time()
                rate = n / (last - t0)
                log(f"{len(done) + n}/{len(done) + len(todo)} {rate:.2f} img/s ETA {(len(todo) - n) / rate / 60:.0f} min")
    log(f"finished: {n} processed this run")


if __name__ == "__main__":
    main()
