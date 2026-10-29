#!/usr/bin/env bash
# SessionStart hook: tells you when this repo has decisions whose answers came back
# and haven't been worked yet.
#
# Deliberately pure shell against the checked-in index — no python, no venv, no DB,
# no running orchestrator. The answer is already in the repo by the time a session
# starts (repo_writer wrote it when the answer landed), so reading anything else
# would be slower and could fail on a machine that has the repo but not the tooling.
# It also costs the agent zero tokens: the count comes from grep, and only the
# actionable rows ever reach the model's context, however long the index gets.
#
# Must never fail a session start — always emits valid JSON, `{}` on any problem.
#
# Not auto-registered: add it to ~/.claude/settings.json's SessionStart hooks
# yourself (see README.md). It only ever reads.

INDEX="$PWD/.claude/decisions/INDEX.md"

emit_nothing() { echo '{}'; exit 0; }

# SessionStart also fires on resume/clear/compact. Nudging on those re-injects the
# same message repeatedly through a long session, which is how a passive notice turns
# into nagging. Only a genuine session start is worth interrupting for.
#
# `timeout` guards against a hook that hangs waiting on stdin — a blocked SessionStart
# hook is a far worse failure than a missed nudge. If source can't be determined we
# nudge anyway: failing open keeps the feature working on a harness that sends no
# payload, and the cost of being wrong is one extra notice.
PAYLOAD="$(timeout 1 cat 2>/dev/null || true)"
if [ -n "$PAYLOAD" ]; then
  SOURCE="$(printf '%s' "$PAYLOAD" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("source",""))' 2>/dev/null || true)"
  case "$SOURCE" in
    resume|clear|compact) emit_nothing ;;
  esac
fi

[ -r "$INDEX" ] || emit_nothing

# Column layout is fixed by repo_writer._render_index: | STATUS | id | title | file |
# ANSWERED rows carry a human's answer; READY rows are tasks the human assigned to an agent.
ROWS="$(grep -E '^\| (ANSWERED|READY) \|' "$INDEX" 2>/dev/null)" || emit_nothing
[ -n "$ROWS" ] || emit_nothing

N_ANSWERED="$(printf '%s\n' "$ROWS" | grep -c '^| ANSWERED |')"
N_READY="$(printf '%s\n' "$ROWS" | grep -c '^| READY |')"
COUNT=$((N_ANSWERED + N_READY))
[ "$COUNT" -gt 0 ] 2>/dev/null || emit_nothing

PARTS=""
if [ "$N_ANSWERED" -gt 1 ]; then PARTS="$N_ANSWERED answered decisions"; elif [ "$N_ANSWERED" -eq 1 ]; then PARTS="1 answered decision"; fi
if [ "$N_READY" -gt 0 ]; then
  TASKS="$N_READY task$([ "$N_READY" -gt 1 ] && echo s) assigned to an agent"
  PARTS="${PARTS:+$PARTS and }$TASKS"
fi
if [ "$COUNT" -gt 1 ]; then VERB="are"; else VERB="is"; fi
SUBJECT="$PARTS"
LIST="$(printf '%s\n' "$ROWS" | head -10 | awk -F' *\\| *' '{print "  #" $3 " " $4 " (" $2 ")"}')"

# Deliberately phrased as a notice, not an instruction. /decisions spawns a subagent
# per decision and would hijack whatever the session was actually opened to do.
MESSAGE="$SUBJECT in this repo $VERB waiting to be worked:
$LIST
The user can run /decisions when these should be picked up — do not run it unprompted,
and do not re-ask these questions. Full records are in .claude/decisions/."

# python3 -c is only for JSON-escaping the message; the hook still works without a
# venv or this project's deps, and falls back to no nudge rather than broken JSON.
python3 -c 'import json,sys; print(json.dumps({"systemMessage": sys.stdin.read().rstrip()}))' <<<"$MESSAGE" 2>/dev/null || emit_nothing
