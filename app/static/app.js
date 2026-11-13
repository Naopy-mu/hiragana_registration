"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { config: null, participant: null, token: null, busy: false, worksheetBusy: false, csrf: null, participants: [], selected: new Set(), previewObjectUrl: null, worksheetRevision: null, worksheetDraft: null, revisionBusy: false };
  const storage = {
    get(store, key) { try { return window[store].getItem(key); } catch { return null; } },
    set(store, key, value) { try { window[store].setItem(key, value); } catch { /* Private browsing can disable storage. */ } }
  };
  const initialToken = location.hash.slice(1);
  if (initialToken) {
    state.token = initialToken;
    storage.set("sessionStorage", "handwriting_resume_token", initialToken);
    // Keep the bearer credential out of the visible address bar and referrers.
    history.replaceState(null, "", location.pathname + location.search);
  } else if (location.pathname !== "/researcher") {
    state.token = storage.get("sessionStorage", "handwriting_resume_token");
  }

  function message(id, text, isError = false) {
    const el = $(id); el.textContent = text; el.hidden = !text;
    if (id === "export-message" || id === "revision-message") el.classList.toggle("error", isError);
  }
  function announce(text) { $("app-status").textContent = text; }
  function errorText(data, status) {
    const detail = data && data.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail.message === "string") return detail.message;
    if (Array.isArray(detail)) return "入力内容を確認してください。";
    return data?.message || (status === 413 ? "ファイルが大きすぎます。サイズを小さくして再度選択してください。" : "処理に失敗しました。通信状態を確認して、もう一度お試しください。");
  }
  async function api(path, { method = "GET", body, participant = false, binary = false, csrf = false } = {}) {
    const headers = {};
    if (participant && state.token) headers.Authorization = `Bearer ${state.token}`;
    if (body && !(body instanceof FormData)) { headers["Content-Type"] = "application/json"; body = JSON.stringify(body); }
    if (csrf && state.csrf) headers["X-CSRF-Token"] = state.csrf;
    let response;
    try { response = await fetch(path, { method, headers, body, credentials: "same-origin", cache: "no-store" }); }
    catch { throw new Error("通信できませんでした。接続を確認して、もう一度お試しください。登録済みの内容は保持されます。"); }
    if (!response.ok) {
      let data; try { data = await response.json(); } catch { data = {}; }
      const error = new Error(errorText(data, response.status));
      error.status = response.status; error.detail = data.detail; throw error;
    }
    return binary ? response.blob() : response.status === 204 ? {} : response.json();
  }
  function showView(name) {
    for (const view of ["registration", "participant", "researcher"]) $(view + "-view").hidden = view !== name;
    $("boot").hidden = true;
  }
  function dateParts(value) {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return [String(value || "—"), ""];
    return [d.toLocaleDateString("ja-JP", { timeZone: "Asia/Tokyo" }), d.toLocaleTimeString("ja-JP", { timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit" })];
  }
  function make(tag, className, text) { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; }
  function registrationKey() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function setParticipantBusy(busy) {
    state.busy = busy;
    $("camera-button").disabled = busy; $("gallery-button").disabled = busy;
    $("upload-section").setAttribute("aria-busy", String(busy));
    $("submit-button").disabled = busy || !state.participant || state.participant.missing_pages.length > 0 || state.participant.status === "submitted";
  }
  function resumeUrl() { return `${location.origin}/#${state.token}`; }
  function renderParticipant(participant) {
    state.participant = participant;
    if (participant.config) state.config = { ...state.config, ...participant.config };
    $("profile-banner").hidden = state.config.production_ready === true;
    showView("participant");
    $("participant-id").textContent = participant.participant_id;
    $("resume-link").value = resumeUrl();
    const submitted = participant.status === "submitted";
    $("participant-heading-title").textContent = submitted ? "ご協力ありがとうございます" : "用紙を撮影して登録";
    $("participant-lead").textContent = submitted ? "提出済みのデータを確認できます。" : "用紙をダウンロードし、印刷・記入してから撮影してください。用紙はどの順番からでも登録できます。";
    const worksheet = participant.worksheet;
    $("download-worksheet").hidden = !worksheet?.available;
    $("download-worksheet").disabled = state.worksheetBusy;
    $("worksheet-unavailable").hidden = Boolean(worksheet?.available);
    $("worksheet-test-note").hidden = !worksheet?.is_test;
    $("worksheet-file-title").textContent = worksheet?.title || "";
    $("upload-section").hidden = submitted;
    $("submitted-panel").hidden = !submitted;
    $("submit-button").hidden = submitted;
    $("page-count").textContent = `${participant.pages.length} / ${state.config.pages.length}`;
    const required = state.config.pages.filter(p => p.required !== false);
    const requiredMissing = participant.missing_pages.length;
    const percent = required.length ? Math.round((required.length - requiredMissing) / required.length * 100) : 0;
    $("progress-fill").style.width = `${percent}%`;
    $("progress-fill").parentElement.setAttribute("aria-valuenow", String(percent));
    $("progress-description").textContent = submitted ? "すべての必須ページを提出しました。" : requiredMissing ? `あと${requiredMissing}ページの登録が必要です。` : "必須ページがすべてそろいました。";
    $("submit-hint").textContent = submitted ? "提出済みです。" : requiredMissing ? "すべての必須ページがそろうと提出できます。" : "プレビューを確認して、提出してください。";
    const list = $("page-list"); list.replaceChildren();
    for (const page of state.config.pages) {
      const saved = participant.pages.find(p => p.qr_code === page.qr_code);
      const row = make("li", "page-row" + (saved ? " complete" : ""));
      row.append(make("span", "page-state", saved ? "✓" : String(page.page_number)));
      const info = make("div", "page-info");
      info.append(make("strong", "", page.label || `ページ ${page.page_number}`), make("small", "", page.qr_code + (page.required === false ? " · 任意" : "")));
      row.append(info);
      if (saved) { const button = make("button", "", "確認"); button.type = "button"; button.setAttribute("aria-label", `${page.label || "ページ " + page.page_number}の補正画像を確認`); button.addEventListener("click", () => openPagePreview(saved)); row.append(button); }
      else row.append(make("span", "missing-label", "未登録"));
      list.append(row);
    }
    setParticipantBusy(state.busy);
  }
  async function loadParticipant() { renderParticipant(await api("/api/participant", { participant: true })); }
  async function blobForPage(page) { return api(page.preview_url || `/api/participant/pages/${encodeURIComponent(page.qr_code)}/preview`, { participant: true, binary: true }); }
  async function openPagePreview(page) {
    try {
      const blob = await blobForPage(page);
      const url = URL.createObjectURL(blob);
      $("dialog-image").src = url;
      $("dialog-image-title").textContent = `ページ ${page.page_number} · ${page.qr_code}`;
      $("preview-dialog").showModal();
      $("preview-dialog").addEventListener("close", () => URL.revokeObjectURL(url), { once: true });
    } catch (error) { message("app-error", error.message); }
  }
  function confirmAction(title, description, acceptText) {
    const dialog = $("confirm-dialog");
    $("confirm-title").textContent = title; $("confirm-description").textContent = description; $("confirm-accept").textContent = acceptText;
    dialog.returnValue = "cancel"; dialog.showModal();
    return new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.returnValue === "accept"), { once: true }));
  }
  async function uploadFile(file) {
    if (!file || state.busy) return;
    const maxMb = state.config.max_upload_mb || 25;
    if (file.size > maxMb * 1024 * 1024) { message("upload-error", `画像は${maxMb}MB以内にしてください。スマートフォンの撮影設定を確認してください。`); return; }
    message("upload-error", ""); message("app-error", "");
    setParticipantBusy(true); $("upload-progress").hidden = false;
    const send = (replace, expectedVersion) => {
      const form = new FormData(); form.append("file", file); form.append("replace", String(replace));
      if (expectedVersion !== undefined) form.append("expected_version", String(expectedVersion));
      return api("/api/participant/pages", { method: "POST", body: form, participant: true });
    };
    try {
      let result;
      try { result = await send(false); }
      catch (error) {
        if (error.status !== 409 || error.detail?.code !== "duplicate_page") throw error;
        $("upload-progress").hidden = true;
        const yes = await confirmAction("登録済みのページです", `ページ ${error.detail.page_number || ""}（${error.detail.qr_code}）を新しい写真に差し替えますか？新しい画像の確認と保存が完了した後に更新されます。`, "差し替える");
        if (!yes) { announce("差し替えをキャンセルしました。元の画像は保持されています。"); return; }
        $("upload-progress").hidden = false;
        result = await send(true, error.detail.version);
      }
      if (result.participant) renderParticipant(result.participant); else await loadParticipant();
      const page = result.page;
      if (page) {
        const blob = await blobForPage(page);
        if (state.previewObjectUrl) URL.revokeObjectURL(state.previewObjectUrl);
        state.previewObjectUrl = URL.createObjectURL(blob);
        $("latest-image").src = state.previewObjectUrl;
        $("latest-title").textContent = `ページ ${page.page_number} · ${page.qr_code}`;
        $("latest-preview").hidden = false;
        $("preview-open").onclick = () => openPagePreview(page);
        announce(`ページ ${page.page_number}を登録しました。補正後の画像を確認してください。`);
      }
    } catch (error) {
      message("upload-error", error.message);
      if (error.status === 409) { try { await loadParticipant(); } catch { /* Keep the original explanation. */ } }
    } finally {
      $("upload-progress").hidden = true; setParticipantBusy(false);
      $("camera-input").value = ""; $("gallery-input").value = "";
    }
  }
  async function download(blob, filename) {
    const url = URL.createObjectURL(blob); const anchor = make("a"); anchor.href = url; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  async function register() {
    if (state.busy || !$("consent-checkbox").checked) return;
    state.busy = true; $("register-button").disabled = true; $("register-button").textContent = "参加者IDを発行しています…";
    message("app-error", "");
    let key = state.registrationKey || storage.get("localStorage", "handwriting_registration_key");
    if (!key) { key = registrationKey(); storage.set("localStorage", "handwriting_registration_key", key); }
    state.registrationKey = key;
    try {
      const result = await api("/api/register", { method: "POST", body: { consent: true, consent_version: state.config.consent_version, idempotency_key: key } });
      state.token = result.resume_token; storage.set("sessionStorage", "handwriting_resume_token", state.token);
      await loadParticipant(); window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (error) { message("app-error", error.message); }
    finally { state.busy = false; $("register-button").disabled = !$("consent-checkbox").checked; $("register-button").textContent = "同意して参加する →"; if (state.participant) setParticipantBusy(false); }
  }
  async function submit() {
    if (state.busy || !state.participant || state.participant.missing_pages.length) return;
    const yes = await confirmAction("すべてのページを提出しますか？", "設定されたページ順でPDFを作成します。提出後は画像を変更できません。", "提出する");
    if (!yes) return;
    setParticipantBusy(true); $("submit-button").textContent = "PDFを作成しています…"; message("app-error", "");
    try { await api("/api/participant/submit", { method: "POST", participant: true }); await loadParticipant(); $("latest-preview").hidden = true; window.scrollTo({ top: 0, behavior: "smooth" }); announce("提出が完了しました。ご協力ありがとうございます。"); }
    catch (error) { message("app-error", error.message + " 提出状況を確認して、再度お試しください。"); try { await loadParticipant(); } catch { /* The server may still be offline. */ } }
    finally { setParticipantBusy(false); $("submit-button").textContent = "提出する →"; }
  }
  function renderResearchers() {
    $("researcher-content").hidden = false; $("login-panel").hidden = true; $("logout-button").hidden = false;
    const submitted = state.participants.filter(p => p.status === "submitted").length;
    const stats = $("research-stats"); stats.replaceChildren();
    for (const [label, count] of [["登録者", state.participants.length], ["提出済み", submitted], ["記入・撮影中", state.participants.length - submitted]]) { const el = make("div", "stat"); el.append(make("span", "", label)); const strong = make("strong", "", String(count)); strong.append(make("small", "", "人")); el.append(strong); stats.append(el); }
    const rows = $("participant-rows"); rows.replaceChildren();
    for (const participant of state.participants) {
      const row = make("tr"); const checkCell = make("td"); const checkbox = make("input"); checkbox.type = "checkbox"; checkbox.checked = state.selected.has(participant.participant_id); checkbox.setAttribute("aria-label", `${participant.participant_id}を選択`);
      checkbox.addEventListener("change", () => { if (checkbox.checked) state.selected.add(participant.participant_id); else state.selected.delete(participant.participant_id); if (state.selected.size) document.querySelector('input[name="export-scope"][value="selected"]').checked = true; updateSelection(); }); checkCell.append(checkbox);
      const [date, time] = dateParts(participant.registered_at); const dateCell = make("td", "", date); dateCell.append(make("span", "date", time));
      const statusCell = make("td"); statusCell.append(make("span", "state-chip" + (participant.status === "submitted" ? " done" : ""), participant.status === "submitted" ? "提出済み" : participant.status === "processing" ? "処理中" : "登録中"));
      row.append(checkCell, make("td", "participant-code", participant.participant_id), dateCell, statusCell, make("td", "", `${participant.page_count} / ${participant.total_pages ?? state.config.pages.length}`)); rows.append(row);
    }
    $("empty-roster").hidden = state.participants.length > 0; updateSelection();
  }
  function updateSelection() {
    $("selected-count").textContent = `（${state.selected.size}人）`;
    $("select-all").checked = state.participants.length > 0 && state.participants.every(p => state.selected.has(p.participant_id));
    $("select-all").indeterminate = state.selected.size > 0 && !$("select-all").checked;
  }
  async function loadResearchers() {
    const result = await api("/api/researcher/participants"); state.participants = result.participants;
    const available = new Set(state.participants.map(p => p.participant_id)); state.selected = new Set([...state.selected].filter(id => available.has(id))); renderResearchers();
    await loadWorksheetRevision();
  }
  function qrList(id, pages) {
    const list = $(id); list.replaceChildren();
    for (const page of pages || []) list.append(make("li", "", `${page.page_number}ページ · ${page.qr_code}`));
  }
  function revisionBusy(busy) {
    state.revisionBusy = busy;
    $("worksheet-management").setAttribute("aria-busy", String(busy));
    $("revision-file").disabled = busy;
    $("inspect-worksheet").disabled = busy || !$("revision-file").files.length;
    $("activate-worksheet").disabled = busy || !state.worksheetDraft;
    $("preview-draft-worksheet").disabled = busy || !state.worksheetDraft;
    $("download-current-worksheet").disabled = busy || !state.worksheetRevision?.download_url;
  }
  function renderWorksheetRevision(revision) {
    state.worksheetRevision = revision;
    $("current-worksheet-name").textContent = revision.original_name || revision.title || "配布用紙は準備中です";
    $("current-worksheet-count").textContent = revision.page_count ? `${revision.page_count}ページ` : "";
    $("current-worksheet-date").textContent = revision.activated_at ? `更新日時：${dateParts(revision.activated_at).filter(Boolean).join(" ")}` : "";
    $("current-worksheet-pages-details").hidden = !revision.pages?.length;
    qrList("current-worksheet-pages", revision.pages);
    revisionBusy(state.revisionBusy);
  }
  async function loadWorksheetRevision() {
    try { renderWorksheetRevision(await api("/api/researcher/worksheet")); }
    catch (error) { message("revision-message", error.message, true); }
  }
  function clearWorksheetDraft() {
    state.worksheetDraft = null;
    $("worksheet-draft").hidden = true;
    qrList("draft-worksheet-pages", []);
    revisionBusy(state.revisionBusy);
  }
  async function inspectWorksheet() {
    const file = $("revision-file").files[0];
    if (state.revisionBusy || !file) return;
    message("revision-message", ""); clearWorksheetDraft();
    const maxMb = state.config.max_upload_mb || 25;
    if (file.size > maxMb * 1024 * 1024) { message("revision-message", `PDFは${maxMb}MB以内にしてください。`, true); return; }
    revisionBusy(true); $("inspect-worksheet").textContent = "全ページを確認しています…";
    try {
      const form = new FormData(); form.append("file", file);
      const draft = await api("/api/researcher/worksheet/inspect", { method: "POST", body: form, csrf: true });
      state.worksheetDraft = draft;
      $("draft-worksheet-name").textContent = draft.original_name || file.name;
      $("draft-worksheet-count").textContent = `${draft.page_count}ページのQRと用紙位置を確認しました。`;
      qrList("draft-worksheet-pages", draft.pages);
      $("worksheet-draft").hidden = false;
      announce("PDFの確認が完了しました。ページ数とQRを確認してから差し替えてください。");
    } catch (error) { message("revision-message", error.message, true); }
    finally { revisionBusy(false); $("inspect-worksheet").textContent = "PDFを確認"; }
  }
  async function activateWorksheet() {
    const draft = state.worksheetDraft;
    if (state.revisionBusy || !draft) return;
    revisionBusy(true);
    const yes = await confirmAction("配布する用紙を差し替えますか？", "新しく参加する方から、この用紙を配布します。登録済みの参加者は登録時の用紙を継続して使用し、登録画像と提出済みPDFも保持されます。", "この用紙に差し替える");
    if (!yes || state.worksheetDraft !== draft) { revisionBusy(false); return; }
    message("revision-message", "");
    try {
      await api("/api/researcher/worksheet/activate", { method: "POST", csrf: true, body: { draft_id: draft.draft_id, expected_active_revision_id: draft.active_revision_id } });
      clearWorksheetDraft(); $("revision-file").value = "";
      await loadWorksheetRevision();
      message("revision-message", "配布用紙を差し替えました。新しく参加する方から適用されます。");
      announce("配布用紙を差し替えました。");
    } catch (error) {
      message("revision-message", error.message, true);
      if (error.status === 409) { clearWorksheetDraft(); await loadWorksheetRevision(); }
    } finally { revisionBusy(false); }
  }
  async function researcherInit() {
    showView("researcher"); $("mode-link").textContent = "参加者画面へ ↗"; $("mode-link").href = "/";
    try { const session = await api("/api/researcher/session"); if (session.authenticated) { state.csrf = session.csrf_token; await loadResearchers(); } }
    catch (error) { if (error.status !== 401) message("login-error", error.message); }
  }
  $("consent-checkbox").addEventListener("change", () => { $("register-button").disabled = state.busy || !$("consent-checkbox").checked; });
  $("register-button").addEventListener("click", register);
  $("copy-link").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(resumeUrl()); $("copy-link").textContent = "コピーしました ✓"; announce("再開リンクをコピーしました。安全な場所に保存してください。"); }
    catch { $("resume-link").focus(); $("resume-link").select(); announce("再開リンクを選択しました。コピーして保存してください。"); }
  });
  $("camera-button").addEventListener("click", () => $("camera-input").click()); $("gallery-button").addEventListener("click", () => $("gallery-input").click());
  $("camera-input").addEventListener("change", e => uploadFile(e.target.files[0])); $("gallery-input").addEventListener("change", e => uploadFile(e.target.files[0]));
  $("submit-button").addEventListener("click", submit);
  $("revision-file").addEventListener("change", () => { clearWorksheetDraft(); message("revision-message", ""); });
  $("inspect-worksheet").addEventListener("click", inspectWorksheet);
  $("activate-worksheet").addEventListener("click", activateWorksheet);
  $("download-current-worksheet").addEventListener("click", async () => {
    if (state.revisionBusy || !state.worksheetRevision?.download_url) return;
    revisionBusy(true); message("revision-message", "");
    try { await download(await api(state.worksheetRevision.download_url, { binary: true }), "worksheet.pdf"); }
    catch (error) { message("revision-message", error.message, true); }
    finally { revisionBusy(false); }
  });
  $("preview-draft-worksheet").addEventListener("click", async () => {
    if (state.revisionBusy || !state.worksheetDraft?.preview_url) return;
    revisionBusy(true); message("revision-message", "");
    try { await download(await api(state.worksheetDraft.preview_url, { binary: true }), "worksheet-preview.pdf"); }
    catch (error) { message("revision-message", error.message, true); }
    finally { revisionBusy(false); }
  });
  $("download-worksheet").addEventListener("click", async () => {
    const worksheet = state.participant?.worksheet;
    if (state.worksheetBusy || !worksheet?.available) return;
    state.worksheetBusy = true;
    const button = $("download-worksheet");
    button.disabled = true; button.textContent = "用紙を準備しています…"; message("worksheet-message", "");
    try {
      const filename = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.pdf$/.test(worksheet.filename || "") ? worksheet.filename : "worksheet.pdf";
      await download(await api(worksheet.download_url || "/api/participant/worksheet", { participant: true, binary: true }), filename);
      announce("記入用紙のダウンロードを開始しました。A4・片面・実際のサイズで印刷してください。");
    } catch (error) { message("worksheet-message", error.message); }
    finally { state.worksheetBusy = false; button.disabled = false; button.textContent = "記入用紙をダウンロード（PDF）"; }
  });
  $("close-preview").addEventListener("click", () => $("preview-dialog").close());
  $("confirm-cancel").addEventListener("click", () => $("confirm-dialog").close("cancel")); $("confirm-accept").addEventListener("click", () => $("confirm-dialog").close("accept"));
  $("download-pdf").addEventListener("click", async () => { const button = $("download-pdf"); button.disabled = true; try { await download(await api("/api/participant/pdf", { participant: true, binary: true }), `${state.participant.participant_id}.pdf`); } catch (error) { message("app-error", error.message); } finally { button.disabled = false; } });
  $("login-form").addEventListener("submit", async event => {
    event.preventDefault(); $("login-button").disabled = true; message("login-error", "");
    try { const result = await api("/api/researcher/login", { method: "POST", body: { password: $("password-input").value } }); $("password-input").value = ""; state.csrf = result.csrf_token; await loadResearchers(); }
    catch (error) { message("login-error", error.message); }
    finally { $("login-button").disabled = false; }
  });
  $("logout-button").addEventListener("click", async () => { try { await api("/api/researcher/logout", { method: "POST", csrf: true }); state.csrf = null; state.selected.clear(); clearWorksheetDraft(); state.worksheetRevision = null; $("revision-file").value = ""; message("revision-message", ""); $("researcher-content").hidden = true; $("login-panel").hidden = false; $("logout-button").hidden = true; } catch (error) { message("app-error", error.message); } });
  $("refresh-participants").addEventListener("click", async () => { $("refresh-participants").disabled = true; try { await loadResearchers(); message("app-error", ""); } catch (error) { message("app-error", error.message); } finally { $("refresh-participants").disabled = false; } });
  $("select-all").addEventListener("change", event => { state.selected = event.target.checked ? new Set(state.participants.map(p => p.participant_id)) : new Set(); document.querySelector('input[name="export-scope"][value="selected"]').checked = true; renderResearchers(); });
  $("export-button").addEventListener("click", async () => {
    const scope = document.querySelector('input[name="export-scope"]:checked').value;
    const types = [...document.querySelectorAll('input[name="export-type"]:checked')].map(el => el.value);
    if (!types.length) { message("export-message", "ファイルの種類を1つ以上選択してください。", true); return; }
    if (scope === "selected" && !state.selected.size) { message("export-message", "一覧で対象の参加者を選択してください。", true); return; }
    if (scope === "submitted" && !state.participants.some(p => p.status === "submitted")) { message("export-message", "提出済みの参加者がいません。", true); return; }
    $("export-button").disabled = true; $("export-button").textContent = "ZIPを準備しています…"; message("export-message", "");
    try { const blob = await api("/api/researcher/download", { method: "POST", csrf: true, binary: true, body: { scope, participant_ids: [...state.selected], types } }); await download(blob, "handwriting_collection.zip"); message("export-message", "選択したデータのダウンロードを開始しました。"); }
    catch (error) { message("export-message", error.message, true); }
    finally { $("export-button").disabled = false; $("export-button").textContent = "ZIPをダウンロード ↓"; }
  });

  async function init() {
    try {
      state.config = await api("/api/config");
      $("profile-banner").hidden = state.config.production_ready === true;
      $("consent-version").textContent = `同意文 ${state.config.consent_version}`;
      $("consent-text").textContent = state.config.consent_text;
      if (state.config.title) { document.title = state.config.title + " | 筆跡収集"; $("study-title").textContent = state.config.title; }
      if (location.pathname.replace(/\/$/, "") === "/researcher") { await researcherInit(); return; }
      if (state.token) {
        try { await loadParticipant(); }
        catch (error) { showView("registration"); $("register-button").disabled = true; $("consent-checkbox").disabled = true; message("app-error", error.status === 401 ? "再開リンクが無効です。保存してあるリンクを確認して開き直してください。参加者IDだけでは再開できません。" : error.message + " ページを再読み込みして再開してください。"); }
      } else showView("registration");
    } catch (error) { $("boot").hidden = true; message("app-error", error.message + " ページを再読み込みしてください。"); }
  }
  init();
})();
