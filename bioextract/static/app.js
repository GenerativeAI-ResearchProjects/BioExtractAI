// Frontend for BioExtractAI: posts the config, streams SSE, renders the
// Domain Research Agent ↔ QA Agent interaction and the final answers.

const $ = (sel) => document.querySelector(sel);

// Default editable values users can reset to / clear at any time.
// Points at a concrete file (.md) rather than a directory, since some tools
// and downstream scripts expect a file path.
const EXAMPLE_PAPER_PATH = "Papers/20008779/20008779.checked.md";
const EXAMPLE_QUESTIONS = [
  "Does the paper report HIV sequences from patient samples?",
  "Does the paper report in vitro drug susceptibility data?",
  "What were the GenBank accession numbers for sequenced HIV isolates?",
  "Which HIV species were studied in the paper?",
  "Which HIV genes were reported to have been sequenced?",
  "From which countries were the sequenced samples obtained?",
  "From what years were the sequenced samples obtained?",
  "What method was used for sequencing? Sanger or NGS or Not Reported?",
  "Were samples cloned prior to sequencing?",
  "Did samples undergo single genome sequencing?",
  "Which types of samples were sequenced? (Select all that apply: Plasma, Whole Blood, PBMC, Proviral DNA, Serum)",
  "Were any sequences obtained from individuals with virological failure on a treatment regimen?",
  "Were any sequences obtained from the proviral DNA reservoir?",
  "Were the patients in the study in a clinical trial?",
  "How many individuals had samples obtained for HIV sequencing? Count all sequenced regardless successful or not.",
  "Does the paper report HIV sequences from individuals who had received ARV drugs?",
  "Which drug classes were received by individuals in the study?",
  "Which drugs were received by individuals in the study?",
  "Are sequences from the paper made publicly available and still accessible now? Follow and check the link if provided.",
].join("\n");

// Hook the small "reset to example" / "clear" links in each fieldset legend.
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".reset-link");
  if (!btn) return;
  e.preventDefault();
  const action = btn.dataset.reset;
  if (action === "paper") $("#paperPath").value = EXAMPLE_PAPER_PATH;
  else if (action === "questions") $("#questionsText").value = EXAMPLE_QUESTIONS;
  else if (action === "clearQuestions") $("#questionsText").value = "";
});

// Build the model dropdowns from /api/config's model_catalog. Includes a
// "Custom…" option that reveals a text input so users can still type any
// model name the catalog doesn't cover.
function buildModelSelect(selectEl, customInputEl, { includeSameAsQa = false } = {}) {
  const cfg = window.__cfg || {};
  const catalog = cfg.model_catalog || {};
  const defaults = cfg.default_models || {};
  selectEl.innerHTML = "";
  if (includeSameAsQa) {
    const opt = document.createElement("option");
    opt.value = ""; opt.textContent = "(same as QA model)";
    selectEl.append(opt);
  } else {
    const opt = document.createElement("option");
    opt.value = ""; opt.textContent = "(auto-detect)";
    selectEl.append(opt);
  }
  for (const provider of cfg.providers || []) {
    const models = catalog[provider] || [];
    if (!models.length) continue;
    const group = document.createElement("optgroup");
    group.label = `${provider}`;
    for (const m of models) {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m + (m === defaults[provider] ? "  · default" : "");
      group.append(opt);
    }
    selectEl.append(group);
  }
  const customOpt = document.createElement("option");
  customOpt.value = "__custom__";
  customOpt.textContent = "Custom…";
  selectEl.append(customOpt);

  selectEl.addEventListener("change", () => {
    if (selectEl.value === "__custom__") {
      customInputEl.classList.remove("hidden");
      customInputEl.focus();
    } else {
      customInputEl.classList.add("hidden");
      customInputEl.value = "";
    }
  });
}

function resolvedModelValue(selectEl, customInputEl) {
  if (selectEl.value === "__custom__") return (customInputEl.value || "").trim();
  return selectEl.value || "";
}
const el = (tag, props = {}, children = []) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c != null) n.append(c);
  return n;
};

let currentStream = null;
const state = { runs: {}, questions: [] };

