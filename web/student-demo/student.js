"use strict";
(function studentPortal() {
  const $ = (selector, root = document) => root.querySelector(selector);
  const view = $("#view"), notice = $("#notice");
  let demoProfiles = [];
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
  let student = null, assignments = [], current = null, page = "", timer = 0, epoch = 0, activeRequest = 0;
  const pending = new Set(), marks = new Map();
  const labStatuses = {
    pending:"Заявка не принята в очередь", queued:"В очереди на развёртывание", provisioning:"Развёртывается VM",
    paused:"Развёртывание приостановлено", awaiting_confirmation:"Ожидает решения преподавателя", error:"Ошибка развёртывания", stopped:"VM выключена", removed:"Стенд удалён",
    ready:"VM готова", expired:"Время истекло"
  };
  const labAttention = state => ["pending","paused","awaiting_confirmation","error","stopped","removed"].includes(state);
  function leaseText(stand) {
    const lease = stand?.actual_lease || stand;
    if (!lease) return "";
    if (lease.expires_at) {
      const date = new Date(lease.expires_at);
      if (!Number.isNaN(date.getTime())) return `До ${date.toLocaleString("ru-RU",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})}`;
    }
    return lease.duration_minutes ? `${lease.duration_minutes} мин с момента готовности` : "Без ограничения";
  }

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
      <section class="login-card"><h2>Вход студента</h2><p class="muted">Введите логин и пароль учебного кабинета, которые выдал преподаватель.</p>
      <form id="login-form"><label class="field">Логин<input name="login" id="login" autocomplete="username" autocapitalize="none" spellcheck="false" required></label>
      <label class="field">Пароль<input name="password" id="password" type="password" autocomplete="current-password" required></label>
      <button type="submit" class="button primary">Войти в кабинет</button></form>
      <div class="demo-accounts" hidden><strong>Попробовать демо</strong><p>Выберите демонстрационный профиль, чтобы подставить его данные для входа.</p>
      <div class="demo-fill"></div></div></section></div>`;
    const accounts = $(".demo-accounts",view);
    demoProfiles = [];
    api("login-options").then(options => {
      if (!accounts.isConnected || !Array.isArray(options.demo_profiles)) return;
      demoProfiles = options.demo_profiles.filter(id => id === "student01" || id === "student02");
      for (const id of demoProfiles) {
        const button = document.createElement("button");
        button.type = "button"; button.dataset.demoLogin = id; button.textContent = id; $(".demo-fill",accounts).append(button);
      }
      accounts.hidden = !demoProfiles.length;
    }).catch(() => {}); // Personal sign-in still works when public suggestions are unavailable.
  }
  function card(a, index) {
    return `<article class="assignment" data-assignment-id="${esc(a.id)}"><div class="card-top"><span class="eyebrow">Практика / ${String(index + 1).padStart(2,"0")}</span><span class="status" data-status></span></div>
      <h3>${esc(a.title)}</h3><p class="brief">${esc(a.description)}</p>
      <p class="template-line">Шаблон: <b>${esc(a.template_name)}</b> · VMID ${esc(a.template_vmid)}</p>
      <details><summary>Условия задания</summary><ol class="steps">${a.steps.map(step => `<li>${esc(step)}</li>`).join("")}</ol></details>
      <div class="actionbox"><div class="progress-wrap" hidden><span data-phase></span><progress max="100" value="0" aria-label="Подготовка стенда"></progress></div>
      <p class="stand-note" data-stand-reason hidden></p>
      <label class="field lease-field" data-lease-field>Срок ${student.managed ? "работы VM" : "демостенда"}, минут<input data-duration type="number" inputmode="numeric" min="1" max="10080" step="1" placeholder="Без ограничения"><small>От 1 минуты до 7 дней после готовности. Пустое поле — без ограничения.</small></label>
      <p class="lease-meta" data-list-lease hidden></p>
      <button type="button" class="button primary" data-deploy>Развернуть стенд</button>
      <a class="button primary" data-open-stand hidden>Открыть карточку стенда</a></div></article>`;
  }
  function updateStatus(root, stand, readyLabel = "Карточка готова") {
    const status = $("[data-status]",root), progress = $("progress",root), wrap = progress.parentElement;
    const state = stand?.status, ready = state === "ready", working = state === "provisioning" || state === "queued";
    status.className = `status ${ready ? "ready" : working ? "provisioning" : labAttention(state) ? "attention" : ""}`;
    status.textContent = stand?.real ? (labStatuses[state] || "Получаем состояние VM") : state === "expired" ? "Время истекло" : state === "stopped" ? "VM выключена" : ready ? readyLabel : working ? "Идёт подготовка карточки" : "Карточка не создана";
    const reason = $("[data-stand-reason]",root);
    if (reason) {
      reason.hidden = !(stand?.real && labAttention(state) && stand.phase && stand.phase !== status.textContent);
      reason.textContent = reason.hidden ? "" : stand.phase;
    }
    wrap.hidden = !working;
    if (working) {
      const percent = Number.isFinite(stand.progress) ? Math.max(0,Math.min(100,stand.progress)) : null;
      if (percent !== null) progress.value = percent; else progress.removeAttribute("value");
      $("[data-phase]",root).textContent = (stand.phase || status.textContent) + (percent === null ? "" : ` · ${percent}%`);
    }
  }
  function dashboard(force = false) {
    const first = page !== "assignments";
    if (first || force) {
      page = "assignments"; current = null; document.title = "Мои задания · Deployer";
      view.innerHTML = `<section class="hero"><p class="eyebrow">Кабинет студента / ${esc(student.group)}</p><h1>Мои <span class="accent">задания.</span></h1>
        <p class="lead">${esc(student.name)}, выберите практическую работу. ${student.managed ? "После запуска Deployer поставит вашу личную VM в очередь Proxmox. Одновременно можно работать на одном стенде." : "Стенд будет закреплён за вашим профилем и этим заданием."}</p>
        <div class="hero-stats"><span>Назначено заданий: <strong>${assignments.length}</strong></span><span>Карточки готовы: <strong id="ready-count">0</strong></span></div></section>
        <div class="section-head"><h2>Практические работы</h2></div>
        ${assignments.length ? `<section class="assignment-grid" aria-label="Список заданий">${assignments.map(card).join("")}</section>` : `<div class="empty">Пока у вас нет назначенных работ.</div>`}`;
    }
    $("#ready-count").textContent = assignments.filter(a => a.stand?.status === "ready").length;
    const cards = view.querySelectorAll("[data-assignment-id]");
    assignments.forEach((a,i) => {
      const root = cards[i]; if (!root) return;
      const button = $("[data-deploy]",root), link = $("[data-open-stand]",root), s = a.stand;
      updateStatus(root,s);
      const restart = (s?.status === "expired" && !s.real && !s.actual_lease) || (s?.real && s.status === "removed"), retry = s?.real && s.status === "pending";
      button.hidden = !!s && !restart && !retry; button.disabled = pending.has(a.id); button.textContent = button.disabled ? "Отправляем запрос…" : retry ? "Повторить заявку" : restart ? "Развернуть заново" : "Развернуть стенд";
      $("[data-lease-field]",root).hidden = button.hidden || retry;
      const lease = $("[data-list-lease]",root);
      lease.hidden = !s?.duration_minutes && !s?.actual_lease?.duration_minutes;
      lease.textContent = lease.hidden ? "" : leaseText(s);
      link.hidden = !s?.real && !["ready","expired","stopped"].includes(s?.status);
      if (!link.hidden) link.href = `#stands/${encodeURIComponent(s.id)}`;
    });
  }
  function proxmoxAccess(access) {
    // Links are assigned by the server, never built from the template VMID or the route hash.
    if (access?.kind !== "proxmox" || access.login !== "root@pam" || !Number.isInteger(access.vmid)
        || access.vmid < 100 || access.vmid > 999999999 || typeof access.url !== "string") return null;
    const match = /^https:\/\/(\d{1,3}(?:\.\d{1,3}){3}):8006\/$/.exec(access.url);
    if (!match) return null;
    const ip = match[1], parts = ip.split(".").map(Number);
    if (parts.some(n => n > 255) || parts.join(".") !== ip || parts[0] !== 10 || parts[1] !== 39
        || (parts[2] === 0 && parts[3] === 0) || (parts[2] === 255 && parts[3] === 255)) return null;
    return {url:`https://${ip}:8006/`, ip, login:access.login, vmid:access.vmid};
  }
  function standPage(result) {
    const a = result.assignment, s = result.stand;
    const actualExpired = s.actual_lease && (s.actual_lease.status === "expired" || s.status === "expired" || Date.parse(s.actual_lease.expires_at) <= Date.now());
    const access = s.status === "ready" && !actualExpired ? proxmoxAccess(s.access) : null;
    const pendingPilot = !actualExpired && !access && s.pilot_pending === true;
    const real = !!(s.real || s.actual_lease || access || pendingPilot);
    const timingFixed = Number(s.actual_lease?.duration_minutes) > 0;
    const key = `stand:${s.id}:${access?.url || (pendingPilot ? "pending" : actualExpired ? "expired" : s.real ? s.status : "demo")}:${access?.vmid || ""}:${timingFixed}`;
    if (page !== key) {
      page = key; document.title = `${s.name} · Практикум`;
      const marked = marks.get(String(s.id)) || new Set();
      view.innerHTML = `<nav class="crumbs" aria-label="Навигация"><a href="#assignments">← Мои задания</a><span>/</span><span>Стенд</span></nav>
        <header class="hero"><p class="eyebrow">Практикум / ${real ? "Персональный Proxmox" : "Демостенд"}</p><h1>${esc(s.name)}</h1><p class="lead">${esc(student.name)} · ${esc(student.group)}</p>
        ${access ? `<div class="guest-launch"><a class="button primary" data-open-proxmox href="${esc(access.url)}" target="_blank" rel="noopener noreferrer" aria-describedby="guest-target">Открыть мой Proxmox</a>
        <span id="guest-target">В новой вкладке · <bdi class="mono">${esc(access.ip)}:8006</bdi></span></div>` : ""}</header>
        <div class="stand-grid"><article class="sheet"><p class="eyebrow">Приложенное задание</p><h2>${esc(a.title)}</h2><p class="brief">${esc(a.description)}</p>
        <h3>Порядок работы</h3><div class="checklist">${a.steps.map((step,i) => `<label><input type="checkbox" data-step="${i}" ${marked.has(i) ? "checked" : ""}><span>${esc(step)}</span></label>`).join("")}</div>
        <p class="check-hint">Это личные заметки: отметки сохраняются в этой вкладке до обновления страницы или выхода.</p>
        <button type="button" id="download-assignment" class="button secondary">Скачать задание (.txt)</button></article>
        <aside class="stand-panel"><p class="eyebrow">Среда</p><h2>${real ? "Персональная VM" : "Учебный симулятор"}</h2><dl>
        <div><dt>Статус</dt><dd data-status></dd></div>
        <div><dt>${real ? "Срок работы VM" : "Срок демостенда"}</dt><dd data-lease></dd></div>
        ${access ? `<div><dt>IP вашей VM</dt><dd class="mono">${esc(access.ip)}</dd></div><div><dt>Логин Proxmox</dt><dd class="mono">${esc(access.login)}</dd></div>
        <div><dt>VMID вашей VM</dt><dd>${esc(access.vmid)}</dd></div>` : ""}
        <div><dt>Шаблон</dt><dd>${esc(a.template_name)}</dd></div>
        <div><dt>VMID шаблона</dt><dd>${esc(a.template_vmid)}</dd></div><div><dt>Владелец</dt><dd>${esc(student.login)}</dd></div></dl>
        <div class="stand-progress" hidden><span data-phase></span><progress max="100" value="0" aria-label="Подготовка стенда"></progress></div><p class="stand-note" data-stand-reason hidden></p>
        ${access ? `<div class="guest-copy"><button type="button" class="button secondary small" data-copy-login>Скопировать логин</button><button type="button" class="button secondary small" data-copy-password>Скопировать пароль</button></div>
        ${s.real ? "" : timingFixed ? '<p class="stand-note">Время работы зафиксировано. Изменить или отменить назначенный срок из кабинета нельзя.</p>' : `<details class="duration-settings"><summary>Назначить время работы</summary><form data-real-duration-form>
          <label class="field">Время с момента подтверждения, минут<input name="duration_minutes" type="number" min="1" max="10080" step="1" inputmode="numeric" required placeholder="Например, 90"></label>
          <p>Без таймера стенд работает бессрочно. Срок назначается один раз: по его окончании VM получит команду мягкого выключения. Диски и задание сохранятся.</p>
          <button class="button secondary small" type="submit">Назначить срок</button></form></details>`}
        <form class="grant-form" data-grant-form hidden><label class="field">Пароль учебного кабинета<input type="password" name="access_code" autocomplete="off" spellcheck="false" required></label><button class="button primary small" type="submit">Подтвердить время работы</button></form>
        <p data-copy-status role="status" aria-live="polite"></p>` : ""}
        <p class="stand-note">${access ? "Скопируйте логин и пароль по отдельности и вставьте их на странице входа в Proxmox."
          : actualExpired ? "Срок работы этой VM закончился. Задание сохранено. Для продления обратитесь к преподавателю."
          : pendingPilot ? "Реальный стенд заказан. Ссылка появится здесь после готовности VM и проверки доступа. Обновлять страницу не нужно."
          : s.real && s.status === "pending" ? "Вернитесь к заданиям и повторите эту заявку, когда её причина устранена. Второй стенд при повторе не создаётся."
          : s.real && ["queued","provisioning"].includes(s.status) ? "Ссылка на ваш Proxmox появится здесь после готовности VM. Страница обновляется автоматически."
          : s.real && ["paused","awaiting_confirmation"].includes(s.status) ? "Заявка сохранена. Продолжить развёртывание может преподаватель через Deployer."
          : s.real && s.status === "removed" ? "Эта VM удалена. Для нового запуска вернитесь в список заданий."
          : s.real && s.status === "expired" ? "Срок работы закончился. Для повторного доступа обратитесь к преподавателю."
          : s.real ? "Доступ к VM сейчас недоступен. Передайте преподавателю название этого стенда."
          : "Работайте с заданием в этой карточке. Сейчас в ней нет доступа к реальной VM. Если для практики нужен Proxmox, обратитесь к преподавателю."}</p></aside></div>`;
    }
    current = result;
    const panel = $(".stand-panel");
    $("[data-lease]",panel).textContent = pendingPilot && !s.actual_lease ? "Уточняется" : !s.real && real && !s.actual_lease ? "Не указан" : leaseText(s);
    if (actualExpired) {
      const status = $("[data-status]",panel); status.className = "status"; status.textContent = "Срок работы истёк";
      $(".stand-progress",panel).hidden = true;
    } else if (pendingPilot) {
      const status = $("[data-status]",panel); status.className = "status provisioning"; status.textContent = "Ожидает запуска Proxmox";
      $(".stand-progress",panel).hidden = true;
    } else {
      updateStatus(panel,s,access ? "Реальная VM привязана" : "Демо готово");
    }
  }
  const routeId = () => {
    const match = /^#stands\/([^/]+)$/.exec(location.hash);
    return match ? match[1] : "";
  };
  function schedule() {
    clearTimeout(timer);
    if (document.hidden || !student) return;
    const stands = page.startsWith("stand:") ? [current?.stand] : assignments.map(a => a.stand);
    let delay = Infinity;
    if (page.startsWith("stand:") && current?.stand.pilot_pending === true && !proxmoxAccess(current.stand.access)) {
      delay = 10000;
    }
    for (const s of stands.filter(Boolean)) {
      if (s.real) delay = Math.min(delay,["queued","provisioning"].includes(s.status) ? 4500 : ["paused","awaiting_confirmation"].includes(s.status) ? 12000 : 30000);
      else if (s.status === "provisioning") delay = Math.min(delay,1250);
      const until = Date.parse((s.actual_lease || s).expires_at) - Date.now();
      if (s.status !== "expired" && until > 0) delay = Math.min(delay,until+100);
    }
    if (Number.isFinite(delay)) timer = setTimeout(() => refresh(),delay);
  }
  async function refresh(userAction = false) {
    const generation = epoch, request = ++activeRequest, id = routeId();
    const relevant = () => generation === epoch && request === activeRequest && routeId() === id;
    try {
      const old = assignments, info = await api("me"); if (!relevant()) return;
      if (student && student.login !== info.student.login) { forget(); await refresh(true); return; }
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
        if (old.some(a => a.stand?.status === "provisioning" && assignments.some(x => x.id === a.id && x.stand?.status === "ready"))) announce("Карточка стенда готова.");
      }
      warn(""); schedule();
    } catch (error) {
      if (!relevant()) return;
      if (error.status === 401) {
        const wasSignedIn = !!student; forget(); loginPage(); warn(wasSignedIn ? "Сессия завершена. Войдите в кабинет снова." : "");
      } else { if (!page) loginPage(); warn(error.message,true); schedule(); }
    }
  }
  function checkedMinutes(input) {
    if (!input.validity.valid || (input.value && (!Number.isInteger(input.valueAsNumber) || input.valueAsNumber < 1 || input.valueAsNumber > 10080))) {
      input.reportValidity(); input.focus(); throw new Error("Укажите от 1 до 10 080 целых минут.");
    }
    return input.value === "" ? null : input.valueAsNumber;
  }
  const message = text => { const output = $("[data-copy-status]"); if (output) output.textContent = text; };
  async function copyVmPassword(button) {
    if (button.disabled || !student || !current || !proxmoxAccess(current.stand.access)) return;
    const stand = current.stand.id, login = student.login, generation = epoch;
    const relevant = () => generation === epoch && student?.login === login && routeId() === String(stand) && button.isConnected;
    const denied = () => Object.assign(new Error(),{clipboard:true});
    button.disabled = true; message("Копируем пароль…");
    try {
      const clipboard = navigator.clipboard;
      const canWriteItem = clipboard?.write && typeof ClipboardItem === "function";
      if (!window.isSecureContext || (!canWriteItem && !clipboard?.writeText)) throw denied();
      const password = api(`stands/${encodeURIComponent(stand)}/credentials`,{}).then(result => {
        if (!relevant() || typeof result.password !== "string" || !result.password) throw new Error();
        return result.password;
      });
      if (canWriteItem) {
        // Begin the write during the original click; Safari cannot regain activation after fetch.
        const blob = password.then(value => new Blob([value],{type:"text/plain"}));
        blob.catch(() => {}); // Some browsers reject write() before reading the item.
        try { await clipboard.write([new ClipboardItem({"text/plain":blob})]); }
        catch { await password; throw denied(); }
      } else {
        const value = await password;
        try { await clipboard.writeText(value); } catch { throw denied(); }
      }
      if (relevant()) message("Пароль скопирован.");
    } catch (err) {
      if (!relevant()) return;
      if (err.status === 401) { await refresh(true); return; }
      message(err.clipboard ? "Браузер не разрешил копирование. Проверьте разрешение для буфера обмена и откройте кабинет по HTTPS."
        : err.status === 429 ? "Слишком много запросов. Попробуйте позже." : "Не удалось получить пароль вашей VM. Проверьте доступность стенда.");
    } finally { button.disabled = false; }
  }
  function beginDurationGrant(minutes) {
    const form = $("[data-grant-form]");
    if (!form || !student || !current || !proxmoxAccess(current.stand.access)) return;
    form.dataset.minutes = minutes ?? "";
    form.hidden = false; form.elements.access_code.focus(); message("Для изменения срока введите пароль учебного кабинета.");
  }
  async function setDuration(minutes, code) {
    if (!student || !current || !proxmoxAccess(current.stand.access)) return;
    const stand = current.stand.id, login = student.login, generation = epoch, form = $("[data-grant-form]");
    if (!form || form.dataset.busy === "yes") return;
    const relevant = () => generation === epoch && student?.login === login && routeId() === stand && current?.stand.id === stand && form.isConnected;
    form.dataset.busy = "yes";
    const buttons = [...view.querySelectorAll("[data-grant-form] button, [data-real-duration-form] button")];
    buttons.forEach(button => {button.disabled = true;});
    try {
      await api(`stands/${encodeURIComponent(stand)}/duration`,{access_code:code,duration_minutes:minutes});
      if (!relevant()) return;
      form.hidden = true;
      message(`Таймер установлен: ${minutes} мин.`);
      await refresh(true);
    } catch (err) {
      if (!relevant()) return;
      if (err.status === 403) { form.hidden = false; form.dataset.minutes = minutes ?? ""; form.elements.access_code.focus(); }
      if (err.status === 401) { await refresh(true); return; }
      message(err.status === 403 ? "Пароль учебного кабинета не принят." : err.status === 429 ? "Слишком много попыток. Попробуйте позже." : "Операция не выполнена. Проверьте доступ к стенду и повторите.");
    } finally {
      if (form.isConnected) { delete form.dataset.busy; buttons.forEach(button => {button.disabled = false;}); }
    }
  }
  view.addEventListener("submit", async event => {
    if (event.target.matches("[data-grant-form]")) {
      event.preventDefault(); const form = event.target, input = form.elements.access_code, code = input.value.trim(); input.value = "";
      if (code) await setDuration(form.dataset.minutes ? Number(form.dataset.minutes) : null, code);
      return;
    }
    if (event.target.matches("[data-real-duration-form]")) {
      event.preventDefault(); let minutes;
      try { minutes = checkedMinutes(event.target.elements.duration_minutes); } catch { return; }
      if (minutes && window.confirm(`Назначить срок ${minutes} мин с этого момента? По окончании VM получит команду мягкого выключения. Изменить срок из кабинета нельзя.`)) beginDurationGrant(minutes);
      return;
    }
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
      if (generation === epoch) warn(error.status === 401 ? "Неверный логин или пароль учебного кабинета." : error.message);
    } finally { submit.disabled = false; }
  });
  view.addEventListener("click",async event => {
    const fill = event.target.closest("[data-demo-login]");
    if (fill) {
      const id = fill.dataset.demoLogin;
      if (!demoProfiles.includes(id)) return;
      $("#login").value = id; $("#password").value = `DemoLab${id.slice(-2)}!`;
      $("#login-form button[type=submit]").focus(); announce(`Учебный профиль ${id} выбран.`); return;
    }
    const deploy = event.target.closest("[data-deploy]");
    if (event.target.closest("[data-copy-login]") && current?.stand.access) {
      try { await navigator.clipboard.writeText(current.stand.access.login); message("Логин скопирован."); }
      catch { message("Не удалось скопировать: выделите логин в карточке и скопируйте вручную."); }
      return;
    }
    const copyPassword = event.target.closest("[data-copy-password]");
    if (copyPassword) { await copyVmPassword(copyPassword); return; }
    if (deploy && student) {
      const root = deploy.closest("[data-assignment-id]"), id = root.dataset.assignmentId;
      let minutes;
      const prior = assignments.find(a => a.id === id)?.stand;
      try { minutes = prior?.real && prior.status === "pending" ? prior.duration_minutes : checkedMinutes($("[data-duration]",root)); } catch (error) { warn(error.message); return; }
      if (pending.has(id)) return;
      const generation = epoch; pending.add(id); deploy.disabled = true; deploy.textContent = "Отправляем запрос…"; warn("");
      try {
        const result = await api(`assignments/${encodeURIComponent(id)}/deploy`,minutes == null ? {} : {duration_minutes:minutes});
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
    ++epoch;
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
