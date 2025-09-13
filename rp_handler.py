import os
import sys
import time
import json
import base64
import subprocess
from pathlib import Path

import runpod  # required by RunPod Queue endpoints

# ---------- Config ----------
MODELS_DIR = Path(os.getenv("COMFYUI_MODEL_DIR", "/runpod-volume/models"))
OUT_DIR    = Path(os.getenv("COMFYUI_OUT_DIR", "/runpod-volume/out"))
COMFY_REPO = Path("/workspace/ComfyUI")
VENV       = COMFY_REPO / ".venv"
PY         = str(VENV / "bin/python")

MANIFEST   = Path("/workspace/model_manifest.txt")

S3_BUCKET  = os.getenv("S3_BUCKET", "")
S3_PREFIX  = os.getenv("S3_PREFIX", "models/")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

IMAGE_VERSION = os.getenv("IMAGE_VERSION", "unset")
ENABLE_AWS_DIAG = os.getenv("AWS_DIAG", "0") == "1"   # set AWS_DIAG=1 to print STS + ls

# ---------- Logging ----------
_t0 = time.time()
def log(msg: str):
    dt = time.time() - _t0
    print(f"[rp][{dt:7.2f}s] {msg}", flush=True)

def _run(cmd, *, capture=True, env=None):
    """Run a command with logging; returns CompletedProcess."""
    log(f"$ {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=capture, text=True, env=env)
    log(f"rc={res.returncode}")
    if capture:
        if res.stdout:
            sys.stdout.write(res.stdout[:2000] + ("\n[rp] ... (stdout truncated)\n" if len(res.stdout) > 2000 else "\n"))
        if res.stderr:
            sys.stderr.write(res.stderr[:2000] + ("\n[rp] ... (stderr truncated)\n" if len(res.stderr) > 2000 else "\n"))
    return res

# ---------- Diagnostics ----------
def aws_diag():
    try:
        _run(["aws", "--version"], capture=True)
        _run(["aws", "sts", "get-caller-identity"], capture=True)
        if S3_BUCKET:
            _run(["aws", "s3", "ls", f"s3://{S3_BUCKET}/{S3_PREFIX}", "--region", AWS_REGION], capture=True)
    except Exception as e:
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

# ---------- Execute Comfy (robust entrypoint + flag fallback) ----------
def _run_with_alt_flags(cmd_base, env):
    """
    Try command first with --output-directory, then retry with --output-path
    if exit code == 2 (likely argparse error). Return the final CompletedProcess.
    """
    def _exec(cmd):
        t0 = time.time()
        res = subprocess.run(cmd, capture_output=True, text=True, env=env)
        log(f"ran: {' '.join(cmd[:3])} ... -> rc={res.returncode} in {time.time()-t0:.2f}s")
        if res.stdout:
            sys.stdout.write(res.stdout[:2000] + ("\n[rp] ... (stdout truncated)\n" if len(res.stdout) > 2000 else "\n"))
        if res.stderr:
            sys.stderr.write(res.stderr[:2000] + ("\n[rp] ... (stderr truncated)\n" if len(res.stderr) > 2000 else "\n"))
        return res

    res = _exec(cmd_base)
    if res.returncode == 2 and "--output-directory" in cmd_base:
        alt = [("--output-path" if x == "--output-directory" else x) for x in cmd_base]
        log("[rp] retrying with --output-path")
        res = _exec(alt)
    return res

def run_comfy_workflow(workflow_json: dict) -> dict:
    wf_path = COMFY_REPO / "input_workflow.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Clean previous files in OUT_DIR (non-recursive)
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

    exec_py = COMFY_REPO / "execute.py"
    main_py = COMFY_REPO / "main.py"

    candidates = []
    if exec_py.exists():
        candidates.append([PY, str(exec_py), "--workflow", str(wf_path), "--output-directory", str(OUT_DIR)])
    if main_py.exists():
        # keep headless if main.py wants to open a UI
        candidates.append([PY, str(main_py), "--workflow", str(wf_path), "--output-directory", str(OUT_DIR), "--disable-auto-launch"])
    # module runner fallback (newer ComfyUI trees)
    candidates.append([PY, "-m", "comfy.cli", "--workflow", str(wf_path), "--output-directory", str(OUT_DIR)])

    last = None
    for cmd in candidates:
        log(f"trying runner: {' '.join(cmd[:3])} ...")
        last = _run_with_alt_flags(cmd, env)
        if last.returncode == 0:
            break

    if last is None or last.returncode != 0:
        raise subprocess.CalledProcessError(
            last.returncode if last else -1,
            "runner",
            last.stdout if last else "",
            last.stderr if last else "no runner succeeded"
        )

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

# ---------- Boot ----------
log(f"boot: image_version={IMAGE_VERSION}")
log(f"boot: model_dir={MODELS_DIR}")
log(f"boot: out_dir={OUT_DIR}")
log(f"boot: bucket={S3_BUCKET} prefix={S3_PREFIX} region={AWS_REGION}")
log(f"boot: env AWS_ACCESS_KEY_ID present? {'AWS_ACCESS_KEY_ID' in os.environ}")
log(f"boot: env AWS_SECRET_ACCESS_KEY present? {'AWS_SECRET_ACCESS_KEY' in os.environ}")

if ENABLE_AWS_DIAG:
    aws_diag()

# Cold start sync (non-fatal if it fails; handler will still return error details)
try:
    t_sync = time.time()
    ensure_models_cached()
    log(f"model cache check/sync took {time.time() - t_sync:.2f}s")
except Exception as e:
    log(f"[startup][WARN] model sync problem: {e}")

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
            sys.stderr.write(e.stderr[:4000] + ("\n[rp] ... (stderr truncated)\n" if len(e.stderr) > 4000 else "\n"))
        return {"error": f"subprocess failed: {e}"}
    except Exception as e:
        log(f"[handler][ERROR] {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}

# ---------- Serverless loop ----------
if __name__ == "__main__":
    log("starting runpod serverless loop ...")
    runpod.serverless.start({"handler": handler})

