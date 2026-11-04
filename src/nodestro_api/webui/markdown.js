/* A small Markdown renderer that builds DOM nodes (never HTML strings): results of a workflow are untrusted text.
   Supports headings, paragraphs, emphasis, inline code, code blocks, lists, block quotes, tables, horizontal rules
   and links (http, https and mailto only). Anything else stays plain text. */
"use strict";

(function () {
  const SAFE_LINK = /^(https?:|mailto:)/i;

  function inline(parent, text) {
    // tokens: `code`, **strong**, *em* / _em_, [label](url), bare https URLs
    const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\s][^*]*\*)|(\b_[^_\s][^_]*_\b)|(\[[^\]]+\]\([^)\s]+\))|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?"'])/g;
    let last = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) parent.appendChild(document.createTextNode(text.slice(last, match.index)));
      const token = match[0];
      if (match[1]) {
        const code = document.createElement("code");
        code.textContent = token.slice(1, -1);
        parent.appendChild(code);
      } else if (match[2] || match[3]) {
        const strong = document.createElement("strong");
        inline(strong, token.slice(2, -2));
        parent.appendChild(strong);
      } else if (match[4] || match[5]) {
        const em = document.createElement("em");
        inline(em, token.slice(1, -1));
        parent.appendChild(em);
      } else if (match[6]) {
        const close = token.indexOf("](");
        const label = token.slice(1, close);
        const url = token.slice(close + 2, -1);
        parent.appendChild(link(label, url));
      } else {
        parent.appendChild(link(token, token));
      }
      last = match.index + token.length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function link(label, url) {
    if (!SAFE_LINK.test(url)) return document.createTextNode(label);
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener noreferrer nofollow";
    a.target = "_blank";
    a.textContent = label;
    return a;
  }

  function cells(line) {
    let body = line.trim();
    if (body.startsWith("|")) body = body.slice(1);
    if (body.endsWith("|")) body = body.slice(0, -1);
    return body.split("|").map((cell) => cell.trim());
  }

  function render(source) {
    const root = document.createElement("div");
    root.className = "prose";
    const lines = String(source).replace(/\r\n?/g, "\n").split("\n");
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index += 1; continue; }
      const fence = line.match(/^\s*(```|~~~)/);
      if (fence) {
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        const body = [];
        index += 1;
        while (index < lines.length && !lines[index].trim().startsWith(fence[1])) { body.push(lines[index]); index += 1; }
        index += 1;
        code.textContent = body.join("\n");
        pre.appendChild(code);
        root.appendChild(pre);
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        const level = Math.min(heading[1].length + 1, 6);
        const h = document.createElement("h" + level);
        inline(h, heading[2].replace(/\s+#+\s*$/, ""));
        root.appendChild(h);
        index += 1;
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { root.appendChild(document.createElement("hr")); index += 1; continue; }
      if (line.trim().startsWith("|") && index + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[index + 1])) {
        const wrap = document.createElement("div");
        wrap.className = "table-wrap";
        const table = document.createElement("table");
        const head = document.createElement("thead");
        const row = document.createElement("tr");
        for (const cell of cells(line)) { const th = document.createElement("th"); inline(th, cell); row.appendChild(th); }
        head.appendChild(row);
        table.appendChild(head);
        const body = document.createElement("tbody");
        index += 2;
        while (index < lines.length && lines[index].trim().startsWith("|")) {
          const tr = document.createElement("tr");
          for (const cell of cells(lines[index])) { const td = document.createElement("td"); inline(td, cell); tr.appendChild(td); }
          body.appendChild(tr);
          index += 1;
        }
        table.appendChild(body);
        wrap.appendChild(table);
        root.appendChild(wrap);
        continue;
      }
      if (/^\s*>/.test(line)) {
        const quote = document.createElement("blockquote");
        const body = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) { body.push(lines[index].replace(/^\s*>\s?/, "")); index += 1; }
        quote.appendChild(render(body.join("\n")));
        root.appendChild(quote);
        continue;
      }
      const item = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
      if (item) {
        const ordered = /\d/.test(item[1]);
        const list = document.createElement(ordered ? "ol" : "ul");
        while (index < lines.length) {
          const current = lines[index].match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
          if (!current || /\d/.test(current[1]) !== ordered) break;
          const li = document.createElement("li");
          const task = current[2].match(/^\[([ xX])\]\s+(.*)$/);
          inline(li, task ? (task[1] === " " ? "☐ " : "☑ ") + task[2] : current[2]);
          list.appendChild(li);
          index += 1;
        }
        root.appendChild(list);
        continue;
      }
      const paragraph = [];
      while (index < lines.length && lines[index].trim() && !/^(#{1,6}\s|\s*```|\s*~~~|\s*>|\s*([-*+]|\d+[.)])\s)/.test(lines[index])) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      if (!paragraph.length) { paragraph.push(line.trim()); index += 1; }
      const p = document.createElement("p");
      paragraph.forEach((text, position) => {
        if (position) p.appendChild(document.createElement("br"));
        inline(p, text);
      });
      root.appendChild(p);
    }
    return root;
  }

  window.NodestroMarkdown = { render };
})();
