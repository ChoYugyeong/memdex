"use strict";

Object.assign(words, {
  qaMessages:["Project messages","프로젝트 대화 보기"], qaTaskList:["Project tasks","프로젝트 작업"], qaCompose:["New review","검토 작성"], qaTaskSearch:["Search tasks by name or number","작업 이름 또는 번호 검색"],
  qaNoMatches:["No matches","검색 결과가 없어요"], qaNoFiles:["No reviewable files in this folder.","이 폴더에 검토할 파일이 없어요."],
  qaMoreFiles:["Show more files","파일 더 보기"], qaFileSearch:["Search file paths","파일 경로 검색"],
  qaQuestionHint:["What should the reviewers check?","세 모델이 무엇을 확인하면 될까요?"],
  qaBegin:["Create a task to start a review.","새 작업을 만들면 검토를 시작할 수 있어요."],
  qaNoProject:["Open a folder or choose a project with history.","폴더를 열거나 기록이 있는 프로젝트를 선택하세요."],
  qaSelectFiles:["Select at least one file.","검토할 파일을 하나 이상 선택하세요."],
  qaView:["Review studio","QA 스튜디오"], qaSetup:["Review setup","검토 설정"], qaNewTask:["Create a task","새 작업 만들기"],
  qaTaskTitle:["Task title","작업 이름"], qaCreate:["Create task","작업 만들기"], qaSkill:["Review skill","검토 스킬"],
  qaFiles:["Project files","프로젝트 파일"], qaAddFile:["Add file","파일 추가"], qaQuestion:["Review question","검토할 질문"],
  qaPayload:["All three reviewers receive the task, question, selected files and skill instructions.",
             "작업·질문·선택한 파일과 스킬을 Claude, Qwen, DeepSeek에 보냅니다."],
  qaStart:["Request three reviews","세 모델에 검토 요청"], qaCompare:["Compare reviews","모델별 검토 결과"],
  qaHistory:["Review history","검토 기록"], qaDefault:["General code review","기본 코드 검토"],
  qaSkillHelp:["About skills","스킬 사용 안내"], qaRequest:["Review request","검토 요청"],
  qaOriginal:["Original response","원문 보기"], qaRecord:["Record a change","작업에 기록"], qaHide:["Hide","접기"],
  qaSkip:["Skip to selected task","선택한 작업으로 이동"],
  qaPartial:["partial","일부 응답"], qaPending:["in progress","진행 중"], qaIssues:["need attention","확인 필요"],
  qaHint:["Registered skills require an approved bundle. Use memdex skills scan / approve.",
          "등록 스킬은 승인된 버전을 사용해요. CLI에서 memdex skills scan / approve로 관리하세요."],
  qaChoose:["Choose a task","작업 선택"], qaNoRuns:["No reviews for this task","이 작업에 검토 기록이 없어요"],
  qaFileHint:["Find a file in this checkout","현재 작업 폴더의 파일 찾기"],
  qaPickFile:["Choose 1–20 files from the project list.","프로젝트 목록에서 파일을 1~20개 선택하세요."],
  qaRemove:["Remove file","파일 빼기"], qaIdle:["Awaiting request","요청 전"], queued:["Queued","요청 대기"],
  running:["Reviewing","검토 중"], completed:["Completed","응답 완료"], failed:["Failed","실패"],
  interrupted:["Interrupted","중단됨"], truncated:["Partial answer","응답 일부"],
  qaAttempt:["Attempt","시도"], qaRetry:["Retry this reviewer","이 모델만 재시도"],
  qaEarlier:["Previous attempts","지난 시도"], qaNote:["Applied change and test result","반영한 변경과 테스트 결과"],
  qaSave:["Record on task","작업에 기록"], qaRecorded:["Recorded on task","작업에 기록됨"],
  qaUnknown:["Could not reach the workspace. Retry to recover the same request.","연결을 확인하고 다시 누르면 같은 요청을 복구해요."]
});

const qaProviders = {claude:"Claude", qwen:"Qwen", deepseek:"DeepSeek"};
let qaOptions = null, qaFiles = [], qaRuns = [], qaRun = null, qaTask = 0;
let qaBusy = false, qaLoading = false, qaSerial = 0, qaDraft = null;
const qaCards = new Map();
let qaTaskSignature = "", qaHistorySignature = "", qaContextSignature = "";

