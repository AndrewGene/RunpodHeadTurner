# =========================
# Stage 1: comfyui-base
# =========================
FROM runpod/base:0.6.2-cuda12.1.0 AS comfyui-base

# ---- system deps ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ffmpeg wget unzip ca-certificates python3-venv build-essential && \
    rm -rf /var/lib/apt/lists/*

# Ensure /usr/bin/python exists
RUN ln -sf /usr/bin/python3 /usr/bin/python

# AWS CLI v2 (for S3 pulls)
RUN wget -q https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -O /tmp/awscliv2.zip && \
    unzip /tmp/awscliv2.zip -d /tmp && /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscliv2.zip

# ---- ComfyUI repo ----
WORKDIR /workspace
RUN git clone --depth=1 https://github.com/comfyanonymous/ComfyUI.git
WORKDIR /workspace/ComfyUI

# OPTIONAL: pin to a specific commit (set at build time with --build-arg COMFY_SHA=<sha>)
ARG COMFY_SHA=""
RUN if [ -n "$COMFY_SHA" ]; then \
      echo "Checking out ComfyUI commit $COMFY_SHA" && \
      git fetch --depth=1 origin "$COMFY_SHA" && \
      git checkout "$COMFY_SHA"; \
    else \
      echo "Using ComfyUI at repository HEAD"; \
    fi

# ---- venv + toolchain ----
RUN python3 -m venv .venv
RUN . .venv/bin/activate && python -m pip install --upgrade pip setuptools wheel

# PyTorch for CUDA 12.1
RUN . .venv/bin/activate && \
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu121 \
        torch==2.3.1 torchvision==0.18.1 torchaudio==2.3.1

# ComfyUI requirements
RUN . .venv/bin/activate && pip install --no-cache-dir -r requirements.txt

# Keep NumPy <2 to avoid ABI breaks with CV/ORT stacks
RUN . .venv/bin/activate && pip install --no-cache-dir "numpy<2"

# ORT-GPU first; fall back to CPU ORT on CI hosts if needed
RUN . .venv/bin/activate && \
    pip install --no-cache-dir onnxruntime-gpu==1.17.1 || \
    (echo "[warn] falling back to CPU onnxruntime" && pip install --no-cache-dir onnxruntime==1.17.1)

# Faceswap / Facerestore deps
RUN . .venv/bin/activate && \
    pip install --no-cache-dir \
      gfpgan==1.3.8 \
      facexlib==0.3.0 \
      opencv-python-headless==4.9.0.80 \
      insightface==0.7.3

# RunPod SDK + requests (for HTTP API driving)
RUN . .venv/bin/activate && pip install --no-cache-dir runpod requests

# Make 'comfy' importable without packaging:
# 1) Add repo to PYTHONPATH at runtime (use default empty to silence linters)
ENV PYTHONPATH=/workspace/ComfyUI:${PYTHONPATH:-}
# 2) Also drop a .pth file into the venv's site-packages
RUN . .venv/bin/activate && python - <<'PY'
import site, os, sys
sp = next(p for p in site.getsitepackages() if p.endswith('site-packages'))
pth = os.path.join(sp, 'comfyui_repo.pth')
with open(pth, 'w') as f:
    f.write('/workspace/ComfyUI\n')
print('wrote', pth, 'pointing to /workspace/ComfyUI', file=sys.stderr)
PY

# Clean pip caches
RUN rm -rf /root/.cache/pip /root/.cache


# =========================
# Stage 2: app (tiny, fast rebuilds)
# =========================
FROM comfyui-base AS app

# Only put mutable labels/args in the app stage so base stays cached
ARG IMAGE_VERSION=v6.6
ENV IMAGE_VERSION=${IMAGE_VERSION}

# Runtime env (models + outputs on the attached Network Volume)
ENV RUNTIME_DOWNLOADS="/runpod-volume" \
    COMFYUI_MODEL_DIR="/runpod-volume/models" \
    COMFYUI_OUT_DIR="/runpod-volume/out"

# App files (changing these only invalidates this small layer)
WORKDIR /workspace
COPY model_manifest.txt /workspace/model_manifest.txt
COPY rp_handler.py      /workspace/rp_handler.py

# Entrypoint: use the venv interpreter so all deps (incl. runpod/requests) are on sys.path
CMD ["/workspace/ComfyUI/.venv/bin/python", "/workspace/rp_handler.py"]
