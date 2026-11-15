/* Real local API/storage/browser verification for a library of selectable PDFs. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('../.browser-tools/node_modules/playwright');
const root = path.resolve(__dirname, '..');
const base = process.env.BROWSER_BASE_URL || 'http://127.0.0.1:8002';
assert.ok(['localhost', '127.0.0.1'].includes(new URL(base).hostname), 'Only an isolated local test server is allowed');
const output = path.join(root, 'artifacts', 'library-browser');
fs.mkdirSync(output, { recursive: true });
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
execFileSync(python, ['-c', [
  'import sys; from pathlib import Path; from pypdf import PdfReader, PdfWriter',
  "source = PdfReader(Path(sys.argv[1]) / 'resources/worksheets/hiragana_collection_a4.pdf')",
  "for index, name in enumerate(['form-a.pdf', 'form-b.pdf']):",
  '    writer = PdfWriter(); writer.add_page(source.pages[index]); writer.write(Path(sys.argv[2]) / name)',
].join('\n'), root, output]);
const inputs = ['form-a.pdf', 'form-b.pdf'].map(name => path.join(output, name));
const bytes = inputs.map(file => fs.readFileSync(file));
const errors = [];

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'chrome' } : {}) });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(60000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (/Content Security|Refused|Uncaught/i.test(message.text())) errors.push(message.text()); });
  const newContext = await browser.newContext({ viewport: { width: 390, height: 844 }, acceptDownloads: true });
  const newcomer = await newContext.newPage();
  newcomer.on('pageerror', error => errors.push(error.message));
  async function enroll(target) {
    await target.goto(base);
    await target.locator('#registration-view').waitFor({ state: 'visible' });
    await target.locator('#consent-checkbox').check(); await target.locator('#register-button').click();
    await target.locator('#participant-view').waitFor({ state: 'visible' });
    return (await target.locator('#resume-link').inputValue()).split('#')[1];
  }
  async function selectAndSave() {
    await page.locator('#save-worksheet-selection').click();
    await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
    assert.match(await page.locator('#confirm-description').textContent(), /登録済み/);
    await page.locator('#confirm-accept').click();
    await page.locator('#save-worksheet-selection:not([disabled])').waitFor();
  }
  try {
    const oldToken = await enroll(page), oldHeaders = { Authorization: 'Bearer ' + oldToken };
    const old = await (await context.request.get(base + '/api/participant', { headers: oldHeaders })).json();
    const oldPdf = await (await context.request.get(base + old.worksheets[0].download_url, { headers: oldHeaders })).body();
    assert.equal((await context.request.get(base + old.worksheets[0].download_url)).status(), 401);
    await page.goto(base + '/researcher');
    await page.locator('#password-input').fill('library-browser-test-password'); await page.locator('#login-button').click();
    await page.locator('#researcher-content').waitFor({ state: 'visible' });
    const initial = await (await context.request.get(base + '/api/researcher/worksheet')).json();
    assert.notEqual(await page.locator('#revision-file').getAttribute('multiple'), null);
    await page.locator('#revision-file').setInputFiles(inputs); await page.locator('#inspect-worksheet:not([disabled])').click();
    await page.waitForFunction(() => document.getElementById('revision-message').textContent.includes('2件のPDFを登録しました'));
    const library = await (await context.request.get(base + '/api/researcher/worksheet')).json();
    assert.equal(library.active_revision_id, initial.active_revision_id, 'Adding PDFs must not publish them');
    const a = library.library.find(file => file.original_name === 'form-a.pdf');
    const b = library.library.find(file => file.original_name === 'form-b.pdf');
    assert.ok(a && b);
    for (const selected of await page.locator('#worksheet-library input:checked').all()) await selected.uncheck();
    await page.locator(`#worksheet-library input[value="${b.id}"]`).check();
    await page.locator(`#worksheet-library input[value="${a.id}"]`).check();
    assert.match(await page.locator('#worksheet-selection-preview').textContent(), /① form-b.pdf.*② form-a.pdf/s);
    await selectAndSave();
    assert.match(await page.locator('#revision-message').textContent(), /配布するPDFを保存しました/);
    const selection = await (await context.request.get(base + '/api/researcher/worksheet')).json();
    assert.deepEqual(selection.selected_worksheet_ids, [b.id, a.id]);
    const oldReturned = await (await context.request.get(base + '/api/participant', { headers: oldHeaders })).json();
    assert.equal(oldReturned.worksheet_revision_id, old.worksheet_revision_id);
    assert.deepEqual(oldReturned.config.pages, old.config.pages);
    assert.ok((await (await context.request.get(base + old.worksheets[0].download_url, { headers: oldHeaders })).body()).equals(oldPdf));
    await page.evaluate(() => document.activeElement?.blur());
    await page.screenshot({ path: path.join(output, 'mobile-library.png'), fullPage: true });
    for (const width of [320, 844, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'Researcher layout must fit');
    }
    await page.screenshot({ path: path.join(output, 'desktop-library.png'), fullPage: true });
    const token = await enroll(newcomer), headers = { Authorization: 'Bearer ' + token };
    const assigned = await (await newContext.request.get(base + '/api/participant', { headers })).json();
    assert.equal(assigned.worksheet_revision_id, selection.active_revision_id);
    assert.deepEqual(assigned.missing_pages, ['HIRA1-02', 'HIRA1-01']);
    const buttons = newcomer.locator('.worksheet-download:visible');
    assert.equal(await buttons.count(), 2);
    assert.match(await buttons.nth(0).textContent(), /^① form-b.pdf/);
    assert.match(await buttons.nth(1).textContent(), /^② form-a.pdf/);
    for (const [index, expected] of [bytes[1], bytes[0]].entries()) {
      const event = newcomer.waitForEvent('download'); await buttons.nth(index).click();
      const file = await event, destination = path.join(output, `download-${index + 1}.pdf`);
      await file.saveAs(destination); assert.ok(fs.readFileSync(destination).equals(expected));
      await newcomer.waitForFunction(() => !document.getElementById('download-worksheet').disabled);
    }
    assert.equal(await newcomer.locator('#page-list .page-row').count(), 2);
    await newcomer.evaluate(() => document.activeElement?.blur());
    await newcomer.screenshot({ path: path.join(output, 'mobile-downloads.png'), fullPage: true, mask: [newcomer.locator('#resume-link')] });
    for (const width of [320, 844, 1440]) {
      await newcomer.setViewportSize({ width, height: 1000 });
      assert.equal(await newcomer.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'Participant layout must fit');
    }
    await page.locator('#revision-file').setInputFiles({ name: 'duplicate-a.pdf', mimeType: 'application/pdf', buffer: bytes[0] });
    await page.locator('#inspect-worksheet:not([disabled])').click();
    await page.waitForFunction(() => document.getElementById('revision-message').textContent.includes('1件のPDFを登録しました'));
    const expanded = await (await context.request.get(base + '/api/researcher/worksheet')).json();
    const duplicate = expanded.library.find(file => file.original_name === 'duplicate-a.pdf');
    await page.locator(`#worksheet-library input[value="${duplicate.id}"]`).check();
    await selectAndSave();
    assert.match(await page.locator('#revision-message').textContent(), /重複/);
    assert.equal((await (await context.request.get(base + '/api/researcher/worksheet')).json()).active_revision_id, selection.active_revision_id);
    await page.reload(); await page.locator('#researcher-content').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#worksheet-library input:checked').count(), 2);
    assert.equal(await page.locator('#worksheet-library input').count(), initial.library.length + 3);
    assert.deepEqual(errors, []);
    const result = { ok: true, multipleUploadsRetained: true, selectedInChosenOrder: true, individualOriginalBytesPreserved: true,
      existingParticipantRetained: true, requiredPagesFromEverySelectedPdf: true, duplicateQrSelectionRejected: true,
      selectionSurvivesReload: true, viewportWidths: [320, 390, 844, 1440], browserErrors: errors, localTestDataOnly: true };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result, null, 2));
  } finally { await newContext.close(); await context.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
