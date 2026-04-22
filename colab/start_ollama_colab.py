from __future__ import annotations

#%%
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path


# Interactive Window / Colab settings
MODEL = os.environ.get("COLAB_OLLAMA_MODEL", "qwen3:14b")
PORT = int(os.environ.get("COLAB_OLLAMA_PORT", "11434"))
HOST = os.environ.get("COLAB_OLLAMA_HOST", "0.0.0.0")
SKIP_TUNNEL = os.environ.get("COLAB_OLLAMA_SKIP_TUNNEL", "").strip().lower() in {"1", "true", "yes", "on"}
SERVER_TIMEOUT_SECONDS = int(os.environ.get("COLAB_OLLAMA_SERVER_TIMEOUT", "120"))
TUNNEL_TIMEOUT_SECONDS = int(os.environ.get("COLAB_OLLAMA_TUNNEL_TIMEOUT", "90"))
LOG_DIR = Path(os.environ.get("COLAB_OLLAMA_LOG_DIR", "/content/ollama_runtime_logs"))
BIN_DIR = Path(os.environ.get("COLAB_OLLAMA_BIN_DIR", "/content/bin"))

CLOUDFLARED_BINARY_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
TRYCLOUDFLARE_PATTERN = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com")
OLLAMA_TAR_ZST_URL = "https://ollama.com/download/ollama-linux-amd64.tar.zst"
OLLAMA_ROOT_DIR = Path(os.environ.get("COLAB_OLLAMA_ROOT_DIR", "/content/ollama"))


#%%
def run_shell(command: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=True,
        check=True,
        text=True,
        executable="/bin/bash",
        env=env,
    )


