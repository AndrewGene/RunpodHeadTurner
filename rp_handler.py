import os
import sys
import time
import json
import base64
import socket
import threading
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Tuple

import requests
import runpod  # RunPod SDK


# ---------------------------
# Paths / Env
# ---------------------------
COMFY_REPO = Path("/workspace/ComfyUI")
PY = str(COMFY_REPO / ".venv/bin/python")

MODELS_DIR = Path(os.getenv("COMFYUI_MODEL_DIR", "/runpod-volume/models"))
OUT_DIR = Path(os.getenv("COMFYUI_OUT_DIR", "/runpod-volume/out"))
MANIFEST = Path("/workspace/model_manifest.txt")

S3_BUCKET = os.getenv("S3_BUCKET")
S3_PREFIX = os.getenv("S3_PREFIX", "models/")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))
FORCED_CKPT = os.getenv("FORCED_CKPT")  # optional: force a specific ckpt_name


# ---------------------------
# Logging
# ---------------------------
def log(msg: str):
    # wall-time seconds for easy correlation across processes
    sys.stdout.write(f"[rp][{time.time():.2f}] {msg}\n")
    sys.stdout.flush()


# ---------------------------
# S3 sync (ignores comments/blank lines)
# ---------------------------
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
                err = (e.stderr or e.stdout or "").strip()
                log(f"ERROR fetching {s3_uri}: rc={e.returncode} :: {err[:500]}")


# ---------------------------
# Model inventory & reconcile
# ---------------------------
def list_local_models() -> Dict[str, List[str]]:
    """Return a snapshot of available model filenames under the runpod volume."""
    roots = {
        "checkpoints": MODELS_DIR / "checkpoints",
        "vae": MODELS_DIR / "vae",
        "loras": MODELS_DIR / "loras",
        "clip": MODELS_DIR / "clip",
        "upscale_models": MODELS_DIR / "upscale_models",
        "controlnet": MODELS_DIR / "controlnet",
        "embeddings": MODELS_DIR / "embeddings",
    }
    out: Dict[str, List[str]] = {}
    for k, p in roots.items():
        if p.exists():
            out[k] = sorted([f.name for f in p.glob("*") if f.is_file()])
        else:
            out[k] = []
    return out


def summarize_models(inv: Dict[str, List[str]]) -> str:
    parts = []
    for k in ["checkpoints", "vae", "loras", "clip", "upscale_models", "controlnet", "embeddings"]:
        n = len(inv.get(k, []))
        sample = ", ".join(inv.get(k, [])[:3])
        parts.append(f"{k}: {n}" + (f" [{sample}]" if sample else ""))
    return " | ".join(parts)