const qaDrafts = new Map();
let qaMode = null, qaSkill = "", fileLimit = 40;
const qaReadOnly = Boolean(archive || (desktopSession && new URL(location.href).searchParams.has("history")));
function qaTaskList(prefer) {
  const tasks = data.tasks;
  const selected = tasks.find(task => task.id === prefer) || tasks[0];
  const id = selected?.id || 0;
  if (id !== qaTask) {
    if (qaTask) qaDrafts.set(qaTask, {question:el("qa-question").value, files:qaFiles, skill:qaSkill, draft:qaDraft});
    const saved = qaDrafts.get(id);
    qaTask = id; qaMode = null; qaRun = null; qaRuns = [];
    qaFiles = saved?.files || []; qaSkill = saved?.skill || ""; qaDraft = saved?.draft || null;
    el("qa-question").value = saved?.question || "";
    el("qa-file").value = ""; fileLimit = 40;
    for (const name of ["qa-error","qa-file-error","qa-load-error"]) el(name).textContent = "";
    qaHistory(); qaSkills(); qaFileList(); qaResults();
  }
  const search = el("qa-task-search");
  search.hidden = tasks.length < 6 && !search.value;
  search.placeholder = t("qaTaskSearch"); search.setAttribute("aria-label",search.placeholder);
  const query = search.value.trim().toLocaleLowerCase();
  const signature = JSON.stringify([korean,id,query,tasks.map(item => [item.id,item.title,item.status])]);
  if (signature !== qaTaskSignature) {
    const active = document.activeElement?.dataset.task;
    const rows = tasks.filter(item => ("#"+item.id+" "+item.title).toLocaleLowerCase().includes(query)).map(item => {
      const row = node("button","qa-task-row"); row.type = "button"; row.dataset.task = item.id;
      row.setAttribute("aria-current",String(id === item.id));
      row.append(node("span","qa-row-meta","#"+item.id+" · "+t(item.status)),node("span","qa-row-title",item.title));
      row.onclick = () => {
        qaTaskList(item.id);
        if (compactQA.matches) {qaSettings(false); el("qa-heading").focus();}
        void qaLoad().catch(qaReport);
      };
      return row;
    });
    el("qa-task").replaceChildren(...(rows.length ? rows : [node("p","qa-empty",t(query ? "qaNoMatches" : "emptyTasks"))]));
    if (active) el("qa-task").querySelector('[data-task="'+active+'"]')?.focus({preventScroll:true});
    qaTaskSignature = signature;
  }
  el("qa-task-count").textContent = tasks.length;
  el("qa-task-meta").textContent = selected ? "#"+id+" · "+t(selected.status) : "";
  el("qa-heading").textContent = selected?.title || t("qaView");
  el("qa-acceptance").textContent = selected?.criterion || (selected ? "" : t(qaReadOnly ? "qaNoProject" : "qaBegin"));
  el("qa-new-task").hidden = qaReadOnly;
  qaButtons(); qaPanels();
}
function qaWritable() {
  const task = data.tasks.find(item => item.id === qaTask);
  return !qaReadOnly && task && !["verified","self_verified"].includes(task.status);
}
function qaPanels() {
  const writable = qaWritable();
  const compose = writable && (qaMode === "compose" || (!qaMode && !qaRun));
  el("qa-modes").hidden = !qaTask;
  el("qa-compose").hidden = !writable;
  el("qa-compose").setAttribute("aria-pressed",String(Boolean(compose)));
  el("qa-compare").setAttribute("aria-pressed",String(!compose));
  el("qa-form").hidden = !compose;
  el("qa-output").hidden = !qaTask || Boolean(compose);
  el("qa-run-count").textContent = qaRuns.length;
  el("qa-history").hidden = qaRuns.length < 2;
  el("qa-cards").hidden = !qaRun;
  el("qa-empty").hidden = Boolean(qaRun);
  el("qa-empty").textContent = t("qaNoRuns");
}
function qaButtons() { el("qa-start").disabled = qaBusy || !qaOptions || !qaWritable(); }
function qaSkills() {
  const approved = (qaOptions?.skills || []).filter(s => s.approved);
  if (qaOptions && !approved.some(s => String(s.id) === qaSkill)) qaSkill = "";
  el("qa-skill").replaceChildren(...[{id:"",name:t("qaDefault")},...approved].map(skill => {
    const label = node("label","qa-choice"), radio = node("input");
    radio.type = "radio"; radio.name = "skill"; radio.value = skill.id; radio.checked = String(skill.id) === qaSkill;
    radio.onchange = () => {qaSkill = radio.value;};
    label.append(radio,node("span","",skill.agent ? skill.name+" · "+skill.agent : skill.name)); return label;
  }));
  el("qa-question").placeholder = t("qaQuestionHint");
  el("qa-file").placeholder = t("qaFileSearch"); el("qa-file").setAttribute("aria-label",t("qaFileSearch"));
}
function qaFileOptions() {
  const all = qaOptions?.files || [];
  const query = el("qa-file").value.trim().toLocaleLowerCase();
  const matches = all.filter(path => path.toLocaleLowerCase().includes(query));
  const rows = matches.slice(0,fileLimit).map(path => {
    const row = node("label","qa-file-row"), check = node("input");
    check.type = "checkbox"; check.value = path; check.checked = qaFiles.includes(path);
    check.onchange = () => {
      if (check.checked && qaFiles.length >= 20) {
        check.checked = false; el("qa-file-error").textContent = t("qaPickFile"); return;
      }
      qaFiles = check.checked ? [...qaFiles,path] : qaFiles.filter(item => item !== path);
      el("qa-file-error").textContent = ""; qaFileTags();
    };
    row.append(check,node("span","",path)); return row;
  });
  if (matches.length > fileLimit) {
    const more = node("button","qa-button",t("qaMoreFiles")); more.type = "button";
    more.onclick = () => {const first = fileLimit; fileLimit += 40; qaFileOptions(); el("qa-file-options").querySelectorAll("input")[first]?.focus();};
    rows.push(more);
  }
  el("qa-file-options").replaceChildren(...(rows.length ? rows : [node("p","qa-empty",t(all.length ? "qaNoMatches" : "qaNoFiles"))]));
}
function qaFileTags() {
  el("qa-file-count").textContent = korean ? qaFiles.length+"개 선택" : qaFiles.length+" selected";
  el("qa-files").replaceChildren(...qaFiles.map(file => {
    const li = node("li"), remove = node("button","","×  "+file);
    remove.type = "button"; remove.setAttribute("aria-label",t("qaRemove")+" "+file);
    remove.onclick = () => {
      qaFiles = qaFiles.filter(f => f !== file); el("qa-file-error").textContent = ""; qaFileList(); el("qa-file").focus();
    };
    li.append(remove); return li;
  }));
}
function qaFileList() {qaFileOptions(); qaFileTags();}
async function qaPost(action, body) {
  if (desktopSession) return (await desktopAPI()).write(desktopSession, action, body);
  const response = await fetch("api/qa/"+action,{method:"POST",headers:{"Content-Type":"application/json"},
                                                body:JSON.stringify(body),cache:"no-store"});
  if (!response.ok) throw new Error((await response.text()) || t("qaUnknown"));
  return response.json();
}
async function qaAction(fn, errorId="qa-error") {
  if (qaBusy || archive) return;
  qaBusy = true; qaButtons(); el(errorId).textContent = "";
  const controls = el(errorId).closest("form,.qa-card")?.querySelectorAll('button[type="submit"],button.qa-button') || [];
  for (const control of controls) control.disabled = true;
  try {await fn();}
  catch (err) {
    const target = el(errorId);
    if (target) target.textContent = err instanceof TypeError ? t("qaUnknown") : err.message;
  }
  finally {for (const control of controls) control.disabled = false; qaBusy = false; qaButtons();}
}
// Render only text and code. Provider output never supplies HTML, links or DOM attributes.
function qaFormattedText(value) {
  const block = node("div","qa-prose");
  let paragraph = [], code = null;
  function flush() {
    if (!paragraph.length) return;
    const line = node("p");
    for (const part of paragraph.join("\n").split(/(`[^`\n]+`)/g)) {
      if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) line.append(node("code","",part.slice(1,-1)));
      else line.append(document.createTextNode(part));
    }
    block.append(line); paragraph = [];
  }
  for (const line of value.split(/\r?\n/)) {
    if (/^\s*```[\w.+-]*\s*$/.test(line)) {
      if (code === null) { flush(); code = []; }
      else { block.append(node("pre","qa-code",code.join("\n"))); code = null; }
    } else if (code !== null) code.push(line);
    else if (!line.trim()) flush();
    else paragraph.push(line);
  }
  if (code !== null) block.append(node("pre","qa-code",code.join("\n")));
  flush();
  return block;
}
function qaAnswer(text) {
  const answer = node("div","qa-answer");
  let fields;
  if (text.trimStart().startsWith("{")) {
    try { fields = Object.entries(JSON.parse(text)); } catch { /* Plain response. */ }
  }
  if (fields?.length && fields.every(([key, value]) => key && typeof value === "string")) {
    const list = node("dl","qa-answer-fields");
    for (const [key,value] of fields) {
      const entry = node("dd"); entry.append(qaFormattedText(value));
      list.append(node("dt","",key),entry);
    }
    answer.append(list);
  } else answer.append(qaFormattedText(text));
  return answer;
}
function qaCard(provider, attempts, runId) {
  const latest = attempts.at(-1), state = latest?.state || "qaIdle";
  const card = node("article","qa-card"); card.dataset.state = state;
  card.dataset.provider = provider;
  const header = node("header","qa-card-head"), title = node("div","qa-provider");
  const mark = node("span","qa-mark",qaProviders[provider][0]);
  mark.setAttribute("aria-hidden","true");
  title.append(mark,node("h2","",qaProviders[provider]));
  if (latest) title.append(node("small","qa-attempt",t("qaAttempt")+" "+latest.attempt));
  header.append(title,node("span","qa-state",t(state))); card.append(header);
  if (!latest) return card;
  const errorId = "qa-job-error-"+latest.id;
  if (latest.result) card.append(qaAnswer(latest.result));
  if (!qaReadOnly && ["failed","interrupted"].includes(state)) {
    const retry = node("button","qa-button",t("qaRetry")); retry.type = "button";
    retry.onclick = () => qaAction(async () => {
      await qaPost("retry",{run:runId,job:latest.id}); await qaLoad();
    },errorId);
    card.append(retry);
  }
  if (attempts.length > 1) {
    const earlier = node("details","qa-earlier"); earlier.append(node("summary","",t("qaEarlier")+" ("+(attempts.length-1)+")"));
    for (const old of attempts.slice(0,-1)) earlier.append(node("pre","",
      "#"+old.id+" · "+t(old.state)+"\n"+(old.result || "")));
    card.append(earlier);
  }
  if (latest.result && ["completed","truncated"].includes(state)) {
    const source = node("details","qa-source"); source.append(node("summary","",t("qaOriginal")),
      node("pre","qa-verbatim",latest.result)); card.append(source);
  }
  if (latest.evidence) {
    card.append(node("p","qa-evidence",t("qaRecorded")+"\n"+latest.evidence));
  } else if (!qaReadOnly && ["completed","truncated"].includes(state)) {
    const form = node("form"), label = node("label","qa-field"), input = node("textarea");
    input.required = true; input.maxLength = 2000; input.rows = 2;
    label.append(node("span","",t("qaNote")), input);
    const save = node("button","qa-button",t("qaSave")); save.type = "submit";
    form.append(label,save);
    form.onsubmit = event => {event.preventDefault(); void qaAction(async () => {
      await qaPost("evidence",{run:runId,job:latest.id,note:input.value.trim()}); await qaLoad();
    },errorId);};
    const record = node("details","qa-record");
    record.append(node("summary","",t("qaRecord")),form); card.append(record);
  }
  const error = node("p","qa-job-error");
  error.id = errorId; error.setAttribute("role","alert"); card.append(error);
  return card;
}
function qaResults() {
  el("qa-summary").hidden = !qaRun;
  const context = JSON.stringify([korean,qaRun?.id,qaRun?.question,qaRun?.files,qaRun?.skill]);
  if (context !== qaContextSignature && qaRun) {
    el("qa-prompt").textContent = qaRun.question;
    el("qa-context").replaceChildren(node("span","qa-rubric",qaRun.skill || t("qaDefault")),
      ...qaRun.files.map(file => node("code","qa-file-tag",file)));
  }
  qaContextSignature = context;
  const states = Object.keys(qaProviders).map(p => qaRun?.attempts.filter(a=>a.provider===p).at(-1)?.state);
  const complete = states.filter(s=>s==="completed").length;
  let summary = qaRun ? (korean ? "응답 완료 "+complete+"/3" : complete+"/3 completed") : "";
  for (const [label,matches] of [["qaPartial",["truncated"]],["qaIssues",["failed","interrupted"]],["qaPending",["queued","running"]]]) {
    const count = states.filter(s=>matches.includes(s)).length;
    if (count) summary += " · "+(korean ? t(label)+" "+count : count+" "+t(label));
  }
  if (el("qa-progress").textContent !== summary) el("qa-progress").textContent = summary;
  for (const provider of Object.keys(qaProviders)) {
    const attempts = (qaRun?.attempts || []).filter(job => job.provider === provider);
    const key = JSON.stringify([korean,qaRun?.id,attempts]);
    const old = qaCards.get(provider);
    if (old?.key === key) continue;
    const card = qaCard(provider,attempts,qaRun?.id);
    if (old) {
      if (old.job === attempts.at(-1)?.id && old.run === qaRun?.id) {
        for (const detail of old.card.querySelectorAll("details[open]")) {
          const next = card.getElementsByClassName(detail.className)[0];
          if (next) next.open = true;
        }
        const draft = old.card.querySelector("textarea"), nextDraft = card.querySelector("textarea");
        if (draft && nextDraft) nextDraft.value = draft.value;
      }
      old.card.replaceWith(card);
    } else el("qa-cards").append(card);
    qaCards.set(provider,{key,card,job:attempts.at(-1)?.id,run:qaRun?.id});
  }
}
function qaHistory(selectedId) {
  const signature = JSON.stringify([korean,qaRuns.map(run => [run.id,run.created,run.question])]);
  if (signature !== qaHistorySignature) {
    el("qa-runs").replaceChildren(...qaRuns.map(run => {
      const row = node("button","qa-run-row"); row.type = "button"; row.dataset.run = run.id;
      row.append(node("span","qa-row-meta","#"+run.id+" · "+date(run.created)),node("span","qa-row-title",run.question));
      row.onclick = () => {void qaLoad(run.id).catch(qaReport);};
      return row;
    }));
    qaHistorySignature = signature;
  }
  for (const row of el("qa-runs").children) {
    const current = String(Number(row.dataset.run) === selectedId);
    if (row.getAttribute("aria-current") !== current) row.setAttribute("aria-current",current);
  }
}
async function qaLoad(prefer) {
  const generation = ++qaSerial, task = qaTask;
  qaLoading = true;
  try {
    const list = !task ? [] : archive ? (archive.reviews || []).filter(r => r.task === task) :
      await request("qa/history/"+task);
    if (generation !== qaSerial) return;
    const chosen = prefer ?? qaRun?.id;
    const active = list.find(r => r.id === chosen) || list[0];
    const run = !active ? null : archive ? active : await request("qa/run/"+active.id);
    if (generation !== qaSerial) return;
    qaRuns = list; qaRun = run; qaHistory(run?.id); qaResults(); qaPanels();
    el("qa-load-error").textContent = "";
    const target = "#qa"+(task ? "/"+task : "")+(run ? "/"+run.id : "");
    if (!desktopSession && !el("qa-workspace").hidden && location.hash !== target) window.history.replaceState(null,"",target);
  } finally {if (generation === qaSerial) qaLoading = false;}
}
function qaReport(err) {el("qa-load-error").textContent = err.message || t("qaUnknown");}
async function qaOpen(savedTask, savedRun) {
  document.body.classList.add("qa-open");
  const skip = document.querySelector(".skip");
  skip.href = "#qa-heading"; skip.dataset.text = "qaSkip"; skip.textContent = t("qaSkip");
  el("message-workspace").hidden = true; el("qa-workspace").hidden = false;
  el("view-qa").setAttribute("aria-current","true"); el("view-messages").setAttribute("aria-current","false");
  qaResults();
  try {
    const initial = await request("state");
    data.tasks = initial.tasks;
    const nextTask = savedTask || (scope.type === "task" ? scope.key : qaTask);
    qaTaskList(nextTask);
    if (!qaReadOnly) {
      qaOptions = await request("qa/options");
      if (!data.tasks.length && !document.body.classList.contains("studio-history")) {
        qaSettings(true); el("qa-new-task").open = true;
      }
      qaSkills(); qaFileList(); qaButtons();
    }
    await qaLoad(savedRun);
  } catch (err) {qaReport(err);}
}
el("view-qa").onclick = () => qaOpen();
document.addEventListener("memdex-state",() => {
  if (el("qa-workspace").hidden) return;
  const selected = qaTask;
  qaTaskList(selected);
  if (selected !== qaTask) {
    qaRun = null; qaRuns = []; qaHistory(); qaResults();
    void qaLoad().catch(qaReport);
  }
});
const compactQA = window.matchMedia("(max-width: 880px)");
function qaSettings(show, moveFocus=false) {
  el("qa-workspace").classList.toggle("settings-closed",!show);
  el("qa-settings").setAttribute("aria-expanded",String(show));
  if (moveFocus && show) (el("qa-task").querySelector('[aria-current="true"]') || el("qa-new-task").querySelector("summary")).focus();
}
qaSettings(!compactQA.matches);
compactQA.addEventListener("change",event => qaSettings(!event.matches));
el("qa-settings").onclick = () => qaSettings(el("qa-settings").getAttribute("aria-expanded")==="false",true);
el("qa-close-settings").onclick = () => {qaSettings(false); el("qa-settings").focus();};
el("view-messages").onclick = () => {
  document.body.classList.remove("qa-open");
  const skip = document.querySelector(".skip");
  skip.href = "#search"; skip.dataset.text = "skip"; skip.textContent = t("skip");
  el("message-workspace").hidden = false; el("qa-workspace").hidden = true;
  el("view-qa").setAttribute("aria-current","false"); el("view-messages").setAttribute("aria-current","true");
  render();
  if (location.hash.startsWith("#qa")) window.history.replaceState(null,"",location.href.split("#")[0]);
};
el("qa-conversations").onclick = () => {
  el("view-messages").click(); setScope("all"); el("search").focus();
};
el("qa-task-search").oninput = () => qaTaskList(qaTask);
el("qa-compose").onclick = () => {qaMode = "compose"; qaPanels(); el("qa-question").focus();};
el("qa-compare").onclick = () => {qaMode = "results"; qaPanels();};
el("qa-file").oninput = () => {fileLimit = 40; qaFileOptions();};
el("qa-file").onkeydown = event => {if (event.key === "Enter") {event.preventDefault(); el("qa-file-options").querySelector("input")?.focus();}};
el("qa-task-form").onsubmit = event => {event.preventDefault(); void qaAction(async () => {
  const res = await qaPost("task",{title:el("qa-title").value,criterion:el("qa-criterion").value});
  el("qa-task-search").value = ""; data.tasks = (await request("state")).tasks; qaTaskList(res.id); el("qa-new-task").open = false;
  el("qa-task-form").reset(); await qaLoad(); if (compactQA.matches) qaSettings(false); el("qa-question").focus();
},"qa-task-error");};
el("qa-form").onsubmit = event => {event.preventDefault(); void qaAction(async () => {
  if (!qaWritable()) return;
  if (!el("qa-question").value.trim()) {el("qa-question").focus(); throw new Error(t("qaQuestionHint"));}
  if (!qaFiles.length) {el("qa-file-error").textContent = t("qaSelectFiles"); el("qa-file").focus(); return;}
  const skill = qaOptions.skills.find(s => String(s.id) === qaSkill);
  const input = {task:qaTask,files:[...qaFiles],question:el("qa-question").value.trim(),
                 skill:skill?.id ?? null,skill_sha:skill?.sha ?? null};
  const signature = JSON.stringify(input);
  if (qaDraft?.signature !== signature) {
    const token = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2,"0")).join("");
    qaDraft = {signature,token};
  }
  const result = await qaPost("start",{...input,token:qaDraft.token});
  if (qaTask === input.task) {qaDraft = null; qaMode = "results"; await qaLoad(result.id); el("qa-compare").focus();}
  else if (qaDrafts.get(input.task)?.draft?.signature === signature) qaDrafts.get(input.task).draft = null;
});};
el("language").addEventListener("click",() => {
  if (!el("qa-workspace").hidden) {qaTaskList(qaTask); qaSkills(); qaFileList(); qaHistory(qaRun?.id); qaResults(); qaPanels();}
});
textUI();
const qaLocation = /^#qa(?:\/(\d+)(?:\/(\d+))?)?$/.exec(location.hash);
if (qaLocation) void qaOpen(Number(qaLocation[1]) || undefined, Number(qaLocation[2]) || undefined);
else if (desktopSession && new URL(location.href).searchParams.get("pane") !== "messages") void qaOpen();
if (!archive) setInterval(() => {
  if (!el("qa-workspace").hidden && !qaBusy && !qaLoading && !paused) void qaLoad().catch(qaReport);
},2000);
