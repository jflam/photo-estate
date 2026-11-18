#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["numpy", "scipy", "scikit-learn", "torch", "pillow>=10.0"]
# ///
"""
train_head.py — aesthetics pilot steps 4-5: how well does a small head on the stored 768-d SigLIP 2 base embeddings
reproduce the teacher's score? 5-fold CV of ridge and a small MLP, learning curve, breakdowns, contact sheets.
All outputs stay in the pilot cache dir (private photos); nothing is written to the repo.

  uv run train_head.py [--dir ~/Library/Caches/photo-estate-aesthetics/pilot]
"""
import argparse, csv, json
from pathlib import Path
import numpy as np
from scipy.stats import spearmanr, pearsonr
from sklearn.linear_model import RidgeCV
from sklearn.model_selection import KFold


def pair_acc(y, p, n=200_000, margin=0.0, seed=0):
    rng = np.random.default_rng(seed)
    i, j = rng.integers(0, len(y), n), rng.integers(0, len(y), n)
    k = np.abs(y[i] - y[j]) > margin
    i, j = i[k], j[k]
    return float(np.mean(np.sign(y[i] - y[j]) == np.sign(p[i] - p[j]))), int(k.sum())


def metrics(y, p):
    return dict(spearman=float(spearmanr(y, p)[0]), pearson=float(pearsonr(y, p)[0]), mae=float(np.mean(np.abs(y - p))),
                pair_all=pair_acc(y, p)[0], pair_gt05=pair_acc(y, p, margin=0.5)[0])


ALPHAS = np.logspace(-3, 2, 11)


def fit_ridge(X, y):
    m = RidgeCV(alphas=ALPHAS, cv=5).fit(X, y)  # inner CV picks alpha
    return m.predict, float(m.alpha_)


def fit_mlp(X, y, seed=0):
    import torch, torch.nn as nn
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(X)); nv = max(1, len(X) // 8)
    va, tr = idx[:nv], idx[nv:]
    mu, sd = y[tr].mean(), y[tr].std()
    Xt, yt = torch.tensor(X[tr]), torch.tensor((y[tr] - mu) / sd, dtype=torch.float32)
    Xv, yv = torch.tensor(X[va]), torch.tensor((y[va] - mu) / sd, dtype=torch.float32)
    net = nn.Sequential(nn.Dropout(0.1), nn.Linear(X.shape[1], 256), nn.GELU(), nn.Dropout(0.3), nn.Linear(256, 1))
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-2)
    best, best_state, bad = 1e9, None, 0
    for ep in range(300):
        net.train()
        perm = torch.randperm(len(Xt))
        for k in range(0, len(perm), 64):
            b = perm[k:k + 64]
            opt.zero_grad(); loss = nn.functional.mse_loss(net(Xt[b]).squeeze(-1), yt[b]); loss.backward(); opt.step()
        net.eval()
        with torch.no_grad():
            v = nn.functional.mse_loss(net(Xv).squeeze(-1), yv).item()
        if v < best - 1e-5:
            best, bad = v, 0
            best_state = {k: t.clone() for k, t in net.state_dict().items()}
        else:
            bad += 1
            if bad >= 20: break
    net.load_state_dict(best_state); net.eval()
    def predict(Z):
        with torch.no_grad():
            return net(torch.tensor(Z)).squeeze(-1).numpy() * sd + mu
    return predict, ep + 1


