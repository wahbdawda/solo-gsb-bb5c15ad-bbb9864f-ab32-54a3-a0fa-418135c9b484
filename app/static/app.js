/* DICOM anonymisation workbench - front-end logic (vanilla JS, no CDN). */

const state = {
  selectedFiles: [],
  jobId: null,
  profile: "recommended",
  profileDefs: {},
  preview: null,
  page: 0,
  pageSize: 50,
  filteredChanges: [],
};
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  setTimeout(() => el.classList.add("hidden"), 3500);
  el.classList.remove("hidden");
}

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(2) + " MB";
}

function setStep(n) {
  $$(".step").forEach((el) => {
    const s = Number(el.dataset.step);
    el.classList.toggle("active", s === n);
    el.classList.toggle("done", s < n);
  });
}

function showCard(which) {
  ["uploadCard", "configCard", "previewCard", "resultCard"].forEach((id) => {
    $("#" + id).classList.toggle("hidden", id !== which);
  });
  const map = { uploadCard: 1, configCard: 2, previewCard: 3, resultCard: 4 };
  setStep(map[which]);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ---------------------------------------------------------------------------
// File selection & upload
// ---------------------------------------------------------------------------

function refreshSelectedFiles() {
  const list = $("#fileList");
  const has = state.selectedFiles.length > 0;
  $("#btnUpload").disabled = !has;
  $("#btnResetFiles").classList.toggle("hidden", !has);
  list.classList.toggle("hidden", !has);
  if (!has) { list.innerHTML = ""; return; }
  list.innerHTML = state.selectedFiles
    .map((f) => `<div class="file-row">
        <span class="name">${escapeHtml(f.relativePath || f.name)}</span>
        <span class="size">${fmtSize(f.size)}</span>
      </div>`)
    .join("");
}

function addFiles(fileList) {
  for (const f of fileList) state.selectedFiles.push(f);
  refreshSelectedFiles();
}

$("#fileInput").addEventListener("change", (e) => addFiles(e.target.files));
$("#dirInput").addEventListener("change", (e) => addFiles(e.target.files));
$("#btnResetFiles").addEventListener("click", () => {
  state.selectedFiles = [];
  $("#fileInput").value = "";
  $("#dirInput").value = "";
  refreshSelectedFiles();
});

const dz = $("#dropzone");
["dragenter", "dragover"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragover"); }));
["dragleave", "drop"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("dragover"); }));
dz.addEventListener("drop", (e) => {
  if (e.dataTransfer?.files) addFiles(e.dataTransfer.files);
});
// click anywhere in dropzone triggers file picker (buttons still work)
dz.addEventListener("click", (e) => {
  if (e.target.closest(".btn")) return;
  $("#fileInput").click();
});

$("#btnUpload").addEventListener("click", async () => {
  if (!state.selectedFiles.length) return;
  const fd = new FormData();
  state.selectedFiles.forEach((f) => {
    // webkitRelativePath is set for directory selection
    const path = f.webkitRelativePath || f.relativePath || f.name;
    fd.append("files", f, path);
  });

  $("#uploadProgress").classList.remove("hidden");
  $("#uploadText").textContent = `上传中… 0/${state.selectedFiles.length}`;
  $("#uploadBar").style.width = "0%";
  $("#btnUpload").disabled = true;

  try {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        $("#uploadBar").style.width = (ev.loaded / ev.total * 100) + "%";
        $("#uploadText").textContent =
          `上传中… ${fmtSize(ev.loaded)} / ${fmtSize(ev.total)}`;
      }
    };
    const resp = await new Promise((resolve, reject) => {
      xhr.onload = () => xhr.status >= 400
        ? reject(new Error((safeJson(xhr.responseText)?.detail) || `HTTP ${xhr.status}`))
        : resolve(safeJson(xhr.responseText));
      xhr.onerror = () => reject(new Error("网络错误，上传失败。"));
      xhr.send(fd);
    });

    state.jobId = resp.job_id;
    $("#uploadText").textContent = `已上传 ${resp.file_count} 个文件。`;
    $("#uploadBar").style.width = "100%";
    toast("上传完成，正在加载配置…");
    await loadProfiles(resp);
    showCard("configCard");
  } catch (err) {
    toast(err.message, true);
    $("#uploadProgress").classList.add("hidden");
    $("#btnUpload").disabled = false;
  }
});

