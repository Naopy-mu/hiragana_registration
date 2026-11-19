/* Submission feedback beside the selected PDF; all APIs are local browser mocks. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('../.browser-tools/node_modules/playwright');

const base = process.env.BROWSER_BASE_URL || 'http://127.0.0.1:8004';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const output = path.resolve(__dirname, '../artifacts/submission-feedback-browser');
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'chrome' } : {}) });
  const cases = [], errors = [];
  async function scenario(mode, run) {
    const single = mode.startsWith('single');
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    await context.addInitScript(() => sessionStorage.setItem('handwriting_resume_token', 'local-feedback-test-not-a-real-token'));
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    page.on('pageerror', error => errors.push(error.message));
    const mainCodes = Array.from({ length: 128 }, (_, index) => `MAIN-${index + 1}`);
    const pendingCodes = mainCodes.slice(14, 29);
    const config = { title: '提出操作の検証', consent_version: 'feedback-test', consent_text: '検証用', production_ready: true,
      pages: [...mainCodes, ...(single ? [] : Array.from({ length: 8 }, (_, index) => `OTHER-${index + 1}`))].map((code, index) =>
        ({ qr_code: code, page_number: index + 1, label: `${index + 1}ページ`, required: true })) };
    let submitted = false, postCount = 0, releasePost, releaseCheck;
    const postGate = new Promise(resolve => { releasePost = resolve; });
    const checkGate = new Promise(resolve => { releaseCheck = resolve; });
    const participant = () => ({ participant_id: 'PTEST', status: single ? 'submitted' : 'partially_submitted', config,
      worksheet: { available: true, title: '検証用紙', is_test: true, filename: 'main.pdf' },
      worksheets: [{ id: 'main', number: 1, number_label: '①', title: '128ページ用紙', page_count: 128 },
        ...(single ? [] : [{ id: 'other', number: 2, number_label: '②', title: '別の用紙', page_count: 8 }])],
      pages: mainCodes.slice(0, 29).map((code, index) => ({ qr_code: code, page_number: index + 1, version: 'v1' })),
      has_pending_changes: !submitted, pending_page_count: submitted ? 0 : 15,
      worksheet_submissions: [{ id: 'main', number: 1, number_label: '①', title: '128ページ用紙', page_count: 128,
        status: 'submitted', registered_pages: 29, missing_page_count: 99,
        blank_pages: submitted ? 99 : 114, blank_qr_codes: mainCodes.slice(submitted ? 29 : 14),
        has_pending_changes: !submitted, pending_page_count: submitted ? 0 : 15, pending_qr_codes: submitted ? [] : pendingCodes,
        submit_url: '/api/participant/worksheets/main/submit', pdf_url: '/api/participant/worksheets/main/submission' },
        ...(single ? [] : [{ id: 'other', number: 2, number_label: '②', title: '別の用紙', page_count: 8, status: 'registered',
          registered_pages: 0, missing_page_count: 8, blank_pages: 0, blank_qr_codes: [], has_pending_changes: false,
          pending_page_count: 0, pending_qr_codes: [], submit_url: '/api/participant/worksheets/other/submit' }])],
    });
    await context.route('**/api/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/api/config') return route.fulfill({ json: config });
      if (pathname === '/api/participant') {
        if (mode === 'checking-success' && postCount) await checkGate;
        return route.fulfill({ json: participant() });
      }
      if (pathname === '/api/participant/submit' || pathname === '/api/participant/worksheets/main/submit') {
        postCount++;
        if (postCount === 1 && mode !== 'noop') await postGate;
        if (postCount === 1 && mode.includes('failure')) return route.fulfill({ status: 503, json: { detail: { message: '提出PDFを保存できませんでした。登録済み写真は保持されています。' } } });
        if (mode !== 'noop') submitted = true;
        return route.fulfill({ json: { participant_id: 'PTEST', status: 'submitted' } });
      }
      throw new Error('Unexpected API request: ' + pathname);
    });
    const row = page.locator('[data-worksheet-id="main"]');
    const button = single ? page.locator('#submit-button') : row.locator('.worksheet-submit');
    const feedback = single ? page.locator('#submit-feedback') : row.locator('.submission-feedback');
    try {
      await page.goto(base + '/static/index.html');
      await page.locator('#participant-view').waitFor({ state: 'visible' });
      await button.scrollIntoViewIfNeeded();
      await run({ page, row, button, feedback, getPostCount: () => postCount, releasePost, releaseCheck, single });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      cases.push(mode);
    } finally {
      releasePost(); releaseCheck(); await context.close();
    }
  }
  try {
    await scenario('cancel', async ({ page, button, feedback, getPostCount }) => {
      await button.click(); await page.locator('#confirm-cancel').click();
      assert.equal(getPostCount(), 0);
      assert.equal(await button.isEnabled(), true);
      assert.equal(await feedback.isHidden(), true);
      assert.match(await button.textContent(), /追加分を提出する/);
    });
    for (const mode of ['failure-retry', 'single-failure-retry']) await scenario(mode, async ({ page, row, button, feedback, releasePost, getPostCount, single }) => {
      await button.click(); await page.locator('#confirm-accept').click();
      await feedback.waitFor({ state: 'visible' });
      assert.match(await button.textContent(), /PDFを作成・保存しています/);
      assert.equal(await button.isDisabled(), true);
      assert.match(await feedback.textContent(), /完了するまで/);
      assert.doesNotMatch(await feedback.textContent(), /提出が完了/);
      if (!single) {
        assert.equal(await row.getAttribute('aria-busy'), 'true');
        assert.equal(await page.locator('[data-worksheet-id="other"] .worksheet-submit').isDisabled(), true);
        await page.screenshot({ path: path.join(output, 'mobile-saving-128-pages.png'), fullPage: false });
      }
      releasePost();
      await feedback.waitFor({ state: 'visible' });
      await page.waitForFunction(single => document.querySelector(single ? '#submit-feedback' : '[data-worksheet-id="main"] .submission-feedback').dataset.state === 'error', single);
      await button.waitFor({ state: 'visible' });
      await page.waitForFunction(single => !document.querySelector(single ? '#submit-button' : '[data-worksheet-id="main"] .worksheet-submit').disabled, single);
      assert.match(await feedback.textContent(), /保存できませんでした/);
      await page.waitForFunction(single => {
        const bounds = document.querySelector(single ? '#submit-feedback' : '[data-worksheet-id="main"] .submission-feedback').getBoundingClientRect();
        return bounds.top < innerHeight && bounds.bottom > 0;
      }, single);
      assert.match(await button.textContent(), /追加分を提出する/);
      await button.click(); await page.locator('#confirm-accept').click();
      await page.waitForFunction(single => document.querySelector(single ? '#submit-feedback' : '[data-worksheet-id="main"] .submission-feedback').dataset.state === 'success', single);
      assert.equal(getPostCount(), 2);
      assert.match(await feedback.textContent(), /以前の提出PDFを更新しました/);
      if (!single) assert.equal(await row.locator('.worksheet-submit').count(), 0);
    });
    await scenario('checking-success', async ({ page, button, feedback, releasePost, releaseCheck }) => {
      await button.click(); await page.locator('#confirm-accept').click();
      await page.waitForFunction(() => document.querySelector('[data-worksheet-id="main"] .submission-feedback').dataset.state === 'saving');
      releasePost();
      await page.waitForFunction(() => document.querySelector('[data-worksheet-id="main"] .submission-feedback').dataset.state === 'checking');
      assert.match(await button.textContent(), /提出内容を確認しています/);
      assert.doesNotMatch(await feedback.textContent(), /提出が完了/);
      releaseCheck();
      await page.waitForFunction(() => document.querySelector('[data-worksheet-id="main"] .submission-feedback').dataset.state === 'success');
      assert.match(await feedback.textContent(), /以前の提出PDFを更新しました/);
    });
    await scenario('noop', async ({ page, button, feedback }) => {
      await button.click(); await page.locator('#confirm-accept').click();
      await page.waitForFunction(() => document.querySelector('[data-worksheet-id="main"] .submission-feedback').dataset.state === 'error');
      assert.match(await feedback.textContent(), /提出PDFに反映されていません/);
      assert.doesNotMatch(await page.locator('#app-status').textContent(), /提出が完了/);
      assert.equal(await button.isEnabled(), true);
    });
    assert.deepEqual(errors, []);
    const result = { ok: true, cases, browserErrors: errors, pageCount: 128, pendingPhotos: 15, studyDataWritten: false };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
