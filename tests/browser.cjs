// Optional browser verification: npm install --no-save playwright, or use Codex's bundled runtime.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const root = path.resolve(__dirname, '..');
const runtime = path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies');
const {chromium} = require(require.resolve('playwright', {paths:[root, path.join(runtime, 'node/node_modules')]}));
const python = process.env.GATE1_PYTHON || (fs.existsSync(path.join(runtime, 'python/python.exe')) ? path.join(runtime, 'python/python.exe') : 'python');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'gate1-ui-'));
const processes = [];
async function serve(user) {
  const port = await new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
  const child = spawn(python, [path.join(root, 'app.py'), '--port', String(port), '--user', String(user), '--db', path.join(temp, 'ui.sqlite3')], {windowsHide:true});
  processes.push(child);
  await new Promise((resolve,reject) => {child.stdout.once('data',resolve);child.once('error',reject);child.once('exit',code=>reject(new Error(`Server exited: ${code}`)));});
  return `http://127.0.0.1:${port}`;
}
(async () => {
  let browser;
  let checks = 0;
  const check = (value, description) => {assert.ok(value, description); checks++; console.log(`PASS ${description}`);};
  try {
    const admin = await serve(1), member = await serve(2);
    browser = await chromium.launch({channel:'msedge', headless:true});
    const page = await browser.newPage({viewport:{width:1100,height:1000}});
    const errors=[]; page.on('pageerror', e => errors.push(e.message));
    const open = async (base,id) => { await page.goto(`${base}/?id=${id}`); await page.locator('#detail').waitFor({state:'visible'}); };
    const message = async (expected) => {await page.waitForFunction(text => document.querySelector('#message').textContent === text, expected); check(await page.locator('#message').isVisible(), expected);};
    const clickStatus = async (status, label) => {await page.locator(`input[value="${status}"]`).check();await page.locator('#status-form button').click();await message(`ステータスを「${label}」に変更しました。`);};
    await open(admin,1);
    check(await page.locator('#status-options input').count() === 1 && await page.locator('#status-options').innerText() === '対応中', 'NEW shows only IN_PROGRESS');
    check(await page.locator('#history').innerText() === '変更履歴はありません', 'Empty history text');
    check(JSON.stringify(await page.locator('#assignee option').allTextContents()) === JSON.stringify(['未割り当て','佐藤 花子','鈴木 一郎','田中 美咲']), 'Admin dropdown, kana order, active users only');
    await page.evaluate(() => window.gate1.changeStatus('DONE'));
    await message('このステータスへは変更できません。画面を再読み込みしてください。');
    check(await page.locator('#message').getAttribute('class') === 'error', 'Invalid transition displayed as red error');
    await page.locator('#comment').fill('あ'.repeat(201));
    await page.locator('#status-form button').click();
    await message('コメントは 200 文字以内で入力してください。');
    await page.locator('#comment').fill('あ'.repeat(200));
    await clickStatus('IN_PROGRESS','対応中');
    check(await page.locator('#current-assignee').innerText() === '佐藤 花子', 'Automatic assignee');
    check(await page.locator('.history-item').count() === 2, 'Both status and automatic assignment history');
    check(await page.locator('#message').getAttribute('class') === 'success', 'Success message green');
    await clickStatus('PENDING','保留');
    check(await page.locator('#status-options input').count() === 1, 'PENDING only resumes');
    await clickStatus('IN_PROGRESS','対応中');
    await clickStatus('DONE','完了');
    check(await page.locator('#assignee-button').isDisabled(), 'DONE assignment disabled');
    check(await page.locator('#status-options').innerText() === '対応中', 'DONE reopens only');
    await clickStatus('IN_PROGRESS','対応中');
    const reopened = await (await fetch(`${admin}/api/inquiries/1`)).json();
    check(reopened.inquiry.closed_at === null, 'Reopening clears closed_at');
    await open(member,5);
    check(await page.locator('#assignee-button').isDisabled() && await page.locator('#assignee-notice').innerText() === '担当者の変更は管理者に依頼してください', 'MEMBER assigned inquiry disabled and notice shown');
    check(!(await page.locator('#assignee option').allTextContents()).includes('未割り当て'), 'MEMBER has no unassigned option');
    await open(admin,5);
    await page.locator('#assignee').selectOption('');await page.locator('#assignee-button').click();
    await message('担当者を未割り当てに戻しました。');
    await open(member,5);
    await page.locator('#assignee').selectOption('1');await page.locator('#assignee-button').click();
    await message('この操作を行う権限がありません。');
    await page.locator('#assignee').selectOption('2');await page.locator('#assignee-button').click();
    await message('担当者を「鈴木 一郎」に変更しました。');
    check(await page.locator('#assignee-button').isDisabled(), 'MEMBER self assignment then disabled');
    for(const [id,color] of [[2,'rgb(207, 34, 46)'],[3,'rgb(128, 128, 128)'],[5,'rgb(0, 0, 0)']]) {
      await open(admin,id);
      check(await page.locator('[class^="priority-"]').evaluate(el => getComputedStyle(el).color) === color, `Priority color id=${id}`);
    }
    await open(admin,2);
    const other = await browser.newPage();await other.goto(`${admin}/?id=2`);await other.locator('#detail').waitFor({state:'visible'});
    await other.locator('#assignee').selectOption('3');await other.locator('#assignee-button').click();
    await other.waitForFunction(() => document.querySelector('#message').textContent === '担当者を「田中 美咲」に変更しました。');
    await page.locator('#status-form button').click();
    await message('他のユーザーが更新しました。画面を再読み込みしてください。');await other.close();
    await page.goto(`${admin}/?id=999`);await message('指定された問い合わせは存在しません。');
    await open(admin,1);await page.getByText('一覧に戻る',{exact:true}).click();await page.locator('#list').waitFor({state:'visible'});
    check(await page.locator('.list-item').count() === 5, 'Existing-list navigation fixture');
    await page.setViewportSize({width:390,height:844});await open(admin,1);
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile width has no horizontal overflow');
    check(errors.length === 0, `No browser errors: ${errors.join(',')}`);
    console.log(`PASS ${checks} browser checks. No screenshots were taken.`);
  } finally {
    if(browser) await browser.close();
    for(const child of processes) child.kill();
    // Keep only temporary test data in the OS temp directory; submission DB is untouched.
  }
})().catch(error => {console.error(error);process.exitCode=1;});