$("#btnBack1").addEventListener("click", () => showCard("uploadCard"));

function safeJson(text) {
  try { return JSON.parse(text); } catch { return null; }
}

// ---------------------------------------------------------------------------
// Profiles & options
// ---------------------------------------------------------------------------

const OPTION_META = {
  keep_dates: ["日期平移保留", "不清空检查日期，而按患者向过去平移固定天数（检查间隔保持一致）。"],
  keep_chars: ["保留人口学特征", "保留性别、年龄、族裔等 Patient Characteristics（不含姓名/ID/生日精确值以外的身份信息）。"],
  keep_staff: ["保留医护姓名", "保留申请医师、操作技师、报告医师等人员姓名（机构外发时通常关闭）。"],
  keep_site: ["保留机构信息", "保留机构名称与地址。"],
  keep_device: ["保留设备标识", "保留厂商、型号、序列号、站点/AE Title 等设备信息。"],
  keep_protocol: ["保留协议名", "保留 Protocol Name。"],
  keep_descriptions: ["保留检查描述", "保留 Study/Series Description 等自由文本（可能含临床信息）。"],
  strip_overlays: ["剥离覆盖层/图形注释", "移除 60xx 覆盖层、50xx 曲线和图形注释序列（可能含烧录文字）。"],
};

async function loadProfiles(uploadResp) {
  const data = await fetch("/api/profiles").then((r) => r.json());
  state.profileDefs = data.profiles;
  state.profile = data.default;
  renderProfiles();
  renderOptions();
}

function renderProfiles() {
  $("#profiles").innerHTML = Object.entries(state.profileDefs)
    .map(([key, def]) => `<div class="profile ${key === state.profile ? "selected" : ""}"
        data-key="${key}">
      <h4>${escapeHtml(def.label)}</h4>
      <p>${escapeHtml(def.description)}</p>
    </div>`).join("");
  $$(".profile").forEach((el) =>
    el.addEventListener("click", () => {
      state.profile = el.dataset.key;
      renderProfiles();
      renderOptions();
    }));
}

function renderOptions() {
  const opts = state.profileDefs[state.profile].options;
  $("#options").innerHTML = Object.entries(OPTION_META)
    .map(([key, [title, desc]]) => `<label class="option">
      <input type="checkbox" data-key="${key}" ${opts[key] ? "checked" : ""}>
      <span><b>${escapeHtml(title)}</b><small>${escapeHtml(desc)}</small></span>
    </label>`).join("");
  $$("#options input").forEach((input) =>
    input.addEventListener("change", () => {
      // customised options remain on the selected profile; backend merges
    }));
}

function currentOptions() {
  const out = {};
  $$("#options input").forEach((input) => {
    out[input.dataset.key] = input.checked;
  });
  return out;
}

