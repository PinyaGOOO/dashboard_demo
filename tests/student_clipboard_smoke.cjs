// Run with: CHROME_BIN=/path/to/chrome node tests/student_clipboard_smoke.cjs
// Chromium handles the actual portal DOM; requests and the OS clipboard are isolated fakes.
"use strict";
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const http = require("node:http"), {execFile} = require("node:child_process");
const run = require("node:util").promisify(execFile);
const web = path.join(__dirname, "../web/student-demo");
const chrome = process.env.CHROME_BIN || (process.platform === "win32" ? "C:/Program Files/Google/Chrome/Application/chrome.exe" : "chromium");
function teacherFixture() {
  const token = "fake-" + crypto.randomUUID(), group = "ДЭ-24"; let submissions = 0, correct = false, copied;
  Object.defineProperty(navigator,"clipboard",{value:{async writeText(text){copied=text;}}});
  window.fetch = async (url,opts) => {
    if (url !== "/api/student-accounts/batch") return new Response("{}",{status:503});
    submissions++;
    const data = JSON.parse(opts.body);
    correct = data.count === 20 && data.group === group && data.prefix === "class-gr" && opts.method === "POST" && opts.headers["X-Admin-Token"] === token;
    const accounts = Array.from({length:data.count},(_,n) => ({group,login:"class-gr"+(n+1),password:"fake-password-"+n}));
    await new Promise(r=>setTimeout(r,30)); return new Response(JSON.stringify({count:data.count,accounts}));
  };
  window.addEventListener("load",()=>setTimeout(async () => {
    const $ = s => document.querySelector(s);
    $("a[href='#student-accounts']").click();
    $("[data-create-student-batch]").click();
    const form = $("#student-batch");
    if (!$("#app").contains(form) || $("#modal-root").childElementCount || location.hash !== "#student-accounts" || form.elements.count.value !== "20" || form.elements.prefix.value !== "student") return;
    form.elements.group.value = group; form.elements.prefix.value = "class-gr"; form.elements.token.value = token;
    form.requestSubmit(); form.requestSubmit(); await new Promise(r=>setTimeout(r,100));
    const roster = $(".student-batch-result textarea");
    if (!roster || roster.value.split("\n").length !== 20 || !roster.value.includes("class-gr20\tfake-password-19")) return;
    $("[data-copy-roster]").click(); await new Promise(r=>setTimeout(r,5));
    if (!correct || submissions !== 1 || copied !== roster.value || JSON.stringify(localStorage).includes("fake-") || JSON.stringify(sessionStorage).includes("fake-")) return;
    if (!$("#app").contains(roster) || $("#modal-root").childElementCount) return;
    $("a[data-route=overview]").click(); await new Promise(r=>setTimeout(r,20));
    if (roster.value === "" && !roster.isConnected && !$(".student-batch-result")) document.body.dataset.qa = "pass";
  },50));
}
function accountAdminFixture() {
  const mode = location.search.slice(1), token = "token-test", secret = "temporary-" + crypto.randomUUID();
  const acct = {login:"class09",group:"ДЭ-24 <тест>",version:1,created:1700000000,last_login:null,blocked:false};
  const writes = [], reads = []; let copied, historyReads = 0, deleted = false, bad = false;
  const delay = (ms=20) => new Promise(done => setTimeout(done,ms));
  Object.defineProperty(navigator,"clipboard",{value:{async writeText(text){copied=text;}}});
  window.fetch = async (url,opts) => {
    if (!url.startsWith("/api/student-accounts")) return new Response("{}",{status:503});
    bad ||= opts.headers["X-Admin-Token"] !== token;
    if (url.startsWith("/api/student-accounts?")) {
      const q = new URL(url,location).searchParams;
      const sort = q.get("sort") || "login";
      bad ||= q.get("limit") !== "50" || !["login","group","group-desc"].includes(sort);
      reads.push([q.get("search"),Number(q.get("offset")),sort]);
      const many = !q.get("search");
      const accounts = deleted ? [] : many && q.get("offset") === "0" ? Array.from({length:50},(_,i)=>({...acct,login:"class" + i})) : [acct];
      return new Response(JSON.stringify({accounts,total:deleted ? 0 : many ? 51 : 1,offset:Number(q.get("offset")),limit:50}));
    }
    if (url === "/api/student-accounts/class09/history") {
      historyReads++; bad ||= (opts.method || "GET") !== "GET";
      if (mode === "roster-history-late") await delay(80);
      return new Response(JSON.stringify({login:acct.login,entries:[{at:1700000000},{at:1690000000}],limit:50}));
    }
    const action = url.split("/").pop();
    bad ||= opts.method !== "POST" || url !== "/api/student-accounts/class09/" + action || JSON.parse(opts.body).version !== acct.version;
    writes.push(action); await delay(30);
    if (mode === "roster-stale" || (mode === "roster-delete-stale" && action === "delete")) return new Response("{}",{status:409});
    if (mode === "roster-denied") return new Response("{}",{status:401});
    if (action === "delete") {
      bad ||= JSON.parse(opts.body).confirm_login !== acct.login; deleted = true;
      return new Response(JSON.stringify({deleted:true,login:acct.login}));
    }
    acct.version++; if (action === "block") acct.blocked = true; if (action === "unblock") acct.blocked = false;
    return new Response(JSON.stringify({account:acct,...(action === "reset-password" ? {password:secret} : {})}));
  };
  window.addEventListener("load",()=>setTimeout(async () => {
    const $ = s => document.querySelector(s), click = sel => { document.body.dataset.qaStep = sel; $(sel).click(); };
    const sidebar = $("a[data-route='student-accounts'][href='#student-accounts']");
    if (!sidebar) return;
    if (mode === "roster-history-nav") {
      // First visit is a direct link, not a click inside an existing admin page.
      if (location.hash !== "#student-accounts" || !$("#app [data-roster-unlock]") || $("#modal-root").childElementCount) return;
      if (performance.getEntriesByType("navigation")[0]?.type !== "reload") { location.reload(); return; }
      click("a[data-route='overview']"); await delay();
      if ($("#app [data-student-admin]") || location.hash !== "#overview") return;
      history.back(); await delay(100);
      if (location.hash === "#student-accounts" && $("#app [data-roster-unlock]") && sidebar.getAttribute("aria-current")==="page" && !$("#modal-root").childElementCount && !reads.length && !writes.length) document.body.dataset.qa = "pass";
      return;
    }
    sidebar.click(); if (reads.length || writes.length || location.hash !== "#student-accounts" || !$("#app [data-roster-unlock]") || $("#modal-root").childElementCount) return;
    const auth = $("[data-roster-unlock]"); auth.elements.token.value = token; auth.requestSubmit(); await delay();
    if (document.querySelectorAll("[data-manage-student]").length !== 50) return;
    click("[data-roster-next]"); await delay(); if (reads.at(-1)[1] !== 50) return;
    click("[data-roster-prev]"); await delay(); if (reads.at(-1)[1] !== 0) return;
    for (const sort of ["login","group","group-desc"]) {
      const field = $("[data-roster-sort]"); field.value = sort; field.dispatchEvent(new Event("change",{bubbles:true})); await delay();
      if (reads.at(-1)[2] !== sort || reads.at(-1)[1] !== 0) return;
    }
    const filter = $("[data-roster-search]"); filter.elements.search.value="ДЭ-24"; filter.requestSubmit(); await delay();
    if (reads.at(-1)[0] !== "ДЭ-24" || reads.at(-1)[2] !== "group-desc" || !$("[data-roster-next]").disabled) return;
    click("[data-manage-student]"); click("[data-account-history]");
    if (mode === "roster-history-late") {
      click("a[data-route='overview']"); await delay(120);
      if (!bad && historyReads === 1 && !writes.length && !$("[data-student-admin]") && !$("[data-history-back]")) document.body.dataset.qa = "pass";
      return;
    }
    await delay();
    const dates = [...document.querySelectorAll("#app [data-login-history] time")].map(t=>Date.parse(t.dateTime)/1000);
    if (historyReads !== 1 || !$("#app [data-history-back]") || $("#modal-root").childElementCount || dates.join(",") !== "1700000000,1690000000") return;
    click("[data-history-back]"); click("[data-roster-back]"); await delay();
    for (const action of ["reset-password","block","unblock","revoke-sessions","delete"]) {
      click("[data-manage-student]"); click('[data-account-action="' + action + '"]');
      if (!$("#app [data-student-admin]").innerText.includes(acct.login) || $("#modal-root").childElementCount) return;
      if (action === "reset-password") {
        click("[data-account-cancel]"); if (writes.length) return;
        click('[data-account-action="reset-password"]');
      }
      if (action === "delete") {
        const field = $("[data-confirm-login]"), confirm = $("[data-account-confirm]"), before = writes.length;
        field.value = "class"; field.dispatchEvent(new Event("input",{bubbles:true})); confirm.click();
        if (writes.length !== before) return;
        field.value = acct.login; field.dispatchEvent(new Event("input",{bubbles:true})); if (confirm.disabled) return;
      }
      click("[data-account-confirm]"); click("[data-account-confirm]");
      if (mode === "roster-late") {
        click("a[data-route='overview']"); await delay(60);
        if (!bad && writes.length === 1 && !$("[data-reset-secret]") && !$("[data-student-admin]")) document.body.dataset.qa = "pass";
        return;
      }
      await delay(60);
      if (mode === "roster-stale" || mode === "roster-denied") {
        if (!bad && writes.length === 1 && !$("[data-reset-secret]") && (mode === "roster-denied" ? !!$("[data-roster-unlock]") : $("[data-student-admin] [role=alert]")?.textContent.includes("изменились"))) document.body.dataset.qa = "pass";
        return;
      }
      if (mode === "roster-delete-stale" && action === "delete") {
        const alert = $("#app [role=alert]");
        if (!bad && !deleted && writes.filter(a => a === "delete").length === 1 && alert?.textContent.includes("изменились") && !$("[data-account-confirm]")) document.body.dataset.qa = "pass";
        return;
      }
      if (action === "reset-password") {
        const field = $("[data-reset-secret]"); if (field?.value !== secret) return;
        click("[data-copy-reset]"); await delay();
        if (copied !== secret || localStorage.length || sessionStorage.length) return;
        click("[data-roster-back]"); await delay(); if (field.value || $("[data-reset-secret]")) return;
      }
      if (action === "block" && !$(".student-account-status").textContent.includes("Заблокирована")) return;
    }
    if (!deleted || $("[data-manage-student]")) return;
    window.dispatchEvent(new StorageEvent("storage",{key:"demoops.adminToken"}));
    if (!bad && writes.join(",") === "reset-password,block,unblock,revoke-sessions,delete" && !$("[data-reset-secret]") && !$("[data-manage-student]") && !$("[data-roster-search]")) document.body.dataset.qa = "pass";
  },50));
}
function managedFixture() {
  const mode = location.search.slice(1);
  const task = {id:"task", title:"Эксперимент",description:"Fixture",template_name:"Fixture",template_vmid:276,steps:[]};
  const student = {login:"class-gr01",name:"Студент",group:"ДЭ-24",managed:true};
  let submissions = 0, correct = false, stand = null;
  window.fetch = async (url,opts) => {
    if (url === "/student-demo/api/assignments/task/deploy") {
      submissions++; correct = opts.method === "POST" && JSON.parse(opts.body).duration_minutes === 90;
      stand = {id:"my-lab",name:"class-gr01-lab",real:true,status:"queued",phase:"Ожидание ресурса",duration_minutes:90};
      return new Response(JSON.stringify({stand}));
    }
    if (url === "/student-demo/api/me") return new Response(JSON.stringify({student,assignments:[{...task,stand}]}));
    if (url === "/student-demo/api/stands/my-lab") {
      stand = {...stand,status:"awaiting_confirmation",phase:"Узел занят"}; return new Response(JSON.stringify({stand,assignment:task}));
    }
    return new Response("{}",{status:404});
  };
  window.addEventListener("load",()=>setTimeout(async () => {
    const $ = s => document.querySelector(s);
    $("[data-duration]").value = "90"; $("[data-deploy]").click(); $("[data-deploy]").click();
    await new Promise(r=>setTimeout(r,80));
    if (!correct || submissions !== 1 || $("[data-status]").textContent !== "В очереди на развёртывание" || !$("[data-deploy]").hidden || $("[data-open-stand]").hidden) return;
    $("[data-open-stand]").click(); await new Promise(r=>setTimeout(r,120));
    if (mode === "managed" && $("[data-status]").textContent === "Ожидает решения преподавателя" && $("[data-stand-reason]").textContent === "Узел занят" && !$("[data-copy-password]") && !document.body.innerText.includes("Учебный симулятор")) document.body.dataset.qa = "pass";
  },50));
}
function assignmentFixture() {
  const imageId = "13579bdf".repeat(4), imagePath = "/student-demo/api/assignments/lab-2-276/images/" + imageId;
  const md = '## Шаги\n\n- **Соберите** схему\n- `ip -br a`\n\n![Топология](attachment:' + imageId + ')\n\n![diagram](https://invalid.example/image.png)\n\n<img src="/never" onerror="window.injection=true">';
  const a = {id:"lab-2-276",title:"Паспорт сети",description:"",template_name:"PR1",template_vmid:276,steps:[],assignment_markdown:md,images_available:true};
  const student = {login:"class01",name:"Test",group:"ДЭ-24",managed:true};
  const stand = {id:"fixture",name:"Личная VM",real:true,status:"ready",access:{kind:"proxmox",login:"root@pam",vmid:321,url:"https://10.39.0.9:8006/"}};
  let hidden = false, blob, filename;
  const task = () => ({...a,withdrawn:hidden,stand}); // Leave stale text intentionally: the client must not show it.
  window.fetch = async url => new Response(JSON.stringify(url.endsWith("/me") ? {student,assignments:[task()]} : {assignment:task(),stand}));
  URL.createObjectURL = b => {blob=b; return "blob:test";};
  const click = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function() { if (this.download) filename=this.download; else click.call(this); };
  window.addEventListener("load",()=>setTimeout(async () => {
    const $ = sel => document.querySelector(sel), delay = () => new Promise(resolve=>setTimeout(resolve,80));
    $(".assignment details summary").click();
    const image = $(".assignment-md img");
    if ($(".assignment-md strong")?.textContent !== "Соберите" || document.querySelectorAll(".assignment-md img").length !== 1 ||
        image.alt !== "Топология" || image.getAttribute("src") !== imagePath) return;
    image.loading = "eager"; await image.decode();
    if (image.naturalWidth !== 1) return;
    $("[data-open-stand]").click(); await delay();
    if ($(".sheet .assignment-md h2")?.textContent !== "Шаги" || $(".sheet .assignment-md img")?.getAttribute("src") !== imagePath || !$("[data-open-proxmox]")) return;
    $("#download-assignment").click();
    if (!filename?.endsWith(".md") || blob.type !== "text/markdown;charset=utf-8" || await blob.text() !== md || window.injection) return;
    hidden = true; window.dispatchEvent(new Event("online")); await delay();
    if ($("#download-assignment") || $(".assignment-md") || document.body.innerText.includes("Паспорт сети") || !$("[data-open-proxmox]")) return;
    location.hash = "assignments"; await delay();
    if ($("details summary") || !$("[data-deploy]").hidden || $("[data-open-stand]").hidden || document.body.textContent.includes(md)) return;
    document.body.dataset.qa = "pass";
  },50));
}
function fixture() {
  const mode = location.search.slice(1), secret = "fake-" + crypto.randomUUID();
  let reads = 0, writes = 0, actual = "", dialogs = 0, bad = false;
  window.prompt = window.confirm = window.alert = () => { dialogs++; throw Error("Unexpected dialog"); };
  const clipboard = {async writeText(value) { writes++; actual = value; }};
  if (mode !== "text") clipboard.write = async items => {
    writes++; const value = await (await items[0].getType("text/plain")).text();
    if (mode === "denied") throw new DOMException("Not allowed","NotAllowedError");
    actual = value;
  };
  Object.defineProperty(navigator,"clipboard",{value:clipboard});
  const assignment = {id:"task",title:"Fixture",description:"Fixture",steps:[],template_name:"Fixture",template_vmid:123};
  const stand = {id:"fixture", name:"Fixture", status:"ready",access:{kind:"proxmox",login:"root@pam",vmid:321,url:"https://10.39.0.9:8006/"}};
  window.fetch = async (url, options) => {
    let body, status = 200;
    switch (url) {
      case "/student-demo/api/me":
        if (mode === "login") { status = 401; body = {}; break; }
        body = {student:{login:"student01",name:"Fixture",group:"Lab"},assignments:[assignment]}; break;
      case "/student-demo/api/login-options": body = {demo_profiles:["student02"]}; break;
      case "/student-demo/api/stands/fixture": body = {assignment,stand}; break;
      case "/student-demo/api/stands/fixture/credentials":
        reads++; bad ||= options.method !== "POST" || options.body !== "{}";
        await new Promise(resolve => setTimeout(resolve,10));
        if (mode === "forbidden") { status = 403; body = {}; } else body = {password:secret}; break;
      default: bad = true; status = 404; body = {};
    }
    return new Response(JSON.stringify(body),{status});
  };
  window.addEventListener("load", () => setTimeout(() => {
    if (mode === "login") {
      const buttons = document.querySelectorAll("[data-demo-login]");
      if (buttons.length !== 1 || buttons[0].dataset.demoLogin !== "student02") return;
      buttons[0].click();
      if (document.querySelector("#login").value === "student02" && document.querySelector("#password").value === "DemoLab02!") document.body.dataset.qa = "pass";
      return;
    }
    document.querySelector("[data-copy-password]").click();
    setTimeout(() => {
      const copied = mode !== "denied" && mode !== "forbidden";
      const status = document.querySelector("[data-copy-status]").textContent;
      const exposed = document.documentElement.innerHTML.includes(secret) || [...document.querySelectorAll("input")].some(el => el.value === secret);
      if (!bad && !dialogs && reads === 1 && writes === 1 && actual === (copied ? secret : "")
          && !!status && (status === "Пароль скопирован.") === copied && !exposed
          && document.querySelector("[data-grant-form]").hidden && !localStorage.length && !sessionStorage.length) document.body.dataset.qa = "pass";
    },200);
  },100));
}
(async () => {
  const html = (dir, script) => fs.readFileSync(path.join(dir,"index.html"),"utf8").replace("<head>","<head><script>(" + script.toString() + ")()</script>");
  const server = http.createServer((req,res) => {
    if (req.url === "/?teacher") { res.setHeader("Content-Type","text/html; charset=utf-8"); res.end(html(path.dirname(web),teacherFixture)); return; }
    if (req.url.startsWith("/?roster")) { res.setHeader("Content-Type","text/html; charset=utf-8"); res.end(html(path.dirname(web),accountAdminFixture)); return; }
    if (req.url.startsWith("/student-demo/?")) { res.setHeader("Content-Type","text/html; charset=utf-8"); res.end(html(web,req.url.includes("?assignment") ? assignmentFixture : req.url.includes("?managed") ? managedFixture : fixture)); return; }
    const asset = req.url.split("?")[0];
    if (asset === "/student-demo/api/assignments/lab-2-276/images/" + "13579bdf".repeat(4)) {
      res.setHeader("Content-Type","image/png");
      res.end(Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0m0AAAAASUVORK5CYII=","base64")); return;
    }
    if (["/styles.css","/app.js","/assignment-images.js"].includes(asset)) {
      res.setHeader("Content-Type",asset.endsWith(".js") ? "text/javascript" : "text/css"); res.end(fs.readFileSync(path.join(web,"..",asset.slice(1)))); return;
    }
    const name = req.url.slice("/student-demo/".length);
    if (["student.css","student.js","assignment-markdown.js"].includes(name)) {
      res.setHeader("Content-Type",name.endsWith(".js") ? "text/javascript" : "text/css"); res.end(fs.readFileSync(path.join(web,name))); return;
    }
    res.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0,"127.0.0.1",resolve));
  const profile = fs.mkdtempSync(path.join(os.tmpdir(),"deployer-smoke-"));
  try {
    for (const mode of ["item","text","denied","forbidden","login","teacher","roster","roster-stale","roster-denied","roster-late","roster-history-nav","roster-history-late","roster-delete-stale","managed","assignment"]) {
      const url = "http://127.0.0.1:" + server.address().port + (mode === "teacher" || mode.startsWith("roster") ? "/?" + mode + (mode === "roster-history-nav" ? "#student-accounts" : "") : "/student-demo/?" + mode + (["managed","assignment"].includes(mode) ? "" : "#stands/fixture"));
      const {stdout} = await run(chrome,["--headless","--disable-gpu","--no-first-run","--disable-background-networking","--user-data-dir=" + profile,"--virtual-time-budget=3000","--dump-dom",url],{timeout:45000,maxBuffer:1_500_000});
      if (!stdout.includes('data-qa="pass"')) throw Error("Failed case: " + mode + (stdout.match(/data-qa-step="([^"]*)"/)?.[1] ? " at " + stdout.match(/data-qa-step="([^"]*)"/)[1] : ""));
      console.log("PASS", mode);
    }
  } finally {
    server.close();
    if (path.dirname(profile) === os.tmpdir() && path.basename(profile).startsWith("deployer-smoke-")) fs.rmSync(profile,{recursive:true,force:true,maxRetries:3,retryDelay:250});
  }
})().catch(err => { console.error(err.message); process.exitCode = 1; });
