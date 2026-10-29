"""Learning from the sessions a repository ran before the plugin (`plugin.history`).

Claude Code's transcripts are laid out as it keeps them, under a `CLAUDE_CONFIG_DIR` of the
test's own; the backfill runs inline (`TE_FOLD_SYNC=1`) at the first session start."""
import json
import os
import time

import pytest

from tokeneconomizer import loopguard, trace
from tokeneconomizer.plugin import history
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin import hosts

from test_live_hooks import call, store_for, trap_session  # noqa: F401 (fixtures below)
from test_live_hooks import env, repo  # noqa: F401


@pytest.fixture
def claude_dir(tmp_path, env, monkeypatch):
    d = tmp_path / "claude-config"
    (d / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    env["CLAUDE_CONFIG_DIR"] = str(d)
    return d


def old_session(claude_dir, cwd, sid, i, age_s=3600, interrupted=False):
    """A transcript Claude Code kept before the plugin: its entries name their `cwd`."""
    proj = claude_dir / "projects" / cwd.replace(os.sep, "-").replace(":", "-").replace("/", "-")
    proj.mkdir(parents=True, exist_ok=True)
    path = str(proj / f"{sid}.jsonl")
    trap_session(path, cwd, i)
    lines = [json.loads(ln) for ln in open(path)]
    for ev in lines:
        ev["cwd"] = cwd
    if interrupted:
        lines.append({"type": "user", "cwd": cwd, "message": {
            "role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}})
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def start(env, repo, sid="live", transcript=""):
    return call(env, {"session_id": sid, "transcript_path": transcript, "cwd": repo,
                      "hook_event_name": "SessionStart", "source": "startup"})


def test_claude_turns_pairs_results_and_marks_interrupts(tmp_path, claude_dir, repo):
    path = old_session(claude_dir, repo, "a", 0, interrupted=True)
    turns = trace.claude_turns(path)
    assert len(turns) == 1
    calls, ended = turns[0]
    assert [c[0] for c in calls] == ["Edit", "Bash", "Bash"]
    assert calls[1][2] == "bash: pytest: command not found"
    assert not ended
    assert trace.claude_session_cwd(path) == repo


def test_first_session_start_learns_from_earlier_sessions(env, repo, claude_dir, tmp_path):
    for i in range(5):
        old_session(claude_dir, repo, f"old{i}", i, age_s=3600 * (10 - i))
    # Another repository's history is not this one's.
    other = tmp_path / "other"
    other.mkdir()
    old_session(claude_dir, str(other), "elsewhere", 9)

    start(env, repo)
    st = store_for(env, repo)
    h = st.read(history.STATE)
    assert h["done"] and h["sessions"] == 5 and h["turns"] == 5
    folded = st.read("folded.json")
    assert {f"old{i}" for i in range(5)} <= set(folded) and "elsewhere" not in folded
    ledger = st.read_lines("ledger.jsonl")
    assert len(ledger) == 5 and all(r["history"] for r in ledger)
    # The facts those sessions teach are already there for the next session.
    assert "pytest" in (st.read("injection.json") or {}).get("rendered", "")
    tracker = loopguard.load_envelope(st)
    assert len(tracker.resolved_turns) == 5

    # Said once, then not again; and not run again.
    res = start(env, repo, sid="live2")
    assert "earlier session(s) of" in json.dumps(res)
    assert "earlier session(s) of" not in json.dumps(start(env, repo, sid="live3"))
    assert len(st.read_lines("ledger.jsonl")) == 5


def test_history_skips_live_recent_and_interrupted(env, repo, claude_dir):
    old_session(claude_dir, repo, "fresh", 0, age_s=5)              # still running
    old_session(claude_dir, repo, "cut", 1, interrupted=True)
    old_session(claude_dir, repo, "live", 2)                        # this session's own
    start(env, repo, sid="live")
    st = store_for(env, repo)
    folded = st.read("folded.json") or {}
    assert "fresh" not in folded and "live" not in folded and "cut" in folded
    # An interrupted turn feeds no envelope, as at its hooks.
    assert loopguard.load_envelope(st).resolved_turns == []


def test_history_respects_off_and_the_cap(env, repo, claude_dir):
    for i in range(4):
        old_session(claude_dir, repo, f"o{i}", i, age_s=3600 * (10 - i))
    env_off = dict(env, TE_HISTORY="off")
    start(env_off, repo)
    assert store_for(env, repo).read(history.STATE) is None

    env_cap = dict(env, TE_HISTORY_SESSIONS="2")
    start(env_cap, repo, sid="live2")
    folded = store_for(env, repo).read("folded.json")
    assert "o2" in folded and "o3" in folded and "o0" not in folded


def test_history_is_claude_code_only(env, repo, claude_dir):
    old_session(claude_dir, repo, "o", 0)
    ctx = LiveContext.build(hosts.HOSTS["codex"](), repo, env=env)
    assert not history.needed(ctx) and history.backfill(ctx) is None


def test_history_writes_no_deny_list_before_a_live_session_has_run(env, repo, claude_dir):
    """Enough earlier sessions to form a deny list: the backfill keeps the evidence and the
    proposal, but queues nothing, so the agent's settings are untouched."""
    for i in range(8):
        old_session(claude_dir, repo, f"o{i}", i, age_s=3600 * (20 - i))
    ctx = LiveContext.build(hosts.HOSTS["claude"](), repo, env=env)
    assert history.backfill(ctx)["sessions"] == 8
    unt = ctx.store.read("untool.json")
    assert unt["sessions"] == 8 and not unt.get("auto_pending")
    assert not os.path.exists(os.path.join(repo, ".claude", "settings.local.json"))
