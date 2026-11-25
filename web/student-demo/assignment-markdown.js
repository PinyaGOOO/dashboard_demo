/* A small, deliberately HTML-free Markdown dialect shared by teacher and student. */
(() => {
  "use strict";
  const escape = text => text.replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[ch]);
  function destination(raw) {
    if (!/^https?:\/\//i.test(raw) || /[\u0000-\u0020\u007f\\<>"']/.test(raw)) return null;
    try {
      const url = new URL(raw);
      return url.hostname && !url.username && !url.password ? escape(url.href) : null;
    } catch { return null; }
  }
  function inline(text, depth = 0) {
    if (depth >= 3) return escape(text);
    // Limits on individual tokens keep malformed markers in class-sized documents cheap.
    const tokens = /\\([\\`*_[\]!|])|`([^`\n]{1,2048})`|(!?)\[([^\]\n]{1,512})\]\(([^()\s]{1,2048})\)|(\*\*|__)(?=\S)([^\n]{1,2048}?)\6|(\*|_)(?=\S)([^\n]{1,2048}?)\8/g;
    let html = "", end = 0;
    for (const m of text.matchAll(tokens)) {
      html += escape(text.slice(end,m.index));
      end = m.index + m[0].length;
      if (m[1]) html += escape(m[1]);
      else if (m[2]) html += `<code>${escape(m[2])}</code>`;
      else if (m[4] !== undefined) {
        // Image syntax is text-only: loading a classroom page must not contact an image host.
        const href = !m[3] && destination(m[5]);
        html += href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${escape(m[4])}</a>` : escape(m[4]);
      } else {
        const tag = m[6] ? "strong" : "em", mark = m[6] || m[8], value = m[7] || m[9];
        const word = c => !!c && /[\p{L}\p{N}]/u.test(c);
        if (/\s$/.test(value) || (mark.startsWith("_") && (word(text[m.index-1]) || word(text[end])))) html += escape(m[0]);
        else html += `<${tag}>${inline(value,depth+1)}</${tag}>`;
      }
    }
    return html + escape(text.slice(end));
  }
  const fence = /^ {0,3}(`{3,}|~{3,})([^\n]*)$/;
  const heading = /^ {0,3}(#{1,6})[\t ]+(.+)$/;
  const item = /^ {0,3}([-+*]|\d{1,9}[.)])[\t ]+(.+)$/;
  const quote = /^ {0,3}> ?/;
  const divider = /^ {0,3}(?:-{3,}|\*{3,}|_{3,})[\t ]*$/;
  function cells(line) {
    // A backslash-escaped pipe is part of the cell, not its separator.
    return line.trim().replace(/^\|/,'').replace(/(?<!\\)\|$/,'').split(/(?<!\\)\|/).map(s => s.trim());
  }
  function render(source) {
    if (typeof source !== "string") return "";
    const bounded = source.length <= 32000 ? source : Array.from(source).slice(0,32000).join("");
    const lines = bounded.replace(/\r\n?/g,"\n").split("\n"), blocks = [];
    for (let i = 0; i < lines.length;) {
      const line = lines[i++]; let m;
      if (!line.trim()) continue;
      if ((m = fence.exec(line))) {
        const marker = m[1], code = [];
        while (i < lines.length) {
          const next = lines[i++], closing = fence.exec(next);
          if (closing && closing[1][0] === marker[0] && closing[1].length >= marker.length && !closing[2].trim()) break;
          code.push(next);
        }
        blocks.push(`<pre><code>${escape(code.join("\n"))}</code></pre>`);
      } else if ((m = heading.exec(line))) {
        const level = m[1].length;
        blocks.push(`<h${level}>${inline(m[2])}</h${level}>`);
      } else if (divider.test(line)) blocks.push("<hr>");
      else if ((m = item.exec(line))) {
        const ordered = /^\d/.test(m[1]), tag = ordered ? "ol" : "ul", first = Number.parseInt(m[1],10);
        const items = [m[2]];
        while (i < lines.length && (m = item.exec(lines[i])) && /^\d/.test(m[1]) === ordered) { items.push(m[2]); i++; }
        blocks.push(`<${tag}${ordered && first !== 1 ? ` start="${first}"` : ""}>${items.map(v => `<li>${inline(v)}</li>`).join("")}</${tag}>`);
      } else if (quote.test(line)) {
        const parts = [line.replace(quote,"")];
        while (i < lines.length && quote.test(lines[i])) parts.push(lines[i++].replace(quote,""));
        blocks.push(`<blockquote>${parts.map(v => inline(v)).join("<br>")}</blockquote>`);
      } else if (line.includes("|") && i < lines.length && isTable(line,lines[i])) {
        const headers = cells(line), rows = []; i++;
        while (i < lines.length && lines[i].includes("|")) {
          const row = cells(lines[i]); if (row.length !== headers.length) break;
          rows.push(`<tr>${row.map(v => `<td>${inline(v)}</td>`).join("")}</tr>`); i++;
        }
        blocks.push(`<div class="md-table"><table><thead><tr>${headers.map(v => `<th scope="col">${inline(v)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`);
      } else {
        const parts = [line];
        while (i < lines.length && lines[i].trim() && ![fence, heading, item, quote, divider].some(re => re.test(lines[i])) && !isTable(lines[i],lines[i+1])) parts.push(lines[i++]);
        blocks.push(`<p>${parts.map(v => inline(v)).join("<br>")}</p>`);
      }
    }
    return blocks.join("\n");
  }
  function isTable(header, bar) {
    if (!bar || !bar.includes("|") || !header.includes("|")) return false;
    const cols = cells(bar);
    return cols.length >= 2 && cols.length === cells(header).length && cols.every(value => /^:?-{3,}:?$/.test(value));
  }
  globalThis.AssignmentMarkdown = Object.freeze({render});
})();
