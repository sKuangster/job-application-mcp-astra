/* Small declarative ATS overrides; shared interactions stay in the DOM engine. */
(function (root) {
  "use strict";
  const adapters = [
    {name: "greenhouse", host: /(^|\.)(greenhouse\.io|greenhouse\.com)$/, marker: "#grnhse_app, #application_form", errors: ".field-error, .error-message", success: "#application_confirmation"},
    {name: "lever", host: /(^|\.)lever\.co$/, marker: ".application-form", errors: ".error-message, .application-error", success: ".application-confirmation"},
    {name: "ashby", host: /(^|\.)ashbyhq\.com$/, marker: "[class*='ashby-application-form']", errors: "[class*='errorMessage']", success: "[data-testid='application-success']"},
    {name: "workday", host: /(^|\.)(myworkdayjobs\.com|myworkdaysite\.com)$/, marker: "[data-automation-id='jobApplicationPage']", errors: "[data-automation-id='errorMessage'], [data-automation-id='errorLabel']", success: "[data-automation-id='applicationConfirmation']"},
    {name: "icims", host: /(^|\.)icims\.com$/, marker: "#icims_content_iframe, .iCIMS_MainWrapper", errors: ".iCIMS_Error, .iCIMS_FieldError", success: ".iCIMS_ApplicationSuccess"},
    {name: "smartrecruiters", host: /(^|\.)smartrecruiters\.com$/, marker: "[data-test='application-form']", errors: "[data-test='field-error']", success: "[data-test='application-success']"}
  ];
  const api = {adapters, detect(document) {
    const host = document.location?.hostname || "";
    return adapters.find(a => a.host.test(host) || document.querySelector(a.marker)) || {name: "generic", errors: "", success: ""};
  }};
  if (typeof module !== "undefined") module.exports = api;
  root.ApplicationAdapters = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
