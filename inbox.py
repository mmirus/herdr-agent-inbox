"""Inbox view for herdr's Agents sidebar.

Inbox membership lives in a pane metadata token named `inbox` (`settled` or
`snoozed`); the inbox view hides agents carrying either value. Herdr drops
metadata tokens and agent views when its server restarts, so the same facts are
kept in STATE_DIR/state.json and reapplied by the `restore` startup hook.
Every herdr session runs the plugin with the same HERDR_PLUGIN_STATE_DIR, while
views, tokens, and pane IDs belong to one server, so STATE_DIR is a
subdirectory per server socket.

Sidebar rows can only style a token by that token's own value, so each agent's
workspace name is reported twice over: `ws_ready` while the agent is not
working and `ws_busy` while it is. A row renders both, styles `$ws_busy` muted,
and whichever is unset disappears. `inbox_rank` (`0` not working, `1` working) is
the inbox view's first sort key.

Herdr runs no long-lived plugin process, so the check that settles agents whose
branch's pull request has merged runs from event hooks and the startup hook,
at most once per MERGE_CHECK_INTERVAL.

A snooze ends when its `inbox` token's TTL expires. Herdr caps TTLs at 24 hours,
so a longer snooze starts without a TTL; hooks call sweep_snoozes, which gives it
a TTL once it is within 24 hours of waking, or wakes it if no hook ran in time.
"""

import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import termios
import time
import tty
from contextlib import contextmanager
from datetime import datetime, timedelta

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "agent-inbox")
SOURCE = f"plugin:{PLUGIN_ID}"
TOKEN = "inbox"
SOCKET_PATH = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
STATE_DIR = os.path.join(
    os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser("~/.local/state/herdr-inbox"),
    "servers",
    hashlib.sha256(os.path.realpath(SOCKET_PATH).encode()).hexdigest()[:16],
)
STATE_FILE = os.path.join(STATE_DIR, "state.json")
MAX_TTL_MS = 86_400_000  # herdr's ttl_ms ceiling
MERGE_CHECK_INTERVAL = 120  # seconds

def at_nine(day):
    return day.replace(hour=9, minute=0, second=0, microsecond=0)


def next_monday(now):
    return at_nine(now + timedelta(days=7 - now.weekday()))


SNOOZE_CHOICES = [
    ("1", "15 minutes", lambda now: now + timedelta(minutes=15)),
    ("2", "1 hour", lambda now: now + timedelta(hours=1)),
    ("3", "3 hours", lambda now: now + timedelta(hours=3)),
    ("4", "Tomorrow 9:00", lambda now: at_nine(now + timedelta(days=1))),
    ("5", "Monday 9:00", next_monday),
    ("6", "1 week", lambda now: now + timedelta(weeks=1)),
]

INBOX_VIEW = {
    "source": SOURCE,
    "label": "inbox",
    "filter": {
        "op": "not",
        "filter": {"op": "in", "field": {"token": TOKEN}, "values": ["settled", "snoozed"]},
    },
    "sort": [
        {"field": {"token": "inbox_rank"}, "order": "asc"},
        {"field": "attention", "order": "desc"},
        {"field": "state_change_seq", "order": "desc"},
    ],
}


class HerdrError(RuntimeError):
    pass


def request(method, params):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(SOCKET_PATH)
        payload = {"id": f"{PLUGIN_ID}:{method}", "method": method, "params": params}
        sock.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    response = json.loads(buf)
    if "error" in response:
        raise HerdrError(f"{method}: {response['error']}")
    return response.get("result")


