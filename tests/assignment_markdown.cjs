"use strict";
const assert = require("node:assert/strict");
require("../web/student-demo/assignment-markdown.js");
const {render} = globalThis.AssignmentMarkdown;

assert.equal(render(null),"");
assert.equal(render("## Тема\r\n\r\n**План** и *теория*\n`ip -br a`"),
  "<h2>Тема</h2>\n<p><strong>План</strong> и <em>теория</em><br><code>ip -br a</code></p>");
assert.equal(render("- первый\n- второй\n\n3. третий\n\n> цитата"),
  '<ul><li>первый</li><li>второй</li></ul>\n<ol start="3"><li>третий</li></ol>\n<blockquote>цитата</blockquote>');
assert.equal(render("````sh\nip addr\n```\n</pre>\n````\n---"),"<pre><code>ip addr\n```\n&lt;/pre&gt;</code></pre>\n<hr>");
assert.match(render("| Поле | Значение |\n| :--- | ---: |\n| if | a\\|b |"), /<th scope="col">Поле<\/th>.*<td>a\|b<\/td>/);
assert.equal(render("vmbr0_ip_mask"),"<p>vmbr0_ip_mask</p>");
assert.equal(render("[Источник](https://example.test/?a=1&b=2)"), '<p><a href="https://example.test/?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">Источник</a></p>');
for (const url of ["javascript:void", "javascript:alert(1)","jAvAsCrIpT:foo", "data:text/html,hello","//untrusted.test/","/api/login","https://user:pass@example.test/","http:\\untrusted.test/","https://","https://example.test/\"onclick=\"hi"]) {
  assert.doesNotMatch(render(`[Метод](${url})`),/<a\b/i);
}
const html = render("# <img src=x onerror=alert(1)>\n\n[<svg/onload=x>](https://example.test/)\n\n```\n</code><SCRIPT>alert(1)</SCRIPT>\n```\n\n![image](https://external.test/image.png)");
assert.doesNotMatch(html,/<(?:script|svg|img)\b/i);
assert.doesNotMatch(html,/external\.test/);
assert.match(html,/&lt;img/);
assert.ok(render("[".repeat(32000)).endsWith("</p>"));
const id = "abcdef0123456789".repeat(2), local = "/student-demo/api/assignments/lab-2-276/images/" + id;
const attachment = '![Схема "VM"](attachment:' + id + ')';
const imageURL = key => (assert.equal(key,id),local);
assert.match(render(attachment,{imageURL}), new RegExp('<img class="assignment-image" src="' + local + '" alt="Схема &quot;VM&quot;"'));
assert.match(render("![](attachment:" + id + ")",{imageURL}), /<img\b[^>]* alt=""/);
assert.equal(render(attachment),"<p>Схема &quot;VM&quot;</p>"); // offline export without asset service
assert.doesNotMatch(render("`" + attachment + "`",{imageURL}),/<img\b/);
assert.doesNotMatch(render(attachment.replace(id,id.toUpperCase()),{imageURL}),/<img\b/);
assert.doesNotMatch(render("![x]("+local+")",{imageURL}),/<img\b/);
for (const src of ["//outside.invalid/x", "data:image/png;base64,x", "blob:https://outside.invalid/" + id,
    "/api/assignment-images/" + id, local+"?token=demo",local+"#fragment",local.slice(0,-1)+"f",local+"\n"]) {
  assert.doesNotMatch(render(attachment,{imageURL:()=>src}),/<img\b/);
}
const viaBlob = "blob:https://class.test/86ea87f9-99ca-4d4f-89bd-32ffc257892b";
globalThis.location = {origin:"https://class.test"};
assert.match(render(attachment,{imageURL:()=>viaBlob}),/<img\b/);
assert.doesNotMatch(render(attachment,{imageURL:()=>viaBlob.replace("class.test","class.test.elsewhere.test")}),/<img\b/);
delete globalThis.location;
console.log("PASS assignment-markdown: scoped attachments, escaped alt, protected preview, external images never requested");
