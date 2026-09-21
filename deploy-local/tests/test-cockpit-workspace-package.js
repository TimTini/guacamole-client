"use strict";

const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

class Element {
    constructor(tagName) {
        this.tagName = tagName;
        this.children = [];
        this.listeners = {};
        this.dataset = {};
        this.hidden = false;
        this.disabled = false;
        this.selected = false;
        this.value = "";
        this.textContent = "";
    }

    get firstChild() { return this.children[0] || null; }
    appendChild(child) { this.children.push(child); return child; }
    removeChild(child) {
        const index = this.children.indexOf(child);
        if (index >= 0) { this.children.splice(index, 1); }
        return child;
    }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    querySelectorAll() { return this.controls || []; }
    reportValidity() { return true; }
}

class Document {
    constructor() {
        this.elements = {};
        [
            "create-form", "name", "template", "assignee", "memory-mib", "vcpus", "submit",
            "stage", "progress", "error", "error-code", "error-message", "template-list",
            "template-list-body", "template-list-empty", "recent-list", "recent-list-body", "recent-empty",
            "result", "result-summary", "result-details", "result-links"
        ].forEach((id) => { this.elements[id] = new Element("div"); });
        this.elements["create-form"].tagName = "form";
        this.elements.template = new Element("select");
        this.elements.assignee = new Element("select");
        this.elements.submit = new Element("button");
        this.elements.progress = new Element("ol");
        this.elements["template-list-body"] = new Element("tbody");
        this.elements["recent-list-body"] = new Element("tbody");
        this.elements["result-details"] = new Element("dl");
        this.elements["result-links"] = new Element("div");
        this.elements["create-form"].controls = [
            this.elements.name, this.elements.template, this.elements.assignee,
            this.elements["memory-mib"], this.elements.vcpus
        ];
    }

    getElementById(id) { return this.elements[id] || null; }
    createElement(tagName) { return new Element(tagName); }
}

class FakeTimers {
    constructor() {
        this.nextId = 1;
        this.entries = [];
    }

    setTimeout(callback, delay) {
        const entry = { id: this.nextId++, callback, delay, cancelled: false };
        this.entries.push(entry);
        return entry.id;
    }

    clearTimeout(id) {
        const entry = this.entries.find((candidate) => candidate.id === id);
        if (entry) { entry.cancelled = true; }
    }

    pending() {
        return this.entries.filter((entry) => !entry.cancelled);
    }

    runNext() {
        const entry = this.pending()[0];
        assert.ok(entry, "expected a scheduled refresh");
        entry.cancelled = true;
        entry.callback();
    }
}

async function flush() {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
}

