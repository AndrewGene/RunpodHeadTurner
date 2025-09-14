# =========================
# Stage 1: comfyui-base
# =========================
FROM runpod/base:0.6.2-cuda12.1.0 AS comfyui-base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ---- system deps ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ffmpeg wget unzip ca-certificates python3-venv python3-pip python3-dev \
    build-essential libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# Ensure /usr/bin/python exists
RUN ln -sf /usr/bin/python3 /usr/bin/python

# ---- AWS CLI v2 (for S3 pulls at runtime) ----
RUN wget -q https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -O /tmp/awscliv2.zip \
 && unzip /tmp/awscliv2.zip -d /tmp \
 && /tmp/aws/install \
 && rm -rf /tmp/aws /tmp/awscliv2.zip

# ---- ComfyUI repo ----
WORKDIR /workspace
RUN git clone --depth=1 https://github.com/comfyanonymous/ComfyUI.git
WORKDIR /workspace/ComfyUI

# OPTIONAL: pin to a specific commit (pass at build time: --build-arg COMFY_SHA=<sha>)
ARG COMFY_SHA=""
RUN if [ -n "$COMFY_SHA" ]; then \
      echo "Checking out ComfyUI commit $COMFY_SHA" && \
      git fetch --depth=1 origin "$COMFY_SHA" && \
      git checkout "$COMFY_SHA"; \
    else \
      echo "Using ComfyUI at repository HEAD"; \
    fi

# ---- venv + toolchain ----
RUN python3 -m venv .venv \
 && . .venv/bin/activate && python -m pip install --upgrade pip setuptools wheel

# CUDA 12.1-matched PyTorch stack
RUN . .venv/bin/activate && \
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu121 \
      torch==2.3.1 torchvision==0.18.1 torchaudio==2.3.1

# ComfyUI requirements + keep NumPy <2 to avoid ABI surprises
RUN . .venv/bin/activate && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir "numpy<2"

# ORT-GPU first; fall back to CPU ORT if GPU wheel unavailable in builder
RUN . .venv/bin/activate && \
    (pip install --no-cache-dir onnxruntime-gpu==1.17.1) || (echo "[warn] falling back to CPU onnxruntime" && pip install --no-cache-dir onnxruntime==1.17.1)

# Faceswap / facerestore deps
RUN . .venv/bin/activate && \
    pip install --no-cache-dir \
      gfpgan==1.3.8 \
      facexlib==0.3.0 \
      opencv-python-headless==4.9.0.80 \
      insightface==0.7.3

# RunPod SDK + requests (for HTTP calls to ComfyUI)
RUN . .venv/bin/activate && pip install --no-cache-dir runpod requests

# (Optional) Make repo importable via PYTHONPATH and a .pth file
ENV PYTHONPATH=/workspace/ComfyUI:${PYTHONPATH:-}
RUN . .venv/bin/activate && python - <<'PY'
import site, os, sys
sp = next(p for p in site.getsitepackages() if p.endswith('site-packages'))
pth = os.path.join(sp, 'comfyui_repo.pth')
with open(pth, 'w') as f:
    f.write('/workspace/ComfyUI\n')
print('wrote', pth, '-> /workspace/ComfyUI', file=sys.stderr)
PY

# Clean pip caches
RUN rm -rf /root/.cache/pip /root/.cache


# =========================
# Stage 2: app (tiny, fast rebuilds)
# =========================
FROM comfyui-base AS app

# Labels/args that change often live here to keep base cached
ARG IMAGE_VERSION=v7.7
ENV IMAGE_VERSION=${IMAGE_VERSION}

# Runtime env (models + outputs on the attached Network Volume)
ENV COMFYUI_MODEL_DIR="/runpod-volume/models" \
    COMFYUI_OUT_DIR="/runpod-volume/out"

WORKDIR /workspace

# Manifest used by handler’s S3 sync (safe to keep in app layer)
COPY model_manifest.txt /workspace/model_manifest.txt

# Baked fallback handler (used if S3 fetch fails at startup)
COPY rp_handler.py /workspace/_baked_rp_handler.py

# Startup script that pulls latest handler from S3 (or falls back)
COPY start-worker.sh /usr/local/bin/start-worker.sh
RUN chmod +x /usr/local/bin/start-worker.sh

# Default command: fetch handler from S3 (S3_BUCKET + HANDLER_S3_KEY) then exec it
CMD ["/usr/local/bin/start-worker.sh"]