import * as pdfjs from "/api/pdfjs/build/pdf.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/api/pdfjs/build/pdf.worker.mjs";
const $ = id => document.getElementById(id);
const message = text => { $("message").textContent = text; };
const asset = "/api/pdfjs/";
let pdf, page = 1, zoom = 1, renderTask, generation = 0, printing = false;
let hits = [], hit = -1, searchGeneration = 0;
const maxPixels = 6000000;

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
    if (token === generation) message("");
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

async function find() {
  if (!pdf) return;
  const query = $("find").value.trim().toLocaleLowerCase();
  const token = ++searchGeneration;
  hits = []; hit = -1;
  if (!query) { $("matches").textContent = ""; return; }
  $("matches").textContent = "Searching…";
  try {
    for (let n = 1; n <= pdf.numPages; n++) {
      const source = await pdf.getPage(n);
      const content = await source.getTextContent();
      if (token !== searchGeneration) return;
      const text = content.items.map(item => item.str || "").join(" ").toLocaleLowerCase();
      if (text.includes(query)) hits.push(n);
    }
    if (token !== searchGeneration) return;
    if (hits.length) nextMatch();
    else $("matches").textContent = "No matching pages";
  } catch { if (token === searchGeneration) $("matches").textContent = "Search unavailable"; }
}
function nextMatch() {
  if (!hits.length) return;
  hit = (hit + 1) % hits.length;
  $("matches").textContent = (hit + 1) + " of " + hits.length + " matching pages";
  show(hits[hit]);
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
$("find").onchange = find;
$("find").onkeydown = event => { if (event.key === "Enter") find(); };
$("find-next").onclick = nextMatch;
$("print").onclick = printDocument;
document.addEventListener("keydown", event => {
  if (event.target.matches("input,button,a") || event.altKey || event.ctrlKey || event.metaKey) return;
  if (["ArrowRight","PageDown","ArrowLeft","PageUp","Home","End"].includes(event.key)) {
    event.preventDefault();
    show(event.key === "Home" ? 1 : event.key === "End" ? pdf?.numPages :
      page + (["ArrowRight","PageDown"].includes(event.key) ? 1 : -1));
  }
});
window.addEventListener("resize", () => { if (zoom === 1) show(page); });
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