def extract_ckpt_refs(workflow: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """
    Find nodes that look like checkpoint loaders and return (node_id, inputs_dict).
    Supports common node types: 'CheckpointLoaderSimple', 'CheckpointLoader'.
    """
    hits: List[Tuple[str, Dict[str, Any]]] = []
    for node_id, node in workflow.items():
        try:
            ct = node.get("class_type") or node.get("class", "")
            if ct in ("CheckpointLoaderSimple", "CheckpointLoader"):
                inputs = node.get("inputs", {})
                if isinstance(inputs, dict):
                    hits.append((node_id, inputs))
        except Exception:
            continue
    return hits


def reconcile_ckpt_names(workflow: Dict[str, Any], available_ckpts: List[str]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Ensure any 'ckpt_name' in the workflow is actually present.
    If not, auto-substitute the first available checkpoint and record a note.
    Returns (possibly modified workflow, notes).
    """
    notes: List[str] = []
    if not available_ckpts:
        return workflow, notes
    ckpt_set = set(available_ckpts)
    for node_id, inputs in extract_ckpt_refs(workflow):
        req = inputs.get("ckpt_name")
        if isinstance(req, str) and req not in ckpt_set:
            substitute = available_ckpts[0]
            inputs["ckpt_name"] = substitute
            notes.append(f"node {node_id}: ckpt_name '{req}' not found; using '{substitute}'.")
    return workflow, notes


# ---------------------------
# ComfyUI server helpers
# ---------------------------
def _ensure_models_symlink():
    """Ensure ComfyUI/models -> /runpod-volume/models for predictable discovery."""
    try:
        target = Path("/runpod-volume/models")
        link = COMFY_REPO / "models"
        if link.exists():
            if link.is_symlink():
                return
            # Attempt to replace a real dir with symlink
            try:
                for p in link.glob("*"):
                    if p.is_file():
                        p.unlink(missing_ok=True)
                link.rmdir()
            except Exception:
                pass
        os.symlink(str(target), str(link))
        log("created symlink ComfyUI/models -> /runpod-volume/models")
    except Exception as e:
        log(f"symlink warn: {e}")


def _write_extra_model_paths_yaml():
    """Write extra_model_paths.yaml to point Comfy directly at the volume."""
    try:
        yaml_text = (
            "models:\n"
            "  checkpoints: /runpod-volume/models/checkpoints\n"
            "  clip: /runpod-volume/models/clip\n"
            "  upscale_models: /runpod-volume/models/upscale_models\n"
            "  embeddings: /runpod-volume/models/embeddings\n"
            "  loras: /runpod-volume/models/loras\n"
            "  controlnet: /runpod-volume/models/controlnet\n"
            "  vae: /runpod-volume/models/vae\n"
        )
        (COMFY_REPO / "extra_model_paths.yaml").write_text(yaml_text)
        log("wrote extra_model_paths.yaml")
    except Exception as e:
        log(f"warn: could not write extra_model_paths.yaml: {e}")


def _start_server():
    env = os.environ.copy()
    env["COMFYUI_MODEL_DIR"] = str(MODELS_DIR)

    _ensure_models_symlink()
    _write_extra_model_paths_yaml()

    cmd = [
        PY,
        str(COMFY_REPO / "main.py"),
        "--disable-auto-launch",
        "--listen",
        "127.0.0.1",
        "--port",
        str(COMFY_PORT),
        "--output-directory",
        str(OUT_DIR),
    ]
    log(f"starting ComfyUI server: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_for_port_or_crash(proc, host, port, timeout=300.0):
    """Wait until port opens or the process exits; return True if open, else False."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        rc = proc.poll()
        if rc is not None:
            try:
                err = proc.stderr.read()
                if err:
                    tail = err[-4000:]
                    sys.stderr.write(tail + ("\n[rp] ... (stderr truncated)\n" if len(err) > 4000 else "\n"))
                    sys.stderr.flush()
            except Exception:
                pass
            log(f"server exited early rc={rc}")
            return False

        with socket.socket() as s:
            s.settimeout(1.0)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.5)

    try:
        err = proc.stderr.read()
        if err:
            tail = err[-4000:]
            sys.stderr.write(tail + ("\n[rp] ... (stderr truncated)\n" if len(err) > 4000 else "\n"))
            sys.stderr.flush()
    except Exception:
        pass
    return False


# ---------------------------
# Workflow runner
# ---------------------------
def run_comfy_workflow(workflow_json: Dict[str, Any]) -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clear previous outputs
    for p in OUT_DIR.glob("*"):
        if p.is_file():
            try:
                p.unlink()
            except Exception as e:
                log(f"warn: could not remove {p}: {e}")

    proc = _start_server()

    # Pipe logs (best-effort)
    def _pipe(stream, target):
        for line in iter(stream.readline, ""):
            if not line:
                break
            try:
                target.write(line)
                target.flush()
            except Exception:
                break

    threading.Thread(target=_pipe, args=(proc.stdout, sys.stdout), daemon=True).start()
    threading.Thread(target=_pipe, args=(proc.stderr, sys.stderr), daemon=True).start()

    if not _wait_for_port_or_crash(proc, "127.0.0.1", COMFY_PORT, timeout=300):
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("ComfyUI server did not open port")

    # Small settle time for initial model index
    time.sleep(2.0)

    # Submit prompt
    url = f"http://127.0.0.1:{COMFY_PORT}/prompt"
    payload = {"prompt": workflow_json}
    log(f"POST {url}")
    r = requests.post(url, json=payload, timeout=60)
    if r.status_code != 200:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        raise RuntimeError(f"submit failed: {r.status_code} {r.text[:500]}")

    # Poll OUT_DIR for images
    t0 = time.time()
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    images = []
    while time.time() - t0 < 300:
        images = [p for p in OUT_DIR.glob("**/*") if p.suffix.lower() in exts]
        if images:
            break
        time.sleep(0.5)

    # Shutdown server
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    # Collect up to 8 newest images
    images = sorted(images, key=lambda p: p.stat().st_mtime, reverse=True)
    payload_imgs = []
    for p in images[:8]:
        try:
            payload_imgs.append(
                {"filename": p.name, "b64": base64.b64encode(p.read_bytes()).decode("utf-8")}
            )
        except Exception as e:
            log(f"warn: failed to encode {p}: {e}")

    log(f"collected {len(payload_imgs)} image(s) from {OUT_DIR}")
    if not payload_imgs:
        raise RuntimeError("no images produced; check server stderr above")

    return {"images": payload_imgs}


# ---------------------------
# RunPod handler
# ---------------------------
def handler(job):
    """RunPod serverless handler."""
    t0 = time.time()
    inp = job.get("input", {})
    log(f"handler start, keys={list(inp.keys())}")

    # Ensure models are present (fast no-op if cached)
    sync_models_from_s3()

    # Inventory & summary
    inv = list_local_models()
    log("model inventory: " + summarize_models(inv))

    # Hard fail if no checkpoints visible at all
    if not inv.get("checkpoints"):
        raise RuntimeError(
            "No checkpoints found under /runpod-volume/models/checkpoints. "
            "Verify symlink (ComfyUI/models → /runpod-volume/models) and extra_model_paths.yaml."
        )

    # Workflow parse
    workflow_json = inp.get("workflow")
    if workflow_json is None:
        raise ValueError("no workflow provided (expected input.workflow)")
    if isinstance(workflow_json, str):
        try:
            workflow_json = json.loads(workflow_json)
        except Exception as e:
            raise ValueError(f"invalid workflow JSON string: {e}")

    # Optional: force a checkpoint for all loader nodes
    if FORCED_CKPT:
        for node_id, inputs in extract_ckpt_refs(workflow_json):
            inputs["ckpt_name"] = FORCED_CKPT
        log(f"forcing ckpt_name to {FORCED_CKPT} for all loader nodes")
    else:
        # Reconcile any unknown ckpt_name to first available
        workflow_json, ckpt_notes = reconcile_ckpt_names(workflow_json, inv.get("checkpoints", []))
        for n in ckpt_notes:
            log(f"ckpt reconcile: {n}")

    result = run_comfy_workflow(workflow_json)
    result["execution_time"] = round(time.time() - t0, 2)
    return result


# ---------------------------
# Main
# ---------------------------
if __name__ == "__main__":
    log(f"boot: image_version={os.getenv('IMAGE_VERSION','unknown')}")
    log(f"boot: model_dir={MODELS_DIR}")
    log(f"boot: out_dir={OUT_DIR}")
    log(f"boot: bucket={S3_BUCKET} prefix={S3_PREFIX} region={AWS_REGION}")
    log(f"boot: env AWS_ACCESS_KEY_ID present? {bool(os.getenv('AWS_ACCESS_KEY_ID'))}")
    log(f"boot: env AWS_SECRET_ACCESS_KEY present? {bool(os.getenv('AWS_SECRET_ACCESS_KEY'))}")
    # Start RunPod loop
    runpod.serverless.start({"handler": handler})
