"use strict";
// DOM engine tests against every local ATS fixture (jsdom + local static server).
const test = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");
const h = require("./helpers.cjs");

let srv;
test.before(async () => { srv = await h.startFixtureServer(); });
test.after(async () => { await srv.close(); });

function subsFor(name) { return srv.submissions.filter(s => s && s.fixture === name); }
const FILE = { name: "resume.pdf", mime: "application/pdf", base64: h.PDF_B64 };

async function fillOk(engine, items) {
  const r = await engine.fill(items);
  for (const x of r.results) assert.ok(x.ok, "fill failed: " + JSON.stringify(x));
  return r.results;
}
function F(snap, label, type) {
  const f = h.field(snap, label, type);
  assert.ok(f, `field ${JSON.stringify(label)} not found in [${snap.fields.map(x => x.label).join(" | ")}]`);
  return f;
}
function B(snap, text) {
  const b = h.button(snap, text);
  assert.ok(b, `button ${JSON.stringify(text)} not found in [${snap.buttons.map(x => x.text).join(" | ")}]`);
  return b;
}
async function settle(engine) { await engine.waitQuiet(150, 3000); }

test("greenhouse: scan, fill all widget types, upload, submit -> confirmation, one POST", async () => {
  const pg = await h.loadPage(srv.base + "/greenhouse/index.html");
  const { engine } = pg;
  let s = engine.scan({});
  assert.equal(s.ats, "greenhouse");
  assert.equal(s.page_kind, "form");
  assert.equal(s.captcha.interactive, false);
  const first = F(s, "First Name");
  assert.equal(first.type, "text"); assert.equal(first.required, true); assert.match(first.key, /^f_[0-9a-f]{8}$/);
  assert.equal(F(s, "Email").type, "email");
  assert.equal(F(s, "Phone").required, false);
  const loc = F(s, "Location (City)");
  assert.equal(loc.type, "combobox"); assert.equal(loc.options_dynamic, true);
  const resume = F(s, "Resume/CV");
  assert.equal(resume.type, "file"); assert.equal(resume.required, true); assert.equal(resume.accept, ".pdf,.doc,.docx,.txt,.rtf");
  const auth = F(s, "Are you legally authorized to work in the United States?");
  assert.equal(auth.type, "combobox"); assert.equal(auth.required, true);
  const why = F(s, "Why do you want to work at Fixture Robotics?");
  assert.equal(why.type, "textarea"); assert.equal(why.max_length, 1500); assert.equal(why.required, true);
  const gender = F(s, "Gender");
  assert.equal(gender.type, "select"); assert.equal(gender.section, "Voluntary Self-Identification");
  assert.deepEqual(gender.options.map(o => o.label), ["Male", "Female", "Decline To Self Identify"]);
  const vet = F(s, "Veteran Status");
  assert.ok(vet.options.some(o => o.label === "I don't wish to answer"));
  assert.equal(B(s, "Submit Application").kind, "submit");
  assert.equal(B(s, "Apply for this job").kind, "apply");
  // keys are stable across scans
  assert.equal(engine.scan({}).fields.find(f => f.id === first.id).key, first.key);

  // empty submit -> validation errors, no POST
  await engine.click(B(s, "Submit Application").id);
  await settle(engine);
  s = engine.scan({});
  assert.equal(subsFor("greenhouse").length, 0);
  assert.ok(s.errors.length >= 5, JSON.stringify(s.errors));
  const fnErr = s.errors.find(e => e.field_id === first.id);
  assert.ok(fnErr && /required/i.test(fnErr.text), JSON.stringify(s.errors));
  assert.equal(F(s, "First Name").error, "This field is required.");

  const res = await fillOk(engine, [
    { field_id: first.id, value: "Ada" },
    { field_id: F(s, "Last Name").id, value: "Lovelace" },
    { field_id: F(s, "Email").id, value: "ada@example.com" },
    { field_id: F(s, "Phone").id, value: "+1 415-555-0100" },
    { field_id: loc.id, value: "San Francisco, California, United States" },
    { field_id: F(s, "LinkedIn Profile").id, value: "https://linkedin.com/in/ada" },
    { field_id: auth.id, value: "Yes" },
    { field_id: F(s, "Will you now or in the future require sponsorship").id, value: "No" },
    { field_id: F(s, "How did you hear about this job?").id, value: "LinkedIn" },
    { field_id: why.id, value: "I love robots." },
    { field_id: gender.id, value: "decline to self identify" },
    { field_id: F(s, "Are you Hispanic/Latino?").id, value: "No" },
    { field_id: vet.id, value: "I am not a protected veteran" },
    { field_id: F(s, "Disability Status").id, value: "I do not want to answer" }
  ]);
  assert.equal(res[4].actual, "San Francisco, California, United States");
  assert.equal(res[10].actual, "Decline To Self Identify");
  const up = await engine.upload(resume.id, FILE);
  assert.deepEqual(up, { ok: true, files: ["resume.pdf"] });

  s = engine.scan({});
  assert.equal(F(s, "Location (City)").value, "San Francisco, California, United States");
  assert.equal(F(s, "Are you legally authorized to work in the United States?").value, "Yes");
  assert.deepEqual(F(s, "Resume/CV").files, ["resume.pdf"]);

  // combobox with no matching option -> NO_OPTION_MATCH with options
  const bad = await engine.fill([{ field_id: auth.id, value: "Maybe" }]);
  assert.equal(bad.results[0].ok, false);
  assert.equal(bad.results[0].error.code, "NO_OPTION_MATCH");
  // select with no match
  const bad2 = await engine.fill([{ field_id: gender.id, value: "Robot" }]);
  assert.equal(bad2.results[0].error.code, "NO_OPTION_MATCH");
  assert.deepEqual(bad2.results[0].options.map(o => o.label), ["Male", "Female", "Decline To Self Identify"]);
  const missing = await engine.fill([{ field_id: "99999", value: "x" }]);
  assert.equal(missing.results[0].error.code, "ELEMENT_NOT_FOUND");

  await engine.click(B(s, "Submit Application").id);
  await h.waitFor(() => subsFor("greenhouse").length === 1, 4000);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "confirmation");
  assert.equal(s.confirmation.detected, true);
  assert.match(s.confirmation.text, /thank you for applying/i);
  await h.sleep(200);
  assert.equal(subsFor("greenhouse").length, 1);
  const data = subsFor("greenhouse")[0].data;
  assert.equal(data.first_name, "Ada");
  assert.equal(data.location, "San Francisco, California, United States");
  assert.equal(data.work_authorization, "Yes");
  assert.equal(data.sponsorship, "No");
  assert.deepEqual(data.resume, ["resume.pdf"]);
  assert.equal(data.gender, "Decline To Self Identify");
  assert.deepEqual(pg.errors, []);
  pg.close();
});

