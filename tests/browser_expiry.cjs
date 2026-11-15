/* Expired-ID recovery with mock API responses; no real registrations. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('../.browser-tools/node_modules/playwright');

const base = 'http://127.0.0.1:8001';
const oldKey = '11111111-1111-4111-8111-111111111111';
const oldToken = 'expired-test-bearer-' + 'x'.repeat(30);
const newToken = 'new-test-bearer-' + 'y'.repeat(30);
const expired = { detail: { code: 'registration_expired', message: '写真を登録しないまま24時間が経過したため削除されました。新しく参加登録してください。' } };
const config = { title: '期限切れの動作確認', consent_version: 'expiry-test', consent_text: '検証用', production_ready: true,
  pages: [{ page_number: 1, qr_code: 'TEST-01', label: '1ページ', required: true }] };
const participant = { participant_id: 'P002', status: 'registered', pages: [], missing_pages: ['TEST-01'], config,
  worksheet: { available: true, title: '検証用紙', filename: 'test.pdf', is_test: true } };
const output = path.resolve(__dirname, '../artifacts/expiry-browser');
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'chrome' } : {}) });
  const cases = [], errors = [];
  async function scenario(mode) {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    await context.addInitScript(({ oldKey, oldToken, mode }) => {
      localStorage.setItem('handwriting_registration_key', oldKey);
      if (mode !== 'registration-key') sessionStorage.setItem('handwriting_resume_token', oldToken);
    }, { oldKey, oldToken, mode });
    const requests = [];
    await context.route('**/api/**', async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      if (pathname === '/api/config') return route.fulfill({ json: config });
      if (pathname === '/api/participant') {
        const token = request.headers().authorization;
        if (token === 'Bearer ' + newToken) return route.fulfill({ json: participant });
        if (mode === 'upload') return route.fulfill({ json: { ...participant, participant_id: 'P001' } });
        return route.fulfill({ status: mode === 'invalid' ? 401 : 410,
          json: mode === 'invalid' ? { detail: { code: 'unauthorized', message: 'リンクを確認してください。' } } : expired });
      }
      if (pathname === '/api/register') {
        const body = request.postDataJSON(); requests.push(body);
        if (body.idempotency_key === oldKey) return route.fulfill({ status: 410, json: expired });
        return route.fulfill({ json: { participant_id: 'P002', resume_token: newToken } });
      }
      if (pathname === '/api/participant/pages') return route.fulfill({ status: 410, json: expired });
      throw new Error('Unexpected API request: ' + pathname);
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    page.on('pageerror', error => errors.push(error.message));
    try {
      await page.goto(base);
      if (mode === 'upload') {
        await page.locator('#participant-view').waitFor({ state: 'visible' });
        await page.locator('#gallery-input').setInputFiles({ name: 'test.jpg', mimeType: 'image/jpeg', buffer: Buffer.from('mock photo') });
        await page.locator('#capture-next-register').click();
      }
      await page.locator('#registration-view').waitFor({ state: 'visible' });
      if (mode === 'invalid') {
        assert.equal(await page.locator('#consent-checkbox').isDisabled(), true);
        assert.equal(requests.length, 0);
      } else {
        if (mode === 'registration-key') {
          await page.locator('#consent-checkbox').check();
          await page.locator('#register-button').click();
        }
        await page.locator('#app-error').waitFor({ state: 'visible' });
        await page.waitForFunction(() => document.getElementById('consent-checkbox').checked === false);
        assert.equal(await page.locator('#consent-checkbox').isDisabled(), false);
        assert.equal(await page.locator('#register-button').isDisabled(), true);
        assert.equal(await page.evaluate(() => sessionStorage.getItem('handwriting_resume_token')), null);
        assert.equal(await page.evaluate(() => localStorage.getItem('handwriting_registration_key')), null);
        assert.equal(await page.locator('#resume-link').inputValue(), '');
        if (mode === 'link') await page.screenshot({ path: path.join(output, 'expired-link-mobile.png'), fullPage: true });
        await page.locator('#consent-checkbox').check();
        await page.locator('#register-button').click();
        await page.locator('#participant-view').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#participant-id').textContent(), 'P002');
        assert.notEqual(requests.at(-1).idempotency_key, oldKey);
        assert.equal(await page.evaluate(() => sessionStorage.getItem('handwriting_resume_token')), newToken);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      }
      cases.push(mode);
    } finally { await context.close(); }
  }
  try {
    for (const mode of ['link', 'registration-key', 'upload', 'invalid']) await scenario(mode);
    assert.deepEqual(errors, []);
    const result = { ok: true, cases, browserErrors: errors, studyDataWritten: false };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
