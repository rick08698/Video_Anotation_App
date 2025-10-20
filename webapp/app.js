// Simple 5-min window annotation tool (no deps)

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  videoId: "",
  windowSeconds: 5 * 60,
  windows: [], // {startSec, endSec, entries:['p'|'m'|'s'], notes:'', history:[]}
  currentIndex: 0,
  absOrigin: null, // number | null (epoch seconds). If set, windows are wall-clock based.
  coverage: null, // [{startAbs, endAbs}] from selected files when wall-clock mode
};

// ---------- UI helpers ----------
function setStatus(message, level = 'info') {
  const box = document.getElementById('videoStatus');
  if (!box) return;
  box.className = `status ${level}`;
  box.textContent = message;
}

function mediaErrorToText(err) {
  if (!err) return '未知のエラー';
  const map = {
    1: '再生が中止されました (ABORTED)',
    2: 'ネットワークエラー (NETWORK)',
    3: 'デコードエラー/コーデック非対応 (DECODE)',
    4: 'ソースがサポートされていません (SRC_NOT_SUPPORTED)'
  };
  return map[err.code] || `エラーコード: ${err.code}`;
}

// ---------- Time helpers ----------
function pad(n) { return n.toString().padStart(2, "0"); }
function secToHMS(sec) {
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h > 0) return `${pad(h)}:${pad(m)}:${pad(r)}`;
  return `${pad(m)}:${pad(r)}`;
}

function formatAbsDateTime(absSec) {
  // Returns YYYY-MM-DD HH:MM:SS
  const d = new Date(absSec * 1000);
  const y = d.getFullYear();
  const mo = pad(d.getMonth() + 1);
  const da = pad(d.getDate());
  const hh = pad(d.getHours());
  const mm = pad(d.getMinutes());
  const ss = pad(d.getSeconds());
  return `${y}-${mo}-${da} ${hh}:${mm}:${ss}`;
}

