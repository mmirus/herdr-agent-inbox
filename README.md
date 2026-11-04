# herdr agent inbox

A herdr plugin that adds an inbox view to the Agents sidebar, inspired by the inbox in [T3 Code](https://github.com/pingdotgg/t3code). Toggling the view hides settled and snoozed agents; toggling it again restores herdr's stock Agents list.

| Action | Effect |
|---|---|
| `agent-inbox.toggle` | Switch the Agents sidebar between the inbox view and the stock view. |
| `agent-inbox.settle` | Hide the focused agent from the inbox until it finishes a turn or asks for input. |
| `agent-inbox.snooze` | Open a popup to hide the focused agent for 15 minutes up to a week, or until it asks for input. |
| `agent-inbox.unsettle` | Return the focused agent to the inbox. |

Agents are also settled automatically when the pull request for their checkout's branch has merged on GitHub. Each merged pull request settles an agent once, so unsettling it afterwards keeps it in the inbox. Agents that are working or waiting for input are not settled, and neither are agents that started working after the merge, so a follow-up you ask for after merging stays visible.

The inbox view puts working agents last, then sorts agents needing attention first, then by most recent state change.

## Install

Requires herdr 0.9.5+ and `python3` on `PATH`. Settling on merge also requires `git` and an authenticated `gh` on the herdr server's `PATH`.

```sh
herdr plugin install mmirus/herdr-agent-inbox
```

Add keybindings to `config.toml`, then run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+i"
type = "plugin_action"
command = "agent-inbox.toggle"
description = "toggle inbox"

[[keys.command]]
key = "prefix+shift+e"
type = "plugin_action"
command = "agent-inbox.settle"
description = "settle agent"

[[keys.command]]
key = "prefix+shift+z"
type = "plugin_action"
command = "agent-inbox.snooze"
description = "snooze agent"

[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "agent-inbox.unsettle"
description = "unsettle agent"
```

Optionally, mute working agents' workspace names and label settled and snoozed agents. Sidebar rows style a token only by its own value, so the plugin reports each agent's workspace name as `$ws_busy` while it is working and as `$ws_ready` otherwise; the unset token renders nothing. These rows replace the built-in `workspace` token, so workspace names disappear from Agent rows if the plugin is disabled, and `$ws_ready` does not turn bold on the focused row as the built-in token does.

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "machine", "$ws_ready", { token = "$ws_busy", dim = true }, "tab"],
  ["agent", { token = "$inbox", dim = true }],
]
```

## Behavior and limits

- Inbox state is a pane metadata token, `inbox`, set to `settled` or `snoozed`. A snooze ends when the token's TTL expires. herdr caps `ttl_ms` at 24 hours, so a longer snooze starts without a TTL and gets one from the first hook that runs within 24 hours of waking: an agent status change or a pane focus change. If no hook runs in that window, the agent wakes at the next one.
- herdr discards metadata tokens and the active agent view when its server restarts. The plugin keeps both in `$HERDR_PLUGIN_STATE_DIR/state.json`, and its startup hook reapplies them, skipping expired snoozes and closed panes.
- herdr allows one agent view per server. Another plugin calling `agent.view.set` replaces the inbox view, and `state.json` will still record it as active until the next toggle.
- The view filters only the Agents panel. The Spaces list is unchanged, and panes without a detected agent never appear in the Agents panel.
- `ws_busy`, `ws_ready`, and `inbox_rank` are refreshed on every agent status change, and seeded from `agent.list` at startup, when the inbox view is turned on, and when a workspace is renamed.
- herdr runs no long-lived plugin process, so the merge check runs when an agent's status changes, when pane focus changes, and at server startup, at most once every 2 minutes. When neither happens, a merge is noticed late. `python3 inbox.py check-merges` runs the check immediately.
- The merge check queries `gh pr list --head <branch> --state merged` for each agent whose checkout is on a branch other than the repository's default branch.
- `python3 inbox.py status` prints the stored state.

## License

MIT. See [LICENSE](LICENSE).