test("lever: job page -> apply link, radio + checkbox group, invisible hCaptcha is not a captcha", async () => {
  let pg = await h.loadPage(srv.base + "/lever/index.html");
  let s = pg.engine.scan({});
  assert.equal(s.ats, "lever");
  assert.equal(s.page_kind, "job_description");
  const apply = B(s, "Apply for this job");
  assert.equal(apply.kind, "apply");
  const c = await pg.engine.click(apply.id);
  assert.equal(c.ok, true);
  assert.equal(c.href, srv.base + "/lever/apply.html");
  pg.close();

  pg = await h.loadPage(c.href);
  const { engine } = pg;
  s = engine.scan({});
  assert.equal(s.page_kind, "form");
  assert.equal(s.captcha.interactive, false);
  assert.notEqual(s.page_kind, "captcha");
  const age = F(s, "Are you at least 18 years of age?");
  assert.equal(age.type, "radio"); assert.equal(age.required, true);
  assert.deepEqual(age.options.map(o => o.label), ["Yes", "No"]);
  const offices = F(s, "Which offices are you interested in?");
  assert.equal(offices.type, "checkbox_group");
  assert.deepEqual(offices.options.map(o => o.label), ["New York", "San Francisco", "Remote"]);
  assert.equal(F(s, "Full name").required, true);
  assert.equal(F(s, "Gender").section, "U.S. Equal Employment Opportunity information");

  await fillOk(engine, [
    { field_id: F(s, "Full name").id, value: "Ada Lovelace" },
    { field_id: F(s, "Email").id, value: "ada@example.com" },
    { field_id: F(s, "Current company").id, value: "Analytical Engines" },
    { field_id: F(s, "GitHub URL").id, value: "https://github.com/ada" },
    { field_id: age.id, value: "yes" },
    { field_id: offices.id, value: ["Remote", "New York"] },
    { field_id: F(s, "Additional information").id, value: "Thanks!" },
    { field_id: F(s, "Veteran status").id, value: "I am not a veteran" }
  ]);
  // re-fill group with a different exact set
  await fillOk(engine, [{ field_id: offices.id, value: ["Remote"] }]);
  await engine.upload(F(s, "Resume/CV").id, FILE);
  s = engine.scan({});
  assert.equal(F(s, "Are you at least 18 years of age?").value, "Yes");
  assert.deepEqual(F(s, "Which offices are you interested in?").value, ["Remote"]);
  const bad = await engine.fill([{ field_id: offices.id, value: ["Mars"] }]);
  assert.equal(bad.results[0].error.code, "NO_OPTION_MATCH");

  await engine.click(B(s, "Submit application").id);
  await h.waitFor(() => subsFor("lever").length === 1);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "confirmation");
  const data = subsFor("lever")[0].data;
  assert.equal(data["cards[abc][field0]"], "Yes");
  assert.deepEqual(data["cards[abc][field1]"], ["Remote"]);
  assert.deepEqual(data.resume, ["resume.pdf"]);
  assert.deepEqual(pg.errors, []);
  pg.close();
});

