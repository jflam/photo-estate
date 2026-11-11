# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["torch", "transformers>=4.49", "pillow", "pillow-heif", "sentencepiece", "protobuf"]
# ///
"""Usage: T=8 uv run tools/sigbench.py bench-store [model-id]
Time SigLIP 2 image embedding on CPU over the benchmark store, plus a zero-shot tag sanity check."""
import sys, time, glob, os
import torch; torch.set_num_threads(int(os.environ.get("T", torch.get_num_threads())))
feat = lambda o: o if torch.is_tensor(o) else o.pooler_output
from PIL import Image
import pillow_heif; pillow_heif.register_heif_opener()
from transformers import AutoModel, AutoProcessor

store = sys.argv[1]
mid = sys.argv[2] if len(sys.argv) > 2 else "google/siglip2-so400m-patch14-384"
paths = sorted(p for p in glob.glob(f"{store}/**/*", recursive=True)
               if p.lower().endswith((".jpg", ".jpeg", ".heic", ".png")))[:96]
print(torch.__version__, "threads", torch.get_num_threads(), "images", len(paths))
t = time.time(); model = AutoModel.from_pretrained(mid, dtype=getattr(torch, os.environ.get("DT","float32"))).eval(); proc = AutoProcessor.from_pretrained(mid)
print(f"load {time.time()-t:.1f}s")
vocab = ["dog", "cat", "food", "beach", "mountains", "snow", "sunset", "night", "flowers", "bird", "car",
         "concert", "wedding", "birthday cake", "christmas tree", "selfie", "group of people", "child",
         "black and white photo", "receipt", "document", "screenshot", "city street", "forest"]
with torch.no_grad():
    ti = proc(text=[f"a photo of {v}" for v in vocab], padding="max_length", max_length=64, return_tensors="pt")
    T = torch.nn.functional.normalize(feat(model.get_text_features(**ti)), dim=-1)
    decode = embed = 0.0; out = []
    for i in range(0, len(paths), 8):
        t = time.time(); ims = []
        for p in paths[i:i+8]:
            im = Image.open(p); im.draft("RGB", (768, 768)); im = im.convert("RGB"); im.thumbnail((768, 768)); ims.append(im)
        decode += time.time() - t
        t = time.time()
        I = torch.nn.functional.normalize(feat(model.get_image_features(**{k: (v.to(model.dtype) if v.is_floating_point() else v) for k, v in proc(images=ims, return_tensors="pt").items()})), dim=-1)
        embed += time.time() - t
        probs = torch.sigmoid((I.float() @ T.float().T) * model.logit_scale.float().exp() + model.logit_bias.float())
        for p, pr in zip(paths[i:i+8], probs):
            top = [(vocab[j], round(float(pr[j]), 2)) for j in pr.argsort(descending=True)[:3] if pr[j] > 0.05]
            out.append((os.path.basename(p)[:10], top))
n = len(paths)
print(f"decode {n/decode:.1f} img/s, embed {n/embed:.1f} img/s -> 96.5k images ≈ {96500/(n/(decode+embed))/3600:.1f} h")
for o in out[:40]: print(o)
