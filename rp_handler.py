import os
import sys
import time
import json
import base64
import subprocess
from pathlib import Path

import runpod  # provided by runpod/base image

# ---------- Config ----------
MODELS_DIR = Path(os.getenv("COMFYUI_MODEL_DIR", "/runpod-volume/models"))
MANIFEST   = Path("/workspace/model_manifest.txt")

S3_BUCKET  = os.getenv("S3_BUCKET", "")
S3_PREFIX  = os.getenv("S3_PREFIX", "models/")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

COMFY_REPO = Path("/workspace/ComfyUI")
VENV       = COMFY_REPO / ".venv"
PY         = str(VENV / "bin/python")

# Persist outputs so you can inspect them between runs if desired.
OUT_DIR    = Path(os.getenv("COMFYUI_OUT_DIR", "/runpod-volume/out"))

IMAGE_VERSION = os.getenv("IMAGE_VERSION", "unset")
ENABLE_AWS_DIAG = os.getenv("AWS_DIAG", "0") == "1"   # set AWS_DIAG=1 to print STS + ls

# ---------- Logging helpers ----------
_t0 = time.time()
def log(msg: str):
    dt = time.time() - _t0
    print(f"[rp][{dt:7.2f}s] {msg}", flush=True)

def run(cmd, check=True, capture=False, env=None):
    """Subprocess wrapper with logging."""
    log(f"$ {' '.join(cmd)}")
    if capture:
        res = subprocess.run(cmd, check=False, text=True, capture_output=True, env=env)
        if res.stdout:
            sys.stdout.write(res.stdout[:2000])  # cap noisy outputs
            if len(res.stdout) > 2000:
                sys.stdout.write("\n[rp] ... (stdout truncated)\n")
        if res.stderr:
            sys.stderr.write(res.stderr[:2000])
            if len(res.stderr) > 2000:
                sys.stderr.write("\n[rp] ... (stderr truncated)\n")
        if check and res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
        return res
    else:
        return subprocess.run(cmd, check=check, env=env)

# ---------- Diagnostics ----------
def aws_diag():
    try:
        log("AWS diagnostics (set AWS_DIAG=1 to enable) ...")
        run(["aws", "--version"], check=False)
        run(["aws", "sts", "get-caller-identity"], capture=True)
        if S3_BUCKET:
            run(["aws", "s3", "ls", f"s3://{S3_BUCKET}/{S3_PREFIX}", "--region", AWS_REGION], capture=True)
        else:
            log("S3_BUCKET not set; skipping bucket list.")
    except subprocess.CalledProcessError as e:
        log(f"[diag][ERROR] {e}")

# ---------- Cold start: ensure model cache ----------
def ensure_models_cached():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if not S3_BUCKET or not MANIFEST.exists():
        log("S3_BUCKET unset or manifest missing; skipping model sync.")
        return

    missing = 0
    with MANIFEST.open() as f:
        for raw in f:
            key = raw.strip()
            if not key or key.startswith("#"):
                continue
            dest = MODELS_DIR / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                missing += 1
                s3_uri = f"s3://{S3_BUCKET}/{S3_PREFIX}{key}"
                log(f"fetching {s3_uri} -> {dest}")
                res = subprocess.run(
                    ["aws", "s3", "cp", s3_uri, str(dest), "--region", AWS_REGION],
                    capture_output=True, text=True
                )
                if res.returncode != 0:
                    log(f"[startup][ERROR] cp failed for {s3_uri}")
                    if res.stderr:
                        sys.stderr.write(res.stderr[:2000] + ("\n[rp] ... (stderr truncated)\n" if len(res.stderr) > 2000 else "\n"))
                    raise subprocess.CalledProcessError(res.returncode, res.args, res.stdout, res.stderr)
            else:
                log(f"cache hit: {dest}")
    if missing == 0:
        log("all manifest files present in cache")