test("ashby: SPA apply, yes/no toggles, location autocomplete, dropzone upload, word-limit error", async () => {
  const pg = await h.loadPage(srv.base + "/ashby/index.html");
  const { engine } = pg;
  let s = engine.scan({});
  assert.equal(s.ats, "ashby");
  assert.equal(s.page_kind, "job_description");
  await engine.click(B(s, "Apply for this Job").id);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "form");
  const spons = F(s, "Will you require visa sponsorship?");
  assert.equal(spons.type, "radio"); assert.equal(spons.hint, "ashby:yesno"); assert.equal(spons.required, true);
  assert.deepEqual(spons.options.map(o => o.label), ["Yes", "No"]);
  const loc = F(s, "Location");
  assert.equal(loc.type, "combobox"); assert.equal(loc.required, true);
  const project = F(s, "Tell us about a robotics project you are proud of.");
  assert.match(project.description, /300 words/);
  const resume = F(s, "Resume");
  assert.equal(resume.type, "file");
  // yes/no buttons are not reported as page buttons
  assert.ok(!s.buttons.some(b => b.text === "Yes"));

  await fillOk(engine, [
    { field_id: F(s, "Name").id, value: "Ada Lovelace" },
    { field_id: F(s, "Email").id, value: "ada@example.com" },
    { field_id: loc.id, value: "Pittsburgh, Pennsylvania, United States" },
    { field_id: spons.id, value: "No" },
    { field_id: F(s, "Are you comfortable working in a hybrid environment").id, value: "Yes" },
    { field_id: project.id, value: Array(320).fill("word").join(" ") }
  ]);
  await engine.upload(resume.id, FILE);
  s = engine.scan({});
  assert.equal(F(s, "Will you require visa sponsorship?").value, "No");
  await engine.click(B(s, "Submit Application").id);
  await settle(engine);
  s = engine.scan({});
  assert.equal(subsFor("ashby").length, 0);
  assert.ok(s.errors.some(e => /300 words/.test(e.text) && e.field_id === project.id), JSON.stringify(s.errors));
  await fillOk(engine, [{ field_id: project.id, value: "A small walking robot." }]);
  await engine.click(B(s, "Submit Application").id);
  await h.waitFor(() => subsFor("ashby").length === 1);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "confirmation");
  const data = subsFor("ashby")[0].data;
  assert.equal(data.sponsorship, "No");
  assert.equal(data.hybrid, "Yes");
  assert.equal(data.location, "Pittsburgh, Pennsylvania, United States");
  assert.deepEqual(data._systemfield_resume, ["resume.pdf"]);
  assert.deepEqual(pg.errors, []);
  pg.close();
});

test("workday: full multi-step walk-through using only engine calls", async () => {
  const pg = await h.loadPage(srv.base + "/workday/index.html");
  const { engine } = pg;
  let s = engine.scan({});
  assert.equal(s.ats, "workday");
  assert.equal(s.page_kind, "job_description");
  await engine.click(B(s, "Apply").id);
  await settle(engine);
  s = engine.scan({});
  const manual = B(s, "Apply Manually");
  assert.equal(manual.kind, "apply");
  await engine.click(manual.id);
  await settle(engine);

  // Create account
  s = engine.scan({});
  assert.equal(s.page_kind, "signup");
  assert.equal(s.signals.has_password, true);
  const pw = s.fields.filter(f => f.type === "password");
  assert.equal(pw.length, 2);
  assert.ok(pw.every(f => f.value === null));
  await fillOk(engine, [
    { field_id: F(s, "Email Address").id, value: "ada@example.com" },
    { field_id: F(s, "Password").id, value: "Secret#123" },
    { field_id: F(s, "Verify New Password").id, value: "Secret#124" },
    { field_id: F(s, "Yes, I have read and consent to the terms and conditions").id, value: true }
  ]);
  const createBtn = s.buttons.find(b => b.text === "Create Account" && b.kind === "signup");
  assert.ok(createBtn);
  await engine.click(createBtn.id);
  await settle(engine);
  s = engine.scan({});
  assert.ok(s.errors.some(e => /Passwords do not match/.test(e.text)), JSON.stringify(s.errors));
  const fillRes = await engine.fill([{ field_id: F(s, "Verify New Password").id, value: "Secret#123" }]);
  assert.equal(fillRes.results[0].ok, true);
  assert.equal("actual" in fillRes.results[0], false, "password must never be echoed");
  await engine.click(createBtn.id);
  await settle(engine);

  // Sign in
  s = engine.scan({});
  assert.equal(s.page_kind, "login");
  await fillOk(engine, [
    { field_id: F(s, "Email Address").id, value: "ada@example.com" },
    { field_id: F(s, "Password").id, value: "wrong" }
  ]);
  await engine.click(B(s, "Sign In").id);
  await settle(engine);
  s = engine.scan({});
  assert.ok(s.errors.some(e => /Wrong email address or password/.test(e.text)));
  await fillOk(engine, [{ field_id: F(s, "Password").id, value: "Secret#123" }]);
  await engine.click(s.buttons.find(b => b.text === "Sign In" && b.kind === "login").id);
  await settle(engine);

  // Step 1 My Information
  s = engine.scan({});
  assert.equal(s.page_kind, "form");
  assert.deepEqual([s.step.current, s.step.total], [1, 6]);
  assert.match(s.step.label, /My Information/);
  const source = F(s, "How Did You Hear About Us?");
  assert.equal(source.type, "combobox"); assert.equal(source.hint, "workday:prompt"); assert.equal(source.required, true);
  const state = F(s, "State");
  assert.equal(state.type, "combobox"); assert.equal(state.hint, "workday:dropdown"); assert.equal(state.required, true);
  assert.equal(F(s, "Country").value, "United States of America");
  const prev = F(s, "Have you previously worked for Fixture Corp?");
  assert.equal(prev.type, "radio");
  assert.equal(B(s, "Save and Continue").kind, "next");
  // validation first
  await engine.click(B(s, "Save and Continue").id);
  await settle(engine);
  s = engine.scan({});
  assert.ok(s.errors.length >= 5);
  assert.ok(s.errors.some(e => /First Name is required/.test(e.text) && e.field_id === F(s, "First Name").id), JSON.stringify(s.errors.slice(0, 4)));
  await fillOk(engine, [
    { field_id: source.id, value: "LinkedIn" },
    { field_id: prev.id, value: "No" },
    { field_id: F(s, "First Name").id, value: "Ada" },
    { field_id: F(s, "Last Name").id, value: "Lovelace" },
    { field_id: F(s, "Address Line 1").id, value: "1 Main St" },
    { field_id: F(s, "City").id, value: "San Francisco" },
    { field_id: state.id, value: "California" },
    { field_id: F(s, "Postal Code").id, value: "94105" },
    { field_id: F(s, "Phone Device Type").id, value: "Mobile" },
    { field_id: F(s, "Phone Number").id, value: "4155550100" }
  ]);
  s = engine.scan({});
  assert.equal(F(s, "How Did You Hear About Us?").value, "LinkedIn");
  assert.equal(F(s, "State").value, "California");
  const noMatch = await engine.fill([{ field_id: state.id, value: "Ontario" }]);
  assert.equal(noMatch.results[0].error.code, "NO_OPTION_MATCH");
  assert.ok(noMatch.results[0].options.some(o => o.label === "Texas"));
  s = engine.scan({});
  assert.equal(F(s, "State").value, "California");
  await engine.click(B(s, "Save and Continue").id);
  await settle(engine);

  // Step 2 My Experience
  s = engine.scan({});
  assert.equal(s.step.current, 2);
  const file = s.fields.find(f => f.type === "file");
  assert.ok(file, "resume input");
  await engine.upload(file.id, FILE);
  await fillOk(engine, [{ field_id: F(s, "LinkedIn").id, value: "https://linkedin.com/in/ada" }]);
  await engine.click(B(s, "Save and Continue").id);
  await settle(engine);

  // Step 3 Application Questions
  s = engine.scan({});
  assert.equal(s.step.current, 3);
  await fillOk(engine, [
    { field_id: F(s, "Are you legally authorized to work in the country in which the job is located?").id, value: "Yes" },
    { field_id: F(s, "Will you now or in the future require sponsorship for employment visa status?").id, value: "No" },
    { field_id: F(s, "Please describe why you are interested in this role.").id, value: "Robots." }
  ]);
  await engine.click(B(s, "Save and Continue").id);
  await settle(engine);

  // Step 4 Voluntary Disclosures
  s = engine.scan({});
  assert.equal(s.step.current, 4);
  await fillOk(engine, [
    { field_id: F(s, "Gender").id, value: "I do not wish to answer" },
    { field_id: F(s, "Please select the veteran status").id, value: "I am not a veteran" },
    { field_id: F(s, "Yes, I have read and consent to the terms and conditions").id, value: true }
  ]);
  await engine.click(B(s, "Save and Continue").id);
  await settle(engine);

  // Step 5 Self Identify
  s = engine.scan({});
  assert.equal(s.step.current, 5);
  const date = F(s, "Date");
  assert.equal(date.type, "date");
  assert.equal(date.hint, "text-date:MM/DD/YYYY");
  const dis = F(s, "Please check one of the boxes below:");
  assert.equal(dis.type, "checkbox_group"); assert.equal(dis.required, true);
  const r = await fillOk(engine, [
    { field_id: F(s, "Name").id, value: "Ada Lovelace" },
    { field_id: date.id, value: "2026-10-03" },
    { field_id: dis.id, value: ["I do not want to answer"] }
  ]);
  assert.equal(r[1].actual, "10/03/2026");
  await engine.click(B(s, "Save and Continue").id);
  await settle(engine);

  // Step 6 Review
  s = engine.scan({});
  assert.equal(s.step.current, 6);
  const submit = B(s, "Submit");
  assert.equal(submit.kind, "submit");
  assert.equal(s.signals.has_submit, true);
  await engine.click(submit.id);
  await h.waitFor(() => subsFor("workday").length === 1);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "confirmation");
  const data = subsFor("workday")[0].data;
  assert.equal(data.source, "LinkedIn");
  assert.equal(data.state, "California");
  assert.equal(data.previousWorker, "No");
  assert.equal(data.disabilityDate, "10/03/2026");
  assert.equal(data.disabilityStatus, "I do not want to answer");
  assert.deepEqual(data.resume, ["resume.pdf"]);
  assert.equal(JSON.stringify(data).includes("Secret#123"), false);
  assert.deepEqual(pg.errors, []);
  pg.close();
});