@contextmanager
def state():
    """Yields the state dict under an exclusive lock and writes it back on exit.

    Event hooks run as separate processes and can overlap, so every
    read-modify-write of state.json holds the lock.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "state.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data["socket"] = SOCKET_PATH
        data.setdefault("view_active", False)
        data.setdefault("panes", {})
        data.setdefault("merge_settled", {})
        data.setdefault("work_started", {})
        data.setdefault("default_branches", {})
        yield data
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)


def set_tokens(pane_id, tokens, ttl_ms=None):
    params = {"pane_id": pane_id, "source": SOURCE, "tokens": tokens}
    if ttl_ms:
        params["ttl_ms"] = max(1, min(int(ttl_ms), MAX_TTL_MS))
    request("pane.report_metadata", params)


def set_token(pane_id, value, ttl_ms=None):
    set_tokens(pane_id, {TOKEN: value}, ttl_ms)


def workspace_labels():
    workspaces = (request("workspace.list", {}) or {}).get("workspaces", [])
    return {w["workspace_id"]: w.get("label") for w in workspaces}


def report_activity(pane_id, status, workspace):
    working = status == "working"
    try:
        set_tokens(
            pane_id,
            {
                "inbox_rank": "1" if working else "0",
                "ws_busy": workspace if working else None,
                "ws_ready": None if working else workspace,
            },
        )
    except HerdrError:
        pass  # pane already gone


def seed_activity():
    labels = workspace_labels()
    for agent in (request("agent.list", {}) or {}).get("agents", []):
        report_activity(agent["pane_id"], agent.get("agent_status"), labels.get(agent.get("workspace_id")))


def clear_token(pane_id):
    try:
        set_token(pane_id, None)
    except HerdrError:
        pass  # pane already gone


def target_pane():
    pane = os.environ.get("INBOX_TARGET_PANE") or os.environ.get("HERDR_PANE_ID")
    if pane:
        return pane
    context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    for key in ("focused_pane_id", "pane_id"):
        if context.get(key):
            return context[key]
    raise SystemExit("inbox: no focused pane in plugin context")


def notify(message):
    try:
        request("notification.show", {"title": "Inbox", "body": message})
    except (HerdrError, OSError):
        pass


def cmd_toggle():
    with state() as s:
        if s["view_active"]:
            request("agent.view.clear", {"source": SOURCE})
            s["view_active"] = False
        else:
            seed_activity()
            request("agent.view.set", INBOX_VIEW)
            s["view_active"] = True


def cmd_settle():
    pane = target_pane()
    with state() as s:
        set_token(pane, "settled")
        s["panes"][pane] = {"state": "settled", "until": None}


def cmd_unsettle():
    pane = target_pane()
    with state() as s:
        clear_token(pane)
        s["panes"].pop(pane, None)


def apply_snooze_token(pane, until):
    """Sets the snoozed token, with a TTL when herdr's cap allows. Returns whether it has one."""
    remaining_ms = (until - time.time()) * 1000
    if remaining_ms <= MAX_TTL_MS:
        set_token(pane, "snoozed", ttl_ms=max(remaining_ms, 1))
        return True
    set_token(pane, "snoozed")
    return False


def snooze(pane, until):
    with state() as s:
        timed = apply_snooze_token(pane, until)
        s["panes"][pane] = {"state": "snoozed", "until": until, "timed": timed}


def sweep_snoozes():
    now = time.time()
    with state() as s:
        for pane, entry in list(s["panes"].items()):
            if entry["state"] != "snoozed":
                continue
            if entry["until"] <= now:
                clear_token(pane)
                s["panes"].pop(pane)
            elif not entry.get("timed") and (entry["until"] - now) * 1000 <= MAX_TTL_MS:
                try:
                    entry["timed"] = apply_snooze_token(pane, entry["until"])
                except HerdrError:
                    pass  # pane already gone


def cmd_snooze_picker():
    request(
        "plugin.pane.open",
        {
            "plugin_id": PLUGIN_ID,
            "entrypoint": "snooze-picker",
            "env": {"INBOX_TARGET_PANE": target_pane()},
            "focus": True,
        },
    )


def read_key():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def cmd_snooze_picker_ui():
    pane = target_pane()
    print(" Snooze agent until…\n")
    for key, label, _ in SNOOZE_CHOICES:
        print(f"   {key}  {label}")
    print("\n   q  cancel")
    while True:
        key = read_key()
        if key in ("q", "\x1b", "\x03"):
            return
        for choice_key, label, until in SNOOZE_CHOICES:
            if key == choice_key:
                snooze(pane, until(datetime.now()).timestamp())
                return


def event_data():
    return json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}").get("data", {})


def cmd_on_status():
    resurface()
    sweep_snoozes()
    check_merges()


def cmd_on_focus():
    sweep_snoozes()
    check_merges()


def resurface():
    """Updates the activity tokens and resurfaces agents that need attention again.

    A settled agent returns when it finishes a turn (`done`, or `idle` if the
    pane is already seen) or asks for input (`blocked`). A snoozed agent
    returns early only when `blocked`; otherwise the token's TTL brings it back.
    """
    data = event_data()
    pane = data.get("pane_id")
    status = data.get("agent_status")
    if pane:
        report_activity(pane, status, workspace_labels().get(data.get("workspace_id")))
    if pane and status == "working":
        with state() as s:
            s["work_started"][pane] = time.time()
    if not pane or status not in ("idle", "done", "blocked"):
        return
    with state() as s:
        entry = s["panes"].get(pane)
        if not entry:
            return
        if entry["state"] == "settled" or status == "blocked":
            clear_token(pane)
            s["panes"].pop(pane, None)