# ---------- Run a single ComfyUI workflow ----------
def run_comfy_workflow(workflow_json: dict) -> dict:
    wf_path = COMFY_REPO / "input_workflow.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Clean old files (don’t nuke subdirs)
    for p in OUT_DIR.glob("*"):
        try:
            if p.is_file():
                p.unlink()
        except Exception as e:
            log(f"warn: could not remove {p}: {e}")

    wf_path.write_text(json.dumps(workflow_json))
    log(f"workflow saved: {wf_path}")

    env = os.environ.copy()
    env["COMFYUI_MODEL_DIR"] = str(MODELS_DIR)

    # Execute ComfyUI once via execute.py (headless)
    cmd = [
        PY,
        str(COMFY_REPO / "execute.py"),
        "--workflow", str(wf_path),
        "--output-directory", str(OUT_DIR),
    ]

    t = time.time()
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    log(f"execute.py finished with code {res.returncode} in {time.time() - t:.2f}s")

    if res.stdout:
        sys.stdout.write(res.stdout[:2000] + ("\n[rp] ... (stdout truncated)\n" if len(res.stdout) > 2000 else "\n"))
    if res.stderr:
        sys.stderr.write(res.stderr[:2000] + ("\n[rp] ... (stderr truncated)\n" if len(res.stderr) > 2000 else "\n"))

    if res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)

    # Collect images
    images = sorted(
        (p for p in OUT_DIR.glob("**/*") if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    payload = []
    for p in images[:8]:
        with p.open("rb") as f:
            payload.append({"filename": p.name, "b64": base64.b64encode(f.read()).decode("utf-8")})
    log(f"collected {len(payload)} image(s) from {OUT_DIR}")
    return {"images": payload}

# ---------- Boot logs ----------
log(f"boot: image_version={IMAGE_VERSION}")
log(f"boot: model_dir={MODELS_DIR}")
log(f"boot: out_dir={OUT_DIR}")
log(f"boot: bucket={S3_BUCKET} prefix={S3_PREFIX} region={AWS_REGION}")
log(f"boot: env AWS_ACCESS_KEY_ID present? {'AWS_ACCESS_KEY_ID' in os.environ}")
log(f"boot: env AWS_SECRET_ACCESS_KEY present? {'AWS_SECRET_ACCESS_KEY' in os.environ}")

if ENABLE_AWS_DIAG:
    aws_diag()

# ---------- Cold start prep ----------
t_start_sync = time.time()
try:
    ensure_models_cached()
    log(f"model cache check/sync took {time.time() - t_start_sync:.2f}s")
except Exception as e:
    log(f"[startup][FATAL] model sync failed: {e}")
    # Don’t crash the process—let the handler return error details.
    # (If you prefer hard-fail, re-raise here.)
# ---------- Handler ----------
def handler(event):
    log(f"handler: event keys = {list((event or {}).keys())}")
    try:
        data = (event or {}).get("input") or {}
        wf = data.get("workflow")
        if not isinstance(wf, dict):
            log("handler: invalid input; missing input.workflow JSON object")
            return {"error": "input.workflow must be a JSON object (ComfyUI graph)"}

        t_run = time.time()
        result = run_comfy_workflow(wf)
        log(f"handler: workflow done in {time.time() - t_run:.2f}s")
        return {"status": "ok", "result": result}
    except subprocess.CalledProcessError as e:
        log(f"[handler][ERROR] subprocess failed: code={e.returncode}")
        if e.stderr:
            sys.stderr.write(e.stderr[:2000] + ("\n[rp] ... (stderr truncated)\n" if len(e.stderr) > 2000 else "\n"))
        return {"error": f"subprocess failed: {e}"}
    except Exception as e:
        log(f"[handler][ERROR] {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}

# ---------- Start serverless loop ----------
if __name__ == "__main__":
    log("starting runpod serverless loop ...")
    runpod.serverless.start({"handler": handler})