test("icims: form inside a same-origin iframe", async () => {
  const pg = await h.loadPage(srv.base + "/icims/index.html");
  const outer = pg.engine.scan({});
  assert.equal(outer.ats, "icims");
  assert.equal(outer.fields.length, 0);
  const iframe = pg.doc.getElementById("icims_content_iframe");
  await h.waitFor(() => iframe.contentDocument && iframe.contentDocument.readyState === "complete" && iframe.contentDocument.getElementById("email"));
  const engine = h.engineFor(iframe.contentDocument);
  let s = engine.scan({});
  assert.equal(s.ats, "icims");
  await fillOk(engine, [{ field_id: F(s, "Email Address").id, value: "ada@example.com" }]);
  await engine.click(B(s, "Next").id);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "form");
  assert.equal(F(s, "Email").disabled, true);
  await fillOk(engine, [
    { field_id: F(s, "First Name").id, value: "Ada" },
    { field_id: F(s, "Last Name").id, value: "Lovelace" },
    { field_id: F(s, "Phone Number").id, value: "415-555-0100" },
    { field_id: F(s, "Address").id, value: "1 Main St" },
    { field_id: F(s, "City").id, value: "Seattle" },
    { field_id: F(s, "State/Province").id, value: "Washington" },
    { field_id: F(s, "Zip/Postal Code").id, value: "98101" },
    { field_id: F(s, "Are you at least 18 years of age?").id, value: "Yes" },
    { field_id: F(s, "Highest level of education").id, value: "Bachelor's Degree" },
    { field_id: F(s, "Gender").id, value: "I do not wish to self-identify" }
  ]);
  await engine.upload(F(s, "Resume").id, FILE);
  await engine.click(B(s, "Submit Application").id);
  await h.waitFor(() => subsFor("icims").length === 1);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "confirmation");
  const data = subsFor("icims")[0].data;
  assert.equal(data.state, "WA");
  assert.equal(data.email, "ada@example.com");
  assert.deepEqual(pg.errors, []);
  pg.close();
});

