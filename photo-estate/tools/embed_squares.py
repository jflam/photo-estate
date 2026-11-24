#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["torch", "transformers>=4.49", "pillow", "sentencepiece", "protobuf", "numpy"]
# ///
"""
embed_squares.py: image vectors from a second model (SigLIP 2 large, 384 px, by default) for the square
JPEG shards tools/export_squares.py makes, one file per shard. `embed.py serve --compare-vectors` reads them
(compare.py) to rank a search by both models side by side.

  uv run tools/embed_squares.py --squares ~/photo-estate-lab/squares512 --out ~/photo-estate-lab/large384
  uv run tools/embed_squares.py --squares ... --out ... --wait     # also the shards still to come
  uv run tools/embed_squares.py --squares ... --out ... --limit 1  # one shard, to try it

Uses the GPU (MPS) when there is one: on an M2 Max about 21 photos/s, so ~65 min for 83,000 photos.
Reads --squares/shard-NNNN.tar (only whole ones: a download that keeps unfinished files apart, such as
rsync --partial-dir, is safe to run alongside) and writes, in --out:

  shard-NNNN.npy        float16, (n, dim), L2-normalised, rows in the tar's member order
  shard-NNNN.shas.txt   the sha of each row
  meta.json             model, side, preprocessing

Each pair is written to a temporary name and renamed, so a rerun skips finished shards. --wait keeps
going until every shard the export will make (from squares512/shas.txt) is embedded.

Preprocessing: the export's 512 square, bicubic to the model's side (384), then the processor with
do_resize=False (rescale and normalise only), as smoke.py. Note this resamples twice (original -> 512
-> 384), so vectors differ slightly from embedding the originals directly at 384.
"""
import argparse
import io
import json
import math
import os
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

def decode(data: bytes, side: int):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        return im.convert("RGB").resize((side, side), Image.Resampling.BICUBIC)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--squares", required=True, help="the folder of shard-NNNN.tar (export_squares.py --out)")
    p.add_argument("--out", required=True)
    p.add_argument("--model", default="google/siglip2-large-patch16-384")
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--shard-size", type=int, default=2000, help="export_squares.py's --shard-size, for the count of shards to wait for")
    p.add_argument("--wait", action="store_true", help="keep waiting for new shards until all are done")
    p.add_argument("--limit", type=int, help="stop after this many shards")
    a = p.parse_args()
    sq, out = Path(a.squares).expanduser(), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    import numpy as np
    import torch
    from transformers import AutoModel, AutoProcessor
    dev = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    model = AutoModel.from_pretrained(a.model, dtype=torch.float32).eval().to(dev)
    proc = AutoProcessor.from_pretrained(a.model)
    side = proc.image_processor.size["height"]
    meta = {"model": a.model, "side": side, "source": str(sq), "dtype": "float16", "normalised": True,
            "preprocess": "512 export square -> bicubic to side -> processor(do_resize=False)", "device": dev}
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"{a.model} on {dev}, side {side}, batch {a.batch}", flush=True)

    def embed(ims):
        px = proc.image_processor(images=ims, do_resize=False, return_tensors="pt")["pixel_values"]
        with torch.no_grad():
            v = model.get_image_features(pixel_values=px.to(dev))
            v = v if torch.is_tensor(v) else v.pooler_output
            return torch.nn.functional.normalize(v, dim=-1).float().cpu().numpy()

    shas_file = sq / "shas.txt"
    expected = math.ceil(sum(1 for _ in open(shas_file)) / a.shard_size) if shas_file.exists() else None
    pool, done_n, t_all, n_all = ThreadPoolExecutor(4), 0, time.time(), 0
    while True:
        todo = [t for t in sorted(sq.glob("shard-*.tar")) if not (out / f"{t.stem}.npy").exists()]
        finished = len(list(out.glob("shard-*.npy")))
        if not todo:
            if not a.wait or (expected is not None and finished >= expected):
                break
            time.sleep(30)
            continue
        for t in todo:
            t0 = time.time()
            with tarfile.open(t) as tar:
                members = [(m.name[:-4], tar.extractfile(m).read()) for m in tar if m.isfile()]
            shas, vecs = [s for s, _ in members], []
            for i in range(0, len(members), a.batch):
                ims = list(pool.map(lambda d: decode(d, side), [d for _, d in members[i:i + a.batch]]))
                vecs.append(embed(ims))
            V = np.concatenate(vecs).astype(np.float16)
            np.save(out / f"{t.stem}.tmp.npy", V)
            (out / f"{t.stem}.shas.tmp").write_text("".join(f"{s}\n" for s in shas))
            os.rename(out / f"{t.stem}.shas.tmp", out / f"{t.stem}.shas.txt")
            os.rename(out / f"{t.stem}.tmp.npy", out / f"{t.stem}.npy")   # last: an .npy means the pair is whole
            dt = time.time() - t0
            n_all += len(shas)
            done_n += 1
            finished += 1
            left = (expected - finished) if expected else 0
            rate = n_all / (time.time() - t_all)
            print(f"{t.stem}: {len(shas)} photos, dim {V.shape[1]}, {dt:.0f} s ({len(shas) / dt:.1f}/s) | "
                  f"{finished}/{expected or '?'} shards, ETA {left * a.shard_size / rate / 60:.0f} min", flush=True)
            if a.limit and done_n >= a.limit:
                return
    print(f"done: {finished} shards", flush=True)


if __name__ == "__main__":
    main()
