"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { config: null, participant: null, token: null, busy: false, collecting: false, worksheetBusy: false, csrf: null, participants: [], selected: new Set(), previewObjectUrl: null, worksheetRevision: null, worksheetSelection: [], revisionBusy: false };
  const storage = {
    get(store, key) { try { return window[store].getItem(key); } catch { return null; } },
    set(store, key, value) { try { window[store].setItem(key, value); } catch { /* Private browsing can disable storage. */ } },
    remove(store, key) { try { window[store].removeItem(key); } catch { /* Private browsing can disable storage. */ } }
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
      error.status = response.status; error.detail = data.detail;
      if (error.status === 410 && error.detail?.code === "registration_expired") resetExpiredRegistration(error.message);
      throw error;
    }
    return binary ? response.blob() : response.status === 204 ? {} : response.json();
  }
  function showView(name) {
    if (name !== "participant") camera.stop();
    for (const view of ["registration", "participant", "researcher"]) $(view + "-view").hidden = view !== name;
    $("boot").hidden = true;
  }
  function resetExpiredRegistration(text) {
    state.token = null; state.participant = null; state.registrationKey = null;
    storage.remove("sessionStorage", "handwriting_resume_token");
    storage.remove("localStorage", "handwriting_registration_key");
    if (state.previewObjectUrl) { URL.revokeObjectURL(state.previewObjectUrl); state.previewObjectUrl = null; }
    $("resume-link").value = "";
    $("latest-preview").hidden = true;
    $("consent-checkbox").checked = false; $("consent-checkbox").disabled = false;
    $("register-button").disabled = true;
    showView("registration"); message("app-error", text);
    window.scrollTo({ top: 0, behavior: "smooth" });
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
    const submitted = state.participant?.status === "submitted";
    const locked = busy || state.collecting || submitted;
    $("camera-button").disabled = locked; $("gallery-button").disabled = locked;
    photoQueue.setBusy(locked);
    $("upload-section").setAttribute("aria-busy", String(busy));
    $("submit-button").disabled = locked || !state.participant || photoQueue.hasPending;
    for (const button of document.querySelectorAll(".worksheet-submit")) button.disabled = locked || photoQueue.hasPending;
    for (const button of document.querySelectorAll(".worksheet-result-download")) button.disabled = busy || state.collecting;
    if (state.participant) $("submit-hint").textContent = submitted ? "提出済みです。" : photoQueue.hasPending ? "一覧の写真を先に登録するか、一覧から外してください。" : state.participant.worksheet_submissions?.length > 1 ? "PDFごとに提出できます。未登録ページは未記入テンプレートで補います。" : "未登録ページは未記入テンプレートで補って提出できます。";
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
    if (submitted) camera.stop();
    $("participant-heading-title").textContent = submitted ? "ご協力ありがとうございます" : "用紙を撮影して登録";
    $("participant-lead").textContent = submitted ? "提出済みのデータを確認できます。" : "用紙をダウンロードし、印刷・記入してから撮影してください。用紙はどの順番からでも登録できます。";
    const worksheet = participant.worksheet;
    $("worksheet-unavailable").hidden = Boolean(worksheet?.available);
    $("worksheet-test-note").hidden = !worksheet?.is_test;
    $("worksheet-file-title").textContent = worksheet?.title || "";
    renderWorksheetDownloads(participant);
    $("upload-section").hidden = submitted;
    $("submitted-panel").hidden = !submitted;
    $("submit-button").hidden = submitted || participant.worksheet_submissions?.length > 1;
    const blankCodes = new Set((participant.worksheet_submissions || []).flatMap(file => file.blank_qr_codes));
    $("page-count").textContent = `${participant.pages.length + blankCodes.size} / ${state.config.pages.length}`;
    const percent = state.config.pages.length ? Math.round((participant.pages.length + blankCodes.size) / state.config.pages.length * 100) : 0;
    $("progress-fill").style.width = `${percent}%`;
    $("progress-fill").parentElement.setAttribute("aria-valuenow", String(percent));
    $("progress-description").textContent = submitted ? `提出が完了しました。写真${participant.pages.length}ページ・未記入${blankCodes.size}ページです。` : `写真${participant.pages.length}ページを登録済み、${blankCodes.size}ページを未記入で提出済みです。未登録ページは未記入テンプレートで補えます。`;
    const list = $("page-list"); list.replaceChildren();
    for (const page of state.config.pages) {
      const saved = participant.pages.find(p => p.qr_code === page.qr_code);
      const row = make("li", "page-row" + (saved ? " complete" : ""));
      row.append(make("span", "page-state", saved ? "✓" : blankCodes.has(page.qr_code) ? "未" : String(page.page_number)));
      const info = make("div", "page-info");
      info.append(make("strong", "", page.label || `ページ ${page.page_number}`), make("small", "", page.qr_code + (blankCodes.has(page.qr_code) ? " · 未記入で提出済み" : page.required === false ? " · 任意" : "")));
      row.append(info);
      if (saved) { const button = make("button", "", "確認"); button.type = "button"; button.setAttribute("aria-label", `${page.label || "ページ " + page.page_number}の補正画像を確認`); button.addEventListener("click", () => openPagePreview(saved)); row.append(button); }
      else row.append(make("span", "missing-label", blankCodes.has(page.qr_code) ? "未記入で提出済み" : "未登録"));
      list.append(row);
    }
    renderWorksheetSubmissions(participant);
    setParticipantBusy(state.busy);
  }
  async function loadParticipant() { renderParticipant(await api("/api/participant", { participant: true })); }
  function numbered(number) {
    return number <= 20 ? String.fromCodePoint(0x2460 + number - 1) : number <= 35 ? String.fromCodePoint(0x3251 + number - 21) : number <= 50 ? String.fromCodePoint(0x32b1 + number - 36) : `(${number})`;
  }
  function renderWorksheetDownloads(participant) {
    const files = participant.worksheet?.available ? participant.worksheets || [] : [];
    const first = $("download-worksheet");
    $("worksheet-downloads").replaceChildren(first);
    first.hidden = !participant.worksheet?.available;
    first.disabled = state.worksheetBusy;
    first.textContent = files.length ? `${files[0].number_label} ${files[0].original_name || files[0].title}（${files[0].page_count}ページ） ↓` : "記入用紙をダウンロード（PDF）";
    for (const file of files.slice(1)) {
      const button = make("button", "button primary worksheet-download", `${file.number_label} ${file.original_name || file.title}（${file.page_count}ページ） ↓`);
      button.type = "button"; button.disabled = state.worksheetBusy;
      button.addEventListener("click", () => downloadWorksheet(file, button));
      $("worksheet-downloads").append(button);
    }
    if (files.length > 1) $("worksheet-file-title").textContent = `${files.length}種類のPDFがあります。それぞれ個別に提出できます。未登録ページは同じ用紙の未記入テンプレートで補います。`;
  }
  function renderWorksheetSubmissions(participant) {
    const files = participant.worksheet_submissions || [];
    $("worksheet-submissions").hidden = files.length <= 1;
    const list = $("worksheet-submission-list"); list.replaceChildren();
    for (const file of files) {
      const row = make("div", "worksheet-submission-row"); row.dataset.worksheetId = file.id; row.dataset.status = file.status;
      const done = file.status === "submitted";
      row.append(make("h3", "", `${file.number_label} ${file.original_name || file.title}`));
      row.append(make("p", "small muted", done ? `提出済み · 写真${file.registered_pages}ページ／未記入${file.blank_pages}ページ` : `全${file.page_count}ページ中、写真${file.registered_pages}ページを登録済み。未登録の${file.missing_page_count}ページは未記入テンプレートで補います。`));
      const button = make("button", `button ${done ? "secondary worksheet-result-download" : "primary worksheet-submit"}`, done ? `${file.number_label}の提出PDFを確認` : `${file.number_label}を提出する`);
      button.type = "button";
      button.addEventListener("click", done ? async () => {
        if (state.busy || state.collecting) return;
        setParticipantBusy(true);
        try { await download(await api(file.pdf_url, { participant: true, binary: true }), `${participant.participant_id}_${String(file.number).padStart(2, "0")}.pdf`); }
        catch (error) { message("app-error", error.message); }
        finally { setParticipantBusy(false); }
      } : () => submit(file));
      row.append(button); list.append(row);
    }
  }
  async function downloadWorksheet(worksheet, button) {
    if (state.worksheetBusy || !worksheet) return;
    state.worksheetBusy = true; const label = button.textContent;
    for (const item of document.querySelectorAll(".worksheet-download")) item.disabled = true;
    button.textContent = "用紙を準備しています…"; message("worksheet-message", "");
    try {
      const filename = /\.pdf$/i.test(worksheet.original_name || "") ? worksheet.original_name : /^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.pdf$/.test(worksheet.filename || "") ? worksheet.filename : "worksheet.pdf";
      await download(await api(worksheet.download_url || "/api/participant/worksheet", { participant: true, binary: true }), filename);
      announce("記入用紙のダウンロードを開始しました。A4・片面・実際のサイズで印刷してください。");
    } catch (error) { message("worksheet-message", error.message); }
    finally {
      state.worksheetBusy = false; button.textContent = label;
      for (const item of document.querySelectorAll(".worksheet-download")) item.disabled = false;
    }
  }
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
    const maxMb = state.config.max_upload_mb || 25;
    if (file.size > maxMb * 1024 * 1024) throw new Error(`画像は${maxMb}MB以内にしてください。`);
    const send = (replace, expectedVersion) => {
      const form = new FormData(); form.append("file", file); form.append("replace", String(replace));
      if (expectedVersion !== undefined) form.append("expected_version", String(expectedVersion));
      return api("/api/participant/pages", { method: "POST", body: form, participant: true });
    };
    let result;
    try { result = await send(false); }
    catch (error) {
      if (error.status !== 409 || error.detail?.code !== "duplicate_page") throw error;
      const yes = await confirmAction("登録済みのページです", `ページ ${error.detail.page_number || ""}（${error.detail.qr_code}）を新しい写真に差し替えますか？新しい画像の確認と保存が完了した後に更新されます。`, "差し替える");
      if (!yes) return { skipped: true };
      result = await send(true, error.detail.version);
    }
    // A successful save is final even if a later preview refresh fails.
    return result;
  }

  async function registerQueuedPhotos() {
    if (state.busy || state.collecting || !state.token || !state.participant || state.participant.status === "submitted") return;
    const entries = photoQueue.pending.slice();
    if (!entries.length) return;
    const token = state.token;
    let saved = 0, failed = 0, skipped = 0, lastPage = null;
    message("upload-error", ""); message("app-error", "");
    $("batch-status").textContent = "";
    setParticipantBusy(true); $("batch-progress").hidden = false;
    try {
      for (const [index, entry] of entries.entries()) {
        if (state.token !== token || state.participant?.status === "submitted") break;
        $("batch-progress-label").textContent = `${index + 1} / ${entries.length}枚目のQR・四隅・画質を確認しています…`;
        photoQueue.mark(entry, "uploading");
        try {
          const result = await uploadFile(entry.file);
          if (result.skipped) { photoQueue.mark(entry, "skipped"); skipped++; continue; }
          photoQueue.mark(entry, "saved", { page: result.page }); saved++;
          if (result.page) lastPage = result.page;
          if (result.participant) renderParticipant(result.participant);
        } catch (error) {
          photoQueue.mark(entry, "failed", { error: error.message }); failed++;
          if (error.status === 401 || error.status === 410 || error.detail?.code === "already_submitted") break;
        }
      }
      if (state.token === token) {
        try { await loadParticipant(); }
        catch { message("upload-error", "登録結果の一覧を更新できませんでした。登録済みの写真は保存されています。未登録の写真がある場合は、このページを閉じずに再試行してください。"); }
        if (lastPage && state.participant?.status !== "submitted") {
          try {
            const blob = await blobForPage(lastPage);
            if (state.previewObjectUrl) URL.revokeObjectURL(state.previewObjectUrl);
            state.previewObjectUrl = URL.createObjectURL(blob);
            $("latest-image").src = state.previewObjectUrl;
            $("latest-title").textContent = `ページ ${lastPage.page_number} · ${lastPage.qr_code}`;
            $("latest-preview").hidden = false;
            $("preview-open").onclick = () => openPagePreview(lastPage);
          } catch { message("upload-error", "写真の登録は完了しましたが、補正プレビューを取得できませんでした。登録状況の「確認」から開き直してください。"); }
        }
      }
    } finally {
      $("batch-progress").hidden = true; setParticipantBusy(false);
      const parts = [`${saved}枚を登録しました。`];
      if (failed) parts.push(`${failed}枚は未登録です。理由を確認して再試行できます。`);
      if (skipped) parts.push(`${skipped}枚の差し替えを見送りました。`);
      $("batch-status").textContent = parts.join(" ");
      announce(parts.join(" "));
    }
  }

  function addPhotos(files, source) {
    if (state.busy || state.collecting || state.participant?.status === "submitted") return;
    const { added, rejected } = photoQueue.add(files, state.config.max_upload_mb || 25);
    message("upload-error", rejected.join(" "));
    if (!added.length) return;
    $("capture-next-count").textContent = `登録待ちの写真は${photoQueue.pending.length}枚です。`;
    $("capture-next-continue").textContent = source === "gallery" ? "続けて選ぶ" : "続けて撮影";
    $("capture-next-dialog").dataset.source = source;
    $("capture-next-dialog").showModal();
  }

  async function capturePhoto() {
    if (state.busy || state.collecting || state.participant?.status === "submitted") return;
    message("upload-error", "");
    if (!camera.supported) { $("camera-input").click(); return; }
    state.collecting = true; setParticipantBusy(false);
    let file;
    try { file = await camera.open(); }
    finally { state.collecting = false; setParticipantBusy(state.busy); }
    if (file) addPhotos([file], "camera");
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
  async function submit(file = null) {
    if (state.busy || state.collecting || photoQueue.hasPending || !state.participant || state.participant.status === "submitted") return;
    setParticipantBusy(true); $("submit-button").textContent = "PDFを作成しています…"; message("app-error", "");
    try {
      const missing = file ? file.missing_page_count : state.config.pages.length - state.participant.pages.length;
      const yes = await confirmAction(file ? `${file.number_label}を提出しますか？` : "この用紙を提出しますか？", `未登録の${missing}ページを、参加登録時の同じ用紙の未記入テンプレートで補います。提出後は、この用紙の写真を変更できません。${file ? "他の未提出の用紙は引き続き登録・提出できます。" : ""}`, "未記入を含めて提出する");
      if (!yes) return;
      await api(file ? file.submit_url : "/api/participant/submit", { method: "POST", participant: true });
      await loadParticipant(); $("latest-preview").hidden = true;
      message("app-error", ""); announce(file ? `${file.number_label}の提出が完了しました。他の未提出の用紙は引き続き登録できます。` : "提出が完了しました。ご協力ありがとうございます。");
      if (file) $("worksheet-submissions").scrollIntoView({ block: "start", behavior: "smooth" });
      else window.scrollTo({ top: 0, behavior: "smooth" });
    }
    catch (error) { message("app-error", error.message + " 提出状況を確認して、再度お試しください。"); try { await loadParticipant(); } catch { /* The server may still be offline. */ } }
    finally { setParticipantBusy(false); $("submit-button").textContent = "提出する →"; }
  }
  function renderResearchers() {
    $("researcher-content").hidden = false; $("login-panel").hidden = true; $("logout-button").hidden = false;
    const submitted = state.participants.filter(p => p.status === "submitted" || p.status === "partially_submitted").length;
    const stats = $("research-stats"); stats.replaceChildren();
    for (const [label, count] of [["登録者", state.participants.length], ["提出あり", submitted], ["提出なし", state.participants.length - submitted]]) { const el = make("div", "stat"); el.append(make("span", "", label)); const strong = make("strong", "", String(count)); strong.append(make("small", "", "人")); el.append(strong); stats.append(el); }
    const rows = $("participant-rows"); rows.replaceChildren();
    for (const participant of state.participants) {
      const row = make("tr"); const checkCell = make("td"); const checkbox = make("input"); checkbox.type = "checkbox"; checkbox.checked = state.selected.has(participant.participant_id); checkbox.setAttribute("aria-label", `${participant.participant_id}を選択`);
      checkbox.addEventListener("change", () => { if (checkbox.checked) state.selected.add(participant.participant_id); else state.selected.delete(participant.participant_id); if (state.selected.size) document.querySelector('input[name="export-scope"][value="selected"]').checked = true; updateSelection(); }); checkCell.append(checkbox);
      const [date, time] = dateParts(participant.registered_at); const dateCell = make("td", "", date); dateCell.append(make("span", "date", time));
      const statusCell = make("td"); statusCell.append(make("span", "state-chip" + (participant.status === "submitted" ? " done" : ""), participant.status === "submitted" ? "提出済み" : participant.status === "partially_submitted" ? "一部提出" : "登録中"));
      if (participant.total_worksheets) statusCell.append(make("span", "date", `${participant.submitted_worksheet_count} / ${participant.total_worksheets}用紙提出 · 未記入${participant.blank_page_count}ページ`));
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
    $("download-current-worksheet").disabled = busy || !state.worksheetRevision?.download_url;
    $("download-current-worksheet").hidden = !state.worksheetRevision?.download_url;
    $("save-worksheet-selection").disabled = busy || !state.worksheetSelection.length;
    for (const control of document.querySelectorAll("#worksheet-library input, #worksheet-library button, #current-worksheets button")) control.disabled = busy;
  }
  function renderWorksheetRevision(revision, preserveSelection = false) {
    const preserve = preserveSelection && state.worksheetRevision?.active_revision_id === revision.active_revision_id;
    state.worksheetRevision = revision;
    if (!preserve) state.worksheetSelection = [...(revision.selected_worksheet_ids || [])];
    $("current-worksheet-name").textContent = revision.worksheets?.length ? `${revision.worksheets.length}種類のPDFを配布中` : revision.original_name || revision.title || "配布用紙は準備中です";
    $("current-worksheet-count").textContent = revision.page_count ? `提出対象：合計${revision.page_count}ページ` : "";
    $("current-worksheet-date").textContent = revision.activated_at ? `更新日時：${dateParts(revision.activated_at).filter(Boolean).join(" ")}` : "";
    $("current-worksheet-pages-details").hidden = !revision.pages?.length;
    qrList("current-worksheet-pages", revision.pages);
    const current = $("current-worksheets"); current.replaceChildren();
    for (const file of revision.worksheets || []) {
      const row = make("li"); const button = make("button", "text-button", `${file.number_label} ${file.original_name || file.title}`);
      button.type = "button"; button.addEventListener("click", () => downloadLibraryFile(file));
      row.append(button); current.append(row);
    }
    renderWorksheetLibrary();
    revisionBusy(state.revisionBusy);
  }
  function renderWorksheetLibrary() {
    const library = $("worksheet-library"); library.replaceChildren();
    for (const file of state.worksheetRevision?.library || []) {
      const row = make("div", "worksheet-library-row");
      const label = make("label", "check-row"); const check = make("input"); check.type = "checkbox"; check.value = file.id;
      check.checked = state.worksheetSelection.includes(file.id); check.disabled = state.revisionBusy;
      check.addEventListener("change", () => {
        state.worksheetSelection = check.checked ? [...state.worksheetSelection, file.id] : state.worksheetSelection.filter(id => id !== file.id);
        renderWorksheetSelection(); revisionBusy(state.revisionBusy);
      });
      const info = make("span", "worksheet-library-info", file.original_name || file.title);
      info.append(make("small", "muted", `${file.page_count}ページ${file.is_test ? " · テスト用紙" : ""}`)); label.append(check, info);
      const button = make("button", "button secondary compact", "PDF確認"); button.type = "button";
      button.addEventListener("click", () => downloadLibraryFile(file)); row.append(label, button); library.append(row);
    }
    if (!library.children.length) library.append(make("p", "small muted", "PDFを登録すると、ここから配布対象を選べます。"));
    renderWorksheetSelection();
  }
  function renderWorksheetSelection() {
    const preview = $("worksheet-selection-preview"); preview.replaceChildren();
    for (const [index, id] of state.worksheetSelection.entries()) {
      const file = state.worksheetRevision.library.find(item => item.id === id);
      if (file) preview.append(make("li", "", `${numbered(index + 1)} ${file.original_name || file.title}`));
    }
    $("worksheet-selection-summary").textContent = `${state.worksheetSelection.length}件を選択中。選んだ順に①・②…と表示します。`;
  }
  async function downloadLibraryFile(file) {
    if (state.revisionBusy) return;
    revisionBusy(true); message("revision-message", "");
    try { await download(await api(file.download_url, { binary: true }), file.original_name || file.filename || "worksheet.pdf"); }
    catch (error) { message("revision-message", error.message, true); }
    finally { revisionBusy(false); }
  }
  async function loadWorksheetRevision(preserveSelection = false) {
    try { renderWorksheetRevision(await api("/api/researcher/worksheet"), preserveSelection); }
    catch (error) { message("revision-message", error.message, true); }
  }
  async function inspectWorksheet() {
    const files = Array.from($("revision-file").files);
    if (state.revisionBusy || !files.length) return;
    message("revision-message", "");
    const maxMb = state.config.max_upload_mb || 25;
    revisionBusy(true); let saved = 0; const errors = [];
    try {
      for (const [index, file] of files.entries()) {
        $("inspect-worksheet").textContent = `${index + 1} / ${files.length}件目を確認しています…`;
        try {
          if (file.size > maxMb * 1024 * 1024) throw new Error(`PDFは${maxMb}MB以内にしてください。`);
          const form = new FormData(); form.append("file", file);
          await api("/api/researcher/worksheet/inspect", { method: "POST", body: form, csrf: true }); saved++;
        } catch (error) { errors.push(`${file.name}：${error.message}`); if (error.status === 401 || error.status === 403) break; }
      }
      $("revision-file").value = ""; await loadWorksheetRevision(true);
      const text = `${saved}件のPDFを登録しました。一覧から配布対象を選択してください。${errors.length ? " " + errors.join(" ") : ""}`;
      message("revision-message", text, errors.length > 0); announce(text);
    } finally { revisionBusy(false); $("inspect-worksheet").textContent = "PDFを登録"; }
  }
  async function activateWorksheet() {
    if (state.revisionBusy || !state.worksheetSelection.length || !state.worksheetRevision) return;
    const worksheetIds = [...state.worksheetSelection], expectedId = state.worksheetRevision.active_revision_id;
    revisionBusy(true);
    const yes = await confirmAction(`${worksheetIds.length}件のPDFを配布しますか？`, "選択したPDFすべてを、新しく参加する方の記入・提出対象にします。登録済みの方は、登録時の用紙を継続します。", "この選択で配布する");
    if (!yes) { revisionBusy(false); return; }
    message("revision-message", "");
    try {
      renderWorksheetRevision(await api("/api/researcher/worksheet/selection", { method: "POST", csrf: true, body: { worksheet_ids: worksheetIds, expected_active_revision_id: expectedId } }));
      message("revision-message", "配布するPDFを保存しました。新しく参加する方から適用されます。");
      announce("配布するPDFを保存しました。");
    } catch (error) {
      message("revision-message", error.message, true);
      if (error.status === 409) await loadWorksheetRevision();
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
  const photoQueue = new window.WorksheetPhotoQueue({
    onChange: () => setParticipantBusy(state.busy),
    onPreview: async entry => {
      if (entry.page) { await openPagePreview(entry.page); return; }
      if (!entry.file) return;
      const url = URL.createObjectURL(entry.file);
      $("dialog-image").src = url; $("dialog-image-title").textContent = entry.name;
      $("preview-dialog").showModal();
      $("preview-dialog").addEventListener("close", () => URL.revokeObjectURL(url), { once: true });
    }
  });
  const camera = new window.WorksheetCamera({ onError: text => message("upload-error", text) });
  $("camera-button").addEventListener("click", capturePhoto);
  $("queue-camera").addEventListener("click", capturePhoto);
  $("gallery-button").addEventListener("click", () => $("gallery-input").click());
  $("queue-gallery").addEventListener("click", () => $("gallery-input").click());
  $("camera-input").addEventListener("change", event => { const files = Array.from(event.target.files); event.target.value = ""; addPhotos(files, "camera"); });
  $("gallery-input").addEventListener("change", event => { const files = Array.from(event.target.files); event.target.value = ""; addPhotos(files, "gallery"); });
  $("batch-register").addEventListener("click", registerQueuedPhotos);
  $("capture-next-continue").addEventListener("click", () => {
    const source = $("capture-next-dialog").dataset.source;
    $("capture-next-dialog").close();
    if (source === "gallery") $("gallery-input").click(); else capturePhoto();
  });
  $("capture-next-register").addEventListener("click", () => { $("capture-next-dialog").close(); registerQueuedPhotos(); });
  $("capture-next-review").addEventListener("click", () => { $("capture-next-dialog").close(); $("photo-queue").scrollIntoView({ block: "start", behavior: "smooth" }); });
  $("submit-button").addEventListener("click", () => submit());
  $("revision-file").addEventListener("change", () => { message("revision-message", ""); revisionBusy(state.revisionBusy); });
  $("inspect-worksheet").addEventListener("click", inspectWorksheet);
  $("save-worksheet-selection").addEventListener("click", activateWorksheet);
  $("download-current-worksheet").addEventListener("click", async () => {
    if (state.revisionBusy || !state.worksheetRevision?.download_url) return;
    revisionBusy(true); message("revision-message", "");
    try { await download(await api(state.worksheetRevision.download_url, { binary: true }), "worksheet.pdf"); }
    catch (error) { message("revision-message", error.message, true); }
    finally { revisionBusy(false); }
  });
  $("download-worksheet").addEventListener("click", () => {
    const worksheet = state.participant?.worksheets?.[0] || (state.participant?.worksheet?.available ? state.participant.worksheet : null);
    downloadWorksheet(worksheet, $("download-worksheet"));
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
  $("logout-button").addEventListener("click", async () => { try { await api("/api/researcher/logout", { method: "POST", csrf: true }); state.csrf = null; state.selected.clear(); state.worksheetRevision = null; $("revision-file").value = ""; message("revision-message", ""); $("researcher-content").hidden = true; $("login-panel").hidden = false; $("logout-button").hidden = true; } catch (error) { message("app-error", error.message); } });
  $("refresh-participants").addEventListener("click", async () => { $("refresh-participants").disabled = true; try { await loadResearchers(); message("app-error", ""); } catch (error) { message("app-error", error.message); } finally { $("refresh-participants").disabled = false; } });
  $("select-all").addEventListener("change", event => { state.selected = event.target.checked ? new Set(state.participants.map(p => p.participant_id)) : new Set(); document.querySelector('input[name="export-scope"][value="selected"]').checked = true; renderResearchers(); });
  $("export-button").addEventListener("click", async () => {
    const scope = document.querySelector('input[name="export-scope"]:checked').value;
    const types = [...document.querySelectorAll('input[name="export-type"]:checked')].map(el => el.value);
    if (!types.length) { message("export-message", "ファイルの種類を1つ以上選択してください。", true); return; }
    if (scope === "selected" && !state.selected.size) { message("export-message", "一覧で対象の参加者を選択してください。", true); return; }
    if (scope === "submitted" && !state.participants.some(p => p.status === "submitted" || p.status === "partially_submitted")) { message("export-message", "提出された用紙がありません。", true); return; }
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
        catch (error) { if (error.status === 410 && error.detail?.code === "registration_expired") return; showView("registration"); $("register-button").disabled = true; $("consent-checkbox").disabled = true; message("app-error", error.status === 401 ? "再開リンクが無効です。保存してあるリンクを確認して開き直してください。参加者IDだけでは再開できません。" : error.message + " ページを再読み込みして再開してください。"); }
      } else showView("registration");
    } catch (error) { $("boot").hidden = true; message("app-error", error.message + " ページを再読み込みしてください。"); }
  }
  init();
})();
