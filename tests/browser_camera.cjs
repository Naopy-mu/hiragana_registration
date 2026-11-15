/* Mock cameras in a real browser; never register or upload to the study DB. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('../.browser-tools/node_modules/playwright');

const base = 'http://127.0.0.1:8001';
const output = path.resolve(__dirname, '../artifacts/camera-browser');
fs.mkdirSync(output, { recursive: true });
const config = {
  title: 'カメラ動作確認', consent_version: 'camera-test', consent_text: '動作確認用',
  production_ready: true, pages: [{ page_number: 1, qr_code: 'TEST-01', label: '1ページ', required: true }]
};
const participant = {
  participant_id: 'PTEST', status: 'registered', pages: [], missing_pages: ['TEST-01'], config,
  worksheet: { available: true, title: '動作確認用紙', is_test: true, filename: 'test.pdf' }
};

async function checkFullscreen(page, width, height) {
  await page.setViewportSize({ width, height });
  await page.evaluate(({ width, height }) => {
    cameraTest.canvas.width = width > height ? 1280 : 960;
    cameraTest.canvas.height = width > height ? 960 : 1280;
  }, { width, height });
  await page.waitForFunction(({ width, height }) => {
    const video = document.getElementById('camera-video');
    return width > height ? video.videoWidth > video.videoHeight : video.videoHeight > video.videoWidth;
  }, { width, height });
  const layout = await page.evaluate(() => {
    const rectangle = id => {
      const rect = document.getElementById(id).getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, right: rect.right, bottom: rect.bottom };
    };
    return { width: innerWidth, height: innerHeight, dialog: rectangle('camera-dialog'), video: rectangle('camera-video'),
      shutter: rectangle('camera-shutter'), light: rectangle('camera-flash'),
      overflow: document.documentElement.scrollWidth > innerWidth };
  });
  assert.ok(Math.abs(layout.dialog.x) < 1 && Math.abs(layout.dialog.y) < 1, 'Camera must start at the viewport edge');
  assert.ok(Math.abs(layout.dialog.width - layout.width) < 1 && Math.abs(layout.dialog.height - layout.height) < 1, 'Camera must fill the viewport');
  assert.ok(Math.abs(layout.video.width - layout.width) < 1 && Math.abs(layout.video.height - layout.height) < 1, 'Preview must use the entire screen');
  assert.ok(Math.abs(layout.shutter.width - layout.shutter.height) < 1, 'Shutter must be round');
  assert.ok(Math.abs(layout.shutter.x + layout.shutter.width / 2 - layout.width / 2) < 1, 'Shutter must be centered');
  assert.ok(layout.light.x > layout.width - 100 && layout.light.y < 80, 'Flash must be at the top right');
  for (const element of [layout.shutter, layout.light]) {
    assert.ok(element.x >= 0 && element.y >= 0 && element.right <= layout.width + 1 && element.bottom <= layout.height + 1, 'Camera controls must remain visible');
  }
  assert.equal(layout.overflow, false);
  assert.equal(await page.locator('#camera-dialog button:visible').count(), 2);
  assert.equal(await page.locator('#camera-flash').evaluate(element => element.tagName), 'BUTTON');
  assert.equal(await page.locator('#camera-close, #camera-native, .camera-heading, .camera-help').count(), 0);
}

async function confirmFlashOff(page) {
  await page.locator('#camera-flash').click();
  await page.locator('#flash-confirm-dialog').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#camera-flash').getAttribute('aria-pressed'), 'true', 'Light stays on until confirmed');
  assert.equal(await page.locator('#camera-shutter').isDisabled(), true);
  await page.locator('#flash-confirm-off').click();
  await page.waitForFunction(() => document.getElementById('camera-flash').getAttribute('aria-pressed') === 'false');
  await page.locator('#camera-shutter:not([disabled])').waitFor();
}

async function registerCapture(page) {
  await page.locator('#capture-next-register').click();
  await page.waitForFunction(() => document.getElementById('batch-progress').hidden && document.querySelectorAll('.queued-photo[data-status="saved"]').length > 0);
}

async function waitPaused(page) {
  await page.waitForFunction(() => cameraTest.track.readyState === 'live' && cameraTest.track.enabled === false);
  await page.locator('#camera-button:not([disabled])').waitFor();
  await page.waitForFunction(() => !history.state?.worksheetCamera);
}

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'chrome' } : {}) });
  const errors = [];
  const cases = [];
  async function scenario(mode, run) {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    await context.addInitScript(mode => {
      sessionStorage.setItem('handwriting_resume_token', 'camera-test-not-a-real-credential');
      window.cameraTest = { mode, calls: [], stopped: 0, uploads: 0, resolveMedia: null };
      Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true, value: async constraints => {
        cameraTest.calls.push({ getUserMedia: constraints });
        if (cameraTest.mode === 'denied') throw new DOMException('Test permission rejection', 'NotAllowedError');
        const canvas = document.createElement('canvas');
        canvas.width = 960; canvas.height = 1280;
        cameraTest.canvas = canvas;
        const context = canvas.getContext('2d');
        const draw = () => { context.fillStyle = '#f5f4ed'; context.fillRect(0, 0, canvas.width, canvas.height); context.fillStyle = '#234'; context.fillText('TEST WORKSHEET', 100, 100); };
        draw();
        const stream = canvas.captureStream(10);
        const track = stream.getVideoTracks()[0];
        let torch = false;
        const timer = setInterval(draw, 100);
        const stop = track.stop.bind(track);
        track.stop = () => { clearInterval(timer); cameraTest.stopped++; stop(); };
        track.getCapabilities = () => ({ torch: mode.startsWith('torch') ? [false, true] : [false] });
        track.getSettings = () => ({ torch });
        track.applyConstraints = async options => {
          const enabled = options.advanced[0].torch;
          cameraTest.calls.push({ torch: enabled });
          if (mode === 'torch-error' && enabled) throw new DOMException('Light unavailable', 'OverconstrainedError');
          if (mode === 'torch-release-error' && !enabled) throw new DOMException('Light shutdown failed', 'OverconstrainedError');
          torch = enabled;
        };
        cameraTest.track = track;
        if (mode === 'delayed') await new Promise(resolve => { cameraTest.resolveMedia = resolve; });
        return stream;
      } });
      window.ImageCapture = class {
        async getPhotoCapabilities() { return { fillLightMode: mode.startsWith('flash') ? ['off', 'flash'] : [] }; }
        async takePhoto(options) {
          cameraTest.calls.push({ photo: options });
          if (mode === 'flash-error') throw new DOMException('Flash failed', 'UnknownError');
          const canvas = document.createElement('canvas'); canvas.width = 1280; canvas.height = 960;
          return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg'));
        }
      };
      if (mode.startsWith('torch')) window.ImageCapture = undefined;
    }, mode);
    let uploads = 0, submitted = false;
    const complete = { ...participant, pages: [{ page_number: 1, qr_code: 'TEST-01', version: 'test' }], missing_pages: [] };
    await context.route('**/api/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/api/config') return route.fulfill({ json: config });
      if (pathname === '/api/participant') return route.fulfill({ json: submitted ? { ...complete, status: 'submitted' } : (mode === 'flash-submit' && uploads ? complete : participant) });
      if (pathname === '/api/participant/submit' && mode === 'flash-submit') {
        submitted = true;
        return route.fulfill({ json: { status: 'submitted' } });
      }
      if (pathname === '/api/participant/pages') {
        uploads++;
        assert.match(route.request().headers()['content-type'], /^multipart\/form-data/);
        assert.ok(route.request().postDataBuffer().includes(Buffer.from('image/jpeg')));
        return route.fulfill({ json: { participant: mode === 'flash-submit' ? complete : participant } });
      }
      throw new Error('Unexpected API request: ' + pathname);
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (/Content Security|Refused|Uncaught/i.test(message.text())) errors.push(message.text()); });
    page.setDefaultTimeout(15000);
    try {
      await page.goto(base);
      await page.locator('#participant-view').waitFor({ state: 'visible' });
      await page.locator('#camera-button').click();
      if (mode !== 'denied') await page.locator('#camera-dialog').waitFor({ state: 'visible' });
      await run(page, () => uploads);
      cases.push(mode);
    } finally { await context.close(); }
  }

  try {
    await scenario('flash', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      assert.equal(await page.locator('#camera-flash').getAttribute('data-available'), 'true');
      await page.locator('#camera-flash').click();
      await page.locator('#flash-confirm-dialog').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#camera-flash').getAttribute('aria-pressed'), 'true');
      await page.evaluate(() => document.getElementById('camera-shutter').click());
      assert.equal(uploads(), 0, 'Cannot take a photo while confirmation is pending');
      await page.screenshot({ path: path.join(output, 'flash-off-confirmation.png'), mask: [page.locator('#resume-link')] });
      await page.locator('#flash-confirm-cancel').click();
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      assert.equal(await page.locator('#camera-flash').getAttribute('aria-pressed'), 'true');
      await page.locator('#camera-flash').click();
      await page.locator('#flash-confirm-dialog').waitFor({ state: 'visible' });
      await page.keyboard.press('Escape');
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      assert.equal(await page.locator('#camera-dialog').isVisible(), true);
      assert.equal(await page.locator('#camera-flash').getAttribute('aria-pressed'), 'true');
      await checkFullscreen(page, 390, 844);
      await page.screenshot({ path: path.join(output, 'mobile-flash.png'), mask: [page.locator('#resume-link')] });
      await checkFullscreen(page, 320, 568);
      await page.screenshot({ path: path.join(output, 'small-mobile-flash.png'), mask: [page.locator('#resume-link')] });
      await checkFullscreen(page, 844, 390);
      await page.screenshot({ path: path.join(output, 'landscape-flash.png'), mask: [page.locator('#resume-link')] });
      await checkFullscreen(page, 390, 650);
      await checkFullscreen(page, 390, 844);
      await page.evaluate(() => { document.getElementById('camera-shutter').click(); document.getElementById('camera-shutter').click(); });
      await registerCapture(page);
      assert.equal(uploads(), 1);
      assert.equal(await page.locator('#camera-dialog').isVisible(), false);
      const state = await page.evaluate(() => ({ calls: cameraTest.calls, stopped: cameraTest.stopped }));
      assert.deepEqual(state.calls.filter(call => call.photo), [{ photo: { fillLightMode: 'flash' } }]);
      assert.equal(state.stopped, 0);
      await waitPaused(page);
      await page.waitForFunction(() => !history.state?.worksheetCamera);
    });
    await scenario('flash-confirm-off', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await confirmFlashOff(page);
      await page.locator('#camera-shutter').click();
      await registerCapture(page);
      assert.equal(uploads(), 1);
      assert.deepEqual(await page.evaluate(() => cameraTest.calls.filter(call => call.photo)), [{ photo: { fillLightMode: 'off' } }]);
    });
    await scenario('flash-on-again', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await confirmFlashOff(page);
      await page.locator('#camera-flash').click();
      assert.equal(await page.locator('#camera-flash').getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('#flash-confirm-dialog').isVisible(), false);
      await page.locator('#camera-shutter').click();
      await registerCapture(page);
      assert.equal(uploads(), 1);
      assert.deepEqual(await page.evaluate(() => cameraTest.calls.filter(call => call.photo)), [{ photo: { fillLightMode: 'flash' } }]);
    });
    await scenario('torch', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      assert.equal(await page.locator('#camera-flash').getAttribute('data-available'), 'true');
      assert.match(await page.locator('#camera-flash').getAttribute('aria-label'), /ライト/);
      await page.locator('#camera-shutter').click();
      await registerCapture(page);
      assert.equal(uploads(), 1);
      assert.ok(await page.evaluate(() => cameraTest.calls.some(call => call.torch === true)));
      await waitPaused(page);
      assert.deepEqual(await page.evaluate(() => cameraTest.calls.filter(call => 'torch' in call)), [{ torch: true }, { torch: false }]);
    });
    await scenario('torch-confirm-off', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await confirmFlashOff(page);
      await page.locator('#camera-shutter').click();
      await registerCapture(page);
      assert.equal(uploads(), 1);
      assert.equal(await page.evaluate(() => cameraTest.calls.some(call => call.torch === true)), false);
      await waitPaused(page);
    });
    for (const mode of ['flash-error', 'torch-error', 'torch-release-error']) await scenario(mode, async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.locator('#camera-shutter').click();
      await page.locator('#upload-error').waitFor({ state: 'visible' });
      assert.equal(uploads(), 0, 'Failed lighting must not silently upload an unlit photo');
      assert.equal(await page.locator('#camera-dialog').isVisible(), false);
      assert.equal(await page.evaluate(() => cameraTest.track.readyState), 'ended');
    });
    await scenario('unsupported', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      assert.equal(await page.locator('#camera-flash').getAttribute('data-available'), 'false');
      await page.locator('#camera-shutter').click();
      await registerCapture(page);
      await waitPaused(page);
      assert.equal(uploads(), 1);
    });
    await scenario('denied', async (page, uploads) => {
      await page.locator('#upload-error').waitFor({ state: 'visible' });
      assert.match(await page.locator('#upload-error').textContent(), /許可されていません/);
      assert.equal(await page.locator('#camera-shutter').isDisabled(), true);
      assert.equal(await page.locator('#camera-dialog').isVisible(), false);
      assert.equal(uploads(), 0);
    });
    await scenario('delayed', async (page, uploads) => {
      await page.waitForFunction(() => Boolean(cameraTest.resolveMedia));
      await page.goBack();
      await page.evaluate(() => cameraTest.resolveMedia());
      await page.waitForFunction(() => cameraTest.track.readyState === 'ended');
      assert.equal(uploads(), 0);
      assert.equal(await page.locator('#camera-dialog').isVisible(), false);
    });
    await scenario('flash-cancel-confirmation', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.locator('#camera-flash').click();
      await page.locator('#flash-confirm-dialog').waitFor({ state: 'visible' });
      await page.goBack();
      await page.locator('#camera-dialog').waitFor({ state: 'hidden' });
      assert.equal(await page.locator('#flash-confirm-dialog').isVisible(), false);
      assert.equal(await page.evaluate(() => cameraTest.track.readyState), 'ended');
      await page.locator('#camera-button').click();
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      assert.equal(await page.locator('#camera-flash').getAttribute('aria-pressed'), 'true');
      assert.equal(uploads(), 0);
      await page.keyboard.press('Escape');
    });
    await scenario('cancel', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !history.state?.worksheetCamera);
      assert.equal(await page.evaluate(() => cameraTest.track.readyState), 'ended');
      await page.locator('#camera-button').click();
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
      assert.equal(await page.evaluate(() => cameraTest.track.readyState), 'ended');
      assert.equal(uploads(), 0);
    });
    await scenario('flash-reuse', async (page, uploads) => {
      for (let shot = 0; shot < 2; shot++) {
        await page.locator('#camera-shutter:not([disabled])').waitFor();
        assert.equal(await page.evaluate(() => cameraTest.track.enabled), true);
        await page.locator('#camera-shutter').click();
        await registerCapture(page);
        await waitPaused(page);
        assert.equal(uploads(), shot + 1);
        if (shot === 0) await page.locator('#camera-button').click();
      }
      assert.equal(await page.evaluate(() => cameraTest.calls.filter(call => call.getUserMedia).length), 1, 'Consecutive photos must reuse one camera permission request');
      await page.locator('#camera-button').click();
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.keyboard.press('Escape');
      assert.equal(await page.evaluate(() => cameraTest.track.readyState), 'ended');
    });
    await scenario('flash-cache-idle', async (page, uploads) => {
      await page.clock.install();
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.locator('#camera-shutter').click();
      await registerCapture(page);
      await waitPaused(page);
      await page.clock.fastForward(5 * 60 * 1000 + 100);
      assert.equal(await page.evaluate(() => cameraTest.track.readyState), 'ended', 'Unused camera access must be released after five minutes');
      assert.equal(uploads(), 1);
    });
    await scenario('flash-cache-hidden', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.locator('#camera-shutter').click();
      await registerCapture(page);
      await waitPaused(page);
      await page.evaluate(() => {
        Object.defineProperty(document, 'hidden', { configurable: true, value: true });
        document.dispatchEvent(new Event('visibilitychange'));
      });
      assert.equal(await page.evaluate(() => cameraTest.track.readyState), 'ended');
      await page.evaluate(() => { delete document.hidden; });
      await page.locator('#camera-button').click();
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      assert.equal(await page.evaluate(() => cameraTest.calls.filter(call => call.getUserMedia).length), 2, 'Returning after a hidden page obtains a fresh camera stream');
      assert.equal(uploads(), 1);
      await page.keyboard.press('Escape');
    });
    await scenario('flash-cache-ended', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.locator('#camera-shutter').click();
      await registerCapture(page);
      await waitPaused(page);
      await page.evaluate(() => cameraTest.track.stop());
      await page.locator('#camera-button').click();
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      assert.equal(await page.evaluate(() => cameraTest.calls.filter(call => call.getUserMedia).length), 2, 'An ended track must never be reused');
      assert.equal(uploads(), 1);
      await page.keyboard.press('Escape');
    });
    await scenario('flash-submit', async (page, uploads) => {
      await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.locator('#camera-shutter').click();
      await registerCapture(page);
      await page.locator('#submit-button:not([disabled])').waitFor();
      assert.equal(await page.evaluate(() => cameraTest.track.readyState), 'live');
      assert.equal(await page.evaluate(() => cameraTest.track.enabled), false);
      await page.locator('#submit-button').click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      await page.locator('#confirm-accept').click();
      await page.locator('#submitted-panel').waitFor({ state: 'visible' });
      assert.equal(await page.evaluate(() => cameraTest.track.readyState), 'ended', 'Submitting must release cached camera access');
      assert.equal(uploads(), 1);
    });
    assert.deepEqual(errors, []);
    const result = { ok: true, cases, fullscreenViewports: ['390x844', '320x568', '844x390', '390x650'], browserErrors: errors, realCameraUsed: false, studyDataWritten: false };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
