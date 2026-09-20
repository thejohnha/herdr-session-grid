#!/usr/bin/env python3
"""herdr_session_grid.py — one picker for every herdr workspace and tab. Spec: ~/git/bin/ai/cli-herdr-grid.md

Every workspace is a row, every tab a cell. A cell that held a Claude session keeps its place when
the tab is closed (parked, shown as P) and one keypress brings it back. herdr answers what is live;
this program's own file answers parked cells, their order and their labels — never both for one fact.

Keys:  arrows move   Enter open   n new session   p park   x forget (parked only)   m grab/drop (reorder)
       r rename tab label   = push the grid's order onto herdr   R refresh   ? help   q quit
`hg` (or `hg go`) — go to the grid: creates the `herdr sessions` workspace at position 0 with one tab named for the herdr
session if missing, relaunches the grid there if the pane is back at a shell, and focuses it.
`hg run` — draw the grid in THIS pane; what `hg` types into the grid's own pane, not a verb to reach for.
"""
import curses, hashlib, json, locale, os, re, socket, subprocess, sys, time, fcntl, unicodedata

HOME = os.path.expanduser("~")
STATE = f"{HOME}/.claude/state/state_claude_session_grid.json"
TITLES = f"{HOME}/.claude/state/state_claude_session_titles.json"
LINEAGE = f"{HOME}/.claude/state/state_claude_session_title_lineage.json"
CURSOR = f"{HOME}/.claude/state/state_claude_session_grid_cursor.json"   # where the cursor was, so a restart lands on the same cell (John 2026-09-16)
TABSTORE = f"{HOME}/.claude/state/state_claude_session_herdr_tabs.json"   # tab -> last session it held; what hr reads
IGNORE_WS = ("selftest",)   # workspace labels the selftests create and tear down; their tabs would otherwise park as cells
SOCK = f"{HOME}/.config/herdr/herdr.sock"
MANAGE = f"{HOME}/git/bin/BinariesExecutables/herdr_session_manage.sh"
GRID_WS = "herdr sessions"   # the workspace hg go keeps first; one tab per herdr session, each running the grid
ACCOUNTS = f"{HOME}/.claude/state/state_claude_session_accounts.json"   # uuid -> account; absent = personal
# The first prompt `n` sends when John typed a few words. His words are quoted; the titling is the
# session's to draft and his to approve — nothing here sets a title, a directive or a done_when.
SEED = ("John started this session from the grid and typed these words for it: «{words}». "
        "From them, propose a long session title, a prime directive and a done_when, show all three to him "
        "in one short message, and set nothing until he approves. Then wait for his go.")
README = ("The grid's own memory: workspace row order, and per workspace the ordered cells. A cell with "
          "tab=null is PARKED — no herdr tab, session on disk, reopened by Enter. Live cells mirror herdr "
          "and are rewritten on every refresh; only parked cells, order and labels are authoritative here. "
          "Written by herdr_session_grid.py; spec ~/git/bin/ai/cli-herdr-grid.md")
# Same split as herdr_session_manage.sh series_members: a lineage is a title minus date, marker and number.
SPLIT = re.compile(r"^(\[RM\]\s+|\[ARCH\]\s+)?(\d{8}\s+)?((?:⭐|☑️|✅|[1-9]️⃣)\s+)?(.*?)(\s+\d{1,3})?(,\s+originally:.*?)?(\s+\d{1,3})?$")   # ☑️ is a marker too; series_members lacks it. Group 7: the number sits LAST, after the ", originally: …" breadcrumb (John 2026-09-19); group 5 still reads the older shape

# ---------------------------------------------------------------- herdr
def herdr(*args):
    r = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=20)
    try: return json.loads(r.stdout)
    except Exception: return {}

def last_line(text, n=120):
    """A failed subprocess's output, fit for the message line: its LAST non-empty line, whitespace collapsed,
    at most n chars. The raw tail carried the manager's earlier stderr lines too, and curses returns ERR for a
    newline on the bottom row — that killed the grid on John's first real `n` (2026-09-18: a 3-word label's
    note preceded the error, so the message had two lines)."""
    lines = [" ".join(l.split()) for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    return (lines[-1] if lines else "")[-n:]

def sock(method, params):
    """One JSON line on the unix socket; the CLI has no verb for tab.move (cli-herdr.md)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(3)
    try:
        s.connect(SOCK); s.sendall((json.dumps({"id": "grid", "method": method, "params": params}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk: break
            buf += chunk
        return json.loads(buf or b"{}")
    except Exception as e:
        return {"error": {"message": str(e)}}
    finally: s.close()

def snapshot():
    """workspaces in herdr order, tabs per workspace in herdr order, agent per tab, first pane per tab."""
    snap = herdr("api", "snapshot").get("result", {}).get("snapshot", {})
    ws = [(w["workspace_id"], w.get("label") or w["workspace_id"]) for w in snap.get("workspaces", [])]
    tabs = {}
    for t in snap.get("tabs", []): tabs.setdefault(t["workspace_id"], []).append((t["tab_id"], t.get("label") or ""))
    agents = {a["tab_id"]: (a["agent_session"]["value"], a.get("agent_status", "unknown"))
              for a in snap.get("agents", []) if (a.get("agent_session") or {}).get("kind") == "id"}
    panes = {}
    for p in snap.get("panes", []): panes.setdefault(p["tab_id"], p["pane_id"])
    return ws, tabs, agents, panes, snap.get("focused_tab_id", "")

# ---------------------------------------------------------------- stores
def jload(p, default):
    try:
        with open(p) as fh: return json.load(fh)
    except Exception: return default

def title_of(uuid, titles):
    t = titles.get(uuid)
    if not isinstance(t, dict): return ""
    return t.get("last_written") or t.get("cached_custom") or t.get("cached_ai") or ""

def lineage_key(title, uuid):
    m = SPLIT.match(title or ""); base = (m.group(4) or "").strip() if m else ""   # the ", originally: …" breadcrumb is group 6, not part of the key
    return base or uuid

def lineage_number(title):
    m = SPLIT.match(title or ""); n = (m.group(5) or m.group(7) or "").strip() if m else ""
    return int(n) if n else -1

def newest_member(uuid, titles):
    """Highest-numbered member of this session's lineage, by title; the uuid itself if none is higher."""
    key = lineage_key(title_of(uuid, titles), uuid)
    if key == uuid: return uuid
    best, best_n = uuid, lineage_number(title_of(uuid, titles))
    for u, t in titles.items():
        if not isinstance(t, dict): continue                     # the store carries a _readme string
        tt = t.get("last_written") or t.get("cached_custom") or ""
        if lineage_key(tt, u) == key and lineage_number(tt) > best_n: best, best_n = u, lineage_number(tt)
    return best

_CTX = {}
def ctx_of(path):
    """Context tokens of the LAST assistant record: input + cache_read + cache_creation — the same sum
    the statusline shows as Cx and the auto-successor cron thresholds on. Reads the tail only; cached by mtime."""
    try: mt = os.path.getmtime(path)
    except OSError: return None
    k = (path, mt)
    if k in _CTX: return _CTX[k]
    val = None
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 4_000_000)); tail = fh.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            if '"assistant"' not in line or '"usage"' not in line: continue
            try: r = json.loads(line)
            except Exception: continue
            u = (r.get("message") or {}).get("usage") or {}
            if r.get("type") == "assistant" and u.get("input_tokens") is not None:
                val = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0); break
    except OSError: pass
    _CTX.clear(); _CTX[k] = val; return val

