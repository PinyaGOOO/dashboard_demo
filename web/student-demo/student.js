"use strict";
(function studentPortal() {
  const $ = (selector, root = document) => root.querySelector(selector);
  const view = $("#view"), notice = $("#notice");
  const demoProfiles = {student01: "DemoLab01!", student02: "DemoLab02!"};
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
  let student = null, assignments = [], current = null, page = "", timer = 0, epoch = 0, activeRequest = 0;
  const pending = new Set(), marks = new Map();

  async function api(path, data) {
    const options = {credentials:"same-origin", cache:"no-store", headers:{Accept:"application/json"}};
    if (data !== undefined) Object.assign(options, {method:"POST", headers:{...options.headers,"Content-Type":"application/json"}, body:JSON.stringify(data)});
    let response;
    try { response = await fetch(`/student-demo/api/${path}`, options); }
    catch { throw new Error("Нет связи с Deployer. Проверьте подключение и повторите попытку."); }
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const message = typeof body?.error === "string" ? body.error : "Не удалось выполнить запрос. Повторите попытку.";
      throw Object.assign(new Error(message), {status:response.status});
    }
    if (!body) throw new Error("Сервис вернул неожиданный ответ. Повторите попытку.");
    return body;
  }
  function warn(message, retry = false) {
    notice.replaceChildren(); notice.hidden = !message;
    if (!message) return;
    notice.append(document.createTextNode(message + " "));
    if (retry) {
      const button = document.createElement("button"); button.type = "button"; button.className = "button secondary small";
      button.textContent = "Повторить"; button.addEventListener("click", () => refresh(true)); notice.append(button);
    }
  }
  function announce(message) { $("#announcer").textContent = message; }
  function focusPage() {
    $("#main").focus({preventScroll:true});
    window.scrollTo({top:0, behavior:"instant"});
  }
  function identity() {
    $("#account-name").hidden = !student; $("#logout").hidden = !student;
    $("#account-name").textContent = student ? student.name : "";
  }
  function forget() {
    ++epoch; ++activeRequest; clearTimeout(timer); student = null; assignments = []; current = null;
    page = ""; pending.clear(); marks.clear(); identity();
  }
  function heroArt() {
    return `<svg class="lab-art" viewBox="0 0 440 132" aria-hidden="true" fill="none">
      <path d="M2 109h99l32-19m113-26h65l33-21h91M224 95h77l24 14h91M2 69h72l28 17" stroke="#594963" stroke-dasharray="4 5"/>
      <g stroke-linejoin="round"><path d="m100 89 78-43 78 43-78 43z" fill="#211c28" stroke="#58455f"/><path d="m100 73 78-43 78 43-78 43z" fill="#2e2238" stroke="#89689c"/>
      <path d="m100 55 78-43 78 43-78 43z" fill="#413049" stroke="#d8b5f2"/><path d="m141 55 37-20 37 20-37 20z" stroke="#d8b5f2"/><path d="m165 54 11-6 15 8-11 6z" fill="#d8b5f2"/></g>
      <g fill="#d9b7fb"><circle cx="344" cy="43" r="3"/><circle cx="74" cy="69" r="3"/><circle cx="325" cy="109" r="3"/></g></svg>`;
  }
  function loginPage() {
    if (page === "login") return;
    page = "login"; document.title = "Вход в практикум · Deployer";
    view.innerHTML = `<div class="login-layout"><section class="login-intro"><p class="eyebrow">Deployer / Учебная лаборатория</p>
      <h1>Личный<br><span class="accent">практикум.</span></h1>
      <p class="lead">Получайте задания и разворачивайте собственные стенды по учебным шаблонам.</p>${heroArt()}</section>
      <section class="login-card"><h2>Вход студента</h2><p class="muted">Используйте один из двух учебных профилей.</p>
      <form id="login-form"><label class="field">Логин<input name="login" id="login" autocomplete="username" autocapitalize="none" spellcheck="false" required></label>
      <label class="field">Пароль<input name="password" id="password" type="password" autocomplete="current-password" required></label>
      <button type="submit" class="button primary">Войти в кабинет</button></form>
      <div class="demo-accounts"><strong>Попробовать демо</strong><p>Нажмите на логин, чтобы подставить учебные данные. У каждого профиля свои задания и стенды.</p>
      <div class="demo-fill">${Object.keys(demoProfiles).map(id => `<button type="button" data-demo-login="${id}">${id}</button>`).join("")}</div></div></section></div>`;
  }
  function card(a, index) {
    return `<article class="assignment" data-assignment-id="${esc(a.id)}"><div class="card-top"><span class="eyebrow">Практика / ${String(index + 1).padStart(2,"0")}</span><span class="status" data-status></span></div>
      <h3>${esc(a.title)}</h3><p class="brief">${esc(a.description)}</p>
      <p class="template-line">Шаблон: <b>${esc(a.template_name)}</b> · VMID ${esc(a.template_vmid)}</p>
      <details><summary>Условия задания</summary><ol class="steps">${a.steps.map(step => `<li>${esc(step)}</li>`).join("")}</ol></details>
      <div class="actionbox"><div class="progress-wrap" hidden><span data-phase></span><progress max="100" value="0" aria-label="Подготовка стенда"></progress></div>
      <button type="button" class="button primary" data-deploy>Развернуть стенд</button>
      <a class="button primary" data-open-stand hidden>Открыть стенд <span aria-hidden="true">→</span></a></div></article>`;
  }
  function updateStatus(root, stand) {
    const status = $("[data-status]",root), progress = $("progress",root), wrap = progress.parentElement;
    const ready = stand?.status === "ready", working = stand?.status === "provisioning";
    status.className = `status ${ready ? "ready" : working ? "provisioning" : ""}`;
    status.textContent = ready ? "● Стенд готов" : working ? "◌ Идёт подготовка" : "Стенд не создан";
    wrap.hidden = !working;
    if (working) {
      const percent = Math.max(0,Math.min(100,Number(stand.progress) || 0));
      progress.value = percent; $("[data-phase]",root).textContent = `${stand.phase || "Подготовка"} · ${percent}%`;
    }
  }
  function dashboard(force = false) {
    const first = page !== "assignments";
    if (first || force) {
      page = "assignments"; current = null; document.title = "Мои задания · Deployer";
      view.innerHTML = `<section class="hero"><p class="eyebrow">Кабинет студента / ${esc(student.group)}</p><h1>Мои <span class="accent">задания.</span></h1>
        <p class="lead">${esc(student.name)}, выберите практическую работу. Стенд будет закреплён за вашим профилем и этим заданием.</p>
        <div class="hero-stats"><span>Назначено заданий: <strong>${assignments.length}</strong></span><span>Стенды готовы: <strong id="ready-count">0</strong></span></div></section>
        <div class="section-head"><h2>Практические работы</h2></div>
        ${assignments.length ? `<section class="assignment-grid" aria-label="Список заданий">${assignments.map(card).join("")}</section>` : `<div class="empty">Пока у вас нет назначенных работ.</div>`}`;
    }
    $("#ready-count").textContent = assignments.filter(a => a.stand?.status === "ready").length;
    const cards = view.querySelectorAll("[data-assignment-id]");
    assignments.forEach((a,i) => {
      const root = cards[i]; if (!root) return;
      const button = $("[data-deploy]",root), link = $("[data-open-stand]",root), s = a.stand;
      updateStatus(root,s);
      button.hidden = !!s; button.disabled = pending.has(a.id); button.textContent = button.disabled ? "Отправляем запрос…" : "Развернуть стенд";
      link.hidden = s?.status !== "ready";
      if (!link.hidden) link.href = `#stands/${encodeURIComponent(s.id)}`;
    });
  }
  function standPage(result) {
    const a = result.assignment, s = result.stand, key = `stand:${s.id}`;
    if (page !== key) {
      page = key; document.title = `${s.name} · Практикум`;
      const marked = marks.get(String(s.id)) || new Set();
      view.innerHTML = `<nav class="crumbs" aria-label="Навигация"><a href="#assignments">← Мои задания</a><span>/</span><span>Стенд</span></nav>
        <header class="hero"><p class="eyebrow">Практикум / Персональный стенд</p><h1>${esc(s.name)}</h1><p class="lead">${esc(student.name)} · ${esc(student.group)}</p></header>
        <div class="stand-grid"><article class="sheet"><p class="eyebrow">Приложенное задание</p><h2>${esc(a.title)}</h2><p class="brief">${esc(a.description)}</p>
        <h3>Порядок работы</h3><div class="checklist">${a.steps.map((step,i) => `<label><input type="checkbox" data-step="${i}" ${marked.has(i) ? "checked" : ""}><span>${esc(step)}</span></label>`).join("")}</div>
        <p class="check-hint">Это личные заметки: отметки сохраняются в этой вкладке до обновления страницы или выхода.</p>
        <button type="button" id="download-assignment" class="button secondary">Скачать задание (.txt)</button></article>
        <aside class="stand-panel"><p class="eyebrow">Среда</p><h2>Учебный стенд</h2><dl>
        <div><dt>Статус</dt><dd data-status></dd></div><div><dt>Шаблон</dt><dd>${esc(a.template_name)}</dd></div>
        <div><dt>VMID шаблона</dt><dd>${esc(a.template_vmid)}</dd></div><div><dt>Владелец</dt><dd>${esc(student.login)}</dd></div></dl>
        <div class="stand-progress" hidden><span data-phase></span><progress max="100" value="0" aria-label="Подготовка стенда"></progress></div>
        <p class="stand-note">Это симулятор развёртывания. Виртуальная машина в Proxmox не создаётся; консоль и сетевые сервисы здесь не запущены.</p></aside></div>`;
    }
    current = result; updateStatus($(".stand-panel"),s);
  }
  const routeId = () => {
    const match = /^#stands\/([^/]+)$/.exec(location.hash);
    return match ? match[1] : "";
  };
  function schedule() {
    clearTimeout(timer);
    if (!document.hidden && student && (page.startsWith("stand:") ? current?.stand.status === "provisioning" : page === "assignments" && assignments.some(a => a.stand?.status === "provisioning"))) {
      timer = setTimeout(() => refresh(), 1250);
    }
  }
  async function refresh(userAction = false) {
    const generation = epoch, request = ++activeRequest, id = routeId();
    const relevant = () => generation === epoch && request === activeRequest && routeId() === id;
    try {
      const old = assignments, info = await api("me"); if (!relevant()) return;
      if (student && student.login !== info.student.login) { marks.clear(); page = ""; current = null; pending.clear(); }
      student = info.student; assignments = info.assignments; identity();
      if (id) {
        try {
          const result = await api(`stands/${id}`); if (!relevant()) return; standPage(result);
        } catch (error) {
          if (error.status !== 404 && error.status !== 403) throw error;
          if (!relevant()) return;
          current = null; page = "unavailable"; document.title = "Стенд недоступен · Deployer";
          view.innerHTML = `<div class="empty"><h2>Стенд недоступен</h2><p>Он не найден среди ваших стендов.</p><a class="button secondary" href="#assignments">Вернуться к заданиям</a></div>`;
        }
      } else {
        const structure = list => JSON.stringify(list.map(({stand,...rest}) => rest));
        dashboard(structure(old) !== structure(assignments));
        if (old.some(a => a.stand?.status === "provisioning" && assignments.some(x => x.id === a.id && x.stand?.status === "ready"))) announce("Ваш стенд готов.");
      }
      warn(""); schedule();
    } catch (error) {
      if (!relevant()) return;
      if (error.status === 401) {
        const wasSignedIn = !!student; forget(); loginPage(); warn(wasSignedIn ? "Сессия завершена. Войдите в кабинет снова." : "");
      } else { if (!page) loginPage(); warn(error.message,true); schedule(); }
    }
  }
  view.addEventListener("submit", async event => {
    if (event.target.id !== "login-form") return;
    event.preventDefault();
    const form = event.target, submit = $("button[type=submit]",form), generation = epoch;
    if (submit.disabled) return; submit.disabled = true; warn("");
    try {
      await api("login",{login:form.elements.login.value.trim(),password:form.elements.password.value});
      if (generation !== epoch) return;
      form.elements.password.value = ""; await refresh(true);
      if (student) { focusPage(); announce("Вы вошли в кабинет."); }
    } catch (error) {
      if (generation === epoch) warn(error.status === 401 ? "Неверный логин или пароль. Выберите учебный профиль или проверьте данные." : error.message);
    } finally { submit.disabled = false; }
  });
  view.addEventListener("click",async event => {
    const fill = event.target.closest("[data-demo-login]");
    if (fill) {
      const id = fill.dataset.demoLogin; $("#login").value = id; $("#password").value = demoProfiles[id];
      $("#login-form button[type=submit]").focus(); announce(`Учебный профиль ${id} выбран.`); return;
    }
    const deploy = event.target.closest("[data-deploy]");
    if (deploy && student) {
      const id = deploy.closest("[data-assignment-id]").dataset.assignmentId;
      if (pending.has(id)) return;
      const generation = epoch; pending.add(id); deploy.disabled = true; deploy.textContent = "Отправляем запрос…"; warn("");
      try {
        const result = await api(`assignments/${encodeURIComponent(id)}/deploy`,{});
        if (generation !== epoch) return;
        const a = assignments.find(a => a.id === id); if (a) a.stand = result.stand;
        if (page === "assignments") dashboard(); announce("Подготовка стенда запущена."); await refresh(true);
      } catch (error) { if (generation === epoch) { if (error.status === 401) await refresh(true); else warn(error.message); } }
      finally { if (generation === epoch) { pending.delete(id); if (page === "assignments") dashboard(); } }
      return;
    }
    if (event.target.closest("#download-assignment") && current) {
      const a = current.assignment;
      const lines = [a.title,`Шаблон: ${a.template_name} · VMID ${a.template_vmid}`,"",a.description,"","Порядок работы:",...a.steps.map((step,i) => `${i+1}. ${step}`)];
      const url = URL.createObjectURL(new Blob(["\uFEFF"+lines.join("\r\n")],{type:"text/plain;charset=utf-8"}));
      const link = document.createElement("a"); link.href = url; link.download = `Задание-${String(a.id).replace(/[^a-zA-Z0-9_-]/g,"_")}.txt`;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url),1000);
    }
  });
  view.addEventListener("change",event => {
    if (!event.target.matches("[data-step]") || !current) return;
    const key = String(current.stand.id), checked = marks.get(key) || new Set(), i = Number(event.target.dataset.step);
    if (event.target.checked) checked.add(i); else checked.delete(i);
    marks.set(key,checked);
  });
  $("#logout").addEventListener("click",async event => {
    const button = event.currentTarget; button.disabled = true; clearTimeout(timer); ++activeRequest;
    try {
      await api("logout",{}); forget(); history.replaceState(null,"",location.pathname+location.search); loginPage(); warn("");
      $("#login").focus(); announce("Вы вышли из кабинета.");
    } catch (error) { if (error.status === 401) await refresh(true); else { warn(error.message); schedule(); } }
    finally { button.disabled = false; }
  });
  window.addEventListener("hashchange",async () => { if (student) { await refresh(true); if (student) focusPage(); } });
  window.addEventListener("online",() => refresh(true));
  window.addEventListener("offline",() => { clearTimeout(timer); warn("Нет подключения. Состояние стендов обновится после восстановления связи.",true); });
  document.addEventListener("visibilitychange",() => { if (document.hidden) clearTimeout(timer); else if (student) refresh(); });
  refresh();
})();
