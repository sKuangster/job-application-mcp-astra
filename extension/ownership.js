(function (root) {
  "use strict";
  const error = (code, message) => Object.assign(new Error(message), {code});
  const clone = value => JSON.parse(JSON.stringify(value));
  class OwnershipManager {
    constructor(storage, digest) {this.storage = storage; this.digest = digest; this.owners = {}; this.receipts = {}; this.locks = new Map(); this.persist = Promise.resolve();}
    async initialize() {const data = await this.storage.get(["ownership", "receipts"]); this.owners = data.ownership || {}; this.receipts = data.receipts || {};}
    async save() {
      const snapshot = {ownership: clone(this.owners), receipts: clone(this.receipts)};
      this.persist = this.persist.catch(() => {}).then(() => this.storage.set(snapshot)); await this.persist;
    }
    check(command) {
      const owner = this.owners[command.application_id];
      if (!owner) throw error("NO_OWNED_TAB", "This application has no owned Firefox tab");
      if (owner.worker_id !== command.worker_id || owner.ownership_token !== command.ownership_token) throw error("OWNERSHIP_MISMATCH", "Worker does not own this application's browser tab");
      return owner;
    }
    async assign(command, tabId, url) {
      if (this.owners[command.application_id]) throw error("ALREADY_OWNED", "Application already owns a tab");
      if (Object.values(this.owners).some(o => o.tab_id === tabId)) throw error("TAB_ALREADY_OWNED", "The tab belongs to another application");
      this.owners[command.application_id] = {tab_id: tabId, worker_id: command.worker_id, ownership_token: command.ownership_token, url}; await this.save(); return this.owners[command.application_id];
    }
    async reassign(command) {
      const old = this.owners[command.application_id], p = command.payload || {};
      if (!old || old.ownership_token !== p.previous_ownership_token || !p.new_worker_id || !p.new_ownership_token || p.new_worker_id !== command.worker_id || p.new_ownership_token !== command.ownership_token) throw error("OWNERSHIP_MISMATCH", "A matching prior lease and authenticated reassignment are required");
      old.worker_id = p.new_worker_id; old.ownership_token = p.new_ownership_token; await this.save(); return {tab_id: old.tab_id, reassigned: true};
    }
    async remove(applicationId) {delete this.owners[applicationId]; await this.save();}
    async run(command, execute) {
      if (!command || ![command.id, command.application_id, command.worker_id, command.ownership_token].every(x => typeof x === "string" && x.length > 0 && x.length <= 256)) throw error("INVALID_COMMAND", "Command identity, application, worker, and ownership token are required");
      const key = `${command.application_id}:${command.id}`;
      const fingerprint = await this.digest(JSON.stringify({action: command.action, payload: command.payload || {}, worker: command.worker_id, token: command.ownership_token}));
      const previous = this.locks.get(command.application_id) || Promise.resolve();
      const running = previous.catch(() => {}).then(async () => {
        const receipt = this.receipts[key];
        if (receipt) {
          if (receipt.fingerprint !== fingerprint) throw error("COMMAND_ID_REUSED", "Command ID was already used for a different operation");
          if (receipt.status === "pending") throw error("UNCERTAIN_ACTION", "A previous session may have performed this action; inspect before proceeding");
          if (receipt.error) throw error(receipt.error.code, receipt.error.message);
          return receipt.result;
        }
        const deadline = typeof command.deadline === "number" ? (command.deadline < 1e12 ? command.deadline * 1000 : command.deadline) : Date.parse(command.deadline);
        if (Number.isFinite(deadline) && Date.now() > deadline) throw error("DEADLINE_EXCEEDED", "Command expired before execution");
        const mutation = !["inspect", "screenshot", "focus", "locate"].includes(command.action);
        if (mutation) {this.receipts[key] = {fingerprint, status: "pending", action: command.action, at: Date.now()}; await this.save();}
        try {
          const result = await execute(command);
          if (mutation) {this.receipts[key] = {fingerprint, status: "done", action: command.action, at: Date.now(), result}; await this.save();}
          return result;
        } catch (e) {
          if (mutation) {this.receipts[key] = {fingerprint, status: "done", action: command.action, at: Date.now(), error: {code: e.code || "BROWSER_ERROR", message: e.message}}; await this.save();}
          throw e;
        }
      });
      this.locks.set(command.application_id, running);
      try {return await running;} finally {if (this.locks.get(command.application_id) === running) this.locks.delete(command.application_id);}
    }
  }
  const api = {OwnershipManager}; if (typeof module !== "undefined") module.exports = api; root.ApplicationOwnership = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
