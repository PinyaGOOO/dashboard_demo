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
    $("[data-student-batch]").click();
    const form = $("#student-batch");
    if (form.elements.count.value !== "20" || form.elements.prefix.value !== "student") return;
    form.elements.group.value = group; form.elements.prefix.value = "class-gr"; form.elements.token.value = token;
    form.requestSubmit(); form.requestSubmit(); await new Promise(r=>setTimeout(r,100));
    const roster = $(".student-batch-result textarea");
    if (!roster || roster.value.split("\n").length !== 20 || !roster.value.includes("class-gr20\tfake-password-19")) return;
    $("[data-copy-roster]").click(); await new Promise(r=>setTimeout(r,5));
    if (!correct || submissions !== 1 || copied !== roster.value || JSON.stringify(localStorage).includes("fake-") || JSON.stringify(sessionStorage).includes("fake-")) return;
    $(".modal__close").click();
    if (roster.value === "") document.body.dataset.qa = "pass";
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
    if (req.url.startsWith("/student-demo/?")) { res.setHeader("Content-Type","text/html; charset=utf-8"); res.end(html(web,req.url.includes("?managed") ? managedFixture : fixture)); return; }
    const asset = req.url.split("?")[0];
    if (["/styles.css","/app.js"].includes(asset)) {
      res.setHeader("Content-Type",asset.endsWith(".js") ? "text/javascript" : "text/css"); res.end(fs.readFileSync(path.join(web,"..",asset.slice(1)))); return;
    }
    const name = req.url.slice("/student-demo/".length);
    if (["student.css","student.js"].includes(name)) {
      res.setHeader("Content-Type",name.endsWith(".js") ? "text/javascript" : "text/css"); res.end(fs.readFileSync(path.join(web,name))); return;
    }
    res.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0,"127.0.0.1",resolve));
  const profile = fs.mkdtempSync(path.join(os.tmpdir(),"deployer-smoke-"));
  try {
    for (const mode of ["item","text","denied","forbidden","login","teacher","managed"]) {
      const url = "http://127.0.0.1:" + server.address().port + (mode === "teacher" ? "/?teacher" : "/student-demo/?" + mode + (mode === "managed" ? "" : "#stands/fixture"));
      const {stdout} = await run(chrome,["--headless","--disable-gpu","--no-first-run","--disable-background-networking","--user-data-dir=" + profile,"--virtual-time-budget=3000","--dump-dom",url],{timeout:45000,maxBuffer:1_500_000});
      if (!stdout.includes('data-qa="pass"')) throw Error("Failed case: " + mode);
      console.log("PASS", mode);
    }
  } finally {
    server.close();
    if (path.dirname(profile) === os.tmpdir() && path.basename(profile).startsWith("deployer-smoke-")) fs.rmSync(profile,{recursive:true,force:true,maxRetries:3,retryDelay:250});
  }
})().catch(err => { console.error(err.message); process.exitCode = 1; });