test("smartrecruiters: open shadow DOM inputs, email confirmation error, submit", async () => {
  let pg = await h.loadPage(srv.base + "/smartrecruiters/index.html");
  let s = pg.engine.scan({});
  assert.equal(s.page_kind, "job_description");
  const c = await pg.engine.click(B(s, "I'm interested").id);
  assert.equal(c.href, srv.base + "/smartrecruiters/apply.html");
  pg.close();
  pg = await h.loadPage(c.href);
  const { engine } = pg;
  s = engine.scan({});
  const fn = F(s, "First name");
  assert.equal(fn.required, true);
  await fillOk(engine, [
    { field_id: fn.id, value: "Ada" },
    { field_id: F(s, "Last name").id, value: "Lovelace" },
    { field_id: F(s, "Email").id, value: "ada@example.com" },
    { field_id: F(s, "Confirm your email").id, value: "ada@example.org" },
    { field_id: F(s, "Phone number").id, value: "+1 415 555 0100" },
    { field_id: F(s, "What is your highest level of education?").id, value: "PhD" },
    { field_id: F(s, "Are you willing to travel up to 25% of the time?").id, value: "Yes" },
    { field_id: F(s, "I agree to the privacy policy").id, value: true }
  ]);
  await engine.upload(F(s, "Resume").id, FILE);
  await engine.click(B(s, "Submit").id);
  await settle(engine);
  s = engine.scan({});
  assert.equal(subsFor("smartrecruiters").length, 0);
  assert.ok(s.errors.some(e => /Emails do not match/.test(e.text) && e.field_id === F(s, "Confirm your email").id), JSON.stringify(s.errors));
  await fillOk(engine, [{ field_id: F(s, "Confirm your email").id, value: "ada@example.com" }]);
  await engine.click(B(s, "Submit").id);
  await h.waitFor(() => subsFor("smartrecruiters").length === 1);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "confirmation");
  const data = subsFor("smartrecruiters")[0].data;
  assert.equal(data.firstName, "Ada");
  assert.equal(data.travel, "Yes");
  assert.equal(data.privacyConsent, true);
  assert.deepEqual(pg.errors, []);
  pg.close();
});

test("custom: multi-step with number/date/month/radio/checkbox group/consent and field validation", async () => {
  const pg = await h.loadPage(srv.base + "/custom/index.html");
  const { engine } = pg;
  let s = engine.scan({});
  assert.equal(s.ats, "generic");
  await fillOk(engine, [
    { field_id: F(s, "First Name").id, value: "Ada" },
    { field_id: F(s, "Last Name").id, value: "Lovelace" },
    { field_id: F(s, "Email").id, value: "ada@example.com" },
    { field_id: F(s, "Phone").id, value: "555-12" },
    { field_id: F(s, "Years of professional experience").id, value: 7 }
  ]);
  await engine.click(B(s, "Next").id);
  await settle(engine);
  s = engine.scan({});
  const phoneErr = s.errors.find(e => /valid phone number/.test(e.text));
  assert.ok(phoneErr, JSON.stringify(s.errors));
  assert.equal(phoneErr.field_id, F(s, "Phone").id);
  assert.equal(F(s, "Phone").error, "Please enter a valid phone number (e.g. 555-555-5555).");
  await fillOk(engine, [{ field_id: F(s, "Phone").id, value: "555-555-5555" }]);
  await engine.click(B(s, "Next").id);
  await settle(engine);
  s = engine.scan({});
  assert.equal(F(s, "Graduation date").type, "date");
  assert.equal(F(s, "Available start month").type, "month");
  const r = await fillOk(engine, [
    { field_id: F(s, "Highest degree").id, value: "Master's" },
    { field_id: F(s, "Graduation date").id, value: "2019-05-20" },
    { field_id: F(s, "Available start month").id, value: "2026-11" },
    { field_id: F(s, "Did you graduate with honors?").id, value: true }
  ]);
  assert.equal(r[1].actual, "2019-05-20");
  assert.equal(r[3].actual, "Yes");
  await engine.click(B(s, "Next").id);
  await settle(engine);
  s = engine.scan({});
  const langs = F(s, "Programming languages");
  assert.equal(langs.type, "checkbox_group"); assert.equal(langs.required, true);
  const certify = F(s, "I certify that the information provided is true");
  assert.equal(certify.type, "checkbox"); assert.equal(certify.required, true);
  assert.equal(F(s, "Cover letter").max_length, 500);
  assert.equal(B(s, "Back").kind, "back");
  assert.equal(B(s, "Review").kind, "next");
  const long = "x".repeat(600);
  const cl = await engine.fill([{ field_id: F(s, "Cover letter").id, value: long }]);
  assert.equal(cl.results[0].ok, true);
  assert.equal(cl.results[0].actual.length, 500);
  await fillOk(engine, [
    { field_id: langs.id, value: ["Python", "Rust"] },
    { field_id: certify.id, value: true }
  ]);
  await engine.click(B(s, "Review").id);
  await settle(engine);
  s = engine.scan({});
  await engine.click(B(s, "Submit").id);
  await h.waitFor(() => subsFor("custom").length === 1);
  await settle(engine);
  assert.equal(engine.scan({}).page_kind, "confirmation");
  const data = subsFor("custom")[0].data;
  assert.equal(data.years_experience, "7");
  assert.equal(data.graduation_date, "2019-05-20");
  assert.equal(data.start_month, "2026-11");
  assert.equal(data.honors, "Yes");
  assert.deepEqual(data.languages, ["Python", "Rust"]);
  assert.equal(data.certify, true);
  assert.deepEqual(pg.errors, []);
  pg.close();
});

