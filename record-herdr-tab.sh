#!/usr/bin/env bash
# SessionStart hook — record which herdr TAB a session is living in, so the tab can still name its
# session after that session exits.
#
# The problem it solves: herdr keeps the pane->session mapping only while the agent is ALIVE. It
# deletes workspaces[].tabs[].panes[].agent_session from ~/.config/herdr/session.json the moment the
# agent goes away, so after a `brew services restart herdr` every tab John did not resume is left with
# nothing but its label. Measured against the hourly backups on 2026-09-08: 13 of 74 tabs still
# carried an id on 2026-09-04, 18 of 78 on 09-06, 24 of 82 on 09-07 — in each case exactly the number
# of agents that were live that hour. This hook writes the same fact down somewhere herdr cannot erase.
#
# The tab id comes from HERDR_TAB_ID, which the herdr server injects into every pane it spawns and
# claude passes down to its hooks. A session started outside herdr has no such variable and no-ops.
#
# Keyed by TAB, not pane: a tab outlives the panes inside it, and the tab label is what John actually
# reads in the tab bar. The sibling store state_claude_session_last_herdr_pane.json goes the other way
# (session -> pane, written by herdr_session_manage.sh) and answers a different question —
# "where is this session now", not "what was this tab". Both are wanted; neither replaces the other.
#
# No title is stored. state_claude_session_titles.json is the canonical session-id -> title map and a
# copy here would drift out of date the first time /retitle ran. `hr` joins the two at read time.
#
# Up to five previous occupants are kept per tab, newest first, because the current row is not always
# the right answer. Any claude started INSIDE a pane inherits that pane's HERDR_TAB_ID and records
# itself as the tab's session — a nested or throwaway one included. Measured 2026-09-08: a diagnostic
# spawned claude in John's own pane, took that tab's row, and its transcript was deleted minutes
# later, so hr resolved the tab to a session that no longer existed and fell through to the picker.
# History lets hr fall back to the previous occupant instead of losing the tab.
#
# Idempotent; mkdir-locked so concurrent SessionStarts cannot clobber the store.
set -euo pipefail

input=$(cat)
sid=$(jq -r '.session_id // empty' <<<"$input")
[ -n "$sid" ] || exit 0
tab="${HERDR_TAB_ID:-}"
[ -n "$tab" ] || exit 0                       # not inside a herdr pane -> nothing to record
# A throwaway session (ci/cii: a temp CLAUDE_CONFIG_DIR carrying the .ci-priv tag that _ci_populate
# writes) is NOT recorded. Its transcript is wiped on exit, so a tab that named it could only resolve
# to a session that no longer exists, and the grid would park it as a cell nobody can wake — it had
# one such ghost, labeled "ci", by 2026-09-16 (John: skip them at the hook).
[ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ -f "$CLAUDE_CONFIG_DIR/.ci-priv" ] && exit 0

cwd=$(jq -r '.cwd // empty' <<<"$input")
pane="${HERDR_PANE_ID:-}"
ws="${HERDR_WORKSPACE_ID:-}"
now=$(date +%Y-%m-%dT%H:%M:%S%z)
# The tab's label, recorded beside the id so hr can tell later whether the two still belong together.
# Empty is a legitimate answer — an unlabelled tab, or herdr's layout file not yet caught up with a
# tab made seconds ago — and hr reads empty as "no check possible", never as a mismatch.
label=$(herdr_session_resolve_tab.py --live-label "$tab" 2>/dev/null || true)

# HERDR_TAB_STORE lets the selftest redirect every write away from John's live store. Never point it
# at the live path in a test — the suite proves the redirect held before it writes anything.
store="${HERDR_TAB_STORE:-$HOME/.claude/state/state_claude_session_herdr_tabs.json}"
mkdir -p "$(dirname "$store")"
lock="$store.lock"
for _ in $(seq 1 50); do
  if mkdir "$lock" 2>/dev/null; then trap 'rmdir "$lock" 2>/dev/null || true' EXIT; break; fi
  sleep 0.05
done
[ -d "$lock" ] || exit 0
[ -f "$store" ] || echo '{}' > "$store"

readme="Which Claude Code session each herdr TAB last held, so a tab whose session is no longer running can still be resumed. Keys are herdr public tab ids such as wA:t2F; the value carries the session uuid and where it was seen. herdr itself forgets this the instant an agent exits, which is why the file exists. Written by dotfiles/.claude/hooks/record-herdr-tab.sh at session start, and every minute by BinariesExecutables/herdr_session_record_live.sh for any tab whose session has since moved to another workspace. Read by the hr shell function, which resumes the calling tab's own session. To turn a session id into a title, look it up in state_claude_session_titles.json, field last_written. Safe to delete: live tabs refill it as their sessions restart, and the hourly backup (the tabstore family) holds the rest."

# Guard the readme as well as the row, or deleting the header would leave it gone for good — every tab
# already recorded would take the early exit before any write could put it back.
[ "$(jq -r --arg t "$tab" '.tabs[$t].uuid // empty' "$store" 2>/dev/null)" = "$sid" ] \
  && [ "$(jq -r '._readme // empty' "$store" 2>/dev/null)" = "$readme" ] && exit 0

# mktemp is load-bearing for the PERMISSIONS, not just for the atomic swap: it creates at 0600
# regardless of umask, and mv is a rename, so the private mode rides onto the store. Creating $tmp any
# other way publishes a file full of session ids at whatever umask happens to be set.
tmp=$(mktemp "$store.XXXXXX")
# `{_readme:...} + del(._readme)` puts the header FIRST; a plain `._readme=$r` would append it last.
if jq --arg t "$tab" --arg s "$sid" --arg p "$pane" --arg w "$ws" --arg c "$cwd" --arg n "$now" --arg l "$label" --arg r "$readme" \
      '{_readme:$r} + del(._readme)
       | .tabs[$t].history = ([(.tabs[$t] // {} | select(.uuid) | {uuid, captured_at})]
                              + (.tabs[$t].history // []) | unique_by(.uuid) | .[0:5])
       | .tabs[$t] += {uuid:$s, pane:$p, workspace:$w, cwd:$c, label:$l, captured_at:$n}' \
      "$store" > "$tmp" 2>/dev/null; then mv "$tmp" "$store"; else rm -f "$tmp"; fi
exit 0
