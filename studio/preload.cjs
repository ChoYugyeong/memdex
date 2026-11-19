const { contextBridge, ipcRenderer } = require("electron");
const token = new URL(location.href).searchParams.get("view");
const pane = () => document.getElementById("qa-workspace")?.hidden ? "messages" : "qa";
const call = (method, ...args) => ipcRenderer.invoke("studio", token, method, ...args);
contextBridge.exposeInMainWorld("memdexStudio", {
  token,
  read: (_view, endpoint) => call("read", endpoint),
  write: (_view, action, body) => call("write", action, body),
  projects: () => call("projects"),
  select: (key, checkout) => call("select", key, checkout, pane()),
  pickFolder: () => call("pickFolder", pane()),
  openTerminal: (cols, rows) => call("terminal.open", cols, rows),
  pollTerminal: cursor => call("terminal.poll", cursor),
  typeTerminal: data => call("terminal.input", data),
  resizeTerminal: (cols, rows) => call("terminal.resize", cols, rows)
});
