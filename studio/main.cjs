const { app, BrowserWindow, dialog, ipcMain, protocol, session } = require("electron");
const path = require("node:path");
const fs = require("node:fs/promises");
const Backend = require("./backend.cjs");
const Terminals = require("./terminals.cjs");

protocol.registerSchemesAsPrivileged([{ scheme: "studio", privileges: { standard: true, secure: true } }]);
app.setName("Memdex Studio");
const base = path.resolve(__dirname, "..");
const site = "studio://app/index.html";
let backend, window, view, terminals, switching = false, closing = false;

function installAssets(board) {
  const files = new Map();
  for (const name of ["index.html", "workspace.js", "reviews.js", "workspace.css"]) files.set(name, path.join(board, name));
  for (const name of ["studio.js", "chats.js", "compose.js", "studio.css"]) files.set(name, path.join(__dirname, name));
  for (const font of ["PretendardVariable.woff2", "IBMPlexMono-Regular.woff2"]) {
    files.set(font, path.join(__dirname, "fonts", font));
  }
  for (const [name, module] of [["xterm.js", "@xterm/xterm"], ["fit.js", "@xterm/addon-fit"]])
    files.set(name, require.resolve(module));
  files.set("xterm.css", path.join(path.dirname(require.resolve("@xterm/xterm")), "../css/xterm.css"));
  protocol.handle("studio", async request => {
    const url = new URL(request.url);
    const name = url.pathname.slice(1);
    if (url.host !== "app" || !files.has(name)) return new Response(null, { status: 404 });
    let body = await fs.readFile(files.get(name));
    if (name === "index.html") {
      body = body.toString("utf8").replace('<meta name="color-scheme" content="dark">', '<meta name="color-scheme" content="light">').replace("</head>",
        '<link rel="stylesheet" href="xterm.css"><link rel="stylesheet" href="studio.css">' +
        '<script defer src="xterm.js"></script><script defer src="fit.js"></script><script defer src="studio.js"></script><script defer src="chats.js"></script><script defer src="compose.js"></script></head>')
        .replace("</footer>", '<span class="studio-version">Studio ' + require("./package.json").version + '</span></footer>');
    }
    const type = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
      ".css": "text/css; charset=utf-8", ".woff2": "font/woff2" }[path.extname(name)];
    return new Response(body, { headers: { "Content-Type": type, "Cache-Control": "no-store",
      "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'" } });
  });
}

async function navigate(next, pane) {
  view = next;
  window.setTitle(next.name ? next.name + " — Memdex Studio" : "Memdex Studio");
  await window.loadURL(site + "?view=" + next.token + (next.folder ? "" : "&history=1") + (pane === "messages" || pane === "qa" ? "&pane=" + pane : ""));
}

async function changeProject(method, args = {}) {
  if (switching) throw new Error("Project selection is in progress.");
  switching = true;
  try {
    if (method === "folder") {
      const picked = await dialog.showOpenDialog(window, { properties: ["openDirectory"], defaultPath: view.folder || undefined });
      if (picked.canceled) return;
      args.folder = picked.filePaths[0];
    }
    const { pane, ...selection } = args;
    await navigate(await backend.request(method, { ...selection, token: view.token }), pane);
  } finally { switching = false; }
}

async function start() {
  const resources = process.resourcesPath;
  const python = app.isPackaged ? path.join(resources, "runtime", "python.exe") : process.env.MEMDEX_PYTHON || "python";
  const board = app.isPackaged ? path.join(resources, "runtime/Lib/site-packages/memdex/board_assets") : path.join(base, "memdex/board_assets");
  const args = app.isPackaged ? ["-I", "-B"] : ["-B"];
  args.push("-m", "memdex.studio_rpc");
  for (const option of ["db", "limit"]) {
    if (app.commandLine.hasSwitch(option)) args.push("--" + option, app.commandLine.getSwitchValue(option));
  }
  backend = new Backend(python, args, app.isPackaged ? resources : base);
  const initial = await backend.request("initial", { folder: app.commandLine.getSwitchValue("project") || process.env.MEMDEX_PROJECT });
  installAssets(board);
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  window = new BrowserWindow({ width: 1320, height: 880, minWidth: 660, minHeight: 500, show: false,
    icon: path.join(board, "studio.ico"), backgroundColor: "#101b1e",
    webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true, sandbox: true, nodeIntegration: false } });
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.on("page-title-updated", event => event.preventDefault());
  window.webContents.on("will-navigate", event => event.preventDefault());
  window.webContents.on("will-attach-webview", event => event.preventDefault());
  window.setMenu(null);
  window.once("close", () => { closing = true; });
  ipcMain.handle("studio", async (event, token, method, ...args) => {
    if (!view || token !== view.token || event.sender !== window.webContents ||
        event.senderFrame !== window.webContents.mainFrame || event.senderFrame.url.split("?")[0] !== site)
      throw new Error("This project view is closed.");
    switch (method) {
      case "read": return backend.request("read", { token, endpoint: args[0] });
      case "write": return backend.request("write", { token, action: args[0], body: args[1] });
      case "projects": return { current: view, groups: await backend.request("groups") };
      case "pickFolder": return changeProject("folder", {pane:args[0]});
      case "select": return changeProject("select", { key: args[0], checkout: args[1], pane:args[2] });
      default:
        if (typeof method !== "string" || !method.startsWith("terminal.")) throw new Error("Unknown Studio action.");
        if (method === "terminal.open") {
          await backend.request("terminalFolder", { token });
          if (view.token !== token) throw new Error("This project view is closed.");
        }
        terminals ||= new Terminals();
        return terminals.action(view.folder, method, args);
    }
  });
  window.once("ready-to-show", () => window.show());
  await navigate(initial, initial.folder ? "messages" : undefined);
}

app.whenReady().then(start).catch(error => {
  if (!closing) dialog.showErrorBox("Memdex Studio", error.message);
  app.quit();
});
app.on("window-all-closed", () => app.quit());
app.on("will-quit", () => { terminals?.close(); backend?.close(); });
