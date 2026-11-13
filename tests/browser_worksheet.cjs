/* Consent-gated worksheet PDF download in a real mobile-width browser. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('../.browser-tools/node_modules/playwright');

const root = path.resolve(__dirname, '..');
const base = process.env.BROWSER_BASE_URL || 'http://127.0.0.1:8000';
const output = path.join(root, 'artifacts', 'worksheet-browser');
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'chrome' } : {}) });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, acceptDownloads: true });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', msg => { if (msg.type() === 'error' && /Content Security|Refused|Uncaught/i.test(msg.text())) errors.push(msg.text()); });
  page.setDefaultTimeout(20000);
  try {
    assert.equal((await context.request.get(base + '/api/participant/worksheet')).status(), 401);
    assert.equal(Object.hasOwn(await (await context.request.get(base + '/api/config')).json(), 'worksheet'), false);
    await page.goto(base);
    await page.locator('#registration-view').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#download-worksheet').isVisible(), false);
    await page.locator('#consent-checkbox').check();
    await page.locator('#register-button').click();
    await page.locator('#participant-view').waitFor({ state: 'visible' });
    await page.locator('#download-worksheet').waitFor({ state: 'visible' });
    const participant = await page.locator('#participant-id').textContent();
    const link = await page.locator('#resume-link').inputValue();
    assert.equal(await page.locator('#worksheet-test-note').isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    let worksheetRequests = 0;
    page.on('request', req => { if (new URL(req.url()).pathname === '/api/participant/worksheet') worksheetRequests++; });
    const downloadPromise = page.waitForEvent('download');
    await page.evaluate(() => { const button = document.getElementById('download-worksheet'); button.click(); button.click(); });
    const downloaded = await downloadPromise;
    assert.equal(downloaded.suggestedFilename(), 'test-worksheet.pdf');
    const pdfPath = path.join(output, 'test-worksheet.pdf');
    await downloaded.saveAs(pdfPath);
    assert.equal(worksheetRequests, 1, 'Rapid repeated clicks must start a single request');
    const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    const pdfInfo = JSON.parse(execFileSync(python, ['-c', 'import json,sys; from pypdf import PdfReader; p=PdfReader(sys.argv[1]); print(json.dumps({"pages":len(p.pages),"width":float(p.pages[0].mediabox.width),"height":float(p.pages[0].mediabox.height)}))', pdfPath], { encoding: 'utf8' }));
    assert.equal(pdfInfo.pages, 3);
    assert.ok(Math.abs(pdfInfo.width - 595.276) < 1 && Math.abs(pdfInfo.height - 841.89) < 1);
    await page.screenshot({ path: path.join(output, 'mobile-worksheet.png'), fullPage: true, mask: [page.locator('#resume-link')], maskColor: '#dfe5d6' });

    const resumed = await browser.newContext({ viewport: { width: 390, height: 844 }, acceptDownloads: true });
    const resumedPage = await resumed.newPage();
    await resumedPage.goto(link);
    await resumedPage.locator('#download-worksheet').waitFor({ state: 'visible' });
    assert.equal(await resumedPage.locator('#participant-id').textContent(), participant);
    const resumedDownloadPromise = resumedPage.waitForEvent('download');
    await resumedPage.locator('#download-worksheet').click();
    const resumedDownload = await resumedDownloadPromise;
    const resumedPath = path.join(output, 'resumed-test-worksheet.pdf');
    await resumedDownload.saveAs(resumedPath);
    assert.equal(fs.readFileSync(pdfPath).equals(fs.readFileSync(resumedPath)), true);
    await resumed.close();

    await page.route('**/api/participant', async route => {
      const response = await route.fetch();
      const participantData = await response.json();
      participantData.worksheet.available = false;
      await route.fulfill({ response, json: participantData });
    });
    await page.reload();
    await page.locator('#worksheet-unavailable').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#download-worksheet').isVisible(), false);
    await page.unroute('**/api/participant');
    await page.reload();
    await page.locator('#download-worksheet').waitFor({ state: 'visible' });
    await page.route('**/api/participant/worksheet', route => route.fulfill({
      status: 401, contentType: 'application/json', body: JSON.stringify({ detail: { message: '専用の再開リンクから開いてください。' } })
    }));
    await page.locator('#download-worksheet').click();
    await page.locator('#worksheet-message').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#worksheet-message').textContent(), '専用の再開リンクから開いてください。');
    assert.equal(await page.locator('#download-worksheet').isDisabled(), false);
    await page.unroute('**/api/participant/worksheet');
    assert.deepEqual(errors, []);
    const result = { ok: true, participant, unavailableBeforeConsent: true, unauthorizedApiRejected: true, worksheetPages: pdfInfo.pages,
      rapidClicksRequestedOnce: true, resumedDownload: true, notConfiguredState: true, downloadFailureExplanation: true, browserErrors: errors };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
