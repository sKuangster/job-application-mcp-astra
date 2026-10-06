/* Only extension runtime messages are accepted; pages cannot invoke this bridge. */
(() => {
  "use strict";
  if (globalThis.__applicationCopilotLoaded) return;
  globalThis.__applicationCopilotLoaded = true;
  const engine = ApplicationDOM.createEngine(document);
  browser.runtime.onMessage.addListener(message => {
    if (message?.channel !== "application-copilot") return undefined;
    return (async () => {
      try {
        const methods = {inspect: () => engine.inspect(), fill: () => engine.fill(message.payload.values), upload: () => engine.upload(message.payload), click: () => engine.click(message.payload), locate: () => engine.locate(message.payload)};
        if (!methods[message.action]) throw new Error("Unsupported content action");
        return {ok: true, result: await methods[message.action]()};
      } catch (error) {return {ok: false, error: {code: error.code || "DOM_ERROR", message: error.message}};}
    })();
  });
})();
