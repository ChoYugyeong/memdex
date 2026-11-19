const path = require("node:path");
const pty = require("node-pty");

const capacity = 256 * 1024;
function dimension(value, max) {
  if (!Number.isInteger(value) || value < 1 || value > max) throw new Error("Invalid terminal size.");
  return value;
}

// Each live checkout owns its PTY. Detaching the panel keeps running commands alive.
class Terminals {
  constructor() { this.sessions = new Map(); }
  action(folder, method, args) {
    if (!folder) throw new Error("Open a local checkout to use its terminal.");
    const key = process.platform === "win32" ? folder.toLowerCase() : folder;
    let term = this.sessions.get(key);
    if (method === "terminal.open") {
      const cols = dimension(args[0], 1000), rows = dimension(args[1], 500);
      const created = !term || term.exited;
      if (created) {
        const windows = process.platform === "win32";
        const shell = windows ? path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
                              : process.env.SHELL || "/bin/sh";
        const child = pty.spawn(shell, windows ? ["-NoLogo"] : [], {
          cwd: folder, cols, rows, name: "xterm-256color",
          env: { ...process.env, MEMDEX_PROJECT: folder, TERM: "xterm-256color" }
        });
        term = { child, buffer: "", end: 0, exited: false };
        this.sessions.set(key, term);
        child.onData(text => {
          term.buffer = (term.buffer + text).slice(-capacity);
          term.end += text.length;
        });
        child.onExit(() => { term.exited = true; });
      } else term.child.resize(cols, rows);
      return { created };
    }
    if (!term) throw new Error("Open the terminal first.");
    if (method === "terminal.poll") {
      const cursor = Number.isSafeInteger(args[0]) ? args[0] : 0;
      const start = term.end - term.buffer.length;
      return { data: term.buffer.slice(Math.max(0, cursor - start)), cursor: term.end, exited: term.exited };
    }
    if (term.exited) return;
    if (method === "terminal.input" && typeof args[0] === "string" && args[0].length <= 65536) {
      term.child.write(args[0]);
    } else if (method === "terminal.resize") {
      term.child.resize(dimension(args[0], 1000), dimension(args[1], 500));
    } else throw new Error("Invalid terminal operation.");
  }
  close() {
    for (const term of this.sessions.values()) if (!term.exited) term.child.kill();
    this.sessions.clear();
  }
}
module.exports = Terminals;
