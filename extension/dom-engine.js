(function (root) {
  "use strict";
  const clean = value => String(value ?? "").replace(/\s+/g, " ").trim();
  const norm = value => clean(value).toLowerCase();
  const fail = (code, message) => Object.assign(new Error(message), {code});
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  function createEngine(document, adapters = root.ApplicationAdapters) {
    const win = document.defaultView;
    const ids = new WeakMap(), elements = new Map();
    let nextId = 1;
    function id(element) {
      if (!ids.has(element)) {const key = `element-${nextId++}`; ids.set(element, key); elements.set(key, element);}
      return ids.get(element);
    }
    function roots() {
      const result = [document];
      for (let i = 0; i < result.length; i++) for (const e of result[i].querySelectorAll("*")) if (e.shadowRoot) result.push(e.shadowRoot);
      return result;
    }
    function all(selector) {return roots().flatMap(r => [...r.querySelectorAll(selector)]);}
    function visible(e) {
      for (let node = e; node && node.nodeType === 1; node = node.parentElement || node.getRootNode().host) {
        if (node.hidden || node.getAttribute("aria-hidden") === "true" || node.hasAttribute("inert")) return false;
        const style = win.getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false;
      }
      return e.type !== "hidden";
    }
    function text() {
      const chunks = [];
      for (const tree of roots()) {
        const walker = document.createTreeWalker(tree, win.NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          const p = walker.currentNode.parentElement;
          if (!p || p.closest("script, style, noscript, template") || !visible(p)) continue;
          chunks.push(walker.currentNode.textContent);
          if (chunks.join(" ").length > 22000) break;
        }
      }
      return clean(chunks.join(" ")).slice(0, 20000);
    }
    function byLabelIds(e) {
      const tree = e.getRootNode();
      return (e.getAttribute("aria-labelledby") || "").split(/\s+/).map(key => tree.getElementById?.(key)?.textContent || "").join(" ");
    }
    function label(e) {
      return clean(e.getAttribute("aria-label") || byLabelIds(e) || [...(e.labels || [])].map(l => l.textContent).join(" ") || e.closest("label")?.textContent || e.getAttribute("placeholder") || e.getAttribute("name") || e.id).slice(0, 2000);
    }
    function radioGroup(e) {return all("input[type='radio']").filter(x => x.name === e.name && x.form === e.form && x.getRootNode() === e.getRootNode() && visible(x));}
    function options(e) {
      if (e.tagName === "SELECT") return [...e.options].filter(o => !o.disabled).map(o => ({value: o.value, label: clean(o.textContent)}));
      if (e.type === "radio") return radioGroup(e).map(r => ({value: r.value, label: label(r)}));
      if (e.getAttribute("role") === "combobox") {
        const target = e.getRootNode().getElementById?.(e.getAttribute("aria-controls") || "");
        return [...(target || e).querySelectorAll("[role='option']")].map(o => ({value: o.getAttribute("data-value") || clean(o.textContent), label: clean(o.textContent)}));
      }
      return [];
    }
    function read(e) {
      if (e.type === "password") return "";
      if (e.type === "file") return [...(e.files || [])].map(f => ({name: f.name, size: f.size, type: f.type}));
      if (e.type === "checkbox") return e.checked;
      if (e.type === "radio") return radioGroup(e).find(r => r.checked)?.value || "";
      if (e.tagName === "SELECT" && e.multiple) return [...e.selectedOptions].map(o => o.value);
      if ("value" in e) return e.value;
      return clean(e.getAttribute("data-value") || e.textContent);
    }
    function field(e) {
      const type = e.getAttribute("role") === "combobox" ? "combobox" : (e.type || (e.isContentEditable || e.hasAttribute("contenteditable") ? "textarea" : e.tagName.toLowerCase()));
      const legend = type === "radio" ? e.closest("fieldset")?.querySelector("legend")?.textContent : "";
      const nearby = clean(e.closest("fieldset, .field, .form-group, [data-automation-id]")?.textContent || "");
      const wordMatch = nearby.match(/(?:maximum|max|limit)\s*[: ]?\s*(\d+)\s*words|\b(\d+)\s*words?\s*(?:maximum|max|limit)/i);
      return {id: id(e), label: clean(legend || label(e)), type, required: !!e.required || e.getAttribute("aria-required") === "true" || (type === "radio" && radioGroup(e).some(r => r.required)), value: read(e), options: options(e), max_length: e.maxLength > 0 ? e.maxLength : null, max_words: wordMatch ? Number(wordMatch[1] || wordMatch[2]) : null, autocomplete: e.autocomplete || "", name: e.name || "", sensitive: type === "password", accept: e.accept || ""};
    }
    function buttonKind(e) {
      const title = norm(e.innerText || e.textContent || e.value || e.getAttribute("aria-label"));
      if (/^(?:verify|verify email|verify code|confirm email|confirm code)$/.test(title)) return "verify";
      if (/create\s+(?:an?\s+)?account|sign\s*up|register/.test(title)) return "create_account";
      if (/sign\s*in|log\s*in/.test(title)) return "login";
      if (/^(?:next|continue|save and continue|review|review application|next step)\b/.test(title)) return "next";
      if (/^(?:submit(?: application| my application)?|send application|finish application|complete application)$/.test(title)) return "submit";
      if (/^(?:apply|apply now|apply for this job|start application|apply with resume)$/.test(title)) return "apply";
      return null;
    }
    function hash(value) {let h = 2166136261; for (const c of value) {h ^= c.charCodeAt(0); h = Math.imul(h, 16777619);} return (h >>> 0).toString(16);}
    function inspect() {
      const adapter = adapters?.detect(document) || {name: "generic"};
      const found = all("input, textarea, select, [role='combobox'], [contenteditable='true']").filter(e => visible(e) && !e.disabled && !["submit", "button", "reset", "image", "hidden"].includes(e.type));
      const seenRadio = new Set();
      const fields = found.filter(e => {
        if (e.type !== "radio" || !e.name) return true;
        const key = radioGroup(e)[0]; if (seenRadio.has(key)) return false; seenRadio.add(key); return true;
      }).slice(0, 250).map(field);
      const buttons = all("button, input[type='submit'], input[type='button'], a[href], [role='button']").filter(e => visible(e) && !e.disabled && e.getAttribute("aria-disabled") !== "true").map(e => ({id: id(e), label: clean(e.innerText || e.textContent || e.value || e.getAttribute("aria-label")), kind: buttonKind(e)})).filter(b => b.kind);
      const pageText = text();
      const captcha = all("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='challenges.cloudflare'], .g-recaptcha, .h-captcha, [data-sitekey]").some(visible) || /verify (?:that )?you (?:are|’re|'re) (?:a )?human|complete the (?:security check|captcha)|checking your browser/i.test(pageText);
      const errorSelector = ["[role='alert']", "[aria-invalid='true']", ".validation-error", ".invalid-feedback", adapter.errors].filter(Boolean).join(",");
      const errors = all(errorSelector).filter(visible).map(e => clean(e.getAttribute("aria-errormessage") ? e.getRootNode().getElementById?.(e.getAttribute("aria-errormessage"))?.textContent : e.textContent || e.validationMessage || label(e))).filter(Boolean);
      const successMatch = pageText.match(/(?:your )?application (?:has been |was )?(?:successfully )?(?:submitted|received)|thank you for (?:applying|your application)|thanks for (?:applying|your application)/i);
      const successElement = adapter.success ? all(adapter.success).find(visible) : null;
      // Success evidence must not coexist with editable required application fields.
      const success = !!(successMatch || successElement) && !fields.some(f => f.required && f.type !== "checkbox");
      const stage = captcha ? "captcha" : success ? "success" : buttons.some(b => b.kind === "create_account") && fields.some(f => f.type === "password") ? "account" : buttons.some(b => b.kind === "login") && fields.some(f => f.type === "password") ? "login" : buttons.some(b => b.kind === "submit") && !buttons.some(b => b.kind === "next") ? "final" : buttons.some(b => b.kind === "verify") || fields.some(f => f.autocomplete === "one-time-code") ? "verification" : fields.length ? "form" : "job";
      const snapshot = {url: document.location.href, title: document.title, text: pageText, fields, buttons, captcha, validation_errors: [...new Set(errors)], success, success_evidence: success ? clean(successMatch?.[0] || successElement?.textContent) : "", stage, platform: adapter.name, unsupported: all("iframe").length ? ["Frames are inspected separately; inaccessible frames require manual review."] : []};
      snapshot.snapshot_id = hash(JSON.stringify({url: snapshot.url, fields: fields.map(f => [f.id, f.value, f.required]), buttons, captcha, stage}));
      return snapshot;
    }
    function get(key) {const e = elements.get(key); if (!e?.isConnected || !visible(e) || e.disabled) throw fail("STALE_FIELD", "The inspected element is no longer available"); return e;}
    function emit(e) {e.dispatchEvent(new win.Event("input", {bubbles: true, composed: true})); e.dispatchEvent(new win.Event("change", {bubbles: true, composed: true})); e.dispatchEvent(new win.Event("blur", {bubbles: true, composed: true}));}
    function nativeSet(e, prop, value) {let p = Object.getPrototypeOf(e); while (p) {const d = Object.getOwnPropertyDescriptor(p, prop); if (d?.set) {d.set.call(e, value); return;} p = Object.getPrototypeOf(p);} e[prop] = value;}
    function optionMatch(available, value) {
      const exact = available.filter(o => String(o.value) === String(value));
      if (exact.length === 1) return exact[0];
      const labeled = available.filter(o => norm(o.label) === norm(value));
      if (labeled.length === 1) return labeled[0];
      throw fail("OPTION_NOT_FOUND", "No unique exact option matches the supplied value");
    }
    async function setValue(e, value) {
      if (e.type === "file") throw fail("UPLOAD_REQUIRED", "Use the upload command for files");
      if (e.type === "checkbox") {
        if (typeof value !== "boolean") throw fail("INVALID_VALUE", "Checkbox answers must be booleans");
        if (e.checked !== value) e.click();
        if (e.checked !== value) throw fail("READBACK_FAILED", "Checkbox did not retain the supplied value");
      } else if (e.type === "radio") {
        const selected = optionMatch(options(e), value), target = radioGroup(e).find(r => r.value === selected.value);
        target.click(); if (!target.checked) throw fail("READBACK_FAILED", "Radio selection did not persist");
      } else if (e.tagName === "SELECT") {
        const values = (e.multiple ? (Array.isArray(value) ? value : [value]) : [value]).map(v => optionMatch(options(e), v).value);
        for (const o of e.options) o.selected = values.includes(o.value);
        if (!e.multiple) nativeSet(e, "value", values[0]);
        emit(e); const actual = read(e); if (JSON.stringify(actual) !== JSON.stringify(e.multiple ? values : values[0])) throw fail("READBACK_FAILED", "Select did not retain the supplied value");
      } else if (e.getAttribute("role") === "combobox") {
        e.click(); let matches = [];
        for (let attempt = 0; attempt < 15; attempt++) {
          const controlled = e.getRootNode().getElementById?.(e.getAttribute("aria-controls") || "");
          const candidates = controlled ? [...controlled.querySelectorAll("[role='option']")] : all("[role='option']");
          matches = candidates.filter(o => visible(o) && (norm(o.textContent) === norm(value) || o.getAttribute("data-value") === String(value)));
          if (matches.length) break; await sleep(80);
        }
        if (matches.length !== 1) throw fail("OPTION_NOT_FOUND", "Custom dropdown has no unique exact match");
        matches[0].click(); emit(e);
        if (norm(read(e)) !== norm(value) && matches[0].getAttribute("aria-selected") !== "true" && matches[0].getAttribute("data-value") !== String(read(e))) throw fail("READBACK_FAILED", "Custom dropdown did not confirm its selection");
      } else {
        const answer = String(value ?? "");
        if (e.maxLength > 0 && answer.length > e.maxLength) throw fail("LENGTH_EXCEEDED", "Answer exceeds the character limit");
        if (e.hasAttribute("contenteditable")) e.textContent = answer; else nativeSet(e, "value", answer);
        emit(e);
        if (e.type === "password" ? e.value !== answer : String(read(e)) !== answer) throw fail("READBACK_FAILED", "Field did not retain the supplied answer");
      }
      await sleep(40);
      return read(e);
    }
    async function fill(values) {
      if (inspect().captcha) throw fail("CAPTCHA", "Complete the visible CAPTCHA manually");
      const results = [];
      for (const item of values) {
        try {const e = get(item.field_id); results.push({field_id: item.field_id, ok: true, value: await setValue(e, item.value), sensitive: e.type === "password"});}
        catch (error) {results.push({field_id: item.field_id, ok: false, error: {code: error.code || "FILL_FAILED", message: error.message}});}
      }
      return {fields: results};
    }
    async function upload(payload) {
      const e = get(payload.field_id);
      if (e.type !== "file") throw fail("NOT_FILE_FIELD", "The selected field is not a file input");
      if (inspect().captcha) throw fail("CAPTCHA", "Complete the visible CAPTCHA manually");
      if (!payload.name || /[/\\]/.test(payload.name)) throw fail("INVALID_FILE_NAME", "A simple filename is required");
      const binary = win.atob(payload.data_base64);
      if (binary.length > 20 * 1024 * 1024) throw fail("FILE_TOO_LARGE", "Upload exceeds the 20 MiB limit");
      const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
      const digest = [...new Uint8Array(await win.crypto.subtle.digest("SHA-256", bytes))].map(b => b.toString(16).padStart(2, "0")).join("");
      if (!payload.sha256 || payload.sha256.toLowerCase() !== digest) throw fail("HASH_MISMATCH", "Upload integrity check failed");
      const transfer = new win.DataTransfer(); transfer.items.add(new win.File([bytes], payload.name, {type: payload.mime_type || "application/octet-stream"}));
      e.files = transfer.files; emit(e);
      if (e.files.length !== 1 || e.files[0].name !== payload.name || e.files[0].size !== bytes.length) throw fail("UPLOAD_FAILED", "Browser did not retain the selected file");
      return {uploaded: true, name: payload.name, size: bytes.length, sha256: digest, value: read(e)};
    }
    function click(payload) {
      const current = inspect();
      if (current.captcha) throw fail("CAPTCHA", "Complete the visible CAPTCHA manually");
      if (payload.expected_url && current.url !== payload.expected_url) throw fail("STALE_PAGE", "The page URL changed since inspection");
      if (!payload.snapshot_id || current.snapshot_id !== payload.snapshot_id) throw fail("STALE_SNAPSHOT", "Inspect and validate the current page before clicking");
      const e = get(payload.button_id), kind = buttonKind(e);
      if (!kind) throw fail("UNSUPPORTED_BUTTON", "Button is not a recognized application action");
      if (kind === "submit") {
        if (current.stage !== "final") throw fail("NOT_FINAL_STAGE", "This is not a final application stage");
        if (current.validation_errors.length || current.fields.some(f => f.required && (f.value === "" || f.value == null || (Array.isArray(f.value) && !f.value.length) || (f.type === "checkbox" && !f.value)))) throw fail("VALIDATION_FAILED", "Required fields or visible validation errors remain");
        if (e.form && !e.form.checkValidity()) throw fail("VALIDATION_FAILED", "Native form validation failed");
      }
      e.click();
      return {clicked: true, kind, url_before: current.url};
    }
    function locate(payload) {
      if (!Number.isFinite(payload.x) || !Number.isFinite(payload.y)) throw fail("INVALID_POINT", "A finite screenshot coordinate is required");
      const current = inspect();
      if (current.captcha) throw fail("CAPTCHA", "Complete the visible CAPTCHA manually");
      if (payload.expected_url !== current.url || payload.snapshot_id !== current.snapshot_id) throw fail("STALE_SNAPSHOT", "Inspect the page before locating a screenshot target");
      let target = document.elementFromPoint(payload.x, payload.y);
      while (target?.shadowRoot?.elementFromPoint) {const inner = target.shadowRoot.elementFromPoint(payload.x, payload.y); if (!inner || inner === target) break; target = inner;}
      const e = target?.closest("input, textarea, select, button, a, [role='button'], [role='combobox'], [contenteditable='true']");
      if (!e || !visible(e)) throw fail("NO_SEMANTIC_TARGET", "No supported form control exists at that point; manual interaction is required");
      const controlId = id(e);
      return {field: current.fields.find(f => f.id === controlId) || null, button: current.buttons.find(b => b.id === controlId) || null};
    }
    return {inspect, fill, upload, click, locate, visible};
  }
  const api = {createEngine}; if (typeof module !== "undefined") module.exports = api; root.ApplicationDOM = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
