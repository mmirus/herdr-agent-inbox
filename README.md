# herdr agent inbox

A herdr plugin that adds an inbox view to the Agents sidebar, inspired by the inbox in [T3 Code](https://github.com/pingdotgg/t3code). Toggling the view hides settled and snoozed agents; toggling it again restores herdr's stock Agents list.

| Action | Effect |
|---|---|
| `agent-inbox.toggle` | Switch the Agents sidebar between the inbox view and the stock view. |
| `agent-inbox.settle` | Hide the focused agent from the inbox until it finishes a turn or asks for input. |
| `agent-inbox.snooze` | Open a popup to hide the focused agent for 15 minutes up to a week, or until it asks for input. |
| `agent-inbox.unsettle` | Return the focused agent to the inbox. |
| `agent-inbox.refresh` | Recompute which agents count as working. The OpenCode bridge runs it when background tasks start or finish. |

Agents are also settled automatically when the pull request for their checkout's branch has merged on GitHub. Each merged pull request settles an agent once, so unsettling it afterwards keeps it in the inbox. Agents that are working or waiting for input are not settled, and neither are agents that started working after the merge, so a follow-up you ask for after merging stays visible.

The inbox view puts working agents last, then sorts agents needing attention first, then by most recent state change. With the [OpenCode bridge](#opencode-bridge-for-full-claude-agent) installed, an idle [Full Claude Agent](https://github.com/mmirus/opencode-full-claude-agent) session with background tasks still running also counts as working.

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

### OpenCode bridge for Full Claude Agent

When a Full Claude Agent turn ends with native background tasks still running, OpenCode reports the session idle, and so does herdr. The `agent-inbox-opencode` directory is an OpenCode 2.x TUI plugin that reads the session's tasks from Full Claude Agent and reports them to herdr. While non-ambient tasks run in the pane's session, it sets the pane token `bg_tasks` to their count and labels the `idle` and `done` states "N Claude tasks running".

To install it, add the directory's absolute path to `plugins` in `~/.config/opencode/opencode.jsonc`, then restart OpenCode. herdr keeps GitHub-installed plugins under `~/.config/herdr/plugins/github/`.

```jsonc
"plugins": ["/path/to/agent-inbox/agent-inbox-opencode"]
```

## Behavior and limits

- Inbox state is a pane metadata token, `inbox`, set to `settled` or `snoozed`. A snooze ends when the token's TTL expires. herdr caps `ttl_ms` at 24 hours, so a longer snooze starts without a TTL and gets one from the first hook that runs within 24 hours of waking: an agent status change or a pane focus change. If no hook runs in that window, the agent wakes at the next one.
- herdr discards metadata tokens and the active agent view when its server restarts. The plugin keeps both in `$HERDR_PLUGIN_STATE_DIR/servers/<id>/state.json`, one file per herdr server socket, because every herdr session shares the plugin's state directory while views, tokens, and pane IDs belong to one server. Its startup hook reapplies them, skipping expired snoozes and closed panes.
- herdr allows one agent view per server. Another plugin calling `agent.view.set` replaces the inbox view, and `state.json` will still record it as active until the next toggle.
- The view filters only the Agents panel. The Spaces list is unchanged, and panes without a detected agent never appear in the Agents panel.
- `ws_busy`, `ws_ready`, and `inbox_rank` are refreshed on every agent status change, and seeded from `agent.list` at startup, when the inbox view is turned on, when a workspace is renamed, and when `agent-inbox.refresh` runs. An agent counts as working when herdr reports it `working`, or when it is `idle` or `done` with a `bg_tasks` token.
- The bridge's `bg_tasks` token expires 2 minutes after the bridge's last report, so a crashed OpenCode TUI leaves it for at most that long. The bridge re-reports every minute while tasks run, and clears the token and labels when the TUI exits or starts.
- herdr runs no long-lived plugin process, so the merge check runs when an agent's status changes, when pane focus changes, and at server startup, at most once every 2 minutes. When neither happens, a merge is noticed late. `python3 inbox.py check-merges` runs the check immediately; run it, like `status`, with `HERDR_PLUGIN_STATE_DIR` set to the plugin's state directory and `HERDR_SOCKET_PATH` set to the server's socket.
- The merge check queries `gh pr list --head <branch> --state merged` for each agent whose checkout is on a branch other than the repository's default branch.
- `python3 inbox.py status` prints the stored state for the server at `HERDR_SOCKET_PATH`.

## Known problems

- **herdr still shows an agent with background tasks as idle.** The OpenCode bridge changes only the inbox's sorting, the `$ws_busy` and `$ws_ready` tokens, and the state label. herdr's state icon, its "finished" notification, and `herdr agent wait` still see the agent as idle. While herdr's OpenCode integration (source `herdr:opencode`) holds a pane, herdr ignores state reports from any other source, so the bridge can report only display metadata. Fixing this needs upstream changes: OpenCode letting plugins register a session's background work, and herdr's OpenCode integration reporting `working` while that work runs. herdr's integration also reports OpenCode's own background shells and subagents as idle.
- **The bridge depends on Full Claude Agent's task RPC** (`full-claude-agent.tasks`, its `list` method and `changed` event). If that RPC changes, the bridge stops reporting tasks.

## License

MIT. See [LICENSE](LICENSE).