// ──────────────────────────────────────────────────────────────────────
// Boot: probe /api/config, build dropdowns, and mark which providers have
// API keys set in the environment.
// ──────────────────────────────────────────────────────────────────────
(async function boot() {
  try {
    const r = await fetch("/api/config");
    const cfg = await r.json();
    window.__cfg = cfg;
    const present = cfg.available_providers || [];
    const missing = (cfg.providers || []).filter((p) => !present.includes(p));

    // Topbar summary.
    const parts = [];
    if (present.length) parts.push(`<span class="ok">env: ${present.join(", ")} ✓</span>`);
    if (missing.length) parts.push(`<span class="missing">no key: ${missing.join(", ")}</span>`);
    $("#envStatus").innerHTML = parts.join(" &middot; ") || "no providers detected";

    // Per-field env-status chips next to each API-key input.
    document.querySelectorAll(".env-status").forEach((el) => {
      const provider = el.dataset.env;
      if (present.includes(provider)) {
        el.textContent = "env ✓";
        el.classList.add("ok");
      } else {
        el.textContent = "not set";
        el.classList.add("missing");
      }
    });

    // Populate model dropdowns.
    buildModelSelect($("#modelSelect"), $("#modelCustom"));
    buildModelSelect($("#adjudicatorModelSelect"), $("#adjudicatorModelCustom"), { includeSameAsQa: true });
  } catch (e) {
    $("#envStatus").textContent = "could not read /api/config";
  }
})();

// ──────────────────────────────────────────────────────────────────────
// Form submission.
// ──────────────────────────────────────────────────────────────────────
$("#runForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (currentStream) { currentStream.close(); currentStream = null; }
  resetResults();
  setStatus("running", "running");
  $("#runBtn").disabled = true;
  $("#stopBtn").disabled = false;

  const fd = new FormData();
  const paperFile = $("#paperFile").files[0];
  if (paperFile) fd.append("paper_file", paperFile);
  if ($("#paperPath").value.trim()) fd.append("paper_path", $("#paperPath").value.trim());
  if ($("#questionsText").value.trim()) fd.append("questions_text", $("#questionsText").value);
  const qFile = $("#questionsFile").files[0];
  if (qFile) fd.append("questions_file", qFile);
  if ($("#provider").value) fd.append("provider", $("#provider").value);

  // Resolve model choice (dropdown + optional custom input).
  const modelVal = resolvedModelValue($("#modelSelect"), $("#modelCustom"));
  if (modelVal) fd.append("model", modelVal);
  const adjModelVal = resolvedModelValue($("#adjudicatorModelSelect"), $("#adjudicatorModelCustom"));
  if (adjModelVal) fd.append("adjudicator_model", adjModelVal);

  fd.append("runs", $("#runs").value);
  fd.append("use_adjudicator", $("#useAdjudicator").checked);
  fd.append("use_domain_agent", $("#useDomainAgent").checked);
  fd.append("max_searches", $("#maxSearches").value);
  fd.append("max_tokens", $("#maxTokens").value);

  // Per-provider API keys (sent only if filled in).
  const keys = {
    openai_api_key: $("#openaiApiKey").value,
    anthropic_api_key: $("#anthropicApiKey").value,
    deepseek_api_key: $("#deepseekApiKey").value,
  };
  for (const [k, v] of Object.entries(keys)) if (v) fd.append(k, v);

  try {
    const r = await fetch("/api/run", { method: "POST", body: fd });
    if (!r.ok) {
      const msg = await r.text();
      throw new Error(msg);
    }
    const { job_id } = await r.json();
    window.__currentJobId = job_id;
    openStream(job_id);
  } catch (err) {
    renderError(err.message || String(err));
    setStatus("error", "error");
    $("#runBtn").disabled = false;
    $("#stopBtn").disabled = true;
  }
});

$("#stopBtn").addEventListener("click", () => {
  if (currentStream) { currentStream.close(); currentStream = null; }
  setStatus("idle", "streaming stopped");
  $("#stopBtn").disabled = true;
  $("#runBtn").disabled = false;
});

// ──────────────────────────────────────────────────────────────────────
// SSE stream handling.
// ──────────────────────────────────────────────────────────────────────
function openStream(jobId) {
  const stream = new EventSource(`/api/jobs/${jobId}/stream`);
  currentStream = stream;

  stream.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      handleEvent(evt);
    } catch (err) {
      appendLog(`[parse error] ${e.data}`);
    }
  };
  stream.onerror = () => {
    stream.close();
    if (currentStream === stream) currentStream = null;
    $("#runBtn").disabled = false;
    $("#stopBtn").disabled = true;
  };
}

function handleEvent(evt) {
  appendLog(JSON.stringify(evt));
  switch (evt.type) {
    case "start":            return onStart(evt);
    case "paper_loaded":     return onPaperLoaded(evt);
    case "run_start":        return onRunStart(evt);
    case "domain_start":     return onDomainStart(evt);
    case "domain_done":      return onDomainDone(evt);
    case "qa_start":         return onQaStart(evt);
    case "qa_done":          return onQaDone(evt);
    case "adj_start":        return onAdjStart(evt);
    case "adj_progress":     return onAdjProgress(evt);
    case "adj_done":         return onAdjDone(evt);
    case "done":             return onDone(evt);
    case "error":            return renderError(evt.message);
  }
}

