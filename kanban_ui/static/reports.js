/* Mission Board — reports: what agents post back on a card.
 *
 * Renders progress / result reports and their assets, with a special case per
 * asset type:
 *   mermaid  → diagram, rendered with mermaid.js (strict security level)
 *   plotly   → interactive chart, rendered with plotly.js
 *   snapshot → raster image (click to enlarge)
 *   svg      → generic SVG, shown through <img> so scripts in it never run
 *   file     → download link
 * Mermaid and Plotly load on first use: from /static/vendor/ when
 * scripts/vendor_libs.sh has fetched them, else from cdn.jsdelivr.net. Both are
 * themed from the page's Material 3 colour roles.
 *
 * Uses helpers from app.js: $, $$, api, toast, escapeHTML, localTime, state.
 */

// ----------------------------------------------------------- Library loading

const REPORT_LIBS = {
  mermaid: {
    global: "mermaid",
    urls: ["/static/vendor/mermaid.min.js",
           "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"],
  },
  plotly: {
    global: "Plotly",
    urls: ["/static/vendor/plotly.min.js",
           "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"],
  },
};
const _libPromises = {};

function loadLib(name) {
  if (_libPromises[name]) return _libPromises[name];
  const lib = REPORT_LIBS[name];
  _libPromises[name] = new Promise((resolve, reject) => {
    if (window[lib.global]) { resolve(window[lib.global]); return; }
    const tryUrl = (i) => {
      if (i >= lib.urls.length) {
        delete _libPromises[name];
        reject(new Error(`could not load ${name} (run scripts/vendor_libs.sh for offline use)`));
        return;
      }
      const s = document.createElement("script");
      s.src = lib.urls[i];
      s.async = true;
      s.onload = () => (window[lib.global] ? resolve(window[lib.global]) : tryUrl(i + 1));
      s.onerror = () => { s.remove(); tryUrl(i + 1); };
      document.head.appendChild(s);
    };
    tryUrl(0);
  });
  return _libPromises[name];
}

function mdToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--md-${name}`).trim();
}

// ----------------------------------------------------------- Mermaid

let _mermaidTheme = null;
let _mermaidSeq = 0;

async function renderMermaidIn(el, source) {
  el.classList.add("asset__render", "asset__render--mermaid");
  el.textContent = "Drawing diagram…";
  try {
    const mermaid = await loadLib("mermaid");
    const theme = document.documentElement.getAttribute("data-theme");
    if (_mermaidTheme !== theme) {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "base",
        fontFamily: getComputedStyle(document.body).fontFamily,
        themeVariables: {
          background: "transparent",
          primaryColor: mdToken("primary-container"),
          primaryTextColor: mdToken("on-primary-container"),
          primaryBorderColor: mdToken("primary"),
          secondaryColor: mdToken("secondary-container"),
          secondaryTextColor: mdToken("on-secondary-container"),
          tertiaryColor: mdToken("tertiary-container"),
          tertiaryTextColor: mdToken("on-tertiary-container"),
          lineColor: mdToken("outline"),
          textColor: mdToken("on-surface"),
          mainBkg: mdToken("primary-container"),
          nodeBorder: mdToken("primary"),
          clusterBkg: mdToken("surface-container-high"),
          clusterBorder: mdToken("outline-variant"),
          edgeLabelBackground: mdToken("surface-container-highest"),
          noteBkgColor: mdToken("tertiary-container"),
          noteTextColor: mdToken("on-tertiary-container"),
          fontSize: "14px",
        },
      });
      _mermaidTheme = theme;
    }
    const { svg } = await mermaid.render(`mmd-${++_mermaidSeq}`, source);
    el.innerHTML = svg;   // sanitised by mermaid at securityLevel "strict"
  } catch (e) {
    el.classList.add("asset__render--error");
    el.innerHTML = `<span class="asset__error">Diagram error: ${escapeHTML(String(e.message || e)).slice(0, 300)}</span>
      <pre class="asset__source">${escapeHTML(source)}</pre>`;
  }
}

// ----------------------------------------------------------- Plotly

function plotlyLayout(layout = {}, { compact = false } = {}) {
  const ink = mdToken("on-surface");
  const grid = mdToken("outline-variant");
  const base = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: getComputedStyle(document.body).fontFamily, color: ink, size: compact ? 10 : 13 },
    colorway: [mdToken("primary"), mdToken("tertiary"), "#2B6A46", "#8B5000", "#B3261E",
               "#006A6A", "#984061", "#5B5F71"],
    margin: compact ? { l: 30, r: 10, t: 10, b: 24 } : { l: 56, r: 24, t: 40, b: 48 },
    xaxis: { gridcolor: grid, zerolinecolor: grid, linecolor: grid },
    yaxis: { gridcolor: grid, zerolinecolor: grid, linecolor: grid },
    legend: { bgcolor: "rgba(0,0,0,0)" },
    hoverlabel: { font: { family: getComputedStyle(document.body).fontFamily } },
    autosize: true,
  };
  const merged = { ...base, ...layout };
  // keep our axis styling under the figure's own axis settings
  for (const k of Object.keys(layout)) {
    if (/^[xy]axis\d*$/.test(k) && typeof layout[k] === "object") {
      merged[k] = { ...base.xaxis, ...layout[k] };
    }
  }
  // figures sit in ~400 px cards: a legend under the plot leaves it room
  if (!layout.legend) {
    merged.legend = { ...base.legend, orientation: "h", x: 0, y: -0.22, yanchor: "top" };
    if (!compact) merged.margin = { ...base.margin, b: 88 };
  }
  if (compact) { merged.showlegend = false; delete merged.title; }
  return merged;
}

async function renderPlotlyIn(el, figure, { compact = false } = {}) {
  el.classList.add("asset__render", "asset__render--plotly");
  el.textContent = "Loading chart…";
  try {
    const Plotly = await loadLib("plotly");
    el.textContent = "";
    await Plotly.newPlot(el, figure.data || [], plotlyLayout(figure.layout || {}, { compact }), {
      responsive: true,
      displaylogo: false,
      staticPlot: compact,
      ...(compact ? {} : { modeBarButtonsToRemove: ["sendDataToCloud", "lasso2d", "select2d"] }),
      ...(figure.config || {}),
    });
  } catch (e) {
    el.classList.add("asset__render--error");
    el.innerHTML = `<span class="asset__error">Chart error: ${escapeHTML(String(e.message || e)).slice(0, 300)}</span>`;
  }
}

// ----------------------------------------------------------- Markdown (small, safe)

/* Escapes everything, then supports headings, lists (incl. - [ ] tasks),
 * quotes, paragraphs, `code`, **bold**, *italic*, http(s) links, fenced code.
 * ```mermaid and ```plotly fences become live figures. */
function mdToHtml(md) {
  const out = [];
  const lines = String(md || "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const inline = (t) => escapeHTML(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```\s*([\w-]*)\s*$/);
    if (fence) {
      closeList();
      const lang = fence[1].toLowerCase();
      const buf = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++;
      const code = buf.join("\n");
      if (lang === "mermaid") {
        out.push(`<div class="md-figure" data-md-mermaid="${escapeHTML(code)}"></div>`);
      } else if (lang === "plotly") {
        out.push(`<div class="md-figure md-figure--plotly" data-md-plotly="${escapeHTML(code)}"></div>`);
      } else {
        out.push(`<pre class="md-code"><code>${escapeHTML(code)}</code></pre>`);
      }
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { closeList(); out.push(`<h${h[1].length + 2} class="md-h">${inline(h[2])}</h${h[1].length + 2}>`); i++; continue; }
    const li = line.match(/^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/);
    if (li) {
      const want = li[2] ? "ol" : "ul";
      if (list !== want) { closeList(); out.push(`<${want} class="md-list">`); list = want; }
      let text = li[3];
      const task = text.match(/^\[([ xX])\]\s+(.*)$/);
      if (task) text = `${task[1] === " " ? "☐" : "☑"} ${task[2]}`;
      out.push(`<li>${inline(text)}</li>`);
      i++;
      continue;
    }
    if (/^\s*>/.test(line)) { closeList(); out.push(`<blockquote class="md-quote">${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`); i++; continue; }
    if (!line.trim()) { closeList(); i++; continue; }
    closeList();
    const para = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*([-*+]|\d+[.)])\s|\s*>)/.test(lines[i])) {
      para.push(lines[i++]);
    }
    out.push(`<p>${inline(para.join(" "))}</p>`);
  }
  closeList();
  return out.join("\n");
}

