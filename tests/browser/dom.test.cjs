const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const {createEngine} = require('../../extension/dom-engine.js');
const adapters = require('../../extension/adapters.js');
const fixture = name => fs.readFileSync(path.join(__dirname, 'fixtures', name + '.html'), 'utf8');
function setup(html, url = 'http://127.0.0.1:8765/fixtures/application.html') {
  const dom = new JSDOM(html, {url, runScripts: 'dangerously', pretendToBeVisual: true});
  return {dom, document: dom.window.document, engine: createEngine(dom.window.document, adapters)};
}
for (const name of ['greenhouse','lever','ashby','workday','icims','smartrecruiters']) test(`detect ${name} ATS and structured controls`, () => {
  const {engine} = setup(fixture(name)); const s = engine.inspect();
  assert.equal(s.platform, name); assert.ok(s.fields.length); assert.ok(s.buttons.length);
  assert.ok(s.fields.every(f => f.id && f.label)); assert.ok(s.text.length <= 20000);
});
test('deterministic native text, select, radio and event readback', async () => {
  const {engine, document} = setup(fixture('greenhouse'));
  const s = engine.inspect(); const byName = name => s.fields.find(f => f.name === name).id;
  let changes = 0; document.addEventListener('input', () => changes++);
  const result = await engine.fill([{field_id: byName('first_name'), value: 'Test'}, {field_id: byName('email'), value: 'test@example.invalid'}, {field_id: byName('country'), value: 'United States'}, {field_id: byName('authorized'), value: 'Yes'}]);
  assert.ok(result.fields.every(f => f.ok)); assert.ok(changes >= 3); assert.equal(document.querySelector('#country').value, 'US');
  assert.equal(engine.inspect().fields.find(f => f.name === 'authorized').value, 'Yes');
  assert.notEqual(engine.inspect().snapshot_id, s.snapshot_id);
});
test('custom combobox exact selection and date fields', async () => {
  const {engine} = setup(fixture('ashby')); const s = engine.inspect();
  const result = await engine.fill([{field_id: s.fields[0].id, value: 'Canada'}, {field_id: s.fields[1].id, value: '2027-05-01'}]);
  assert.ok(result.fields.every(f => f.ok)); assert.equal(engine.inspect().fields[0].value, 'Canada');
});
test('reject ambiguous dropdowns and excessive lengths without truncation', async () => {
  const {engine} = setup('<label>Name<input maxlength="3"></label><label>Choice<select><option value="a">Same</option><option value="b">Same</option></select></label>');
  const s = engine.inspect(); const result = await engine.fill([{field_id:s.fields[0].id,value:'too long'}, {field_id:s.fields[1].id,value:'Same'}]);
  assert.equal(result.fields[0].error.code, 'LENGTH_EXCEEDED'); assert.equal(result.fields[1].error.code, 'OPTION_NOT_FOUND');
});
test('account password snapshots and fill responses never return secret values', async () => {
  const {engine} = setup(fixture('workday')); const s = engine.inspect(); assert.equal(s.stage, 'account');
  const password = s.fields.find(f => f.type === 'password'); const result = await engine.fill([{field_id: password.id, value: 'private-test-password'}]);
  assert.equal(result.fields[0].ok, true); assert.equal(result.fields[0].value, '');
  assert.ok(!JSON.stringify(engine.inspect()).includes('private-test-password')); assert.ok(!JSON.stringify(result).includes('private-test-password'));
});
test('CAPTCHA blocks all filling and final submission', async () => {
  const {engine} = setup(fixture('captcha')); const s = engine.inspect(); assert.equal(s.captcha, true); assert.equal(s.stage, 'captcha');
  await assert.rejects(engine.fill([{field_id:s.fields[0].id,value:'test@example.invalid'}]), {code:'CAPTCHA'});
  assert.throws(() => engine.click({button_id:s.buttons[0].id, snapshot_id:s.snapshot_id, expected_url:s.url}), {code:'CAPTCHA'});
});
test('final submission requires required values and fresh inspected snapshot', async () => {
  const {engine, document} = setup(fixture('smartrecruiters')); let clicks = 0; document.querySelector('button').addEventListener('click',e => {e.preventDefault(); clicks++;});
  let s = engine.inspect(); assert.equal(s.stage, 'final');
  assert.throws(() => engine.click({button_id:s.buttons[0].id,snapshot_id:s.snapshot_id,expected_url:s.url}), {code:'VALIDATION_FAILED'});
  await engine.fill([{field_id:s.fields.find(f=>f.type==='checkbox').id,value:true}]);
  assert.throws(() => engine.click({button_id:s.buttons[0].id,snapshot_id:s.snapshot_id,expected_url:s.url}), {code:'STALE_SNAPSHOT'});
  s = engine.inspect(); assert.equal(engine.click({button_id:s.buttons[0].id,snapshot_id:s.snapshot_id,expected_url:s.url}).kind, 'submit'); assert.equal(clicks, 1);
});
test('next buttons never become final submit buttons', () => {
  const {engine} = setup(fixture('greenhouse')); const s = engine.inspect(); assert.equal(s.stage, 'form'); assert.equal(s.buttons[0].kind, 'next');
});
test('visible validation errors prevent submission', () => {
  const {engine} = setup('<form><p role="alert">Email already registered</p><button>Submit application</button></form>'); const s = engine.inspect();
  assert.deepEqual(s.validation_errors, ['Email already registered']); assert.throws(() => engine.click({button_id:s.buttons[0].id,snapshot_id:s.snapshot_id,expected_url:s.url}), {code:'VALIDATION_FAILED'});
});
test('submission success requires affirmative visible evidence', () => {
  assert.equal(setup(fixture('success')).engine.inspect().success, true);
  assert.equal(setup('<h1>Submit your application</h1><p hidden>Thank you for applying!</p><script>"Your application was submitted";</script>').engine.inspect().success, false);
  assert.equal(setup('<p>Your application was submitted</p><label>Email<input required></label>').engine.inspect().success, false);
});
test('open shadow roots inspected; hidden and disabled fields excluded', () => {
  const {engine, document} = setup('<div id="host"></div><input hidden name="trap"><input disabled name="disabled"><section style="display:none"><input name="hidden_parent"></section>');
  document.getElementById('host').attachShadow({mode:'open'}).innerHTML = '<label>Name<input name="name" required></label>';
  const s = engine.inspect(); assert.equal(s.fields.length, 1); assert.equal(s.fields[0].label, 'Name');
});
test('verification codes expose a dedicated stage and button', () => {
  const s = setup(fixture('verification')).engine.inspect(); assert.equal(s.stage, 'verification'); assert.equal(s.buttons[0].kind, 'verify');
});
test('removed elements fail safely and optional checkbox false stays false', async () => {
  const {engine, document} = setup('<label>Name<input name="name"></label><label><input type="checkbox">Opt in</label>'); const s = engine.inspect(); document.querySelector('input').remove();
  const result = await engine.fill([{field_id:s.fields[0].id,value:'Test'}, {field_id:s.fields[1].id,value:false}]); assert.equal(result.fields[0].error.code,'STALE_FIELD'); assert.equal(result.fields[1].value,false);
});
