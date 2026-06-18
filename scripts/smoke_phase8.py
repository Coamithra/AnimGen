"""Phase 8 smoke test - OPT-IN offline integration (no spend, no GPU, NO real ComfyUI).

This phase is deliberately NOT part of the always-run gate (`for n in 1 2 3 4 5 6 7`):
unlike the pure-function phases (rule #4), it spins up a real socket server + a real
`JobManager`/`QThreadPool` + the best-effort progress WebSocket and drives a take through
them, so it's heavier and timing-sensitive. Run it explicitly when touching the local
queue / comfy backend / mock_comfy:

    QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 \
        .venv/Scripts/python.exe scripts/smoke_phase8.py

What it covers (card #76): the supported offline harness `scripts/mock_comfy.py` driven
in-process, so one real take goes through the full `comfy_client.submit -> /history ->
claim` path against the fake ComfyUI and lands DONE (output file claimed, written through
to takes.json), and with the mock's failure injection lands FAILED + interrupted=False -
the server stays UP, so it's a genuine workflow error, not a crash (mirrors the GUI
`--fail-rate` path; rule #12's "up == not a crash" discrimination at the take-record level).

It needs zero production change: the mock's `Handler` runs on an ephemeral port and
`comfy_client.COMFY_URL` is monkeypatched at it (the same redirect pattern phase 2 uses),
while `ANIMGEN_COMFY_DIR` (set before imports) aligns the mock's output dir with
`comfy_client.COMFY_OUTPUT_DIR` so the claim copy finds the canned clip.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Point BOTH the mock (scripts/mock_comfy.py) and comfy_client at the same throwaway COMFY
# dir BEFORE either is imported - each reads ANIMGEN_COMFY_DIR at import time, and they must
# agree on <comfy>/output/ so the claim copy finds the mock's canned clip.
os.environ["ANIMGEN_COMFY_DIR"] = tempfile.mkdtemp(prefix="animgen_mock_comfy_")

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))                       # repo root: paths/backends/store
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/: mock_comfy (not a package)

import mock_comfy  # noqa: E402
import paths  # noqa: E402
from backends import comfy_client  # noqa: E402
from backends.jobs import JobManager  # noqa: E402
from store.models import STATUS_DONE, STATUS_FAILED, STATUS_PENDING  # noqa: E402
from store.project import Project  # noqa: E402

paths.SCRATCH_DIR = Path(tempfile.mkdtemp())  # keep untitled-project scratch out of data/


def test_mock_comfy_take_end_to_end() -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    # In-process fake ComfyUI on an OS-assigned port; render fast so the phase is a few seconds.
    srv = mock_comfy.make_server(0, render_s=1.0, jitter_s=0.0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    saved_url, saved_port = comfy_client.COMFY_URL, comfy_client.COMFY_PORT
    comfy_client.COMFY_URL = f"http://127.0.0.1:{port}"
    comfy_client.COMFY_PORT = port
    out_dir = Path(tempfile.mkdtemp(prefix="animgen_takes_"))
    try:
        project = Project.new()
        shot = project.add_shot("kick", model_id="local-flf-wan14b")
        jm = JobManager(project)
        done: list[str] = []
        failed: list[tuple[str, str]] = []
        jm.finished.connect(done.append)
        jm.failed.connect(lambda tid, err: failed.append((tid, err)))

        def make_comfy_runner(take_id: str):
            """A runner that drives the REAL submit->/history->claim path against the mock.

            A trivial `{}` workflow is enough - the mock ignores the node graph and serves the
            canned clip. on_submit records the prompt id exactly like ui.main_window._make_runner.
            """
            out_path = out_dir / f"{take_id}.mp4"

            def on_submit(pid: str) -> None:
                project.update_take(take_id, backend_job_id=pid)

            def runner(progress):
                return comfy_client.submit({}, out_path, progress, poll_s=1, timeout_s=60,
                                           on_submit=on_submit)
            return runner, out_path

        # --- success take: DONE, output claimed, write-through to takes.json, interrupted False
        ok = project.add_take(shot.id, status=STATUS_PENDING,
                              settings_snapshot={"backend": "comfyui"})
        ok_runner, ok_out = make_comfy_runner(ok.id)
        jm.enqueue(ok.id, "comfyui", ok_runner)
        assert jm.wait_for_done(60000), "success take did not finish"
        app.processEvents()

        got_ok = project.get_take(ok.id)
        assert got_ok.status == STATUS_DONE, got_ok.status
        assert got_ok.video_path == str(ok_out), got_ok.video_path
        assert ok_out.exists() and ok_out.stat().st_size > 0, "claim must copy the canned clip"
        assert got_ok.backend_job_id, "on_submit must record the prompt id mid-render"
        assert got_ok.interrupted is False
        assert ok.id in done, "finished signal must fire for the DONE take"

        # write-through landed on disk: takes.json carries the take as done
        doc = json.loads((project.assets_dir / "takes.json").read_text(encoding="utf-8"))
        persisted = {t["id"]: t for t in doc.get("takes", [])}
        assert persisted.get(ok.id, {}).get("status") == STATUS_DONE, "takes.json must persist DONE"

        # --- failure take: FAILED + interrupted=False. The mock returns a status_str=="error"
        # /history entry with the server still UP, so it's a genuine workflow error (not a crash).
        mock_comfy.FAIL_RATE = 1.0
        bad = project.add_take(shot.id, status=STATUS_PENDING,
                               settings_snapshot={"backend": "comfyui"})
        bad_runner, _ = make_comfy_runner(bad.id)
        jm.enqueue(bad.id, "comfyui", bad_runner)
        assert jm.wait_for_done(60000), "failure take did not finish"
        app.processEvents()

        got_bad = project.get_take(bad.id)
        assert got_bad.status == STATUS_FAILED, got_bad.status
        assert "workflow error" in (got_bad.error or ""), got_bad.error
        assert got_bad.interrupted is False, "server-up failure is a genuine error, not a crash"
        assert any(tid == bad.id for tid, _ in failed), "failed signal must fire for the FAILED take"
    finally:
        comfy_client.COMFY_URL, comfy_client.COMFY_PORT = saved_url, saved_port
        srv.shutdown()
        srv.server_close()
    print("mock_comfy end-to-end OK: real submit->/history->claim drives a take to DONE "
          "(output claimed + takes.json write-through) and a --fail-rate take to "
          "FAILED+interrupted=False")


if __name__ == "__main__":
    test_mock_comfy_take_end_to_end()
    print("PHASE 8 SMOKE: PASS")
