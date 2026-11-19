#!/usr/bin/env bash
# Prepares a Linux cloud development environment (Claude Code on the web, a Codespace, any Debian or
# Ubuntu container) for the web app: uv, a CPU-only PyTorch, the app's Python dependencies, the SigLIP 2
# search model and exiftool. Run it once when the environment is built:
#
#   bash photo-estate/tools/cloud-setup.sh
#
# Every step is allowed to fail (a blocked download, no root): the script carries on and ends with a
# list of what is ready and what is not, so one run shows everything the environment still lacks.
# The iOS app is not set up here: it needs Xcode, which only runs on macOS.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # photo-estate/photo-estate
MODEL="google/siglip2-base-patch16-256"                    # DEFAULT_MODEL in embed.py
TORCH_CPU="https://download.pytorch.org/whl/cpu"
export PATH="$HOME/.local/bin:$PATH"

report=()
ok()   { report+=("ready    $1"); }
miss() { report+=("MISSING  $1"); }

# 1. uv, which fetches Python and every script's dependencies
if ! command -v uv >/dev/null; then
    pip install --user --quiet uv 2>/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
fi
if command -v uv >/dev/null; then ok "uv ($(uv --version))"; else miss "uv: could not be installed from PyPI or astral.sh"; fi

# 2. CPU-only PyTorch. On x86-64 Linux the PyPI wheel brings several GB of CUDA libraries that a cloud
#    container cannot use. This is written to uv's per-user settings, not the repo, so it holds for
#    every later `uv run` in this environment and changes nothing on a Mac.
if [ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ]; then
    if curl -fsSL --max-time 20 -o /dev/null "$TORCH_CPU/torch/"; then
        mkdir -p "$HOME/.config/uv"
        if ! grep -qs "$TORCH_CPU" "$HOME/.config/uv/uv.toml"; then
            printf 'extra-index-url = ["%s"]\nindex-strategy = "unsafe-best-match"\n' "$TORCH_CPU" >> "$HOME/.config/uv/uv.toml"
        fi
        ok "CPU-only PyTorch index ($TORCH_CPU)"
    else
        miss "CPU-only PyTorch index: download.pytorch.org is not reachable, so torch comes from PyPI with CUDA (works, several GB larger)"
    fi
fi

# 3. the app's dependencies, downloaded now so the first `uv run` in a session does not wait for them
if command -v uv >/dev/null; then
    if uv run --quiet "$HERE/embed.py" --help >/dev/null; then ok "embed.py dependencies (torch, transformers, pillow)"
    else miss "embed.py dependencies: \`uv run embed.py --help\` failed"; fi
    if uv run --quiet "$HERE/estate.py" --help >/dev/null; then ok "estate.py dependencies"
    else miss "estate.py dependencies: \`uv run estate.py --help\` failed"; fi
fi

# 4. the search model (1.4 GB), into the Hugging Face cache where embed.py looks for it. With it there,
#    HF_HUB_OFFLINE=1 can be set for sessions; without it, browsing works and typed search does not.
if command -v uv >/dev/null && uv run --quiet --with huggingface_hub python -c \
    "from huggingface_hub import snapshot_download; snapshot_download('$MODEL')" >/dev/null; then
    ok "search model $MODEL"
else
    miss "search model $MODEL: huggingface.co is not reachable (typed search will not work)"
fi

# 5. exiftool, used only by `estate.py exif` when a real library is ingested
if ! command -v exiftool >/dev/null && command -v apt-get >/dev/null; then
    SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo -n"
    $SUDO apt-get install -y -qq libimage-exiftool-perl >/dev/null 2>&1 \
        || { $SUDO apt-get update -qq >/dev/null 2>&1 && $SUDO apt-get install -y -qq libimage-exiftool-perl >/dev/null 2>&1; }
fi
if command -v exiftool >/dev/null; then ok "exiftool"; else miss "exiftool: not installed (only \`estate.py exif\` needs it)"; fi

echo
echo "Photo Estate cloud setup:"
printf '  %s\n' "${report[@]}"
echo
echo "Start the app on the demo library with:"
echo "  cd photo-estate && uv run embed.py serve --demo --port 8767"