def sheet(d, shas, captions, path, cols=6, cell=256):
    from PIL import Image, ImageDraw
    rows = (len(shas) + cols - 1) // cols
    cap = 30
    S = Image.new("RGB", (cols * cell, rows * (cell + cap)), "black")
    dr = ImageDraw.Draw(S)
    for n, (s, c) in enumerate(zip(shas, captions)):
        with Image.open(d / "img" / f"{s}.jpg") as im:
            im = im.convert("RGB"); im.thumbnail((cell, cell))
            x, y = (n % cols) * cell, (n // cols) * (cell + cap)
            S.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
            dr.text((x + 4, y + cell + 4), c, fill="white")
    S.save(path, "JPEG", quality=85)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path.home() / "Library/Caches/photo-estate-aesthetics/pilot"))
    a = ap.parse_args()
    d = Path(a.dir).expanduser()
    z = np.load(d / "sample.npz")
    sc = {r["sha"]: float(r["score"]) for r in csv.DictReader(open(d / "teacher.csv"))}
    keep = [i for i, s in enumerate(z["sha"]) if s in sc]
    sha, X = z["sha"][keep], z["vec"][keep].astype(np.float32)
    y = np.array([sc[s] for s in sha]); make = z["make"][keep]; fav = z["favorited"][keep]
    N = len(y); print(f"N={N}")

    q = np.percentile(y, [0, 5, 25, 50, 75, 95, 100])
    print("teacher score: min %.2f p5 %.2f p25 %.2f p50 %.2f p75 %.2f p95 %.2f max %.2f | mean %.3f std %.3f" % (*q, y.mean(), y.std()))

    oof = {"ridge": np.zeros(N), "mlp": np.zeros(N)}
    info = {"ridge": [], "mlp": []}
    per_fold = {"ridge": [], "mlp": []}
    for f, (tr, te) in enumerate(KFold(5, shuffle=True, random_state=44).split(X)):
        for name, fit in (("ridge", fit_ridge), ("mlp", fit_mlp)):
            pred, extra = fit(X[tr], y[tr]); oof[name][te] = pred(X[te]); info[name].append(extra)
            per_fold[name].append(metrics(y[te], oof[name][te]))
    print("\nheld-out (5-fold; mean of fold metrics ± std across folds | pooled out-of-fold)")
    print(f"{'model':6} {'spearman':>14} {'pearson':>14} {'MAE':>14} {'pair all':>14} {'pair |d|>0.5':>14}")
    for name in oof:
        row = []
        for k in ("spearman", "pearson", "mae", "pair_all", "pair_gt05"):
            v = np.array([m[k] for m in per_fold[name]]); row.append(f"{v.mean():.3f}±{v.std():.3f}")
        print(f"{name:6} " + " ".join(f"{r:>14}" for r in row))
        pm = metrics(y, oof[name]); print(f"{'  pooled':6} " + " ".join(f"{pm[k]:>14.3f}" for k in ("spearman", "pearson", "mae", "pair_all", "pair_gt05")))
    print("ridge alpha per fold:", info["ridge"], "| mlp epochs run per fold:", info["mlp"])
    print(f"baseline: predicting the mean gives MAE {np.mean(np.abs(y - y.mean())):.3f}")

    print("\nlearning curve: held-out Spearman of ridge (mean over 5 folds; sizes capped at fold's training set)")
    folds = list(KFold(5, shuffle=True, random_state=44).split(X))
    for n in (250, 500, 1000, None):
        v = []
        for tr, te in folds:
            sub = tr[np.random.default_rng(1).permutation(len(tr))[: n or len(tr)]]
            p, _ = fit_ridge(X[sub], y[sub]); v.append(spearmanr(y[te], p(X[te]))[0])
        print(f"  n={n or 'all'} ({min(n or len(folds[0][0]), len(folds[0][0]))}): {np.mean(v):.3f} ± {np.std(v):.3f}")

    print("\nmean teacher score by camera make")
    names, cnt = np.unique(make, return_counts=True)
    top = [n for n in names[np.argsort(-cnt)] if n][:6]
    for n in top: print(f"  {n:20} n={int((make == n).sum()):4} mean={y[make == n].mean():.3f}")
    print(f"  {'(no make)':20} n={int((make == '').sum()):4} mean={y[make == ''].mean():.3f}")
    if fav.sum():
        print(f"favourites: n={int(fav.sum())} mean={y[fav == 1].mean():.3f} | rest: n={int((fav == 0).sum())} mean={y[fav == 0].mean():.3f}")
    else:
        print("favourites: none in sample")

    o = np.argsort(y)
    sheet(d, sha[o[::-1][:30]], [f"{y[i]:.2f}" for i in o[::-1][:30]], d / "teacher_top.jpg")
    sheet(d, sha[o[:30]], [f"{y[i]:.2f}" for i in o[:30]], d / "teacher_bottom.jpg")
    best = "mlp" if np.mean([m["spearman"] for m in per_fold["mlp"]]) >= np.mean([m["spearman"] for m in per_fold["ridge"]]) else "ridge"
    dis = np.argsort(-np.abs(y - oof[best]))[:30]
    sheet(d, sha[dis], [f"T {y[i]:.2f}  S {oof[best][i]:.2f}" for i in dis], d / "disagree.jpg")
    print(f"\ncontact sheets written (disagree uses held-out {best} predictions)")
    json.dump({"y": y.tolist(), "ridge": oof["ridge"].tolist(), "mlp": oof["mlp"].tolist()}, open(d / "oof.json", "w"))


if __name__ == "__main__":
    main()