def fmt_tokens(n):
    return f"{n/1_000_000:.1f}M".replace(".0M", "M") if n >= 1_000_000 else f"{n/1000:.0f}k" if n >= 1000 else str(n)

def ctx_kind(n):
    """Statusline's smart-zone tiers in absolute tokens: green under 150k, yellow to 200k, red past it."""
    return "ctx_ok" if n < 150_000 else "ctx_warn" if n < 200_000 else "ctx_crit"

def transcript(uuid):
    d = f"{HOME}/.claude/projects"
    for proj in os.listdir(d) if os.path.isdir(d) else []:
        p = f"{d}/{proj}/{uuid}.jsonl"
        if os.path.exists(p): return p
    return ""

def load_state():
    st = jload(STATE, {})
    if not isinstance(st, dict) or "workspaces" not in st: st = {"order": [], "workspaces": {}}
    return st

DEMO = False                                                 # `run --demo`: every name a placeholder, nothing written, nothing reordered — for a screenshot that can be shared
DEMO_WS = "bookkeeping taxes website hiring travel research writing email legal server backup photos music school garden car health reading recipes budget insurance moving newsletter podcast events".split()
def demo_ws(i, label): return label if not DEMO else DEMO_WS[i % len(DEMO_WS)] + ("" if i < len(DEMO_WS) else str(i // len(DEMO_WS)))
def demo_n(uuid): return int(hashlib.sha1(("demo " + uuid).encode()).hexdigest(), 16) % 90 + 10   # a stable two-digit stand-in per session
def save_state(st):
    if DEMO: return
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    out = {"_readme": README, "order": st["order"], "workspaces": st["workspaces"]}
    with open(STATE + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as fh: json.dump(out, fh, indent=1, ensure_ascii=False); fh.write("\n")
        os.replace(tmp, STATE)
    global _saved_at; _saved_at = os.path.getmtime(STATE)
_saved_at = 0.0

# ---------------------------------------------------------------- model
class Grid:
    def __init__(self):
        self.st = load_state(); self.titles = {}; self.lineage = {}; self.tabstore = {}; self.accounts = {}
        self.ws = []; self.tabs = {}; self.agents = {}; self.panes = {}; self.focused = ""
        self.row = 0; self.col = 0; self.want_col = 0; self.grab = False; self.msg = ""
        self.redraw = lambda: None                               # set by main(): paint the status line before a slow step
        self._cursor_saved = None

    def cursor_key(self):
        """The cell under the cursor by identity, not index: uuid first, then tab id, then the row's
        workspace with a column, so a restart finds it again after rows or cells have moved."""
        rows = self.rows(); c = self.cur()
        if not rows or not c: return None
        return {"ws": rows[self.row][0], "uuid": c.get("uuid"), "tab": c.get("tab"), "row": self.row, "col": self.col}

    def save_cursor(self):
        if DEMO: return
        """Idle tick and quit: write the cursor only when it moved. A sidecar file, not the grid file,
        so the hourly backup does not copy the grid for a cursor move."""
        k = self.cursor_key()
        if k is None or k == self._cursor_saved: return
        try:
            with open(CURSOR + ".tmp", "w") as fh: json.dump(k, fh)
            os.replace(CURSOR + ".tmp", CURSOR); self._cursor_saved = k
        except OSError: pass

    def restore_cursor(self):
        """Start: put the cursor back on the cell it was on, by uuid, else tab, else the same column
        in the same workspace row, else the top. Never raises: a stale or missing file means top-left."""
        k = jload(CURSOR, None)
        if not isinstance(k, dict): return
        rows = self.rows()
        for r, (w, e) in enumerate(rows):
            cells = e.get("cells") or []
            for j, c in enumerate(cells):
                if (k.get("uuid") and c.get("uuid") == k["uuid"]) or (k.get("tab") and c.get("tab") == k["tab"]):
                    self.row, self.col = r, j; self.want_col = j; self._cursor_saved = self.cursor_key(); return
        for r, (w, e) in enumerate(rows):
            if w == k.get("ws"):
                self.row = r; self.want_col = self.col = max(0, min(int(k.get("col") or 0), len(e.get("cells") or []) - 1))
                self._cursor_saved = self.cursor_key(); return

    def say(self, msg):
        self.msg = msg; self.redraw()

    def refresh(self):
        try:                                                     # another writer (a script, a second grid) since
            if os.path.getmtime(STATE) > _saved_at: self.st = load_state()   # our last save: take its file, not ours
        except OSError: pass
        self.titles = jload(TITLES, {}); self.lineage = jload(LINEAGE, {})
        self.accounts = jload(ACCOUNTS, {}) or {}                # uuid -> account; absent = personal
        self.tabstore = (jload(TABSTORE, {}) or {}).get("tabs", {}) or {}
        self.ws, self.tabs, self.agents, self.panes, self.focused = snapshot()
        self.reconcile(); save_state(self.st)

    def reconcile(self):
        """Live tabs are truth for what exists; the file is truth for parked cells, order, labels."""
        W = self.st["workspaces"]
        ignored = lambda label: (label or "").lower().startswith(IGNORE_WS)
        for w in [w for w, e in W.items() if ignored(e.get("label"))]: del W[w]   # 0. throwaway rows never persist
        self.ws = [(w, l) for w, l in self.ws if not ignored(l)]
        live_ws = {w: l for w, l in self.ws}
        all_live = {t for ts in self.tabs.values() for t, _ in ts}
        for ent in W.values():                                   # 1. tabs that went away, BEFORE attaching:
            for c in list(ent["cells"]):                         #    a cell mid-move still carries its old id
                if c.get("uuid"): c["key"] = lineage_key(title_of(c["uuid"], self.titles), c["uuid"])  # parked too: a key set by older code stays stale otherwise
                if c.get("tab") and c["tab"] not in all_live:
                    if c.get("uuid"): c["tab"] = None            # parked
                    else: ent["cells"].remove(c)                 # a shell has nothing to bring back
        def twin(uuid, key):                                     # a parked cell of this session or its lineage, anywhere
            for w2, e2 in W.items():
                for c in e2["cells"]:
                    if c.get("tab") is None and (c.get("uuid") == uuid or (key and c.get("key") == key)): return w2, c
            return None, None
        for w, label in self.ws:                                 # 2. every live tab has a cell
            ent = W.setdefault(w, {"label": label, "cells": []}); ent["label"] = label
            cells = ent["cells"]
            for tab, tlabel in self.tabs.get(w, []):
                uuid, _ = self.agents.get(tab, ("", ""))
                if not uuid:                                     # no agent: the tab store says what it last held
                    row = self.tabstore.get(tab) or {}
                    uuid = row.get("uuid") or "" if isinstance(row, dict) else ""
                key = lineage_key(title_of(uuid, self.titles), uuid) if uuid else ""
                cell = next((c for c in cells if c.get("tab") == tab), None)
                if cell is None:                                 # a parked twin is absorbed in step 3
                    cell = {"key": "", "uuid": "", "label": "", "tab": tab}; cells.append(cell)
                cell["tab"] = tab
                if tlabel: cell["label"] = tlabel
                if uuid: cell["uuid"], cell["key"] = uuid, key
        for w, ent in W.items():                                 # 3. one cell per lineage: a live cell absorbs any
            for c in list(ent["cells"]):                         #    parked twin, taking its slot when in the same row
                if not (c.get("tab") and c.get("uuid")): continue
                w2, p = twin(c["uuid"], c.get("key"))
                if not p: continue
                if not c.get("label"): c["label"] = p.get("label", "")
                if p.get("mark"): c["mark"] = True
                if w2 == w:
                    idx = ent["cells"].index(p); ent["cells"].remove(p); ent["cells"].remove(c)
                    ent["cells"].insert(min(idx, len(ent["cells"])), c)
                else: W[w2]["cells"].remove(p)
        for w in list(W):                                        # 4. workspaces herdr no longer has
            if w not in live_ws and not any(c.get("uuid") for c in W[w]["cells"]): del W[w]
        order = [w for w in self.st["order"] if w in W] + [w for w, _ in self.ws if w not in self.st["order"]]
        order += [w for w in W if w not in order]
        self.st["order"] = order
        self.clamp()

    # ------------------------------------------------------------ navigation
    def rows(self): return [(w, self.st["workspaces"][w]) for w in self.st["order"]]
    def cells(self, r): return self.rows()[r][1]["cells"] if self.rows() else []
    def clamp(self):
        n = len(self.rows()); self.row = max(0, min(self.row, n - 1))
        m = len(self.cells(self.row)) if n else 0
        self.col = max(0, min(self.want_col if not self.grab else self.col, m - 1))
    def cur(self):
        c = self.cells(self.row); return c[self.col] if c and 0 <= self.col < len(c) else None

    def move(self, dr, dc):
        if self.grab: return self.drag(dr, dc)
        if dc: self.col = max(0, min(self.col + dc, len(self.cells(self.row)) - 1)); self.want_col = self.col
        if dr: self.row = max(0, min(self.row + dr, len(self.rows()) - 1))
        self.clamp()

    def drag(self, dr, dc):
        rows = self.rows(); cells = self.cells(self.row); c = self.cur()
        if c is None: return
        if dc:
            j = self.col + dc
            if 0 <= j < len(cells): cells[self.col], cells[j] = cells[j], cells[self.col]; self.col = j
        elif dr:
            i = self.row + dr
            if 0 <= i < len(rows):
                cells.remove(c); dest = rows[i][1]["cells"]; j = min(self.col, len(dest)); dest.insert(j, c)
                self.row, self.col = i, j
        self.want_col = self.col

    # ------------------------------------------------------------ status
    def status(self, c):
        if c is None: return "empty"
        if c.get("tab") is None: return "parked"
        if not c.get("uuid"): return "shell"
        if c["tab"] not in self.agents: return "stopped"         # tab open, session not running
        return self.agents[c["tab"]][1]

    def marked(self, c):
        """Bookmarked: the cell's own flag, or the picker's ⭐ title marker (Alt-' in ch-pick) on its session."""
        if not c or not c.get("uuid"): return False
        if c.get("mark"): return True
        m = SPLIT.match(title_of(c["uuid"], self.titles) or ""); return bool(m and (m.group(3) or "").startswith("⭐"))

    def glyph(self, c):
        gch, cp = {"working": ("●", 2), "done": ("●", 1), "idle": ("○", 1), "blocked": ("●", 3),   # herdr's sidebar: yellow = working, green filled = done and not yet looked at, green hollow = looked at since; both greens wait for input
                   "parked": ("P", 4), "stopped": ("◌", 4), "shell": ("·", 5)}.get(self.status(c), ("○", 5))
        if not self.marked(c): return (gch, cp)
        if gch == "P": cp = 11                                 # a parked star in a lighter blue than the P's around it, so the bookmarks stand out across a mostly-parked row
        return ("☆" if gch == "○" else "★", cp)               # a bookmark keeps the status COLOR and the fill: hollow dot, hollow star

    def toggle_mark(self):
        c = self.cur()
        if not c or not c.get("uuid"): self.msg = "bookmarks go on sessions, not shells"; return
        if not c.get("mark") and self.marked(c): self.msg = "starred in its title — clear that with Alt-' in ch-pick"; return
        c["mark"] = not c.get("mark"); save_state(self.st)
        self.msg = ("bookmarked " if c["mark"] else "unbookmarked ") + (c.get("label") or c["uuid"][:8])

    def next_marked(self):
        """Tab: jump to the next bookmarked cell in row-major order, wrapping; stays put if there is none."""
        rows = self.rows(); seq = [(r, j) for r, (_, e) in enumerate(rows) for j in range(len(e["cells"]))]
        if not seq: return
        start = seq.index((self.row, self.col)) if (self.row, self.col) in seq else -1
        for k in range(1, len(seq) + 1):
            r, j = seq[(start + k) % len(seq)]
            if self.marked(rows[r][1]["cells"][j]): self.row, self.col, self.want_col = r, j, j; return
        self.msg = "no bookmarks — b sets one"

    # ------------------------------------------------------------ actions
    def order_pending(self):
        """Workspaces whose tab order differs from the grid, plus 1 if the row order differs."""
        n = sum(1 for w, e in self.rows() if [c["tab"] for c in e["cells"] if c.get("tab")] != [t for t, _ in self.tabs.get(w, [])])
        live = {w for w, _ in self.ws}
        return n + (1 if [w for w in self.st["order"] if w in live] != [w for w, _ in self.ws] else 0)

    def apply_order(self, only_ws=None):
        """The grid's order prevails: reorder herdr's live tabs in each workspace, and the workspaces
        themselves, to match the grid. Every move goes to an EARLIER index, which sidesteps herdr's
        count-before-removal indexing (cli-herdr.md)."""
        moved = 0
        if only_ws is None:
            want = [w for w in self.st["order"] if w in {x for x, _ in self.ws}]; have = [w for w, _ in self.ws]
            for i, w in enumerate(want):
                if have[i] != w:
                    r = sock("workspace.move", {"workspace_id": w, "insert_index": i})
                    if "error" in r: self.msg = f"workspace.move {w}: {r['error'].get('message')}"; return moved
                    have.remove(w); have.insert(i, w); moved += 1
        for w, ent in self.rows():
            if only_ws and w != only_ws: continue
            want = [c["tab"] for c in ent["cells"] if c.get("tab")]
            have = [t for t, _ in self.tabs.get(w, [])]
            if want == have or len(want) != len(have): continue
            for i, tab in enumerate(want):
                if have[i] != tab:
                    r = sock("tab.move", {"tab_id": tab, "insert_index": i})
                    if "error" in r: self.msg = f"tab.move {tab}: {r['error'].get('message')}"; return moved
                    have.remove(tab); have.insert(i, tab); moved += 1
        return moved

    def goto(self, tab):
        subprocess.run([MANAGE, "goto", tab], capture_output=True, text=True, timeout=30)

    def open(self):
        c = self.cur(); w = self.rows()[self.row][0]
        if c is None: return
        if c.get("tab") and self.status(c) != "stopped": self.goto(c["tab"]); return
        uuid = newest_member(c["uuid"], self.titles)
        if c.get("tab"):                                         # stopped: resume in the tab it already has
            pane = self.panes.get(c["tab"], "")
            self.say(f"resuming {uuid[:8]} in {pane} — 10-30 s …")
            r = subprocess.run([MANAGE, "resume", uuid, "--pane", pane], capture_output=True, text=True, timeout=180)
            if r.returncode != 0: self.msg = last_line(r.stdout + r.stderr); return
            c["uuid"] = uuid; self.refresh(); self.msg = f"resumed {c.get('label') or pane} — Enter again to go there"; return
        label = c.get("label") or (lineage_key(title_of(uuid, self.titles), uuid) or "session")   # full description; herdr shortens it on screen
        root = ""
        if w not in {x for x, _ in self.ws}:                     # its workspace was closed: rebuild it, same label,
            w, root, root_tab = self.recreate_workspace(w)       # same position, and resume into its root pane
            if not w: return
        self.say(f"resuming {uuid[:8]} into {w} — 10-30 s …")
        args = ["--pane", root] if root else ["--workspace", w, "--label", label, "--new-tab"]
        r = subprocess.run([MANAGE, "resume", uuid, *args], capture_output=True, text=True, timeout=180)
        if r.returncode != 0: self.msg = last_line(r.stdout + r.stderr); return
        if root: herdr("tab", "rename", root_tab, label)
        c["uuid"] = uuid
        self.refresh()                                           # reconcile re-attaches the parked cell, in its own slot
        self.apply_order(w); save_state(self.st)
        self.msg = f"resumed {label} — Enter again to go there"   # deliberately no goto: wake several, then jump

    def new_session(self, scr):
        """`n`: start a new Claude session in the selected row's workspace. Tab label required; a few words
        optional, sent as the first prompt through SEED. Account follows the cell under the cursor (the
        neighbour is usually the same kind of work), model and effort are the defaults, and the cursor
        stays in the grid on the new cell — like waking a parked one, Enter goes there."""
        rows = self.rows()
        if not rows: return
        w, ent = rows[self.row]
        if ent.get("label") == GRID_WS: self.msg = "n: pick a row other than the grid's own"; return
        label = prompt(scr, "new session — tab label:")
        if not label: self.msg = "cancelled — a tab label is required"; return
        words = prompt(scr, "a few words for its first prompt (Enter for none):")
        c = self.cur(); acct = self.accounts.get(c.get("uuid") or "", "") if c else ""
        root_tab = ""
        if w not in {x for x, _ in self.ws}:                     # a closed workspace: rebuild it at its row, same label
            w, _root, root_tab = self.recreate_workspace(w)
            if not w: return
        self.say(f"starting {label} in {ent['label']}{' as ' + acct if acct else ''} — 10-30 s …")
        args = [MANAGE, "start", label, "--workspace", w] + (["--account", acct] if acct else [])
        try: r = subprocess.run(args, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired: self.msg = "start timed out after 180 s"; return
        if r.returncode != 0:
            self.msg = last_line(r.stdout + r.stderr)
            tabs = [t.get("tab_id") for t in herdr("tab", "list").get("result", {}).get("tabs", []) if t.get("workspace_id") == w]
            if root_tab and any(t != root_tab for t in tabs): herdr("tab", "close", root_tab)   # the manager made its tab before failing (2026-09-18: the session was up, the report was not); the empty first tab must not outlive that
            return
        m = re.search(r"uuid=(\S+)", r.stdout); uuid = m.group(1) if m else ""
        if root_tab: herdr("tab", "close", root_tab)              # the rebuilt workspace's empty first tab
        note = ""
        if words and uuid:
            self.say(f"started {label} — sending its first prompt, 10-30 s …")
            try: t = subprocess.run([MANAGE, "tell", uuid, SEED.format(words=words)], capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired: t = None
            if t is None or t.returncode != 0: note = " — the first prompt was NOT delivered; open the tab and type it"
        self.refresh(); self.apply_order(w); save_state(self.st)
        for j, cc in enumerate(self.cells(self.row)):            # cursor onto the new cell
            if uuid and cc.get("uuid") == uuid: self.col = self.want_col = j
        self.msg = f"started {label} — Enter to go there{note}"

    def recreate_workspace(self, old):
        """A closed workspace with parked cells keeps its row; opening one of them brings the workspace back
        under a NEW id at the row's stored position, and the row is re-keyed to that id."""
        ent = self.st["workspaces"][old]
        out = herdr("workspace", "create", "--label", ent["label"], "--no-focus").get("result", {})
        new = (out.get("workspace") or {}).get("workspace_id")
        if not new: self.msg = f"could not recreate workspace {ent['label']}"; return "", "", ""
        root = out.get("root_pane") or {}                        # a new workspace arrives WITH one tab; use it
        pos = self.st["order"].index(old)
        live_ids = {x for x, _ in self.ws}                       # herdr's index skips rows whose workspace is closed
        hpos = sum(1 for x in self.st["order"][:pos] if x in live_ids)
        self.st["workspaces"][new] = self.st["workspaces"].pop(old)
        self.st["order"][pos] = new
        time.sleep(1); self.ws, self.tabs, self.agents, self.panes, self.focused = snapshot()
        if [x for x, _ in self.ws].index(new) != hpos:           # it lands last; move it back (backward move, index exact)
            sock("workspace.move", {"workspace_id": new, "insert_index": hpos})
        save_state(self.st); return new, root.get("pane_id", ""), root.get("tab_id", "")

    def park(self):
        c = self.cur()
        if not c or not c.get("tab"): self.msg = "nothing live to park"; return
        pane = self.panes.get(c["tab"], "")
        if c.get("uuid") and pane and self.status(c) != "stopped":
            self.say(f"parking {c.get('label') or c['tab']} — quitting its session …")
            r = subprocess.run([MANAGE, "quit", pane], capture_output=True, text=True, timeout=120)
            if r.returncode != 0: self.msg = last_line(r.stdout + r.stderr); return
        herdr("tab", "close", c["tab"]); self.msg = f"parked {c.get('label') or c['tab']}"
        self.refresh()

    def forget(self):
        c = self.cur()
        if not c or c.get("tab") is not None: self.msg = "forget works on a parked cell only"; return
        self.cells(self.row).remove(c); save_state(self.st); self.clamp(); self.msg = "forgotten (transcript untouched)"

    def rename(self, label):
        c = self.cur()
        if not c or not label: return
        c["label"] = label
        if c.get("tab"): herdr("tab", "rename", c["tab"], label)
        save_state(self.st)

    def drop(self):
        """End a grab: persist, then push the order onto herdr (a cross-workspace drag moves the pane)."""
        self.grab = False; c = self.cur(); w = self.rows()[self.row][0]
        if c and c.get("tab") and not c["tab"].startswith(w + ":"):
            subprocess.run([MANAGE, "move", c["tab"], w], capture_output=True, text=True, timeout=120)
            for _ in range(20):                                  # herdr re-registers the moved agent a beat later
                self.ws, self.tabs, self.agents, self.panes, self.focused = snapshot()
                if any(t.startswith(w + ":") and u == c["uuid"] for t, (u, _) in self.agents.items()): break
                time.sleep(0.5)
            self.refresh()
        save_state(self.st); n = self.apply_order(); self.refresh(); self.msg = f"dropped; {n} herdr tab(s) moved"

# ---------------------------------------------------------------- view
def cols(s):
    """Display width: an emoji or CJK glyph takes two columns, which len() counts as one."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 0 if unicodedata.combining(ch) else 1 for ch in s)

def rpad(s, w):
    """Right-align to w display columns, truncating from the right when the label is wider."""
    while cols(s) > w: s = s[:-1]
    return " " * (w - cols(s)) + s

def when(ts):
    """'3d ago · 12-Sep 14:31' — elapsed first; hh:mm only inside a week; the year only when it is not this one."""
    now = time.time(); d = max(0, now - ts)
    ago = (f"{int(d/60)}m" if d < 3600 else f"{int(d/3600)}h" if d < 86400 else f"{int(d/86400)}d" if d < 7*86400
           else f"{int(d/(7*86400))}w" if d < 60*86400 else f"{int(d/(30*86400))}mo")
    lt = time.localtime(ts); date = time.strftime("%d-%b", lt).lstrip("0")
    if lt.tm_year != time.localtime(now).tm_year: date += time.strftime("-%Y", lt)
    if d < 7*86400: date += time.strftime(" %H:%M", lt)
    return f"{ago} ago · {date}"

def wrap(s, w):
    out, line = [], ""
    for word in s.split():                                       # cols(), not len(): an emoji is two columns
        if cols(line) + cols(word) + 1 > w: out.append(line); line = word
        else: line = (line + " " + word).strip()
    return out + ([line] if line else [])

def detail_lines(g, c, width):
    """(text, kind) per line. Kinds map to colours in draw(), with the statusline's meanings:
    label bold · title plain · status in the cell's status colour · size blue (the statusline's
    session-size blue) · time and done-when dim · directive plain."""
    if c is None: return [("(no cells in this workspace)", "dim")]
    uuid = c.get("uuid", ""); t = title_of(uuid, g.titles) if uuid else ""
    lin = g.lineage.get(uuid, {}) if uuid else {}
    st = g.status(c); pane = g.panes.get(c.get("tab") or "", "")
    if DEMO and uuid:                                             # placeholders that keep the shape of the real thing
        n = demo_n(uuid); c = dict(c, label=f"task {n}"); t = f"20260406 Example session doing one job {n:02d}"; uuid = hashlib.sha1(("demo " + uuid).encode()).hexdigest()
        lin = {"prime_directive": "Example prime directive: one sentence saying what this lineage of sessions is for.", "done_when": "an example of when it is finished"}
    lines = [(c.get("label") or (c.get("tab") or "(unlabelled)"), "label"), ("", "")]
    if uuid:
        lines += [(l, "title") for l in wrap(t or uuid, width)]  # the full title, wrapped — never cut at the panel edge
        segs = [(f"{st}{' (tab open, session not running)' if st == 'stopped' else ''}", "status"), (f" · {pane or 'no tab'}", "title"), (f" · {uuid[:8]}", "sid"),
                (f" · {'personal' if DEMO else g.accounts.get(uuid) or 'personal'}", "title")]   # the account, as the statusline's marker names it (John 2026-09-17)
        if g.marked(c): segs.append((" · ★ bookmarked", "gold"))
        lines.append((segs, "segments"))                          # status in its colour, pane plain, id dim — like the statusline
        p = transcript(c.get("uuid", "")) if uuid else None
        if p:
            sz = os.path.getsize(p) / 1048576; segs = [(f"{sz:.1f} MB", "size")]   # the statusline's cornflower blue
            n = ctx_of(p)
            if n is not None: segs += [(" · Cx ", "dim"), (fmt_tokens(n), ctx_kind(n))]
            lines += [(segs, "segments"), (f"last activity {when(os.path.getmtime(p))}", "dim")]
        pdv = lin.get("prime_directive") or ""; dw = lin.get("done_when") or ""
        lines.append(("", ""))
        lines += [(l, "body") for l in wrap(pdv, width)] if pdv else [("(no prime directive)", "dim")]
        if dw: lines += [("", "")] + [(l, "dim") for l in wrap("done when: " + dw, width)]
    else:
        lines += [("shell tab, no session", "dim"), (f"{c.get('tab')} · {pane}", "dim")]
    return lines

def draw(scr, g):
    scr.erase(); H, W = scr.getmaxyx()
    legend = [(" ● working", 2), ("  ● done", 1), ("  ○ idle", 1), ("  ● blocked", 3), ("  ◌ stopped", 4), ("  P parked", 4), ("  · shell", 5), ("  ★☆ bookmarked", 5)]
    keys = "  ⏎ open  n new  b bookmark  ⇥ next ★  p park  x forget  m grab  ↑↓←→ move  r rename  = push order  R refresh  q quit"
    F = 3 if sum(len(t) for t, _ in legend) + len(keys) <= W - 1 else 4   # footer lines: rule + legend(+keys) + message; keys drop to their own line when the pane is narrow (John 2026-09-17)
    live = sum(1 for _, e in g.rows() for c in e["cells"] if c.get("tab") and c.get("uuid"))
    parked = sum(1 for _, e in g.rows() for c in e["cells"] if c.get("tab") is None)
    marked = sum(1 for _, e in g.rows() for c in e["cells"] if g.marked(c))
    head = f" herdr grid   {live} live · {parked} parked" + (f" · {marked} ★" if marked else "") + ("   MOVE" if g.grab else "")
    scr.addnstr(0, 0, head.ljust(W), W - 1, curses.A_BOLD | (curses.color_pair(3) if g.grab else 0))
    labw = min(max([cols(demo_ws(i, e["label"])) for i, (_, e) in enumerate(g.rows())] + [4]), 15)
    gridw = labw + 3 + max([len(e["cells"]) for _, e in g.rows()] + [1]) * 2 + 2
    detx = min(gridw, W // 2); detw = W - detx - 2
    for i, (w, ent) in enumerate(g.rows()):
        y = 2 + i
        if y >= H - F: break
        scr.addstr(y, 0, rpad(demo_ws(i, ent["label"]), labw) + ": ")
        for j, c in enumerate(ent["cells"]):
            gch, cp = g.glyph(c); attr = curses.color_pair(cp)
            if i == g.row and j == g.col: attr |= curses.A_REVERSE
            if c.get("tab") == g.focused: attr |= curses.A_UNDERLINE
            x = labw + 2 + j * 2
            if x + 1 < detx: scr.addstr(y, x, gch, attr)
    cur = g.cur(); _, scp = g.glyph(cur) if cur else ("", 5)
    kind_attr = {"label": curses.A_BOLD, "title": 0, "status": curses.color_pair(scp), "size": curses.color_pair(6),
                 "sid": curses.A_DIM, "ctx_ok": curses.color_pair(7), "ctx_warn": curses.color_pair(8), "ctx_crit": curses.color_pair(9), "gold": curses.color_pair(10),
                 "dim": curses.A_DIM, "body": 0, "": 0}
    for k, (line, kind) in enumerate(detail_lines(g, cur, detw)):
        if 2 + k >= H - F: break
        if kind == "segments":                                   # one line, several colours; cut at the panel edge
            x = detx + 1
            for text, kd in line:
                room = detx + 1 + detw - x
                if room <= 0: break
                scr.addnstr(2 + k, x, text, room, kind_attr.get(kd, 0)); x += min(cols(text), room)
        else: scr.addnstr(2 + k, detx + 1, line, detw, kind_attr.get(kind, 0))
    if detx < W - 1:
        for y in range(1, H - F): scr.addstr(y, detx, "│")          # the same unicode bar as the rule below, not ACS_VLINE: a capture that lacks the DEC charset drew that as "x"
    scr.addnstr(H - F, 0, "─" * (W - 1), W - 1)
    x = 0
    for txt, cp in legend:
        scr.addnstr(H - F + 1, x, txt, W - 1 - x, curses.color_pair(cp)); x += len(txt)
    if F == 3: scr.addnstr(H - 2, x, keys, max(0, W - 1 - x))
    else: scr.addnstr(H - 2, 0, keys.lstrip(), W - 1)
    scr.addnstr(H - 1, 0, (" " + " ".join(g.msg.split())).ljust(W - 1), W - 1, curses.A_DIM)   # collapsed: a newline here returns ERR and would kill the grid
    scr.refresh()

def prompt(scr, label, initial=""):
    """One-line editor on the bottom row, prefilled with `initial` and the cursor at its end, so a
    rename starts from the current label instead of a blank (John 2026-09-16). Backspace, ←/→,
    Home/End, ctrl-U (clear) and ctrl-A/E work; Enter accepts, Esc cancels and returns ""."""
    H, W = scr.getmaxyx(); buf = list(initial); pos = len(buf); x0 = len(label) + 2
    curses.curs_set(1); scr.timeout(-1)                          # the grid's 3 s idle tick would abort the edit
    try:
        while True:
            room = max(1, W - 1 - x0); start = 0 if pos < room else pos - room + 1   # a long dictation scrolls; it never ran off the row before (John 2026-09-18)
            scr.addnstr(H - 1, 0, (" " + label + " " + "".join(buf[start:start + room])).ljust(W - 1), W - 1)
            scr.move(H - 1, min(x0 + pos - start, W - 2)); scr.refresh(); k = scr.get_wch()
            if k in ("\n", "\r", curses.KEY_ENTER, 10, 13): return "".join(buf).strip()
            if k == "\x1b": return ""
            if k in (curses.KEY_BACKSPACE, "\x7f", "\b", 127, 8):
                if pos: del buf[pos - 1]; pos -= 1
            elif k == curses.KEY_DC:
                if pos < len(buf): del buf[pos]
            elif k == curses.KEY_LEFT: pos = max(0, pos - 1)
            elif k == curses.KEY_RIGHT: pos = min(len(buf), pos + 1)
            elif k in (curses.KEY_HOME, "\x01"): pos = 0
            elif k in (curses.KEY_END, "\x05"): pos = len(buf)
            elif k == "\x15": buf = []; pos = 0
            elif isinstance(k, str) and k.isprintable(): buf.insert(pos, k); pos += 1
    except Exception: return ""
    finally: curses.curs_set(0); scr.timeout(3000)

HELP = [
    ("↑ ↓ ← →", "move between cells; up/down remembers the column you came from"),
    ("Enter",   "live cell: go to its tab. Stopped (◌) or parked (P): bring the session back, stay in the grid — Enter again to go. Wake several, then jump"),
    ("p",       "park: quit the session and close its tab — the cell stays as P   (asks first)"),
    ("x",       "forget: remove a PARKED cell from the grid; the transcript is untouched   (asks first)"),
    ("m",       "grab the cell; arrows carry it along the row or into another workspace; m or Enter drops, Esc cancels"),
    ("r",       "rename the herdr tab label (the current label is loaded to edit; Esc keeps it)"),
    ("n",       "new session in this row's workspace: tab label (required), then a few words for its first prompt (optional). Account follows the cell under the cursor; the session drafts its title and directive from your words and waits for your approval"),
    ("b",       "bookmark the cell (★ in place of its dot, ☆ when the dot is hollow, status color kept) — or remove the bookmark. Stored in the grid's own file, so it survives parks and successors; a ⭐ in the session's title (ch-pick Alt-') shows the same way"),
    ("Tab",     "jump to the next bookmarked cell, wrapping"),
    ("=",       "push the grid's order onto herdr — tabs within rows AND the row order of the sidebar   (asks first when it would move any)"),
    ("R",       "refresh now (it also refreshes every 3 s)"),
    ("?",       "this list"),
    ("q",       "quit the grid (Esc only cancels a grab)"),
    ("hg go",   "from any pane: go to the grid workspace, creating or relaunching it as needed"),
]

def help_screen(scr):
    scr.erase(); H, W = scr.getmaxyx()
    scr.addnstr(0, 0, " herdr grid — keys", W - 1, curses.A_BOLD)
    for i, (k, text) in enumerate(HELP):
        if 2 + i >= H - 1: break
        scr.addnstr(2 + i, 2, f"{k:<9} {text}", W - 3)
    scr.addnstr(H - 1, 0, " any key to return", W - 1, curses.A_DIM); scr.refresh(); scr.timeout(-1); scr.getch(); scr.timeout(3000)

def confirm(scr, text):
    H, W = scr.getmaxyx(); scr.addnstr(H - 1, 0, (" " + text + "  y/n").ljust(W - 1), W - 1, curses.A_BOLD); scr.refresh()
    scr.timeout(-1); k = scr.getch(); scr.timeout(3000)
    return k in (ord("y"), ord("Y"))

def herdr_session_name():
    """The herdr session this socket belongs to — the grid represents exactly one, so its tab is named for it."""
    for x in herdr("session", "list", "--json").get("sessions", []):
        if x.get("socket_path") == SOCK or x.get("running") and x.get("default"): return x.get("name") or "default"
    return "default"

def go():
    """Create the grid workspace at position 0 with one tab named for the herdr session, launch the grid in it if needed, focus it."""
    ws, tabs, agents, panes, _ = snapshot(); name = herdr_session_name()
    gw = next((w for w, l in ws if l == GRID_WS), None)
    if gw is None:
        out = herdr("workspace", "create", "--label", GRID_WS, "--no-focus").get("result", {})
        gw = out["workspace"]["workspace_id"]; tab = out["root_pane"]["tab_id"]; pane = out["root_pane"]["pane_id"]
        herdr("tab", "rename", tab, name); time.sleep(1.5)
        ws, tabs, agents, panes, _ = snapshot()
    else:
        tab = tabs[gw][0][0]; pane = panes[tab]
    if ws and ws[0][0] != gw: sock("workspace.move", {"workspace_id": gw, "insert_index": 0})
    pi = herdr("pane", "process-info", "--pane", pane).get("result", {}).get("process_info")
    # Type into the pane ONLY when it is provably at a bare shell: the foreground list holds the
    # shell itself and nothing else. Anything more — the grid, or something else — gets nothing
    # typed into it, because text sent into the running grid lands on its keys; an unreadable
    # process list is treated the same way.
    fg = [p for p in ((pi or {}).get("foreground_processes") or []) if p.get("pid") != (pi or {}).get("shell_pid")]
    if pi is None: state = "unreadable — nothing sent"
    elif fg: state = "already running" if any("herdr_session_grid.py" in (p.get("cmdline") or "") for p in fg) else "busy — nothing sent"
    else:
        herdr("pane", "send-text", pane, "herdr_session_grid.py run"); herdr("pane", "send-keys", pane, "Enter"); state = "launched"
        for _ in range(20):                                      # a second go inside this window must see it running
            fg2 = (herdr("pane", "process-info", "--pane", pane).get("result", {}).get("process_info") or {}).get("foreground_processes") or []
            if any("herdr_session_grid.py" in (p.get("cmdline") or "") for p in fg2): break
            time.sleep(0.25)
    subprocess.run([MANAGE, "goto", tab], capture_output=True, text=True, timeout=30)
    print(f"grid: {state} in its own workspace {gw} ({GRID_WS}), tab {tab}, pane {pane} — not the caller's workspace")   # a peer read the old line as "you are here" and parked a session in the grid's workspace (2026-09-17)

def main(scr):
    curses.curs_set(0); curses.use_default_colors()
    for i, col in enumerate([curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_BLUE, -1], 1):
        curses.init_pair(i, col, -1)
    # 6 cornflower (statusline BLUE 137,180,250 → xterm 111), 7/8/9 the statusline's context tiers
    # (166,227,161 → 151 · 249,226,175 → 223 · 243,139,168 → 211). Under 256 colors: blue, green, yellow, red.
    hi = curses.COLORS >= 256
    for i, (c256, basic) in enumerate([(111, curses.COLOR_BLUE), (151, curses.COLOR_GREEN), (223, curses.COLOR_YELLOW), (211, curses.COLOR_RED), (220, curses.COLOR_YELLOW)], 6):   # 10 = gold, the picker's ⭐
        curses.init_pair(i, c256 if hi else basic, -1)
    curses.init_pair(2, 226 if hi else curses.COLOR_YELLOW, -1)   # working: pure yellow (xterm 226) — the terminal's basic yellow read dull on John's screen
    curses.init_pair(11, 123 if hi else curses.COLOR_CYAN, -1)  # bookmarked parked cell's ★: xterm 123 — John's pick after seeing 51, 81 and 45 live on the grid
    curses.init_pair(4, 111 if hi else curses.COLOR_BLUE, -1)   # parked/stopped: the statusline's cornflower (xterm 111), the same pair the panel uses for size — John picked it from a six-colour field test against the green ★ (2026-09-16)
    curses.init_pair(1, 46 if hi else curses.COLOR_GREEN, -1)   # waiting for input (done/idle): pure green (xterm 46) — John's pick from an eight-green field test against the 111 stars; the theme's mint green blurred into them (2026-09-16)
    scr.timeout(3000)
    g = Grid(); g.redraw = lambda: draw(scr, g); g.refresh(); DEMO or g.apply_order(); g.restore_cursor(); last = time.time()
    while True:
        try: draw(scr, g)
        except curses.error as e: scr.erase(); scr.addnstr(0, 0, "draw failed: " + " ".join(str(e).split()), 100); scr.refresh()   # never exit over one bad cell; the next tick redraws
        k = scr.getch()
        if k == -1:
            if time.time() - last >= 3: g.refresh(); g.save_cursor(); last = time.time()
            continue
        if k == ord("q") and not g.grab: g.save_cursor(); break  # only q quits — Esc is a cancel, never an exit
        if k == 27:
            if g.grab: g.grab = False; g.refresh(); g.msg = "move cancelled"
            continue
        if k == curses.KEY_UP: g.move(-1, 0)
        elif k == curses.KEY_DOWN: g.move(1, 0)
        elif k == curses.KEY_LEFT: g.move(0, -1)
        elif k == curses.KEY_RIGHT: g.move(0, 1)
        elif k in (10, 13, curses.KEY_ENTER): g.drop() if g.grab else g.open(); curses.flushinp()   # keys typed during a 10-30 s wait would replay into the grid (q quits, p parks)
        elif k == ord("m"): g.drop() if g.grab else setattr(g, "grab", True)
        elif k == ord("p"):
            c = g.cur()
            if c and c.get("tab") and confirm(scr, f"park '{c.get('label') or c['tab']}' — quit its session (any background work stops) and close the tab?"): g.park(); curses.flushinp()
            elif c and not c.get("tab"): g.msg = "already parked"
        elif k == ord("x"):
            c = g.cur()
            if c and c.get("tab") is None and confirm(scr, f"forget '{c.get('label') or c.get('uuid','')[:8]}' — drop it from the grid?"): g.forget()
            elif c: g.msg = "forget works on a parked cell only"
        elif k == ord("r"): g.rename(prompt(scr, "new tab label:", (g.cur() or {}).get("label") or ""))
        elif k == ord("b"): g.toggle_mark()
        elif k == ord("n"): g.new_session(scr); curses.flushinp()
        elif k == 9: g.next_marked()                              # Tab
        elif k == ord("="):
            pending = g.order_pending()
            if pending == 0: g.msg = "herdr already matches the grid"
            elif confirm(scr, f"reorder herdr ({pending} workspace(s) or the sidebar) to match the grid?"):
                n = g.apply_order(); g.refresh(); g.msg = f"{n} herdr move(s) to match the grid"
        elif k == ord("R"): g.refresh(); g.msg = "refreshed"
        elif k == ord("?"): help_screen(scr)

if __name__ == "__main__":
    locale.setlocale(locale.LC_ALL, "")
    if not os.path.exists(SOCK): sys.exit("herdr is not running (no socket)")
    # Bare `hg` GOES to the grid (John 2026-09-17: "why can't hg just take me to the grid"); `run` draws it in
    # THIS pane and is what go() types into the grid's own pane — a launcher detail, not a verb he needs.
    if len(sys.argv) == 1 or sys.argv[1] == "go": go(); sys.exit(0)
    if sys.argv[1] != "run": sys.exit(__doc__)
    DEMO = "--demo" in sys.argv[2:]
    curses.wrapper(main)
