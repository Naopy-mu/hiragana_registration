/* Researcher worksheet replacement and participant revision pinning in Chrome. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('../.browser-tools/node_modules/playwright');

const root = path.resolve(__dirname, '..');
const base = process.env.BROWSER_BASE_URL || 'http://127.0.0.1:8000';
const output = path.join(root, 'artifacts', 'revision-browser');
const originalPath = path.join(root, 'resources', 'worksheets', 'hiragana_collection_a4.pdf');
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'chrome' } : {}) });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(120000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', msg => { if (msg.type() === 'error' && /Content Security|Refused|Uncaught/i.test(msg.text())) errors.push(msg.text()); });
  const original = fs.readFileSync(originalPath);
  try {
    assert.equal((await context.request.get(base + '/api/researcher/worksheet')).status(), 401);
    assert.equal((await context.request.get(base + '/api/researcher/worksheet/pdf')).status(), 401);
    await page.goto(base);
    await page.locator('#registration-view').waitFor({ state: 'visible' });
    await page.locator('#consent-checkbox').check();
    await page.locator('#register-button').click();
    await page.locator('#participant-view').waitFor({ state: 'visible' });
    const beforeParticipant = await page.locator('#participant-id').textContent();
    const beforeLink = await page.locator('#resume-link').inputValue();
    const beforeToken = beforeLink.split('#')[1];
    const beforeHeaders = { Authorization: 'Bearer ' + beforeToken };
    const beforeData = await (await context.request.get(base + '/api/participant', { headers: beforeHeaders })).json();
    const beforePdf = await (await context.request.get(base + '/api/participant/worksheet', { headers: beforeHeaders })).body();

    await page.goto(base + '/researcher');
    await page.locator('#password-input').fill(fs.readFileSync(path.join(root, 'data', 'local-researcher-password.txt'), 'utf8').trim());
    await page.locator('#login-button').click();
    await page.locator('#researcher-content').waitFor({ state: 'visible' });
    await page.locator('#download-current-worksheet:not([disabled])').waitFor();
    const currentDownload = page.waitForEvent('download');
    await page.locator('#download-current-worksheet').click();
    const currentPdf = await currentDownload;
    const currentPath = path.join(output, 'current-worksheet.pdf');
    await currentPdf.saveAs(currentPath);
    assert.ok(fs.readFileSync(currentPath).equals(original), 'The supplied original must be distributed unchanged');

    await page.locator('#revision-file').setInputFiles(originalPath);
    await page.locator('#inspect-worksheet').click();
    await page.locator('#worksheet-draft').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#draft-worksheet-pages li').count(), 8);
    assert.match(await page.locator('#draft-worksheet-count').textContent(), /8/);
    const previewDownload = page.waitForEvent('download');
    await page.locator('#preview-draft-worksheet').click();
    const preview = await previewDownload;
    const previewPath = path.join(output, 'verified-draft.pdf');
    await preview.saveAs(previewPath);
    assert.ok(fs.readFileSync(previewPath).equals(original));
    await page.locator('#preview-draft-worksheet:not([disabled])').waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'Mobile management layout overflow');
    await page.screenshot({ path: path.join(output, 'mobile-draft.png'), fullPage: true });
    await page.locator('#activate-worksheet').click();
    await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
    assert.match(await page.locator('#confirm-description').textContent(), /登録済み/);
    await page.locator('#confirm-accept').click();
    await page.locator('#worksheet-draft').waitFor({ state: 'hidden' });
    await page.locator('#revision-message').waitFor({ state: 'visible' });
    assert.match(await page.locator('#revision-message').textContent(), /差し替えました/);
    const current = await (await context.request.get(base + '/api/researcher/worksheet')).json();
    assert.equal(current.page_count, 8);
    assert.equal(await page.locator('#current-worksheet-pages li').count(), 8);

    const afterOld = await (await context.request.get(base + '/api/participant', { headers: beforeHeaders })).json();
    assert.equal(afterOld.worksheet_revision_id, beforeData.worksheet_revision_id);
    assert.deepEqual(afterOld.config.pages, beforeData.config.pages);
    const afterOldPdf = await (await context.request.get(base + '/api/participant/worksheet', { headers: beforeHeaders })).body();
    assert.ok(afterOldPdf.equals(beforePdf), 'An existing participant retains their worksheet PDF');

    const newContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const newPage = await newContext.newPage();
    await newPage.goto(base);
    await newPage.locator('#registration-view').waitFor({ state: 'visible' });
    await newPage.locator('#consent-checkbox').check();
    await newPage.locator('#register-button').click();
    await newPage.locator('#participant-view').waitFor({ state: 'visible' });
    const newParticipant = await newPage.locator('#participant-id').textContent();
    const newToken = (await newPage.locator('#resume-link').inputValue()).split('#')[1];
    const newData = await (await newContext.request.get(base + '/api/participant', { headers: { Authorization: 'Bearer ' + newToken } })).json();
    assert.equal(newData.worksheet_revision_id, current.active_revision_id);
    assert.equal(newData.config.pages.length, 8);
    assert.equal(await newPage.locator('#page-list .page-row').count(), 8);
    await newContext.close();

    // Mock another participant's pinned one-page version while the public active config has eight pages.
    await page.route('**/api/participant', route => {
      const pinned = { ...afterOld, config: { ...afterOld.config, pages: [afterOld.config.pages[0]] }, pages: [], missing_pages: [afterOld.config.pages[0].qr_code] };
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(pinned) });
    });
    await page.goto(beforeLink);
    await page.locator('#participant-view').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#page-list .page-row').count(), 1, 'Participant rendering must use pinned config, not public active config');
    await page.unroute('**/api/participant');
    await page.goto(base + '/researcher');
    await page.locator('#researcher-content').waitFor({ state: 'visible' });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.screenshot({ path: path.join(output, 'desktop-current.png'), fullPage: true });
    assert.deepEqual(errors, []);
    const result = { ok: true, beforeParticipant, newParticipant, originalBytesPreserved: true, inspectedPages: 8, replacementConfirmed: true,
      existingParticipantRevisionRetained: true, newParticipantUsesCurrentRevision: true, participantConfigOverridesActiveConfig: true, browserErrors: errors };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