function hydrateMarkdownFigures(root) {
  root.querySelectorAll("[data-md-mermaid]").forEach((el) => {
    renderMermaidIn(el, el.getAttribute("data-md-mermaid"));
    el.removeAttribute("data-md-mermaid");
  });
  root.querySelectorAll("[data-md-plotly]").forEach((el) => {
    const raw = el.getAttribute("data-md-plotly");
    el.removeAttribute("data-md-plotly");
    try { renderPlotlyIn(el, JSON.parse(raw)); }
    catch { el.innerHTML = `<pre class="md-code"><code>${escapeHTML(raw)}</code></pre>`; }
  });
}

// ----------------------------------------------------------- Assets

const ASSET_TYPE_LABEL = {
  mermaid: "Diagram", plotly: "Chart", snapshot: "Snapshot", svg: "Drawing", file: "File",
};

function humanBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

async function renderAssetBody(el, a, { compact = false } = {}) {
  if (a.type === "snapshot" || a.type === "svg") {
    const img = document.createElement("img");
    img.src = a.url;
    img.alt = a.caption || ASSET_TYPE_LABEL[a.type];
    img.loading = "lazy";
    img.className = "asset__img";
    if (!compact) img.addEventListener("click", () => openLightbox(a.url, a.caption));
    el.appendChild(img);
  } else if (a.type === "mermaid") {
    const div = document.createElement("div");
    el.appendChild(div);
    const src = await fetch(a.url).then(r => r.text());
    await renderMermaidIn(div, src);
  } else if (a.type === "plotly") {
    const div = document.createElement("div");
    div.className = compact ? "asset__plot asset__plot--compact" : "asset__plot";
    el.appendChild(div);
    const fig = await fetch(a.url).then(r => r.json());
    await renderPlotlyIn(div, fig, { compact });
  } else {
    el.innerHTML = `<a class="asset__file" href="${a.url}?download=true">
      <span class="asset__file-icon">⬇</span>
      <span>${escapeHTML(a.filename || "attachment")}</span>
      <span class="asset__file-size">${humanBytes(a.size)}</span></a>`;
  }
}

// ----------------------------------------------------------- Card: reports tab

const REVIEW_LABEL = {
  submitted: "awaiting review", accepted: "accepted",
  changes_requested: "changes requested", superseded: "superseded",
};

async function loadTaskReports(t) {
  const box = $("#m-reports");
  box.innerHTML = "";
  $("#m-review").hidden = true;
  let reports = [];
  let messages = [];
  try {
    const [r, m] = await Promise.all([
      api("GET", `/api/tasks/${encodeURIComponent(t.id)}/reports`),
      api("GET", `/api/tasks/${encodeURIComponent(t.id)}/messages`),
    ]);
    reports = r.reports || [];
    messages = m.messages || [];
  } catch (e) {
    box.innerHTML = `<p class="reports__empty">Could not load reports: ${escapeHTML(e.message)}</p>`;
    return reports;
  }
  if (!state.modalTask || state.modalTask.id !== t.id) return reports;
  const badge = $("#m-reports-count");
  badge.hidden = reports.length === 0;
  badge.textContent = reports.length;
  if (!reports.length) {
    box.innerHTML = `<p class="reports__empty">No reports yet. Agents post them with
      <code>report_post</code>: progress updates as they go, and a final result that moves
      the card to Results.</p>`;
    return reports;
  }
  // review bar: the newest result still waiting for review
  const pending = reports.find(r => r.kind === "result" && r.review_status === "submitted");
  if (pending) {
    $("#m-review").hidden = false;
    $("#m-review").dataset.reportId = pending.id;
    $("#m-review-which").textContent = `“${pending.title}”`;
    $("#m-review-comment").value = "";
  }
  // threads: messages anchored to reports / figures, with replies under them
  const byParent = {};
  for (const m of messages) if (m.parent_id) (byParent[m.parent_id] ||= []).push(m);
  const anchored = messages.filter(m => !m.parent_id && (m.report_id || m.asset_id));
  // results first (newest result pinned), then progress, newest first
  const ordered = [...reports].sort((a, b) =>
    (b.kind === "result") - (a.kind === "result") || b.id - a.id);
  for (const r of ordered) box.appendChild(reportEl(t, r, anchored, byParent));
  hydrateMarkdownFigures(box);
  return reports;
}