$("#btnPreview").addEventListener("click", async () => {
  const body = {
    profile: state.profile,
    ...currentOptions(),
    salt: $("#saltInput").value.trim() || null,
  };
  const btn = $("#btnPreview");
  btn.disabled = true; btn.textContent = "正在预览…";
  try {
    const resp = await fetch(`/api/jobs/${state.jobId}/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || "预览失败");
    state.preview = await resp.json();
    renderPreview(state.preview);
    showCard("previewCard");
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "生成变更预览";
  }
});

// ---------------------------------------------------------------------------
// Preview rendering
// ---------------------------------------------------------------------------

const ACTION_LABELS = {
  removed: "删除",
  blanked: "置空",
  pseudonymised: "伪名替换",
  uid_remapped: "UID 重写",
  shifted: "日期平移",
  kept: "保留",
  added: "新增声明",
};

function renderSummary(target, counts, patients) {
  const c = counts || {};
  target.innerHTML = [
    ["ok", c.files ?? 0, "输出影像"],
    ["danger", c.private_removed ?? 0, "私有标签剥离"],
    ["info", c.uid_remapped ?? 0, "UID 重写"],
    ["", c.pseudonymised ?? 0, "伪名/ID 替换"],
    ["", c.removed ?? 0, "标签删除"],
    ["warn", c.blanked ?? 0, "标签置空"],
    ["", c.shifted ?? 0, "日期平移"],
    ["warn", c.skipped ?? 0, "跳过文件"],
  ].map(([cls, num, label]) =>
    `<div class="tile ${cls}"><div class="num">${num}</div>
       <div class="label">${label}</div></div>`).join("") +
    `<div class="tile"><div class="num">${(patients || []).length}</div>
       <div class="label">识别患者</div></div>`;
}

function renderWarnings(box, warnings) {
  if (!warnings || !warnings.length) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  box.innerHTML = `<h4>⚠ ${warnings.length} 条警告（请人工核查）</h4>
    <ul>${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`;
}

const GATE_TYPE_LABELS = {
  patient_name: "患者姓名",
  patient_id: "患者 ID",
  original_uid: "原始 UID",
};

function renderGate(box, gate) {
  if (!gate) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  const ids = gate.identifiers || {};
  const byType = gate.hits_by_type || {};
  const head = gate.passed
    ? `<h4 class="gate-title">🛡 残留标识门禁：通过</h4>`
    : `<h4 class="gate-title">⛔ 残留标识门禁：未通过（${gate.hit_count} 处命中，ZIP 已阻止）</h4>`;
  const meta = `<div class="gate-meta">
      扫描 ${gate.scanned_files ?? 0} 个文件（DICOM ${gate.scanned_dicom_files ?? 0} ·
      报告 ${gate.scanned_report_files ?? 0}）、${gate.values_checked ?? 0} 个文本值；
      比对源标识：患者姓名 ${ids.patient_names ?? 0} ·
      患者 ID ${ids.patient_ids ?? 0} · 原始 UID ${ids.original_uids ?? 0}；
      命中：姓名 ${byType.patient_name ?? 0} · ID ${byType.patient_id ?? 0}
      · UID ${byType.original_uid ?? 0}
    </div>`;
  let hits = "";
  if (gate.hits && gate.hits.length) {
    hits = `<div class="table-wrap gate-hits">
      <table>
        <thead><tr><th>输出文件</th><th>位置（标签 / JSON 路径）</th><th>VR</th>
          <th>命中类型</th><th>命中的源标识</th></tr></thead>
        <tbody>${gate.hits.map((h) => `<tr>
          <td class="code-cell">${escapeHtml(h.file)}</td>
          <td class="code-cell">${escapeHtml(h.location)}</td>
          <td>${escapeHtml(h.vr || "-")}</td>
          <td><span class="badge b-gate-${escapeHtml(h.hit_type)}">${
            escapeHtml(h.hit_type_label || GATE_TYPE_LABELS[h.hit_type] || h.hit_type)}</span></td>
          <td class="code-cell oldval">${escapeHtml(h.matched)}</td>
        </tr>`).join("")}</tbody>
      </table>${gate.hits_truncated
        ? `<div class="gate-trunc">仅显示前 ${gate.hits.length} 条，完整命中数为 ${gate.hit_count}。</div>`
        : ""}
    </div>`;
  }
  box.className = "gate " + (gate.passed ? "gate-ok" : "gate-fail");
  box.innerHTML = head + meta + hits;
}

function renderPreview(data) {
  $("#profileTag")?.remove();
  renderSummary($("#summary"), data.counts, data.patients);
  renderGate($("#previewGate"), data.gate);
  renderWarnings($("#warningBox"), data.warnings);

  // Gate failures at preview time cannot become a downloadable ZIP: keep the
  // action visible (the hit table explains why) but block the click.
  const processBtn = $("#btnProcess");
  if (data.gate && data.gate.passed === false) {
    processBtn.disabled = true;
    processBtn.textContent = `残留标识门禁未通过（${data.gate.hit_count} 处命中）`;
  } else {
    processBtn.disabled = false;
    processBtn.textContent = "执行匿名化并打包";
  }

  // changes
  const actionSelect = $("#changeAction");
  const actions = [...new Set(data.changes.map((c) => c.action))];
  actionSelect.innerHTML = `<option value="">全部动作</option>` +
    actions.map((a) => `<option value="${a}">${ACTION_LABELS[a] || a}</option>`)
      .join("");
  state.page = 0;
  applyChangeFilter();

  // files
  const filesByIdx = Object.fromEntries(data.files.map((f) => [f.index, f]));
  $("#filesBody").innerHTML = data.files.map((f) => {
    const status = f.error
      ? `<span class="badge b-error">跳过：${escapeHtml(f.error)}</span>`
      : `<span class="badge b-added">已匿名化</span>`;
    return `<tr>
      <td>${f.index + 1}</td>
      <td class="code-cell">${escapeHtml(f.source_path)}</td>
      <td>${escapeHtml(f.modality || "-")}</td>
      <td class="code-cell oldval">${escapeHtml(f.old_patient_name || "")}
        ${f.old_patient_id ? `<br><small>${escapeHtml(f.old_patient_id)}</small>` : ""}</td>
      <td class="code-cell newval">${escapeHtml(f.new_patient_name || "-")}
        ${f.new_patient_id ? `<br><small>${escapeHtml(f.new_patient_id)}</small>` : ""}</td>
      <td class="code-cell">${uidPair(f.old_study_uid, f.new_study_uid)}</td>
      <td class="code-cell">${uidPair(f.old_sop_uid, f.new_sop_uid)}</td>
      <td>${status}</td>
    </tr>`;
  }).join("");

  $("#patientsBody").innerHTML = data.patients.map((p) => `<tr>
    <td class="code-cell oldval">${escapeHtml(p.key)}</td>
    <td><code>${escapeHtml(p.pseudonym)}</code></td>
    <td><code>${escapeHtml(p.pseudonymous_id)}</code></td>
    <td>${p.day_offset ? `向过去 ${p.day_offset} 天` : "不适用"}</td>
  </tr>`).join("");
}

function uidPair(oldV, newV) {
  if (!oldV && !newV) return "-";
  if (oldV === newV && oldV) return `<code>${escapeHtml(oldV)}</code>`;
  return `${oldV ? `<span class="oldval">${escapeHtml(short(oldV))}</span>` : ""}
    <br>→ <span class="newval">${escapeHtml(short(newV || ""))}</span>`;
}

function short(uid) {
  return uid.length > 46 ? uid.slice(0, 22) + "…" + uid.slice(-20) : uid;
}

function applyChangeFilter() {
  const data = state.preview;
  if (!data) return;
  const q = $("#changeFilter").value.trim().toLowerCase();
  const action = $("#changeAction").value;
  const filesByIdx = Object.fromEntries(data.files.map((f) => [f.index, f]));

  state.filteredChanges = data.changes.filter((c) => {
    if (action && c.action !== action) return false;
    if (!q) return true;
    const fname = filesByIdx[c.file_index]?.source_path || "";
    return [c.tag, c.name, c.action, c.old, c.new, fname]
      .some((v) => (v || "").toString().toLowerCase().includes(q));
  });
  const pages = Math.max(1, Math.ceil(state.filteredChanges.length / state.pageSize));
  state.page = Math.min(state.page, pages - 1);
  renderChangesPage(filesByIdx);
}

function renderChangesPage(filesByIdx) {
  const data = state.preview;
  filesByIdx = filesByIdx || Object.fromEntries(data.files.map((f) => [f.index, f]));
  const start = state.page * state.pageSize;
  const slice = state.filteredChanges.slice(start, start + state.pageSize);
  $("#changesBody").innerHTML = slice.map((c) => {
    const fname = filesByIdx[c.file_index]?.source_path || `#${c.file_index + 1}`;
    return `<tr>
      <td class="code-cell" title="${escapeHtml(fname)}">#${c.file_index + 1}
        <br><small>${escapeHtml(basename(fname))}</small></td>
      <td><code>${escapeHtml(c.tag)}</code></td>
      <td>${escapeHtml(c.name || "")}</td>
      <td>${escapeHtml(c.vr || "")}</td>
      <td><span class="badge b-${c.action}">${ACTION_LABELS[c.action] || c.action}</span></td>
      <td class="code-cell oldval">${escapeHtml(c.old ?? "")}</td>
      <td class="code-cell newval">${escapeHtml(c.new ?? "")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:20px">
    没有匹配的变更记录</td></tr>`;

  const pages = Math.max(1, Math.ceil(state.filteredChanges.length / state.pageSize));
  $("#pageInfo").textContent =
    `第 ${state.page + 1} / ${pages} 页 · 共 ${state.filteredChanges.length} 条变更`;
  $("#changeCount").textContent = `${data.changes.length} 条总变更`;
  $("#prevPage").disabled = state.page === 0;
  $("#nextPage").disabled = state.page >= pages - 1;
}

function basename(p) { const i = p.lastIndexOf("/"); return i >= 0 ? p.slice(i + 1) : p; }

$("#changeFilter").addEventListener("input", () => { state.page = 0; applyChangeFilter(); });
$("#changeAction").addEventListener("change", () => { state.page = 0; applyChangeFilter(); });
$("#prevPage").addEventListener("click", () => { state.page -= 1; renderChangesPage(); });
$("#nextPage").addEventListener("click", () => { state.page += 1; renderChangesPage(); });

$$(".tab").forEach((tab) => tab.addEventListener("click", () => {
  $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  $$(".tabpane").forEach((p) =>
    p.classList.toggle("hidden", p.id !== "tab-" + tab.dataset.tab));
}));

$("#btnBack2").addEventListener("click", () => showCard("configCard"));

// ---------------------------------------------------------------------------
// Process & download
// ---------------------------------------------------------------------------

$("#btnProcess").addEventListener("click", async () => {
  const body = {
    profile: state.profile,
    ...currentOptions(),
    salt: $("#saltInput").value.trim() || null,
  };
  const btn = $("#btnProcess");
  btn.disabled = true; btn.textContent = "正在执行匿名化（可能需要一些时间）…";
  try {
    const resp = await fetch(`/api/jobs/${state.jobId}/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) {
      // 409: residual identifier gate blocked ZIP generation -- show the
      // hit table on the preview page instead of a plain toast.
      if (resp.status === 409 && data.detail?.residue_identifier_gate) {
        const gate = data.detail.residue_identifier_gate;
        if (state.preview) state.preview.gate = gate;
        renderGate($("#previewGate"), gate);
        btn.textContent = `残留标识门禁未通过（${gate.hit_count} 处命中）`;
        toast("残留标识门禁阻止了 ZIP 生成，请查看命中清单并调整选项。", true);
        return;
      }
      throw new Error(
        (typeof data.detail === "string" ? data.detail : data.detail?.message)
        || "处理失败");
    }

    if (resp.status === 202) {
      await pollUntilDone();
    } else {
      finishJob(data);
    }
  } catch (err) {
    toast(err.message, true);
    btn.disabled = false; btn.textContent = "执行匿名化并打包";
  }
});

async function pollUntilDone() {
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    const s = await fetch(`/api/jobs/${state.jobId}/status`).then((r) => r.json());
    if (s.status === "done") { finishJob(s); return; }
    if (s.status === "blocked") {
      if (s.gate && state.preview) {
        state.preview.gate = s.gate;
        renderGate($("#previewGate"), s.gate);
        showCard("previewCard");
      }
      throw new Error(s.error || "残留标识门禁未通过。");
    }
    if (s.status === "error") throw new Error(s.error || "处理失败");
  }
  throw new Error("处理超时，请稍后重试。");
}

function finishJob(data) {
  $("#btnDownload").href = data.download_url;
  $("#btnDownload").setAttribute("download", data.zip_filename || "anonymized.zip");
  renderSummary($("#resultSummary"), data.counts, []);
  renderGate($("#resultGate"), data.gate);
  renderWarnings($("#resultWarnings"), data.warnings);
  showCard("resultCard");
  toast("匿名化完成，ZIP 已就绪。");
}

$("#btnStartOver").addEventListener("click", () => {
  state.jobId = null;
  state.preview = null;
  state.selectedFiles = [];
  $("#fileInput").value = "";
  $("#dirInput").value = "";
  $("#saltInput").value = "";
  $("#uploadProgress").classList.add("hidden");
  refreshSelectedFiles();
  showCard("uploadCard");
});

// ---------------------------------------------------------------------------

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

showCard("uploadCard");
