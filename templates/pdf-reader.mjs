import * as pdfjs from "/api/pdfjs/build/pdf.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/api/pdfjs/build/pdf.worker.mjs";
const $ = id => document.getElementById(id);
const message = text => { $("message").textContent = text; };
const asset = "/api/pdfjs/";
let pdf, page = 1, zoom = 1, renderTask, generation = 0, printing = false;
let hits = [], hit = -1, searchGeneration = 0;
const maxPixels = 6000000;
let layerTask, visibleLayer, scrollMatch = false, searching = false, searchStatus = "";
// Search offsets are UTF-16 offsets into the original PDF.js text items.
function pageMatches(items, query) {
  let text = "", offsets = [], previous = null;
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (typeof item.str !== "string") continue;
    const a = previous?.transform, b = item.transform;
    const gap = a && b && Math.abs(a[1]) < 0.01 && Math.abs(b[1]) < 0.01 &&
      (Math.abs(b[5]-a[5]) > Math.max(1, previous.height*0.5) ||
       b[4]-a[4]-previous.width > Math.max(1, previous.height*0.15));
    if (previous && (previous.hasEOL || gap)) { text += " "; offsets.push(null); }
    let j = 0;
    for (const ch of item.str) {
      const folded = ch.toLowerCase();
      text += folded;
      for (let k = 0; k < folded.length; k++) offsets.push({i, start:j, end:j+ch.length});
      j += ch.length;
    }
    previous = item;
  }
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  const hits = [];
  for (let start = text.indexOf(needle); start !== -1; start = text.indexOf(needle, start+needle.length)) {
    const segments = [];
    for (const offset of offsets.slice(start, start+needle.length)) {
      if (!offset) continue;
      const last = segments.at(-1);
      if (last?.i === offset.i) last.end = offset.end;
      else segments.push({...offset});
    }
    if (segments.length) hits.push(segments);
  }
  return hits;
}
function wrappedMatch(index, delta, count) {
  return count ? (index + delta + count) % count : -1;
}



async function draw(target, number, scale, limit = maxPixels) {
  const source = await pdf.getPage(number);
  const base = source.getViewport({scale: 1});
  const bounded = Math.min(scale, Math.sqrt(limit / (base.width * base.height)));
  const viewport = source.getViewport({scale: bounded});
  target.width = Math.max(1, Math.floor(viewport.width));
  target.height = Math.max(1, Math.floor(viewport.height));
  const task = source.render({canvas: target, canvasContext: target.getContext("2d"), viewport,
    annotationMode: pdfjs.AnnotationMode.DISABLE});
  await task.promise;
}

async function show(number) {
  if (!pdf) return;
  page = Math.max(1, Math.min(pdf.numPages, Number(number) || 1));
  $("page").value = page;
  $("prev").disabled = page === 1;
  $("next").disabled = page === pdf.numPages;
  const token = ++generation;
  if (layerTask) { layerTask.cancel(); layerTask = null; }
  visibleLayer = null;
  $("text-layer").replaceChildren(); $("highlights").replaceChildren();
  updateMatches();
  if (renderTask) { renderTask.cancel(); try { await renderTask.promise; } catch {} }
  try {
    const source = await pdf.getPage(page);
    if (token !== generation) return;
    const base = source.getViewport({scale: 1});
    const scale = Math.min(zoom * Math.max(0.1, ($("document").clientWidth - 12) / base.width),
      Math.sqrt(maxPixels / (base.width * base.height)));
    const viewport = source.getViewport({scale});
    const canvas = $("paper");
    canvas.width = Math.max(1, Math.floor(viewport.width));
    canvas.height = Math.max(1, Math.floor(viewport.height));
    renderTask = source.render({canvas, canvasContext: canvas.getContext("2d"), viewport,
      annotationMode: pdfjs.AnnotationMode.DISABLE});
    await renderTask.promise;
    if (token !== generation) return;
    message("");
    const content = await source.getTextContent();
    if (token !== generation) return;
    const container = $("text-layer");
    container.style.setProperty("--scale-factor", String(viewport.scale));
    container.style.setProperty("--total-scale-factor", String(viewport.scale));
    container.style.setProperty("--user-unit", "1");
    layerTask = new pdfjs.TextLayer({textContentSource:content, container, viewport});
    await layerTask.render();
    if (token !== generation) return;
    visibleLayer = {items:content.items, divs:layerTask.textDivs};
    paintMatches();
  } catch (error) {
    if (token === generation && error.name !== "RenderingCancelledException") message("Unable to render this page.");
  }
}

async function thumbnails() {
  const panel = $("thumbs");
  panel.hidden = !panel.hidden;
  $("toggle").setAttribute("aria-expanded", String(!panel.hidden));
  if (panel.hidden || panel.childElementCount || !pdf) return;
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      observer.unobserve(entry.target);
      const canvas = document.createElement("canvas");
      entry.target.append(canvas);
      draw(canvas, Number(entry.target.dataset.page), 0.18, 40000).catch(() => canvas.remove());
    }
  }, {root: panel});
  for (let n = 1; n <= pdf.numPages; n++) {
    const button = document.createElement("button");
    button.textContent = String(n); button.dataset.page = String(n);
    button.onclick = () => show(n);
    panel.append(button); observer.observe(button);
  }
}