test("validation: server-style rejection is extracted, fixing the phone succeeds", async () => {
  const pg = await h.loadPage(srv.base + "/validation/index.html");
  const { engine } = pg;
  let s = engine.scan({});
  await fillOk(engine, [
    { field_id: F(s, "Full Name").id, value: "Ada Lovelace" },
    { field_id: F(s, "Email").id, value: "ada@example.com" },
    { field_id: F(s, "Phone").id, value: "555-555-1234" }
  ]);
  await engine.click(B(s, "Submit Application").id);
  await settle(engine);
  s = engine.scan({});
  assert.equal(subsFor("validation").length, 0);
  assert.equal(s.page_kind, "form");
  assert.ok(s.errors.some(e => /There were problems with your submission/.test(e.text)));
  const pe = s.errors.find(e => /format \+1 555-555-5555/.test(e.text));
  assert.ok(pe);
  assert.equal(pe.field_id, F(s, "Phone").id);
  await fillOk(engine, [{ field_id: F(s, "Phone").id, value: "+1 555-555-1234" }]);
  await engine.click(B(s, "Submit Application").id);
  await h.waitFor(() => subsFor("validation").length === 1);
  await settle(engine);
  assert.equal(engine.scan({}).page_kind, "confirmation");
  pg.close();
});

test("captcha: visible challenge detected, invisible badge ignored, resumes after solve", async () => {
  const pg = await h.loadPage(srv.base + "/captcha/index.html");
  const { engine } = pg;
  let s = engine.scan({});
  assert.equal(s.page_kind, "form");
  assert.equal(s.captcha.interactive, false);
  await fillOk(engine, [
    { field_id: F(s, "Name").id, value: "Ada" },
    { field_id: F(s, "Email").id, value: "ada@example.com" }
  ]);
  await engine.click(B(s, "Submit Application").id);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "captcha");
  assert.deepEqual([s.captcha.present, s.captcha.interactive, s.captcha.kind], [true, true, "recaptcha"]);
  assert.equal(subsFor("captcha").length, 0);
  // the engine refuses to work inside the captcha frame itself
  const frame = pg.doc.querySelector(".g-recaptcha iframe");
  await h.waitFor(() => frame.contentDocument && frame.contentDocument.readyState === "complete");
  const fe = h.engineFor(frame.contentDocument);
  const fs = fe.scan({});
  assert.equal(fs.page_kind, "captcha");
  assert.equal(fs.fields.length, 0);
  await assert.rejects(fe.click("1"), e => e.code === "UNSUPPORTED");
  pg.win.FixtureKit.solveCaptcha();
  s = engine.scan({});
  assert.equal(s.page_kind, "form");
  await engine.click(B(s, "Submit Application").id);
  await h.waitFor(() => subsFor("captcha").length === 1);
  await settle(engine);
  assert.equal(engine.scan({}).page_kind, "confirmation");
  pg.close();
});

test("captcha: vendor-neutral human-verification page is detected without help-copy false positives", () => {
  const challenged = new JSDOM(
    '<!doctype html><title>Security verification</title><main><h1>Robot check</h1>' +
    '<div id="security-captcha" role="alert">Please verify that you are a human</div></main>',
    { url: 'https://jobs.example/challenge', pretendToBeVisual: true });
  let s = h.engineFor(challenged.window.document).scan({});
  assert.equal(s.page_kind, "captcha");
  assert.equal(s.captcha.interactive, true);
  challenged.window.close();

  const ordinary = new JSDOM(
    '<!doctype html><title>Application</title><main><h1>Apply</h1>' +
    '<p>Our privacy policy explains automated abuse prevention.</p>' +
    '<form><label>Name <input name="name" required></label><button type="submit">Submit</button></form></main>',
    { url: 'https://jobs.example/apply', pretendToBeVisual: true });
  s = h.engineFor(ordinary.window.document).scan({});
  assert.equal(s.page_kind, "form");
  assert.equal(s.captcha.interactive, false);
  ordinary.window.close();
});

test("captcha: an embedded hCaptcha widget on a login page is diagnostic, not blocking", () => {
  const login = new JSDOM(
    '<!doctype html><title>Sign in</title><main><h1>Sign in</h1><form>' +
    '<label>Email <input type="email" name="email"></label>' +
    '<label>Password <input type="password" name="password"></label>' +
    '<div class="h-captcha" data-sitekey="site-key"><iframe title="hCaptcha checkbox" ' +
    'src="https://newassets.hcaptcha.com/captcha/v1/anchor"></iframe></div>' +
    '<button type="submit">Sign in</button></form></main>',
    { url: 'https://careers.example/login', pretendToBeVisual: true });
  const s = h.engineFor(login.window.document).scan({});
  assert.equal(s.page_kind, "login");
  assert.equal(s.captcha.present, true);
  assert.equal(s.captcha.library_detected, true);
  assert.equal(s.captcha.widget_present, true);
  assert.equal(s.captcha.challenge_blocking, false);
  assert.equal(s.captcha.interactive, false);
  login.window.close();
});