def live_panes():
    return {p["pane_id"] for p in (request("pane.list", {}) or {}).get("panes", [])}


def prune(s, live):
    for key in ("panes", "merge_settled", "work_started"):
        for pane in list(s[key]):
            if pane not in live:
                s[key].pop(pane)


def cmd_on_closed():
    live = live_panes()
    with state() as s:
        prune(s, live)


def run(cmd, cwd):
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def default_branch(top, cache):
    if top not in cache:
        ref = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], top)
        if ref:
            cache[top] = ref.split("/", 1)[-1]
        else:
            cache[top] = run(
                ["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
                top,
            )
    return cache[top]


def merged_pr(top, branch):
    """Returns (url, merged_at epoch seconds) for the branch's latest merged PR, or None."""
    out = run(
        ["gh", "pr", "list", "--head", branch, "--state", "merged", "--limit", "1",
         "--json", "url,mergedAt", "--jq", '.[0] | select(.) | "\\(.url) \\(.mergedAt)"'],
        top,
    )
    if not out:
        return None
    url, merged_at = out.split(" ", 1)
    return url, datetime.fromisoformat(merged_at.replace("Z", "+00:00")).timestamp()


def check_merges(force=False):
    """Settles agents whose checkout's branch has a merged pull request.

    Agents that are working or blocked are left alone, as are agents that
    started working after the merge: herdr does not report sent messages, so the
    agent starting work stands in for the user asking for more after merging.
    Each merged pull request settles a pane once, so unsettling an agent after
    its merge keeps it in the inbox. GitHub is queried outside the state lock because the queries are slow
    and every other hook needs that lock; a separate lock keeps checks from
    overlapping.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "merges.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with state() as s:
            now = time.time()
            if not force and now - s.get("last_merge_check", 0) < MERGE_CHECK_INTERVAL:
                return
            s["last_merge_check"] = now
            skip = set(s["panes"])
            already = dict(s["merge_settled"])
            work_started = dict(s["work_started"])
            defaults = dict(s["default_branches"])

        found = {}
        by_branch = {}
        for agent in (request("agent.list", {}) or {}).get("agents", []):
            pane = agent["pane_id"]
            if pane in skip or agent.get("agent_status") in ("working", "blocked"):
                continue
            cwd = agent.get("foreground_cwd") or agent.get("cwd")
            top = cwd and run(["git", "rev-parse", "--show-toplevel"], cwd)
            branch = top and run(["git", "symbolic-ref", "--short", "HEAD"], top)
            if not branch or branch == default_branch(top, defaults):
                continue
            if (top, branch) not in by_branch:
                by_branch[(top, branch)] = merged_pr(top, branch)
            pr = by_branch[(top, branch)]
            if pr and already.get(pane) != pr[0] and pr[1] > work_started.get(pane, 0):
                found[pane] = pr[0]

        with state() as s:
            s["default_branches"].update(defaults)
            for pane, url in found.items():
                if pane in s["panes"]:
                    continue
                set_token(pane, "settled")
                s["panes"][pane] = {"state": "settled", "until": None}
                s["merge_settled"][pane] = url


def cmd_restore():
    seed_activity()
    live = live_panes()
    now = time.time()
    with state() as s:
        prune(s, live)
        for pane, entry in list(s["panes"].items()):
            if entry["state"] == "settled":
                set_token(pane, "settled")
            elif entry["until"] <= now:
                s["panes"].pop(pane)
            else:
                entry["timed"] = apply_snooze_token(pane, entry["until"])
        if s["view_active"]:
            request("agent.view.set", INBOX_VIEW)
    check_merges(force=True)


def cmd_status():
    with state() as s:
        print(json.dumps(s, indent=2))


COMMANDS = {
    "seed": seed_activity,
    "check-merges": lambda: check_merges(force=True),
    "toggle": cmd_toggle,
    "settle": cmd_settle,
    "unsettle": cmd_unsettle,
    "snooze-picker": cmd_snooze_picker,
    "snooze-picker-ui": cmd_snooze_picker_ui,
    "on-status": cmd_on_status,
    "on-focus": cmd_on_focus,
    "on-closed": cmd_on_closed,
    "restore": cmd_restore,
    "status": cmd_status,
}


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "snooze":
        snooze(target_pane(), time.time() + int(sys.argv[2]))
        return
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit(f"usage: inbox.py {{{'|'.join(COMMANDS)}}} | snooze SECONDS")
    try:
        COMMANDS[sys.argv[1]]()
    except HerdrError as e:
        notify(f"inbox: {e}")
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
