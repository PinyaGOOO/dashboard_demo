/* Private, same-origin attachments for the operator's Markdown editor. */
(() => {
  "use strict";
  const MiB = 1024 * 1024, maxInput = 12 * MiB, maxPNG = 2 * MiB, maxImages = 12;
  const types = ["image/png","image/jpeg","image/webp"];
  const badFormat = "Выберите файл PNG, JPEG или WebP.";
  function dimensions(buffer, type) {
    const a = new Uint8Array(buffer), v = new DataView(buffer);
    const marker = (i, text) => [...text].every((ch,j) => a[i+j] === ch.charCodeAt(0));
    if (type === "image/png" && a.length >= 33 && marker(0,"\x89PNG\r\n\x1a\n") && marker(12,"IHDR"))
      return [v.getUint32(16),v.getUint32(20)];
    if (type === "image/jpeg" && a[0] === 255 && a[1] === 216) {
      let i = 2;
      while (i + 8 < a.length && a[i++] === 255) {
        while (a[i] === 255) i++;
        const tag = a[i++];
        if (i+7 > a.length || tag === 218 || tag === 217) break;
        const n = v.getUint16(i);
        if (n < 2 || i+n > a.length) break;
        if ([192,193,194,195,197,198,199,201,202,203,205,206,207].includes(tag) && n >= 8)
          return [v.getUint16(i+5),v.getUint16(i+3)];
        i += n;
      }
    }
    if (type === "image/webp" && a.length >= 30 && marker(0,"RIFF") && marker(8,"WEBP")) {
      const uint24 = i => a[i] | (a[i+1]<<8) | (a[i+2]<<16);
      if (marker(12,"VP8X")) return [1+uint24(24),1+uint24(27)];
      if (marker(12,"VP8L") && a[20] === 47) {
        const n = v.getUint32(21,true); return [1+(n & 16383),1+((n>>>14) & 16383)];
      }
      if (marker(12,"VP8 ") && marker(23,"\x9d\x01\x2a")) return [v.getUint16(26,true)&16383,v.getUint16(28,true)&16383];
    }
    throw new Error(badFormat);
  }
  function checkShape(w,h) {
    if (!w || !h || Math.max(w,h)>16384 || w*h>32000000) throw new Error("Слишком большое изображение: максимум 32 Мп и 16 384 пикселя по стороне.");
  }
  async function canonicalPNG(file) {
    if (!types.includes(file.type)) throw new Error(badFormat);
    if (!file.size || file.size > maxInput) throw new Error("Размер исходного файла должен быть не больше 12 МиБ.");
    checkShape(...dimensions(await file.slice(0,MiB).arrayBuffer(),file.type));
    const src = URL.createObjectURL(file), img = new Image(), canvas = document.createElement("canvas");
    try {
      img.src = src;
      try { await img.decode(); } catch { throw new Error("Не удалось прочитать изображение."); }
      const w = img.naturalWidth, h = img.naturalHeight;
      checkShape(w,h);
      let scale = Math.min(1,4096/Math.max(w,h),Math.sqrt(8000000/(w*h)));
      for (let tries=0; tries<7; tries++) {
        canvas.width = Math.max(1,Math.floor(w*scale)); canvas.height = Math.max(1,Math.floor(h*scale));
        const ctx = canvas.getContext("2d");
        if (!ctx) throw new Error("Браузер не смог подготовить изображение.");
        ctx.imageSmoothingQuality = "high";
        ctx.drawImage(img,0,0,canvas.width,canvas.height);
        const png = await new Promise(resolve=>canvas.toBlob(resolve,"image/png"));
        if (!png) throw new Error("Не удалось подготовить PNG.");
        if (png.size <= maxPNG) return png;
        scale *= Math.min(.85,.94*Math.sqrt(maxPNG/png.size));
      }
      throw new Error("Не удалось уменьшить PNG до 2 МиБ. Уменьшите изображение и повторите.");
    } finally { URL.revokeObjectURL(src); canvas.width=canvas.height=1; }
  }
  function install({form,markdown,preview,content,submit,alive,token,unlock}) {
    const button = form.querySelector("[data-upload-image]"), picker = form.querySelector("[data-image-input]");
    const note = form.querySelector("[data-image-status]"), controller = new AbortController();
    const urls = new Map(), failures = new Set(), loading = new Set();
    let requested = new Set(), pending = false, closed = false;
    const active = () => !closed && alive();
    const endpoint = "/api/assignment-images";
    const say = text => { if (active()) note.textContent = text; };
    function dispose() {
      closed = true; controller.abort(); requested.clear();
      urls.forEach(url => URL.revokeObjectURL(url)); urls.clear();
    }
    async function request(id, body) {
      const headers = {}, key = token();
      if (key) headers["X-Admin-Token"] = key;
      if (body) headers["Content-Type"]="image/png";
      const response = await fetch(id ? endpoint+"/"+id : endpoint, {method:body?"POST":"GET",
        headers,body,signal:controller.signal,cache:"no-store",credentials:"same-origin",redirect:"error"});
      if (!response.ok) {
        if (response.status === 401) {
          if (active()) unlock();
          throw new Error("Введите административный токен и повторите действие. Текст задания сохранён в форме.");
        }
        const detail = await response.json().catch(()=>({}));
        throw new Error(detail.error || (response.status === 413 ? "Изображение слишком большое для сервера." : "Не удалось получить или загрузить изображение."));
      }
      return response;
    }
    function paint() {
      if (!active() || !preview.open || !window.AssignmentMarkdown) return;
      const wanted = new Set();
      const html = window.AssignmentMarkdown.render(markdown.value,{imageURL(id) {
        if (!/^[0-9a-f]{32}$/.test(id) || (!wanted.has(id) && wanted.size>=maxImages)) return null;
        wanted.add(id); return urls.get(id) || null;
      }});
      requested = wanted;
      for (const [id,url] of urls) if (!wanted.has(id)) { URL.revokeObjectURL(url); urls.delete(id); }
      if (markdown.value.trim()) content.innerHTML = html;
      else content.textContent = "Здесь появится оформленное условие задания.";
      pump();
    }
    function pump() {
      if (!active() || !preview.open) return;
      for (const id of requested) {
        if (loading.size >= 3) return;
        if (loading.has(id) || urls.has(id) || failures.has(id)) continue;
        loading.add(id);
        (async()=>{
          try {
            const response = await request(id);
            if (!/^image\/png(?:;|$)/i.test(response.headers.get("Content-Type") || "")) throw new Error("Неверный формат ответа сервера.");
            const blob = await response.blob();
            if (active() && requested.has(id)) urls.set(id,URL.createObjectURL(blob));
          } catch (e) {
            if (active()) { failures.add(id); say(e.message); }
          } finally { loading.delete(id); if (active()) paint(); }
        })();
      }
    }
    async function insert(files) {
      if (!active() || submit.disabled || !files.length) return;
      const refs = new Set(Array.from(markdown.value.matchAll(/!\[[^\]\n]*\]\(attachment:([0-9a-f]{32})\)/g),m=>m[1]));
      if (refs.size+files.length>maxImages) { say("В одно задание можно добавить до 12 изображений."); return; }
      const start = markdown.selectionStart, end = markdown.selectionEnd;
      const alts = files.map(file=>(file.name || "").replace(/\.[a-z0-9]+$/i,"").replace(/[\[\]\(\)\\<>*_\x00-\x1f\x7f]/g," ").trim().slice(0,100) || "Изображение");
      if (markdown.value.length-(end-start)+alts.reduce((n,alt)=>n+alt.length+50,0)>32000) {
        say("Ссылка на картинку не поместится: лимит задания — 32 000 символов."); return;
      }
      pending = true; button.disabled = submit.disabled = markdown.readOnly = true;
      try {
        const snippets = [];
        for (const [i,file] of files.entries()) {
          say("Загружаем изображение "+(i+1)+" из "+files.length+"…");
          const png = await canonicalPNG(file);
          if (!active()) return;
          const result = await (await request(null,png)).json();
          if (!active()) return;
          if (!/^[a-f0-9]{32}$/.test(result.id || "")) throw new Error("Сервер вернул некорректную ссылку на изображение.");
          snippets.push("!["+alts[i]+"](attachment:"+result.id+")");
        }
        markdown.setRangeText(snippets.join("\n"),start,end,"end");
        markdown.dispatchEvent(new Event("input",{bubbles:true})); markdown.focus();
        say("Изображение добавлено. Сохраните сценарий, чтобы закрепить его в задании.");
      } catch (e) { if (active()) say(e.message || "Не удалось загрузить изображение."); }
      finally { if (active()) { pending=false; button.disabled=submit.disabled=markdown.readOnly=false; } }
    }
    button.addEventListener("click",()=>picker.click());
    picker.addEventListener("change",()=>{const files=Array.from(picker.files); picker.value=""; insert(files);});
    markdown.addEventListener("paste",e=>{
      const files=Array.from(e.clipboardData?.items || []).filter(i=>i.kind==="file").map(i=>i.getAsFile()).filter(Boolean);
      if (!files.length) return;
      e.preventDefault(); insert(files);
    });
    preview.addEventListener("toggle",()=>{ if (preview.open) {failures.clear(); paint();} });
    window.addEventListener("pagehide", dispose,{once:true,signal:controller.signal});
    return {paint,dispose,get pending(){return pending;}};
  }
  globalThis.AssignmentImageEditor = Object.freeze({install});
})();