test("login: passwordless iCIMS passkey prompt is authentication, not an unknown page", () => {
  const passkey = new JSDOM(
    '<!doctype html><title>Log in to Johns Hopkins APL</title><main>' +
    '<h1>Use fingerprint or face recognition to login | Applicant Tracking</h1>' +
    '<p>Press the button below and follow your browser steps to log in.</p>' +
    '<p>Awaiting device confirmation</p><button>Continue</button><button>Use password</button></main>',
    { url: 'https://login.icims.com/u/mfa-webauthn-platform-challenge?state=secret', pretendToBeVisual: true });
  const s = h.engineFor(passkey.window.document).scan({});
  assert.equal(s.page_kind, "login");
  assert.equal(s.fields.length, 0);
  assert.ok(s.buttons.some(b => b.text === "Use password"));
  assert.equal(s.captcha.interactive, false);
  passkey.window.close();
});

test("verification: signup -> code page -> application -> confirmation; link page", async () => {
  const pg = await h.loadPage(srv.base + "/verification/index.html");
  const { engine } = pg;
  let s = engine.scan({});
  assert.equal(s.page_kind, "signup");
  await fillOk(engine, [
    { field_id: F(s, "Email").id, value: "ada@example.com" },
    { field_id: F(s, "Password").id, value: "longpassword1" },
    { field_id: F(s, "Confirm Password").id, value: "longpassword1" }
  ]);
  await engine.click(B(s, "Create Account").id);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "verification");
  assert.equal(B(s, "Verify").kind, "verify");
  const code = F(s, "Verification code");
  await fillOk(engine, [{ field_id: code.id, value: "000000" }]);
  await engine.click(B(s, "Verify").id);
  await settle(engine);
  s = engine.scan({});
  assert.ok(s.errors.some(e => /Invalid verification code/.test(e.text)));
  await fillOk(engine, [{ field_id: code.id, value: "123456" }]);
  await engine.click(B(s, "Verify").id);
  await settle(engine);
  s = engine.scan({});
  assert.equal(s.page_kind, "form");
  assert.equal(F(s, "Email").value, "ada@example.com");
  await fillOk(engine, [{ field_id: F(s, "Full Name").id, value: "Ada Lovelace" }]);
  await engine.upload(F(s, "Resume").id, FILE);
  await engine.click(B(s, "Submit Application").id);
  await h.waitFor(() => subsFor("verification").length === 1);
  await settle(engine);
  assert.equal(engine.scan({}).page_kind, "confirmation");
  assert.deepEqual(Object.keys(subsFor("verification")[0].data).sort(), ["email", "name", "resume"]);
  pg.close();

  const link = await h.loadPage(srv.base + "/verification/link.html");
  assert.equal(link.engine.scan({}).page_kind, "verification");
  link.close();
});

test("closed, simplify, 404 and job description page kinds", async () => {
  let pg = await h.loadPage(srv.base + "/closed/index.html");
  assert.equal(pg.engine.scan({}).page_kind, "closed");
  pg.close();
  pg = await h.loadPage(srv.base + "/simplify/index.html");
  const s = pg.engine.scan({});
  assert.equal(s.ats, "simplify");
  assert.equal(s.page_kind, "job_description");
  assert.equal(s.ats_data.simplify.company, "Fixture Robotics");
  assert.equal(s.ats_data.simplify.apply_url, "../greenhouse/index.html");
  const c = await pg.engine.click(B(s, "Apply").id);
  assert.equal(c.href, srv.base + "/greenhouse/index.html");
  pg.close();
  const dom = new JSDOM("<title>404</title><h1>Page not found</h1><p>Sorry.</p>", { url: "https://example.com/x" });
  assert.equal(h.engineFor(dom.window.document).scan({}).page_kind, "closed");
});

function domFrom(html, url = "https://example.com/apply") {
  return new JSDOM(html, { url, pretendToBeVisual: true }).window;
}

test("button classification", () => {
  const win = domFrom(`
    <button>Submit Application</button><button>Send application</button><button>Next</button>
    <button>Save and Continue</button><button>Review</button><button>Back</button>
    <a href="/apply">Apply now</a><button>I'm interested</button><button>Apply Manually</button>
    <button>Sign In</button><button>Log in</button><button>Create Account</button><button>Register</button>
    <button>Verify</button><button>+ Add another</button><button>Add</button><button>Cancel</button>
    <button hidden>Hidden submit</button><div style="display:none"><button>Also hidden</button></div>
    <a href="/about">About us</a><button disabled>Continue</button>`);
  const s = h.engineFor(win.document).scan({});
  const kinds = Object.fromEntries(s.buttons.map(b => [b.text, b.kind]));
  assert.deepEqual(kinds, {
    "Submit Application": "submit", "Send application": "submit", "Next": "next", "Save and Continue": "next",
    "Review": "next", "Back": "back", "Apply now": "apply", "I'm interested": "apply", "Apply Manually": "apply",
    "Sign In": "login", "Log in": "login", "Create Account": "signup", "Register": "signup", "Verify": "verify",
    "+ Add another": "add", "Add": "add", "Cancel": "other", "Continue": "next"
  });
  assert.equal(s.buttons.find(b => b.text === "Continue").disabled, true);
});

