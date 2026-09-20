#!/usr/bin/env bash
# Record WHICH sessions are running in herdr right now, so that after herdr stops there is still an
# answer to "which dozen did I have open?".
#
# The tab store (state_claude_session_herdr_tabs.json) says what each herdr tab IS. It cannot say
# which tabs were LIVE, and after a restart every pane is a bare shell, so the live set is exactly
# the thing that is lost. This writes it down every minute instead of inferring it afterwards.
#
# THE LOAD-BEARING RULE: when herdr is unreachable, write NOTHING and exit 0. The file then still
# holds the last set seen while herdr was up, which is the set the restore wants. A recorder that
# wrote an empty list on failure would erase the answer at exactly the moment it is needed —
# the file is only ever overwritten by a reading taken from a herdr that answered.
#
# Speaks the socket directly rather than the herdr CLI. The CLI refuses every call while the
# installed binary is newer than the running server, which is the state the machine sits in
# whenever a restart is pending. See ai/cli-herdr.md.
#
# It has a second job, below: re-recording the TAB store for any tab whose session moved.
# Runs from cron every minute.
set -uo pipefail

SOCK="${HERDR_SOCKET_PATH:-$HOME/.config/herdr/herdr.sock}"
STORE="${HERDR_LIVE_STORE:-$HOME/.claude/state/state_claude_herdr_live_sessions.json}"
TITLES="${CLAUDE_TITLE_STORE:-$HOME/.claude/state/state_claude_session_titles.json}"

# No -S test on the socket first: nc fails the same way on a missing socket as on a dead one, so the
# empty-reply guard below already covers both and a second check was a mechanism no test could see.
reply=$(printf '{"id":"rec","method":"agent.list","params":{}}\n' | nc -U -w5 "$SOCK" 2>/dev/null)
# ONE guard for every way herdr can fail to answer: no socket, a dead socket, a malformed reply, or a
# real error reply. An earlier draft also tested -S on the socket and -n on the reply; neither could
# be told apart from this line by any test, so both went. What matters is that all four cases exit 0
# in SILENCE — this runs from cron every minute, and a noisy failure is a mail every minute.
# Removing this line does not merely skip a check: jq given EMPTY stdin reads no input, exits 0 and
# prints nothing, so the script would mv a ZERO-BYTE file over the store and erase the restore list
# without a word. Measured by mutation 2026-09-08. The guard is the whole safety of this script.
jq -e '.result.agents' >/dev/null 2>&1 <<<"$reply" || exit 0

