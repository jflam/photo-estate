// The book's behaviour: theme, chapter drawer, copy buttons, search, the page's table of contents
// following the reader, and Mermaid diagrams drawn in the theme the reader chose.

const root = document.documentElement;
const $ = (s, c = document) => c.querySelector(s);
const $$ = (s, c = document) => [...c.querySelectorAll(s)];

// ---------------------------------------------------------------- theme
const dark = () => root.getAttribute("data-theme") === "dark";
function setTheme(t, remember) {
  root.setAttribute("data-theme", t);
  if (remember) { try { localStorage.setItem("book-theme", t); } catch (e) {} }
  drawDiagrams();
}
$(".theme")?.addEventListener("click", () => setTheme(dark() ? "light" : "dark", true));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  let stored = null;
  try { stored = localStorage.getItem("book-theme"); } catch (_) {}
  if (!stored) setTheme(e.matches ? "dark" : "light", false);     // follow the system until the reader chooses
});

// ---------------------------------------------------------------- drawer (small screens)
const closeDrawer = () => { document.body.classList.remove("open"); $(".menu")?.setAttribute("aria-expanded", "false"); $(".scrim").hidden = true; };
$(".menu")?.addEventListener("click", () => {
  const open = document.body.classList.toggle("open");
  $(".menu").setAttribute("aria-expanded", String(open));
  $(".scrim").hidden = !open;
});
$(".scrim")?.addEventListener("click", closeDrawer);
$$("#sidebar nav a").forEach((a) => a.addEventListener("click", closeDrawer));
$("#sidebar .current")?.scrollIntoView({ block: "center" });

// ---------------------------------------------------------------- copy buttons
$$(".code .copy").forEach((b) => b.addEventListener("click", async () => {
  const text = $("code", b.parentElement).innerText;
  try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; } catch (e) { b.textContent = "Press ⌘C"; }
  setTimeout(() => (b.textContent = "Copy"), 1400);
}));

// ---------------------------------------------------------------- on this page
const links = $$(".toc a");
if (links.length) {
  const byId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
  const heads = [...byId.keys()].map((id) => document.getElementById(id)).filter(Boolean);
  const mark = () => {
    let cur = heads[0];
    for (const h of heads) if (h.getBoundingClientRect().top < 120) cur = h;
    links.forEach((a) => a.classList.toggle("on", cur && a === byId.get(cur.id)));
  };
  addEventListener("scroll", mark, { passive: true });
  mark();
}