test("labels, required markers, hidden fields and honeypots", () => {
  const win = domFrom(`
    <h2>Contact</h2>
    <div><span id="lb">Preferred name</span><input aria-labelledby="lb"></div>
    <label>Middle name (required) <input name="middle"></label>
    <input aria-label="Nickname" required>
    <div class="question"><div class="question-text">Your favourite colour? *</div><div><input name="colour"></div></div>
    <input placeholder="Postcode">
    <fieldset><legend>Date of birth</legend><input name="dob" placeholder="MM/DD/YYYY"></fieldset>
    <input type="hidden" name="secret"><input name="h1" hidden><div style="display:none"><input name="h2"></div>
    <div aria-hidden="true"><input name="h3"></div>
    <input name="website" tabindex="-1" style="position:absolute;left:-5000px">
    <label for="hp">Leave this field blank</label><input id="hp" name="contact_me_by_fax_only">
    <h2>Preferences</h2>
    <div role="radiogroup" aria-label="Shift"><div role="radio" aria-checked="false">Day</div><div role="radio" aria-checked="false">Night</div></div>
    <select name="size"><option value="">Select...</option><option value="s">Small</option><option value="l">Large</option></select>`);
  const s = h.engineFor(win.document).scan({});
  const labels = s.fields.map(f => f.label);
  assert.deepEqual(labels, ["Preferred name", "Middle name", "Nickname", "Your favourite colour?", "Postcode", "Date of birth", "Shift", "Size"]);
  const byLabel = Object.fromEntries(s.fields.map(f => [f.label, f]));
  assert.equal(byLabel["Middle name"].required, true);
  assert.equal(byLabel["Nickname"].required, true);
  assert.equal(byLabel["Your favourite colour?"].required, true);
  assert.equal(byLabel["Preferred name"].required, false);
  assert.equal(byLabel["Preferred name"].section, "Contact");
  assert.equal(byLabel["Shift"].section, "Preferences");
  assert.equal(byLabel["Date of birth"].type, "date");
  assert.equal(byLabel["Shift"].type, "radio");
  assert.deepEqual(byLabel["Size"].options.map(o => o.label), ["Small", "Large"]);
});

test("aria radio fill, select by value, extract_text, click_text, click_point guard, wait_quiet", async () => {
  const win = domFrom(`<h1>Hello</h1><p>Visible text</p><p hidden>secret hidden</p>
    <div role="radiogroup" aria-label="Shift"><div role="radio" aria-checked="false" tabindex="0">Day</div><div role="radio" aria-checked="false" tabindex="0">Night</div></div>
    <select aria-label="Size"><option value="">Select...</option><option value="s">Small</option><option value="l">Large</option></select>
    <a href="/next-page">Continue to step two</a>
    <script></script>`);
  win.document.querySelectorAll("[role=radio]").forEach(r => r.addEventListener("click", () => {
    win.document.querySelectorAll("[role=radio]").forEach(x => x.setAttribute("aria-checked", String(x === r)));
  }));
  const engine = h.engineFor(win.document);
  const s = engine.scan({ include_text: true, max_text: 100 });
  assert.match(s.text, /Visible text/);
  assert.doesNotMatch(s.text, /secret hidden/);
  const shift = s.fields.find(f => f.label === "Shift");
  const size = s.fields.find(f => f.label === "Size");
  const r = await engine.fill([{ field_id: shift.id, value: "Night" }, { field_id: size.id, value: "l" }]);
  assert.deepEqual(r.results.map(x => [x.ok, x.actual]), [[true, "Night"], [true, "Large"]]);
  const t = engine.extractText(1000);
  assert.match(t.text, /Hello Visible text/);
  const ct = await engine.clickText("continue to step", false);
  assert.equal(ct.ok, true);
  assert.equal(ct.href, "https://example.com/next-page");
  await assert.rejects(engine.clickText("nonexistent words", true), e => e.code === "ELEMENT_NOT_FOUND");
  await assert.rejects(engine.clickPoint(1, 1), e => e.code === "UNSUPPORTED" || e.code === "ELEMENT_NOT_FOUND");
  setTimeout(() => win.document.body.appendChild(win.document.createElement("div")), 50);
  const q = await engine.waitQuiet(200, 2000);
  assert.equal(q.quiet, true);
  assert.ok(q.waited_ms >= 200);
  const iv = setInterval(() => win.document.body.appendChild(win.document.createElement("span")), 20);
  const q2 = await engine.waitQuiet(200, 500);
  clearInterval(iv);
  assert.equal(q2.quiet, false);
});

test("custom-styled radios/checkboxes with hidden inputs are kept and filled via label", async () => {
  const win = domFrom(`<style>.vh{display:none}</style>
    <fieldset><legend>Remote OK?</legend>
      <input class="vh" type="radio" name="r" id="r1" value="y"><label for="r1">Yes</label>
      <input class="vh" type="radio" name="r" id="r2" value="n"><label for="r2">No</label></fieldset>
    <label><input class="vh" type="checkbox" name="agree"> I agree</label>
    <div style="display:none"><input type="radio" name="gone" id="g"><label for="g">Gone</label></div>`);
  const engine = h.engineFor(win.document);
  const s = engine.scan({});
  assert.deepEqual(s.fields.map(f => [f.label, f.type]), [["Remote OK?", "radio"], ["I agree", "checkbox"]]);
  const r = await engine.fill([{ field_id: s.fields[0].id, value: "No" }, { field_id: s.fields[1].id, value: true }]);
  assert.ok(r.results.every(x => x.ok), JSON.stringify(r));
  assert.equal(win.document.getElementById("r2").checked, true);
});
