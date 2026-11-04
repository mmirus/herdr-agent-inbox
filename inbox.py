"""Inbox view for herdr's Agents sidebar.

Inbox membership lives in a pane metadata token named `inbox` (`settled` or
`snoozed`); the inbox view hides agents carrying either value. Herdr drops
metadata tokens and agent views when its server restarts, so the same facts are
kept in STATE_DIR/state.json and reapplied by the `restore` startup hook.

Sidebar rows can only style a token by that token's own value, so the agent's
name is reported twice over: `name_ready` while the agent is not working and
`name_busy` while it is. A row renders both, styles `$name_busy` muted, and
whichever is unset disappears. `inbox_rank` (`0` not working, `1` working) is
the inbox view's first sort key.
"""

import fcntl
import json
import os
import socket
import sys
import termios
import time
import tty
from contextlib import contextmanager

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "mmirus.inbox")
SOURCE = f"plugin:{PLUGIN_ID}"
TOKEN = "inbox"
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
    "~/.local/state/herdr-inbox"
)
STATE_FILE = os.path.join(STATE_DIR, "state.json")
MAX_TTL_MS = 86_400_000  # herdr's ttl_ms ceiling

SNOOZE_CHOICES = [
    ("1", "15 minutes", 15 * 60),
    ("2", "1 hour", 60 * 60),
    ("3", "3 hours", 3 * 60 * 60),
    ("4", "8 hours", 8 * 60 * 60),
    ("5", "24 hours", 24 * 60 * 60),
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
    path = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(path)
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
        data.setdefault("view_active", False)
        data.setdefault("panes", {})
        yield data
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)


def set_tokens(pane_id, tokens, ttl_ms=None):
    params = {"pane_id": pane_id, "source": SOURCE, "tokens": tokens}
    if ttl_ms:
        params["ttl_ms"] = min(int(ttl_ms), MAX_TTL_MS)
    request("pane.report_metadata", params)


def set_token(pane_id, value, ttl_ms=None):
    set_tokens(pane_id, {TOKEN: value}, ttl_ms)


def report_activity(pane_id, status, name):
    working = status == "working"
    try:
        set_tokens(
            pane_id,
            {
                "inbox_rank": "1" if working else "0",
                "name_busy": name if working else None,
                "name_ready": None if working else name,
            },
        )
    except HerdrError:
        pass  # pane already gone


def seed_activity():
    for agent in (request("agent.list", {}) or {}).get("agents", []):
        report_activity(
            agent["pane_id"], agent.get("agent_status"), agent.get("display_agent") or agent.get("agent")
        )


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


def snooze(pane, seconds):
    with state() as s:
        set_token(pane, "snoozed", ttl_ms=seconds * 1000)
        s["panes"][pane] = {"state": "snoozed", "until": time.time() + seconds}


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
        for choice_key, label, seconds in SNOOZE_CHOICES:
            if key == choice_key:
                snooze(pane, seconds)
                return


def event_data():
    return json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}").get("data", {})


def cmd_on_status():
    """Updates the activity tokens and resurfaces agents that need attention again.

    A settled agent returns when it finishes a turn (`done`, or `idle` if the
    pane is already seen) or asks for input (`blocked`). A snoozed agent
    returns early only when `blocked`; otherwise the token's TTL brings it back.
    """
    data = event_data()
    pane = data.get("pane_id")
    status = data.get("agent_status")
    if pane:
        report_activity(pane, status, data.get("display_agent") or data.get("agent"))
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


def cmd_on_closed():
    live = live_panes()
    with state() as s:
        for pane in list(s["panes"]):
            if pane not in live:
                s["panes"].pop(pane)


def cmd_restore():
    seed_activity()
    live = live_panes()
    now = time.time()
    with state() as s:
        for pane, entry in list(s["panes"].items()):
            if pane not in live or (entry["until"] and entry["until"] <= now):
                s["panes"].pop(pane)
                continue
            ttl_ms = (entry["until"] - now) * 1000 if entry["until"] else None
            set_token(pane, entry["state"], ttl_ms=ttl_ms)
        if s["view_active"]:
            request("agent.view.set", INBOX_VIEW)


def cmd_status():
    with state() as s:
        print(json.dumps(s, indent=2))


COMMANDS = {
    "toggle": cmd_toggle,
    "settle": cmd_settle,
    "unsettle": cmd_unsettle,
    "snooze-picker": cmd_snooze_picker,
    "snooze-picker-ui": cmd_snooze_picker_ui,
    "on-status": cmd_on_status,
    "on-closed": cmd_on_closed,
    "restore": cmd_restore,
    "status": cmd_status,
}


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "snooze":
        snooze(target_pane(), int(sys.argv[2]))
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