function threadEl(roots, byParent) {
  const wrap = document.createElement("div");
  wrap.className = "thread";
  for (const m of roots) {
    const row = document.createElement("div");
    row.className = "thread__msg";
    row.innerHTML = `<span class="thread__who">${escapeHTML(m.author)}</span>
      <span class="thread__text">${escapeHTML(m.body)}</span>
      <span class="thread__state">${m.acked_at ? "handled" : m.delivered_at ? "read" : "sent"}</span>`;
    wrap.appendChild(row);
    for (const rep of byParent[m.id] || []) {
      const rr = document.createElement("div");
      rr.className = "thread__msg thread__msg--reply";
      rr.innerHTML = `<span class="thread__who">${escapeHTML(rep.author)}</span>
        <span class="thread__text">${escapeHTML(rep.body)}</span>`;
      wrap.appendChild(rr);
    }
  }
  return wrap;
}

function commentBox(t, anchor, onSent) {
  const box = document.createElement("div");
  box.className = "comment-box";
  box.innerHTML = `<textarea rows="2" placeholder="Comment for the agent…"></textarea>
    <div class="comment-box__actions">
      <button class="btn btn--ghost" type="button" data-cancel>Cancel</button>
      <button class="btn btn--primary" type="button" data-send>Send</button>
    </div>`;
  const ta = box.querySelector("textarea");
  box.querySelector("[data-cancel]").addEventListener("click", () => box.remove());
  const send = async () => {
    const text = ta.value.trim();
    if (!text) return;
    try {
      const m = await api("POST", `/api/tasks/${encodeURIComponent(t.id)}/messages`, { text, ...anchor });
      toast(m.agent_name ? `Comment sent to ${m.agent_name}` : "Comment queued for the card's agent");
      box.remove();
      onSent && onSent();
    } catch (e) { toast(`Error: ${e.message}`, "err"); }
  };
  box.querySelector("[data-send]").addEventListener("click", send);
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(); });
  setTimeout(() => ta.focus(), 0);
  return box;
}

function reportEl(t, r, anchored, byParent) {
  const el = document.createElement("article");
  el.className = `report report--${r.kind}` + (r.review_status ? ` report--${r.review_status}` : "");
  el.innerHTML = `
    <header class="report__head">
      <span class="chip ${r.kind === "result" ? "chip--report" : "chip--status"}">${r.kind === "result" ? "Result" : "Progress"}</span>
      ${r.review_status ? `<span class="chip chip--review-${escapeHTML(r.review_status)}">${escapeHTML(REVIEW_LABEL[r.review_status] || r.review_status)}</span>` : ""}
      <span class="report__meta">${escapeHTML(r.author)} · ${escapeHTML(localTime(r.created_at))}</span>
      <button class="btn btn--text report__comment" type="button">Comment</button>
    </header>
    <h3 class="report__title">${escapeHTML(r.title)}</h3>
    ${r.summary_md ? `<div class="report__summary md">${mdToHtml(r.summary_md)}</div>` : ""}
    ${r.review_comment ? `<div class="report__review-note">Review: ${escapeHTML(r.review_comment)}</div>` : ""}
    <div class="report__assets"></div>
  `;
  const refresh = () => loadTaskReports(state.modalTask || t);
  const reportThreads = anchored.filter(m => m.report_id === r.id && !m.asset_id);
  if (reportThreads.length) el.insertBefore(threadEl(reportThreads, byParent), el.querySelector(".report__assets"));
  el.querySelector(".report__comment").addEventListener("click", () => {
    if (el.querySelector(":scope > .comment-box")) return;
    el.insertBefore(commentBox(t, { report_id: r.id }, refresh), el.querySelector(".report__assets"));
  });
  const grid = el.querySelector(".report__assets");
  for (const a of r.assets) {
    const fig = document.createElement("figure");
    fig.className = `asset asset--${a.type}`;
    fig.innerHTML = `<div class="asset__body"></div>
      <figcaption class="asset__caption">
        <span class="asset__type">${ASSET_TYPE_LABEL[a.type] || a.type}</span>
        <span class="asset__text">${escapeHTML(a.caption || "")}</span>
        <button class="btn btn--text asset__comment" type="button">Comment</button>
      </figcaption>`;
    grid.appendChild(fig);
    renderAssetBody(fig.querySelector(".asset__body"), a);
    const threads = anchored.filter(m => m.asset_id === a.id);
    if (threads.length) fig.appendChild(threadEl(threads, byParent));
    fig.querySelector(".asset__comment").addEventListener("click", () => {
      if (fig.querySelector(".comment-box")) return;
      fig.appendChild(commentBox(t, { asset_id: a.id }, refresh));
    });
  }
  return el;
}

