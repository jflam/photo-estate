# Tagging research: a local specialist pipeline

*Reworked 2026-11-08. The first version led with rented-GPU vision-language models (VLMs). This
version leads with small specialist models that run on this laptop, and keeps VLMs for the few
jobs only they can do. The bake-off on the benchmark album decides; nothing here is final.*

## The argument in one paragraph

"Tagging" is mostly **recognition**: is there a dog, a beach, a cake, some text? Small specialist
models (0.2–1B parameters) do recognition with calibrated confidence scores, a fixed vocabulary
(always "dog", never "puppy" one day and "canine" the next) and few made-up tags, at a fraction
of the compute. That means the whole backfill can run **at home** (measured: an overnight-to-two-days
background job on this laptop), so no thumbnail ever leaves the house. A general VLM earns its cost only on **understanding**: events ("a child's birthday
party"), relationships ("dog on the couch") and documents ("Home Depot receipt, $84.12"). So
specialists run on everything, and a VLM runs only where they flag that it would add something.

## The pipeline

```
every photo (≈96.5k), local CPU
  Tier 0  deterministic     time, place, device, file facts                     minutes
  Tier 1  SigLIP 2          embedding → semantic search + zero-shot tags        6–46 h (measured, below)
  Tier 2  RAM++             ~4,585 open-set tags with confidences                not measured yet
  Tier 3  OCR               PP-OCRv5, only on photos Tier 1/2 flag as text-heavy  ~2 s each, few k photos
  Tier 4  faces             InsightFace detect + embed, cluster, seed with Google names

a few thousand photos, local (or one short rental)
  Tier 5  VLM               documents/screenshots + one representative per event
```

### Tier 0: deterministic (unchanged)

| Tag | How |
|---|---|
| Local time | Sidecar UTC time + timezone at the GPS point (`timezonefinder`, offline) |
| Season, time of day | Month + hemisphere; `astral` sun position (golden hour, blue hour, night) |
| Holidays, personal dates | `holidays` by country; your birthday/anniversary list |
| Events, trips | Split on 2–4 h gaps; runs more than ~100 km from home lasting at least a night |
| Place | GeoNames `cities500` (`reverse_geocoder`); optional self-hosted Photon for venues |
| Home / work / named places | DBSCAN on GPS (~150 m); you name the clusters |
| Inferred GPS | From GPS photos in the same event, stored as `inferred` |
| Device / file | Camera, front lens ≈ selfie, panorama, screenshot, scan, burst, Live Photo, favourite, albums, Google people |

### Tier 1: SigLIP 2 (search and zero-shot tags), the backbone

**Model:** `google/siglip2-so400m-patch14-384`, Apache-2.0.

**It does two jobs with one forward pass per photo:**
1. **Search.** The image embedding goes into `sqlite-vec`. A query like "kids at the lake" is
   embedded as text and matched. For most "find photos of X" searches this replaces tags
   entirely.
2. **Tags.** Score every photo against your fixed vocabulary of ~300–500 tags ("a photo of
   {tag}"). SigLIP's sigmoid output is a per-tag probability, so each tag gets its own threshold,
   tuned on the benchmark album.

**Measured on this laptop** (PyTorch 2.15 on CPU, 8 threads, 96 benchmark photos, 2026-11-08):

| Variant | Images/s | 96.5k photos |
|---|---|---|
| `siglip2-base-patch16-256` (86M) | 6.8 | **~6 h** |
| `siglip2-so400m-patch16-256` (400M) | 1.7 | ~18 h |
| `siglip2-so400m-patch14-384` (400M, 384 px) | 0.6 | ~46 h |

Decoding the photos (HEIC/JPEG to 768 px) is ~12 images/s, so it isn't the bottleneck.
bfloat16 was *slower* than float32 on this CPU, and all 12 threads were no faster than 8.
PyTorch's CPU path on ARM Linux isn't heavily optimized; an ONNX Runtime int8 export would
probably be faster, but that's untested.

**What this means:** "local" is an overnight-to-two-days background job, not a few hours. That's
fine for a one-time backfill, and new photos (a few a day) take seconds. Start with
**so400m-patch16-256 (~18 h)** and check in the bake-off whether the base model loses anything
on your searches. If it doesn't, the base model at ~6 h wins. Whichever model is chosen, queries
must use the same model forever, so pick it once.

Zero-shot scores are low in absolute terms (a confident match is often only 0.2–0.3), which is
normal for SigLIP's sigmoid output. Thresholds have to be set per tag on the benchmark album,
not globally.

### Tier 2: RAM++ (a second, independent opinion on tags)

**Model:** RAM++ (Recognize Anything Plus), Apache-2.0. It's trained specifically for
multi-label tagging, with ~4,585 common tags plus open-set tags you add. The paper reports it
beating CLIP and the original RAM on OpenImages and ImageNet tag categories. ONNX and LiteRT
exports exist, which help with CPU speed.

**Why both Tier 1 and Tier 2:** SigLIP tags come from text–image similarity; RAM++ was trained
as a tagger. Where they agree, the tag is reliable. Where they disagree, the bake-off shows which
one to trust for which kind of tag. If one turns out clearly better, drop the other.

### Tier 3: OCR, only where there's text

**Model:** PaddleOCR's PP-OCRv5 (Apache-2.0). The official CPU benchmark is ~1.75 s per image
for the mobile model, so run it only on photos Tier 1/2 tag as `document`, `receipt`,
`screenshot`, `whiteboard`, `sign`, `menu`, `book`. That's probably a few thousand photos, an hour
or two. The text goes into FTS5 search.

### Tier 4: faces (unchanged)

InsightFace `buffalo_l` (detection + 512-d embeddings), clustered with HDBSCAN. Clusters that
mostly overlap photos Google tagged "Alice" are named Alice automatically. The code is MIT; the
weights are non-commercial only, which is fine for a personal archive.

### Tier 5: VLM, only where understanding pays

| Input | Roughly how many | What the VLM adds |
|---|---|---|
| Photos tagged document / receipt / screenshot / whiteboard | a few thousand | "Home Depot receipt, $84.12"; "slide about R packages"; what kind of document it is |
| One representative photo per Tier 0 event | ~5–10k | An event caption ("Mia's 7th birthday party at home") applied to the whole event |
| Photos where Tiers 1–2 are unsure | measured in the bake-off | A tie-breaker |

**Model:** Qwen3.5-9B (Apache-2.0) locally. It gets **Tier 0–2 output in the prompt** (place,
date, tags, people names), so it writes "Thanksgiving at Grandma's in Ohio" instead of "people at
a table". At ~10–20k images, it's worth timing the 9B on this laptop's CPU/Vulkan first. If that's
too slow, a 1-hour rental (~$3) of a larger model on thumbnails of *these* images only is the
fallback. Documents should stay local regardless; they're the most sensitive photos.

### Alternatives worth knowing

| Model | What | Why not the default |
|---|---|---|
| **Florence-2** (0.23B / 0.77B, MIT) | Captions, detection, grounding and OCR from one small model | Not a tagger, and needs `trust_remote_code`. Some integrations broke with newer transformers. Good as a bake-off arm for cheap captions |
| Object detectors (RT-DETR, Grounding DINO, YOLO) | Boxes and counts | Only needed for counts ("3 people") or crops. Note that Ultralytics YOLO is **AGPL-3.0**; RT-DETR is Apache-2.0 |
| SAM 2 | Segmentation | Useful for crops or editing, not tagging |
| MobileCLIP2 (Apple) | Smaller CLIP, near-SigLIP quality | If Tier 1 is too slow |
| Places365 classifiers | Scene type | SigLIP zero-shot covers scenes well enough |
| Qwen3-VL-Embedding-8B | Stronger embeddings | Needs a GPU for the backfill; an upgrade path, not a starting point |

## Bake-off on the benchmark album (137 files, local, free)

Three arms, same 112 images:

| Arm | What runs |
|---|---|
| A. Specialists | Tier 0 + SigLIP 2 tags + RAM++ + OCR on text photos |
| B. VLM only | Qwen3.5-9B with the same fixed vocabulary in the prompt |
| C. Specialists + VLM | A, plus the VLM on documents and one photo per event, with A's output in the prompt |

**Score on what you'll actually use:**
1. **~20 real searches** you'd make ("kids at the lake", "receipt from Home Depot", "Christmas
   at grandma's", "the black Honda"). For each, mark which arm's top 10 results are right
   (precision@10), using SigLIP search plus tags plus OCR text plus captions.
2. **Tag quality** on a labelled subset: missed obvious tags, invented tags.
3. **Speed** on this laptop, extrapolated to 96.5k photos.

The likely result is that A gets most searches right, and C wins on events and documents. If B
doesn't clearly beat C, the large rented model isn't needed at all.

## Privacy

With this pipeline, the default is that **nothing leaves the house**. If Tier 5 needs a rental,
send only those images' thumbnails, EXIF-stripped, to a datacenter tier (RunPod Secure Cloud,
Lambda), never community hosts, then destroy the instance and volume. Documents and identity
photos stay local.

## Sources

* RAM++: [paper](https://arxiv.org/abs/2310.15200v2), [official repo](https://github.com/xinyu1205/recognize-anything), [LiteRT export](https://huggingface.co/litert-community/RAM-Plus-LiteRT)
* SigLIP 2: [paper](https://arxiv.org/pdf/2502.14786)
* Florence-2: [model card](https://huggingface.co/microsoft/Florence-2-base), [FiftyOne note on transformers versions](https://docs.voxel51.com/plugins/plugins_ecosystem/florence2.html), [Roboflow OCR guide](https://blog.roboflow.com/florence-2-ocr/)
* PP-OCRv5: [docs and CPU benchmark](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5.html), [PaddleOCR 3.0 report](https://arxiv.org/abs/2507.05595)
* MobileCLIP2: [paper](https://arxiv.org/abs/2508.20691); Qwen3-VL-Embedding: [paper](https://arxiv.org/abs/2601.04720)
* Qwen3.5-9B: [model card](https://huggingface.co/Qwen/Qwen3.5-9B)
* Faces: [Immich facial recognition](https://docs.immich.app/features/facial-recognition)
* Geocoding: [reverse_geocoder](https://github.com/thampiman/reverse-geocoder)
