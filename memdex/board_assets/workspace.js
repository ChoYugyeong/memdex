"use strict";

const el = id => document.getElementById(id);
const words = {
  skip:["Skip to messages","메시지로 이동"], explorer:["Explorer","탐색기"], messages:["Messages","메시지"],
  thread:["Conversation","대화 흐름"], tasks:["Task tree","작업 트리"], sessions:["AI sessions","AI 세션"],
  all:["All messages","전체 메시지"], older:["Load earlier messages","이전 메시지 더 보기"],
  pause:["Pause","일시정지"], resume:["Resume","다시 시작"], live:["Live","실시간"], paused:["Paused","일시정지됨"],
  retry:["Reconnecting","재연결 중"], loading:["Loading…","불러오는 중…"], failure:["Could not load. Try again.","불러오지 못했어요. 다시 시도하세요."],
  search:["Search message previews","메시지 미리보기 검색"], offline:["Snapshot","스냅샷"], emptyTasks:["No tasks yet.","등록된 작업이 없어요."],
  emptySessions:["No sessions yet.","등록된 세션이 없어요."], emptyMessages:["No messages recorded in Memdex yet.","Memdex로 주고받은 메시지가 아직 없어요."],
  noMatch:["No matching messages on the loaded pages.","불러온 메시지 중 일치하는 항목이 없어요."],
  select:["Select a message to read its conversation.","메시지를 선택하면 원문과 답장을 볼 수 있어요."],
  everyone:["Everyone","전체"], reply:["Reply to","답장 대상"], task:["Task","작업"], context:["Context at send time","발신 당시 작업 기록"],
  owner:["Owner","담당"], blocker:["Blocked by","차단 사유"], criterion:["Acceptance","완료 기준"],
  expand:["Expand","펼치기"], collapse:["Collapse","접기"], local:["Local workspace","로컬 작업 공간"],
  todo:["To do","할 일"], in_progress:["In progress","진행 중"], blocked:["Blocked","차단됨"],
  reported:["Reported","결과 보고"], verified:["Verified","검증 완료"], self_verified:["Self-verified","자체 검증"]
};
if (window.memdexStudio) Object.assign(words, {
  showReply:["View new reply","새 답장 보기"], newReply:["A new reply is available.","새 답장이 도착했어요."],
  all:["All chats","전체 대화"], messages:["Chat list","채팅 목록"], search:["Search project chats","프로젝트 대화 검색"],
  emptyMessages:["Start this project's first chat below.","아래 입력창에서 프로젝트의 첫 대화를 시작하세요."]
});
let korean = navigator.language.toLowerCase().startsWith("ko");
const t = key => words[key]?.[korean ? 1 : 0] ?? key;
const node = (tag, cls, text) => {
  const item = document.createElement(tag);
  if (cls) item.className = cls;
  if (text !== undefined) item.textContent = text;
  return item;
};
function button(cls, key, selected, action) {
  const item = node("button", cls);
  item.type = "button";
  item.dataset.key = key;
  item.setAttribute("aria-current", String(selected));
  item.addEventListener("click", action);
  return item;
}
const archive = el("archive") ? JSON.parse(el("archive").textContent) : null;
const desktopSession = el("desktop-session")?.value || window.memdexStudio?.token;
async function desktopAPI() {
  if (window.memdexStudio) return window.memdexStudio;
  if (!window.pywebview?.api?.read) {
    await new Promise(resolve => window.addEventListener("pywebviewready", resolve, {once:true}));
  }
  return window.pywebview.api;
}
const archivedById = new Map(), archivedReplies = new Map();
if (archive) {
  for (const m of archive.messages) {
    archivedById.set(m.id,m);
    if (!archivedReplies.has(m.reply)) archivedReplies.set(m.reply,[]);
    archivedReplies.get(m.reply).push(m);
  }
}
function archivedPage(endpoint) {
  const [action, rawId] = endpoint.split("/"), id = Number(rawId);
  if (action === "thread") {
    let root = archivedById.get(id);
    const visited = new Set();
    while (root && archivedById.has(root.reply) && !visited.has(root.id)) {
      visited.add(root.id); root = archivedById.get(root.reply);
    }
    const result = new Map(), pending = root ? [root] : [];
    while (pending.length) {
      const msg = pending.pop();
      if (result.has(msg.id)) continue;
      result.set(msg.id,msg);
      for (const child of archivedReplies.get(msg.id) || []) pending.push(child);
    }
    return {messages:[...result.values()].sort((a,b) => a.id-b.id)};
  }
  const rows = action === "history" ? archive.messages.filter(m => m.id < id) : archive.messages;
  return {project:archive.project, tasks:archive.tasks, sessions:archive.sessions,
          messages:rows.slice(0,archive.pageSize).map(m => ({...m, body:m.body.replace(/\s+/g," ").slice(0,240)})),
          more:rows.length > archive.pageSize};
}
async function request(endpoint) {
  if (archive) return archivedPage(endpoint);
  if (desktopSession) return (await desktopAPI()).read(desktopSession, endpoint);
  const response = await fetch("api/" + endpoint, {cache:"no-store"});
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}
let data = {tasks:[], sessions:[], messages:[], more:false}, history = new Map();
let scope = {type:"all"}, selected = null, thread = [], threadStatus = "select";
let more = false, paging = false, paused = false, status = "loading", lastState = "";
let threadRequest = 0, refreshing = false, findEarlier = false;
const folded = new Set();
let studioNewChat = false;
const directModels = {"model:claude":"Claude", "model:codex":"Codex", "model:qwen":"Qwen", "model:deepseek":"DeepSeek"};

