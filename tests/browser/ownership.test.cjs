const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {OwnershipManager} = require('../../extension/ownership.js');
const digest = async text => createHash('sha256').update(text).digest('hex');
const command = (overrides={}) => ({id:'command-1', application_id:'app-a', worker_id:'worker-a', ownership_token:'token-a', action:'fill', payload:{values:[]}, ...overrides});
async function setup() {const data={}; const storage={get:async()=>structuredClone(data),set:async value=>Object.assign(data,structuredClone(value))}; const manager=new OwnershipManager(storage,digest); await manager.initialize(); return {manager,storage,data};}
test('different workers cannot mutate the same owned application or tab', async () => {
  const {manager} = await setup(); await manager.assign(command(), 11, 'https://example.invalid');
  assert.equal(manager.check(command()).tab_id, 11);
  assert.throws(()=>manager.check(command({worker_id:'worker-b'})), {code:'OWNERSHIP_MISMATCH'});
  assert.throws(()=>manager.check(command({ownership_token:'wrong'})), {code:'OWNERSHIP_MISMATCH'});
  await assert.rejects(manager.assign(command({application_id:'app-b'}),11,'https://example.invalid'), {code:'TAB_ALREADY_OWNED'});
});
test('two simultaneous copies of a command execute once', async () => {
  const {manager} = await setup(); let executions=0;
  const execute=async()=>{executions++; await new Promise(resolve=>setImmediate(resolve)); return {filled:true};};
  const result=await Promise.all([manager.run(command(),execute),manager.run(command(),execute)]);
  assert.equal(executions,1); assert.deepEqual(result,[{filled:true},{filled:true}]);
});
test('different commands serialize within app while distinct applications run concurrently', async () => {
  const {manager} = await setup(); const order=[]; let release;
  const barrier=new Promise(resolve=>{release=resolve;});
  const first=manager.run(command(),async()=>{order.push('a-start'); await barrier; order.push('a-end');});
  await new Promise(resolve=>setImmediate(resolve));
  const second=manager.run(command({id:'command-2'}),async()=>{order.push('a-next');});
  await manager.run(command({application_id:'app-b'}),async()=>{order.push('b');}); release(); await Promise.all([first,second]);
  assert.deepEqual(order,['a-start','b','a-end','a-next']);
});
test('completed receipts survive restart and never repeat a final click', async () => {
  const {manager,storage} = await setup(); let clicks=0; const c=command({action:'click'});
  await manager.run(c,async()=>{clicks++;return {clicked:true};});
  const restarted=new OwnershipManager(storage,digest);await restarted.initialize(); await restarted.run(c,async()=>{clicks++;}); assert.equal(clicks,1);
});
test('pending mutation after crash is uncertain and never replayed', async () => {
  const {manager,data} = await setup(); const c=command({action:'click'});
  const fingerprint=await digest(JSON.stringify({action:c.action,payload:c.payload,worker:c.worker_id,token:c.ownership_token}));
  data.receipts={'app-a:command-1':{status:'pending',fingerprint}}; await manager.initialize();
  await assert.rejects(manager.run(c,async()=>assert.fail('must not execute')), {code:'UNCERTAIN_ACTION'});
});
test('command ID cannot be reused for a changed answer', async () => {
  const {manager} = await setup();await manager.run(command(),async()=>({ok:true}));
  await assert.rejects(manager.run(command({payload:{values:['changed']}}),async()=>{}), {code:'COMMAND_ID_REUSED'});
});
test('ownership transfer requires prior token and updates both identity fields', async () => {
  const {manager} = await setup();await manager.assign(command(),5,'https://example.invalid');
  const next=command({action:'reassign',worker_id:'worker-b',ownership_token:'token-b',payload:{previous_ownership_token:'token-a',new_worker_id:'worker-b',new_ownership_token:'token-b'}});
  await assert.rejects(manager.reassign({...next,payload:{...next.payload,previous_ownership_token:'wrong'}}), {code:'OWNERSHIP_MISMATCH'});
  await manager.reassign(next);assert.equal(manager.check(next).tab_id,5);assert.throws(()=>manager.check(command()), {code:'OWNERSHIP_MISMATCH'});
});
test('expired commands fail before browser action', async () => {
  const {manager}=await setup();await assert.rejects(manager.run(command({deadline:Date.now()-10000}),async()=>assert.fail('expired operation')), {code:'DEADLINE_EXCEEDED'});
});
