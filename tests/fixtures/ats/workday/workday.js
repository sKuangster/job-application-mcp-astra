/*
 * Workday-style mock application flow (single document, JS-driven steps).
 * LOCAL TEST FIXTURE — NOT A REAL APPLICATION.
 */
(function () {
  "use strict";

  var ACCOUNTS_KEY = "fixture_workday_accounts";
  var memoryAccounts = {};
  var TOTAL_STEPS = 6;
  var STEP_NAMES = ["My Information", "My Experience", "Application Questions", "Voluntary Disclosures", "Self Identify", "Review"];

  var LISTBOX_OPTIONS = {
    country: [["USA", "United States of America"], ["CAN", "Canada"], ["GBR", "United Kingdom"]],
    state: [["CA", "California"], ["NY", "New York"], ["TX", "Texas"], ["WA", "Washington"], ["MA", "Massachusetts"]],
    phoneType: [["Mobile", "Mobile"], ["Home", "Home"], ["Work", "Work"]],
    yesNo: [["Yes", "Yes"], ["No", "No"]],
    gender: [["Male", "Male"], ["Female", "Female"], ["Decline", "I do not wish to answer"]],
    ethnicity: [
      ["AIAN", "American Indian or Alaska Native"],
      ["ASIAN", "Asian"],
      ["BLACK", "Black or African American"],
      ["HISPANIC", "Hispanic or Latino"],
      ["NHPI", "Native Hawaiian or Other Pacific Islander"],
      ["WHITE", "White"],
      ["TWO", "Two or More Races"],
      ["Decline", "I do not wish to answer"]
    ],
    veteran: [
      ["NotVeteran", "I am not a veteran"],
      ["Protected", "I identify as one or more of the classifications of protected veteran"],
      ["Decline", "I do not wish to answer"]
    ]
  };

  var state = {
    email: "",
    source: "",
    resumeFiles: [],
    step: 1,
    submitted: false,
    workCount: 0
  };

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function byId(id) { return document.getElementById(id); }

  /* ---------------- accounts (localStorage) ---------------- */
  function loadAccounts() {
    try {
      var raw = window.localStorage.getItem(ACCOUNTS_KEY);
      var parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (e) { return memoryAccounts; }
  }
  function saveAccount(email, password) {
    var accounts = loadAccounts();
    accounts[email] = password;
    memoryAccounts = accounts;
    try { window.localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(accounts)); } catch (e) { /* in-memory fallback */ }
  }
  function normEmail(v) { return String(v || "").trim().toLowerCase(); }

  /* ---------------- pages ---------------- */
  var PAGES = ["step-job", "step-create-account", "step-sign-in", "apply-flow", "step-confirmation"];
  function showPage(id) {
    PAGES.forEach(function (p) { byId(p).hidden = p !== id; });
    closeAllListboxes();
  }

  /* ---------------- error helpers (Workday markup) ---------------- */
  function fieldContainer(control) {
    return control.closest('[data-automation-id^="formField"]') || control.parentNode;
  }
  function labelTextFor(control) {
    var container = fieldContainer(control);
    var lab = container && (container.querySelector("legend") || container.querySelector("label"));
    var text = lab ? lab.textContent : (control.getAttribute("aria-label") || control.id);
    return text.replace(/\*/g, "").replace(/\s+/g, " ").trim();
  }
  function showFieldError(control, message) {
    var container = fieldContainer(control);
    var errId = (control.id || "field") + "-error";
    var node = byId(errId);
    if (!node) {
      node = document.createElement("div");
      node.id = errId;
      node.setAttribute("data-automation-id", "errorMessage");
      node.setAttribute("role", "alert");
      container.appendChild(node);
    }
    node.innerHTML = "";
    var p = document.createElement("p");
    p.textContent = message;
    node.appendChild(p);
    control.setAttribute("aria-invalid", "true");
    var described = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    if (described.indexOf(errId) < 0) described.push(errId);
    control.setAttribute("aria-describedby", described.join(" "));
  }
  function requiredError(control) {
    showFieldError(control, "Error: The field " + labelTextFor(control) + " is required and must have a value.");
  }
  function clearFieldError(control) {
    if (!control) return;
    var errId = (control.id || "field") + "-error";
    var node = byId(errId);
    if (node) node.parentNode.removeChild(node);
    control.removeAttribute("aria-invalid");
    var described = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(function (x) { return x && x !== errId; });
    if (described.length) control.setAttribute("aria-describedby", described.join(" "));
    else control.removeAttribute("aria-describedby");
    // group controls (radio / checkbox groups) share the first control's error id
    var group = control.name ? $all('input[name="' + control.name + '"]') : [];
    group.forEach(function (g) {
      if (g === control) return;
      var gId = (g.id || "field") + "-error";
      var gNode = byId(gId);
      if (gNode) gNode.parentNode.removeChild(gNode);
      g.removeAttribute("aria-invalid");
    });
  }
  function clearAllErrors(root) {
    $all('[data-automation-id="errorMessage"]', root).forEach(function (n) { n.parentNode.removeChild(n); });
    $all('[aria-invalid="true"]', root).forEach(function (n) { n.removeAttribute("aria-invalid"); });
    $all('[data-automation-id="errorBanner"]', root).forEach(function (b) { b.hidden = true; });
  }
  function showBanner(root) {
    var b = root.querySelector('[data-automation-id="errorBanner"]');
    if (b) b.hidden = false;
  }
  function hideBannerIfClean(root) {
    if (!root.querySelector('[data-automation-id="errorMessage"]')) {
      $all('[data-automation-id="errorBanner"]', root).forEach(function (b) { b.hidden = true; });
    }
  }

  function val(el) { return String(el && el.value != null ? el.value : "").trim(); }

  /* ---------------- job page / apply modal ---------------- */
  var applyModal = byId("apply-modal");
  $('[data-automation-id="adventureButton"]').addEventListener("click", function (e) {
    e.preventDefault();
    applyModal.hidden = false;
  });
  $('[data-automation-id="closeButton"]', applyModal).addEventListener("click", function () { applyModal.hidden = true; });
  function goCreateAccount(e) {
    if (e) e.preventDefault();
    applyModal.hidden = true;
    clearAllErrors(byId("step-create-account"));
    showPage("step-create-account");
  }
  function goSignIn(e, notice) {
    if (e) e.preventDefault();
    applyModal.hidden = true;
    clearAllErrors(byId("step-sign-in"));
    var n = $('[data-automation-id="signInNotice"]');
    n.textContent = notice || "";
    n.hidden = !notice;
    var err = $('[data-automation-id="signInError"]');
    err.hidden = true; err.innerHTML = "";
    showPage("step-sign-in");
  }
  $('[data-automation-id="autofillWithResume"]').addEventListener("click", goCreateAccount);
  $('[data-automation-id="applyManually"]').addEventListener("click", goCreateAccount);
  $('[data-automation-id="useMyLastApplication"]').addEventListener("click", function (e) { goSignIn(e); });
  $('[data-automation-id="signInLink"]').addEventListener("click", function (e) { goSignIn(e); });
  $('[data-automation-id="createAccountLink"]').addEventListener("click", goCreateAccount);

  /* ---------------- create account ---------------- */
  var caPage = byId("step-create-account");
  var caForm = byId("create-account-form");
  var caEmail = byId("ca-email"), caPassword = byId("ca-password"), caVerify = byId("ca-verify-password"), caTerms = byId("ca-terms");
  [caEmail, caPassword, caVerify].forEach(function (el) {
    el.addEventListener("input", function () { clearFieldError(el); hideBannerIfClean(caPage); });
  });
  caTerms.addEventListener("change", function () { clearFieldError(caTerms); hideBannerIfClean(caPage); });

  function validEmail(v) { return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v); }
  function strongPassword(p) {
    return p.length >= 8 && /[A-Z]/.test(p) && /[0-9]/.test(p) && /[^A-Za-z0-9]/.test(p);
  }

  function handleCreateAccount(e) {
    if (e) e.preventDefault();
    clearAllErrors(caPage);
    var ok = true;
    var email = normEmail(caEmail.value);
    var pw = caPassword.value || "";
    var pw2 = caVerify.value || "";
    if (!email) { requiredError(caEmail); ok = false; }
    else if (!validEmail(email)) { showFieldError(caEmail, "Error: Enter a valid email address."); ok = false; }
    else if (Object.prototype.hasOwnProperty.call(loadAccounts(), email)) {
      showFieldError(caEmail, "Error: An account already exists for this email address. Please sign in."); ok = false;
    }
    if (!pw) { requiredError(caPassword); ok = false; }
    else if (!strongPassword(pw)) {
      showFieldError(caPassword, "Error: Password must be at least 8 characters and include an uppercase letter, a number, and a special character.");
      ok = false;
    }
    if (!pw2) { requiredError(caVerify); ok = false; }
    else if (pw && pw2 !== pw) { showFieldError(caVerify, "Passwords do not match"); ok = false; }
    if (!caTerms.checked) { requiredError(caTerms); ok = false; }
    if (!ok) { showBanner(caPage); return; }
    saveAccount(email, pw);
    caForm.reset();
    goSignIn(null, "Account created. Please sign in.");
  }
  caForm.addEventListener("submit", handleCreateAccount);

  /* ---------------- sign in ---------------- */
  var siPage = byId("step-sign-in");
  var siForm = byId("sign-in-form");
  var siEmail = byId("si-email"), siPassword = byId("si-password");
  var siError = $('[data-automation-id="signInError"]');
  [siEmail, siPassword].forEach(function (el) {
    el.addEventListener("input", function () { clearFieldError(el); hideBannerIfClean(siPage); });
  });
  function handleSignIn(e) {
    if (e) e.preventDefault();
    clearAllErrors(siPage);
    siError.hidden = true; siError.innerHTML = "";
    var ok = true;
    var email = normEmail(siEmail.value);
    var pw = siPassword.value || "";
    if (!email) { requiredError(siEmail); ok = false; }
    if (!pw) { requiredError(siPassword); ok = false; }
    if (!ok) { showBanner(siPage); return; }
    var accounts = loadAccounts();
    if (!Object.prototype.hasOwnProperty.call(accounts, email) || accounts[email] !== pw) {
      siError.innerHTML = '<div data-automation-id="errorMessage" role="alert" id="signIn-error"><p>Wrong email address or password.</p></div>';
      siError.hidden = false;
      siEmail.setAttribute("aria-invalid", "true");
      siPassword.setAttribute("aria-invalid", "true");
      return;
    }
    state.email = email;
    siForm.reset();
    var who = $('[data-automation-id="signedInUser"]');
    who.textContent = "Signed in as " + email;
    who.hidden = false;
    $('[data-automation-id="signInNotice"]').hidden = true;
    showPage("apply-flow");
    goToStep(1);
  }
  siForm.addEventListener("submit", handleSignIn);

  /* ---------------- listbox buttons ---------------- */
  var openListbox = null;
  function closeAllListboxes() {
    $all('ul[role="listbox"][data-fixture-listbox]').forEach(function (ul) { ul.parentNode.removeChild(ul); });
    $all('button[aria-haspopup="listbox"]').forEach(function (b) { b.setAttribute("aria-expanded", "false"); b.removeAttribute("aria-controls"); });
    openListbox = null;
    closeSourceList();
  }
  function listboxBaseLabel(btn) {
    var lab = document.querySelector('label[for="' + btn.id + '"]');
    return lab ? lab.textContent.replace(/\*/g, "").replace(/\s+/g, " ").trim() : btn.id;
  }
  function setListboxValue(btn, value, text) {
    btn.textContent = text;
    btn.value = value;
    btn.setAttribute("value", value);
    btn.setAttribute("data-selected-label", text);
    var required = /Required$/.test(btn.getAttribute("aria-label") || "");
    btn.setAttribute("aria-label", listboxBaseLabel(btn) + " " + text + (required ? " Required" : ""));
    clearFieldError(btn);
    var page = btn.closest("#apply-flow");
    if (page) hideBannerIfClean(page);
  }
  function openListboxFor(btn) {
    closeAllListboxes();
    var key = btn.getAttribute("data-listbox");
    var opts = LISTBOX_OPTIONS[key] || [];
    var ul = document.createElement("ul");
    ul.setAttribute("role", "listbox");
    ul.setAttribute("data-fixture-listbox", "");
    ul.setAttribute("data-automation-id", btn.getAttribute("data-automation-id") + "-listbox");
    ul.id = btn.id + "-listbox";
    ul.setAttribute("aria-label", listboxBaseLabel(btn));
    opts.forEach(function (o, i) {
      var li = document.createElement("li");
      li.setAttribute("role", "option");
      li.setAttribute("data-automation-id", "promptOption");
      li.setAttribute("data-automation-label", o[1]);
      li.setAttribute("data-value", o[0]);
      li.id = btn.id + "-option-" + i;
      li.setAttribute("aria-selected", btn.value === o[0] ? "true" : "false");
      li.tabIndex = -1;
      var div = document.createElement("div");
      div.textContent = o[1];
      li.appendChild(div);
      li.addEventListener("click", function (ev) {
        ev.stopPropagation();
        setListboxValue(btn, o[0], o[1]);
        closeAllListboxes();
        btn.dispatchEvent(new Event("change", { bubbles: true }));
        try { btn.focus(); } catch (e) { /* ignore */ }
      });
      li.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); li.click(); }
      });
      ul.appendChild(li);
    });
    btn.parentNode.appendChild(ul);
    btn.setAttribute("aria-expanded", "true");
    btn.setAttribute("aria-controls", ul.id);
    openListbox = { btn: btn, ul: ul };
  }
  $all('button[aria-haspopup="listbox"]').forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (openListbox && openListbox.btn === btn) closeAllListboxes();
      else openListboxFor(btn);
    });
    btn.addEventListener("keydown", function (e) {
      if ((e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") && !(openListbox && openListbox.btn === btn)) {
        e.preventDefault();
        openListboxFor(btn);
        var first = openListbox.ul.querySelector('li[role="option"]');
        if (first) first.focus();
      } else if (e.key === "Escape") closeAllListboxes();
    });
  });
  function listboxValue(id) { return byId(id).value || ""; }
  function listboxText(id) { var b = byId(id); return b.value ? b.textContent : ""; }

  /* ---------------- "How Did You Hear About Us?" prompt ---------------- */
  var sourceField = $('[data-automation-id="formField-source"]');
  var sourceContainer = byId("source-container");
  var sourceInput = byId("source--source");
  var sourceList = byId("source-options");
  var sourceSelected = byId("source-selected");
  function openSourceList() {
    if (openListbox) { $all('ul[role="listbox"][data-fixture-listbox]').forEach(function (ul) { ul.parentNode.removeChild(ul); }); openListbox = null; }
    sourceList.hidden = false;
    sourceInput.setAttribute("aria-expanded", "true");
    filterSource();
  }
  function closeSourceList() {
    if (!sourceList) return;
    sourceList.hidden = true;
    sourceInput.setAttribute("aria-expanded", "false");
  }
  function filterSource() {
    var q = sourceInput.value.trim().toLowerCase();
    $all('[data-automation-id="promptOption"]', sourceList).forEach(function (o) {
      o.hidden = q ? o.textContent.toLowerCase().indexOf(q) < 0 : false;
    });
  }
  sourceContainer.addEventListener("click", function (e) { e.stopPropagation(); openSourceList(); });
  sourceInput.addEventListener("focus", openSourceList);
  sourceInput.addEventListener("input", function () { if (sourceList.hidden) openSourceList(); else filterSource(); });
  sourceInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      var first = $all('[data-automation-id="promptOption"]', sourceList).filter(function (o) { return !o.hidden; })[0];
      if (first) first.click();
    } else if (e.key === "Escape") closeSourceList();
  });
  function chooseSource(opt) {
    state.source = opt.getAttribute("data-automation-label");
    $all('[data-automation-id="promptOption"]', sourceList).forEach(function (o) { o.setAttribute("aria-selected", o === opt ? "true" : "false"); });
    sourceSelected.innerHTML = "";
    var pill = document.createElement("div");
    pill.setAttribute("data-automation-id", "selectedItem");
    pill.setAttribute("title", state.source);
    pill.textContent = state.source;
    sourceSelected.appendChild(pill);
    sourceInput.value = "";
    filterSource();
    closeSourceList();
    clearFieldError(sourceInput);
    hideBannerIfClean(byId("apply-flow"));
  }
  $all('[data-automation-id="promptOption"]', sourceList).forEach(function (opt) {
    opt.addEventListener("click", function (e) { e.stopPropagation(); chooseSource(opt); });
    opt.addEventListener("mousedown", function (e) { e.preventDefault(); });
  });
  sourceList.addEventListener("click", function (e) { e.stopPropagation(); });

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (openListbox && !openListbox.ul.contains(t) && t !== openListbox.btn) closeAllListboxes();
    if (!sourceList.hidden && !sourceField.contains(t)) closeSourceList();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeAllListboxes(); });

  /* ---------------- generic input error clearing in the flow ---------------- */
  var flow = byId("apply-flow");
  flow.addEventListener("input", function (e) {
    var t = e.target;
    if (t && t !== sourceInput && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) { clearFieldError(t); hideBannerIfClean(flow); }
  });
  flow.addEventListener("change", function (e) {
    var t = e.target;
    if (t && (t.type === "radio" || t.type === "checkbox")) { clearFieldError(t.name ? $('input[name="' + t.name + '"]') : t); clearFieldError(t); hideBannerIfClean(flow); }
  });

  /* ---------------- disability checkboxes behave like radios ---------------- */
  var disabilityBoxes = $all('input[name="disabilityStatus"]');
  disabilityBoxes.forEach(function (cb) {
    cb.addEventListener("change", function () {
      if (cb.checked) disabilityBoxes.forEach(function (o) { if (o !== cb) o.checked = false; });
    });
  });

  /* ---------------- work experience ---------------- */
  var workList = byId("workExperience-list");
  byId("workExperience-add").addEventListener("click", function () {
    state.workCount += 1;
    var n = state.workCount;
    var block = document.createElement("div");
    block.className = "wd-work-block";
    block.setAttribute("data-automation-id", "workExperience-" + n);
    block.innerHTML =
      '<h4>Work Experience ' + n + '</h4>' +
      '<div data-automation-id="formField-jobTitle"><label for="workExperience-' + n + '--jobTitle">Job Title<abbr title="required">*</abbr></label>' +
      '<div><input type="text" id="workExperience-' + n + '--jobTitle" data-automation-id="jobTitle" aria-required="true"></div></div>' +
      '<div data-automation-id="formField-companyName"><label for="workExperience-' + n + '--companyName">Company<abbr title="required">*</abbr></label>' +
      '<div><input type="text" id="workExperience-' + n + '--companyName" data-automation-id="company" aria-required="true"></div></div>' +
      '<button type="button" class="wd-link" data-automation-id="panel-set-delete-button">Delete</button>';
    block.querySelector('[data-automation-id="panel-set-delete-button"]').addEventListener("click", function () {
      block.parentNode.removeChild(block);
    });
    workList.appendChild(block);
  });
  function workExperience() {
    return $all(".wd-work-block", workList).map(function (b) {
      return { jobTitle: val(b.querySelector('[data-automation-id="jobTitle"]')), company: val(b.querySelector('[data-automation-id="company"]')) };
    });
  }

  /* ---------------- resume upload ---------------- */
  var resumeInput = byId("resumeAttachments--attachments");
  var dropZone = byId("resume-drop-zone");
  var fileList = byId("resume-file-list");
  var MAX_BYTES = 5 * 1024 * 1024;
  function renderFiles() {
    fileList.innerHTML = "";
    state.resumeFiles.forEach(function (f, i) {
      var row = document.createElement("div");
      row.className = "wd-file-row";
      row.setAttribute("data-automation-id", "file-upload-item");
      var name = document.createElement("div");
      name.setAttribute("data-automation-id", "file-upload-successful");
      name.textContent = f.name;
      var del = document.createElement("button");
      del.type = "button";
      del.className = "wd-link";
      del.setAttribute("data-automation-id", "delete-file");
      del.setAttribute("aria-label", "Delete " + f.name);
      del.textContent = "Delete";
      del.addEventListener("click", function () { state.resumeFiles.splice(i, 1); renderFiles(); });
      row.appendChild(name);
      row.appendChild(del);
      fileList.appendChild(row);
    });
  }
  function addFiles(files) {
    var arr = Array.prototype.slice.call(files || []);
    var tooBig = false;
    arr.forEach(function (f) {
      if (!f || !f.name) return;
      if (typeof f.size === "number" && f.size > MAX_BYTES) { tooBig = true; return; }
      state.resumeFiles = state.resumeFiles.filter(function (x) { return x.name !== f.name; });
      state.resumeFiles.push(f);
    });
    renderFiles();
    if (tooBig) showFieldError(resumeInput, "Error: The file exceeds the maximum size of 5MB.");
    else if (state.resumeFiles.length) { clearFieldError(resumeInput); hideBannerIfClean(flow); }
  }
  $('[data-automation-id="select-files"]').addEventListener("click", function (e) { e.preventDefault(); resumeInput.click(); });
  resumeInput.addEventListener("click", function (e) { e.stopPropagation(); });
  resumeInput.addEventListener("change", function () { addFiles(resumeInput.files); });
  ["dragenter", "dragover"].forEach(function (t) {
    dropZone.addEventListener(t, function (e) { e.preventDefault(); dropZone.classList.add("dragover"); });
  });
  dropZone.addEventListener("dragleave", function () { dropZone.classList.remove("dragover"); });
  dropZone.addEventListener("drop", function (e) {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    var files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    try { resumeInput.files = files; } catch (err) { /* jsdom / read-only */ }
    addFiles(files);
  });

  /* ---------------- step navigation ---------------- */
  var navBack = byId("nav-back");
  var navNext = byId("nav-next");
  var flowBanner = byId("flow-error-banner");

  function renderProgress() {
    $all('[data-automation-id="progressBar"] li').forEach(function (li) {
      var n = parseInt(li.getAttribute("data-step"), 10);
      var sr = li.querySelector("[data-step-sr]");
      if (n === state.step) {
        li.setAttribute("data-automation-id", "progressBarActiveStep");
        li.setAttribute("aria-current", "step");
        sr.textContent = "current step " + n + " of " + TOTAL_STEPS;
      } else {
        li.setAttribute("data-automation-id", n < state.step ? "progressBarCompletedStep" : "progressBarInactiveStep");
        li.removeAttribute("aria-current");
        sr.textContent = (n < state.step ? "completed step " : "step ") + n + " of " + TOTAL_STEPS;
      }
    });
  }
  function goToStep(n) {
    closeAllListboxes();
    state.step = n;
    for (var i = 1; i <= TOTAL_STEPS; i++) byId("flow-step-" + i).hidden = i !== n;
    clearAllErrors(flow);
    flowBanner.hidden = true;
    navBack.hidden = n === 1;
    navNext.textContent = n === TOTAL_STEPS ? "Submit" : "Save and Continue";
    if (n === TOTAL_STEPS) renderReview();
    renderProgress();
  }

  function requireText(id) {
    var el = byId(id);
    if (!val(el)) { requiredError(el); return false; }
    return true;
  }
  function requireListbox(id) {
    var el = byId(id);
    if (!el.value) { requiredError(el); return false; }
    return true;
  }

  function validateStep(n) {
    var ok = true;
    function chk(r) { if (!r) ok = false; }
    if (n === 1) {
      if (!state.source) { requiredError(sourceInput); ok = false; }
      if (!$('input[name="previousWorker"]:checked')) { requiredError(byId("previousWorker-yes")); ok = false; }
      chk(requireText("name--legalName--firstName"));
      chk(requireText("name--legalName--lastName"));
      chk(requireListbox("country--country"));
      chk(requireText("address--addressLine1"));
      chk(requireText("address--city"));
      chk(requireListbox("address--countryRegion"));
      var pc = byId("address--postalCode");
      if (requireText("address--postalCode")) {
        if (byId("country--country").value === "USA" && !/^\d{5}$/.test(val(pc))) { showFieldError(pc, "Postal Code must be 5 digits"); ok = false; }
      } else ok = false;
      chk(requireListbox("phoneNumber--phoneType"));
      var ph = byId("phoneNumber--phoneNumber");
      if (requireText("phoneNumber--phoneNumber")) {
        var raw = val(ph);
        var digits = raw.replace(/[\s().-]/g, "");
        if (!/^\d{10}$/.test(digits)) { showFieldError(ph, "Enter a valid phone number"); ok = false; }
      } else ok = false;
    } else if (n === 2) {
      $all(".wd-work-block input", workList).forEach(function (el) { if (!val(el)) { requiredError(el); ok = false; } });
      if (!state.resumeFiles.length) { showFieldError(resumeInput, "Please upload your resume"); ok = false; }
    } else if (n === 3) {
      chk(requireListbox("primaryQuestionnaire--workAuthorization"));
      chk(requireListbox("primaryQuestionnaire--sponsorship"));
      chk(requireText("primaryQuestionnaire--whyInterested"));
    } else if (n === 4) {
      var agree = byId("termsAndConditions--acceptTermsAndAgreements");
      if (!agree.checked) { requiredError(agree); ok = false; }
    } else if (n === 5) {
      chk(requireText("selfIdentifiedDisabilityData--name"));
      var d = byId("selfIdentifiedDisabilityData--dateSignedOn");
      if (requireText("selfIdentifiedDisabilityData--dateSignedOn")) {
        if (!validDate(val(d))) { showFieldError(d, "Enter a date in the format MM/DD/YYYY"); ok = false; }
      } else ok = false;
      var checked = disabilityBoxes.filter(function (c) { return c.checked; });
      if (checked.length !== 1) { requiredError(disabilityBoxes[0]); ok = false; }
    }
    return ok;
  }
  function validDate(s) {
    var m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(s);
    if (!m) return false;
    var mo = +m[1], da = +m[2], yr = +m[3];
    if (mo < 1 || mo > 12 || da < 1 || yr < 1900 || yr > 2100) return false;
    var dt = new Date(yr, mo - 1, da);
    return dt.getFullYear() === yr && dt.getMonth() === mo - 1 && dt.getDate() === da;
  }

  function gatherData() {
    var pw = $('input[name="previousWorker"]:checked');
    var dis = disabilityBoxes.filter(function (c) { return c.checked; })[0];
    return {
      email: state.email,
      source: state.source,
      previousWorker: pw ? pw.value : "",
      firstName: val(byId("name--legalName--firstName")),
      lastName: val(byId("name--legalName--lastName")),
      country: listboxText("country--country"),
      addressLine1: val(byId("address--addressLine1")),
      city: val(byId("address--city")),
      state: listboxText("address--countryRegion"),
      postalCode: val(byId("address--postalCode")),
      phoneDeviceType: listboxText("phoneNumber--phoneType"),
      phoneCountryCode: "+1",
      phoneNumber: val(byId("phoneNumber--phoneNumber")),
      workExperience: workExperience(),
      resume: state.resumeFiles.map(function (f) { return f.name; }),
      linkedin: val(byId("socialNetworkAccounts--linkedInAccount")),
      workAuthorization: listboxText("primaryQuestionnaire--workAuthorization"),
      sponsorship: listboxText("primaryQuestionnaire--sponsorship"),
      whyInterested: val(byId("primaryQuestionnaire--whyInterested")),
      gender: listboxText("personalInfoUS--gender"),
      ethnicity: listboxText("personalInfoUS--ethnicity"),
      veteran: listboxText("personalInfoUS--veteranStatus"),
      agreement: !!byId("termsAndConditions--acceptTermsAndAgreements").checked,
      disabilityName: val(byId("selfIdentifiedDisabilityData--name")),
      disabilityDate: val(byId("selfIdentifiedDisabilityData--dateSignedOn")),
      disabilityStatus: dis ? dis.value : ""
    };
  }

  function renderReview() {
    var d = gatherData();
    var sections = [
      ["My Information", [
        ["Email Address", d.email], ["How Did You Hear About Us?", d.source], ["Have you previously worked for Fixture Corp?", d.previousWorker],
        ["First Name", d.firstName], ["Last Name", d.lastName], ["Country", d.country], ["Address Line 1", d.addressLine1],
        ["City", d.city], ["State", d.state], ["Postal Code", d.postalCode], ["Phone Device Type", d.phoneDeviceType],
        ["Country Phone Code", "United States of America (+1)"], ["Phone Number", d.phoneNumber]
      ]],
      ["My Experience", [
        ["Work Experience", d.workExperience.length ? d.workExperience.map(function (w) { return w.jobTitle + " at " + w.company; }).join("; ") : "None"],
        ["Resume/CV", d.resume.join(", ")], ["LinkedIn", d.linkedin]
      ]],
      ["Application Questions", [
        ["Are you legally authorized to work in the country in which the job is located?", d.workAuthorization],
        ["Will you now or in the future require sponsorship for employment visa status?", d.sponsorship],
        ["Please describe why you are interested in this role.", d.whyInterested]
      ]],
      ["Voluntary Disclosures", [
        ["Gender", d.gender], ["Please select the ethnicity which most clearly describes how you identify", d.ethnicity],
        ["Please select the veteran status which most accurately describes your relationship with the military", d.veteran],
        ["Terms and Conditions", d.agreement ? "Yes, I have read and consent to the terms and conditions" : ""]
      ]],
      ["Self Identify", [["Name", d.disabilityName], ["Date", d.disabilityDate], ["Disability Status", d.disabilityStatus]]]
    ];
    var root = byId("review-content");
    root.innerHTML = "";
    sections.forEach(function (s) {
      var h = document.createElement("h3");
      h.textContent = s[0];
      root.appendChild(h);
      var dl = document.createElement("dl");
      dl.className = "wd-review";
      s[1].forEach(function (row) {
        var dt = document.createElement("dt"); dt.textContent = row[0];
        var dd = document.createElement("dd"); dd.textContent = row[1] || "—";
        dl.appendChild(dt); dl.appendChild(dd);
      });
      root.appendChild(dl);
    });
  }

  navBack.addEventListener("click", function () {
    if (state.step > 1 && !state.submitted) goToStep(state.step - 1);
  });
  navNext.addEventListener("click", function () {
    if (state.submitted) return;
    closeAllListboxes();
    if (state.step < TOTAL_STEPS) {
      clearAllErrors(flow);
      if (!validateStep(state.step)) { flowBanner.hidden = false; return; }
      goToStep(state.step + 1);
      return;
    }
    // Review -> Submit (re-validate everything)
    clearAllErrors(flow);
    for (var i = 1; i < TOTAL_STEPS; i++) {
      if (!validateStep(i)) { goToStep(i); validateStep(i); flowBanner.hidden = false; return; }
      clearAllErrors(flow);
    }
    state.submitted = true;
    navNext.disabled = true;
    navBack.disabled = true;
    window.FixtureKit.submit("workday", gatherData(), {
      confirm: function () { showPage("step-confirmation"); }
    });
  });

  // expose for debugging only
  window.__workdayFixture = { state: state, stepNames: STEP_NAMES };
})();
