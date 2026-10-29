---
title: "Test the plugin against real session history in shadow mode, not with a throwaway state folder"
author: Mannymal
date: 2026-10-29
branch: history-backfill
cost: { minutes: 1, tokens: 1372 }
id: l-b9440640
scope: repo
status: active
src: agent
touches:
  - tokeneconomizer/plugin/history.py
  - tokeneconomizer/plugin/fold.py
  - plugin/scripts/te-hook
---

## Decided
Running the plugin against real past sessions is only safe with shadow mode on. A throwaway state folder keeps the plugin's own state out of the real one, but tool pruning in auto mode still writes its deny list into each real repository's Claude Code settings, so tools vanish from live sessions. Folding past sessions now never queues a deny list either: only a fold after a live session can, behind the usual quiet period and notice.

## Tried and ruled out
A dry run with only TOKENECONOMIZER_HOME pointed at a temp folder: it denied 264 tools in the home repository's settings, which had to be undone with the toolprune restore command.

## Watch out
On Windows with core.autocrlf=true, the shell launchers get CRLF line endings when git rewrites them, and every hook then fails silently. The Windows Store python3 alias also sits on PATH with no Python behind it; the launchers skip it.

In the code:
- TE_SHADOW=1 for any run over real transcripts; `te history run --all` walks every repository in ~/.claude/projects
- fold_session(history=True) passes hold=False to the toolprune folds, so no auto_pending is set
- `te --host claude --cwd <repo> toolprune restore` removes the plugin's deny entries
- plugin/scripts/te and te-hook must stay LF
