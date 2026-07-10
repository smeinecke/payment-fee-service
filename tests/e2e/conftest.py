from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

_PORT = 18000
_FIXTURES = Path(__file__).parent.parent / "fixtures"

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def server_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    config_dir = tmp_path_factory.mktemp("config")
    config_path = config_dir / "config.json"
    config = {
        "refresh_interval_seconds": 0,
        "admin_token": "test-token",
        "validate_json_schema": False,
        "providers": {
            "paypal": {
                "data_path": str(_FIXTURES / "paypal"),
                "data_ref": "fixture-paypal",
            },
            "stripe": {
                "data_path": str(_FIXTURES / "stripe"),
                "data_ref": "fixture-stripe",
            },
        },
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    env = os.environ.copy()
    env["PAYMENT_FEE_CONFIG_FILE"] = str(config_path)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "payment_fee_service.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(_PORT),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{_PORT}"
    deadline = time.time() + 30
    try:
        with httpx.Client(base_url=base_url, timeout=1.0) as client:
            while time.time() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError("Server process exited before readiness")
                try:
                    response = client.get("/health/ready")
                    if response.status_code == 200:
                        break
                except httpx.ConnectError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Server did not become ready")
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        raise

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def client(server_url: str) -> httpx.Client:
    with httpx.Client(base_url=server_url, timeout=10.0) as client:
        yield client