// ---------------------------------------------------------------- search
const input = $("#q"), box = $("#results");
let index = null, picked = -1;
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
async function load() {
  if (!index) index = await fetch("assets/search.json").then((r) => r.json()).catch(() => []);
  return index;
}
function snippet(text, words) {
  const low = text.toLowerCase();
  let at = Math.min(...words.map((w) => (low.indexOf(w) < 0 ? 1e9 : low.indexOf(w))));
  at = at === 1e9 ? 0 : Math.max(0, at - 50);
  let s = esc(text.slice(at, at + 150));
  for (const w of words) s = s.replace(new RegExp(`(${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig"), "<mark>$1</mark>");
  return (at ? "…" : "") + s + "…";
}
async function find(q) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) { box.hidden = true; return; }
  const rows = await load();
  const hits = [];
  for (const r of rows) {
    const head = (r.h + " " + r.t).toLowerCase(), body = r.x.toLowerCase();
    if (!words.every((w) => head.includes(w) || body.includes(w))) continue;
    let score = 0;
    for (const w of words) score += (r.h.toLowerCase().includes(w) ? 8 : 0) + (r.t.toLowerCase().includes(w) ? 3 : 0) + Math.min(5, body.split(w).length - 1);
    hits.push([score, r]);
  }
  hits.sort((a, b) => b[0] - a[0]);
  picked = -1;
  box.innerHTML = hits.length
    ? hits.slice(0, 8).map(([, r]) => `<a href="${r.p}${r.a ? "#" + r.a : ""}"><small>${esc(r.t)}</small><b>${esc(r.h || r.t)}</b><p>${snippet(r.x, words)}</p></a>`).join("")
    : `<div class="none">Nothing for “${esc(q)}”.</div>`;
  box.hidden = false;
}
input?.addEventListener("input", () => find(input.value.trim()));
input?.addEventListener("focus", () => { load(); if (input.value.trim()) find(input.value.trim()); });
input?.addEventListener("keydown", (e) => {
  const items = $$("a", box);
  if (e.key === "Escape") { box.hidden = true; input.blur(); }
  else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    picked = (picked + (e.key === "ArrowDown" ? 1 : -1) + items.length) % Math.max(items.length, 1);
    items.forEach((a, i) => a.classList.toggle("on", i === picked));
    items[picked]?.scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter") { (items[picked] || items[0])?.click(); }
});
document.addEventListener("click", (e) => { if (!e.target.closest(".search")) box.hidden = true; });
addEventListener("keydown", (e) => {
  if (e.key === "/" && !/input|textarea/i.test(document.activeElement.tagName)) { e.preventDefault(); document.body.classList.add("open"); input.focus(); }
});

// ---------------------------------------------------------------- diagrams
const pres = $$("pre.mermaid");
pres.forEach((p) => (p.dataset.src = p.textContent));
let mermaid = null, drawing = Promise.resolve();
function css(name) { return getComputedStyle(root).getPropertyValue(name).trim(); }
function drawDiagrams() {
  if (!pres.length) return;
  drawing = drawing.then(async () => {
    try {
      mermaid ??= (await import("https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs")).default;
    } catch (e) { return; }                                   // offline: the diagram stays as its source
    const d = dark();
    mermaid.initialize({
      startOnLoad: false, securityLevel: "loose", theme: "base", fontFamily: css("--sans"),
      flowchart: { useMaxWidth: false, htmlLabels: true, curve: "basis" }, sequence: { useMaxWidth: false }, pie: { useMaxWidth: false },
      themeVariables: {
        darkMode: d, background: css("--paper-2"), fontSize: "14px",
        primaryColor: d ? "#2a2620" : "#fff7e8", primaryTextColor: css("--ink"), primaryBorderColor: css("--sun"),
        secondaryColor: css("--paper-3"), tertiaryColor: css("--paper-2"),
        lineColor: css("--ink-3"), textColor: css("--ink"), mainBkg: d ? "#2a2620" : "#fff7e8", nodeBorder: css("--sun"),
        clusterBkg: css("--paper-3"), clusterBorder: css("--line"), edgeLabelBackground: css("--paper-2"),
        actorBkg: d ? "#2a2620" : "#fff7e8", actorBorder: css("--sun"), actorTextColor: css("--ink"), actorLineColor: css("--ink-3"),
        signalColor: css("--ink-2"), signalTextColor: css("--ink"), noteBkgColor: css("--sun-wash"), noteTextColor: css("--ink"),
        noteBorderColor: css("--sun"), labelBoxBkgColor: css("--paper-3"), labelTextColor: css("--ink"),
        pie1: "#c9690d", pie2: "#e0a458", pie3: "#8a6d3b", pie4: "#b5a642", pie5: "#6f8f72", pie6: "#5b7c99", pie7: "#8d6a9f", pie8: "#a85c5c",
      },
    });
    for (const p of pres) {
      p.removeAttribute("data-processed");
      p.textContent = p.dataset.src;
    }
    try { await mermaid.run({ nodes: pres }); } catch (e) { console.warn("diagram:", e); }
    for (const svg of $$("pre.mermaid svg")) {            // natural size, shrunk to fit, but never below a readable width
      const w = svg.viewBox?.baseVal?.width;
      if (w) { svg.style.width = w + "px"; svg.style.minWidth = Math.min(w, 620) + "px"; }
    }
  });
}
drawDiagrams();