# ---------------------------------------------------------------------------
# SECOND JOB: keep the TAB store honest when a tab MOVES.
#
# record-herdr-tab.sh writes a tab's session ONCE, at SessionStart. Move that session to another
# workspace and herdr cannot carry the tab across — tab.move takes an insert_index and no
# workspace_id — so the PANE moves and a NEW tab is built around it with a NEW public id. The old
# row still names the session and the new tab has no row at all, which is silent: `hr` in the moved
# tab finds nothing and drops to the picker, looking like an ordinary miss. Measured 2026-09-08:
# a session moved wC:tW -> w8:tS and the store still pointed at wC:tW half an hour later.
#
# Fixing herdr_session_manage.sh move would only cover moves made through that verb. This
# runs against whatever herdr actually reports, so a tab dragged by hand in the GUI is covered too,
# within a minute. That minute is the whole cost: a move followed by an immediate `hr` still misses.
#
# Placed BEFORE the live-store write because that block returns early on an unchanged set.
TAB_STORE="${HERDR_TAB_STORE:-$HOME/.claude/state/state_claude_session_herdr_tabs.json}"
# herdr_session_resolve_tab.py sits beside this script, and cron's PATH is /usr/bin:/bin — which does
# NOT carry BinariesExecutables. A bare call therefore resolves to nothing under cron and the label is
# written EMPTY, in silence, while every interactive test passes. Same class as md5 living in /sbin.
# Prefer the sibling; fall back to PATH so a copy run from elsewhere still finds one.
RESOLVER="$(dirname "${BASH_SOURCE[0]}")/herdr_session_resolve_tab.py"
[ -x "$RESOLVER" ] || RESOLVER=herdr_session_resolve_tab.py
reconcile_tabs() {
  # The hook owns this file's creation and its _readme. Never create it here: a store conjured by
  # cron would have no header, and the hook's early-exit would then never put one back.
  [ -f "$TAB_STORE" ] || return 0

  # Which live tabs disagree with the store. Read-only, and the common answer is none.
  local stale
  stale=$(jq -r --slurpfile st "$TAB_STORE" '
            .result.agents[]
            | select(.agent_session.value != null)
            | select((($st[0].tabs // {})[.tab_id] // {}).uuid != .agent_session.value)
            | [.tab_id, .agent_session.value, .pane_id, .workspace_id, (.cwd // "")] | @tsv
          ' <<<"$reply") || return 0
  [ -n "$stale" ] || return 0

  # Same mkdir lock the hook uses, so a SessionStart landing mid-reconcile cannot interleave.
  local lock="$TAB_STORE.lock"
  local got=0 i
  for i in $(seq 1 50); do
    if mkdir "$lock" 2>/dev/null; then got=1; break; fi
    sleep 0.05
  done
  [ "$got" = 1 ] || return 0                  # busy is not an error; the next minute retries
  trap 'rmdir "$lock" 2>/dev/null || true' RETURN

  local tab uuid pane ws cwd label now tmp
  now=$(date +%Y-%m-%dT%H:%M:%S%z)
  while IFS=$'\t' read -r tab uuid pane ws cwd; do
    [ -n "$tab" ] && [ -n "$uuid" ] || continue
    # The tab's CURRENT label, from the same reader the hook uses, so both writers mean the same
    # thing by the field. Empty is legitimate and hr reads it as "no check possible".
    label=$("$RESOLVER" --live-label "$tab" 2>/dev/null || true)
    tmp=$(mktemp "$TAB_STORE.XXXXXX") || continue    # 0600 whatever the umask; mv carries it
    # Byte-for-byte the hook's own upsert, minus the _readme handling it owns: the previous
    # occupant is pushed onto history rather than dropped, newest first, five deep.
    if jq --arg t "$tab" --arg s "$uuid" --arg p "$pane" --arg w "$ws" --arg c "$cwd" \
          --arg n "$now" --arg l "$label" '
          .tabs[$t].history = ([(.tabs[$t] // {} | select(.uuid) | {uuid, captured_at})]
                               + (.tabs[$t].history // []) | unique_by(.uuid) | .[0:5])
          | .tabs[$t] += {uuid:$s, pane:$p, workspace:$w, cwd:$c, label:$l, captured_at:$n}
        ' "$TAB_STORE" > "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
      mv "$tmp" "$TAB_STORE"
    else
      rm -f "$tmp"
    fi
  done <<<"$stale"
}
reconcile_tabs

readme="The sessions that were running in herdr the last time herdr answered. Written every minute by BinariesExecutables/herdr_session_record_live.sh, and NEVER written when herdr is unreachable, so after herdr stops this still holds the set that was live at the end. Read by herdr_session_restore.py, which hr calls when it is run outside a herdr pane. captured_at is when this exact set was first seen, not when it was last confirmed. Safe to delete: it refills within a minute while herdr is up, but deleting it while herdr is DOWN loses the restore list."

mkdir -p "$(dirname "$STORE")"
tmp=$(mktemp "$STORE.XXXXXX") || exit 0        # mktemp is 0600 whatever the umask, and mv carries it
trap 'rm -f "$tmp"' EXIT

if ! jq --arg r "$readme" --arg n "$(date +%Y-%m-%dT%H:%M:%S%z)" --slurpfile t "$TITLES" '
      {_readme: $r,
       captured_at: $n,
       sessions: [ .result.agents[]
                   | select(.agent_session.value != null)
                   | {tab: .tab_id, pane: .pane_id, workspace: .workspace_id,
                      uuid: .agent_session.value,
                      status: .agent_status,
                      title: (.terminal_title_stripped // ""),
                      stored_title: (($t[0][.agent_session.value] // {})
                                     | (.last_written // .cached_custom // .cached_ai // ""))} ]}
       | .sessions |= sort_by(.tab)' <<<"$reply" > "$tmp"; then
  # NOT silenced, and NOT exit 0. herdr being down is the quiet case and returns above; reaching here
  # means herdr answered and the filter failed, which is a bug in this script. A cron job that hides
  # that never writes the file and never says so — measured 2026-09-08, when a misplaced brace made
  # this exit 0 without writing and the only symptom was a file that never appeared.
  echo "herdr_session_record_live.sh: herdr answered but the jq filter failed" >&2
  exit 1
fi

# Only rewrite when the SET changed, so an unchanged minute costs no disk write and captured_at keeps
# meaning "when this set was first seen". Compare the sessions alone — captured_at always differs.
if [ -f "$STORE" ] \
   && [ "$(jq -cS '.sessions' "$STORE" 2>/dev/null)" = "$(jq -cS '.sessions' "$tmp")" ] \
   && [ "$(jq -r '._readme' "$STORE" 2>/dev/null)" = "$readme" ]; then
  exit 0
fi
mv "$tmp" "$STORE"
trap - EXIT
exit 0
