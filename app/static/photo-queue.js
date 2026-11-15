"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  window.WorksheetPhotoQueue = class {
    constructor({ onChange = () => {}, onPreview = () => {} } = {}) {
      this.entries = [];
      this.busy = false;
      this.sequence = 0;
      this.onChange = onChange;
      this.onPreview = onPreview;
      this.thumbnailWork = Promise.resolve();
    }

    get pending() { return this.entries.filter(entry => entry.status === "pending" || entry.status === "failed"); }
    get hasPending() { return this.pending.length > 0 || this.entries.some(entry => entry.status === "uploading"); }

    add(files, maxMb) {
      const added = [], rejected = [];
      const accepted = Array.from(files).filter(file => {
        if (file.size > maxMb * 1024 * 1024) { rejected.push(`${file.name}：${maxMb}MBを超えています。`); return false; }
        return true;
      });
      if (accepted.length) {
        for (const entry of [...this.entries]) if (entry.status === "saved" || entry.status === "skipped") this.remove(entry, false);
        for (const file of accepted) {
          const entry = { id: ++this.sequence, file, name: file.name, status: "pending", error: "", thumbnail: null, page: null };
          this.entries.push(entry); added.push(entry);
          // Decode only one source image at a time and retain small thumbnails.
          this.thumbnailWork = this.thumbnailWork.then(async () => {
            if (!this.entries.includes(entry)) return;
            const thumbnail = await this.thumbnail(file);
            if (this.entries.includes(entry)) { entry.thumbnail = thumbnail; this.render(); }
            else if (thumbnail) URL.revokeObjectURL(thumbnail);
          }).catch(() => { /* An unreadable thumbnail can still be checked by the server. */ });
        }
        $("batch-status").textContent = "";
        this.changed();
      }
      return { added, rejected };
    }

    async thumbnail(file) {
      const url = URL.createObjectURL(file);
      try {
        const image = new Image();
        await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = reject; image.src = url; });
        const scale = Math.min(1, 240 / Math.max(image.naturalWidth, image.naturalHeight));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
        canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
        const context = canvas.getContext("2d");
        if (!context) return null;
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.8));
        return blob ? URL.createObjectURL(blob) : null;
      } catch { return null; }
      finally { URL.revokeObjectURL(url); }
    }

    remove(entry, notify = true) {
      if (entry.thumbnail) URL.revokeObjectURL(entry.thumbnail);
      entry.file = null;
      this.entries = this.entries.filter(item => item !== entry);
      if (notify) this.changed();
    }

    mark(entry, status, { error = "", page = null } = {}) {
      if (!this.entries.includes(entry)) return;
      entry.status = status; entry.error = error; entry.page = page || entry.page;
      if (status === "saved") entry.file = null;
      this.changed();
    }

    setBusy(busy) { this.busy = busy; this.render(); }
    changed() { this.render(); this.onChange(); }

    render() {
      $("photo-queue").hidden = !this.entries.length;
      $("photo-queue").setAttribute("aria-busy", String(this.busy));
      $("queue-count").textContent = `${this.pending.length}枚が未登録`;
      $("batch-register").disabled = this.busy || !this.pending.length;
      $("batch-register").textContent = this.entries.some(entry => entry.status === "failed") ? `未登録の写真を再試行（${this.pending.length}枚）` : `まとめて登録（${this.pending.length}枚）`;
      $("queue-camera").disabled = this.busy;
      $("queue-gallery").disabled = this.busy;
      const list = $("queue-list"); list.replaceChildren();
      const labels = { pending: "登録待ち", uploading: "登録しています…", saved: "登録済み", failed: "未登録", skipped: "差し替えを見送り" };
      for (const entry of this.entries) {
        const row = element("li", "queued-photo"); row.dataset.status = entry.status;
        const preview = element("button", "queued-photo-preview"); preview.type = "button";
        preview.setAttribute("aria-label", `${entry.name}を確認`);
        preview.disabled = this.busy || (!entry.file && !entry.page);
        if (entry.thumbnail) {
          const image = element("img"); image.src = entry.thumbnail; image.alt = ""; image.loading = "lazy"; preview.append(image);
        } else preview.append(element("span", "small muted", "写真"));
        preview.addEventListener("click", () => this.onPreview(entry));
        row.append(preview);
        const information = element("div", "queued-photo-info");
        information.append(element("strong", "queued-photo-name", entry.name), element("span", "queued-photo-state", labels[entry.status]));
        if (entry.page) information.append(element("span", "small muted", `ページ ${entry.page.page_number} · ${entry.page.qr_code}`));
        if (entry.error) information.append(element("span", "queued-photo-error", entry.error));
        row.append(information);
        const remove = element("button", "queued-photo-remove", "×"); remove.type = "button"; remove.disabled = this.busy;
        remove.setAttribute("aria-label", `${entry.name}を一覧から外す`);
        remove.addEventListener("click", () => { if (!this.busy) this.remove(entry); });
        row.append(remove); list.append(row);
      }
    }
  };
})();
