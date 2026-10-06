/*
 * ApplyPilot ATS adapters.
 *
 * UMD module: content-script global `ApplyPilotAdapters`, or `module.exports` in Node.
 *
 * An adapter is {name, matches(location, document) -> bool, ...optional hooks}:
 *   labelFor(el, ctx)                 -> raw label text (may contain "*") or null
 *   extraFields(document, ctx)        -> [descriptor] for custom widgets. A descriptor is
 *        {el, type, widget, members?, claim?, label?, rawLabel?, required?, hint?,
 *         options?, optionEls?, group?, readValue?()}; `el` gets the field id;
 *        members/claim are excluded from generic discovery.
 *   fieldHint(el, descriptor, ctx)    -> hint string or null
 *   classifyButton(el, text, ctx)     -> button kind or null
 *   pageKind(document, info, ctx)     -> page kind or null
 *   step(document, ctx)               -> {current, total, label} or null
 *   fillCustom(descriptor, el, value, ctx) -> Promise<{ok, actual?, error?, options?}> or null (= not handled)
 *   errors(document, ctx)             -> [string | {text, el?}]
 *   atsData(document, ctx)            -> extra snapshot data (ats_data) or null
 *
 * ctx.util holds the shared primitives from dom-engine.js (text, visibility,
 * events, option matching, combobox filling...).
 *
 * Registry: ApplyPilotAdapters.detect(location, document) -> [specific adapters..., generic]
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  else root.ApplyPilotAdapters = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function host(location) {
    try { return String(location && location.hostname || "").toLowerCase(); } catch (e) { return ""; }
  }
  function has(doc, selector) {
    try { return !!doc.querySelector(selector); } catch (e) { return false; }
  }

  var generic = { name: "generic", matches: function () { return true; } };

  var greenhouse = {
    name: "greenhouse",
    matches: function (location, doc) {
      return /(^|\.)greenhouse\.io$/.test(host(location)) ||
        has(doc, "#grnhse_app, #grnhse_iframe, iframe[src*='greenhouse.io'], form#application_form, #application-form[action*='greenhouse'], [data-provides='greenhouse']");
    },
    fieldHint: function (el, d) {
      if (d.widget === "combobox-input" && el.closest && el.closest("[class*='select__control'], .select__container, [class*=select-shell]")) return "greenhouse:react-select";
      return null;
    }
  };

  var lever = {
    name: "lever",
    matches: function (location, doc) {
      return /(^|\.)lever\.co$/.test(host(location)) ||
        has(doc, "form#application-form.application-form, .posting-page, a.postings-btn, .postings-btn-wrapper, [data-qa='posting-name']");
    },
    labelFor: function (el, ctx) {
      var q = el.closest && el.closest(".application-question");
      if (!q) return null;
      if (el.type === "radio" || el.type === "checkbox") return null; // group label handled generically
      var l = q.querySelector(".application-label");
      return l ? ctx.util.textOf(l, null, { max: 400 }) : null;
    }
  };

  var ashby = {
    name: "ashby",
    matches: function (location, doc) {
      return /(^|\.)ashbyhq\.com$/.test(host(location)) ||
        has(doc, "[class*='ashby-application-form'], [class*='ashby-job-posting'], .ashby-application-form-container");
    },
    extraFields: function (doc, ctx) {
      var u = ctx.util;
      var out = [];
      u.queryAll("[class*='_yesno_'], [class*='yesno']").forEach(function (box) {
        if (!u.visible(box)) return;
        var buttons = [].slice.call(box.querySelectorAll("button"));
        if (buttons.length < 2 || buttons.length > 4) return;
        var entry = box.closest("[class*='field-entry'], [class*='fieldEntry']") || box.parentElement;
        var title = entry && entry.querySelector("[class*='question-title'], label");
        var raw = title ? u.textOf(title, null, { max: 400 }) : u.questionText([box], []);
        out.push({
          el: box,
          type: "radio",
          widget: "button-group",
          members: buttons,
          group: box,
          rawLabel: raw,
          label: u.stripRequired(raw),
          required: u.looksRequired(raw) || u.attr(box, "aria-required") === "true",
          hint: "ashby:yesno"
        });
      });
      return out;
    }
  };

  function workdayPromptValue(container, ctx) {
    var items = container.querySelectorAll("[data-automation-id='selectedItem']");
    return [].map.call(items, function (i) { return ctx.util.clean(i.textContent); }).filter(Boolean).join(", ");
  }

  var workday = {
    name: "workday",
    matches: function (location, doc) {
      var h = host(location);
      return /myworkdayjobs\.com$|myworkday\.com$|(^|\.)workday\.com$|wd\d+\.myworkday/.test(h) ||
        has(doc, "[data-automation-id='jobPostingPage'], [data-automation-id='adventureButton'], [data-automation-id='progressBar'], [data-automation-id='legalNameSection_firstName'], [data-automation-id='applyFlowPage'], [data-automation-id='signInSubmitButton'], [data-automation-id='createAccountSubmitButton']");
    },
    labelFor: function (el, ctx) {
      var ff = el.closest && el.closest("[data-automation-id^='formField-']");
      if (!ff || el.type === "radio" || el.type === "checkbox") return null;
      if (ff.querySelectorAll("input:not([type=hidden]), textarea, select, button[aria-haspopup]").length > 1 && !ff.querySelector("[data-automation-id='multiSelectContainer']")) return null;
      var l = ff.querySelector("label, legend");
      return l ? ctx.util.textOf(l, null, { max: 400 }) : null;
    },
    extraFields: function (doc, ctx) {
      var u = ctx.util;
      var out = [];
      // prompt / multiselect search fields
      u.queryAll("[data-automation-id='multiSelectContainer']").forEach(function (box) {
        if (!u.visible(box)) return;
        var input = box.querySelector("input[data-automation-id='searchBox'], input");
        if (!input) return;
        var ff = box.closest("[data-automation-id^='formField-']") || box.parentElement;
        var lab = ff && ff.querySelector("label, legend");
        var raw = (lab ? u.textOf(lab, null, { max: 400 }) : "") || u.attr(input, "aria-label");
        var claim = [].slice.call(box.querySelectorAll("button, [role=button]"));
        out.push({
          el: input,
          type: "combobox",
          widget: "combobox-input",
          members: [input],
          claim: claim,
          rawLabel: raw,
          label: u.stripRequired(raw),
          required: u.looksRequired(raw) || u.attr(input, "aria-required") === "true",
          hint: "workday:prompt",
          optionsDynamic: true,
          readValue: function () { return workdayPromptValue(ff || box, ctx) || null; }
        });
      });
      // segmented dates (MM / DD / YYYY inputs)
      u.queryAll("[data-automation-id='dateInputWrapper']").forEach(function (wrap) {
        var m = wrap.querySelector("[data-automation-id='dateSectionMonth-input']");
        var y = wrap.querySelector("[data-automation-id='dateSectionYear-input']");
        if (!m || !y || !u.visible(m)) return;
        var dd = wrap.querySelector("[data-automation-id='dateSectionDay-input']");
        var ff = wrap.closest("[data-automation-id^='formField-']") || wrap.parentElement;
        var lab = ff && ff.querySelector("label, legend");
        var raw = lab ? u.textOf(lab, null, { max: 400 }) : "Date";
        var members = [m, dd, y].filter(Boolean);
        out.push({
          el: m,
          type: dd ? "date" : "month",
          widget: "custom",
          members: members,
          rawLabel: raw,
          label: u.stripRequired(raw),
          required: u.looksRequired(raw) || u.attr(m, "aria-required") === "true",
          hint: "workday:date-segments",
          segments: { m: m, d: dd, y: y },
          readValue: function () {
            var mv = m.value, dv = dd ? dd.value : "", yv = y.value;
            if (!mv && !yv) return null;
            return dd ? (mv + "/" + dv + "/" + yv) : (mv + "/" + yv);
          }
        });
      });
      return out;
    },
    fieldHint: function (el, d) {
      if (d.widget === "listbox-button") return "workday:dropdown";
      return null;
    },
    classifyButton: function (el) {
      var id = el.getAttribute("data-automation-id") || "";
      switch (id) {
        case "bottom-navigation-next-button": return /submit/i.test(el.textContent || "") ? "submit" : "next";
        case "bottom-navigation-previous-button": return "back";
        case "adventureButton": case "applyManually": return "apply";
        case "autofillWithResume": case "useMyLastApplication": return "other";
        case "createAccountSubmitButton": case "createAccountLink": return "signup";
        case "signInSubmitButton": case "signInLink": case "utilityButtonSignIn": return "login";
        case "Add": case "add-button": return "add";
        default: return null;
      }
    },
    step: function (doc, ctx) {
      var u = ctx.util;
      var bar = u.queryAll("[data-automation-id='progressBar']").filter(function (b) { return u.visible(b); })[0];
      if (!bar) return null;
      var items = [].slice.call(bar.querySelectorAll("li"));
      if (!items.length) return null;
      var active = bar.querySelector("[data-automation-id='progressBarActiveStep'], [aria-current='step']");
      var idx = active ? items.indexOf(active.closest("li") || active) : -1;
      if (idx < 0) return null;
      var label = u.clean(u.textOf(active, null, { max: 200 }).replace(/(current )?step \d+ of \d+/i, ""));
      return { current: idx + 1, total: items.length, label: label || null };
    },
    fillCustom: function (d, el, value, ctx) {
      var u = ctx.util;
      if (d.hint === "workday:date-segments") {
        return (async function () {
          var p = u.isoParts(value);
          if (!p) return { ok: false, error: { code: "BAD_REQUEST", message: "expected ISO date" } };
          var s = d.segments;
          u.typeText(s.m, p.m);
          if (s.d) u.typeText(s.d, p.d || "01");
          u.typeText(s.y, p.y);
          await u.sleep(30);
          var actual = d.readValue();
          var ok = s.m.value.replace(/^0/, "") === p.m.replace(/^0/, "") && s.y.value === p.y;
          return ok ? { ok: true, actual: actual } : { ok: false, actual: actual, error: { code: "VERIFY_FAILED", message: "date segments did not stick" } };
        })();
      }
      if (d.hint === "workday:prompt") {
        return (async function () {
          var r = await u.fillCombobox(d, value, 2500);
          if (!r.ok && r.error && r.error.code === "NO_OPTION_MATCH" && !(r.options && r.options.length)) {
            // real Workday searches on Enter
            u.focus(el);
            u.setNativeValue(el, String(value));
            u.inputEvents(el, String(value));
            u.key(el, "Enter");
            await u.sleep(400);
            r = await u.fillCombobox(d, value, 2500);
          }
          return r;
        })();
      }
      return null;
    }
  };

  var icims = {
    name: "icims",
    matches: function (location, doc) {
      return /(^|\.)icims\.com$/.test(host(location)) ||
        has(doc, "iframe#icims_content_iframe, [class*='iCIMS_'], #iCIMS_MainWrapper");
    }
  };

  var smartrecruiters = {
    name: "smartrecruiters",
    matches: function (location, doc) {
      return /(^|\.)smartrecruiters\.com$/.test(host(location)) ||
        has(doc, "oc-input, oc-select, spl-input, [data-test='apply-button'], [data-test='footer-submit'], .job-sections");
    },
    labelFor: function (el) {
      var r = el.getRootNode && el.getRootNode();
      if (r && r.host) {
        var l = r.host.getAttribute("label");
        if (l) return l + (r.host.hasAttribute("required") ? " *" : "");
      }
      return null;
    }
  };

  function nextData(doc) {
    var s = doc.getElementById && doc.getElementById("__NEXT_DATA__");
    if (!s) return null;
    try { return JSON.parse(s.textContent || "null"); } catch (e) { return null; }
  }

  var simplify = {
    name: "simplify",
    matches: function (location, doc) {
      if (/(^|\.)simplify\.jobs$/.test(host(location))) return true;
      var s = doc.getElementById && doc.getElementById("__NEXT_DATA__");
      return !!(s && /"jobPosting"/.test(s.textContent || ""));
    },
    atsData: function (doc) {
      var d = nextData(doc);
      var jp = d && d.props && d.props.pageProps && d.props.pageProps.jobPosting;
      if (!jp) return null;
      var company = jp.job && jp.job.company || {};
      return {
        simplify: {
          id: jp.id || null,
          title: jp.title || null,
          company: company.name || (jp.tracked_obj && jp.tracked_obj.company_name) || null,
          apply_url: (jp.tracked_obj && jp.tracked_obj.url) || null,
          locations: (jp.locations || []).map(function (l) { return l && (l.value || l.name) || String(l); }),
          requirements: jp.requirements || [],
          responsibilities: jp.responsibilities || []
        }
      };
    },
    pageKind: function (doc, info) {
      return info.fields.length === 0 && info.buttons.some(function (b) { return b.kind === "apply"; }) ? "job_description" : null;
    }
  };

  var specific = [workday, greenhouse, lever, ashby, icims, smartrecruiters, simplify];

  function detect(location, doc) {
    var out = [];
    for (var i = 0; i < specific.length; i++) {
      try {
        if (specific[i].matches(location, doc)) out.push(specific[i]);
      } catch (e) { /* ignore broken matcher */ }
    }
    out.push(generic);
    return out;
  }
  function get(name) {
    if (name === "generic") return generic;
    for (var i = 0; i < specific.length; i++) if (specific[i].name === name) return specific[i];
    return null;
  }

  return { detect: detect, get: get, adapters: specific.concat([generic]) };
});
