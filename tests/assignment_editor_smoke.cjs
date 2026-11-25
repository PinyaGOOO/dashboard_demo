// Real app, isolated Chrome profile and a local fake API. Nothing is written to Deployer.
"use strict";
const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
const http = require("node:http"), run = require("node:util").promisify(require("node:child_process").execFile);
const web = path.join(__dirname,"../web");
function fixture() {
  const secret = "fixture-operator-key", draft = "Закрытое **условие**", saved = [];
  let challenge = true, lag = false;
  const uploads = new Map(), reads = [], revoked = new Set();
  let denyUpload = false;
  const origRevoke = URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL = s=>{revoked.add(s); origRevoke(s);};
  const bp = {id:1, code:"TEST", name:"Практика", template_vmid:276, tags:[], status:"active", student_visible:false, deploy_script:"echo ok", category:"Общий", version:"1"};
  const assignment = {assignment_markdown:draft, student_visible:false, student_groups:null};
  const classA="ССА <25> & А", classB="ССА-26", classC="ССА-27";
  let groups=[classA,classB,classC], groupReads=0, groupFailure=false;
  const reply = (obj,status=200) => new Response(JSON.stringify(obj),{status});
  const pause = ms => new Promise(resolve=>setTimeout(resolve,ms));
  window.fetch = async (url,opts={}) => {
    if (url === "/api/assignment-images" && opts.method === "POST") {
      if (denyUpload || opts.headers["X-Admin-Token"] !== secret) return reply({error:"fixture auth"},401);
      if (!(opts.body instanceof Blob) || opts.headers["Content-Type"] !== "image/png" || opts.body.type !== "image/png") return reply({},400);
      await pause(30);
      const id = (uploads.size+1).toString(16).padStart(32,"a");
      uploads.set(id,opts.body); return reply({id},201);
    }
    if (url.startsWith("/api/assignment-images/") && opts.method === "GET") {
      if (opts.headers["X-Admin-Token"] !== secret) return reply({},401);
      const id = url.split("/").pop(); reads.push(id); return new Response(uploads.get(id),{headers:{"Content-Type":"image/png"}});
    }
    if (url === "/api/bootstrap") return reply({blueprints:[bp],templates:[{name:"Nested",vmid:276}],stands:[],integration:{connected:true}, overview:{attention:0}});
    if (url === "/api/student-accounts?limit=1") {
      groupReads++;
      if (opts.headers["X-Admin-Token"] !== secret) return reply({},401);
      if (groupFailure) return reply({},503);
      return reply({groups, accounts:[]});
    }
    if (url === "/api/blueprints/1/assignment") {
      if (challenge && opts.headers["X-Admin-Token"] !== secret) return reply({error:"Нужен токен"},401);
      if (lag) await pause(120);
      return reply(assignment);
    }
    if (["/api/blueprints","/api/blueprints/1"].includes(url) && ["POST","PATCH"].includes(opts.method)) {
      const body = JSON.parse(opts.body); saved.push(body);
      if (opts.method === "PATCH") for (const key of Object.keys(assignment)) if (key in body) assignment[key]=body[key];
      return reply({id:1});
    }
    return reply({},404);
  };
  window.addEventListener("load",async () => {
    const $=s=>document.querySelector(s), assert=(v,k)=>{if(!v)throw Error(k);};
    const wait=async(s)=>{for(let i=0;i<80;i++){const el=$(s);if(el)return el;await pause(5);}throw Error(s);};
    const input=el=>el.dispatchEvent(new Event("input",{bubbles:true}));
    try {
      (await wait("[data-blueprint-edit]")).click();
      (await wait("[data-token-cancel]")).click();
      await pause(210); assert(!$("#blueprint-form")&&!saved.length,"unauthorized edit");
      $("[data-blueprint-edit]").click();
      (await wait("#admin-token-input")).value=secret; $("#admin-token-form").requestSubmit();
      const f=await wait("#blueprint-form"), md=f.elements.assignment_markdown, flag=f.elements.student_visible;
      assert(md.value===draft&&!flag.checked&&$("[data-assignment-audience]").value==="all"&&!groupReads,"stored all-group draft");
      flag.click(); md.value="  "; input(md); f.requestSubmit();
      assert(!saved.length&&!md.checkValidity(),"published blank");
      md.value="## Практика\n\nПроверьте **узел**. <span>Текст</span>"; input(md);
      const preview=$(".assignment-preview"); preview.open=true; await pause(25);
      assert(preview.querySelector("strong")?.textContent==="узел"&&!preview.querySelector("span"),"safe preview");
      f.requestSubmit(); await pause(210);
      assert(saved[0].student_visible===true&&saved[0].assignment_markdown===md.value&&saved[0].deploy_script==="echo ok","published payload");
      $("[data-blueprint-new]").click();
      const newForm=await wait("#blueprint-form"); newForm.elements.name.value="Черновик";
      assert(!newForm.elements.student_visible.checked,"default visible");
      newForm.elements.assignment_markdown.value="Ещё не готово"; newForm.requestSubmit(); await pause(210);
      assert(saved.length===2&&saved[1].student_visible===false&&saved[1].template_vmid===0&&saved[1].student_groups.length===0,"private draft payload");
      lag=true; challenge=false;
      $("[data-blueprint-edit]").click(); $(".modal__close").click(); $("[data-blueprint-new]").click();
      const open=await wait("#blueprint-form"); await pause(180);
      assert(open.isConnected&&!open.elements.assignment_markdown.value,"out of order read");
      const src=open.elements.assignment_markdown, send=$('[form="blueprint-form"]'), pick=$("[data-image-input]");
      const canvas=document.createElement("canvas"); canvas.width=3;canvas.height=2;
      canvas.getContext("2d").fillRect(1,0,1,1);
      const file = async (type, name)=>new File([await new Promise(resolve=>canvas.toBlob(resolve,type))],name,{type});
      const screenshot=await file("image/png","Сеть.png"), photo=await file("image/jpeg","Шаг 2.jpg");
      const paste=f=>{const d=new DataTransfer();d.items.add(f);const e=new ClipboardEvent("paste",{bubble:true,cancelable:true,clipboardData:d});src.dispatchEvent(e);return e;};
      const during=async predicate=>{for(let i=0;i<220;i++){if(predicate())return;await pause(10);}throw Error("waiting: "+($("[data-image-status]")?.textContent||""));};
      src.value="x [место] y";src.setSelectionRange(2,9);
      denyUpload=true;assert(paste(screenshot).defaultPrevented && send.disabled && src.readOnly,"paste/upload lock");
      await during(()=>!send.disabled); assert(!uploads.size && src.value==="x [место] y" && $("[data-blueprint-token]"),"auth preserved draft");
      denyUpload=false; $("[data-blueprint-token]").value=secret;
      paste(screenshot); await during(()=>!send.disabled);
      assert(src.value==="x ![Сеть](attachment:"+"a".repeat(31)+"1) y"&&!reads.length,"caret placement/no eager download");
      const pair=new DataTransfer(); pair.items.add(photo); pick.files=pair.files;
      src.setSelectionRange(src.value.length,src.value.length);pick.dispatchEvent(new Event("change"));
      await during(()=>!send.disabled); assert(uploads.size===2&&src.value.endsWith("![Шаг 2](attachment:"+"a".repeat(31)+"2)"),"picker converted JPEG");
      const second=uploads.get("a".repeat(31)+"2"), bytes=new DataView(await second.arrayBuffer());
      assert(bytes.getUint32(0)===0x89504e47 && bytes.getUint32(16)===3 && bytes.getUint32(20)===2,"PNG envelope, original size");
      $(".assignment-preview").open=true;
      await during(()=>open.querySelectorAll("article img").length===2);
      const previews=[...open.querySelectorAll("article img")].map(img=>img.src);
      assert(previews.every(s=>s.startsWith("blob:"+location.origin+"/")) && reads.length===2,"protected image previews");
      input(src);await pause(40);assert(reads.length===2,"no redundant fetch");
      const prev=src.value; paste(new File(["not an image"],"fake.svg",{type:"image/svg+xml"}));
      await during(()=>!send.disabled); assert(src.value===prev && uploads.size===2,"SVG not uploaded");
      $(".modal__close").click(); assert(previews.every(s=>revoked.has(s)),"preview URL cleanup");
      lag=false;
      const edit=async()=>{ $("[data-blueprint-edit]").click(); return await wait("#blueprint-form"); };
      const mode=()=>$("[data-assignment-audience]");
      const choose=value=>{mode().value=value;mode().dispatchEvent(new Event("change",{bubbles:true}));};
      const boxes=()=>[...document.querySelectorAll("[data-group-options] input")];
      const box=group=>boxes().find(b=>b.value===group);
      const targeted=await edit(), base=saved.length;
      assert(targeted.elements.student_visible.checked && mode().value==="all","unchanged publication");
      choose("selected"); await during(()=>boxes().length===3);
      targeted.requestSubmit(); assert(saved.length===base && !mode().checkValidity(),"empty audience cannot publish");
      box(classA).click(); box(classB).click(); targeted.requestSubmit(); await pause(210);
      assert(JSON.stringify(saved.at(-1).student_groups)===JSON.stringify([classA,classB]),"save exact multi-group audience");
      groups=[classA,classC]; groupFailure=true;
      const again=await edit(); assert(mode().value==="selected"&&box(classB).checked,"reopened stored selection");
      $("[data-group-picker]").open=true; await during(()=>!$("[data-retry-groups]").hidden);
      assert(box(classA).checked&&box(classB).checked&&mode().value==="selected","directory failure kept restrictions");
      again.elements.assignment_markdown.value+="!"; again.requestSubmit(); await pause(210);
      assert(!("student_groups" in saved.at(-1)) && assignment.student_groups.includes(classB),"text-only save cannot replace audience");
      const refreshed=await edit(); $("[data-group-picker]").open=true; await during(()=>!$("[data-retry-groups]").hidden);
      groupFailure=false; $("[data-retry-groups]").click(); await during(()=>!!box(classC));
      assert(box(classB).checked && box(classB).closest("label").textContent.includes("Пока нет учёток"),"retired group preserved");
      choose("all"); refreshed.requestSubmit(); await pause(210);
      assert(saved.at(-1).student_groups===null,"explicitly change to all groups");
      assignment.student_groups={invalid:true}; $("[data-blueprint-edit]").click(); await pause(230);
      assert(!$("#blueprint-form"),"invalid policy cannot be edited as unrestricted");
      document.body.dataset.qa="pass";
    } catch (e) {document.body.dataset.failure=e.message;}
  });
}
(async()=>{
  const server=http.createServer((req,res)=>{
    const assets={"/app.js":"app.js","/assignment-images.js":"assignment-images.js","/styles.css":"styles.css","/student-demo/assignment-markdown.js":"student-demo/assignment-markdown.js"};
    if(req.url==="/"){res.setHeader("Content-Type","text/html; charset=utf-8");return res.end(fs.readFileSync(path.join(web,"index.html"),"utf8").replace("<head>","<head><script>("+fixture.toString()+")()</script>"));}
    const file=assets[req.url];if(!file)return res.writeHead(404).end();
    res.setHeader("Content-Type",file.endsWith("js")?"text/javascript":"text/css");res.end(fs.readFileSync(path.join(web,file)));
  });
  await new Promise(resolve=>server.listen(0,"127.0.0.1",resolve));
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),"assignment-editor-smoke-"));
  try {
    const chrome=process.env.CHROME_BIN || (process.platform==="win32"?"C:/Program Files/Google/Chrome/Application/chrome.exe":"chromium");
    const {stdout}=await run(chrome,["--headless","--disable-gpu","--no-first-run","--disable-background-networking","--user-data-dir="+profile,"--virtual-time-budget=12000","--dump-dom","http://127.0.0.1:"+server.address().port+"/#blueprints"],{timeout:35000,maxBuffer:1500000});
    if(!stdout.includes('data-qa="pass"'))throw Error("Editor smoke failed: "+(stdout.match(/data-failure="([^"]*)"/)?.[1]||"no result"));
    console.log("PASS editor: gated draft, Markdown, images, group selection / reopen / directory failure / publication");
  } finally {
    server.close();
    if(path.dirname(profile)===os.tmpdir() && path.basename(profile).startsWith("assignment-editor-smoke-"))fs.rmSync(profile,{recursive:true,force:true,maxRetries:3,retryDelay:250});
  }
})().catch(err=>{console.error(err.message);process.exitCode=1;});
