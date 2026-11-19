"use strict";

(() => {
  if (new URL(location.href).searchParams.has("history")) return;
  const api = window.memdexStudio, tr = (en, ko) => korean ? ko : en;
  const feedPane = document.querySelector("#message-workspace .feed");
  const readerPane = document.querySelector("#message-workspace .reader");
  const narrow = matchMedia("(max-width:790px)");
  const btn = cls => { const b=node("button",cls); b.type="button"; return b; };
  const newChat = btn("compose-new");
  feedPane.querySelector(".pane-label").append(newChat);

  const form = node("form","inline-compose"); form.id="chat-compose";
  const header=node("div","compose-heading"), heading=node("strong"), reset=btn("compose-new"), shared=node("span","compose-shared");
  heading.id="compose-heading"; form.setAttribute("aria-labelledby",heading.id); header.append(heading,shared,reset);
  const field=node("fieldset","compose-choices"), legend=node("legend","compose-sr"), families=node("div","compose-families");
  const changeMode=btn("compose-switch"), choices=node("div","compose-rows"), search=node("input","qa-search");
  search.type="search"; const targets=node("div","compose-targets"); targets.append(search,choices);
  const info=node("p","compose-info"), reload=btn("compose-switch");
  field.append(legend,families,targets,info,reload);
  const box=node("div","compose-box"), body=node("textarea"), send=node("button","qa-primary"), foot=node("div","compose-actions");
  body.id="compose-body"; body.rows=2; body.maxLength=12000; body.required=true;
  const keys=node("span","compose-keys");keys.id="compose-keys";body.setAttribute("aria-describedby",keys.id);
  send.type="submit";send.id="compose-send";foot.append(keys,send);box.append(body,foot);
  const notice=node("p","compose-error");notice.setAttribute("role","alert");
  form.append(header,field,box,notice);feedPane.append(form);

  let rows=[], dest=null, family="", direct=true, parent=null, fetching=false, busy=false, composing=false;
  let unavailable=false, loadId=0, storageKey="", drafts={}, preferred="model:claude", request=null;
  let preferredTask=null, wanted=null, wantedTask=null;
  let catalogSignature="";
  let newMembers=["model:claude"], changing=false, displayedMembers="";
  function members() {
    if (!parent) return newMembers;
    return data.participants?.[parent.root] ?? modelTargets(parent.to);
  }
  const draftKey=()=>String(parent?.root || 0);
  const groupName=agentName=>{
    const name=agentName.toLowerCase().split("@")[0].split("/")[0];
    return ({codex:"Codex",gpt:"Codex",claude:"Claude",dsh:"DeepSeek",deepseek:"DeepSeek",qwen:"Qwen",gemini:"Gemini"})[name.split("-")[0]] || name;
  };
  function remember() {
    if (!storageKey) return;
    const id=draftKey();
    if (body.value || request) drafts[id]={text:body.value,to:dest?.id || wanted,task:dest?.task ?? wantedTask,direct,family,request};
    else delete drafts[id];
    try {sessionStorage.setItem(storageKey,JSON.stringify({drafts,parent,preferred,preferredTask,newMembers}));} catch {}
  }
  function recipientLabel(row) {
    return [row.label || row.agent,row.task ? "#"+row.task+" "+row.title : ""].filter(Boolean).join(" · ");
  }
  function context(m) {
    if (!m) return null;
    const human = key => session(key)?.agent==="studio-user";
    const to=human(m.from) ? m.to : human(m.to) ? m.from : null;
    return to ? {root:m.chatRoot || m.id,id:m.id,to,task:m.task ?? null} : null;
  }
  function controls() {
    send.disabled=!storageKey || busy || fetching || !dest || changing || (direct && !members().length) || !body.value.trim();
    field.disabled=busy || fetching || changing;body.readOnly=busy;newChat.disabled=busy;reset.disabled=busy;
    send.textContent=busy ? tr("Sending…","전송 중…") : tr("Send","보내기");
  }
  function labels() {
    newChat.textContent=tr("+ New chat","+ 새 대화");reset.textContent=tr("New chat","새 대화");
    newChat.setAttribute("aria-controls",form.id);reset.hidden=!parent;
    const name=parent ? (direct ? members().map(modelName).join(" · ") : agent(parent.to)) : "";
    heading.textContent=parent ? (name ? tr("Reply to "+name,name+"에 답장") : tr("Continue chat","대화 이어쓰기")) : tr("New chat","새 대화");
    if (parent) heading.title=tr("Conversation #","대화 #")+parent.root;else heading.removeAttribute("title");
    shared.textContent=tr("Shared with project","프로젝트 공유");
    legend.textContent=direct ? tr("Participating AI","참여 AI") : tr("Session inbox","세션 수신함");
    legend.className=direct ? "compose-legend" : "compose-sr";
    body.setAttribute("aria-label",parent ? heading.textContent : tr("Project message","프로젝트 메시지"));
    body.placeholder=parent ? tr("Continue this chat…","이 대화에 이어서 입력하세요…") : tr("Write a project message…","프로젝트에 보낼 메시지를 입력하세요…");
    keys.textContent=tr("Shift+Enter for a new line","Shift+Enter 줄바꿈");
    search.placeholder=tr("Find a session or task","세션 또는 작업 검색");search.setAttribute("aria-label",search.placeholder);
    reload.textContent=tr("Refresh recipients","받는 AI 새로고침");changeMode.textContent=direct ? tr("Session inbox","세션 수신함") : tr("AI models","모델 선택");
    controls();
  }
  function chooseDest(row) {
    dest=row;wanted=row?.id || null;wantedTask=row?.task ?? null;
    if (row) {family=groupName(row.agent);direct=Boolean(row.direct);}
  }
  async function toggleMember(id) {
    if (changing || busy) return;
    const previous=members(), root=parent?.root;
    const next=(previous.includes(id) ? previous.filter(v=>v!==id) : [...previous,id]).sort();
    notice.textContent="";
    if (!root) newMembers=next;
    else {
      changing=true;controls();
      try {
        const saved=await api.write(api.token,"message/participants",{root,expected:previous,models:next});
        (data.participants ||= {})[root]=saved;
      } catch {
        notice.textContent=tr("Could not update participants. Check the current selection and try again.",
          "참여 AI를 변경하지 못했어요. 현재 선택을 확인하고 다시 시도하세요.");
      } finally {changing=false;void refresh();}
    }
    resolveTarget();remember();renderTargets();
  }
  function renderTargets() {
    const original=document.activeElement, focus=original?.dataset.family || original?.dataset.target;
    displayedMembers=JSON.stringify(members());
    const available=rows.filter(row=>Boolean(row.direct)===direct);
    const names=direct ? available.map(row=>row.id).sort((a,b)=>modelName(a)?.localeCompare(modelName(b)) || 0)
      : [...new Set(available.map(row=>groupName(row.agent)))].sort((a,b)=>a.localeCompare(b));
    const peers=available.filter(row=>groupName(row.agent)===family);
    const query=search.value.trim().toLocaleLowerCase();
    const matched=peers.filter(row=>[row.agent,recipientLabel(row),row.id].join(" ").toLocaleLowerCase().includes(query));
    families.replaceChildren(...names.map(name=>{
      const b=btn("compose-chip");b.textContent=direct ? modelName(name) || available.find(row=>row.id===name)?.agent : name;b.dataset.family=name;
      const model=direct && available.find(row=>row.id===name);
      b.setAttribute("aria-pressed",String(model ? members().includes(model.id) : family===name));
      b.onclick=()=>{
        if(model){void toggleMember(model.id);return;}
        const same=available.filter(row=>groupName(row.agent)===name);
        family=name;chooseDest(same.length===1 ? same[0] : null);search.value="";notice.textContent="";
        remember();renderTargets();if(dest)body.focus();
      };return b;
    }));
    changeMode.hidden=!!parent || !rows.some(row=>Boolean(row.direct)!==direct);families.append(changeMode);
    addModel.hidden=!direct;families.append(addModel);
    search.hidden=peers.length<5;targets.hidden=!!parent || peers.length<2;
    choices.replaceChildren(...matched.map(row=>{
      const b=btn("compose-row");b.dataset.target=row.id;b.setAttribute("aria-pressed",String(dest?.id===row.id));
      b.append(node("strong","",recipientLabel(row)),node("span","",row.id.startsWith("route:") ? row.agent+" · "+row.id.slice(-8) : row.id));
      b.onclick=()=>{chooseDest(row);notice.textContent="";remember();renderTargets();body.focus();};return b;
    }));
    families.hidden=!!parent && !direct;
    let hint="";
    if(fetching)hint=tr("Loading recipients…","받는 AI를 불러오는 중…");
    else if(unavailable)hint=tr("Could not load recipients.","받는 AI를 불러오지 못했어요.");
    else if(direct && !members().length)hint=tr("Choose an AI to send your next message.","다음 메시지를 보낼 AI를 선택하세요.");
    else if(direct)hint=tr("Selected AI can read this conversation and reply to your next message.","선택한 AI가 이 대화를 읽고 다음 메시지에 답해요.");
    else if(parent && !dest)hint=tr("This recipient is unavailable.","원래 받는 AI를 사용할 수 없어요.");
    else if(!direct && !available.length)hint=tr("No session inbox registered in this project.","이 프로젝트에 등록된 세션 수신함이 없어요.");
    else if(!dest && wanted)hint=tr("The previous recipient is unavailable. Choose a recipient.","이전에 선택한 수신함을 사용할 수 없어요. 받는 AI를 선택하세요.");
    else if(!direct && family && !matched.length)hint=tr("No matching session.","일치하는 세션이 없어요.");
    else if(!direct && dest)hint=recipientLabel(dest);
    info.textContent=hint;info.hidden=!hint;
    reload.hidden=!(unavailable || (!direct && !dest) || notice.textContent);
    field.hidden=!direct && !!parent && !!dest && !notice.textContent;
    labels();
    if(focus && !original.isConnected) field.querySelector((original.dataset.family ? "[data-family=" : "[data-target=")+'"'+CSS.escape(focus)+'"]')?.focus({preventScroll:true});
  }
  const addModel=btn("compose-switch"), dialog=node("dialog","model-dialog");
  const editor=node("form"), title=node("h2"), fields=node("fieldset"), rowActions=node("div","model-actions");
  const cancel=btn("qa-button"), save=node("button","qa-primary"), error=node("p","compose-error");
  title.id="model-dialog-title";dialog.setAttribute("aria-labelledby",title.id);error.setAttribute("role","alert");
  save.type="submit";save.id="custom-save";rowActions.append(cancel,save);
  function inputRow(id,tag) {
    const row=node("label"), caption=node("span"), input=node(tag);
    input.id=id;row.htmlFor=id;input.required=true;row.append(caption,input);fields.append(row);
    return {row,caption,input};
  }
  const display=inputRow("custom-name","input"), connection=inputRow("custom-provider","select"),
        modelId=inputRow("custom-id","input"), endpoint=inputRow("custom-base","input");
  display.input.maxLength=48;modelId.input.maxLength=160;endpoint.input.type="url";endpoint.input.maxLength=1024;
  const keyTip=node("p","model-help");
  fields.append(keyTip);editor.append(title,fields,error,rowActions);dialog.append(editor);document.body.append(dialog);
  function editorLabels() {
    addModel.textContent=tr("+ Add model","+ 모델 추가");title.textContent=tr("Add a project model","프로젝트 모델 추가");
    display.caption.textContent=tr("Display name","표시 이름");connection.caption.textContent=tr("Connection","연결 방식");
    modelId.caption.textContent=tr("Model ID","모델 ID");endpoint.caption.textContent=tr("API base URL","API 기본 URL");
    cancel.textContent=tr("Cancel","취소");save.textContent=tr("Add and select","추가하고 참여");
    if(!connection.input.options.length) {
      for(const [value,label] of [["ollama","Ollama"],["codex","Codex CLI"],["claude","Claude CLI"],["deepseek","DeepSeek Harness"],["compatible","Chat Completions API"]]) {
        const option=node("option","",label);option.value=value;connection.input.append(option);
      }
    }
    const provider=connection.input.value, remote=provider==="compatible";
    modelId.input.placeholder=({ollama:"qwen2.5:latest",claude:"sonnet",deepseek:"deepseek-flash"})[provider] || tr("Provider model ID","공급자가 지정한 모델 ID");
    endpoint.row.hidden=!remote;endpoint.input.required=remote;endpoint.input.disabled=!remote;
    endpoint.input.placeholder="http://localhost:1234/v1";
    if(remote)keyTip.textContent=tr("Include /v1 if required. For authentication, set MEMDEX_STUDIO_API_KEYS to a JSON object mapping the base URL to its key, then restart Studio.",
      "서버에 따라 주소에 /v1을 포함하세요. 인증이 필요하면 MEMDEX_STUDIO_API_KEYS 환경 변수에 기본 URL과 키를 JSON 객체로 지정한 뒤 Studio를 다시 여세요.");
    else keyTip.textContent=provider==="ollama" ? tr("Use the name of an installed Ollama chat model.","이 PC에 설치된 Ollama 채팅 모델 이름을 입력하세요.")
      : tr("Use a model available to your signed-in CLI account.","이 PC에서 로그인한 계정으로 사용할 수 있는 모델 ID를 입력하세요.");
  }
  let registering=false;
  addModel.onclick=()=>{editor.reset();error.textContent="";editorLabels();dialog.showModal();display.input.focus();};
  cancel.onclick=()=>dialog.close();connection.input.onchange=editorLabels;
  dialog.addEventListener("cancel",e=>{if(registering)e.preventDefault();});
  editor.onsubmit=async e=>{
    e.preventDefault();if(registering)return;
    const root=parent?.root || null;
    const config={name:display.input.value.trim(),provider:connection.input.value,model:modelId.input.value.trim(),base:endpoint.input.value.trim()};
    registering=true;fields.disabled=true;cancel.disabled=true;save.disabled=true;error.textContent="";
    try {
      const added=await api.write(api.token,"model/add",config);
      (data.customModels ||= {})[added.id]=added.name;
      await load();
      if(direct && (parent?.root || null)===root && !members().includes(added.id))await toggleMember(added.id);
      dialog.close();addModel.focus({preventScroll:true});
      if(!notice.textContent)el("feedback").textContent=tr("Model added: ","모델을 추가했어요 · ")+added.name;
    } catch(err) {
      const reason=String(err.message || err).replace(/^Error invoking remote method 'studio':\s*(?:Error:\s*)?/,"");
      const local={"This display name already exists.":"이미 사용하는 표시 이름이에요. 다른 이름을 입력하세요.",
        "Enter a display name of 1 to 48 characters.":"표시 이름은 1~48자로 입력하세요.",
        "Enter a model ID without spaces, such as qwen2.5:latest.":"모델 ID에 공백이나 지원하지 않는 문자가 있어요.",
        "Use an HTTPS base URL, or HTTP on localhost, without credentials, query or fragment.":"HTTPS 기본 주소를 입력하세요. 로컬 서버는 HTTP도 사용할 수 있어요. 주소에서 인증 정보와 쿼리를 빼 주세요."};
      error.textContent=korean && Object.hasOwn(local,reason) ? local[reason] : reason;
    } finally {registering=false;fields.disabled=false;cancel.disabled=false;save.disabled=false;}
  };
  editorLabels();
  function resolveTarget() {
    if(direct) {
      dest=rows.find(row=>row.direct && members().includes(row.id)) || null;
      return;
    }
    const id=parent?.to || wanted, task=parent?.task ?? wantedTask;
    dest=rows.find(row=>row.id===id && (row.task ?? null)===(task ?? null)) || null;
    if(dest){family=groupName(dest.agent);direct=Boolean(dest.direct);}
  }
  function withProjectModels(directory) {
    const custom=Object.entries(data.customModels || {}).map(([id, name])=>({
      id,agent:name,label:name,direct:true,task:null
    }));
    return directory.filter(row=>!row.id.startsWith("model:custom-")).concat(custom);
  }
  async function load() {
    const n=++loadId;fetching=true;unavailable=false;renderTargets();
    try {
      const result=await api.read(api.token,"message/recipients");
      if(n!==loadId)return;rows=withProjectModels(result);resolveTarget();
    } catch {if(n!==loadId)return;rows=[];dest=null;unavailable=true;}
    finally {if(n===loadId){fetching=false;renderTargets();}}
  }
  function restore(next) {
    parent=next;const saved=drafts[draftKey()];
    body.value=saved?.text || "";request=saved?.request || null;search.value="";notice.textContent="";
    wanted=next ? next.to : saved ? saved.to : preferred;wantedTask=next ? next.task : saved ? saved.task : preferredTask;
    direct=next ? Boolean(modelTargets(next.to).length) : (saved?.direct ?? wanted?.startsWith("model:") ?? true);
    family=saved?.family || "";resolveTarget();renderTargets();
  }
  function begin(next,focus=true) {
    if(busy)return;
    remember();restore(next);remember();
    if(focus)body.focus({preventScroll:true});
    void load();
  }
  function place() {
    const container=narrow.matches && document.body.dataset.pane==="reader" ? readerPane : feedPane;
    if(form.parentElement!==container)container.append(form);
  }
  new MutationObserver(place).observe(document.body,{attributes:true,attributeFilter:["data-pane"]});
  narrow.addEventListener("change",place);place();
  newChat.onclick=reset.onclick=()=>{
    studioNewChat=true;choose(null);pane("feed");render();place();begin(null);
  };
  document.addEventListener("studio-open-chat",e=>{place();begin(context(e.detail),false);});
  document.addEventListener("studio-scope-change",()=>{if(parent)begin(null,false);});
  async function reveal(id, stillCurrent=()=>true) {
    const result=await api.read(api.token,"thread/"+id);
    const m=result.messages.find(row=>row.id===id);
    if(!m) throw Error("Message unavailable");
    const state=await api.read(api.token,"state");
    if(!stillCurrent())return null;
    for(const s of state.sessions)if((s.key===m.from || s.key===m.to) && !session(s.key))data.sessions.push(s);
    history.set(id,{...m,body:m.body.replace(/\s+/g," ").slice(0,240)});
    studioNewChat=false;scope={type:"all"};el("search").value="";
    if (thread.length && thread[0].id===result.messages[0]?.id) await readThread(selected,false);
    else choose(id);
    render();void refresh();return m;
  }
  function setup() {
    if(storageKey || !data.projectKey)return;
    storageKey="memdex.inline-chat.v1:"+data.projectKey;
    let saved={};
    try {saved=JSON.parse(sessionStorage.getItem(storageKey)) || {};} catch {}
    if (Array.isArray(saved.newMembers)) newMembers=saved.newMembers.filter(modelName);
    drafts=saved.drafts || {};preferred=saved.preferred || preferred;preferredTask=saved.preferredTask ?? null;
    const latest=context(data.messages[0]);
    const initial=Object.hasOwn(saved,"parent") ? saved.parent : (modelTargets(latest?.to).length ? latest : null);
    studioNewChat=!initial;restore(initial);void load();
    if(parent) {
      const opening=parent.id;
      void reveal(opening,()=>parent?.id===opening).catch(()=>{if(parent?.id===opening)begin(null,false);});
    } else {choose(null);reader();}
  }
  // The answer to a pending question becomes the context for the next line in that same chat.
  new MutationObserver(()=>{
    if(!parent || busy)return;
    const reply=thread.findLast(m=>m.id>parent.id && context(m)?.root===parent.root && (direct || m.from===parent.to));
    if(reply){parent=context(reply);remember();}
  }).observe(el("reader"),{childList:true});
  document.addEventListener("memdex-state",()=>{
    const signature=JSON.stringify(data.customModels || {});
    const changed=catalogSignature!==signature;
    catalogSignature=signature;
    if(changed)rows=withProjectModels(rows);
    setup();
    if(direct && !changing && (changed || displayedMembers!==JSON.stringify(members()))) {resolveTarget();renderTargets();}
  });setup();
  window.addEventListener("pagehide",remember);
  changeMode.onclick=()=>{
    direct=!direct;chooseDest(null);family="";search.value="";notice.textContent="";
    if(direct)resolveTarget();
    else if(rows.filter(row=>!row.direct).length===1)chooseDest(rows.find(row=>!row.direct));
    remember();renderTargets();
  };
  reload.onclick=load;search.oninput=renderTargets;
  body.oninput=()=>{controls();remember();};
  body.oncompositionstart=()=>{composing=true;};body.oncompositionend=()=>{composing=false;};
  body.onkeydown=e=>{
    if(e.key!=="Enter" || e.shiftKey || e.altKey || e.isComposing || composing || e.keyCode===229)return;
    e.preventDefault();
    if(!e.repeat && !send.disabled)form.requestSubmit();
  };
  form.onsubmit=async e=>{
    e.preventDefault();
    if(send.disabled || composing)return;
    const owner=draftKey(),address=dest;
    const payload={to:address.id,task:address.task,reply:parent?.id || null,text:body.value.trim()};
    if(direct) payload.models=[...members()].sort();
    const signature=JSON.stringify(payload);
    if(request?.signature!==signature)request={signature,key:crypto.randomUUID()};
    busy=true;notice.textContent="";remember();controls();
    let receipt;
    try {
      receipt=await api.write(api.token,"message",{...payload,key:request.key});
    } catch(err) {
      const reason=String(err.message || "").replace(/^Error invoking remote method 'studio':\s*(?:Error:\s*)?/,"");
      notice.textContent=tr("Could not send. ","전송하지 못했어요. ")+(reason.includes("Recipient changed") ? tr("Refresh recipients and choose again.","받는 AI를 새로고침하고 다시 선택하세요.") : reason);
      busy=false;renderTargets();return;
    }
    delete drafts[owner];body.value="";request=null;preferred=address.id;preferredTask=address.task ?? null;
    parent={id:receipt.id,root:parent?.root || receipt.id,to:payload.models?.length>1 ? "models:"+payload.models.map(id=>id.slice(6)).join("+") : address.id,task:address.task};
    remember();renderTargets();
    try {
      const sent=await reveal(receipt.id);
      parent=context(sent) || parent;
      const reply=thread.findLast(m=>m.reply===sent.id && (payload.models?.includes(m.from) || m.from===address.id));
      if(reply)parent=context(reply) || parent;
      el("feedback").textContent=tr("Message sent.","메시지를 보냈어요.");
    } catch {el("feedback").textContent=tr("Sent #","#")+receipt.id+tr(". Reopen the chat to view it."," 전송 완료. 대화를 다시 열면 볼 수 있어요.");}
    finally {busy=false;remember();renderTargets();body.focus({preventScroll:true});}
  };
  el("language").addEventListener("click",()=>{editorLabels();renderTargets();});
  renderTargets();
})();
