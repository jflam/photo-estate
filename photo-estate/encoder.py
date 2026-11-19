"""
encoder.py — the SigLIP 2 model as the photo app (`embed.py serve`) holds it: its text side, for typed
searches (browse.py), and with `images` its image side too, for photos arriving from the phone (admin.py).

  Encoder(model_id, threads, images)   .start() loads it in the background; .ready is set once loaded
  .text(q)                             a query's unit vector (waits for the model)
  .image(im)                           a PIL image's float16 vector, as `embed.py run` makes it
  features(torch, out)                 the vector in what a model call returns (embed.py uses it too)

Importing PyTorch and loading the model is most of the app's startup time, and only typed queries need
it, so it loads while the page, filters and thumbnails are already being served.
"""
from __future__ import annotations

import threading
import time


def features(torch, out):
    return out if torch.is_tensor(out) else out.pooler_output


class Encoder:
    def __init__(self, model_id: str, threads: int, images: bool = False):
        self.model_id, self.threads, self.want_images = model_id, threads, images
        self.parts = {}                      # torch, model, tok, and images (the image processor)
        self.ready, self.lock = threading.Event(), threading.Lock()   # one forward pass at a time

    def start(self) -> None:
        threading.Thread(target=self.load, daemon=True).start()

    def load(self) -> None:
        t = time.time()
        import torch
        from transformers import AutoImageProcessor, AutoModel, AutoTokenizer
        torch.set_num_threads(self.threads)
        self.parts.update(torch=torch, model=AutoModel.from_pretrained(self.model_id, dtype=torch.float32).eval(),
                          tok=AutoTokenizer.from_pretrained(self.model_id))
        if self.want_images:   # the image side, for photos arriving from the phone
            self.parts["images"] = AutoImageProcessor.from_pretrained(self.model_id)
        self.ready.set()
        self.text("warm-up")  # the first forward pass is ~1 s slower; take it here, not on a user's query
        print(f"text encoder ready in {time.time() - t:.1f}s", flush=True)

    def text(self, q: str):
        import numpy as np
        # average the raw query with a "a photo of ..." prompt: SigLIP was trained on captions
        prompts = [q.lower(), f"a photo of {q.lower()}"]
        self.ready.wait()
        torch, model = self.parts["torch"], self.parts["model"]
        with self.lock, torch.no_grad():
            ti = self.parts["tok"](prompts, padding="max_length", max_length=64, return_tensors="pt")
            v = torch.nn.functional.normalize(features(torch, model.get_text_features(**ti)), dim=-1).mean(0)
        v = v.numpy()
        return v / np.linalg.norm(v)

    @property
    def side(self) -> int:
        """The image side's input size, in pixels (waits for the model)."""
        self.ready.wait()
        return self.parts["images"].size["height"]

    def image(self, small):
        """The vector for a photo already squashed to side x side, as `embed.py run` computes it."""
        import numpy as np
        self.ready.wait()
        torch, model = self.parts["torch"], self.parts["model"]
        with self.lock, torch.no_grad():
            px = self.parts["images"](images=[small], return_tensors="pt")["pixel_values"]
            v = torch.nn.functional.normalize(features(torch, model.get_image_features(pixel_values=px)), dim=-1)[0]
        return v.numpy().astype(np.float16)