async function main() {
    const document = new Document();
    const calls = [];
    const progress = ["validation", "disk-overlay", "domain", "dhcp", "rdp", "guacamole", "permissions"];
    const readyWorkspace = {
        name: "wtest", templateVersion: "windows11-v1", ip: "192.168.250.20",
        assigneeType: "USER", assigneeName: "taitt7", status: "ready", stage: "permissions"
    };
    const pendingWorkspace = {
        name: "vm-pending", templateVersion: "windows11-v1", ip: "192.168.250.21",
        assigneeType: "USER", assigneeName: "alice", status: "sync-failed", stage: "sync",
        errorCode: "SYNC_COMMIT_UNKNOWN", errorMessage: "secret-output-must-not-render"
    };
    const failedWithoutInventory = {
        name: "vm-lost", templateVersion: "windows11-v1", ip: "", assigneeType: "USER", assigneeName: "alice",
        status: "failed-without-inventory", stage: "command", errorCode: "WORKSPACE_NOT_FOUND"
    };
    const listPayload = {
        ok: true,
        templates: [{ version: "windows11-v1", createdAt: "2026-09-20T00:00:00Z", virtualSize: 123, hashState: "recorded", dependentCloneCount: 2 }],
        workspaces: [readyWorkspace, pendingWorkspace, failedWithoutInventory],
        clones: [readyWorkspace, pendingWorkspace, failedWithoutInventory],
        guacamoleAssignees: {
            users: [{ type: "USER", name: "alice", label: "alice" }, { type: "USER", name: "taitt7", label: "taitt7" }],
            groups: [{ type: "USER_GROUP", name: "developers", label: "developers" }]
        }
    };
    const queuedPayload = {
        ok: true,
        status: "queued",
        stage: "validation",
        job: {
            name: "vm-new", templateVersion: "windows11-v1", assigneeType: "USER_GROUP",
            assigneeName: "developers", status: "queued", stage: "validation",
            progress: progress.map((stage) => ({ stage, status: "pending" }))
        }
    };
    const activePayload = Object.assign({}, listPayload, {
        workspaces: [Object.assign({}, pendingWorkspace, { status: "running", stage: "rdp" })],
        clones: [Object.assign({}, pendingWorkspace, { status: "running", stage: "rdp" })]
    });
    let listResponse = listPayload;
    const repairPayload = { ok: true, status: "queued", stage: "repair", job: { name: "vm-pending", status: "queued", stage: "repair" } };
    let deferredListResolve = null;
    let deferredListCallNumber = 0;
    const cockpit = {
        failNextStart: false,
        spawn(argv, options) {
            calls.push({ argv, options });
            if (argv[1] === "list") {
                const listCalls = calls.filter((call) => call.argv[1] === "list").length;
                if (deferredListCallNumber === listCalls) {
                    return new Promise((resolve) => { deferredListResolve = resolve; });
                }
                return Promise.resolve(JSON.stringify(listResponse));
            }
            if (argv[1] === "start") {
                if (this.failNextStart) {
                    this.failNextStart = false;
                    return Promise.reject(new Error("secret spawn output"));
                }
                return Promise.resolve(JSON.stringify(queuedPayload));
            }
            assert.strictEqual(argv[1], "repair");
            return Promise.resolve(JSON.stringify(repairPayload));
        }
    };

    const source = fs.readFileSync(require.resolve("../../deploy-local/cockpit/workspace_templates/workspace-templates.js"), "utf8");
    assert.ok(source.includes("refreshGeneration"));
    const timers = new FakeTimers();
    vm.runInNewContext(source, {
        document, cockpit, isFinite, console,
        setTimeout: timers.setTimeout.bind(timers),
        clearTimeout: timers.clearTimeout.bind(timers)
    });
    await flush();

    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].argv[1], "list");
    assert.strictEqual(document.elements.template.children.length, 2);
    assert.strictEqual(document.elements.assignee.children.length, 4);
    assert.strictEqual(document.elements["recent-list-body"].children.length, 3);
    assert.strictEqual(document.elements["recent-list-body"].children[0].children[0].textContent, "wtest");
    assert.strictEqual(document.elements["recent-list-body"].children[1].children[5].textContent, "Guacamole synchronization outcome is unknown; repair is required.");
    assert.notStrictEqual(document.elements["recent-list-body"].children[1].children[5].textContent, "secret-output-must-not-render");

    listResponse = activePayload;
    document.elements.name.value = "vm-new";
    document.elements.template.value = "windows11-v1";
    document.elements.assignee.value = "USER_GROUP:developers";
    document.elements["create-form"].listeners.submit({ preventDefault() {} });
    await flush();

    assert.strictEqual(calls.length, 3);
    assert.strictEqual(calls[1].argv[1], "start");
    assert.ok(calls[1].argv.indexOf("--name") >= 0);
    assert.ok(calls[1].argv.indexOf("vm-new") >= 0);
    assert.strictEqual(calls[1].options.superuser, "require");
    assert.strictEqual(calls[1].options.err, "message");
    assert.strictEqual(Object.prototype.hasOwnProperty.call(calls[1].options, "stream"), false);
    assert.strictEqual(document.elements["result-summary"].textContent, "Workspace vm-new is queued.");

    assert.strictEqual(calls[2].argv[1], "list");
    assert.strictEqual(timers.pending().length, 1);
    timers.runNext();
    await flush();
    assert.strictEqual(calls.filter((call) => call.argv[1] === "list").length, 3);
    assert.strictEqual(timers.pending().length, 1);
    listResponse = listPayload;
    timers.runNext();
    await flush();
    assert.strictEqual(timers.pending().length, 0);
    assert.strictEqual(document.elements["recent-list-body"].children.length, 3);

    const pendingRow = document.elements["recent-list-body"].children[1];
    deferredListCallNumber = calls.filter((call) => call.argv[1] === "list").length + 1;
    document.elements.name.value = "vm-newer";
    document.elements.template.value = "windows11-v1";
    document.elements.assignee.value = "USER_GROUP:developers";
    document.elements["create-form"].listeners.submit({ preventDefault() {} });
    await flush();
    assert.ok(deferredListResolve);
    const stalePayload = Object.assign({}, listPayload, { workspaces: [readyWorkspace], clones: [readyWorkspace] });
    const repairButton = pendingRow.children[6].children[0];
    assert.ok(repairButton);
    repairButton.listeners.click();
    await flush();
    const repairCall = calls.filter((call) => call.argv[1] === "repair").at(-1);
    assert.strictEqual(repairCall.argv.indexOf("vm-pending") >= 0, true);
    assert.strictEqual(calls.at(-1).argv[1], "list");
    deferredListResolve(JSON.stringify(stalePayload));
    await flush();
    assert.strictEqual(document.elements["recent-list-body"].children.length, 3);
    assert.strictEqual(document.elements["recent-list-body"].children[2].children[6].children.length, 0);

    const freshDocument = new Document();
    const freshTimers = new FakeTimers();
    listResponse = activePayload;
    vm.runInNewContext(source, {
        document: freshDocument, cockpit, isFinite, console,
        setTimeout: freshTimers.setTimeout.bind(freshTimers),
        clearTimeout: freshTimers.clearTimeout.bind(freshTimers)
    });
    await flush();
    assert.strictEqual(freshTimers.pending().length, 1);
    listResponse = listPayload;
    freshTimers.runNext();
    await flush();
    assert.strictEqual(freshTimers.pending().length, 0);

    cockpit.failNextStart = true;
    document.elements.name.value = "vm-failed";
    document.elements.template.value = "windows11-v1";
    document.elements.assignee.value = "USER_GROUP:developers";
    document.elements["create-form"].listeners.submit({ preventDefault() {} });
    await flush();
    assert.strictEqual(document.elements["error-code"].textContent, "SPAWN_FAILED");
    assert.strictEqual(document.elements["error-message"].textContent, "The workspace helper could not be started.");
    assert.strictEqual(document.elements["error-message"].textContent.includes("secret"), false);
    console.log("TASK6_COCKPIT_PERSISTENT_JOB_UI_TEST_OK");
}

main().catch((error) => {
    console.error(error.stack || error.message || error);
    process.exitCode = 1;
});
