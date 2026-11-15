/* Independent submissions with real local API/storage, including all-white PDFs. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('../.browser-tools/node_modules/playwright');
const root = path.resolve(__dirname, '..');
const base = process.env.BROWSER_BASE_URL || 'http://127.0.0.1:8003';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
const output = path.join(root, 'artifacts', 'submission-browser'); fs.mkdirSync(output, { recursive: true });
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
execFileSync(python, ['-c', [
  'import sys; from pathlib import Path; from pypdf import PdfReader, PdfWriter; import fitz; from PIL import Image, ImageDraw',
  'root, folder = map(Path, sys.argv[1:]); source = root / "resources/worksheets/hiragana_collection_a4.pdf"',
  'reader = PdfReader(source)',
  'for name, indexes in [("form-a.pdf", [0, 1]), ("form-b.pdf", [2])]:',
  '    writer = PdfWriter()',
  '    for index in indexes: writer.add_page(reader.pages[index])',
  '    writer.write(folder / name)',
  'with fitz.open(source) as document:',
  '    pix = document[2].get_pixmap(matrix=fitz.Matrix(2, 2)); page = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)',
  'pen = ImageDraw.Draw(page)',
  'for y in range(250,1350,35):',
  '    for x in range(180,1000,60): pen.line([(x,y),(x+20,y+20),(x+40,y+3)],fill=(75,75,75),width=3)',
  'photo = Image.new("RGB", (page.width+200,page.height+200), "white"); photo.paste(page,(100,100)); photo.save(folder/"photo-b.jpg",quality=95)',
].join('\n'), root, output]);

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'chrome' } : {}) });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, acceptDownloads: true });
  const page = await context.newPage(), errors = [], requests = [];
  page.setDefaultTimeout(60000); page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (/Content Security|Refused|Uncaught/i.test(message.text())) errors.push(message.text()); });
  page.on('request', request => { if (/\/worksheets\/.+\/submit$/.test(request.url())) requests.push(request.url()); });
  const checkPdf = filename => JSON.parse(execFileSync(python, ['-c', 'import json,sys; from pypdf import PdfReader; p=PdfReader(sys.argv[1]); print(json.dumps({"pages":len(p.pages),"images":[len(list(page.images)) for page in p.pages]}))', filename], { encoding: 'utf8' }));
  try {
    const login = await context.request.post(base + '/api/researcher/login', { data: { password: 'library-browser-test-password' } });
    assert.equal(login.status(), 200); const csrf = (await login.json()).csrf_token;
    const ids = [];
    for (const name of ['form-a.pdf', 'form-b.pdf']) {
      const response = await context.request.post(base + '/api/researcher/worksheet/inspect', {
        headers: { 'X-CSRF-Token': csrf }, multipart: { file: { name, mimeType: 'application/pdf', buffer: fs.readFileSync(path.join(output, name)) } },
      });
      assert.equal(response.status(), 200, await response.text()); ids.push((await response.json()).draft_id);
    }
    const active = await (await context.request.get(base + '/api/researcher/worksheet')).json();
    assert.equal((await context.request.post(base + '/api/researcher/worksheet/selection', { headers: { 'X-CSRF-Token': csrf },
      data: { worksheet_ids: ids, expected_active_revision_id: active.active_revision_id } })).status(), 200);
    await page.goto(base); await page.locator('#consent-checkbox').check(); await page.locator('#register-button').click();
    await page.locator('#worksheet-submissions').waitFor({ state: 'visible' });
    const first = page.locator(`[data-worksheet-id="${ids[0]}"]`), second = page.locator(`[data-worksheet-id="${ids[1]}"]`);
    assert.equal(await first.locator('.worksheet-submit').isEnabled(), true, 'Zero photographs may be submitted as blank pages');
    assert.equal(await page.locator('#submit-button').isVisible(), false, 'Multiple files have independent submit buttons');
    await first.locator('.worksheet-submit').click();
    assert.match(await page.locator('#confirm-description').textContent(), /未登録の2ページは白紙/);
    await page.locator('#confirm-cancel').click(); assert.equal(requests.length, 0);
    await first.locator('.worksheet-submit').evaluate(button => { button.click(); button.click(); });
    await page.locator('#confirm-accept').click();
    await page.locator(`[data-worksheet-id="${ids[0]}"][data-status="submitted"]`).waitFor();
    assert.equal(requests.length, 1);
    assert.equal(await second.locator('.worksheet-submit').isEnabled(), true);
    assert.equal(await page.locator('#camera-button').isEnabled(), true);
    const event = page.waitForEvent('download'); await first.locator('.worksheet-result-download').click();
    const download = await event, destination = path.join(output, 'blank-a.pdf'); await download.saveAs(destination);
    assert.deepEqual(checkPdf(destination), { pages: 2, images: [0, 0] });
    await page.reload(); await page.locator(`[data-worksheet-id="${ids[0]}"][data-status="submitted"]`).waitFor();
    await page.evaluate(() => document.activeElement?.blur());
    await page.screenshot({ path: path.join(output, 'mobile-partial-submission.png'), fullPage: true, mask: [page.locator('#resume-link')] });
    for (const width of [320, 844, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    }
    await page.setViewportSize({ width: 390, height: 844 });
    const researcher = await context.newPage(); await researcher.goto(base + '/researcher');
    await researcher.getByText('一部提出', { exact: true }).waitFor();
    assert.match(await researcher.locator('#participant-rows').textContent(), /1 \/ 2用紙提出 · 白紙2ページ/);
    await researcher.close();
    await page.locator('#gallery-input').setInputFiles(path.join(output, 'photo-b.jpg'));
    await page.locator('#capture-next-register').click();
    await page.waitForFunction(() => document.getElementById('batch-progress').hidden && document.getElementById('batch-status').textContent.includes('1枚を登録しました'));
    assert.match(await second.textContent(), /写真1ページ/);
    await second.locator('.worksheet-submit').click();
    assert.match(await page.locator('#confirm-description').textContent(), /未登録の0ページ/);
    await page.locator('#confirm-accept').click(); await page.locator('#submitted-panel').waitFor({ state: 'visible' });
    const combinedEvent = page.waitForEvent('download'); await page.locator('#download-pdf').click();
    const combined = await combinedEvent, combinedPath = path.join(output, 'combined.pdf'); await combined.saveAs(combinedPath);
    assert.deepEqual(checkPdf(combinedPath), { pages: 3, images: [0, 0, 1] });
    assert.equal(requests.length, 2); assert.deepEqual(errors, []);
    const result = { ok: true, independentSubmitButtons: true, zeroPhotoSubmission: true, blankPageCounts: [2, 0],
      confirmationAndCancellation: true, duplicateClickRequestedOnce: true, otherFileRemainsEditable: true,
      stateSurvivesReload: true, researcherShowsPartialSubmission: true, combinedPageImages: [0, 0, 1],
      viewportWidths: [320, 390, 844, 1440], browserErrors: errors, localTestDataOnly: true };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result, null, 2));
  } finally { await context.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