async function reviewPending(decision) {
  const t = state.modalTask;
  const rid = $("#m-review").dataset.reportId;
  if (!t || !rid) return;
  const comment = $("#m-review-comment").value.trim();
  if (decision === "changes" && !comment) {
    toast("Say what should change", "err");
    $("#m-review-comment").focus();
    return;
  }
  try {
    await api("POST", `/api/reports/${rid}/review`, { decision, comment });
    toast(decision === "accept" ? `${t.id} accepted → Done` : `Changes requested — ${t.id} back to In progress`);
    closeModal();
    await loadBoard();
    await loadProjects();
  } catch (e) { toast(`Error: ${e.message}`, "err"); }
}

// ----------------------------------------------------------- Board card thumbnail

function cardThumbHTML(t) {
  if (!t.thumb) return "";
  if (t.thumb.type === "snapshot" || t.thumb.type === "svg") {
    return `<img class="card__thumb" src="${t.thumb.url}" alt="${escapeHTML(t.thumb.caption || "latest figure")}" loading="lazy" />`;
  }
  return "";
}

function cardReportChip(t) {
  if (!t.reports) return "";
  const kind = t.thumb && (t.thumb.type === "plotly" ? "chart" : t.thumb.type === "mermaid" ? "diagram" : "");
  return `<span class="chip chip--reports" title="${t.reports} report(s)">▤ ${t.reports}${kind ? ` · ${kind}` : ""}</span>`;
}

// ----------------------------------------------------------- Gallery

async function openGallery() {
  $("#gallery-modal").hidden = false;
  await loadGallery();
}

function closeGallery() {
  $("#gallery-modal").hidden = true;
  $("#gallery").innerHTML = "";
}

async function loadGallery() {
  const box = $("#gallery");
  box.innerHTML = `<p class="reports__empty">Loading…</p>`;
  let items = [];
  try {
    const kind = $("#gallery-kind").value;
    const r = await api("GET", `/api/reports?project=${encodeURIComponent(state.projectId)}&kind=${kind}`);
    items = r.assets || [];
  } catch (e) {
    box.innerHTML = `<p class="reports__empty">${escapeHTML(e.message)}</p>`;
    return;
  }
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = `<p class="reports__empty">No figures yet. They appear here when agents post
      results with snapshots, SVG drawings, charts or diagrams.</p>`;
    return;
  }
  for (const a of items) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `gallery__item asset--${a.type}`;
    card.innerHTML = `<div class="gallery__body"></div>
      <div class="gallery__meta">
        <span class="gallery__caption">${escapeHTML(a.caption || a.report_title)}</span>
        <span class="gallery__task">${escapeHTML(a.task_id)} · ${escapeHTML(a.task_title)}</span>
      </div>`;
    box.appendChild(card);
    renderAssetBody(card.querySelector(".gallery__body"), a, { compact: true });
    card.addEventListener("click", () => {
      closeGallery();
      openTaskModal(a.task_id, { tab: "reports" });
    });
  }
}

// ----------------------------------------------------------- Lightbox

function openLightbox(url, caption) {
  $("#lightbox-img").src = url;
  $("#lightbox-caption").textContent = caption || "";
  $("#lightbox").hidden = false;
}

function closeLightbox() {
  $("#lightbox").hidden = true;
  $("#lightbox-img").removeAttribute("src");
}