function updateMatches() {
  $("find-prev").disabled = $("find-next").disabled = searching || !hits.length;
  const count = hits.filter(h => h.page === page).length;
  $("matches").textContent = searching ? "Searching…" : hits.length ?
    (hit+1) + " of " + hits.length + " · " + count + " on this page" : searchStatus;
}
function paintMatches() {
  const overlay = $("highlights");
  overlay.replaceChildren();
  if (!visibleLayer) return;
  const {items, divs} = visibleLayer;
  const indices = new Map();
  let index = 0;
  items.forEach((item,i) => { if (typeof item.str === "string") indices.set(i,index++); });
  if (index !== divs.length) return;
  const origin = overlay.getBoundingClientRect();
  let active;
  hits.forEach((match,i) => {
    if (match.page !== page) return;
    for (const segment of match.segments) {
      const div = divs[indices.get(segment.i)];
      if (!div || div.textContent !== items[segment.i].str || !div.firstChild ||
          div.firstChild.nodeType !== Node.TEXT_NODE) continue;
      const range = document.createRange();
      range.setStart(div.firstChild, segment.start); range.setEnd(div.firstChild, segment.end);
      for (const rect of range.getClientRects()) {
        if (!rect.width || !rect.height) continue;
        const mark = document.createElement("span");
        mark.className = i === hit ? "active-match" : "search-match";
        Object.assign(mark.style,{left:(rect.left-origin.left)+"px",top:(rect.top-origin.top)+"px",
          width:rect.width+"px",height:rect.height+"px"});
        overlay.append(mark);
        if (i === hit && !active) active = mark;
      }
    }
  });
  if (scrollMatch && active) {
    active.scrollIntoView({block:"center",inline:"center"});
    scrollMatch = false;
  }
}
async function find() {
  if (!pdf) return;
  const query = $("find").value.trim();
  const token = ++searchGeneration;
  hits = []; hit = -1; scrollMatch = false; searchStatus = "";
  searching = !!query;
  updateMatches(); paintMatches();
  if (!query) return;
  let hasText = false;
  const results = [];
  try {
    for (let n = 1; n <= pdf.numPages; n++) {
      const source = await pdf.getPage(n);
      if (token !== searchGeneration) return;
      const content = await source.getTextContent();
      if (token !== searchGeneration) return;
      hasText ||= content.items.some(item => item.str?.trim());
      for (const segments of pageMatches(content.items,query)) results.push({page:n,segments});
    }
    if (token !== searchGeneration) return;
    hits = results; searching = false;
    if (hits.length) nextMatch(1);
    else { searchStatus = hasText ? "No matches" : "No searchable text"; updateMatches(); }
  } catch {
    if (token === searchGeneration) { searching = false; searchStatus = "Search unavailable"; updateMatches(); }
  }
}
function nextMatch(delta = 1) {
  if (searching || !hits.length) return;
  hit = wrappedMatch(hit < 0 && delta < 0 ? 0 : hit,delta,hits.length);
  scrollMatch = true;
  updateMatches();
  if (page === hits[hit].page && visibleLayer) paintMatches();
  else show(hits[hit].page);
}

async function printDocument() {
  if (!pdf || printing) return;
  printing = true; $("print").disabled = true;
  const output = $("print-pages");
  try {
    if (pdf.numPages > 100) throw Error("limit");
    for (let n = 1; n <= pdf.numPages; n++) {
      message("Preparing print page " + n + " of " + pdf.numPages);
      const canvas = document.createElement("canvas"); output.append(canvas);
      await draw(canvas, n, 1.5, Math.min(1800000, Math.floor(30000000 / pdf.numPages)));
    }
    window.print();
    message("");
  } catch { message("Print preparation unavailable. Download the PDF to print locally."); }
  finally { output.replaceChildren(); printing = false; $("print").disabled = false; }
}
$("prev").onclick = () => show(page - 1);
$("next").onclick = () => show(page + 1);
$("page").onchange = () => show($("page").value);
$("in").onclick = () => { zoom = Math.min(4, zoom * 1.25); show(page); };
$("out").onclick = () => { zoom = Math.max(0.25, zoom / 1.25); show(page); };
$("fit").onclick = () => { zoom = 1; show(page); };
$("toggle").onclick = thumbnails;
$("find").oninput = find;
$("find").onkeydown = event => { if (event.key === "Enter") { event.preventDefault(); nextMatch(event.shiftKey ? -1 : 1); } };
$("find-prev").onclick = () => nextMatch(-1);
$("find-next").onclick = () => nextMatch(1);
$("print").onclick = printDocument;
document.addEventListener("keydown", event => {
  if (event.target.matches("input,button,a") || event.altKey || event.ctrlKey || event.metaKey) return;
  if (["ArrowRight","PageDown","ArrowLeft","PageUp","Home","End"].includes(event.key)) {
    event.preventDefault();
    show(event.key === "Home" ? 1 : event.key === "End" ? pdf?.numPages :
      page + (["ArrowRight","PageDown"].includes(event.key) ? 1 : -1));
  }
});
window.addEventListener("resize", () => show(page));
try {
  if (pdfjs.version !== "6.5.438") throw Error("Unexpected PDF.js version");
  const url = document.body.dataset.document;
  if (!/^\/api\/library\/pdf\/[a-z0-9-]{1,63}\/document\.pdf$/.test(url)) throw Error("Invalid document");
  const response = await fetch(url, {cache:"no-store", credentials:"omit", redirect:"error"});
  if (!response.ok) throw Error("Document unavailable");
  const data = new Uint8Array(await response.arrayBuffer());
  pdf = await pdfjs.getDocument({data, isEvalSupported:false, enableXfa:false,
    useWasm:false, cMapUrl:asset+"cmaps/", cMapPacked:true,
    standardFontDataUrl:asset+"standard_fonts/", iccUrl:asset+"iccs/",
    wasmUrl:asset+"wasm/", useWorkerFetch:true, maxImageSize:16000000}).promise;
  $("total").textContent = "of " + pdf.numPages;
  $("page").max = pdf.numPages;
  await show(1);
} catch { message("Offline PDF viewer unavailable. Use Download PDF or contact the administrator."); }