def _extract_with_tar_zstd(archive_path: Path, destination: Path) -> bool:
    result = subprocess.run(
        ["tar", "--zstd", "-xf", str(archive_path), "-C", str(destination)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _extract_with_zstd_command(archive_path: Path, destination: Path) -> bool:
    if shutil.which("zstd") is None:
        return False

    decoder = subprocess.Popen(  # noqa: S603
        ["zstd", "-d", "-c", str(archive_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    extractor = subprocess.run(
        ["tar", "-xf", "-", "-C", str(destination)],
        stdin=decoder.stdout,
        capture_output=True,
        text=True,
    )
    if decoder.stdout is not None:
        decoder.stdout.close()
    decoder_returncode = decoder.wait()
    return decoder_returncode == 0 and extractor.returncode == 0


def _extract_with_python_zstandard(archive_path: Path, destination: Path) -> bool:
    try:
        import zstandard  # type: ignore
    except ImportError:
        return False

    tar_path = destination / "ollama-linux-amd64.tar"
    with archive_path.open("rb") as compressed:
        dctx = zstandard.ZstdDecompressor()
        with tar_path.open("wb") as expanded:
            dctx.copy_stream(compressed, expanded)
    with tarfile.open(tar_path, "r:") as archive:
        archive.extractall(destination)
    tar_path.unlink(missing_ok=True)
    return True


def extract_tar_zst(archive_path: Path, destination: Path) -> None:
    if _extract_with_tar_zstd(archive_path, destination):
        return
    if _extract_with_zstd_command(archive_path, destination):
        return
    if _extract_with_python_zstandard(archive_path, destination):
        return
    raise RuntimeError(
        "Could not extract the Ollama .tar.zst archive. "
        "Retry on Colab, or install zstd in the runtime with `!apt-get update && !apt-get install -y zstd`."
    )


def ensure_ollama_installed() -> None:
    if shutil.which("ollama"):
        return
    if platform.system() != "Linux":
        raise RuntimeError(
            "This notebook is for Colab/Linux. You are not on Linux, so Colab Computing Unit cannot be used here. "
            "Open this file in a Colab notebook or connect VS Code to a Linux/Colab kernel."
        )

    bin_path = OLLAMA_ROOT_DIR / "bin" / "ollama"
    if bin_path.exists():
        os.environ["PATH"] = f"{bin_path.parent}:{os.environ.get('PATH', '')}"
        return

    OLLAMA_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = OLLAMA_ROOT_DIR / "ollama-linux-amd64.tar.zst"
    urllib.request.urlretrieve(OLLAMA_TAR_ZST_URL, archive_path)
    extract_tar_zst(archive_path, OLLAMA_ROOT_DIR)
    os.environ["PATH"] = f"{(OLLAMA_ROOT_DIR / 'bin')}:{os.environ.get('PATH', '')}"
    if not shutil.which("ollama"):
        raise RuntimeError("Ollama download completed but the binary was not found under /content/ollama/bin.")


def ensure_cloudflared_installed(bin_dir: Path) -> Path:
    existing = shutil.which("cloudflared")
    if existing:
        return Path(existing)

    bin_dir.mkdir(parents=True, exist_ok=True)
    destination = bin_dir / "cloudflared"
    urllib.request.urlretrieve(CLOUDFLARED_BINARY_URL, destination)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
    return destination


def start_background(command: list[str], log_path: Path, env: dict[str, str] | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    process = subprocess.Popen(  # noqa: S603
        command,
        stdout=handle,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    return process.pid


def wait_for_url(url: str, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for {url}: {last_error}")


def wait_for_trycloudflare_url(log_path: Path, timeout_seconds: int) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8", errors="ignore")
            match = TRYCLOUDFLARE_PATTERN.search(content)
            if match:
                return match.group(0)
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for trycloudflare URL in {log_path}")


def detect_gpu() -> list[str]:
    try:
        result = subprocess.run(  # noqa: S603
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:  # noqa: BLE001
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def pull_model(model: str, local_base_url: str) -> None:
    env = os.environ.copy()
    env["OLLAMA_HOST"] = local_base_url
    run_shell(f"ollama pull {shlex.quote(model)}", env=env)


def start_ollama_colab(
    model: str = MODEL,
    port: int = PORT,
    host: str = HOST,
    skip_tunnel: bool = SKIP_TUNNEL,
    server_timeout_seconds: int = SERVER_TIMEOUT_SECONDS,
    tunnel_timeout_seconds: int = TUNNEL_TIMEOUT_SECONDS,
    log_dir: Path = LOG_DIR,
    bin_dir: Path = BIN_DIR,
) -> dict[str, object]:
    ollama_log = log_dir / "ollama.log"
    tunnel_log = log_dir / "cloudflared.log"

    ensure_ollama_installed()
    if not skip_tunnel:
        ensure_cloudflared_installed(bin_dir)

    local_base_url = f"http://127.0.0.1:{port}"
    bind_address = f"{host}:{port}"
    server_env = os.environ.copy()
    server_env["OLLAMA_HOST"] = bind_address

    ollama_pid = start_background(["ollama", "serve"], ollama_log, env=server_env)
    wait_for_url(f"{local_base_url}/api/tags", server_timeout_seconds)
    pull_model(model, local_base_url)

    public_url = None
    tunnel_pid = None
    if not skip_tunnel:
        tunnel_pid = start_background(
            ["cloudflared", "tunnel", "--url", local_base_url, "--no-autoupdate"],
            tunnel_log,
        )
        public_url = wait_for_trycloudflare_url(tunnel_log, tunnel_timeout_seconds)

    return {
        "model": model,
        "gpu": detect_gpu(),
        "local_base_url": local_base_url,
        "public_base_url": public_url,
        "ollama_pid": ollama_pid,
        "cloudflared_pid": tunnel_pid,
        "ollama_log": str(ollama_log),
        "cloudflared_log": str(tunnel_log) if tunnel_pid else None,
        "next_env": {
            "OLLAMA_BASE_URL": public_url or local_base_url,
            "OLLAMA_MODEL": model,
        },
    }


#%%
result = start_ollama_colab()
print(json.dumps(result, ensure_ascii=False, indent=2))


#%%
from google.colab import runtime
runtime.unassign()

# %%
