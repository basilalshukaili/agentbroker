"""Bounded stdio bridge to a separately installed official SDK client."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

CLIENT = Path(__file__).with_name("oauth_sdk_client.py")


def sdk_python():
    return os.environ.get("MCP_SDK_PYTHON", sys.executable)


def client_env():
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_sdk(base, scenario, person=None, *, timeout=60, **kwargs):
    parsed = urlsplit(base)
    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port, "SDK tests require loopback HTTP"
    # The client needs no provider credentials or application environment.
    events = queue.Queue()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        process = subprocess.Popen([sdk_python(), str(CLIENT)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8", env=client_env())
        def read_events():
            for line in process.stdout:
                events.put(line)
            events.put(None)

        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        result = None
        try:
            process.stdin.write(json.dumps({"base": base, "scenario": scenario,
                                           "redirect_uri": "http://localhost:8765/callback", **kwargs}) + "\n")
            process.stdin.flush()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("official SDK client did not complete before its deadline")
                try:
                    line = events.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError("official SDK client did not complete before its deadline") from None
                if line is None:
                    break
                event = json.loads(line)
                if event["event"] == "redirect":
                    assert person is not None, "unexpected SDK sign-in"
                    async def browser_callback():
                        await person.redirect_handler(event["url"])
                        return await person.callback_handler()

                    async def bounded_callback():
                        return await asyncio.wait_for(browser_callback(), timeout=max(0, deadline - time.monotonic()))

                    code, state = asyncio.run(bounded_callback())
                    process.stdin.write(json.dumps({"code": code, "state": state}) + "\n")
                    process.stdin.flush()
                elif event["event"] == "result":
                    result = event["result"]
                else:
                    raise AssertionError(f"unknown SDK bridge event: {event['event']}")
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            errors.seek(0)
            assert process.returncode == 0, errors.read()
            assert result is not None, "SDK client exited without exercising the scenario"
            return result
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdin.close()
            reader.join(timeout=10)
            process.stdout.close()
