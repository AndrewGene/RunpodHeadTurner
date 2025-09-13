import socket, requests, threading

COMFY_PORT = int(os.getenv("COMFY_PORT", "8188"))

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
    # Run ComfyUI headless server (no UI) in the venv
    env = os.environ.copy()
    env["COMFYUI_MODEL_DIR"] = str(MODELS_DIR)
    cmd = [
        PY, str(COMFY_REPO / "main.py"),
        "--disable-auto-launch",
        "--listen", "127.0.0.1",
        "--port", str(COMFY_PORT),
        "--output-directory", str(OUT_DIR),
    ]
    log(f"starting ComfyUI server: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    return proc

def run_comfy_workflow(workflow_json: dict) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clean previous files
    for p in OUT_DIR.glob("*"):
        try:
            if p.is_file():
                p.unlink()
        except Exception as e:
            log(f"warn: could not remove {p}: {e}")

    # Boot server
    proc = _start_server()
    # Background log tail (non-blocking, truncated)
    def _pipe(name, stream):
        for i, line in enumerate(iter(stream.readline, '')):
            if i < 200:  # keep logs light
                sys.stdout.write(line if name=='stdout' else "")
                sys.stderr.write(line if name=='stderr' else "")
    threading.Thread(target=_pipe, args=("stdout", proc.stdout), daemon=True).start()
    threading.Thread(target=_pipe, args=("stderr", proc.stderr), daemon=True).start()

    if not _wait_for_port("127.0.0.1", COMFY_PORT, timeout=60):
        proc.kill()
        raise RuntimeError("ComfyUI server did not open port in time")

    # Submit workflow to API
    url = f"http://127.0.0.1:{COMFY_PORT}/prompt"
    payload = {"prompt": workflow_json}
    log(f"POST {url}")
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code != 200:
        proc.kill()
        raise RuntimeError(f"submit failed: {r.status_code} {r.text[:2000]}")

    # Poll for outputs: simplest is to watch the OUT_DIR for new files
    # (Alternatively use /history or /queue endpoints; dir watch is robust.)
    t0 = time.time()
    images_prev = set()
    while time.time() - t0 < 300:
        images = {p for p in OUT_DIR.glob("**/*") if p.suffix.lower() in {".png",".jpg",".jpeg",".webp"}}
        new = images - images_prev
        if new:
            break
        images_prev = images
        time.sleep(0.5)

    # Shutdown server
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    # Collect images
    images = sorted(
        (p for p in OUT_DIR.glob("**/*") if p.suffix.lower() in {".png",".jpg",".jpeg",".webp"}),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    payload = []
    for p in images[:8]:
        with p.open("rb") as f:
            payload.append({"filename": p.name, "b64": base64.b64encode(f.read()).decode("utf-8")})
    log(f"collected {len(payload)} image(s) from {OUT_DIR}")
    if not payload:
        raise RuntimeError("no images produced; check server stderr above")
    return {"images": payload}

