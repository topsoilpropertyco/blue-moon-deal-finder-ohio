"""Nightly automatic update: the LaunchAgent, the status reader, the CLI and scripts/auto_update.sh itself.

The script runs against a made-up git repo with a fake GitHub (a bare repo), and stub `uv` and `launchctl`
programs on PATH that only write down how they were called. Nothing here touches the real app folder.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from bluemoon import autoupdate

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "auto_update.sh"
GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(not GIT or not shutil.which("bash"), reason="needs git and bash")


# --- the LaunchAgent ---------------------------------------------------------------------------

def test_plist_runs_the_script_nightly_at_3_17_with_uv_and_git_on_path():
    path_env = autoupdate.tool_path({"uv": "/Users/seth/.local/bin/uv", "git": "/usr/bin/git"})
    doc = plistlib.loads(autoupdate.make_plist(Path("/Users/seth/blue-moon"), path_env, 3, 17, keep_lines=400))
    assert doc["Label"] == "com.bluemoon.autoupdate"
    assert doc["ProgramArguments"] == ["/bin/bash", "/Users/seth/blue-moon/scripts/auto_update.sh"]
    assert doc["WorkingDirectory"] == "/Users/seth/blue-moon"
    assert doc["StartCalendarInterval"] == {"Hour": 3, "Minute": 17}
    assert doc["RunAtLoad"] is False
    dirs = doc["EnvironmentVariables"]["PATH"].split(":")
    assert dirs[:2] == ["/Users/seth/.local/bin", "/usr/bin"]
    assert "/opt/homebrew/bin" in dirs and len(dirs) == len(set(dirs))     # no folder twice
    assert doc["EnvironmentVariables"]["BLUEMOON_UPDATE_KEEP_LINES"] == "400"
    assert doc["EnvironmentVariables"]["BLUEMOON_UPDATE_TIMEOUT_MINUTES"] == "20"
    assert doc["StandardErrorPath"] == "/Users/seth/blue-moon/logs/auto_update.launchd.log"
    assert "KEY" not in json.dumps(doc)    # no keys in the job


def test_tool_path_without_uv_still_has_the_usual_folders():
    dirs = autoupdate.tool_path({"uv": None, "git": "/opt/homebrew/bin/git"}).split(":")
    assert dirs[0] == "/opt/homebrew/bin" and "/usr/bin" in dirs and str(Path.home() / ".local/bin") in dirs


def test_clock():
    assert autoupdate.clock(3, 17) == "3:17 AM"
    assert autoupdate.clock(0, 5) == "12:05 AM"
    assert autoupdate.clock(15, 0) == "3:00 PM"


def test_schedule_comes_from_config_yaml(settings):
    cfg = autoupdate.update_settings(settings)
    assert (cfg.hour, cfg.minute, cfg.keep_log_lines, cfg.check_timeout_minutes) == (3, 17, 500, 20)


@pytest.fixture
def fake_launchctl(tmp_path, monkeypatch) -> Path:
    """launchctl that writes down its arguments; HOME in a temp folder so the plist lands there."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    calls = tmp_path / "launchctl_calls"
    stub = bin_dir / "launchctl"
    stub.write_text(f'#!/bin/bash\necho "$*" >> "{calls}"\nexit 0\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return calls


def test_install_writes_and_loads_the_plist_then_uninstall_removes_it(settings, fake_launchctl, tmp_path):
    app = tmp_path / "app"
    path = autoupdate.install(settings, app_dir=app)
    assert path == tmp_path / "home" / "Library" / "LaunchAgents" / "com.bluemoon.autoupdate.plist"
    doc = plistlib.loads(path.read_bytes())
    assert doc["StartCalendarInterval"] == {"Hour": 3, "Minute": 17}
    assert str(Path(shutil.which("uv")).parent) in doc["EnvironmentVariables"]["PATH"].split(":")
    assert (app / "logs").is_dir()
    uid = os.getuid()
    lines = fake_launchctl.read_text().splitlines()
    assert lines == [f"bootout gui/{uid}/com.bluemoon.autoupdate", f"bootstrap gui/{uid} {path}"]
    assert autoupdate.status(settings)["installed"] is True

    assert autoupdate.uninstall() is True
    assert not path.exists()
    assert fake_launchctl.read_text().splitlines()[-1] == f"bootout gui/{uid}/com.bluemoon.autoupdate"
    assert autoupdate.uninstall() is False
    assert autoupdate.status(settings)["installed"] is False


def test_install_without_launchctl_says_it_is_for_the_mac(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(autoupdate.shutil, "which", lambda name: None if name == "launchctl" else "/usr/bin/" + name)
    with pytest.raises(autoupdate.UpdateError, match="Mac Mini"):
        autoupdate.install(settings, app_dir=tmp_path / "app")


# --- the status reader -------------------------------------------------------------------------

def _state(root: Path, **values) -> None:
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "auto_update_state.json").write_text(json.dumps(values), encoding="utf-8")


def test_status_reads_the_last_result(settings, tmp_root, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _state(tmp_root, when="2026-10-29T03:17:04-0400", result="updated", message="Updated to abc1234, 2026-10-28.",
           commit="abc1234", commit_date="2026-10-28")
    st = autoupdate.status(settings)
    assert st["installed"] is False and st["time"] == "3:17 AM"
    assert st["last"] == {"when": "2026-10-29T03:17:04-04:00", "result": "updated", "result_text": "ok",
                          "message": "Updated to abc1234, 2026-10-28.", "ok": True}
    assert st["version"]["commit"]            # this checkout's own commit
    line = autoupdate.status_line(st)
    assert line.startswith(f"Version: {st['version_text']} · Nightly updates are off · ")
    assert line.endswith("Last update: 2026-10-29 03:17, ok")

    _state(tmp_root, when="2026-10-30T03:17:00-0400", result="failed", message="The quick tests failed.")
    st = autoupdate.status(settings)
    assert st["last"]["ok"] is False and autoupdate.status_line(st).endswith("Last update: 2026-10-30 03:17, failed")


def test_status_when_it_never_ran_or_the_file_is_broken(settings, tmp_root):
    assert autoupdate.status(settings)["last"] is None
    assert autoupdate.status_line(autoupdate.status(settings)).endswith("Last update: never")
    (tmp_root / "logs").mkdir()
    (tmp_root / "logs" / "auto_update_state.json").write_text("{not json", encoding="utf-8")
    assert autoupdate.status(settings)["last"] is None


def test_version_unknown_outside_git(tmp_path):
    assert autoupdate.current_version(tmp_path) == {"commit": "", "date": ""}


def test_cli_status_and_settings_screen(settings, tmp_root, tmp_path, monkeypatch, capsys):
    import run
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _state(tmp_root, when="2026-10-29T03:17:04-0400", result="up_to_date", message="Up to date (abc1234, 2026-10-28).")
    assert run.main(["autoupdate", "status"]) == 0
    out = capsys.readouterr().out
    assert "Nightly updates are off" in out and "Last update: 2026-10-29 03:17, ok, already up to date" in out
    assert "uv run run.py autoupdate install" in out

    from fastapi.testclient import TestClient
    from bluemoon.web.app import app
    with TestClient(app) as c:
        up = c.get("/api/settings").json()["updates"]
    assert up["time"] == "3:17 AM" and up["installed"] is False and up["last"]["result"] == "up_to_date"
    assert up["line"].startswith("Version: ") and "log" not in up


def test_settings_screen_has_the_updates_line():
    js = (REPO / "bluemoon" / "web" / "static" / "settings.js").read_text(encoding="utf-8")
    assert "updatesPanel(st.updates)" in js
    for text in ("Version: ", "Updates itself nightly at ", "Last update: ", "autoupdate install", "autoupdate now"):
        assert text in js, text


# --- scripts/auto_update.sh against a made-up repo ---------------------------------------------

GIT_ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
           "GIT_COMMITTER_EMAIL": "test@example.com", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


JOBS_CALL = "uv run --frozen --quiet run.py autoupdate jobs"


class Box:
    """A fake GitHub (bare repo), the app folder cloned from it, a second clone to push updates from, and stubs."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.remote = tmp / "github.git"
        self.app = tmp / "app"
        self.dev = tmp / "dev"
        self.bin = tmp / "bin"
        self.calls = tmp / "calls"
        self.bin.mkdir()
        self.env = {**GIT_ENV, "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(tmp),
                    "BLUEMOON_APP_DIR": str(self.app), "BLUEMOON_HOME": str(self.app),
                    "BLUEMOON_SMOKE_TESTS": "tests/test_quick.py"}
        # uv: notes its arguments; "autoupdate jobs" answers $FAKE_JOBS (0); any other "uv run" fails while the
        # repo has a BREAK file (a bad update) and hangs while it has a HANG file (a step that never ends).
        self._stub("uv", '[ "$*" = "run --frozen --quiet run.py autoupdate jobs" ] && { echo "${FAKE_JOBS:-0}"; exit 0; }\n'
                         '[ "$1" = run ] && [ -f HANG ] && sleep 30\n'
                         '[ "$1" = run ] && [ -f BREAK ] && { echo "Traceback: the new version is broken"; exit 1; }')
        self._stub("launchctl", "")
        self.git("init", "--bare", "-b", "main", str(self.remote), cwd=tmp)
        self.git("clone", str(self.remote), str(self.dev), cwd=tmp)
        (self.dev / ".gitignore").write_text("config.local.yaml\n.env\nlogs/\n")
        (self.dev / "app.txt").write_text("version 1\n")
        self.git("add", ".", cwd=self.dev)
        self.git("commit", "-m", "first", cwd=self.dev)
        self.git("push", "origin", "main", cwd=self.dev)
        self.git("clone", str(self.remote), str(self.app), cwd=tmp)

    def _stub(self, name: str, body: str) -> None:
        p = self.bin / name
        p.write_text(f'#!/bin/bash\necho "{name} $*" >> "{self.calls}"\n{body}\nexit 0\n')
        p.chmod(0o755)

    def git(self, *args: str, cwd: Path) -> str:
        return subprocess.run([GIT, *args], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True,
                              text=True, check=True).stdout.strip()

    def push(self, name: str, text: str) -> str:
        (self.dev / name).write_text(text)
        self.git("add", ".", cwd=self.dev)
        self.git("commit", "-m", f"change {name}", cwd=self.dev)
        self.git("push", "origin", "main", cwd=self.dev)
        return self.git("rev-parse", "HEAD", cwd=self.dev)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD", cwd=self.app)

    def run(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/bash", str(SCRIPT)], env={**self.env, **env}, capture_output=True, text=True,
                              timeout=120)

    def state(self) -> dict:
        return json.loads((self.app / "logs" / "auto_update_state.json").read_text())

    def called(self) -> list[str]:
        return [c for c in (self.calls.read_text().splitlines() if self.calls.exists() else [])
                if c != JOBS_CALL]

    def asked_for_jobs(self) -> int:
        return self.calls.read_text().splitlines().count(JOBS_CALL) if self.calls.exists() else 0

    def log(self) -> str:
        return (self.app / "logs" / "auto_update.log").read_text()


@pytest.fixture
def box(tmp_path) -> Box:
    return Box(tmp_path)


def test_nothing_new_logs_up_to_date(box):
    before = box.head()
    res = box.run()
    assert res.returncode == 0, res.stderr
    st = box.state()
    assert st["result"] == "up_to_date" and st["commit"] == before[:len(st["commit"])]
    assert st["message"].startswith("Up to date (") and st["commit_date"]
    assert box.head() == before and box.called() == []     # no install, no restart
    assert "Up to date" in box.log()


def test_new_commit_is_pulled_checked_and_the_services_restarted(box):
    new = box.push("app.txt", "version 2\n")
    (box.app / "config.local.yaml").write_text("photos:\n  batch: 7\n")     # Seth's own settings never block it
    (box.app / "notes.txt").write_text("an untracked file\n")
    res = box.run()
    assert res.returncode == 0, res.stdout + res.stderr
    assert box.head() == new and (box.app / "app.txt").read_text() == "version 2\n"
    assert (box.app / "config.local.yaml").read_text() == "photos:\n  batch: 7\n"
    uid = os.getuid()
    calls = box.called()
    assert calls[0] == "uv sync --locked --quiet"      # exactly what uv.lock says; never rewrites it
    assert "uv run --frozen python -c import bluemoon.web.app" in calls and "uv run --frozen run.py --help" in calls
    assert any(c.startswith("uv run --frozen pytest") and c.endswith("tests/test_quick.py") for c in calls)
    # Only the dashboard restarts: the team link keeps its address because its code didn't change.
    assert calls[-1] == f"launchctl kickstart -k gui/{uid}/com.bluemoon.dealfinder"
    assert not any("com.bluemoon.share" in c for c in calls)
    st = box.state()
    assert st["result"] == "updated" and new.startswith(st["commit"]) and st["message"] == f"Updated to {st['commit']}, {st['commit_date']}."
    assert "Restarted com.bluemoon.dealfinder." in box.log()
    assert not (box.app / "logs" / "auto_update_pending").exists()


def test_failing_check_rolls_back_and_leaves_the_dashboard_alone(box):
    before = box.head()
    box.push("BREAK", "this update is broken\n")
    res = box.run()
    assert res.returncode == 1
    assert box.head() == before and not (box.app / "BREAK").exists()
    calls = box.called()
    assert calls.count("uv sync --locked --quiet") == 1 and calls.count("uv sync --frozen --quiet") == 1
    assert not any(c.startswith("launchctl") for c in calls)
    st = box.state()
    assert st["result"] == "failed" and before.startswith(st["commit"])
    assert "went back to" in st["message"] and "doesn't load" in st["message"] and "Traceback" in st["message"]
    assert "Update to" in box.log() and not (box.app / "logs" / "auto_update_pending").exists()
    # Next night, with the broken update still on GitHub, it tries again and goes back again.
    assert box.run().returncode == 1 and box.head() == before


def test_changed_tracked_file_is_skipped_and_kept(box):
    before = box.head()
    box.push("app.txt", "version 2\n")
    (box.app / "app.txt").write_text("Seth's own edit\n")
    res = box.run()
    assert res.returncode == 0
    assert box.head() == before and (box.app / "app.txt").read_text() == "Seth's own edit\n"
    st = box.state()
    assert st["result"] == "skipped" and "app.txt" in st["message"] and "never overwrites" in st["message"]
    assert box.called() == []


def test_not_on_main_is_skipped(box):
    box.git("checkout", "-b", "trying-something", cwd=box.app)
    box.push("app.txt", "version 2\n")
    res = box.run()
    assert res.returncode == 0
    assert box.state()["result"] == "skipped" and "trying-something" in box.state()["message"]
    assert (box.app / "app.txt").read_text() == "version 1\n" and box.called() == []


def test_no_internet_is_a_failure_that_changes_nothing(box):
    before = box.head()
    shutil.rmtree(box.remote)
    res = box.run()
    assert res.returncode == 1
    assert box.state()["result"] == "failed" and "Couldn't reach GitHub" in box.state()["message"]
    assert box.head() == before


def test_a_restart_that_fails_is_logged_but_the_update_counts(box):
    (box.bin / "launchctl").write_text('#!/bin/bash\necho "Could not find service" >&2\nexit 113\n')
    box.push("app.txt", "version 2\n")
    assert box.run().returncode == 0
    assert box.state()["result"] == "updated"
    assert "Didn't restart com.bluemoon.dealfinder" in box.log()


def test_log_is_trimmed_and_only_one_run_at_a_time(box):
    for _ in range(4):
        box.run(BLUEMOON_UPDATE_KEEP_LINES="3")
    assert len(box.log().splitlines()) == 3
    (box.app / "logs" / "auto_update.lock").mkdir()
    res = box.run()
    assert res.returncode == 0 and "Another update is running" in res.stdout
    assert (box.app / "logs" / "auto_update.lock").is_dir()      # someone else's lock is left alone


def test_an_update_stopped_halfway_goes_back_next_time(box):
    """Killed during its check (power cut, hang): the next run goes back instead of calling it 'up to date'."""
    before = box.head()
    box.push("BREAK", "this update never passed its check\n")
    (box.dev / "HANG").write_text("")
    box.git("add", ".", cwd=box.dev)
    box.git("commit", "-m", "hang", cwd=box.dev)
    box.git("push", "origin", "main", cwd=box.dev)
    proc = subprocess.Popen(["/bin/bash", str(SCRIPT)], env=box.env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    with pytest.raises(subprocess.TimeoutExpired):     # the run is killed while its check hangs
        proc.wait(timeout=5)
    os.killpg(proc.pid, 9)
    proc.wait()
    pending = box.app / "logs" / "auto_update_pending"
    assert pending.read_text().strip() == before and (box.app / "BREAK").exists()
    shutil.rmtree(box.app / "logs" / "auto_update.lock", ignore_errors=True)     # as if the lock went stale
    res = box.run()
    assert res.returncode == 1
    assert box.head() == before and not (box.app / "BREAK").exists() and not pending.exists()
    st = box.state()
    assert st["result"] == "failed" and "stopped before its check finished" in st["message"]
    assert "went back to" in st["message"] and before.startswith(st["commit"])


def test_a_step_that_hangs_is_stopped_and_rolled_back(box):
    before = box.head()
    (box.dev / "HANG").write_text("")
    box.push("app.txt", "version 2\n")
    res = box.run(BLUEMOON_UPDATE_TIMEOUT_SECONDS="2")
    assert res.returncode == 1 and box.head() == before
    st = box.state()
    assert st["result"] == "failed" and "went back to" in st["message"] and "took longer than" in st["message"]


def test_a_stale_uv_lock_never_blocks_later_updates(box):
    (box.dev / "uv.lock").write_text("lock 1\n")
    box.push("app.txt", "version 2\n")
    assert box.run().returncode == 0
    (box.app / "uv.lock").write_text("lock rewritten by uv run\n")      # uv re-locked on this Mac
    new = box.push("app.txt", "version 3\n")
    res = box.run()
    assert res.returncode == 0 and box.state()["result"] == "updated" and box.head() == new
    assert (box.app / "uv.lock").read_text() == "lock 1\n"


def test_a_running_job_postpones_the_update(box):
    before = box.head()
    box.push("app.txt", "version 2\n")
    res = box.run(FAKE_JOBS="2")
    assert res.returncode == 0 and box.head() == before
    st = box.state()
    assert st["result"] == "skipped" and "2 job(s) are running" in st["message"]
    assert box.called() == []        # nothing pulled, installed or restarted
    res = box.run()
    assert box.state()["result"] == "updated" and box.head() != before


def test_a_job_that_starts_during_the_check_delays_only_the_restart(box):
    box.push("app.txt", "version 2\n")
    (box.bin / "uv").write_text((box.bin / "uv").read_text().replace(
        '{ echo "${FAKE_JOBS:-0}"; exit 0; }',
        '{ n=$(cat "$HOME/jobcalls" 2>/dev/null || echo 0); echo $((n+1)) > "$HOME/jobcalls"; '
        '[ "$n" -ge 1 ] && echo "${LATE_JOBS:-0}" || echo 0; exit 0; }'))
    assert box.run(LATE_JOBS="1").returncode == 0
    st = box.state()
    assert st["result"] == "updated" and "keeps running the version before" in st["message"]
    assert not any(c.startswith("launchctl") for c in box.called())
    assert (box.app / "logs" / "auto_update_restart_pending").exists()
    assert box.run().returncode == 0       # next night: nothing new, the job is done, so it restarts now
    assert box.state()["result"] == "up_to_date" and "restarted to use it" in box.state()["message"]
    assert box.called()[-1].endswith("/com.bluemoon.dealfinder")
    assert not (box.app / "logs" / "auto_update_restart_pending").exists()


def test_team_link_restarts_only_when_its_code_changed_and_says_so(box):
    (box.dev / "bluemoon").mkdir()
    box.push("bluemoon/share.py", "new share code\n")
    assert box.run().returncode == 0
    uid = os.getuid()
    assert box.called()[-2:] == [f"launchctl kickstart -k gui/{uid}/com.bluemoon.dealfinder",
                                 f"launchctl kickstart -k gui/{uid}/com.bluemoon.share"]
    assert "new address" in box.state()["message"]


def test_active_jobs_counts_only_live_queued_or_running_jobs(settings):
    from bluemoon import db, jobs
    assert autoupdate.active_jobs(settings) >= 0
    conn = db.connect(settings=settings)
    alive = json.dumps({jobs.PID_KEY: os.getpid()})
    dead = json.dumps({jobs.PID_KEY: 999999999})
    for status, params in (("running", alive), ("queued", alive), ("running", dead), ("done", alive)):
        conn.execute("INSERT INTO jobs(kind, status, progress, message, params_json, created_at) "
                     "VALUES ('photos_all', ?, 0, '', ?, ?)", (status, params, db.now_iso()))
    conn.commit()
    conn.close()
    assert autoupdate.active_jobs(settings) == 2


def test_cli_jobs_prints_just_the_number(settings, capsys):
    import run
    assert run.main(["autoupdate", "jobs"]) == 0
    assert capsys.readouterr().out.strip().isdigit()