// ── Summary ──
function onStart(e) {
  $("#placeholder").classList.add("hidden");
  $("#runSummary").classList.remove("hidden");
  const adj = e.adjudicator
    ? `${e.adjudicator.provider}:${e.adjudicator.model}`
    : "<i>disabled</i>";
  $("#runSummary").innerHTML =
    `<b>QA:</b> ${e.provider}:${e.model} &middot; <b>runs:</b> ${e.runs} ` +
    `&middot; <b>domain agent:</b> ${e.use_domain_agent ? "on" : "off"} ` +
    `&middot; <b>adjudicator:</b> ${adj} &middot; <b>questions:</b> ${e.question_count}`;
}
function onPaperLoaded(e) {
  $("#runSummary").innerHTML += ` &middot; <b>paper:</b> ${escapeHtml(e.path)} (${e.char_count.toLocaleString()} chars)`;
}

// ── Per-run UI ──
function onRunStart(e) {
  $("#runsSection").classList.remove("hidden");
  const card = el("div", { class: "run-card", id: `run-${e.run}` });
  card.append(
    el("div", { class: "run-header" }, [
      el("div", { html: `<b>Run ${e.run}</b> / ${e.total_runs}` }),
      el("div", { class: "persona", id: `run-${e.run}-persona`, text: "…" }),
    ]),
    el("div", { class: "run-body", id: `run-${e.run}-body` })
  );
  $("#runsList").append(card);
  state.runs[e.run] = { node: card };
}

function onDomainStart(e) {
  setRunPersona(e.run, e.persona);
  const body = document.getElementById(`run-${e.run}-body`);
  body.append(
    el("div", { class: "stage domain", id: `run-${e.run}-domain` }, [
      el("div", { class: "stage-label", text: "Domain Research Agent · searching…" }),
      el("ul", { class: "queries", id: `run-${e.run}-queries` }),
      el("div", { class: "briefing-preview hidden", id: `run-${e.run}-briefing` }),
    ])
  );
}

function onDomainDone(e) {
  const label = document.querySelector(`#run-${e.run}-domain .stage-label`);
  const queriesList = document.getElementById(`run-${e.run}-queries`);
  const preview = document.getElementById(`run-${e.run}-briefing`);

  const searchedLabel = e.used_web_search
    ? `Domain Research Agent · ${e.search_queries.length} search${e.search_queries.length === 1 ? "" : "es"}`
    : `Domain Research Agent · no web search (${e.web_search_note || "provider does not expose search"})`;
  if (label) label.textContent = searchedLabel;

  queriesList.innerHTML = "";
  for (const q of e.search_queries || []) queriesList.append(el("li", { text: q }));

  preview.textContent = e.briefing_preview || "(empty briefing)";
  preview.classList.remove("hidden");
  if (e.briefing_text && e.briefing_text.length > (e.briefing_preview || "").length) {
    // Allow expanding to full briefing on click.
    preview.title = "click to expand / collapse full briefing";
    preview.style.cursor = "pointer";
    preview.addEventListener("click", () => {
      preview.textContent = preview.dataset.expanded === "1"
        ? (e.briefing_preview || "")
        : (e.briefing_text || "");
      preview.dataset.expanded = preview.dataset.expanded === "1" ? "0" : "1";
    });
  }
}

function onQaStart(e) {
  const body = document.getElementById(`run-${e.run}-body`);
  body.append(
    el("div", { class: "stage qa", id: `run-${e.run}-qa` }, [
      el("div", { class: "stage-label", text: "QA Agent · reading paper + briefing…" }),
    ])
  );
}

function onQaDone(e) {
  const stage = document.getElementById(`run-${e.run}-qa`);
  if (!stage) return;
  stage.querySelector(".stage-label").textContent =
    `QA Agent · answered ${e.answered}/${e.total}`;
  stage.append(
    el("div", { class: "qa-meta", text: `tokens: in=${e.input_tokens.toLocaleString()}, out=${e.output_tokens.toLocaleString()}` })
  );
  // If no adjudicator, surface the per-run answers directly.
  seedAnswersFromRun(e.run, e.records);
}

// ── Adjudicator UI ──
function onAdjStart(e) {
  $("#answersSection").classList.remove("hidden");
  ensureAnswerRows(e.total);
  setStatus("running", `adjudicating (0/${e.total})`);
}

function onAdjProgress(e) {
  setStatus("running", `adjudicating (${e.done}/${e.total})`);
  const row = document.getElementById(`ans-${e.qid}`);
  if (!row) return;
  row.classList.remove("pending");
  row.querySelector(".q-text").textContent = e.question;
  const confText = e.confidence == null ? "" : `conf ${Number(e.confidence).toFixed(2)}`;
  const confNode = row.querySelector(".conf");
  if (confText) { confNode.textContent = confText; confNode.classList.remove("hidden"); }
  row.querySelector(".final-answer").innerHTML = `<b>Final:</b> ${escapeHtml(e.final_answer || "")}`;
  if (e.rationale) {
    row.querySelector(".rationale").textContent = e.rationale;
    row.querySelector(".rationale").classList.remove("hidden");
  }
}

