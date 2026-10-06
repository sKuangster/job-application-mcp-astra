/* Notification formatting kept pure so desktop-attention behavior is testable. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  else root.ApplyPilotAttention = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function key(item) {
    return item.application_id + ":" + item.kind + ":" + item.since + ":" + (item.attention_revision || 0);
  }

  function notification(item, iconUrl) {
    var captcha = item.kind === "captcha";
    return {
      type: "basic",
      iconUrl: iconUrl,
      title: (captcha ? "CAPTCHA REQUIRED: " : "ApplyPilot needs you: ") +
        (item.company || "") + " — " + (item.title || ""),
      message: (item.reason || item.kind || "") + (item.instructions ? "\n" + item.instructions : "")
    };
  }

  return { key: key, notification: notification };
});
