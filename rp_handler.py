import os, sys, time, json, base64, subprocess, socket, threading
from pathlib import Path
import requests
import runpod  # RunPod SDK

# --- config paths ---
COMFY_REPO = Path("/workspace/ComfyUI")
PY = str(COMFY_REPO / ".venv/bin/python")

MODELS_DIR = Path(os.getenv("COMFYUI_MODEL_DIR", "/runpod-volume/models"))
OUT_DIR = Path(os.getenv("COMFYUI_OUT_DIR", "/runpod-volume/out"))
MANIFEST = Path("/workspace/model_manifest.txt")

# S3 env vars
S3_BUCKET = os.getenv("S3_BUCKET")
S3_PREFIX = os.getenv("S3_PREFIX", "models/")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))


# --- logging helper ---
def log(msg: str):
    sys.stdout.write(f"[rp][{time.time():7.2f}] {msg}\n")
    sys.stdout.flush()


# --- sync from S3 (cold start) ---
def sync_models_from_s3():
    if not S3_BUCKET:
        log("no S3_BUCKET set, skipping model sync")
        return
    if not MANIFEST.exists():
        log("no model_manifest.txt, skipping model sync")
        return

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open() as f:
        for raw in f:
            line = raw.strip()
            # ignore blanks and comments
            if not line or line.startswith("#"):
                continue

            relpath = line
            dest = MODELS_DIR / relpath
            if dest.exists():
                log(f"cache hit: {dest}")
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            s3_uri = f"s3://{S3_BUCKET}/{S3_PREFIX}{relpath}"
            log(f"fetching {s3_uri} -> {dest}")
            try:
                subprocess.run(
                    ["aws", "s3", "cp", s3_uri, str(dest), "--region", AWS_REGION],
                    check=True,
                    text=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                # Show a concise error; keep going on other files
                err = (e.stderr or e.stdout or "").strip()[:500]
                log(f"ERROR fetching {s3_uri}: rc={e.returncode} :: {err}")



# --- comfyui server helpers ---
def _wait_for_port(host, port, timeout=60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket() as s:
            s.settimeout(1.0)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.5)
    return False


def _start_server():
    env = os.environ.copy()
    env["COMFYUI_MODEL_DIR"] = str(MODELS_DIR)  # fine to keep
    # ensure ComfyUI/models -> /runpod-volume/models symlink as a fallback
    try:
        target = Path("/runpod-volume/models")
        link = COMFY_REPO / "models"
        if not link.exists() and target.exists():
            os.symlink(str(target), str(link))
    except Exception as e:
        log(f"symlink warn: {e}")

    cmd = [
        PY, str(COMFY_REPO / "main.py"),
        "--disable-auto-launch",
        "--listen", "127.0.0.1",
        "--port", str(COMFY_PORT),
        "--output-directory", str(OUT_DIR),
        "--base-directory", "/runpod-volume"     # <-- add this
    ]
    log(f"starting ComfyUI server: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)



# --- workflow runner ---
def run_comfy_workflow(workflow_json: dict) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # clear previous outputs
    for p in OUT_DIR.glob("*"):
        if p.is_file():
            try:
                p.unlink()
            except Exception as e:
                log(f"warn: could not remove {p}: {e}")

    proc = _start_server()

    # background log tail
    def _pipe(stream, target):
        for line in iter(stream.readline, ""):
            if not line:
                break
            target.write(line)
            target.flush()

    threading.Thread(target=_pipe, args=(proc.stdout, sys.stdout), daemon=True).start()
    threading.Thread(target=_pipe, args=(proc.stderr, sys.stderr), daemon=True).start()

    if not _wait_for_port("127.0.0.1", COMFY_PORT, timeout=60):
        proc.kill()
        raise RuntimeError("ComfyUI server did not open port")

    # submit workflow
    url = f"http://127.0.0.1:{COMFY_PORT}/prompt"
    payload = {"prompt": workflow_json}
    log(f"POST {url}")
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code != 200:
        proc.kill()
        raise RuntimeError(f"submit failed: {r.status_code} {r.text[:500]}")

    # wait for outputs
    t0 = time.time()
    imgs = []
    while time.time() - t0 < 300:
        imgs = [
            p
            for p in OUT_DIR.glob("**/*")
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ]
        if imgs:
            break
        time.sleep(0.5)

    # shutdown server
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    # collect images
    imgs = sorted(imgs, key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for p in imgs[:8]:
        try:
            result.append(
                {
                    "filename": p.name,
                    "b64": base64.b64encode(p.read_bytes()).decode("utf-8"),
                }
            )
        except Exception as e:
            log(f"warn: failed to encode {p}: {e}")

    if not result:
        raise RuntimeError("no images produced; check server stderr above")

    return {"images": result}


# --- runpod handler ---
def handler(job):
    """RunPod entrypoint"""
    t0 = time.time()
    inp = job.get("input", {})
    log(f"handler start, keys={list(inp.keys())}")

    sync_models_from_s3()

    workflow_json = inp.get("workflow")
    if isinstance(workflow_json, str):
        try:
            workflow_json = json.loads(workflow_json)
        except Exception as e:
            raise ValueError(f"invalid workflow JSON: {e}")

    if not workflow_json:
        raise ValueError("no workflow provided")

    out = run_comfy_workflow(workflow_json)
    out["execution_time"] = round(time.time() - t0, 2)
    return out


# --- main ---
if __name__ == "__main__":
    log(f"boot: image_version={os.getenv('IMAGE_VERSION','unknown')}")
    log(f"boot: model_dir={MODELS_DIR}")
    log(f"boot: out_dir={OUT_DIR}")
    log(f"boot: bucket={S3_BUCKET} prefix={S3_PREFIX} region={AWS_REGION}")
    runpod.serverless.start({"handler": handler})