function onAdjDone() { /* final state applied by onDone */ }

// ── Finish ──
function onDone(e) {
  currentStream && currentStream.close();
  currentStream = null;
  setStatus("done", "done");
  $("#runBtn").disabled = false;
  $("#stopBtn").disabled = true;

  const result = e.result;
  if (!result) return;
  state.questions = result.questions;
  ensureAnswerRows(result.questions.length);

  // Wire up the download links for the now-finished job.
  const jobId = window.__currentJobId;
  if (jobId) {
    $("#downloadJson").href = `/api/jobs/${jobId}/download.json`;
    $("#downloadXlsx").href = `/api/jobs/${jobId}/download.xlsx`;
    $("#downloadsSection").classList.remove("hidden");
  }

  for (const q of result.questions) {
    const row = document.getElementById(`ans-${q.qid}`);
    if (!row) continue;
    row.classList.remove("pending");
    row.querySelector(".q-text").textContent = q.question;

    if (q.final) {
      row.querySelector(".final-answer").innerHTML = `<b>Final:</b> ${escapeHtml(q.final.answer)}`;
      if (q.final.rationale) {
        row.querySelector(".rationale").textContent = q.final.rationale;
        row.querySelector(".rationale").classList.remove("hidden");
      }
      if (q.final.confidence != null) {
        const c = row.querySelector(".conf");
        c.textContent = `conf ${Number(q.final.confidence).toFixed(2)}`;
        c.classList.remove("hidden");
      }
    }

    // Per-run answers below the final.
    const perRun = row.querySelector(".per-run");
    perRun.innerHTML = "";
    q.runs.forEach((r, i) => {
      perRun.append(el("span", { class: "run-tag", text: `Run ${i + 1}: ${r.answer || "—"}` }));
    });
  }
}

// ── Helpers ──
function setRunPersona(run, persona) {
  const node = document.getElementById(`run-${run}-persona`);
  if (node) node.textContent = persona;
}
function ensureAnswerRows(n) {
  const list = $("#answersList");
  if (list.children.length >= n) return;
  for (let i = list.children.length + 1; i <= n; i++) {
    list.append(
      el("div", { class: "answer-row pending", id: `ans-${i}` }, [
        el("div", { class: "q-head" }, [
          el("div", { html: `<span class="q-num">Q${i}</span><span class="q-text">…</span>`, class: "q-head-left" }),
          el("span", { class: "conf hidden" }),
        ]),
        el("div", { class: "final-answer", text: "" }),
        el("div", { class: "rationale hidden" }),
        el("div", { class: "per-run" }),
      ])
    );
  }
}
function seedAnswersFromRun(run, records) {
  if (!records) return;
  $("#answersSection").classList.remove("hidden");
  ensureAnswerRows(records.length);
  records.forEach((rec, i) => {
    const row = document.getElementById(`ans-${i + 1}`);
    if (!row) return;
    row.querySelector(".q-text").textContent = rec.question || `Question ${i + 1}`;
    const perRun = row.querySelector(".per-run");
    const existing = perRun.querySelector(`[data-run="${run}"]`);
    const tag = el("span", { class: "run-tag", "data-run": String(run), text: `Run ${run}: ${rec.answer || "—"}` });
    if (existing) existing.replaceWith(tag); else perRun.append(tag);
  });
}

function setStatus(kind, text) {
  const chip = $("#statusChip");
  chip.className = `chip ${kind}`;
  chip.textContent = text;
}

function resetResults() {
  state.runs = {}; state.questions = [];
  $("#runsList").innerHTML = ""; $("#answersList").innerHTML = "";
  $("#runsSection").classList.add("hidden");
  $("#answersSection").classList.add("hidden");
  $("#runSummary").classList.add("hidden");
  $("#placeholder").classList.add("hidden");
  $("#downloadsSection").classList.add("hidden");
  $("#logSection").classList.remove("hidden");
  $("#eventLog").textContent = "";
}

function renderError(msg) {
  setStatus("error", "error");
  $("#placeholder").classList.remove("hidden");
  $("#placeholder").innerHTML = `<p class="err-text" style="color:#c03a3a"><b>Error:</b> ${escapeHtml(msg)}</p>`;
}
function appendLog(line) {
  const pre = $("#eventLog");
  pre.textContent += line + "\n";
  pre.scrollTop = pre.scrollHeight;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}