function absToHMS(absSec) {
  // HH:MM:SS from absolute seconds
  const d = new Date(absSec * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function formatWindowLabel(startRel, endRel) {
  if (state.absOrigin != null) {
    const a = state.absOrigin + startRel;
    const b = state.absOrigin + endRel;
    // Show full datetime for clarity
    return `${formatAbsDateTime(a)} – ${formatAbsDateTime(b)}`;
  }
  return `${secToHMS(startRel)} – ${secToHMS(endRel)}`;
}

function parseHMS(text) {
  if (!text) return 0;
  const parts = text.split(":").map((x) => parseInt(x, 10));
  if (parts.some((v) => Number.isNaN(v))) return 0;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 1) return parts[0];
  return 0;
}

// ---------- Rendering ----------
function renderList() {
  const list = $("#windowList");
  list.innerHTML = "";
  state.windows.forEach((w, i) => {
    const pending = w.entries.filter((e) => e === "p").length;
    const moving = w.entries.filter((e) => e === "m").length;
    const staying = w.entries.filter((e) => e === "s").length;
    const total = pending + moving + staying;
    const el = document.createElement("div");
    el.className = "window-item" + (i === state.currentIndex ? " active" : "");
    el.innerHTML = `
      <div class="range">${state.absOrigin != null ? `${formatAbsDateTime(state.absOrigin + w.startSec)} - ${formatAbsDateTime(state.absOrigin + w.endSec)}` : `${secToHMS(w.startSec)} - ${secToHMS(w.endSec)}`}</div>
      <div class="counts">P:${pending} M:${moving} S:${staying} All:${total}</div>
    `;
    el.addEventListener("click", () => {
      state.currentIndex = i;
      renderActive();
    });
    list.appendChild(el);
  });
}

function renderActive() {
  if (!state.windows.length) {
    $("#windowTitle").textContent = "[未生成]";
    $("#windowIndex").textContent = "";
    $("#pendingCount").textContent = 0;
    $("#movingCount").textContent = 0;
    $("#stayingCount").textContent = 0;
    $("#totalUnique").textContent = 0;
    $("#notes").value = "";
    renderList();
    return;
  }

  const i = state.currentIndex = Math.max(0, Math.min(state.currentIndex, state.windows.length - 1));
  const w = state.windows[i];
  $("#windowTitle").textContent = formatWindowLabel(w.startSec, w.endSec);
  $("#windowIndex").textContent = `${i + 1} / ${state.windows.length}`;
  const pending = w.entries.filter((e) => e === "p").length;
  const moving = w.entries.filter((e) => e === "m").length;
  const staying = w.entries.filter((e) => e === "s").length;
  const total = pending + moving + staying;
  $("#pendingCount").textContent = pending;
  $("#movingCount").textContent = moving;
  $("#stayingCount").textContent = staying;
  $("#totalUnique").textContent = total;
  $("#notes").value = w.notes || "";

  renderList();
}

// ---------- Persistence ----------
function storageKey() { return `annot:${state.videoId}`; }
function saveToLocalStorage() {
  if (!state.videoId) return;
  const data = JSON.stringify({
    videoId: state.videoId,
    windowSeconds: state.windowSeconds,
    windows: state.windows,
    currentIndex: state.currentIndex,
    absOrigin: state.absOrigin,
  });
  localStorage.setItem(storageKey(), data);
}

function loadFromLocalStorage() {
  if (!state.videoId) return false;
  const raw = localStorage.getItem(storageKey());
  if (!raw) return false;
  try {
    const data = JSON.parse(raw);
    state.videoId = data.videoId || state.videoId;
    state.windowSeconds = data.windowSeconds || state.windowSeconds;
    state.windows = Array.isArray(data.windows) ? data.windows : [];
    state.currentIndex = data.currentIndex || 0;
    state.absOrigin = (typeof data.absOrigin === 'number') ? data.absOrigin : null;
    $("#videoId").value = state.videoId;
    $("#windowMinutes").value = Math.max(1, Math.round(state.windowSeconds / 60));
    renderActive();
    return true;
  } catch (_) {
    return false;
  }
}

// ---------- CSV ----------
function csvEscape(v) {
  const s = String(v ?? "");
  if (/[",\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
  return s;
}

function exportCSV() {
  const header = [
    "video_id","window_start","window_end","total_unique","moving_count","staying_count","notes"
  ];
  const rows = [header.join(",")];
  for (const w of state.windows) {
    const pending = w.entries.filter((e) => e === "p").length;
    if (pending > 0) {
      alert("未確定の人数が残っています。すべて移動/滞留に振り分けてください。");
      return;
    }
    const moving = w.entries.filter((e) => e === "m").length;
    const staying = w.entries.filter((e) => e === "s").length;
    const total = moving + staying;
    const row = [
      state.videoId,
      state.absOrigin != null ? absToHMS(state.absOrigin + w.startSec) : secToHMS(w.startSec),
      state.absOrigin != null ? absToHMS(state.absOrigin + w.endSec) : secToHMS(w.endSec),
      total,
      moving,
      staying,
      w.notes || "",
    ].map(csvEscape).join(",");
    rows.push(row);
  }
  const blob = new Blob([rows.join("\n") + "\n"], {type: "text/csv;charset=utf-8"});
  downloadBlob(blob, `${state.videoId || "annotations"}.csv`);
}

function exportDetailCSV() {
  const header = ["video_id","window_start","person_local_id","visible_sec","behavior","remarks"];
  const rows = [header.join(",")];
  for (const w of state.windows) {
    const pending = w.entries.filter((e) => e === "p").length;
    if (pending > 0) {
      alert("未確定の人数が残っています。すべて移動/滞留に振り分けてください。");
      return;
    }
    let idx = 1;
    w.entries.forEach((e) => {
      if (e === "m" || e === "s") {
        const row = [
          state.videoId,
          state.absOrigin != null ? absToHMS(state.absOrigin + w.startSec) : secToHMS(w.startSec),
          `p${String(idx).padStart(3, "0")}`,
          "",
          e === "m" ? "moving" : "staying",
          "",
        ].map(csvEscape).join(",");
        rows.push(row);
        idx += 1;
      }
    });
  }
  const blob = new Blob([rows.join("\n") + "\n"], {type: "text/csv;charset=utf-8"});
  downloadBlob(blob, `${state.videoId || "annotations"}_detail.csv`);
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- Init & Events ----------
function generateWindows() {
  const vid = $("#videoId").value.trim();
  const start = parseHMS($("#startTime").value.trim());
  const end = parseHMS($("#endTime").value.trim());
  const minutes = Math.max(1, parseInt($("#windowMinutes").value, 10) || 5);
  if (!vid) {
    alert("video_id を入力してください。");
    return;
  }
  if (!(end > start)) {
    alert("開始/終了時刻を正しく入力してください。");
    return;
  }
  state.videoId = vid;
  state.windowSeconds = minutes * 60;
  state.absOrigin = null; // manual mode: relative timeline
  state.windows = [];
  for (let t = start; t < end; t += state.windowSeconds) {
    const w = { startSec: t, endSec: Math.min(t + state.windowSeconds, end), entries: [], notes: "", history: [] };
    state.windows.push(w);
  }
  state.currentIndex = 0;
  renderActive();
  saveToLocalStorage();
}

function buildWindowsFromAbsRange(startAbs, endAbs, minutes) {
  const vid = $("#videoId").value.trim();
  if (!vid) {
    alert("video_id を入力してください。");
    return false;
  }
  const step = Math.max(1, minutes) * 60; // align to on-time grid of 'step' seconds (default 5min)
  state.videoId = vid;
  state.windowSeconds = step;
  state.absOrigin = startAbs;
  state.windows = [];
  // Align to exact on-time boundaries
  const alignDown = (ts) => Math.floor(ts / step) * step;
  const alignUp = (ts) => Math.ceil(ts / step) * step;
  const alignedStart = alignUp(startAbs);
  const alignedEnd = alignDown(endAbs);

  // Generate only fully covered on-time windows
  const cov = Array.isArray(state.coverage) ? state.coverage : [{ startAbs, endAbs }];
  const isCovered = (a, b) => cov.some((c) => c.startAbs <= a && b <= c.endAbs);

  for (let t = alignedStart; t + step <= alignedEnd; t += step) {
    const a = t;
    const b = t + step;
    if (!isCovered(a, b)) continue; // skip if not fully covered by any file
    const startRel = a - startAbs;
    const endRel = b - startAbs;
    state.windows.push({ startSec: startRel, endSec: endRel, entries: [], notes: "", history: [] });
  }
  if (state.windows.length === 0) {
    setStatus('オンタイム5分枠に完全一致する範囲が見つかりませんでした。前後のファイルを追加してカバーしてください。', 'warn');
  }
  state.currentIndex = 0;
  renderActive();
  saveToLocalStorage();
  return true;
}

function ensureWindowsOrGenerate() {
  if (state.windows.length) return true;
  // Try to auto-generate if all fields are present
  const vid = document.getElementById("videoId").value.trim();
  const start = parseHMS(document.getElementById("startTime").value.trim());
  const end = parseHMS(document.getElementById("endTime").value.trim());
  if (vid && end > start) {
    generateWindows();
    return true;
  } else {
    alert("まず動画を読み込み（または各値を入力）し、ウィンドウ生成してください。");
    return false;
  }
}

function pushEntry(kind) {
  if (!ensureWindowsOrGenerate()) {
    // Try to auto-generate if all fields are present
    return;
  }
  const w = state.windows[state.currentIndex];
  if (kind === "m" || kind === "s") w.entries.push(kind);
  renderActive();
  saveToLocalStorage();
}

// New: pending-first workflow
function addPerson() {
  if (!ensureWindowsOrGenerate()) return;
  const w = state.windows[state.currentIndex];
  w.entries.push('p');
  w.history.push({op:'add'});
  renderActive();
  saveToLocalStorage();
}

function removePerson() {
  if (!ensureWindowsOrGenerate()) return;
  const w = state.windows[state.currentIndex];
  // remove last 'p' if available
  for (let i = w.entries.length - 1; i >= 0; i--) {
    if (w.entries[i] === 'p') {
      w.entries.splice(i, 1);
      w.history.push({op:'remove'});
      renderActive();
      saveToLocalStorage();
      return;
    }
  }
  alert('未確定の人数がありません。');
}

function assignFromPending(toKind) {
  if (!ensureWindowsOrGenerate()) return;
  const w = state.windows[state.currentIndex];
  for (let i = w.entries.length - 1; i >= 0; i--) {
    if (w.entries[i] === 'p') {
      w.entries[i] = toKind; // 'm' or 's'
      w.history.push({op:'assign', index: i, from:'p', to: toKind});
      renderActive();
      saveToLocalStorage();
      return;
    }
  }
  alert('未確定の人数がありません。まず「人数 +1」でカウントしてください。');
}

function revertToPending(fromKind) {
  if (!ensureWindowsOrGenerate()) return;
  const w = state.windows[state.currentIndex];
  for (let i = w.entries.length - 1; i >= 0; i--) {
    if (w.entries[i] === fromKind) {
      w.entries[i] = 'p';
      w.history.push({op:'assign', index: i, from: fromKind, to: 'p'});
      renderActive();
      saveToLocalStorage();
      return;
    }
  }
  alert(`${fromKind === 'm' ? '移動' : '滞留'} に分類された人数がありません。`);
}

function subEntry(kind) {
  if (!state.windows.length) return;
  const w = state.windows[state.currentIndex];
  // remove last occurrence of kind
  for (let i = w.entries.length - 1; i >= 0; i--) {
    if (w.entries[i] === kind) { w.entries.splice(i, 1); break; }
  }
  renderActive();
  saveToLocalStorage();
}

function undo() {
  if (!state.windows.length) return;
  const w = state.windows[state.currentIndex];
  const last = w.history && w.history.pop();
  if (!last) { return; }
  if (last.op === 'add') {
    // remove last entry if it's 'p'
    for (let i = w.entries.length - 1; i >= 0; i--) {
      if (w.entries[i] === 'p') { w.entries.splice(i, 1); break; }
    }
  } else if (last.op === 'remove') {
    w.entries.push('p');
  } else if (last.op === 'assign') {
    w.entries[last.index] = last.from;
  }
  renderActive();
  saveToLocalStorage();
}

function clearCurrent() {
  if (!state.windows.length) return;
  if (!confirm("このウィンドウのカウントをクリアしますか？")) return;
  const w = state.windows[state.currentIndex];
  w.entries = [];
  w.notes = "";
  renderActive();
  saveToLocalStorage();
}

function nextWindow() {
  if (!state.windows.length) return;
  state.currentIndex = Math.min(state.windows.length - 1, state.currentIndex + 1);
  renderActive();
}

function prevWindow() {
  if (!state.windows.length) return;
  state.currentIndex = Math.max(0, state.currentIndex - 1);
  renderActive();
}

function bindEvents() {
  $("#generateBtn").addEventListener("click", generateWindows);
  // pending-first bindings
  $("#addPersonBtn").addEventListener("click", addPerson);
  $("#subPersonBtn").addEventListener("click", removePerson);
  $("#addMovingBtn").addEventListener("click", () => assignFromPending('m'));
  $("#subMovingBtn").addEventListener("click", () => revertToPending('m'));
  $("#addStayingBtn").addEventListener("click", () => assignFromPending('s'));
  $("#subStayingBtn").addEventListener("click", () => revertToPending('s'));
  $("#undoBtn").addEventListener("click", undo);
  $("#clearBtn").addEventListener("click", clearCurrent);
  $("#nextBtn").addEventListener("click", nextWindow);
  $("#prevBtn").addEventListener("click", prevWindow);

  $("#notes").addEventListener("input", (e) => {
    if (!state.windows.length) return;
    state.windows[state.currentIndex].notes = e.target.value;
    saveToLocalStorage();
  });

  $("#saveLocalBtn").addEventListener("click", saveToLocalStorage);
  $("#loadLocalBtn").addEventListener("click", () => {
    const ok = loadFromLocalStorage();
    if (!ok) alert("ローカル保存は見つかりませんでした。");
  });
  $("#saveServerBtn").addEventListener("click", async () => {
    if (!state.videoId) { alert("video_id を入力してください。"); return; }
    try {
      const payload = { videoId: state.videoId, windowSeconds: state.windowSeconds, windows: state.windows, currentIndex: state.currentIndex, absOrigin: state.absOrigin };
      const res = await fetch("/api/annotations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      if (!res.ok) throw new Error("save failed");
      alert("サーバに保存しました。");
    } catch (e) {
      alert("サーバ保存に失敗しました。server.py を起動していますか？");
    }
  });
  $("#loadServerBtn").addEventListener("click", async () => {
    if (!state.videoId) { alert("video_id を入力してください。"); return; }
    try {
      const res = await fetch(`/api/annotations?video_id=${encodeURIComponent(state.videoId)}`);
      if (!res.ok) throw new Error("load failed");
      const data = await res.json();
      if (!data || !Array.isArray(data.windows)) { alert("サーバにデータがありません。"); return; }
      state.videoId = data.videoId || state.videoId;
      state.windowSeconds = data.windowSeconds || state.windowSeconds;
      state.windows = data.windows;
      state.currentIndex = data.currentIndex || 0;
       state.absOrigin = (typeof data.absOrigin === 'number') ? data.absOrigin : null;
      $("#windowMinutes").value = Math.max(1, Math.round(state.windowSeconds / 60));
      renderActive();
      alert("サーバから読み込みました。");
    } catch (e) {
      alert("サーバ読込に失敗しました。server.py を起動していますか？");
    }
  });
  $("#exportCsvBtn").addEventListener("click", exportCSV);
  $("#exportDetailCsvBtn").addEventListener("click", exportDetailCSV);

  $("#importJsonInput").addEventListener("change", async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    try {
      const text = await f.text();
      const data = JSON.parse(text);
      if (!data || !Array.isArray(data.windows)) throw new Error("invalid");
      state.videoId = data.videoId || "";
      state.windowSeconds = data.windowSeconds || 300;
      state.windows = data.windows;
      state.currentIndex = data.currentIndex || 0;
      state.absOrigin = (typeof data.absOrigin === 'number') ? data.absOrigin : null;
      $("#videoId").value = state.videoId;
      $("#windowMinutes").value = Math.max(1, Math.round(state.windowSeconds / 60));
      renderActive();
      alert("読み込みました。");
    } catch (err) {
      alert("JSONを読み込めませんでした。");
    } finally {
      e.target.value = "";
    }
  });

  document.addEventListener("keydown", (e) => {
    if ((e.target instanceof HTMLInputElement) || (e.target instanceof HTMLTextAreaElement)) return;
    const k = e.key.toLowerCase();
    if (k === "a") addPerson();
    else if (k === "m") assignFromPending('m');
    else if (k === "s") assignFromPending('s');
    else if (k === "z") undo();
    else if (e.key === "ArrowRight" || k === "n") nextWindow();
    else if (e.key === "ArrowLeft") prevWindow();
  });
}

function initFromHash() {
  // Allow quick resume via #video_id
  const hash = location.hash.replace(/^#/, "");
  if (hash) {
    $("#videoId").value = hash;
    state.videoId = hash;
    loadFromLocalStorage();
  }
}

function main() {
  bindEvents();
  initFromHash();
  renderActive();
}

document.addEventListener("DOMContentLoaded", main);

// ---- Video file handling ----
document.addEventListener("DOMContentLoaded", () => {
  const fi = document.getElementById("videoFileInput");
  const fai = document.getElementById("videoFileAddInput");
  const addBtn = document.getElementById("addFilesBtn");
  const video = document.getElementById("videoEl");
  if (!fi || !video) return;

  fi.addEventListener("change", () => {
    const files = Array.from(fi.files || []);
    if (!files.length) return;

    const toHMS = (hhmmss) => `${hhmmss.slice(0,2)}:${hhmmss.slice(2,4)}:${hhmmss.slice(4,6)}`;

    // Multi-file mode
    if (files.length > 1) {
      const metas = [];
      for (const f of files) {
        const info = parseP(f.name);
        if (!info) {
          alert(`複数ファイルモードでは、ファイル名が PYYMMDD_HHMMSS_HHMMSS.* 形式である必要があります: ${f.name}`);
          return;
        }
        metas.push({ file: f, info });
      }
      metas.sort((a,b) => a.info.startAbs - b.info.startAbs);
      const minStart = metas[0].info.startAbs;
      const maxEnd = metas[metas.length - 1].info.endAbs;

      // Display first file immediately for preview
      const url0 = URL.createObjectURL(metas[0].file);
      video.src = url0;
      // Reflect start/end fields from filenames immediately
      $("#startTime").value = metas[0].info.startStr;
      $("#endTime").value = metas[metas.length-1].info.endStr;

      setStatus(`複数ファイルを読み込みました（${files.length}件）。\n${formatAbsDateTime(minStart)} – ${formatAbsDateTime(maxEnd)} の範囲でウィンドウを生成します。`, 'info');

      // Suggest a group video_id if空
      const vidInput = document.getElementById("videoId");
      if (!vidInput.value) {
        const base0 = metas[0].file.name.replace(/\.[^.]+$/, "");
        vidInput.value = `${base0}_set`;
      }
      // Confirm overwrite (after reflecting fields)
      if (state.windows.length && !confirm("既存のウィンドウを上書きして再生成しますか？")) return;
      const minutes = Math.max(1, parseInt($("#windowMinutes").value, 10) || 5);
      // coverage from all files to restrict generation to fully covered on-time slots
      state.coverage = metas.map(m => ({ startAbs: m.info.startAbs, endAbs: m.info.endAbs }));
      buildWindowsFromAbsRange(minStart, maxEnd, minutes);
      return;
    }

    // Single file mode
    const f = files[0];
    const url = URL.createObjectURL(f);
    video.src = url;
    let attemptedTranscode = false;
    setStatus(`動画を読み込みました: ${f.name}\nメタデータ取得を試行中…`, 'info');

    const base = f.name.replace(/\.[^.]+$/, "");
    const pm = parseP(f.name);
    document.getElementById("videoId").value = base;

    if (pm) {
      // Reflect filename-stated times regardless of regeneration
      $("#startTime").value = pm.startStr;
      $("#endTime").value = pm.endStr;
      // Inform user immediately so status doesn't stay on "メタデータ取得を試行中…"
      setStatus('ファイル名から開始/終了を読み取りました。', 'info');
      // Build windows by absolute wall-clock range from filename
      if (state.windows.length && !confirm("既存のウィンドウを上書きして再生成しますか？")) return;
      const minutes = Math.max(1, parseInt($("#windowMinutes").value, 10) || 5);
      state.coverage = [{ startAbs: pm.startAbs, endAbs: pm.endAbs }];
      buildWindowsFromAbsRange(pm.startAbs, pm.endAbs, minutes);
      // Warn if head/tail aligned 5-min slots are not fully covered by this single file
      const step = minutes * 60;
      const alignDown = (ts) => Math.floor(ts / step) * step;
      const alignUp = (ts) => Math.ceil(ts / step) * step;
      const prevStart = alignDown(pm.startAbs);
      const nextEnd = alignUp(pm.endAbs);
      const headMissing = pm.startAbs > prevStart;
      const tailMissing = pm.endAbs < nextEnd;
      if (headMissing || tailMissing) {
        let msg = 'オンタイム5分スロットのみ生成しました。';
        if (headMissing) msg += `\n先頭不足: ${formatAbsDateTime(prevStart)} – ${formatAbsDateTime(prevStart + step)} をカバーする前のファイルを追加してください。`;
        if (tailMissing) msg += `\n末尾不足: ${formatAbsDateTime(nextEnd - step)} – ${formatAbsDateTime(nextEnd)} をカバーする次のファイルを追加してください。`;
        setStatus(msg, 'warn');
      } else {
        setStatus('ファイル名から開始/終了を読み取り、オンタイム5分ウィンドウを生成しました。', 'info');
      }
    } else {
      document.getElementById("startTime").value = "00:00:00";
      // Wait for metadata to set end time from duration
      const onMeta = () => {
        const dur = Math.floor(video.duration || 0);
        document.getElementById("endTime").value = secToHMS(dur);
        video.removeEventListener("loadedmetadata", onMeta);
        if (dur > 0) {
          if (state.windows.length && !confirm("既存のウィンドウを上書きして再生成しますか？")) return;
          generateWindows();
          setStatus(`メタデータから動画長を取得: ${secToHMS(dur)}`, 'info');
        }
      };
      video.addEventListener("loadedmetadata", onMeta);

      // If metadata cannot be read (e.g., HEVC on unsupported browser), try server probe via ffprobe
      probeDurationViaServer(f).then((dur) => {
        if (!dur) return;
        const endInput = document.getElementById("endTime");
        if (!endInput.value || endInput.value === "00:00:00") {
          endInput.value = secToHMS(Math.floor(dur));
          if (state.windows.length && !confirm("既存のウィンドウを上書きして再生成しますか？")) return;
          generateWindows();
          setStatus(`サーバで動画長を取得: ${secToHMS(dur)}`, 'info');
        }
      }).catch(() => {/* ignore */});

      // If playback is not supported, attempt server-side transcode to H.264
      const onError = () => {
        if (attemptedTranscode) return;
        attemptedTranscode = true;
        const errText = mediaErrorToText(video.error);
        setStatus(`再生エラー: ${errText}\nH.264 への変換を試行します…`, 'warn');
        transcodeViaServer(f).then((res) => {
          if (!res || !res.url) return;
          video.src = res.url;
          // duration from server if provided
          if (res.duration && (!document.getElementById("endTime").value || document.getElementById("endTime").value === "00:00:00")) {
            document.getElementById("endTime").value = secToHMS(Math.floor(res.duration));
          }
          // On metadata of transcoded file, generate windows if none
          const onTMeta = () => {
            video.removeEventListener("loadedmetadata", onTMeta);
            if (!state.windows.length || confirm("トランスコード後の動画長でウィンドウを再生成しますか？")) {
              generateWindows();
            }
            setStatus('変換完了: 再生可能な形式に変換しました。', 'info');
          };
          video.addEventListener("loadedmetadata", onTMeta);
        }).catch(() => {/* ignore */});
      };
      video.addEventListener('error', onError, { once: true });
    }
  });

  if (addBtn && fai) {
    addBtn.addEventListener('click', () => fai.click());
    fai.addEventListener('change', () => {
      const files = Array.from(fai.files || []);
      if (!files.length) return;
      const newCov = [];
      for (const f of files) {
        const info = parseP(f.name);
        if (!info) { alert(`ファイル名が PYYMMDD_HHMMSS_HHMMSS.*（またはハイフン区切り）形式ではありません: ${f.name}`); return; }
        newCov.push({ startAbs: info.startAbs, endAbs: info.endAbs });
      }
      if (!Array.isArray(state.coverage)) state.coverage = [];
      state.coverage.push(...newCov);
      const minutes = Math.max(1, parseInt($("#windowMinutes").value, 10) || 5);
      recomputeFromCoverage(minutes);
      setStatus(`ファイルを追加しました（${files.length}件）。オンタイム枠を再生成しました。`, 'info');
      fai.value = '';
    });
  }
});

async function probeDurationViaServer(file) {
  try {
    const fd = new FormData();
    fd.append('file', file, file.name);
    const res = await fetch('/api/probe-duration', { method: 'POST', body: fd });
    if (!res.ok) {
      let msg = '';
      try { const j = await res.json(); msg = j && j.message ? j.message : ''; } catch (_) {}
      if (res.status === 501) {
        setStatus('ffprobe が見つかりません。HEVC の動画長取得には ffmpeg のインストールが必要です。', 'error');
      } else {
        setStatus(`ffprobe での動画長取得に失敗しました。\n${msg}`, 'error');
      }
      return null;
    }
    const data = await res.json();
    return data && data.duration ? data.duration : null;
  } catch (_) {
    return null;
  }
}

async function transcodeViaServer(file) {
  try {
    const fd = new FormData();
    fd.append('file', file, file.name);
    setStatus('変換を開始しました（サーバへアップロード中）…', 'info');
    const res = await fetch('/api/transcode-start', { method: 'POST', body: fd });
    if (!res.ok) {
      let msg = '';
      try { const j = await res.json(); msg = j && j.message ? j.message : ''; } catch (_) {}
      if (res.status === 501) {
        setStatus('ffmpeg が見つかりません。HEVC を再生用に変換するには ffmpeg のインストールが必要です。', 'error');
      } else {
        setStatus(`変換の開始に失敗しました。\n${msg}`, 'error');
      }
      return null;
    }
    const { job } = await res.json();
    if (!job) { setStatus('変換ジョブIDが取得できませんでした。', 'error'); return null; }
    return await pollTranscode(job);
  } catch (_) {
    return null;
  }
}

async function pollTranscode(jobId) {
  return new Promise((resolve) => {
    let timer = null;
    const tick = async () => {
      try {
        const r = await fetch(`/api/transcode-status?job=${encodeURIComponent(jobId)}`);
        if (!r.ok) { setStatus('変換ステータス取得に失敗しました。', 'error'); clearInterval(timer); resolve(null); return; }
        const j = await r.json();
        if (j.status === 'running') {
          const pct = Math.round((j.progress || 0) * 100);
          setStatus(`変換中… ${pct}%`, 'info');
        } else if (j.status === 'done') {
          setStatus('変換完了: 再生可能な形式に変換しました。', 'info');
          clearInterval(timer);
          resolve({ url: j.url, duration: j.duration });
          return;
        } else if (j.status === 'error') {
          setStatus(`変換エラー: ${j.message || ''}`, 'error');
          clearInterval(timer);
          resolve(null);
          return;
        }
      } catch (e) {
        setStatus('変換ステータス取得中にエラーが発生しました。', 'error');
        clearInterval(timer);
        resolve(null);
        return;
      }
    };
    timer = setInterval(tick, 1000);
    tick();
  });
}
