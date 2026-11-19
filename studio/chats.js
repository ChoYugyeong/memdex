"use strict";

(() => {
  const api = window.memdexStudio;
  const msg = (en, ko) => korean ? ko : en;
  const sources = {memdex:"Memdex", codex:"Codex", claude:"Claude"};
  const workspace = el("message-workspace");
  const bar = node("nav","chat-sources");
  workspace.before(bar);
  const buttons = {};
  for (const [key,name] of Object.entries(sources)) {
    const button = node("button","",name); button.type = "button"; button.dataset.source = key;
    button.onclick = () => {chosen = true; selectSource(key);};
    bar.append(button); buttons[key] = button;
  }

  const library = node("section","chat-library");
  const sidebar = node("section","chat-index"), detail = node("section","chat-detail");
  const heading = node("h2"), search = node("input","qa-search"), list = node("div","chat-list scroll");
  const refresh = node("button","qa-button"), top = node("header","chat-index-head");
  search.type = "search"; refresh.type = "button"; refresh.onclick = () => loadList(true);
  top.append(heading,refresh); sidebar.append(top,search,list);
  const back = node("button","qa-button chat-back"), title = node("h2","chat-title");
  back.type = "button"; title.tabIndex = -1;
  const detailHead = node("header","chat-detail-head"), transcript = node("div","chat-transcript scroll");
  const turns = node("div"), info = node("p","chat-info"), more = node("button","qa-button chat-more");
  info.setAttribute("role","status"); more.type = "button";
  detailHead.append(back,title); transcript.append(turns,info,more); detail.append(detailHead,transcript);
  library.append(sidebar,detail); workspace.append(library); library.hidden = true;

  let source = "memdex", chosen = false, groups = {}, records = [], selectedChat = null;
  let listVersion = 0, version = 0, loading = false, next = null, status = "", skipped = 0;
  let listState = "idle", listError = "", readError = "", loaded = false;
  let firstProject = !new URL(location.href).searchParams.has("pane");
  el("view-qa").addEventListener("click",() => {firstProject = false;});
  const mobile = matchMedia("(max-width:790px)");
  const displayName = row => row.title || sources[row.source]+" · "+row.id.split(":")[1].slice(0,8);
  function labels() {
    el("view-messages").textContent = msg("Conversations","대화");
    el("qa-conversations").textContent = msg("Project conversations","프로젝트 대화 보기");
    bar.setAttribute("aria-label",msg("Conversation source","대화 출처"));
    for (const [key,button] of Object.entries(buttons)) {
      const count = key === "memdex" ? groups.messages : groups[key];
      const text = sources[key]+(count == null ? "" : " · "+count);
      if (button.textContent !== text) button.textContent = text;
      button.setAttribute("aria-pressed",String(source===key));
    }
    heading.textContent = sources[source]+msg(" chats"," 채팅");
    refresh.textContent = msg("Refresh","새로고침");
    search.placeholder = msg("Search chat titles","채팅 제목 검색"); search.setAttribute("aria-label",search.placeholder);
    back.textContent = msg("Chat list","채팅 목록");
    more.textContent = readError ? msg("Try again","다시 읽기") : msg("Read more","다음 내용 읽기");
    library.setAttribute("aria-label",heading.textContent);
    sidebar.setAttribute("aria-label",msg("Chat list","채팅 목록")); detail.setAttribute("aria-label",msg("Conversation","대화 본문"));
  }
  function renderList() {
    if (source === "memdex") return;
    const query = search.value.trim().toLocaleLowerCase();
    const rows = records.filter(r => r.source === source && displayName(r).toLocaleLowerCase().includes(query));
    const focus = document.activeElement?.dataset.chat;
    const items = rows.map(row => {
      const b = node("button","chat-row"); b.type = "button"; b.dataset.chat = row.id;
      b.setAttribute("aria-current",String(selectedChat?.id === row.id));
      b.append(node("span","chat-name",displayName(row)),node("time","chat-date",date(row.seen,true)));
      b.onclick = () => openChat(row,true);
      return b;
    });
    let empty;
    if (listState === "loading") empty = t("loading");
    else if (listState === "error") empty = listError;
    else empty = query ? msg("No matching titles.","일치하는 채팅 제목이 없어요.") :
      msg("No local "+sources[source]+" chats for this project.","이 프로젝트에 저장된 "+sources[source]+" 채팅이 없어요.");
    list.replaceChildren(...(items.length ? items : [node("p","chat-info",empty)]));
    if (focus) list.querySelector('[data-chat="'+CSS.escape(focus)+'"]')?.focus({preventScroll:true});
  }
  function renderStatus() {
    more.hidden = !selectedChat || (next === null && !readError);
    more.disabled = loading;
    let note = "";
    if (readError) note = readError;
    else if (loading) note = t("loading");
    else if (status==="empty") note = msg("No user or assistant text in this chat.","이 채팅에 표시할 사용자·어시스턴트 텍스트가 없어요.");
    else if (status==="continue") note = msg("More records are available.","뒤에 이어지는 기록이 있어요.");
    if (skipped) note += " "+msg("Large records skipped: ","크기가 큰 기록을 건너뜀 · ")+skipped;
    info.textContent = note.trim(); info.hidden = !info.textContent;
    more.textContent = readError ? msg("Try again","다시 읽기") : msg("Read more","다음 내용 읽기");
    title.textContent = selectedChat ? displayName(selectedChat) : msg("Select a chat","채팅을 선택하세요");
  }
  function renderTurn(turn) {
    const article = node("article","chat-turn"), header = node("header");
    article.dataset.role = turn.role;
    const who = node("strong","",turn.role === "user" ? msg("You","사용자") : sources[source]);
    const when = new Date(turn.time);
    header.append(who);
    if (turn.time && Number.isFinite(when.valueOf())) header.append(node("time","chat-date",date(when.valueOf()/1000,true)));
    article.append(header,node("p","chat-body",turn.text));
    return article;
  }
  async function readPage() {
    if (!selectedChat || loading) return;
    const ticket = version, chat = selectedChat;
    loading = true; readError = ""; renderStatus();
    try {
      // Pages with only tool records need no extra click before the first visible turn.
      for (let pages=0; pages<5; pages++) {
        const page = await api.read(api.token,"ide/chat/"+chat.id+"/"+(next || 0));
        if (ticket !== version) return;
        turns.append(...page.messages.map(renderTurn));
        skipped += page.skipped; next = page.next;
        if (page.messages.length || next === null) break;
      }
      status = next !== null ? "continue" : turns.childElementCount ? "" : "empty";
    } catch {
      if (ticket === version) readError = msg("Could not read this chat. Refresh the list if it moved.",
                                             "채팅을 읽지 못했어요. 파일이 이동했다면 목록을 새로고침하세요.");
    } finally {
      if (ticket === version) {loading = false; renderStatus();}
    }
  }
  function clearChat() {
    version++; selectedChat = null; next = null; loading = false; skipped = 0; status = ""; readError = "";
    turns.replaceChildren(); renderStatus(); library.classList.remove("reading");
  }
  function openChat(row, focus=false) {
    clearChat(); selectedChat = row; renderList(); renderStatus();
    library.classList.add("reading"); transcript.scrollTop = 0;
    if (focus) title.focus({preventScroll:true});
    void readPage();
  }
  async function loadList(force=false) {
    const ticket = ++listVersion;
    listState = "loading"; refresh.disabled = true; renderList();
    try {
      const data = await api.read(api.token,force ? "ide/refresh" : "ide/chats");
      if (ticket !== listVersion) return;
      records = data; loaded = true; listState = "ready";
      groups = {...groups,codex:data.filter(r=>r.source==="codex").length,claude:data.filter(r=>r.source==="claude").length};
      labels();
      const selected = records.find(r=>r.id===selectedChat?.id);
      if (selected && force) openChat(selected);
      else if (!selected) {
        clearChat();
        const first=records.find(r=>r.source===source);
        if (first && !mobile.matches && !search.value) openChat(first);
      }
      renderList();
      if (force) document.dispatchEvent(new Event("studio-chat-refresh"));
    } catch {
      if (ticket !== listVersion) return;
      loaded = false; listState = "error";
      listError = msg("Could not load chats. Try Refresh.","채팅 목록을 읽지 못했어요. 새로고침으로 다시 시도하세요.");
      renderList();
    } finally {if (ticket === listVersion) refresh.disabled = false;}
  }
  function selectSource(key) {
    source = key; listVersion++; refresh.disabled = false;
    document.body.classList.toggle("studio-ide",key!=="memdex");
    library.hidden = key==="memdex"; search.value = "";
    clearChat(); labels();
    if (source !== "memdex") {
      renderList(); void loadList();
    }
  }
  search.oninput = renderList; more.onclick = readPage;
  back.onclick = () => {library.classList.remove("reading"); (list.querySelector('[aria-current="true"]') || search).focus();};
  document.addEventListener("studio-catalog",event => {
    groups = event.detail;
    if (firstProject && groups.key) {
      firstProject = false;
      if (!groups.tasks && (groups.messages || groups.codex || groups.claude)) el("view-messages").click();
    }
    if (!chosen && groups.key && !el("message-workspace").hidden) defaultSource();
    labels();
  });
  function defaultSource() {
    const liveProject = !new URL(location.href).searchParams.has("history");
    const best = liveProject || groups.messages || !(groups.codex || groups.claude) ? "memdex" : groups.codex ? "codex" : "claude";
    if (source !== best) selectSource(best);
  }
  el("view-messages").addEventListener("click",() => {
    if (!chosen) defaultSource();
    if (source!=="memdex" && !loaded && listState!=="loading") void loadList();
  });
  el("qa-conversations").addEventListener("click",() => {if (source!=="memdex") search.focus();});
  el("language").addEventListener("click",() => {
    labels(); renderList(); renderStatus();
    for (const elem of turns.querySelectorAll('[data-role="user"] strong')) elem.textContent = msg("You","사용자");
  });
  document.addEventListener("keydown",e => {
    if (source!=="memdex" && !workspace.hidden && e.key==="/" && !e.isComposing &&
        !e.ctrlKey && !e.metaKey && !e.altKey && !e.target.closest("input,textarea,[contenteditable=true],.xterm")) {
      e.preventDefault(); e.stopPropagation(); library.classList.remove("reading"); search.focus();
    }
  },true);
  labels(); renderStatus();
})();
