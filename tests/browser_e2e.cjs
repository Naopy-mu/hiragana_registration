/* Real Chromium integration check. See README for the local-only password setup. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('../.browser-tools/node_modules/playwright');

const root = path.resolve(__dirname, '..');
const base = process.env.BROWSER_BASE_URL || 'http://127.0.0.1:8000';
const output = path.join(root, 'artifacts', 'browser');
fs.mkdirSync(output, { recursive: true });

(async () => {
  const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'chrome' } : {}) });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, isMobile: true, hasTouch: true, acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(120000);
  const errors = [];
  let committedRegistration;
  let registrationKey;
  await page.route(base + '/api/register', async route => {
    const key = route.request().postDataJSON().idempotency_key;
    if (!committedRegistration) {
      registrationKey = key;
      committedRegistration = (await (await route.fetch()).json()).participant_id;
      // The server commits successfully, but the phone loses the response.
      await route.abort('failed');
    } else {
      assert.equal(key, registrationKey, 'Network retry must reuse idempotency key');
      await route.continue();
    }
  });
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', msg => { if (msg.type() === 'error' && /Content Security|Refused|Uncaught/i.test(msg.text())) errors.push(msg.text()); });
  try {
    await page.goto(base);
    await page.locator('#registration-view').waitFor({ state: 'visible' });
    await page.screenshot({ path: path.join(output, 'mobile-registration.png'), fullPage: true });
    assert.equal(await page.locator('#register-button').isDisabled(), true);
    await page.locator('#consent-checkbox').check();
    await page.locator('#register-button').click();
    await page.locator('#app-error').waitFor({ state: 'visible' });
    await page.locator('#register-button:not([disabled])').waitFor();
    await page.locator('#register-button').click();
    await page.locator('#participant-view').waitFor({ state: 'visible' });
    const participantId = await page.locator('#participant-id').textContent();
    assert.match(participantId, /^P\d{3,}$/);
    assert.equal(participantId, committedRegistration, 'Lost registration response must not create a second participant');
    const link = await page.locator('#resume-link').inputValue();
    assert.ok(link.includes('/#') && link.split('#')[1].length >= 32);
    assert.equal(await page.locator('#submit-button').isDisabled(), false);

    async function upload(number, replace = false) {
      await page.locator('#gallery-input').setInputFiles(path.join(root, 'local-data', 'fixtures', `photo-${String(number).padStart(2, '0')}.jpg`));
      await page.locator('#capture-next-register').click();
      if (replace) {
        await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
        await page.locator('#confirm-accept').click();
      }
      await page.waitForFunction(() => document.getElementById('batch-progress').hidden && document.querySelectorAll('.queued-photo[data-status="saved"]').length > 0);
      await page.locator('#camera-button:not([disabled])').waitFor();
      assert.equal(await page.locator('#upload-error').isVisible(), false, await page.locator('#upload-error').textContent());
    }
    await upload(3);
    await page.locator('.page-row.complete').first().waitFor();
    assert.match(await page.locator('#latest-title').textContent(), /3/);
    assert.equal(await page.locator('#submit-button').isDisabled(), false);
    await upload(1);
    assert.equal(await page.locator('.page-row.complete').count(), 2);
    await upload(1, true);
    assert.equal(await page.locator('.page-row.complete').count(), 2);
    await upload(2);
    assert.equal(await page.locator('.page-row.complete').count(), 3);
    assert.equal(await page.locator('#submit-button').isDisabled(), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, 'Mobile layout overflow');
    await page.screenshot({ path: path.join(output, 'mobile-ready.png'), fullPage: true, mask: [page.locator('#resume-link')], maskColor: '#dfe5d6' });
    await page.locator('#submit-button').click();
    await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
    await page.locator('#confirm-accept').click();
    await page.locator('#submitted-panel').waitFor({ state: 'visible' });
    const pdfEvent = page.waitForEvent('download');
    await page.locator('#download-pdf').click();
    const pdf = await pdfEvent;
    assert.equal(pdf.suggestedFilename(), participantId + '.pdf');
    await pdf.saveAs(path.join(output, participantId + '.pdf'));

    // A fresh browser context can resume using only the difficult-to-guess link.
    const resumed = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const resumedPage = await resumed.newPage();
    await resumedPage.goto(link);
    await resumedPage.locator('#submitted-panel').waitFor({ state: 'visible' });
    assert.equal(await resumedPage.locator('#participant-id').textContent(), participantId);
    assert.equal(new URL(resumedPage.url()).hash, '');
    await resumed.close();

    await page.goto(base + '/researcher');
    await page.locator('#login-panel').waitFor({ state: 'visible' });
    await page.locator('#password-input').fill(fs.readFileSync(path.join(root, 'data', 'local-researcher-password.txt'), 'utf8').trim());
    await page.locator('#login-button').click();
    await page.locator('#researcher-content').waitFor({ state: 'visible' });
    await page.getByRole('checkbox', { name: participantId + 'を選択', exact: true }).check();
    await page.locator('input[name="export-type"][value="pdf"]').check();
    await page.locator('input[name="export-type"][value="csv"]').check();
    await page.locator('input[name="export-type"][value="original"]').uncheck();
    await page.locator('input[name="export-type"][value="corrected"]').uncheck();
    const zipEvent = page.waitForEvent('download');
    await page.locator('#export-button').click();
    const zip = await zipEvent;
    const zipPath = path.join(output, 'selected-pdf-csv.zip');
    await zip.saveAs(zipPath);
    const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    const zipMembers = JSON.parse(execFileSync(python, ['-c', 'import json,sys,zipfile; print(json.dumps(zipfile.ZipFile(sys.argv[1]).namelist()))', zipPath], { encoding: 'utf8' }));
    assert.ok(zipMembers.some(name => name.endsWith(participantId + '.pdf')));
    assert.ok(zipMembers.some(name => name.endsWith('.csv')));
    assert.ok(zipMembers.every(name => name.startsWith(participantId + '/') && (/\.pdf$|\.csv$/).test(name)), JSON.stringify(zipMembers));
    await page.evaluate(() => document.activeElement.blur());
    await page.screenshot({ path: path.join(output, 'mobile-researcher.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.evaluate(() => document.activeElement.blur());
    await page.screenshot({ path: path.join(output, 'desktop-researcher.png'), fullPage: true });
    await page.locator('#logout-button').click();
    await page.locator('#login-panel').waitFor({ state: 'visible' });
    assert.deepEqual(errors, [], 'Browser console/page errors');
    const result = { ok: true, participant: participantId, lostRegistrationResponseRetried: true, uploadOrder: [3, 1, 1, 2], replaced: 1, zipMembers, screenshots: output, browserErrors: errors };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
