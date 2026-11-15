/* Batch registration UI with isolated API/camera mocks; no study data writes. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('../.browser-tools/node_modules/playwright');

const base = 'http://127.0.0.1:8001';
const output = path.resolve(__dirname, '../artifacts/batch-browser');
fs.mkdirSync(output, { recursive: true });
const config = { title: '一括登録の動作確認', consent_version: 'batch-test', consent_text: '検証用', max_upload_mb: 25,
  production_ready: true, pages: [1, 2, 3].map(number => ({ page_number: number, qr_code: `TEST-0${number}`, label: `${number}ページ`, required: true })) };

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'chrome' } : {}) });
  const cases = [], errors = [];
  async function scenario(mode, run) {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    await context.addInitScript(mode => {
      sessionStorage.setItem('handwriting_resume_token', 'batch-test-not-a-real-token');
      window.batchCamera = { requests: 0, shots: 0 };
      Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true, value: async () => {
        batchCamera.requests++;
        const canvas = document.createElement('canvas'); canvas.width = 960; canvas.height = 1280;
        const paint = () => { const ctx = canvas.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 960, 1280); };
        paint();
        const stream = canvas.captureStream(10), track = stream.getVideoTracks()[0];
        const timer = setInterval(paint, 100), stop = track.stop.bind(track);
        track.stop = () => { clearInterval(timer); stop(); };
        track.getCapabilities = () => ({ torch: false });
        batchCamera.track = track;
        return stream;
      } });
      window.ImageCapture = class {
        async getPhotoCapabilities() { return { fillLightMode: ['flash', 'off'] }; }
        async takePhoto() {
          batchCamera.shots++;
          const canvas = document.createElement('canvas'); canvas.width = 960; canvas.height = 1280;
          return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg'));
        }
      };
      if (mode === 'native-camera') Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { value: undefined });
    }, mode);
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (/Content Security|Refused|Uncaught/i.test(message.text())) errors.push(message.text()); });
    const images = await page.evaluate(() => [1, 2, 3].map(number => {
      const canvas = document.createElement('canvas'); canvas.width = 300; canvas.height = 420;
      const ctx = canvas.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 300, 420);
      ctx.fillStyle = '#16363c'; ctx.font = '24px sans-serif'; ctx.fillText('PAGE ' + number, 80, 80);
      for (const [x, y] of [[15, 15], [280, 15], [15, 400], [280, 400]]) { ctx.beginPath(); ctx.arc(x, y, 4, 0, 7); ctx.fill(); }
      for (let y = 120; y < 380; y += 30) { ctx.fillRect(30, y, 240, 1); }
      return canvas.toDataURL('image/jpeg').split(',')[1];
    }));
    const files = images.map((data, index) => ({ name: `page-${index + 1}.jpg`, mimeType: 'image/jpeg', buffer: Buffer.from(data, 'base64') }));
    const saved = new Map(), requests = [], attempts = new Map();
    let active = 0, maxActive = 0, expiredOnce = false;
    const photo = number => ({ page_number: number, qr_code: `TEST-0${number}`, version: 'v1', preview_url: `/api/participant/pages/TEST-0${number}/preview` });
    if (mode.startsWith('duplicate')) saved.set(1, photo(1));
    if (mode === 'submit-blocked') for (let i = 1; i <= 3; i++) saved.set(i, photo(i));
    const participant = () => ({ participant_id: 'PTEST', status: 'registered', config, pages: [...saved.values()],
      missing_pages: config.pages.filter(item => !saved.has(item.page_number)).map(item => item.qr_code),
      worksheet: { available: true, title: '検証用紙', is_test: true, filename: 'test.pdf' } });
    await context.route('**/api/**', async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      if (pathname === '/api/config') return route.fulfill({ json: config });
      if (pathname === '/api/participant') return route.fulfill({ json: participant() });
      if (pathname === '/api/register') return route.fulfill({ json: { participant_id: 'PNEW', resume_token: 'new-batch-token-' + 'x'.repeat(32) } });
      if (pathname.endsWith('/preview')) return mode === 'preview-error'
        ? route.fulfill({ status: 503, json: { detail: { message: 'プレビュー取得失敗' } } })
        : route.fulfill({ contentType: 'image/jpeg', body: files[0].buffer });
      if (pathname === '/api/participant/pages') {
        const body = request.postDataBuffer().toString('latin1');
        const name = body.match(/filename="([^"]+)"/)[1];
        const replace = /name="replace"\r\n\r\ntrue/.test(body);
        const number = name.startsWith('page-') ? Number(name.match(/page-(\d+)/)[1]) : requests.length + 1;
        requests.push({ name, replace });
        const attempt = (attempts.get(name) || 0) + 1; attempts.set(name, attempt);
        active++; maxActive = Math.max(maxActive, active);
        await new Promise(resolve => setTimeout(resolve, 20)); active--;
        if (mode === 'expiry' && !expiredOnce) { expiredOnce = true; return route.fulfill({ status: 410, json: { detail: { code: 'registration_expired', message: '期限切れです。新しく参加登録してください。' } } }); }
        if (mode === 'partial' && number === 2 && attempt === 1) return route.fulfill({ status: 422, json: { detail: { message: 'QRコードを確認してください。' } } });
        if (saved.has(number) && !replace) return route.fulfill({ status: 409, json: { detail: { code: 'duplicate_page', page_number: number, qr_code: `TEST-0${number}`, version: 'v1' } } });
        if (replace) assert.ok(/name="expected_version"\r\n\r\nv1/.test(body));
        const entry = photo(number); saved.set(number, entry);
        return route.fulfill({ json: { page: entry, participant: participant() } });
      }
      throw new Error('Unexpected API request: ' + pathname);
    });
    try {
      await page.goto(base); await page.locator('#participant-view').waitFor({ state: 'visible' });
      await run({ page, files, requests, saved, getMaxActive: () => maxActive });
      cases.push(mode);
    } finally { await context.close(); }
  }
  async function review(page) {
    await page.locator('#capture-next-dialog').waitFor({ state: 'visible' });
    await page.locator('#capture-next-review').click();
  }
  async function batchDone(page) {
    await page.waitForFunction(() => document.getElementById('batch-progress').hidden && document.getElementById('batch-status').textContent.includes('登録しました'));
  }
  try {
    await scenario('gallery', async ({ page, files, requests, getMaxActive }) => {
      assert.notEqual(await page.locator('#gallery-input').getAttribute('multiple'), null);
      await page.locator('#gallery-input').setInputFiles(files.slice(0, 1));
      await page.locator('#capture-next-dialog').waitFor({ state: 'visible' });
      assert.equal(requests.length, 0);
      const picker = page.waitForEvent('filechooser'); await page.locator('#capture-next-continue').click();
      const chooser = await picker; assert.equal(chooser.isMultiple(), true); await chooser.setFiles(files.slice(1));
      await review(page);
      assert.equal(await page.locator('#queue-list li').count(), 3);
      assert.equal(requests.length, 0, 'Selection must not upload before explicit batch registration');
      await page.locator('#queue-list img').first().waitFor();
      await page.screenshot({ path: path.join(output, 'queued-photos-mobile.png'), fullPage: true, mask: [page.locator('#resume-link')] });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      for (const width of [320, 844, 1200]) {
        await page.setViewportSize({ width, height: 844 });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        assert.ok(await page.locator('.queued-photo-preview img').first().evaluate(image => {
          const bounds = image.getBoundingClientRect(), parent = image.parentElement.getBoundingClientRect();
          return bounds.height > 0 && bounds.bottom <= parent.bottom && bounds.right <= parent.right;
        }), 'Thumbnails must stay inside their preview area');
      }
      await page.setViewportSize({ width: 390, height: 844 });
      await page.locator('.queued-photo-preview').first().click();
      await page.locator('#preview-dialog').waitFor({ state: 'visible' });
      assert.match(await page.locator('#dialog-image').getAttribute('src'), /^blob:/);
      await page.locator('#close-preview').click();
      await page.evaluate(() => { document.getElementById('batch-register').click(); document.getElementById('batch-register').click(); });
      await batchDone(page);
      assert.deepEqual(requests.map(item => item.name), files.map(file => file.name));
      assert.equal(getMaxActive(), 1);
      assert.equal(await page.locator('#queue-list [data-status="saved"]').count(), 3);
      assert.equal(await page.locator('#submit-button').isDisabled(), false);
    });
    await scenario('camera', async ({ page, requests }) => {
      await page.locator('#camera-button').click(); await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.locator('#camera-shutter').click();
      await page.locator('#capture-next-dialog').waitFor({ state: 'visible' }); assert.equal(requests.length, 0);
      await page.locator('#capture-next-continue').click();
      await page.locator('#camera-shutter:not([disabled])').waitFor(); await page.locator('#camera-shutter').click();
      await page.locator('#capture-next-dialog').waitFor({ state: 'visible' });
      assert.match(await page.locator('#capture-next-count').textContent(), /2枚/); assert.equal(requests.length, 0);
      assert.equal(await page.evaluate(() => batchCamera.requests), 1);
      await page.locator('#capture-next-register').click(); await batchDone(page);
      assert.equal(requests.length, 2);
    });
    await scenario('partial', async ({ page, files, requests }) => {
      await page.locator('#gallery-input').setInputFiles(files); await page.locator('#capture-next-register').click(); await batchDone(page);
      assert.equal(await page.locator('#queue-list [data-status="saved"]').count(), 2);
      assert.equal(await page.locator('#queue-list [data-status="failed"]').count(), 1);
      assert.match(await page.locator('#batch-status').textContent(), /1枚は未登録/);
      await page.locator('#batch-register').click(); await batchDone(page);
      assert.deepEqual(requests.map(item => item.name), ['page-1.jpg', 'page-2.jpg', 'page-3.jpg', 'page-2.jpg']);
      assert.equal(await page.locator('#queue-list [data-status="saved"]').count(), 3);
    });
    await scenario('mixed', async ({ page, files, requests }) => {
      await page.locator('#gallery-input').setInputFiles(files.slice(0, 1)); await review(page);
      await page.locator('#queue-camera').click(); await page.locator('#camera-shutter:not([disabled])').waitFor();
      await page.locator('#camera-shutter').click(); await review(page);
      const picker = page.waitForEvent('filechooser'); await page.locator('#queue-gallery').click();
      await (await picker).setFiles(files.slice(2)); await review(page);
      assert.equal(requests.length, 0);
      assert.equal(await page.locator('#queue-list li').count(), 3);
      await page.locator('#batch-register').click(); await batchDone(page);
      assert.equal(await page.locator('#queue-list [data-status="saved"]').count(), 3);
    });
    await scenario('native-camera', async ({ page, files, requests }) => {
      const picker = page.waitForEvent('filechooser'); await page.locator('#camera-button').click();
      const chooser = await picker; assert.equal(chooser.isMultiple(), false);
      await chooser.setFiles(files.slice(0, 1));
      await page.locator('#capture-next-dialog').waitFor({ state: 'visible' });
      const nextPicker = page.waitForEvent('filechooser'); await page.locator('#capture-next-continue').click();
      await (await nextPicker).setFiles(files.slice(1, 2));
      assert.equal(requests.length, 0);
      await page.locator('#capture-next-register').click(); await batchDone(page);
      assert.equal(requests.length, 2);
      assert.equal(await page.evaluate(() => batchCamera.requests), 0);
    });
    await scenario('oversize', async ({ page, files, requests }) => {
      await page.locator('#gallery-input').setInputFiles([files[0], { name: 'too-large.jpg', mimeType: 'image/jpeg', buffer: Buffer.alloc(26 * 1024 * 1024) }]);
      await review(page);
      assert.equal(await page.locator('#queue-list li').count(), 1);
      assert.match(await page.locator('#upload-error').textContent(), /too-large.jpg/);
      await page.locator('#batch-register').click(); await batchDone(page);
      assert.deepEqual(requests.map(item => item.name), ['page-1.jpg']);
    });
    for (const accept of [false, true]) await scenario(accept ? 'duplicate-accept' : 'duplicate-cancel', async ({ page, files, requests, saved }) => {
      await page.locator('#gallery-input').setInputFiles(files.slice(0, 2)); await page.locator('#capture-next-register').click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      await page.locator(accept ? '#confirm-accept' : '#confirm-cancel').click(); await batchDone(page);
      assert.equal(saved.size, 2);
      assert.equal(requests.length, accept ? 3 : 2);
      assert.equal(requests.some(item => item.replace), accept);
      assert.equal(await page.locator('#queue-list [data-status="skipped"]').count(), accept ? 0 : 1);
    });
    await scenario('preview-error', async ({ page, files, requests }) => {
      await page.locator('#gallery-input').setInputFiles(files); await page.locator('#capture-next-register').click(); await batchDone(page);
      assert.equal(await page.locator('#queue-list [data-status="saved"]').count(), 3);
      assert.equal(await page.locator('#batch-register').isDisabled(), true);
      assert.match(await page.locator('#upload-error').textContent(), /登録は完了/); assert.equal(requests.length, 3);
    });
    await scenario('remove', async ({ page, files, requests }) => {
      await page.locator('#gallery-input').setInputFiles(files.slice(0, 2)); await review(page);
      await page.getByRole('button', { name: 'page-1.jpgを一覧から外す', exact: true }).click();
      await page.locator('#batch-register').click(); await batchDone(page);
      assert.deepEqual(requests.map(item => item.name), ['page-2.jpg']);
    });
    await scenario('submit-blocked', async ({ page, files }) => {
      assert.equal(await page.locator('#submit-button').isDisabled(), false);
      await page.locator('#gallery-input').setInputFiles(files.slice(0, 1)); await review(page);
      assert.equal(await page.locator('#submit-button').isDisabled(), true);
      await page.getByRole('button', { name: 'page-1.jpgを一覧から外す', exact: true }).click();
      assert.equal(await page.locator('#submit-button').isDisabled(), false);
    });
    await scenario('expiry', async ({ page, files, requests }) => {
      await page.locator('#gallery-input').setInputFiles(files); await page.locator('#capture-next-register').click();
      await page.locator('#registration-view').waitFor({ state: 'visible' }); await batchDone(page);
      assert.equal(requests.length, 1, 'An expired participant must not send remaining queued photos');
      await page.locator('#consent-checkbox').check(); await page.locator('#register-button').click();
      await page.locator('#participant-view').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#queue-list li').count(), 3, 'Unregistered photos survive recovery in the current page');
      await page.locator('#batch-register').click(); await batchDone(page); assert.equal(requests.length, 4);
    });
    assert.deepEqual(errors, []);
    const result = { ok: true, cases, browserErrors: errors, studyDataWritten: false };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