function modelName(key) { return Object.hasOwn(directModels,key) ? directModels[key] : Object.hasOwn(data.customModels || {},key) ? data.customModels[key] : null; }
function modelTargets(key) {
  if (modelName(key)) return [key];
  if (!key?.startsWith("models:")) return [];
  return key.slice(7).split("+").map(name=>"model:"+name).filter(modelName);
}
function pane(name) {
  document.body.dataset.pane = name;
  document.querySelectorAll("[data-pane-target]").forEach(b =>
    b.setAttribute("aria-current", String(b.dataset.paneTarget === name)));
}
function setScope(type, key) {
  if (window.memdexStudio) studioNewChat=true;
  scope = {type, key}; choose(null); el("search").value = ""; el("feed-scroll").scrollTop = 0;
  pane("feed"); render();
  if (window.memdexStudio) document.dispatchEvent(new Event("studio-scope-change"));
}
function session(key) { return data.sessions.find(item => item.key === key); }
function agent(key) {
  if (!key) return t("everyone");
  if (modelTargets(key).length) return modelTargets(key).map(modelName).join(" · ");
  const name = session(key)?.agent || key;
  if (name === "studio-user") return korean ? "Studio 사용자" : "Studio user";
  return name.startsWith("route:") ? "…"+name.slice(-8) : name.split("@")[0];
}
function identity(key) {
  if (modelTargets(key).length) return modelTargets(key).map(modelName).join(" · ");
  const chat = session(key);
  if (chat?.agent === "studio-user") return agent(key);
  return chat && key.startsWith("route:") ? chat.agent + " · " + key.slice(-8) : key || t("everyone");
}
function date(stamp, full=false) {
  if (stamp == null) return "";
  return new Date(stamp * 1000).toLocaleString(korean ? "ko-KR" : undefined,
    {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit", ...(full ? {year:"numeric"} : {})});
}
function textUI() {
  document.documentElement.lang = korean ? "ko" : "en";
  document.querySelectorAll("[data-text]").forEach(item => {
    if (words[item.dataset.text]) item.textContent = t(item.dataset.text);
  });
  el("language").textContent = korean ? "EN" : "한국어";
  el("search").placeholder = t("search");
  el("search").setAttribute("aria-label", t("search"));
  el("project-status").textContent = t("local");
  showStatus();
}
function showStatus() {
  const current = archive ? "offline" : paused ? "paused" : status;
  if (el("connection").textContent !== t(current)) el("connection").textContent = t(current);
  el("connection").dataset.state = current;
  el("pause").textContent = t(paused ? "resume" : "pause");
  el("pause").hidden = Boolean(archive);
  el("pause").setAttribute("aria-pressed", String(paused));
}
function explorer() {
  el("all").setAttribute("aria-current", String(scope.type === "all"));
  el("task-count").textContent = data.tasks.length;
  const aiSessions = data.sessions.filter(chat => chat.agent !== "studio-user");
  el("session-count").textContent = aiSessions.length;
  const children = new Map(), ids = new Set(data.tasks.map(task => task.id));
  for (const task of data.tasks) {
    const parent = ids.has(task.parent_id) ? task.parent_id : null;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(task);
  }
  function tree(parent) {
    const ul = node("ul", "tree");
    for (const task of children.get(parent) || []) {
      const li = node("li"), line = node("div", "task-line");
      if (children.has(task.id)) {
        const closed = folded.has(task.id);
        const toggle = button("toggle", "fold-" + task.id, false, () => {
          if (closed) folded.delete(task.id); else folded.add(task.id);
          render();
        });
        toggle.textContent = closed ? "▸" : "▾";
        toggle.setAttribute("aria-label", t(closed ? "expand" : "collapse") + " " + task.title);
        toggle.setAttribute("aria-expanded", String(!closed)); line.append(toggle);
      } else {line.append(node("span","leaf"));}
      const item = button("nav-row", "task-" + task.id, scope.type === "task" && scope.key === task.id,
                          () => setScope("task", task.id));
      item.title = "#" + task.id + " · " + task.title + " · " + t(task.status);
      item.append(node("span","small","#"+task.id), node("span","nav-name",task.title));
      line.append(item); li.append(line);
      if (children.has(task.id) && !folded.has(task.id)) li.append(tree(task.id));
      ul.append(li);
    }
    return ul;
  }
  el("tasks").replaceChildren(data.tasks.length ? tree(null) : node("p","empty",t("emptyTasks")));
  const chats = aiSessions.map(chat => {
    const item = button("nav-row", "chat-" + chat.key, scope.type === "chat" && scope.key === chat.key,
                        () => setScope("chat", chat.key));
    const name = node("span","session-text");
    const sub = [chat.checkout, chat.task ? "#"+chat.task : "", chat.key.startsWith("route:") ? chat.key.slice(-8) : ""];
    name.append(node("span","nav-name",agent(chat.key)), node("span","secondary",sub.filter(Boolean).join(" · ")));
    item.title = [identity(chat.key), chat.label, date(chat.seen,true)].filter(Boolean).join(" · ");
    const dot = node("span", "dot");
    dot.dataset.seen = chat.seen || 0;
    item.append(dot, name);
    return item;
  });
  el("sessions").replaceChildren(...(chats.length ? chats : [node("p","empty",t("emptySessions"))]));
  presence();
}
function presence() {
  document.querySelectorAll(".dot[data-seen]").forEach(dot =>
    dot.classList.toggle("on", Date.now()/1000 - Number(dot.dataset.seen) < 600));
}
function feed() {
  let title = t("all"), details = "", taskIds;
  if (scope.type === "chat") {
    const chat = session(scope.key);
    title = agent(scope.key);
    details = [identity(scope.key), chat?.label, chat?.task ? t("task")+" #"+chat.task : ""].filter(Boolean).join(" · ");
  } else if (scope.type === "task") {
    const task = data.tasks.find(item => item.id === scope.key);
    if (task) {
      title = "#"+task.id+" "+task.title;
      details = [t(task.status), task.owner && t("owner")+" · "+task.owner, task.blocker && t("blocker")+" · "+task.blocker,
                 task.criterion && t("criterion")+" · "+task.criterion].filter(Boolean).join("\n");
    }
    taskIds = new Set([scope.key]);
    const descendants = [scope.key];
    while (descendants.length) {
      const parent = descendants.pop();
      for (const item of data.tasks) if (item.parent_id === parent && !taskIds.has(item.id)) {
        taskIds.add(item.id); descendants.push(item.id);
      }
    }
  }
  el("scope").textContent = title; el("scope-info").textContent = details; el("scope-info").hidden = !details;
  const query = el("search").value.trim().toLowerCase();
  let messages = [...history.values()].filter(m => {
    if (scope.type === "chat" && m.from !== scope.key && m.to !== scope.key) return false;
    if (taskIds && !taskIds.has(m.task)) return false;
    return !query || [m.body, m.subject || "", "#"+m.id, m.chatRoot ? "#"+m.chatRoot : "", identity(m.from), identity(m.to)].join(" ").toLowerCase().includes(query);
  }).sort((a,b) => b.id-a.id);
  const isStudio = Boolean(window.memdexStudio);
  const groupId = m => m?.chatRoot ?? m?.id;
  const selectedGroup = groupId(history.get(selected) || thread.find(row=>row.id===selected));
  if (isStudio) {
    const groups = new Map();
    for (const m of messages) if (!groups.has(groupId(m))) groups.set(groupId(m),m);
    messages = [...groups.values()];
  }
  // A search can hide the current chat. Only an explicit choice changes its reply destination.
  const needsSelection = isStudio ? selected===null : !messages.some(m=>m.id===selected);
  if (!studioNewChat && needsSelection) choose(messages[0]?.id ?? null);
  findEarlier = (scope.type !== "all" || Boolean(query)) && !messages.length && more;
  const items = messages.map(m => {
    const item = button("message", "msg-"+(isStudio ? groupId(m) : m.id), m.id === selected || (isStudio && groupId(m)===selectedGroup), () => {
      studioNewChat=false;choose(m.id); pane("reader"); render();
      if (isStudio) document.dispatchEvent(new CustomEvent("studio-open-chat",{detail:m}));
    });
    const line = node("span","line");
    line.append(node("span","pair",agent(m.from)+" → "+agent(m.to)),node("time","meta",date(m.time)));
    if (isStudio) {
      item.append(node("strong","chat-subject",m.subject || m.body),line);
      if (m.reply) item.append(node("span","preview",m.body));
    } else item.append(line, node("span","preview",m.body), node("span","small","#"+m.id));
    if (m.task) item.append(node("span","tag",t("task")+" #"+m.task));
    if (m.reply && !isStudio) item.append(node("span","tag","↳ #"+m.reply));
    return item;
  });
  el("messages").replaceChildren(...(items.length ? items : [node("p","empty",t(findEarlier ? "loading" : history.size ? "noMatch" : "emptyMessages"))]));
  el("feed-count").textContent = messages.length;
  el("older").hidden = !more; el("older").disabled = paging;
  if (findEarlier && !paging && !el("message-workspace").hidden) void loadOlder();
}
function reader() {
  el("thread-count").textContent = thread.length || "";
  if (!thread.length) {el("reader").replaceChildren(node("p","empty",studioNewChat ?
    (korean ? "메시지를 보내면 여기에 대화가 표시돼요." : "Send a message to start this chat.") : t(threadStatus))); return;}
  const openContext = new Set([...el("reader").querySelectorAll("details[open]")].map(d => d.dataset.id));
  const root = thread[0];
  const items = [node("h2","thread-title",agent(root.from)+" → "+agent(root.to))];
  for (const m of thread) {
    const entry = node("article","entry"+(m.id === selected ? " selected" : ""));
    entry.id = "entry-"+m.id;
    entry.append(node("div","small","#"+m.id+" · "+date(m.time,true)+(m.reply ? " · "+t("reply")+" #"+m.reply : "")),
                 node("h3","",identity(m.from)+" → "+identity(m.to)), node("pre","",m.body));
    if (m.context) {
      const ctx = node("details"); ctx.dataset.id = String(m.id); ctx.open = openContext.has(String(m.id));
      ctx.append(node("summary","",t("context")), node("pre","",m.context)); entry.append(ctx);
    }
    if (m.auto) modelProgress(entry, m, m.auto);
    for (const [model, state] of Object.entries(m.modelStatuses || {})) modelProgress(entry, m, state, model);
    items.push(entry);
  }
  el("reader").replaceChildren(...items);
}
const transcript = el("reader"), jumpToReply = el("new-reply");
// Measure the old layout before replacing the entries. Each window follows independently.
const atTranscriptEnd = () => transcript.scrollHeight - transcript.clientHeight - transcript.scrollTop <= 2;
let unreadReplies = 0;
function setUnreadReplies(count) {
  unreadReplies = count;
  jumpToReply.hidden = !count;
  const label = korean ? "새 답장 "+count+"개 보기" : "View "+count+" new "+(count===1 ? "reply" : "replies");
  if (count) jumpToReply.textContent = label;
  const live = el("reply-announcement");
  const announcement = count ? label : "";
  if (live.textContent !== announcement) live.textContent = announcement;
}
function scrollToLatest() {
  transcript.scrollTop = transcript.scrollHeight;
  setUnreadReplies(0);
}
if (window.memdexStudio) {
  jumpToReply.onclick = () => {
    scrollToLatest();
    const latest = transcript.lastElementChild;
    if (latest) {latest.tabIndex = -1; latest.focus({preventScroll:true});}
  };
  transcript.addEventListener("scroll", () => {
    if (transcript.clientHeight && atTranscriptEnd()) setUnreadReplies(0);
  }, {passive:true});
}
function updateThread(messages) {
  const previous = thread;
  thread = messages;
  if (!window.memdexStudio || !previous.length || previous[0].id !== messages[0]?.id) {
    reader(); return;
  }
  const visible = transcript.clientHeight > 0;
  const following = visible && atTranscriptEnd();
  const top = transcript.getBoundingClientRect().top;
  const anchor = visible && !following && [...transcript.children].find(entry =>
    entry.id && entry.getBoundingClientRect().bottom > top);
  const oldY = anchor && anchor.getBoundingClientRect().top;
  reader();
  if (following) {scrollToLatest(); return;}
  // A waiting/error label above the reader's place can change height in the same update.
  const restored = anchor && el(anchor.id);
  if (restored) transcript.scrollTop += restored.getBoundingClientRect().top - oldY;
  const lastId = previous.at(-1).id;
  const arrived = messages.filter(m => m.id > lastId && m.reply && m.from !== "studio-user" && session(m.from)?.agent !== "studio-user").length;
  if (arrived) setUnreadReplies(unreadReplies + arrived);
}
function modelProgress(entry, m, {state,error,attempt}, model) {
  if (state === "completed") return;
  const pending = state === "running" || state === "queued";
  const label = pending ? (korean ? "답변을 기다리는 중" : "Waiting for a reply") :
    state === "truncated" ? (korean ? "응답 길이 한도에 도달했어요" : "Reply reached its length limit") :
    (korean ? "응답을 받지 못했어요" : "Could not get a reply");
  const status = node("div","model-status");
  const text = node("p","",agent(model || m.to)+" · "+label+(error ? " — "+error : ""));
  text.setAttribute("role","status");status.append(text);
  if (["failed","interrupted"].includes(state) && window.memdexStudio && !new URL(location.href).searchParams.has("history")) {
    const retry = node("button","qa-button",korean ? "이 모델 다시 시도" : "Retry this model");
    retry.type="button";
    retry.onclick=async()=>{
      retry.disabled=true;
      try {
        await window.memdexStudio.write(desktopSession,"message/retry",{id:m.id,attempt,model});
        if (selected!==null) await readThread(selected,false);
        void refresh();
      } catch(err) {
        text.textContent=err.message; retry.disabled=false;
      }
    };
    status.append(retry);
  }
  entry.append(status);
}
async function readThread(id, scroll) {
  const generation = ++threadRequest;
  try {
    const result = await request("thread/"+id);
    if (generation !== threadRequest || selected !== id) return;
    threadStatus = "select";
    if (JSON.stringify(result.messages) !== JSON.stringify(thread)) updateThread(result.messages);
    if (scroll) {
      if (window.memdexStudio && id === thread.at(-1)?.id) scrollToLatest();
      else el("entry-"+id)?.scrollIntoView({block:"nearest"});
    }
  } catch {
    if (generation === threadRequest) {threadStatus = "failure"; reader();}
  }
}
function choose(id) {
  if (selected === id) return;
  setUnreadReplies(0);
  selected = id; thread = []; threadStatus = id === null ? "select" : "loading"; ++threadRequest;
  reader();
  if (id !== null) void readThread(id, true);
}
function render() {
  const focused = document.activeElement?.dataset.key;
  explorer(); feed(); reader();
  if (focused) document.querySelector('[data-key="'+CSS.escape(focused)+'"]')?.focus({preventScroll:true});
}
async function refresh() {
  if (refreshing || paused) return;
  refreshing = true;
  try {
    const next = await request("state"); if (paused) return;
    status = "live";
    const signature = JSON.stringify(next);
    if (signature !== lastState) {
      const previous = selected;
      const overlap = next.messages.some(m => history.has(m.id));
      if (!overlap || !next.more) {history = new Map(); more = next.more;}
      for (const m of next.messages) history.set(m.id,m);
      data = next; lastState = signature; el("project").textContent = data.project;
      document.dispatchEvent(new Event("memdex-state"));
      document.title = data.project + " · Memdex"; render();
      if (selected !== null && selected === previous) void readThread(selected, false);
    } else if (selected !== null && (threadStatus === "failure" || thread.some(m=>[m.auto,...Object.values(m.modelStatuses || {})].some(s=>["queued","running"].includes(s?.state)))))
      void readThread(selected, false);
  } catch { status = "retry"; }
  finally {refreshing = false; showStatus(); if (!paused) presence();}
}
async function loadOlder() {
  if (paging || !history.size) return;
  paging = true; el("older").disabled = true;
  const ids = [...history.keys()], oldest = ids.reduce((a,b) => Math.min(a,b));
  try {
    const page = await request("history/"+oldest);
    if (!history.has(oldest)) {findEarlier = false; return;}
    for (const m of page.messages) history.set(m.id,m);
    more = page.more && page.messages.some(m => m.id < oldest); el("feedback").textContent = ""; render();
  } catch {findEarlier = false; el("feedback").textContent = t("failure");}
  finally {paging = false; el("older").disabled = false;}
  if (findEarlier && !el("message-workspace").hidden) void loadOlder();
}
el("older").onclick = loadOlder;
el("all").onclick = () => setScope("all");
el("search").oninput = () => {feed(); reader();};
el("pause").onclick = () => {paused = !paused; showStatus(); if (!paused) void refresh();};
el("language").onclick = () => {korean = !korean; textUI(); setUnreadReplies(unreadReplies); render();};
document.querySelectorAll("[data-pane-target]").forEach(b => b.onclick = () => pane(b.dataset.paneTarget));
document.addEventListener("keydown", event => {
  if (event.key === "/" && el("qa-workspace").hidden && !event.defaultPrevented &&
      !event.target.closest?.("input,textarea,select,[contenteditable]:not([contenteditable=false]),.xterm") &&
      !event.ctrlKey && !event.metaKey && !event.altKey && !event.isComposing) {
    event.preventDefault(); pane("feed"); el("search").focus();
  }
});
textUI(); pane("feed"); reader(); void refresh();
if (!archive) setInterval(refresh, 2000);
