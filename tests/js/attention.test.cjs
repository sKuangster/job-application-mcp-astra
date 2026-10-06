"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const attention = require("../../firefox/background/attention.js");

test("CAPTCHA desktop notification identifies the application and supports reminders", () => {
  const item = {
    application_id: "app-1", kind: "captcha", company: "Acme", title: "SWE Intern",
    reason: "reCAPTCHA detected", instructions: "Complete it in Firefox.", since: 100,
    attention_revision: 0
  };
  const n = attention.notification(item, "moz-extension://icon.png");
  assert.equal(n.type, "basic");
  assert.match(n.title, /^CAPTCHA REQUIRED: Acme — SWE Intern$/);
  assert.match(n.message, /Complete it in Firefox/);
  const first = attention.key(item);
  item.attention_revision = 1;
  assert.notEqual(attention.key(item), first);
});
