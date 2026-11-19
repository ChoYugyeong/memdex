"use strict";

// This script is served only by the desktop app's local protocol.
(() => {
  const api = window.memdexStudio;
  const toolbar = node("div", "studio-bar");
  const select = node("select"), checkout = node("select");
  const folder = node("button"), toggle = node("button");
  const info = node("span", "studio-counts"), notice = node("span", "studio-notice");
  notice.setAttribute("role", "status");
  const projectLabel = node("label","studio-project-label");
  select.id = "studio-project-picker"; projectLabel.htmlFor = select.id;
  const projectName = node("strong","studio-project-name");
  toolbar.append(projectLabel, select, projectName, checkout, info, notice, folder, toggle);
  let names = new Map();
  function optionLabels() {
    for (const opt of select.options) {
      const group = groups.find(g => g.key === opt.value);
      const ide = (group.codex || 0) + (group.claude || 0);
      opt.text = names.get(opt.value) + " · Memdex "+group.messages+" · IDE "+ide;
    }
    projectName.textContent = names.get(current?.key) || label("Choose a folder","폴더를 열어 주세요");
  }
  document.querySelector(".brand").append(node("span","studio-product","Studio"));
  document.querySelector(".appbar").after(toolbar);
  const panel = node("section", "studio-terminal");
  panel.hidden = true;
  const host = node("div", "studio-terminal-host");
  panel.append(host);
  document.querySelector("footer").before(panel);
  toggle.setAttribute("aria-expanded", "false");
  let groups = [], current, term, fit, attached = false, busy = false, cursor = 0, timer, generation = 0;
  const label = (en, ko) => korean ? ko : en;
  function error(err) { notice.textContent = err.message || String(err); }
  function labels() {
    optionLabels();
    select.setAttribute("aria-label", label("Project", "프로젝트"));
    projectLabel.textContent = select.getAttribute("aria-label");
    checkout.setAttribute("aria-label", label("Checkout folder", "작업 폴더"));
    folder.textContent = label("Open folder…", "폴더 열기…");
    toggle.textContent = label("Terminal", "터미널");
    panel.setAttribute("aria-label", toggle.textContent);
    const row = groups.find(group => group.key === current?.key);
    if (row) {
      info.textContent = select.hidden
        ? label(row.messages+" messages · "+row.tasks+" tasks","메시지 "+row.messages+" · 작업 "+row.tasks)
        : label(row.tasks+" tasks","작업 "+row.tasks);
      if (!current.folder) info.textContent += label(" · History only", " · 기록 보기");
    }
  }
  async function catalog() {
    const snapshot = await api.projects();
    groups = snapshot.groups; current = snapshot.current;
    const byName = new Map();
    for (const g of groups) {
      const name = g.name.toLowerCase();
      if (!byName.has(name)) byName.set(name, []);
      byName.get(name).push(g.key.split(/[\\/]/));
    }
    names = new Map();
    select.replaceChildren(...groups.map(g => {
      const peers = byName.get(g.name.toLowerCase());
      const segments = g.key.split(/[\\/]/);
      let count = 1;
      while (count < segments.length && peers.filter(p => p.slice(-count).join("/") === segments.slice(-count).join("/")).length > 1)
        count++;
      const opt = new Option(count === 1 ? g.name : segments.slice(-count).join("/"), g.key);
      names.set(g.key,opt.text);
      opt.title = g.key;
      return opt;
    }));
    select.value = current.key;
    select.disabled = !groups.length;
    select.hidden = groups.length < 2; projectName.hidden = !select.hidden;
    select.title = current.key;
    const active = groups.find(g => g.key === current.key);
    checkout.replaceChildren(...(active?.checkouts || []).map(root => new Option(root, root)));
    const exact = active?.checkouts.find(root => root === current.folder) || current.folder;
    if (exact) checkout.value = exact;
    checkout.hidden = checkout.options.length < 2;
    toggle.disabled = !current.folder;
    labels(); updateMessageProjects();
    document.dispatchEvent(new CustomEvent("studio-catalog",{detail:active || {}}));
  }
  const alternatives = node("div","studio-message-projects");
  el("messages").after(alternatives);
  function updateMessageProjects() {
    const active = groups.find(g => g.key === current?.key);
    const others = groups.filter(g => g.messages > 0 && g.key !== current?.key);
    alternatives.hidden = !current || Boolean(active?.messages) || !others.length;
    if (alternatives.hidden) return;
    alternatives.replaceChildren(node("p","",label("Projects with messages","대화가 있는 프로젝트")),
      ...others.slice(0,5).map(g => {
        const link = node("button","qa-button",names.get(g.key)+" · "+g.messages);
        link.type = "button"; link.title = g.key; link.onclick = () => switchTo(g.key);
        return link;
      }));
  }
  async function switchTo(key, root) {
    try { await api.select(key, root); }
    catch (err) { await catalog(); error(err); }
  }
  select.onchange = () => switchTo(select.value);
  checkout.onchange = () => switchTo(current.key, checkout.value);
  // Refresh the options when the user returns from work in another app.
  window.addEventListener("focus", () => catalog().catch(error));
  let countSignature;
  document.addEventListener("memdex-state", () => {
    const signature = JSON.stringify([data.tasks.length,data.messages[0]?.id,data.messages.length,data.more]);
    if (signature !== countSignature) {
      countSignature = signature;
      catalog().catch(error);
    }
  });
  document.addEventListener("studio-chat-refresh", () => catalog().catch(error));
  folder.onclick = () => api.pickFolder().catch(error);
  function dimensions() {
    fit.fit();
    return [Math.max(2, term.cols), Math.max(1, term.rows)];
  }
  async function poll(version) {
    if (!attached || generation !== version) return;
    try {
      const result = await api.pollTerminal(cursor);
      if (generation !== version) return;
      cursor = result.cursor;
      if (result.data) term.write(result.data);
      if (result.exited) {
        attached = false;
        notice.textContent = label("Shell exited. Reopen Terminal to start again.", "셸이 종료됐어. 터미널을 다시 열면 시작해.");
      }
    } catch (err) { attached = false; error(err); }
    if (attached && !panel.hidden) timer = setTimeout(() => poll(version), 90);
  }
  toggle.onclick = async () => {
    if (busy) return;
    busy = true;
    const version = ++generation;
    try {
      if (!panel.hidden) {
        clearTimeout(timer); panel.hidden = true;
        toggle.setAttribute("aria-expanded", "false"); return;
      }
      panel.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      if (!term) {
        await document.fonts.load('14px "IBM Plex Mono"');
        term = new Terminal({ cursorBlink: !matchMedia("(prefers-reduced-motion: reduce)").matches,
          screenReaderMode: true, scrollback: 2000, fontFamily: '"IBM Plex Mono", "Pretendard", monospace', fontSize: 14,
          theme: { background: "#192436", foreground: "#f1f4fb", cursor: "#f3bc60" } });
        fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(host);
        term.onData(data => { if (attached) api.typeTerminal(data).catch(error); });
        new ResizeObserver(() => {
          if (attached && !panel.hidden) api.resizeTerminal(...dimensions()).catch(error);
        }).observe(host);
      }
      const { created } = await api.openTerminal(...dimensions());
      if (!attached || created) { cursor = 0; term.reset(); }
      attached = true; notice.textContent = ""; term.focus(); await poll(version);
    } catch (err) { error(err); panel.hidden = true; toggle.setAttribute("aria-expanded", "false"); }
    finally { busy = false; }
  };
  if (new URL(location.href).searchParams.has("history")) document.body.classList.add("studio-history");
  el("language").addEventListener("click", () => { labels(); updateMessageProjects(); });
  labels(); catalog().catch(error);
})();
