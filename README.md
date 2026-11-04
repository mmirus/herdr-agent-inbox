# herdr inbox

A herdr plugin that adds an inbox view to the Agents sidebar, inspired by t3code's inbox. Toggling the view hides settled and snoozed agents; toggling it again restores herdr's stock Agents list.

| Action | Effect |
|---|---|
| `mmirus.inbox.toggle` | Switch the Agents sidebar between the inbox view and the stock view. |
| `mmirus.inbox.settle` | Hide the focused agent from the inbox until it finishes a turn or asks for input. |
| `mmirus.inbox.snooze` | Open a popup to hide the focused agent for 15 minutes to 24 hours, or until it asks for input. |
| `mmirus.inbox.unsettle` | Return the focused agent to the inbox. |

The inbox view puts working agents last, then sorts agents needing attention first, then by most recent state change.

## Install

Requires herdr 0.9.5+ and `python3` on `PATH`.

```sh
herdr plugin link /path/to/herdr-t3code-inbox
```

Add keybindings to `config.toml`, then run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "mmirus.inbox.toggle"
description = "toggle inbox"

[[keys.command]]
key = "prefix+shift+e"
type = "plugin_action"
command = "mmirus.inbox.settle"
description = "settle agent"

[[keys.command]]
key = "prefix+shift+z"
type = "plugin_action"
command = "mmirus.inbox.snooze"
description = "snooze agent"

[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "mmirus.inbox.unsettle"
description = "unsettle agent"
```

Optionally, mute working agents' names and label settled and snoozed agents. Sidebar rows style a token only by its own value, so the plugin reports each agent's name as `$name_busy` while it is working and as `$name_ready` otherwise; the unset token renders nothing. These rows replace the built-in `agent` token, so agent names disappear if the plugin is disabled.

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "machine", "workspace", "tab"],
  ["$name_ready", { token = "$name_busy", dim = true }, { token = "$inbox", dim = true }],
]
```

## Behavior and limits

- Inbox state is a pane metadata token, `inbox`, set to `settled` or `snoozed`. A snooze is a token TTL, so herdr's 24-hour `ttl_ms` limit caps snooze length.
- herdr discards metadata tokens and the active agent view when its server restarts. The plugin keeps both in `$HERDR_PLUGIN_STATE_DIR/state.json`, and its startup hook reapplies them, skipping expired snoozes and closed panes.
- herdr allows one agent view per server. Another plugin calling `agent.view.set` replaces the inbox view, and `state.json` will still record it as active until the next toggle.
- The view filters only the Agents panel. The Spaces list is unchanged, and panes without a detected agent never appear in the Agents panel.
- `name_busy`, `name_ready`, and `inbox_rank` are refreshed on every agent status change and seeded from `agent.list` at startup and when the inbox view is turned on.
- `python3 inbox.py status` prints the stored state.
