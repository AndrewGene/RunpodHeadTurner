# ---------- Base: CUDA 12.1 (compatible with RunPod pools) ----------
FROM runpod/base:0.6.2-cuda12.1.0

ARG IMAGE_VERSION=v4.4
ENV IMAGE_VERSION=${IMAGE_VERSION}

# ---------- System deps ----------
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ffmpeg wget unzip ca-certificates python3-venv build-essential && \
    rm -rf /var/lib/apt/lists/*

# Ensure `python` is available (some bases only have python3)
RUN ln -sf /usr/bin/python3 /usr/bin/python

# ---------- AWS CLI v2 (for S3 pulls on cold start) ----------
RUN wget -q https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -O /tmp/awscliv2.zip && \
    unzip /tmp/awscliv2.zip -d /tmp && /tmp/aws/install && \
    rm -rf /tmp/aws /tmp/awscliv2.zip

# ---------- ComfyUI (headless) ----------
WORKDIR /workspace
RUN git clone --depth=1 https://github.com/comfyanonymous/ComfyUI.git

WORKDIR /workspace/ComfyUI

# venv
RUN python3 -m venv .venv

# Upgrade build tooling first
RUN . .venv/bin/activate && \
    python -m pip install --upgrade pip setuptools wheel

# CUDA 12.1–matched PyTorch (no torchaudio to save size)
RUN . .venv/bin/activate && \
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu121 \
        torch==2.3.1 torchvision==0.18.1

# ComfyUI requirements (should not re-pin torch)
RUN . .venv/bin/activate && \
    pip install --no-cache-dir -r requirements.txt

# RunPod SDK only (no extra heavy packages)
RUN . .venv/bin/activate && \
    pip install --no-cache-dir runpod && \
    rm -rf /root/.cache/pip /root/.cache

# ---------- Runtime env ----------
ENV RUNTIME_DOWNLOADS="/runpod-volume"
ENV COMFYUI_MODEL_DIR="/runpod-volume/models"
ENV COMFYUI_OUT_DIR="/runpod-volume/out"

# ---------- App files ----------
WORKDIR /workspace
COPY model_manifest.txt /workspace/model_manifest.txt
COPY rp_handler.py      /workspace/rp_handler.py

# ---------- Entrypoint ----------
CMD ["python3", "/workspace/rp_handler.py"]

