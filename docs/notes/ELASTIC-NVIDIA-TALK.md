# Following up the Elastic talk at the NVIDIA booth

*Written 2026-11-19 from primary sources (elastic.co, docs.typesafe.ai, arxiv.org, github.com,
huggingface.co, nvidia.com), with a few secondary sources where no primary page could be found; those
are marked. Vendor numbers are marked **(vendor)**. Where something could not be found, this note says
so instead of filling the gap. Arithmetic of my own is marked **(my arithmetic)**.*

The talk, as remembered: an Elastic speaker at an NVIDIA booth in November 2026 covered (1) search with
embeddings and HNSW, from local to cloud, (2) a VLM that describes each video (a demo of roughly 50
videos), and (3) Jev, TypeSafe AI's "System One" decision model, for classification.

## Summary

- **The talk itself could not be identified.** It was not at GTC Washington, D.C.: that runs
  November 30 to December 3, after this note was written. NVIDIA had booths with theater sessions at
  Microsoft Ignite and at SC26 the week of November 16; neither schedule is published in detail.
- **GPU indexing in Elasticsearch** builds a CAGRA graph on the GPU, converts it to an ordinary HNSW
  graph and searches it on the CPU. It needs an Enterprise licence and an NVIDIA Ampere or newer GPU,
  covers `hnsw` and `int8_hnsw` with float vectors only (not BBQ), and is documented for self-managed
  Linux only. None of it matters at 100k photos.
- **BBQ is easy to copy in outline**: one bit per dimension around a centroid, a 4-bit query, popcounts,
  then a rescore with better vectors. The published recall numbers are all for text embeddings. For the
  iPhone the bits are about 10 MB, but **the rescore needs higher-precision vectors on the phone too**
  (int8 is about 77 MB), and **the phone also needs the SigLIP text encoder** to turn a typed query into
  a vector. Measure recall on the real library before building anything.
- **Jev has changed since the earlier notes**: Jev 1.13 and 1.14 accept images (opt-in, PNG/JPEG,
  marked not production-ready), and 1.14 adds a Box question type. It is still hosted only, with no
  published rate limits, $0.042 per million input tokens and free output **(vendor)**.
- **Open Jev-style models**: the only multimodal one that `vllm-jev` serves on a Mac is
  Valen-Preview-0923 (licence not found). Clef-flash (9B, Apache-2.0, images and video) is the strongest
  open multimodal option, but `vllm-jev` serves it on Linux CUDA only. The "Releasing the Inner Jev"
  paper shows that a plain LLM's option probabilities, read from one forward pass, already work as a
  decision model, so **the Qwen3.5-9B already planned for captions can answer typed questions itself**.
- **Recommendation**: keep (c), linear heads on the stored SigLIP vectors, as the default for anything
  visual you can label, since they cover all 100k photos for free. Add (b), local typed questions, only
  where a question needs the caption plus place, date, people and album, on the photos that get
  captions. Use (a), hosted Jev, at most on the demo library as a reference: it breaks the
  nothing-leaves-the-house rule. For **videos**, cut at scene boundaries, put keyframes through the
  existing SigLIP path (roadmap item 5), and caption a keyframe only where it is worth it. V-JEPA 2
  doesn't fit this job.

---

## A. The talk itself

### 1. Which event and talk

**Not found.** What the sources do say:

