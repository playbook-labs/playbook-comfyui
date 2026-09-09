import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const PLAYBOOK_NODES = ["PlaybookSave", "PlaybookLoad"];
const NONE = "(no board)";
const MIN_WIDTH = 260;

let boardCache = null;

// The key is collected in this overlay and posted straight to our own endpoint, which
// stores it server side. It is deliberately NOT a registered ComfyUI setting: those are
// persisted to comfy.settings.json and readable by any other extension through
// GET /settings/{id}. It is not a node widget either -- widget values are serialised
// into workflow.json and into the metadata of every PNG the user shares.
async function post(path, body) {
  const response = await api.fetchApi(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // Not every failure comes back as JSON -- an unhandled server error is an HTML page, and
  // parsing that would report a syntax error instead of the status that caused it.
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Playbook rejected the request (HTTP ${response.status})`);
  }
  return payload;
}

async function status() {
  const response = await api.fetchApi("/playbook/status");
  return response.json();
}

function element(tag, style, text) {
  const node = document.createElement(tag);
  Object.assign(node.style, style);
  if (text) node.textContent = text;
  return node;
}

function openConnectDialog(state) {
  const backdrop = element("div", {
    position: "fixed", inset: "0", background: "rgba(0,0,0,.6)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: "10000",
  });
  const panel = element("div", {
    background: "#222", color: "#eee", padding: "20px", borderRadius: "8px",
    width: "420px", font: "13px system-ui, sans-serif",
  });

  panel.appendChild(element("div", { fontSize: "15px", marginBottom: "4px" }, "Connect Playbook"));
  panel.appendChild(element("div", { opacity: ".7", marginBottom: "12px" },
    "Create a token under Developer → SDK in the Playbook app. It is stored on this machine, outside your workflows."));

  const input = document.createElement("input");
  input.type = "password";
  input.placeholder = state?.connected ? `${state.masked} — paste a new key to replace` : "API key";
  Object.assign(input.style, {
    width: "100%", padding: "8px", boxSizing: "border-box", marginBottom: "10px",
    background: "#111", color: "#eee", border: "1px solid #444", borderRadius: "4px",
  });

  const workspace = document.createElement("select");
  Object.assign(workspace.style, {
    width: "100%", padding: "8px", boxSizing: "border-box", marginBottom: "10px",
    background: "#111", color: "#eee", border: "1px solid #444", borderRadius: "4px",
    display: "none",
  });

  const message = element("div", { minHeight: "18px", marginBottom: "10px", opacity: ".8" });
  const buttons = element("div", { display: "flex", gap: "8px", justifyContent: "flex-end" });
  const cancel = element("button", { padding: "6px 12px" }, "Cancel");
  const connect = element("button", { padding: "6px 12px" }, "Connect");

  buttons.append(cancel, connect);
  panel.append(input, workspace, message, buttons);
  backdrop.appendChild(panel);
  document.body.appendChild(backdrop);
  input.focus();

  const close = () => backdrop.remove();
  cancel.onclick = close;
  backdrop.onclick = (event) => {
    if (event.target === backdrop) close();
  };

  connect.onclick = async () => {
    const key = input.value.trim();
    if (!key) {
      message.textContent = "Paste a key first.";
      return;
    }

    connect.disabled = true;
    message.textContent = "Checking…";
    try {
      const result = await post("/playbook/credentials", {
        api_key: key,
        slug: workspace.value || undefined,
      });

      // More than one workspace and none chosen yet: ask, rather than storing a null
      // slug that makes every later call fail with "no workspace selected".
      if (!result.slug && (result.workspaces || []).length > 1) {
        workspace.innerHTML = "";
        for (const item of result.workspaces) {
          const option = document.createElement("option");
          option.value = item.slug;
          option.textContent = item.name || item.slug;
          workspace.appendChild(option);
        }
        workspace.style.display = "block";
        message.textContent = "Pick a workspace, then Connect again.";
        connect.disabled = false;
        return;
      }

      boardCache = null;
      message.textContent = `Connected: ${result.masked}${result.slug ? ` (${result.slug})` : ""}`;
      setTimeout(close, 900);
    } catch (error) {
      message.textContent = error.message;
      connect.disabled = false;
    }
  };
}

const titles = new Map();

async function boardTitle(token) {
  if (titles.has(token)) return titles.get(token);

  let title = null;
  try {
    const response = await api.fetchApi(`/playbook/board?token=${encodeURIComponent(token)}`);
    const body = await response.json();
    if (response.ok) title = body.title;
  } catch (error) {
    console.warn("[Playbook]", error.message);
  }

  titles.set(token, title);
  return title;
}

async function loadBoards(force = false) {
  if (boardCache && !force) return boardCache;

  const response = await api.fetchApi("/playbook/boards");
  const body = await response.json();
  if (!response.ok) {
    boardCache = null;
    throw new Error(body.error || "Could not load boards");
  }

  const seen = new Map();
  boardCache = (body.boards || []).map((board) => {
    const count = (seen.get(board.title) || 0) + 1;
    seen.set(board.title, count);
    // Two boards can carry the same title; the combo entry has to stay distinguishable.
    return {
      label: count > 1 ? `${board.title} (${count})` : board.title,
      token: board.token,
    };
  });
  return boardCache;
}

// Picked in an overlay rather than a combo widget: a combo's `values` are read when the
// widget is created, and mutating them afterwards does not reach the Vue frontend, so a
// list filled by an async fetch never appeared. The overlay owns its own DOM.
let boardDialogOpen = false;

function openBoardDialog(onPick) {
  // Opened synchronously and filled afterwards. Fetching first meant several seconds of
  // nothing happening on a workspace with many boards, during which a second click
  // started a second dialog.
  if (boardDialogOpen) return;
  boardDialogOpen = true;

  const backdrop = element("div", {
    position: "fixed", inset: "0", background: "rgba(0,0,0,.6)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: "10000",
  });
  const panel = element("div", {
    background: "#222", color: "#eee", padding: "20px", borderRadius: "8px",
    width: "420px", maxHeight: "70vh", display: "flex", flexDirection: "column",
    font: "13px system-ui, sans-serif",
  });

  panel.appendChild(element("div", { fontSize: "15px", marginBottom: "10px" }, "Pick a board"));

  const search = document.createElement("input");
  search.placeholder = "Filter…";
  Object.assign(search.style, {
    width: "100%", padding: "8px", boxSizing: "border-box", marginBottom: "10px",
    background: "#111", color: "#eee", border: "1px solid #444", borderRadius: "4px",
  });

  const list = element("div", { overflowY: "auto", flex: "1", minHeight: "0" });
  const footer = element("div", { display: "flex", justifyContent: "space-between",
                                  alignItems: "center", marginTop: "10px" });
  const note = element("div", { opacity: ".6" });
  const reload = element("button", { padding: "6px 12px" }, "Reload");
  footer.append(note, reload);

  const close = () => {
    boardDialogOpen = false;
    backdrop.remove();
  };

  let boards = [];

  const render = (filter) => {
    list.innerHTML = "";
    const needle = filter.trim().toLowerCase();
    const shown = boards.filter((board) => !needle || board.label.toLowerCase().includes(needle));

    // Always offered: without it a token picked once could never be taken back, and the
    // node would keep uploading into a board the graph no longer means to use.
    const clear = element("div", { padding: "8px", borderRadius: "4px", cursor: "pointer",
                                   opacity: ".7" }, NONE);
    clear.onclick = () => {
      onPick(null);
      close();
    };
    list.appendChild(clear);

    if (!shown.length) {
      list.appendChild(element("div", { opacity: ".6", padding: "8px" }, "Nothing matches."));
      return;
    }

    for (const board of shown) {
      const row = element("div", { padding: "8px", borderRadius: "4px", cursor: "pointer" },
        board.label);
      row.onmouseenter = () => (row.style.background = "#333");
      row.onmouseleave = () => (row.style.background = "transparent");
      row.onclick = () => {
        onPick(board);
        close();
      };
      list.appendChild(row);
    }
  };

  const fill = async (force) => {
    list.innerHTML = "";
    list.appendChild(element("div", { opacity: ".6", padding: "8px" }, "Loading boards…"));
    reload.disabled = true;

    try {
      // Cached between openings: this listing is expensive server side, and boards do
      // not change between two presses of the same button.
      boards = await loadBoards(force);
      note.textContent = `${boards.length} boards`;
      render(search.value);
    } catch (error) {
      list.innerHTML = "";
      list.appendChild(element("div", { padding: "8px" }, error.message));
    } finally {
      reload.disabled = false;
    }
  };

  search.oninput = () => render(search.value);
  reload.onclick = () => fill(true);

  panel.append(search, list, footer);
  backdrop.appendChild(panel);
  document.body.appendChild(backdrop);
  search.focus();

  backdrop.onclick = (event) => {
    if (event.target === backdrop) close();
  };

  fill(false);
}

// Writes the token into the real `board` widget and shows the title beside it: the token
// is what the graph must store, because titles are neither unique nor stable.
function attachPicker(node) {
  const target = node.widgets?.find((widget) => widget.name === "board");
  if (!target) return;

  // Display only, derived from `board`. read_only is what the frontend's text widget
  // actually honours (WidgetInputText: read_only || disabled); the callback re-deriving
  // the value is the fallback for frontends that predate it.
  const chosen = node.addWidget("text", "selected board", NONE, () => syncLabel());
  chosen.serialize = false;
  chosen.options = { ...(chosen.options || {}), read_only: true };

  // `board` is a real widget, so a token picked once is saved into the workflow and comes
  // back on load -- while this label does not, being serialize=false. Without this the
  // label read "(no board)" over a node that was still uploading into the saved one.
  const syncLabel = async () => {
    const token = (target.value || "").trim();
    if (!token) {
      chosen.value = NONE;
      return;
    }

    const known = (boardCache || []).find((board) => board.token === token);
    if (known) {
      chosen.value = known.label;
      return;
    }

    // A workflow loaded from disk carries the token but no name, and fetching every board
    // just to label one node would be wasteful. Ask for the one.
    chosen.value = "…";
    const title = await boardTitle(token);

    // The selection may have moved on while that was in flight; a slow answer for the old
    // board must not label a node that now points at another one.
    if ((target.value || "").trim() !== token) return;

    chosen.value = title || `token ${token}`;
    app.graph.setDirtyCanvas(true);
  };

  const originalConfigure = node.onConfigure;
  node.onConfigure = function (data) {
    originalConfigure?.apply(this, arguments);
    syncLabel();
  };

  // Also when the field is edited or emptied by hand -- otherwise clearing the token
  // leaves the label naming a board the node no longer uploads to.
  const originalCallback = target.callback;
  target.callback = function () {
    const result = originalCallback?.apply(this, arguments);
    syncLabel();
    return result;
  };

  const pick = node.addWidget("button", "Pick board", null, () => {
    openBoardDialog((board) => {
      target.value = board ? board.token : "";
      syncLabel();
      app.graph.setDirtyCanvas(true);
    });
  });
  pick.serialize = false;

  syncLabel();

  const connect = node.addWidget("button", "Connect Playbook", null, async () => {
    let state = {};
    try {
      state = await status();
    } catch (error) {
      console.warn("[Playbook]", error.message);
    }
    if (state.multi_user) {
      alert(
        "This ComfyUI runs in multi-user mode. Set PLAYBOOK_API_KEY in the environment, " +
          "or wire a key into the node's api_key input.",
      );
      return;
    }
    openConnectDialog(state);
  });
  connect.serialize = false;

  // Every optional input gets a widget, and the default width that comes out of that is
  // wider than the node needs. Only for freshly dropped nodes: one loaded from a workflow
  // has its own saved size applied after this, which is the user's own choice.
  const width = node.size?.[0] || 0;
  if (width > MIN_WIDTH) {
    node.setSize([Math.max(MIN_WIDTH, Math.round(width * 0.8)), node.size[1]]);
  }
}

function attachStatus(nodeType) {
  const original = nodeType.prototype.onExecuted;

  nodeType.prototype.onExecuted = function (message) {
    original?.apply(this, arguments);

    const summary = message?.playbook?.[0]?.summary;
    if (!summary) return;

    let widget = this.widgets?.find((entry) => entry.name === "last run");
    if (!widget) {
      widget = this.addWidget("text", "last run", "", () => {});
      widget.serialize = false;
    }
    widget.value = summary;
    this.setDirtyCanvas(true);
  };
}

app.registerExtension({
  name: "playbook.creative",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name === "PlaybookSave") attachStatus(nodeType);
  },

  async nodeCreated(node) {
    if (PLAYBOOK_NODES.includes(node.comfyClass)) attachPicker(node);
  },
});
