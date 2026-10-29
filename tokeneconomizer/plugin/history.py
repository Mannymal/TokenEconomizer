"""Learning from the sessions a repository ran before the plugin was installed.

Every mechanism otherwise starts from nothing at install time, though Claude Code has kept every
session's transcript all along: `<config dir>/projects/<encoded directory>/<session id>.jsonl`,
the config directory being `$CLAUDE_CONFIG_DIR`, else `~/.claude`. The first session start in a
repository (with `history=on`, the default) hands those transcripts to a detached `te history`,
which folds them oldest first, as if each had just ended, before the plugin learns from new
sessions -- and, since the hooks never saw their turns, replays each prompt-turn through the
loop guard's `TurnGuard` so the ended-green ones feed the envelope as their Stop hooks would
have.

What keeps that from counting anything twice or spending anything:

  * A session is folded once, by id (`folded.json`), whichever path folds it; one the hooks
    recorded (`sessions/<id>.json`) is the live path's and is never taken here, so neither its
    facts nor its turns are learned twice. A transcript still being written is left alone.
  * The folds make no model call (`fold_session(history=True)`): a backlog of a hundred
    sessions would otherwise run the micro-skills proposer some thirty times. Their digests stay
    in the proposer's window, and its next tick, in a live session's fold, reads them.
  * The deny list they may form is not queued here (`fold_session(history=True)`): the next
    live session's fold queues it, behind `toolprune_quiet_minutes` and announced as always,
    so nothing changes in the agent's settings before a session with the plugin has run.
  * At most `history_sessions` of the latest sessions are folded, and it runs once per
    repository and agent (`history.json`); `te history run` looks again.

A session belongs to the repository its starting directory resolves to (`repo_identity`), as a
live session's first hook decides: another clone or worktree of the same repository counts, a
subdirectory too, and nothing else does. Only Claude Code keeps its sessions where this reads.
"""
import os
import time

from .. import loopguard, trace
from . import fold
from .paths import repo_identity

STATE = "history.json"
RESPAWN_S = 1800        # a backfill started this recently is taken to be still running


def supports(host_name):
    return host_name == "claude"


def claude_home(env=None):
    env = os.environ if env is None else env
    return env.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~"), ".claude")


def _session_files(project_dir):
    """A project directory's own session transcripts (a subagent's live one level down)."""
    try:
        names = os.listdir(project_dir)
    except OSError:
        return []
    return [os.path.join(project_dir, n) for n in sorted(names) if n.endswith(".jsonl")]


def projects(env=None):
    """[(cwd, [transcript, ...])] per project directory the agent kept: every session in one
    was started in the same directory, which the first transcript that records one names."""
    base = os.path.join(claude_home(env), "projects")
    try:
        dirs = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for d in dirs:
        files = _session_files(os.path.join(base, d))
        cwd = next((c for c in map(trace.claude_session_cwd, files[:20]) if c), None)
        if cwd and files:
            out.append((cwd, files))
    return out


def candidates(ctx, now=None, env=None):
    """[(session_id, transcript)] of this repository's earlier sessions that are still to be
    folded, oldest first: the latest `history_sessions` of them."""
    now = now or time.time()
    folded = ctx.store.read("folded.json") or {}
    keys = {}
    found = []
    for cwd, files in projects(ctx.env if env is None else env):
        if cwd not in keys:
            try:
                keys[cwd] = repo_identity(cwd, ctx.env).key
            except OSError:
                keys[cwd] = None
        if keys[cwd] != ctx.repo.key:
            continue
        for path in files:
            sid = os.path.splitext(os.path.basename(path))[0]
            if sid in folded or os.path.exists(ctx.store.path("sessions", f"{sid}.json")):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if now - mtime < fold.STALE_S:          # still running, or only just ended
                continue
            found.append((mtime, sid, path))
    found.sort()
    return [(sid, path) for _, sid, path in found[-max(0, ctx.config.history_sessions):]]


def replay_turns(ctx, transcript):
    """Feed the envelope the session's prompt-turns, as their Stop hooks would have; returns
    how many turns were replayed. Each call is folded as its PostToolUse would have folded it,
    with no judge and a redirect budget no turn reaches, so no turn is cut short here that the
    hooks did not see."""
    if ctx.config.loopguard == "off" or ctx.config.shadow:
        return 0
    n = 0
    for calls, ended in trace.claude_turns(transcript):
        if not calls:
            continue
        g = loopguard.TurnGuard()
        for name, args, text, code in calls:
            g.fold(name, args, text, code, nudge_budget=len(calls) + 1)
        if ended:
            loopguard.record_turn(ctx.store, g, level=ctx.config.loopguard_aggr,
                                  overrides=ctx.config.loopguard_thresholds)
        n += 1
    return n


def needed(ctx):
    return (supports(ctx.host.name) and ctx.config.history != "off"
            and not (ctx.store.read(STATE) or {}).get("done"))


def backfill(ctx, force=False):
    """Fold this repository's earlier sessions (`candidates`). Returns the state it leaves in
    `history.json`, or None when it did not run: off, not this agent, already done (unless
    `force`), or another backfill holds the lock."""
    if not supports(ctx.host.name) or ctx.config.history == "off":
        return None
    if not force and (ctx.store.read(STATE) or {}).get("done"):
        return None
    with ctx.store.locked("history", timeout=0) as got:
        if not got:
            return None
        state = ctx.store.read(STATE) or {}
        todo = candidates(ctx)
        sessions = turns = 0
        for sid, path in todo:
            if fold.fold_session(ctx, sid, transcript_path=path, history=True) is None:
                continue
            sessions += 1
            turns += replay_turns(ctx, path)
            state.update(sessions=int(state.get("sessions", 0)) + 1, updated=time.time())
            ctx.store.write(STATE, state)      # progress, should the job be killed
        state.update(done=time.time(), source=os.path.join(claude_home(ctx.env), "projects"),
                     last_run={"found": len(todo), "folded": sessions, "turns": turns},
                     turns=int(state.get("turns", 0)) + turns)
        state.setdefault("sessions", 0)
        ctx.store.write(STATE, state)
        return state


def start(ctx):
    """At a session start: the first time in this repository, run `backfill` in the background
    (inline under `TE_FOLD_SYNC=1`, for tests). A start marker keeps concurrent session starts
    from piling up jobs."""
    if not needed(ctx):
        return False
    if ctx.env.get("TE_FOLD_SYNC") == "1":
        return backfill(ctx) is not None
    state = ctx.store.read(STATE) or {}
    if time.time() - float(state.get("started") or 0) < RESPAWN_S:
        return False
    ctx.store.write(STATE, dict(state, started=time.time()))
    return ctx.spawn(["history", "run"], "fold.log")


def notice(ctx, command):
    """Once a backfill has folded anything: the sentence saying so, else None."""
    state = ctx.store.read(STATE) or {}
    n = int(state.get("sessions") or 0)
    if not state.get("done") or not n:
        return None
    return (f"TokenEconomizer learned from {n} earlier session(s) of {ctx.repo.label} found in "
            f"{state.get('source') or 'the session history'} (review with `{command}`).")
