// OpenCode 2.x TUI plugin that reports Full Claude Agent background tasks to herdr.
//
// When a Claude turn ends with native background tasks still running, OpenCode
// reports the session idle and herdr's OpenCode integration reports the pane
// idle. Herdr ignores agent state from any other source while its integration
// holds the pane, so this plugin reports display-only metadata instead: the
// `bg_tasks` pane token, which the inbox counts as working, and a state label.

import net from "node:net";

const SOURCE = "plugin:agent-inbox:opencode";
const TOKEN = "bg_tasks";
const INBOX_PLUGIN_ID = "agent-inbox";
// The token outlives a crashed TUI by at most TOKEN_TTL_MS; while tasks run it
// is re-reported every TOKEN_REFRESH_MS.
const TOKEN_TTL_MS = 120_000;
const TOKEN_REFRESH_MS = 60_000;
const ROUTE_POLL_MS = 500;
// The provider's RPC events are live-only, so a periodic read recovers
// changes missed while the server connection was down.
const FALLBACK_READ_MS = 30_000;

// Full Claude Agent's task RPC. The client uses only the id and the method and
// event names, so no schemas are needed.
const NativeTasks = { id: "full-claude-agent.tasks", methods: { list: {} }, events: { changed: {} } };

let sequence = Date.now() * 1000;

function request(method, params) {
  return new Promise((resolve) => {
    let buffer = "";
    const client = net.createConnection(process.env.HERDR_SOCKET_PATH, () => {
      client.write(`${JSON.stringify({ id: `${SOURCE}:${++sequence}`, method, params })}\n`);
    });
    const finish = (ok) => {
      client.destroy();
      resolve(ok);
    };
    client.setTimeout(2_000, () => finish(false));
    client.on("data", (chunk) => {
      buffer += chunk;
      if (buffer.endsWith("\n")) {
        try {
          finish(!JSON.parse(buffer).error);
        } catch {
          finish(false);
        }
      }
    });
    client.on("error", () => finish(false));
  });
}

function label(count) {
  return `${count} Claude task${count === 1 ? "" : "s"} running`;
}

async function report(count) {
  const pane = process.env.HERDR_PANE_ID;
  const params = count
    ? { tokens: { [TOKEN]: String(count) }, state_labels: { idle: label(count), done: label(count) }, ttl_ms: TOKEN_TTL_MS }
    : { tokens: { [TOKEN]: null }, clear_state_labels: true };
  return request("pane.report_metadata", { pane_id: pane, source: SOURCE, seq: ++sequence, ...params });
}

function refreshInbox() {
  return request("plugin.action.invoke", { plugin_id: INBOX_PLUGIN_ID, action_id: "refresh" });
}

function setup(api) {
  if (process.env.HERDR_ENV !== "1" || !process.env.HERDR_SOCKET_PATH || !process.env.HERDR_PANE_ID) return;

  const rpc = api.client.rpc(NativeTasks);
  let disposed = false;
  let selected;
  let watch;
  // Undefined until the first report, which also clears a token a crashed TUI
  // left on this pane.
  let reported;
  let reportedAt = 0;
  let chain = Promise.resolve();

  function root(id) {
    const seen = new Set();
    while (typeof id === "string" && !seen.has(id)) {
      seen.add(id);
      const session = api.data.session.get(id);
      if (!session?.parentID) return id;
      id = session.parentID;
    }
  }

  function routedRoot() {
    const route = api.ui.router.current();
    return route.type === "session" ? root(route.sessionID) : undefined;
  }

  function publish(count) {
    chain = chain.then(async () => {
      const now = Date.now();
      if (count === reported && (!count || now - reportedAt < TOKEN_REFRESH_MS)) return;
      if (!(await report(count))) return;
      const flipped = reported === undefined || !count !== !reported;
      reported = count;
      reportedAt = now;
      if (flipped) await refreshInbox();
    });
  }

  async function read(sessionID, signal) {
    const location = api.data.session.get(sessionID)?.location ?? api.location ?? api.data.location.default();
    try {
      const { tasks } = await rpc.list({ sessionID }, { location, signal });
      return tasks.filter((task) => task.state === "running" && !task.ambient).length;
    } catch {
      return 0;
    }
  }

  function follow(sessionID) {
    watch?.abort();
    watch = new AbortController();
    const { signal } = watch;
    if (!sessionID) {
      publish(0);
      return;
    }
    const update = async () => {
      const count = await read(sessionID, signal);
      if (!signal.aborted) publish(count);
    };
    rpc.events.on("changed", (event) => {
      if (event.data.sessionID === sessionID) void update();
    }, { signal });
    const reconnect = api.data.on("server.connected", () => void update());
    const fallback = setInterval(() => void update(), FALLBACK_READ_MS);
    signal.addEventListener("abort", () => {
      reconnect();
      clearInterval(fallback);
    });
    void update();
  }

  function sync() {
    if (disposed) return;
    const id = routedRoot();
    if (id !== selected) {
      selected = id;
      follow(id);
    } else if (reported) {
      publish(reported);
    }
  }

  const poll = setInterval(sync, ROUTE_POLL_MS);
  sync();
  return () => {
    disposed = true;
    clearInterval(poll);
    watch?.abort();
    publish(0);
  };
}

export default { id: "agent-inbox.opencode", setup };