- Elastic's GTC Washington, D.C. page lists booth #537 and "live demos of GPU-accelerated search" for
  sovereign, air-gapped and edge settings. It gives no dates, lists no talks, and says nothing about
  video, VLMs or Jev ([elastic.co](https://www.elastic.co/events/nvidia-gtc-washington-dc)).
- GTC Washington, D.C. 2026 runs **November 30 to December 3, 2026**
  ([nvidia.com](https://www.nvidia.com/en-us/gtc-dc/attend/)), so a talk heard by November 19 was not
  there.
- Two NVIDIA booths had theater sessions in the week of November 16:
  - **Microsoft Ignite** (San Francisco): NVIDIA booth 5644 with 30 theater sessions, November 17–19.
    The sessions aren't listed individually
    ([Microsoft Tech Community](https://techcommunity.microsoft.com/blog/azureinfrastructureblog/join-nvidia-and-microsoft-at-ignite-2026/4573012)).
    Elastic had its own booth #5334 and one listed talk, about AI SREs, not search
    ([elastic.co](https://www.elastic.co/events/microsoft-ignite)).
  - **SC26** (Chicago, November 15–20): NVIDIA has a booth with theater sessions
    ([nvidia.com](https://www.nvidia.com/en-us/events/supercomputing/)). No Elastic session turned up.
- Elastic's other NVIDIA events this year were GTC San Jose, with an on-demand session about the 12x
  GPU indexing ([elastic.co](https://www.elastic.co/events/nvidia-gtc)), and GTC Berlin, booth #3002
  ([elastic.co](https://www.elastic.co/events/nvidia-gtc-berlin)).
- No Elasticsearch Labs post pairs a VLM with Jev. The nearest is **"AI video search with
  Elasticsearch and Jina"** (JD Armada, September 18, 2026), demo repo
  [jdarmada/omnishot](https://github.com/jdarmada/omnishot). It uses no VLM and no Jev; see question 2
  ([elastic.co](https://www.elastic.co/search-labs/blog/ai-video-search-jina-embeddings)).

**To pin it down:** the event app's session list, a badge scan or follow-up email, or the slide's
footer. If it was the NVIDIA booth at Ignite, the speaker may post the demo on Elasticsearch Labs later.

### 2. Which VLM, how frames were sampled, what the Jev questions were

**Not found**, because the talk wasn't found. For comparison, the Omnishot demo
([elastic.co](https://www.elastic.co/search-labs/blog/ai-video-search-jina-embeddings)):

- **Segmentation:** PySceneDetect's `AdaptiveDetector` (threshold 3.0, scenes at least 1.5 s). A clip
  with no cut becomes one chunk. Chunks are copied losslessly with FFmpeg, then made into 640 px silent
  proxies of at most 3 s.
- **Embedding:** `jina-embeddings-v5-omni-small` samples 32 evenly spaced frames per chunk and returns
  one 1,024-d vector. The same model embeds text queries, and Elasticsearch searches with cosine
  similarity over HNSW.
- **Scale:** 70 clips, "a few hundred chunks"; the post estimates about 1,200 vectors per hour of
  footage.
- **No captions, no classifier.** The mapping has `transcript` and `tags` fields, but no step fills
  them.

So the talk's "VLM description then Jev" pipeline is a different design. Elastic has published the
embed-the-video design.

---

## B. GPU vector indexing in Elasticsearch (cuVS)

### 3. How it works

- **The graph is built on the GPU with CAGRA and converted to HNSW.** Vectors are buffered on the Java
  heap, sent to the GPU, built into a CAGRA graph, copied back, converted to HNSW and written to disk.
  The post says the result is the same format the CPU would have built
  ([Chapter 2, December 3, 2025](https://www.elastic.co/search-labs/blog/elasticsearch-gpu-accelerated-vector-indexing-nvidia)).
- **Search runs on the CPU.** The converted graph is immediately searchable on the CPU (same post).
  The current docs say GPU indexing only speeds up building the HNSW graph
  ([docs](https://www.elastic.co/docs/solutions/search/vector/gpu-vector-indexing)).
- The first exploration (Chapter 1, GTC 2025) also considered searching the CAGRA graph on the GPU,
  through the `lucene-cuvs` codec. The shipped path is build on the GPU, search on the CPU
  ([Chapter 1](https://www.elastic.co/search-labs/blog/gpu-accelerated-vector-search-elasticsearch-nvidia)).
- **Searching a GPU-built index on a machine without a GPU:** the docs don't say so directly. Because
  the stored format is ordinary HNSW, it should work. That is my inference, not a documented guarantee.
- Elasticsearch calls cuVS through `cuvs-java`, a Panama (foreign function) binding maintained by
  NVIDIA, packaged as an Elasticsearch plugin
  ([elastic.co](https://www.elastic.co/search-labs/blog/nvidia-cuvs-elasticsearch-gpu-vector-indexing)).
  The plugin is ELv2 and needs the Enterprise tier
  ([Elastic–NVIDIA partner FAQ](https://www.elastic.co/partners/nvidia)).

### 4. Version, status, where it runs, requirements

| Item | What the source says |
|---|---|
| Status | Live docs: no status shown. A preview build of the same docs page says **"Generally available since 9.4, Preview in 9.3"** and **"Elastic Cloud Serverless: Unavailable"** ([docs preview build](https://docs-v3-preview.elastic.dev/elastic/docs-content/pull/8179/solutions/search/vector/gpu-vector-indexing.md)). It is a docs pull request, not the published page |
| Where it runs | Only a self-managed Docker example is documented; Elastic Cloud Hosted and ECK are not mentioned ([docs](https://www.elastic.co/docs/solutions/search/vector/gpu-vector-indexing)) |
| Licence | Enterprise subscription (same page) |
| GPU | NVIDIA Ampere or newer, compute capability ≥ 8.0, at least 8 GB of GPU memory (same page) |
| Software | GPU driver, CUDA and the cuVS runtime; Linux x86_64 only; Java 22+. The Docker example uses Elasticsearch 9.5.2, CUDA 12.9.1 and cuVS 26.04 (same page) |
| Setting | `vectors.indexing.use_gpu` (node level). By default the GPU is used when one is detected; with `true`, the node refuses to start without one ([mapping reference](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/gpu-vector-indexing)) |

### 5. The benchmarks behind the headline numbers

**"138M vectors in under 10 minutes"**
([Search Labs, September 25, 2026](https://www.elastic.co/search-labs/blog/vector-indexing-gpu-elasticsearch-cuvs)):

| Item | Value |
|---|---|
| Data | MS MARCO v2 (`msmarco-v2-vector` Rally track), 138 million vectors, 1,024 dimensions |
| Index | HNSW, target recall 95%; int8 and BBQ are not mentioned |
| GPU run | 8 × NVIDIA RTX PRO 6000 |
| CPU baseline | 2-socket AMD EPYC 9555, GPUs off |
| Build time | ~1 hour → under 10 minutes; 38K → 281K docs/s (the "7x throughput") |
| p90 search latency while indexing | 45 ms → 7 ms |
| Force merge to 4 segments per shard | 249 min → 5 min (~48x) |
| Recall | Same 95% target; GPU-built and CPU-built recall curves overlap |

**"Up to 12x faster indexing" and "7x faster merging"**
([Chapter 2, December 3, 2025](https://www.elastic.co/search-labs/blog/elasticsearch-gpu-accelerated-vector-indexing-nvidia)):

| Item | Value |
|---|---|
| Data | 2.6 million 1,536-d vectors from the OpenAI Rally track, sent base64-encoded, indexed as float32 `hnsw` |
| GPU | AWS g6.4xlarge (the post names only the instance; g6 instances carry an NVIDIA L4) |
| Baseline for ~12x indexing and ~7x force merge | The same g6.4xlarge with the GPU unused, 8 indexing threads |
| Baseline for the cost comparison (~5x indexing, ~6x force merge) | A CPU-only g6.8xlarge: 32 vCPU AMD EPYC, 64 GB, 16 threads |
| Recall | Up to 95% for all graphs; the GPU graph marginally higher |
| Absolute throughput | Not given, only the ratios |

So the earlier summary's "12x indexing, 7x merging" is the December 2025 single-L4 test, and "7x" in the
September 2026 post means indexing throughput on 8 GPUs.

### 6. BBQ

The docs list only `hnsw` and `int8_hnsw`, float element type only, and don't mention `bbq_hnsw`
([docs](https://www.elastic.co/docs/solutions/search/vector/gpu-vector-indexing)). So, as documented,
**BBQ indexes are not built on the GPU**.

---

## C. Compressed vectors: BBQ and DiskBBQ

### 7. Recall

All of Elastic's published BBQ numbers are for **text** embeddings. **None of them use image
embeddings.**

| Source | Data | Setting | Recall |
|---|---|---|---|
| [OSQ post](https://www.elastic.co/search-labs/blog/optimized-scalar-quantization-elasticsearch) (8.18) | Cohere 768-d | BBQ → BBQ with OSQ, Recall@10 from 50 candidates (5x oversample, rescored) | 0.933 → 0.938 |
| same | Cohere 1024-d | same | 0.932 → 0.945 |
| same | E5-small-v2 (Quora) | same | 0.972 → 0.975 |
| same | GIST-1M | same | 0.740 → 0.989 |
| [BBQ post](https://www.elastic.co/search-labs/blog/better-binary-quantization-lucene-elasticsearch) (8.16) | E5-small, 500k | 1 bit; oversampling not stated | 74% |
| same | Cohere v3, 1M × 1024 | 1 bit + HNSW, 3x oversample | above 90% |
| same | MS MARCO v2, 138M × 1024 | k=10 | 45% (20 candidates), 70% (100 candidates, rerank 50), 90% (1,000 candidates, rerank 200) |
| [Measuring recall](https://www.elastic.co/search-labs/blog/recall-vector-search-quantization) | DBPedia-14 "Film", Jina v5 text-small, default `semantic_text` (HNSW + BBQ, rescored) | Recall@10 vs a `flat` float baseline, 1k–40k docs | 0.992–1.000 |
| [Jina v5 + BBQ](https://www.elastic.co/search-labs/blog/bbq-quantization-jina-embeddings-v5) | Jina v5 text-small, 1,024-d, 5 languages | BBQ vs float32 | 0.994 Recall@10, at about 142 B per vector (~29x smaller); Jina v5 is trained for quantization |
| [DiskBBQ vs Qdrant](https://www.elastic.co/search-labs/blog/vector-search-benchmark-elasticsearch-vs-qdrant) | (see post) | DiskBBQ, `oversample=1`, `visit_percentage` 1 → 5 | Recall@100 0.879 → 0.972; Qdrant disputes the comparison ([qdrant.tech](https://qdrant.tech/blog/benchmark-elastic-diskbbq/)) |

"Without rescoring" numbers aren't published pair-for-pair. The 74% and 45% rows are the closest to
"little or no rescoring".

**On image embeddings**, outside Elastic: a May 2026 paper argues that binary quantization works well on
embeddings trained contrastively and badly on others
([arXiv 2605.17524](https://arxiv.org/pdf/2605.17524)). SigLIP is trained contrastively, with a sigmoid
loss. Another paper, which builds the *graph itself* from bits, found multimodal CLIP data only
"moderately effective" (71–78% Recall@10), against at least 88% for contrastive text models
([arXiv 2605.02171](https://arxiv.org/html/2605.02171v3)). That's a harder setting than bits plus a
rescore, but it is a warning that image vectors may lose more than text vectors. **This has to be
measured on the real SigLIP vectors.**

### 8. Can it be copied in NumPy and Swift?

**Mostly yes.** The BBQ post
([elastic.co](https://www.elastic.co/search-labs/blog/better-binary-quantization-lucene-elasticsearch))
describes:

1. **Centre** every vector on one centroid (per Lucene segment; for us, the whole library).
2. **One bit per dimension**: 1 if the centred value is > 0. Bits are packed into `dims/8` bytes, plus
   2 corrective floats for Euclidean distance or 3 for dot product.
3. **Asymmetric query**: the centred query is scaled to int4 (0–15) from its own min and max.
4. **Score with popcounts**: split the int4 query into 4 bit planes; the dot product is
   Σᵢ popcount(planeᵢ & doc_bits) << i.
5. **Rescore**: estimate scores for everything, keep the top `k × oversample`, and recompute those with
   the raw float vectors. The benchmarks sweep oversample from 1 to 5.

**What is not published in the posts I could read** is the exact formula for the corrective terms and
for the distance estimate. The OSQ post (8.18, where each vector gets its own optimised interval) lists
what is stored: the bits, the interval ends, one correction term, and the sum of the quantized values
([elastic.co](https://www.elastic.co/search-labs/blog/optimized-scalar-quantization-elasticsearch)).
It defers the maths to a separate post, which I could not open. Lucene's source is the authority there;
I did not read it.

**For Photo Estate this doesn't matter much**, because there is no graph to traverse: a brute-force
scan of 100k bit vectors is cheap. Plain centred sign bits plus a rescore may be enough, and the
corrective terms only refine the first-pass ranking. The parts that do matter:

| On the phone | Size for 100k × 768-d **(my arithmetic)** |
|---|---|
| 1-bit codes | 9.6 MB (+1.2 MB if 3 floats of corrections are kept) |
| Rescore vectors, int8 | 76.8 MB |
| Rescore vectors, float16 | 153.6 MB |
| Rescore vectors, 4-bit | 38.4 MB |
| Today's float32 matrix | 307 MB |

1. **The rescore needs better vectors on the phone.** Rescoring the top few hundred photos means having
   *their* vectors, which means having all of them, because any photo can be in the top few hundred.
   Elastic's own posts rescore against float32. The phone would need int8 or 4-bit vectors as well:
   about 40–90 MB in total, not 10 MB.
2. **A typed query needs the SigLIP text encoder on the phone** (Core ML or similar). Without it,
   offline search is limited to "more like this photo", which only needs stored vectors. The size and
   speed of the text tower on an iPhone are not measured here.
3. **Measure first, on the Mac mini.** A recall test needs only the stored matrix. A minimal version,
   which leaves out the corrective terms:

```python
import numpy as np

def bbq_recall(M, queries, k=10, oversample=(1, 3, 5, 10, 30)):
    """M: (N, 768) float32 SigLIP vectors, L2-normalised. queries: (Q, 768) text-query vectors."""
    c = M.mean(axis=0)
    bits = ((M - c) > 0).astype(np.float32)            # 1 bit per dimension (unpacked for clarity)
    out = {}
    for os_ in oversample:
        hits = 0
        for q in queries:
            exact = np.argpartition(-(M @ q), k)[:k]    # today's brute-force answer
            est = bits @ (q - c)                         # first pass: bits against the float query
            cand = np.argpartition(-est, k * os_)[:k * os_]
            top = cand[np.argsort(-(M[cand] @ q))[:k]]   # rescore with float (try int8 too)
            hits += len(set(top) & set(exact))
        out[os_] = hits / (k * len(queries))
    return out
```

Run it with the ~20 real searches from the bake-off as queries, and again with int8 in place of `M` in
the rescore line. If recall@10 is ≥ 0.95 at an oversample of 10–30 with int8 rescoring, the iPhone
design is sound.

---

## D. Jev (TypeSafe AI)

### 9. The API

**State.** What the model evaluates: a string, a JSON object or an array. Every question sees the same
state and is answered independently. Objects with named fields are recommended
([State](https://docs.typesafe.ai/concepts/state.md)). The State page gives no size limit. The
**limit is on the Models page**: by default 64k tokens per request and 32k per question; with the
`TypeSafe-Danger-Zone-Long-Context: true` header, 262k for both, described as slower and less accurate
([Models](https://docs.typesafe.ai/models.md)). The request body is at most 32 MiB, nested at most
64 levels deep ([Errors](https://docs.typesafe.ai/api/errors.md)).

**Question types** ([System One](https://docs.typesafe.ai/concepts/system-one)):

| Type | Asks | Returns |
|---|---|---|
| Choice | Pick one option | `choice`, `probabilities` (one per option), `confidence` |
| Score | Rate against ordered levels | `score`, `probabilities`, `confidence` |
| Noul | Is this statement true? | a probability, 0 to 1 |
| Box | Where something is in an image | four edge coordinates as fractions 0–1; Jev 1.14 and the images header only ([Images](https://docs.typesafe.ai/images.md)) |

- **How many questions per call:** no number is published. "One or more" is all the State page says,
  and types can be mixed ([State](https://docs.typesafe.ai/concepts/state.md)).
- **Option and level limits** (up to 255 Choice options, 2–10 Score levels) appear only in secondary
  sources ([opentweet.io](https://opentweet.io/jev/limits)); I didn't find them in the primary docs.

**Output.** The raw JSON has `model`, `answers` (one per question ID) and `usage` (`input_tokens`,
`output_tokens`) ([Quick start](https://docs.typesafe.ai/introduction/quickstart)).

**Calibration.** **(vendor)** TypeSafe says the probabilities are trained against outcomes to reflect
uncertainty. It adds that calibration is measured over groups of predictions and does not mean any one
answer is right ([System One](https://docs.typesafe.ai/concepts/system-one)). Independent
measurements:
- Mean ECE 0.066 for Jev on held-out decision sets, against 0.047 for an open fine-tuned 31B
  ([arXiv 2610.17205](https://arxiv.org/pdf/2610.17205)).
- On seven political-science replications, Jev 1.13's probabilities were better calibrated than a
  low-cost commercial LLM's token probabilities on all eight tasks compared. Its accuracy was on par,
  with little cost advantage over batch or local models ([arXiv 2610.06625](https://arxiv.org/pdf/2610.06625)).

**Rate limits.** Not published. The docs describe `429` (back off and retry) and `529` (overloaded),
and the SDKs retry with backoff ([Errors](https://docs.typesafe.ai/api/errors.md)).

**Pricing** **(vendor)**: $0.042 per million input tokens, output free, the same for Jev 1.13 and
1.14 ([Models](https://docs.typesafe.ai/models.md); also on
[OpenRouter](https://openrouter.ai/typesafe)). Images are billed as input tokens at the same price,
with no formula yet for how many tokens an image costs ([Images](https://docs.typesafe.ai/images.md)).
**Latency** **(vendor)**: 70–500 ms end to end, as reported by
[InfoQ](https://infoq.com/news/2026/10/typesafe-ai-jev-released); the docs give no figure.

**Models.** Jev 1.14 (`jev-1.14.0`, from October 14, 2026) and Jev 1.13. Both `jev-latest` and
`jev-preview` point to 1.14 ([Models](https://docs.typesafe.ai/models.md)). Jev is also on OpenRouter
as `typesafe/jev-1.14` ([OpenRouter docs](https://openrouter.ai/docs/guides/community/jev)).

**Python SDK.** `typesafe-sdk` (Python 3.10+). It reads `TYPESAFE_API_KEY` and defaults to
`jev-latest` ([Quick start](https://docs.typesafe.ai/introduction/quickstart)). The documented shape,
applied to one photo. **This example is my own and is untested** (no API key here):

```python
# uv add typesafe-sdk
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

photo = {   # what estate.py would send: caption + facts, no pixels
    "caption": "Three kids in swimsuits on a wooden dock, lake and pine trees behind",
    "place": "Lake Wenatchee, Washington, United States",
    "date": "2019-07-14 16:20",
    "people": ["Ben", "Matthew"],
    "album": "Summer 2019",
}

with TypeSafeClient(model="jev-1.14.0") as client:   # pin the version, not jev-latest
    r = client.system_one(
        state=photo,
        questions={
            "setting": Choice(
                instructions="Where the photo was taken",
                criteria={"home": "Inside or around a home", "outdoors": "Nature, parks, water",
                          "city": "Streets, shops, restaurants", "other": "None of these fits"},
            ),
            "is_event": Noul(instructions="The photo is from a celebration or organised event"),
            "people_shown": Score(
                instructions="How many people are in the photo",
                criteria=["Nobody", "One person", "Two or three", "A group of four or more"],
            ),
        },
    )

print(r.choices["setting"].choice, r.choices["setting"].probabilities)
print(r.nouls["is_event"].noul)
print(r.scores["people_shown"].score)
```

### 10. Images and video

**Images are already supported, opt-in** ([Images](https://docs.typesafe.ai/images.md)):
- Jev 1.13 and 1.14 accept images when each request sends `TypeSafe-Danger-Zone-Images: true`; Box
  needs 1.14.
- The docs call it not yet production-ready, with limits that may change.
- Still PNG or JPEG only. Each side must be 280–1568 px, each file at most 16 MiB, and the whole
  request at most 32 MiB. Images outside the limits are rejected, not resized.
- EXIF orientation is not applied (rotate the pixels first) and metadata is stripped.

The Models page still says text only, with no audio or video
([Models](https://docs.typesafe.ai/models.md)); the two pages disagree.

**Video** isn't supported. The State page says audio and video are not supported "(yet)"
([State](https://docs.typesafe.ai/concepts/state.md)). The Images page suggests sending stills of
individual frames. No date or plan is published.

### 11. Data handling

- **Training:** Jev is "not trained on customer requests or responses". It isn't fine-tuned or
  LoRA-adapted with customer data, and every account uses the same weights
  ([Models](https://docs.typesafe.ai/models.md)).
- **Retention:** covered by the Data Processing Agreement
  ([Legal](https://docs.typesafe.ai/legal.md)). I couldn't open the DPA, so **the retention period is
  unknown**.
- **Zero data retention:** offered to enterprise customers on request (same page).
- **Region, subprocessors:** not stated anywhere I could read.
- **On-prem or open weights:** none. Jev is hosted only; the docs mention no self-hosted option.

### 12. The three papers

**"Releasing the Inner Jev: A Simple Recipe for Open Decision Models"**
([arXiv 2610.17205](https://arxiv.org/pdf/2610.17205), October 2026)
- **Recipe:** each decision becomes a lettered multiple-choice prompt, with thinking turned off. One
  forward pass, then a softmax over the letter tokens' logits gives the option probabilities; zero-shot,
  this is already close to Jev.
- **Training:** LoRA (rank 32) with a decision head initialised from the letter rows of the LM head;
  400 steps of cross-entropy on a pool of 417,825 questions; options shuffled; one temperature per
  question type for calibration. No reinforcement learning.
- **Base models and releases:** Gemma 4 31B → Jeff-1.0-Large; Qwen3.6-27B → Jeff-1.0-Qwen-27B;
  Gemma 4 12B → Jeff-1.0-Medium; Qwen3.5-4B → Jeff-1.0-Qwen-4B. The paper states no licence for the
  models.
- **Against Jev:**
  - OpenDecisions capability: 84.4 vs 80.1 (zero-shot Gemma 4 31B: 79.6).
  - JevBench v1.7: 81.5 vs 77.1.
  - Mean ECE: 0.047 vs 0.066.
  - On knowledge-heavy multiple choice Jev still leads, 92.9 vs 88.8.
- **Small models:**
  - Zero-shot macro accuracy on held-out sets is 48.9 for Qwen3.5-0.8B, 57.6 for 2B, 76.8 for 4B,
    82.2 for 9B and 86.5 for 27B.
  - Training lifts the 4B from 76.8 to 85.0.
  - Text and JSON only.

**"Same Attack, Different Judge"** ([arXiv 2610.17820](https://arxiv.org/pdf/2610.17820))
- **What it tests:** a controlled adversarial comparison of Jev 1.13, LLM judges and purpose-built
  safety classifiers.
- **Input moderation:** Jev was the most robust. Forged structured input never got a blocked request
  through, and an innocent-sounding "teaching slide" framing got 9% of Jev's blocks through, against
  54–56% for two frontier LLM judges.
- **Phishing detection:** Jev was among the weaker systems, detecting 62% unattacked against 77–81% for
  the GLM judges. Attacks then bypassed 19–29% of its detections.
- **Speed and cost:** Jev had the lowest median latency (0.7 s, against 4–24 s) and the lowest price.
- **Purpose-built classifiers aren't automatically robust.** One planted statement made Llama Guard 3
  miss 62% of the phishing emails it had flagged.
- **False positives on 100 harmless requests** (JailbreakBench's benign set, strict routing, Table 2):

| System | False-positive rate [95% CI] |
|---|---|
| Jev 1.13 | 0.27 [0.19, 0.36] |
| GPT-6 Astra | 0.15 [0.09, 0.22] |
| GPT-5.6 Sol | 0.17 [0.10, 0.25] |
| GLM-5.3 | 0.12 [0.06, 0.19] |
| GLM-5.2 | 0.09 [0.04, 0.15] |
| GLM-5.1 | 0.12 [0.06, 0.19] |

**"Jev in the Wild"** ([arXiv 2609.30216](https://arxiv.org/pdf/2609.30216); Ling, Xue, Ye)
- **Data:** 2,170 public GitHub projects as of September 22, 2026.
- **Domains:** Content & Expert Tasks 17.8%, Search & Memory 17.6%, Software Engineering 14.2%,
  Simulation & Control 11.6%, Routing & Automation 11.5%, Infrastructure 11.2%, Interface Agents 8.1%,
  Safety & Governance 8.1%.
- **Purposes:** attribute judgment 77%, scoring and ranking 52%, action selection 31%, outcome judgment
  20%, content filtering 15%, model or tool selection 13%.
- **Interfaces:** Choice in 81.0% of projects, Noul in 72.2%, Score in 45.4%.
- **Attention:** stars are concentrated; Routing & Automation has 41.4% of them from 11.5% of the
  projects.
- **Media:** no photo-tagging or media projects are identified.

### 13. Open Jev-style checkpoints served by `mode-io/vllm-jev`

**The server itself** is Apache-2.0 ([github.com/mode-io/vllm-jev](https://github.com/mode-io/vllm-jev)):
- **On a Mac:** it needs Apple Silicon and macOS 15+. Text models run through MLX (Laya through PyTorch
  MPS), and Valen runs multimodal through MLX.
- **Concurrency:** a Mac serves one request at a time.
- **Hardware tested:** the only Mac benchmark machine is an **M5 with 16 GB**. On it, OpenJev-0.6B
  answers text in 67.9 ms and Valen-Preview-0923 answers about an image in 205.9 ms.
- **M1:** not tested.

| Model | Input | Size | Licence | Accuracy found | Runs on a Mac in `vllm-jev`? | M1 16 GB |
|---|---|---|---|---|---|---|
| **Open-Jev-2B** (ZefanCai) | Text | 2B (Qwen3.5-2B + LoRA + head) | Code MIT, Qwen base Apache-2.0; checkpoint licence not stated ([wiki](https://github.com/Zefan-Cai/Open-Jev/wiki)) | JevBench public 64.94%, Hard 41.44% (same) | Yes | Likely (2B), untested |
| Open-Jev-9B | Text | 9B | as above | JevBench public 77.49%, Hard 59.46% | No (Linux) | bf16 is ~18 GB **(my arithmetic)**: too big unquantized |
| Open-Jev-27B v1.1 | Text | 27B | as above | JevBench public 85.28%, Hard 72.07% | Not listed | No |
| OpenJev-0.6B (IamBusy) | Text | 0.6B | not checked | — | Yes | Yes |
| Tiny-Jev (lostargon) | Text | not stated | not checked | — | Yes | Likely |
| Laya (convaiinnovations) | Text | not stated | not checked | — | Yes (MPS) | Likely |
| **Valen-Preview-0923** | Text, images, video (video experimental since 2026-09-28) | not stated | **not found** for this checkpoint; later Valen-Decision 1.5 releases are listed as Apache-2.0 by a third party ([madebyagents](https://www.madebyagents.com/models/valen-decision-1-5-4b)) | Matched the Linux reference on all 42 test questions; 86.8% target-support accuracy on its image test ([vllm-jev](https://github.com/mode-io/vllm-jev)); theory-of-mind suite 49.5 vs Jev 73.2 ([arXiv 2610.18610](https://arxiv.org/pdf/2610.18610)) | **Yes (MLX)** | The only multimodal option with a Mac path; untested on M1 |
| vjev-vision / -pilot (yah01) | Text, images | 4.5B (Qwen3.5-4B) | Apache-2.0 per [Featherless](https://featherless.ai/models/yah01/vjev-vision) (secondary) | — | No (Linux CUDA) | Not through `vllm-jev` |
| RSI-Jev v4.0-VL (shgao) | Text, images | 2B (Qwen3.5-2B) | not checked | — | No | — |
| JevAny-Qwen3.5-4B-LoRA | Text, images | 4B | not checked | Authors claim the 27B beats Jev 1.13 on JevBench (secondary, LinkedIn) | No | — |
| **Clef-flash** (Cloudflare) | Text, JSON, images, video | 9B (Qwen3.5-9B) | **Apache-2.0** ([model card](https://huggingface.co/Cloudflare/clef-flash)) | **(vendor)** BFCL 98.8, median 38.8 ms on an H200; 57.1–77.0 on four TypeSafe workflows | No (Linux CUDA) | Not through `vllm-jev`; 65 community quantizations exist, with no MLX path documented |
| Clef (Cloudflare) | Text, JSON, images, video | 27B (Qwen3.8-27B) | Apache-2.0 ([announcement](https://developers.cloudflare.com/changelog/post/2026-10-01-clef-workers-ai/)) | **(vendor)** first on Cloudflare's Decision Index | No | No |
| Kev, Decider, Mica, This-That, JevK5 | Text | 0.6–27B | not checked | the README warns that Kev-0.8B, This-That and Decider-2B can change answers under concurrency | Some (Decider-0.8B, This-That 1.2, JevK5-2B) | — |

**Best candidate for classifying photo captions locally:**

- **For text captions, the cheapest thing to try is no extra model.** The "Inner Jev" paper's readout
  (lettered options, one forward pass, softmax over the letter logits) works with any instruction LLM.
  Its zero-shot numbers are 76.8 for Qwen3.5-4B and 82.2 for 9B. The project already plans to run
  Qwen3.5-9B for captions, so the same model can answer typed questions with calibrated probabilities.
  A 4-bit 9B is about 5 GB of weights **(my arithmetic)** and fits in 16 GB. A related paper reports that
  a vision-language backbone answers *image* questions the same way, zero-shot: Qwen3.5-4B scores 82.4%
  on MMBench ([arXiv 2610.02076](https://arxiv.org/pdf/2610.02076)). So the caption step could
  sometimes be skipped.
- **If a packaged decision model is preferred:** Open-Jev-2B (text) or Valen-Preview-0923 (images) are
  the two that `vllm-jev` serves on a Mac. Check Valen's licence first. Clef-flash is the best-licensed
  multimodal one, but it would need its own MLX or llama.cpp path on the Mac.

---

## E. Recommendation for Photo Estate

### 14. Caption → typed questions → filter columns: which engine?

The pipeline shape is sound. **Typed questions with probabilities are a better fit for filter columns
than free-text tags**: they give a fixed vocabulary and a threshold per question. That matches what
TAGGING-RESEARCH.md already argues for SigLIP zero-shot tags. The question is which engine answers.

| | (a) Hosted Jev | (b) Local Jev-style model | (c) Linear heads on SigLIP vectors |
|---|---|---|---|
| **Cost, 100k photos** | ~$2–4 for 500–1,000 input tokens per photo at $0.042/M **(my arithmetic, vendor price)**; images extra, at an unknown token count | Electricity; one more model run over the photos that have captions | ~0: a dot product per head over vectors already stored |
| **Privacy** | **Breaks the rule.** Captions, names, places and dates leave the house. No published retention period, region or on-prem option; ZDR for enterprise only | Nothing leaves | Nothing leaves |
| **Setup effort** | Low: an API key and ~30 lines | Medium: a Qwen3.5-9B letter-logit readout (or `vllm-jev` on the Mac) plus per-question thresholds | Low to medium: label ~100–300 examples per attribute and train as in chapter 25 (`train_head.py`) |
| **Coverage** | Only photos with a caption (today: documents and one per event) unless images are sent | Same as (a) | **All 100k photos**, including new ones in seconds |
| **Quality** | Good zero-shot on questions that need reasoning over caption + metadata. Calibrated in aggregate. Known weak spots: knowledge-heavy and expert tasks, and strict-policy false positives (27% in the safety study) | Close to Jev at 9–31B according to the "Inner Jev" paper; weaker at 2B (JevBench 65% vs 85% at 27B) | Best for **visual** attributes you can label (taste worked this way). Can't use place, date, people or album unless they are added as features. Each new question needs labels |

**Recommendation:**
1. **(c) first, for anything visual.** Indoor/outdoor, beach, snow, food, document, screenshot,
   "kids", "dog": these are attributes of pixels. SigLIP vectors already hold them, and a head per
   attribute covers the whole library offline at zero cost. Start each head from the SigLIP zero-shot
   score already planned in Tier 1, and train a head only where zero-shot is poor on the benchmark
   album.
2. **(b) for questions that need context**, run on the captioned subset with Qwen3.5-9B's option
   probabilities. Examples: "Is this a birthday?" (caption + date + people), "Is this a work trip?"
   (place + album + time), and "What kind of document is this?". Use the same JSON state as the Jev
   example above. Store the probability, not just the answer, so the threshold can be moved later.
3. **(a) only as a yardstick**, if at all. Run hosted Jev on the demo library or the 137-file benchmark
   album (photos you're willing to send) to see whether the local answers are far off. Don't run it on
   the real library.
4. Add the three arms to the existing bake-off: zero-shot SigLIP vs trained head vs local typed
   questions, scored on the ~20 real searches.

### 15. Videos on an M1 Mac mini with 16 GB

| Approach | Gives | Cost on an M1 | Fit |
|---|---|---|---|
| **Scene-cut keyframes → SigLIP** (roadmap item 5) | Text search over videos, "more like this", and every linear head from question 14, through the same path as photos | One SigLIP pass per keyframe; PySceneDetect cuts as in Elastic's demo | **Best default** |
| VLM over keyframes (Qwen3.5-9B) | A caption per video or scene, for full-text search and typed questions | Seconds per keyframe; worth it for a few keyframes per video, not all of them | **Add selectively**: one keyframe per video, or per scene for long ones |
| Video decision model (Valen on MLX, video experimental; Clef-flash on Linux) | Typed answers straight from frames | Valen is the only one with a Mac path; video support is experimental, licence unknown | Revisit later |
| Video embedding (Jina v5 omni, 32 frames per chunk) | One vector per scene, text-searchable | The Elastic demo calls it through the Jina API, which is hosted; I didn't check whether local weights exist | Breaks the rule if hosted |
| V-JEPA 2 + attentive probe | Motion and action classes (SSv2 77.3% with a probe) ([vjepa2](https://raw.githubusercontent.com/facebookresearch/vjepa2/main/README.md)) | A probe has to be trained per label set, and the features aren't aligned with text, so they give no search | **Not a fit**: built for action recognition and robotics, not family-video search |

**Recommendation:** cut at scenes with PySceneDetect. Embed the middle frame of each scene with SigLIP
2 into the existing vectors table, keyed to the video and timestamp, so search can return "the right
seconds" as Omnishot does. Then caption one keyframe per video with the local VLM. That reuses
everything built for photos and keeps everything in the house.

---

## Sources

Primary:
- Elastic: [GTC D.C. page](https://www.elastic.co/events/nvidia-gtc-washington-dc), [GTC San Jose](https://www.elastic.co/events/nvidia-gtc), [GTC Berlin](https://www.elastic.co/events/nvidia-gtc-berlin), [Ignite](https://www.elastic.co/events/microsoft-ignite), [partner FAQ](https://www.elastic.co/partners/nvidia)
- Elastic GPU indexing: [Chapter 1](https://www.elastic.co/search-labs/blog/gpu-accelerated-vector-search-elasticsearch-nvidia), [Chapter 2](https://www.elastic.co/search-labs/blog/elasticsearch-gpu-accelerated-vector-indexing-nvidia), [cuvs-java plugin](https://www.elastic.co/search-labs/blog/nvidia-cuvs-elasticsearch-gpu-vector-indexing), [138M vectors](https://www.elastic.co/search-labs/blog/vector-indexing-gpu-elasticsearch-cuvs), [docs](https://www.elastic.co/docs/solutions/search/vector/gpu-vector-indexing), [mapping reference](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/gpu-vector-indexing), [docs preview build](https://docs-v3-preview.elastic.dev/elastic/docs-content/pull/8179/solutions/search/vector/gpu-vector-indexing.md)
- Elastic BBQ: [BBQ](https://www.elastic.co/search-labs/blog/better-binary-quantization-lucene-elasticsearch), [OSQ](https://www.elastic.co/search-labs/blog/optimized-scalar-quantization-elasticsearch), [measuring recall](https://www.elastic.co/search-labs/blog/recall-vector-search-quantization), [Jina v5 + BBQ](https://www.elastic.co/search-labs/blog/bbq-quantization-jina-embeddings-v5), [DiskBBQ vs Qdrant](https://www.elastic.co/search-labs/blog/vector-search-benchmark-elasticsearch-vs-qdrant), [video search with Jina](https://www.elastic.co/search-labs/blog/ai-video-search-jina-embeddings)
- TypeSafe: [System One](https://docs.typesafe.ai/concepts/system-one), [State](https://docs.typesafe.ai/concepts/state.md), [Images](https://docs.typesafe.ai/images.md), [Models](https://docs.typesafe.ai/models.md), [Errors](https://docs.typesafe.ai/api/errors.md), [Quick start](https://docs.typesafe.ai/introduction/quickstart), [Legal](https://docs.typesafe.ai/legal.md), [docs index](https://docs.typesafe.ai/llms.txt)
- Papers: [2610.17205](https://arxiv.org/pdf/2610.17205), [2610.17820](https://arxiv.org/pdf/2610.17820), [2609.30216](https://arxiv.org/pdf/2609.30216), [2610.06625](https://arxiv.org/pdf/2610.06625), [2610.02076](https://arxiv.org/pdf/2610.02076), [2610.18610](https://arxiv.org/pdf/2610.18610), [2605.17524](https://arxiv.org/pdf/2605.17524), [2605.02171](https://arxiv.org/html/2605.02171v3), [V-JEPA 2](https://www.arxiv.org/pdf/2506.09985)
- Models: [vllm-jev](https://github.com/mode-io/vllm-jev), [Open-Jev wiki](https://github.com/Zefan-Cai/Open-Jev/wiki), [Clef-flash](https://huggingface.co/Cloudflare/clef-flash), [Clef announcement](https://developers.cloudflare.com/changelog/post/2026-10-01-clef-workers-ai/)
- NVIDIA: [GTC D.C. dates](https://www.nvidia.com/en-us/gtc-dc/attend/), [SC26](https://www.nvidia.com/en-us/events/supercomputing/); Microsoft: [NVIDIA at Ignite](https://techcommunity.microsoft.com/blog/azureinfrastructureblog/join-nvidia-and-microsoft-at-ignite-2026/4573012)

Secondary, used only where noted: [InfoQ](https://infoq.com/news/2026/10/typesafe-ai-jev-released) (latency), [opentweet.io](https://opentweet.io/jev/limits) (option limits), [Featherless](https://featherless.ai/models/yah01/vjev-vision) (vjev licence), [madebyagents](https://www.madebyagents.com/models/valen-decision-1-5-4b) (Valen 1.5 licence), [Qdrant's rebuttal](https://qdrant.tech/blog/benchmark-elastic-diskbbq/).

**Could not be read:** the TypeSafe DPA and changelog, Elastic's maths post for optimised scalar
quantization, and the Hugging Face model cards for Open-Jev-2B, Valen-Preview-0923 and the smaller
text checkpoints. Their licences and retention terms are open questions above.
