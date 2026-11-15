"use strict";
(() => {
  const $ = id => document.getElementById(id);

  // The native file picker exposes no flash controls. This camera is optional:
  // the native camera remains available when a browser cannot control its light.
  window.WorksheetCamera = class {
    constructor({ onError = () => {} } = {}) {
      this.session = null;
      this.onError = onError;
      this.historyClosing = false;
      this.historySequence = 0;
      this.cachedStream = null;
      this.cacheTimer = null;
      this.dialog = $("camera-dialog");
      this.video = $("camera-video");
      this.flashConfirmation = $("flash-confirm-dialog");
      this.dialog.addEventListener("cancel", event => { event.preventDefault(); this.stop(); });
      this.dialog.addEventListener("close", () => { if (!this.dialog.open && this.session) this.stop(); });
      $("camera-shutter").addEventListener("click", () => this.capture());
      $("camera-flash").addEventListener("click", () => this.toggleFlash());
      $("flash-confirm-cancel").addEventListener("click", () => this.flashConfirmation.close("cancel"));
      $("flash-confirm-off").addEventListener("click", () => this.flashConfirmation.close("off"));
      this.flashConfirmation.addEventListener("cancel", event => { event.preventDefault(); this.flashConfirmation.close("cancel"); });
      window.addEventListener("popstate", () => {
        const ownHistoryChange = this.historyClosing;
        this.historyClosing = false;
        if (this.session && history.state?.worksheetCamera !== this.session.historyKey) this.stop(true);
        else if (!this.session && !ownHistoryChange) this.discardCachedStream();
      });
      window.addEventListener("pagehide", () => this.stop(true));
      document.addEventListener("visibilitychange", () => { if (document.hidden) this.stop(); });
    }

    get supported() { return window.isSecureContext && Boolean(navigator.mediaDevices?.getUserMedia); }

    open() {
      if (this.session || this.historyClosing) return Promise.resolve(null);
      return new Promise(resolve => {
        const session = { resolve, stream: null, track: null, photo: null, modes: [], light: null, flash: false, confirming: false, busy: false, historyKey: null };
        this.session = session;
        $("camera-shutter").disabled = true;
        this.setFlash(false);
        $("camera-flash").setAttribute("aria-label", "フラッシュの対応を確認しています");
        this.status("カメラの使用を許可してください。");
        this.dialog.showModal();
        try {
          session.historyKey = `camera-${Date.now()}-${++this.historySequence}`;
          history.pushState({ ...history.state, worksheetCamera: session.historyKey }, "", location.href);
        } catch { session.historyKey = null; }
        this.start(session);
      });
    }

    status(text, error = false) {
      $("camera-status").textContent = text;
      $("camera-status").classList.toggle("error", error);
      if (error) { this.stop(); this.onError(text); }
    }

    setFlash(enabled) {
      if (this.session) this.session.flash = enabled;
      const indicator = $("camera-flash");
      const available = Boolean(this.session?.light);
      const label = this.session?.light === "torch" ? "撮影ライト" : "フラッシュ";
      const text = available ? `${label}：${enabled ? "オン（オフにするには確認が必要です）" : "オフ（押すとオン）"}` : "この端末ではフラッシュを操作できません";
      indicator.dataset.available = String(available);
      indicator.setAttribute("aria-pressed", String(enabled));
      indicator.setAttribute("aria-label", text);
      indicator.title = text;
    }

    async toggleFlash() {
      const session = this.session;
      if (!session?.light || session.busy || session.confirming) return;
      if (!session.flash) { this.setFlash(true); this.status("フラッシュをオンにしました。"); return; }
      session.confirming = true;
      $("camera-shutter").disabled = true;
      $("camera-flash").disabled = true;
      try {
        this.flashConfirmation.returnValue = "cancel";
        this.flashConfirmation.showModal();
        const accepted = await new Promise(resolve => this.flashConfirmation.addEventListener("close", () => resolve(this.flashConfirmation.returnValue === "off"), { once: true }));
        if (this.session === session && accepted) {
          this.setFlash(false);
          this.status("フラッシュをオフにしました。");
        }
      } catch { /* Keep the light on if the confirmation cannot be shown. */ }
      finally {
        session.confirming = false;
        if (this.session === session && session.track?.readyState === "live") {
          $("camera-shutter").disabled = false;
          $("camera-flash").disabled = false;
        }
      }
    }

    release(session) {
      if (session?.onTrackEnded) session.track.removeEventListener("ended", session.onTrackEnded);
      if (session?.stream) {
        session.stream.getTracks().forEach(track => { if (track.readyState !== "ended") track.stop(); });
        session.stream = null;
      }
      this.video.pause();
      this.video.srcObject = null;
    }

    discardCachedStream() {
      window.clearTimeout(this.cacheTimer);
      this.cacheTimer = null;
      if (this.cachedStream) {
        this.cachedStream.getTracks().forEach(track => { if (track.readyState !== "ended") track.stop(); });
        this.cachedStream = null;
      }
    }

    reuseStream() {
      const stream = this.cachedStream;
      window.clearTimeout(this.cacheTimer);
      this.cacheTimer = null;
      this.cachedStream = null;
      if (!stream) return null;
      if (!stream.getVideoTracks().some(track => track.readyState === "live")) {
        stream.getTracks().forEach(track => track.stop());
        return null;
      }
      stream.getTracks().forEach(track => { track.enabled = true; });
      return stream;
    }

    stop(historyMoved = false) {
      this.discardCachedStream();
      this.finish(null, historyMoved);
    }

    finish(file, historyMoved = false) {
      const session = this.session;
      if (!session) return;
      this.session = null;
      if (this.flashConfirmation.open) this.flashConfirmation.close("cancel");
      if (file && session.stream && session.track?.readyState === "live" && !document.hidden) {
        // Pause between photos without repeating getUserMedia's permission flow.
        // No frames are displayed or uploaded while the track is disabled.
        this.discardCachedStream();
        if (session.onTrackEnded) session.track.removeEventListener("ended", session.onTrackEnded);
        session.stream.getTracks().forEach(track => { track.enabled = false; });
        this.cachedStream = session.stream;
        session.stream = null;
        this.video.pause();
        this.video.srcObject = null;
        this.cacheTimer = window.setTimeout(() => this.discardCachedStream(), 5 * 60 * 1000);
      } else this.release(session);
      if (this.dialog.open) this.dialog.close();
      if (!historyMoved && session.historyKey && history.state?.worksheetCamera === session.historyKey) {
        this.historyClosing = true;
        history.back();
      }
      session.resolve(file);
    }

    async start(session) {
      try {
        const stream = this.reuseStream() || await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: { facingMode: { ideal: "environment" }, width: { ideal: 3840 }, height: { ideal: 2160 } }
        });
        // A permission prompt can resolve after the person closes the dialog.
        if (this.session !== session) { stream.getTracks().forEach(track => track.stop()); return; }
        session.stream = stream;
        session.track = stream.getVideoTracks()[0];
        if (!session.track) throw new Error("missing_video");
        session.onTrackEnded = () => {
          if (this.session !== session) return;
          this.release(session);
          $("camera-shutter").disabled = true;
          this.status("カメラが停止しました。もう一度カメラを開いてください。", true);
        };
        session.track.addEventListener("ended", session.onTrackEnded, { once: true });
        this.video.srcObject = stream;
        await this.video.play();
        if (this.session !== session) return;
        if (typeof window.ImageCapture === "function") {
          try {
            session.photo = new ImageCapture(session.track);
            const capabilities = await session.photo.getPhotoCapabilities();
            session.modes = capabilities.fillLightMode || [];
          } catch { /* A video stream can work without the still-photo API. */ }
        }
        if (this.session !== session) return;
        let torch = false;
        try {
          const capability = session.track.getCapabilities?.().torch;
          torch = capability === true || (Array.isArray(capability) && capability.includes(true));
        } catch { /* Some browsers do not expose track capabilities. */ }
        session.light = session.modes.includes("flash") ? "flash" : torch ? "torch" : null;
        this.setFlash(Boolean(session.light));
        $("camera-flash").disabled = !session.light;
        if (session.light === "flash") {
          this.status("撮影時にフラッシュが光ります。用紙の四隅とQRを画面に入れてください。");
        } else if (session.light === "torch") {
          this.status("撮影する瞬間にライトを点灯します。用紙の四隅とQRを画面に入れてください。");
        } else {
          this.status("この端末ではフラッシュを操作できません。ライトなしで撮影できます。");
        }
        $("camera-shutter").disabled = false;
      } catch (error) {
        if (this.session !== session) return;
        this.release(session);
        const denied = error.name === "NotAllowedError" || error.name === "SecurityError";
        this.status(denied ? "カメラの使用が許可されていません。ブラウザのカメラ設定を確認してください。撮影済み画像を選ぶこともできます。" : "カメラを起動できませんでした。もう一度カメラを開くか、撮影済み画像を選んでください。", true);
      }
    }

    async jpeg(source, width, height) {
      if (Math.min(width, height) < 700) throw new Error("low_resolution");
      // Bound full-resolution still photos to the server's pixel/size limits.
      const scale = Math.min(1, 4096 / Math.max(width, height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);
      const context = canvas.getContext("2d");
      if (!context) throw new Error("capture_unavailable");
      context.drawImage(source, 0, 0, canvas.width, canvas.height);
      return new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("capture_failed")), "image/jpeg", 0.95));
    }

    async photoJpeg(blob) {
      const url = URL.createObjectURL(blob);
      try {
        const image = new Image();
        await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = reject; image.src = url; });
        return await this.jpeg(image, image.naturalWidth, image.naturalHeight);
      } finally { URL.revokeObjectURL(url); }
    }

    async capture() {
      const session = this.session;
      if (!session?.track || session.busy || session.confirming || $("camera-shutter").disabled) return;
      session.busy = true;
      $("camera-shutter").disabled = true;
      $("camera-flash").disabled = true;
      const useLight = Boolean(session.light) && session.flash;
      let torchOn = false;
      this.status("撮影しています。そのまま動かさずにお待ちください。");
      try {
        if (useLight && session.light === "torch") {
          torchOn = true;
          await session.track.applyConstraints({ advanced: [{ torch: true }] });
          if (session.track.getSettings?.().torch === false) throw new Error("light_failed");
          await new Promise(resolve => window.setTimeout(resolve, 300));
        }
        if (this.session !== session) return;
        let blob;
        if (session.photo && session.light === "flash" && (useLight || session.modes.includes("off"))) {
          // Do not silently retry an unsuccessful flash shot without its light.
          blob = await this.photoJpeg(await session.photo.takePhoto({ fillLightMode: useLight ? "flash" : "off" }));
        } else {
          blob = await this.jpeg(this.video, this.video.videoWidth, this.video.videoHeight);
        }
        if (this.session !== session) return;
        // Switch the lamp off before retaining a paused stream for the next shot.
        if (torchOn && session.track.readyState === "live") {
          await session.track.applyConstraints({ advanced: [{ torch: false }] });
          torchOn = false;
        }
        if (this.session !== session) return;
        this.finish(new File([blob], `worksheet-${Date.now()}.jpg`, { type: "image/jpeg" }));
      } catch (error) {
        if (this.session === session) {
          this.status(error.message === "low_resolution" ? "写真の解像度が不足しています。スマホのカメラアプリで撮影し、撮影済み画像から選んでください。" : "撮影できませんでした。もう一度カメラを開いて撮影してください。", true);
        }
      } finally {
        if (torchOn && session.track.readyState === "live") {
          try { await session.track.applyConstraints({ advanced: [{ torch: false }] }); }
          catch {
            // Stopping the stream also extinguishes a light that cannot be reset.
            if (this.session === session) {
              this.release(session);
              this.status("ライトを停止するためカメラを閉じました。もう一度カメラを開いてください。", true);
            }
          }
        }
        session.busy = false;
        if (this.session === session && session.track.readyState === "live") {
          $("camera-shutter").disabled = false;
          $("camera-flash").disabled = !session.light;
        }
      }
    }
  };
})();
