(function () {
    "use strict";

    var HELPER = "/usr/local/libexec/guacamole-workspace-helper";
    var PROGRESS_STAGES = ["validation", "disk-overlay", "domain", "dhcp", "rdp", "guacamole", "permissions"];
    var STAGE_LABELS = {
        validation: "Validation",
        "disk-overlay": "Disk overlay",
        domain: "Libvirt domain",
        dhcp: "DHCP reservation",
        rdp: "RDP readiness",
        guacamole: "Guacamole connection",
        permissions: "Permissions"
    };
    var REPAIRABLE = ["pending", "waiting-rdp", "sync-failed", "failed", "stale"];
    var SAFE_ERROR_MESSAGES = {
        JOB_START_FAILED: "The workspace job could not be started.",
        JOB_UNIT_MISSING: "The workspace job stopped before completion; repair is available.",
        JOB_UNIT_FAILED: "The workspace job failed; repair is available.",
        JOB_UNIT_TERMINAL: "The workspace job stopped before completion; repair is available.",
        JOB_STALE: "The workspace job stopped responding; repair is available.",
        RDP_NOT_READY: "RDP is not ready yet; the workspace remains waiting.",
        SYNC_OWNERSHIP_UNAVAILABLE: "Guacamole ownership could not be proven; repair is required.",
        SYNC_OWNERSHIP_INVALID: "Guacamole ownership could not be proven; repair is required.",
        SYNC_COMMIT_UNKNOWN: "Guacamole synchronization outcome is unknown; repair is required.",
        ROLLBACK_FAILED: "Workspace compensation could not be confirmed; repair is required.",
        CLONE_STATE_INVALID: "The workspace identity could not be verified; repair is required.",
        ARTIFACTS_UNVERIFIED: "The failed workspace still has unverified artifacts; repair is disabled.",
        WORKSPACE_NOT_FOUND: "The workspace record is unavailable.",
        WORKSPACE_NOT_REPAIRABLE: "This workspace cannot be repaired safely.",
        SPAWN_FAILED: "The workspace helper could not be started."
    };
    var form = document.getElementById("create-form");
    var nameInput = document.getElementById("name");
    var templateSelect = document.getElementById("template");
    var assigneeSelect = document.getElementById("assignee");
    var memoryInput = document.getElementById("memory-mib");
    var vcpusInput = document.getElementById("vcpus");
    var submit = document.getElementById("submit");
    var stage = document.getElementById("stage");
    var progress = document.getElementById("progress");
    var error = document.getElementById("error");
    var errorCode = document.getElementById("error-code");
    var errorMessage = document.getElementById("error-message");
    var templateList = document.getElementById("template-list");
    var templateListBody = document.getElementById("template-list-body");
    var templateListEmpty = document.getElementById("template-list-empty");
    var recentList = document.getElementById("recent-list");
    var recentListBody = document.getElementById("recent-list-body");
    var recentEmpty = document.getElementById("recent-empty");
    var result = document.getElementById("result");
    var resultSummary = document.getElementById("result-summary");
    var resultDetails = document.getElementById("result-details");
    var resultLinks = document.getElementById("result-links");
    var running = false;
    var pollTimer = null;
    var refreshGeneration = 0;

    function beginRefresh() {
        refreshGeneration += 1;
        if (pollTimer) { clearTimeout(pollTimer); }
        pollTimer = null;
        return refreshGeneration;
    }

    function scheduleRefresh(payload, generation) {
        var activeItems = (payload.workspaces || []).concat(payload.jobs || []);
        var active = activeItems.some(function (item) {
            return item && (item.status === "queued" || item.status === "running" || item.jobStatus === "queued" || item.jobStatus === "running");
        });
        if (active && generation === refreshGeneration && typeof setTimeout === "function") {
            pollTimer = setTimeout(function () {
                if (generation === refreshGeneration) { refreshWorkspaceList().catch(function () {}); }
            }, 3000);
        }
    }

    function setStage(value) { stage.textContent = value; }

    function clearError() {
        error.hidden = true;
        errorCode.textContent = "";
        errorMessage.textContent = "";
    }

    function showError(code, message) {
        errorCode.textContent = code || "HELPER_FAILED";
        errorMessage.textContent = message || "The workspace helper did not complete the request.";
        error.hidden = false;
    }

    function parsePayload(output) {
        var payload = JSON.parse(output);
        if (!payload || typeof payload !== "object") {
            throw new Error("The workspace helper returned an invalid response.");
        }
        return payload;
    }

    function helperError(payload) {
        var candidate = typeof payload.code === "string" ? payload.code : "HELPER_FAILED";
        var code = SAFE_ERROR_MESSAGES[candidate] ? candidate : "HELPER_FAILED";
        return {
            code: code,
            message: SAFE_ERROR_MESSAGES[code] || "The workspace helper rejected the request."
        };
    }

    function showPayloadError(payload) {
        var failure = helperError(payload);
        showError(failure.code, failure.message);
        setStage(typeof payload.stage === "string" ? "Stage: " + payload.stage : "The operation failed.");
        setProgress(payload.progress);
    }

    function optionValue(value) {
        if (typeof value === "string") { return value; }
        if (value && typeof value === "object") {
            if (typeof value.username === "string") { return value.username; }
            if (typeof value.name === "string") { return value.name; }
            if (typeof value.identifier === "string") { return value.identifier; }
        }
        return "";
    }

    function addOption(select, value, label) {
        if (!value) { return; }
        var option = document.createElement("option");
        option.value = value;
        option.textContent = label || value;
        select.appendChild(option);
    }

    function resetOptions(select, placeholder) {
        while (select.firstChild) { select.removeChild(select.firstChild); }
        var option = document.createElement("option");
        option.value = "";
        option.textContent = placeholder;
        option.disabled = true;
        option.selected = true;
        select.appendChild(option);
    }

    function formatTemplateSize(value) {
        if (typeof value !== "number" || !isFinite(value) || value < 0) { return "Unknown"; }
        return String(value) + " bytes";
    }

    function renderTemplateList(templates) {
        while (templateListBody.firstChild) { templateListBody.removeChild(templateListBody.firstChild); }
        if (!templates.length) {
            templateList.hidden = true;
            templateListEmpty.hidden = false;
            return;
        }
        templates.forEach(function (template) {
            var row = document.createElement("tr");
            [
                optionValue(template && template.version),
                typeof template.createdAt === "string" ? template.createdAt : "Unknown",
                formatTemplateSize(template && template.virtualSize),
                typeof template.hashState === "string" ? template.hashState : "Unknown",
                typeof template.dependentCloneCount === "number" ? String(template.dependentCloneCount) : "0"
            ].forEach(function (value) {
                var cell = document.createElement("td");
                cell.textContent = value || "Unknown";
                row.appendChild(cell);
            });
            templateListBody.appendChild(row);
        });
        templateList.hidden = false;
        templateListEmpty.hidden = true;
    }

    function assigneeValue(item, fallbackType) {
        var type = item && typeof item === "object" && typeof item.type === "string" ? item.type : fallbackType;
        var name = optionValue(item);
        if (type !== "USER" && type !== "USER_GROUP") { return ""; }
        return name ? type + ":" + name : "";
    }

    function assigneeLabel(item, fallbackType) {
        var type = item && typeof item === "object" && typeof item.type === "string" ? item.type : fallbackType;
        var name = optionValue(item);
        var label = item && typeof item === "object" && typeof item.label === "string" ? item.label : name;
        return (type === "USER_GROUP" ? "Group: " : "User: ") + label;
    }

    function populateOptions(payload) {
        resetOptions(templateSelect, "Select a template");
        resetOptions(assigneeSelect, "Select a user or group");
        var templates = Array.isArray(payload.templates) ? payload.templates : [];
        renderTemplateList(templates);
        templates.forEach(function (template) {
            var value = optionValue(template && (template.version || template.name || template));
            var created = template && typeof template.createdAt === "string" ? " — " + template.createdAt : "";
            addOption(templateSelect, value, value + created);
        });
        var assignees = payload.guacamoleAssignees || payload.assignees || {};
        (Array.isArray(assignees.users) ? assignees.users : []).forEach(function (user) {
            addOption(assigneeSelect, assigneeValue(user, "USER"), assigneeLabel(user, "USER"));
        });
        (Array.isArray(assignees.groups) ? assignees.groups : []).forEach(function (group) {
            addOption(assigneeSelect, assigneeValue(group, "USER_GROUP"), assigneeLabel(group, "USER_GROUP"));
        });
        updateSubmitState();
    }

    function updateSubmitState() { submit.disabled = running || !templateSelect.value || !assigneeSelect.value; }

    function setRunning(value) {
        running = value;
        submit.disabled = value;
        form.querySelectorAll("input, select").forEach(function (control) { control.disabled = value; });
        if (!value) { updateSubmitState(); }
    }

    function setProgress(items) {
        while (progress.firstChild) { progress.removeChild(progress.firstChild); }
        var states = {};
        if (Array.isArray(items)) {
            items.forEach(function (item) {
                if (item && typeof item.stage === "string" && typeof item.status === "string") { states[item.stage] = item.status; }
            });
        }
        PROGRESS_STAGES.forEach(function (stageName) {
            var item = document.createElement("li");
            item.dataset.status = states[stageName] || "pending";
            item.textContent = STAGE_LABELS[stageName] + ": " + item.dataset.status;
            progress.appendChild(item);
        });
    }

    function clearResult() {
        result.hidden = true;
        resultSummary.textContent = "";
        while (resultDetails.firstChild) { resultDetails.removeChild(resultDetails.firstChild); }
        while (resultLinks.firstChild) { resultLinks.removeChild(resultLinks.firstChild); }
        resultLinks.hidden = true;
    }

    function addResultDetail(label, value) {
        if (!value) { return; }
        var term = document.createElement("dt");
        term.textContent = label;
        var description = document.createElement("dd");
        description.textContent = value;
        resultDetails.appendChild(term);
        resultDetails.appendChild(description);
    }

    function showReadyLinks() {
        while (resultLinks.firstChild) { resultLinks.removeChild(resultLinks.firstChild); }
        var machines = document.createElement("a");
        machines.href = "/machines/";
        machines.textContent = "Open Cockpit Machines";
        machines.target = "_blank";
        machines.rel = "noopener";
        var guacamole = document.createElement("a");
        guacamole.href = "/guacamole/";
        guacamole.textContent = "Open Guacamole";
        guacamole.target = "_blank";
        guacamole.rel = "noopener";
        resultLinks.appendChild(machines);
        resultLinks.appendChild(guacamole);
        resultLinks.hidden = false;
    }

    function workspaceValue(workspace, key) {
        if (workspace && typeof workspace[key] === "string") { return workspace[key]; }
        if (workspace && workspace.result && typeof workspace.result[key] === "string") { return workspace.result[key]; }
        return "";
    }

    function appendCell(row, value) {
        var cell = document.createElement("td");
        cell.textContent = value || "—";
        row.appendChild(cell);
        return cell;
    }

    function renderRecent(workspaces) {
        while (recentListBody.firstChild) { recentListBody.removeChild(recentListBody.firstChild); }
        var values = Array.isArray(workspaces) ? workspaces : [];
        recentList.hidden = values.length === 0;
        recentEmpty.hidden = values.length !== 0;
        values.forEach(function (workspace) {
            var row = document.createElement("tr");
            appendCell(row, workspaceValue(workspace, "name"));
            appendCell(row, workspaceValue(workspace, "templateVersion"));
            appendCell(row, workspaceValue(workspace, "ip"));
            var assignee = workspaceValue(workspace, "assigneeName");
            appendCell(row, assignee ? (workspaceValue(workspace, "assigneeType") === "USER_GROUP" ? "Group: " : "User: ") + assignee : "—");
            var status = workspaceValue(workspace, "status") || "pending";
            var statusCell = appendCell(row, status);
            statusCell.dataset.status = status;
            var errorCode = typeof workspace.errorCode === "string" ? workspace.errorCode : (workspace.error && typeof workspace.error.code === "string" ? workspace.error.code : "");
            var detail = errorCode ? (SAFE_ERROR_MESSAGES[errorCode] || "The workspace reported an error; repair may be required.") : (workspace.stage ? "Stage: " + workspace.stage : "");
            appendCell(row, detail);
            var actionCell = document.createElement("td");
            if (REPAIRABLE.indexOf(status) >= 0) {
                var repair = document.createElement("button");
                repair.type = "button";
                repair.textContent = "Repair / resume";
                repair.addEventListener("click", function () { repairWorkspace(workspaceValue(workspace, "name")); });
                actionCell.appendChild(repair);
            }
            row.appendChild(actionCell);
            recentListBody.appendChild(row);
        });
    }

    function showCloneResult(payload) {
        var job = payload.job && typeof payload.job === "object" ? payload.job : payload;
        var clone = payload.clone && typeof payload.clone === "object" ? payload.clone : (job.result && typeof job.result === "object" ? job.result : job);
        var status = typeof payload.status === "string" ? payload.status : (typeof job.status === "string" ? job.status : clone.status);
        if (payload.ok !== true) { showPayloadError(payload); return; }
        result.hidden = false;
        var name = typeof clone.name === "string" ? clone.name : (typeof job.name === "string" ? job.name : "workspace");
        resultSummary.textContent = "Workspace " + name + " is " + (status || "reported by the helper") + ".";
        addResultDetail("VM name", name);
        addResultDetail("IP address", typeof clone.ip === "string" ? clone.ip : "");
        addResultDetail("Assignee", typeof clone.assigneeName === "string" ? clone.assigneeName : (typeof job.assigneeName === "string" ? job.assigneeName : ""));
        if (typeof clone.connectionId === "number") { addResultDetail("Guacamole connection", String(clone.connectionId)); }
        setProgress(payload.progress || job.progress);
        setStage(status ? "Stage: " + (job.stage || payload.stage || status) : "Workspace request submitted.");
        if (status === "ready") { showReadyLinks(); }
    }

    function showSpawnError(reason) {
        showError("SPAWN_FAILED", SAFE_ERROR_MESSAGES.SPAWN_FAILED);
        setStage("The operation failed before a helper result was returned.");
    }

    function refreshWorkspaceList() {
        var generation = beginRefresh();
        return cockpit.spawn([HELPER, "list", "--json"], { superuser: "require", err: "message" }).then(function (output) {
            var payload = parsePayload(output);
            if (generation !== refreshGeneration) { return payload; }
            if (payload.ok !== true) { showPayloadError(payload); return payload; }
            renderRecent(payload.workspaces || payload.clones);
            scheduleRefresh(payload, generation);
            return payload;
        }).catch(function (reason) {
            if (generation === refreshGeneration) { showSpawnError(reason); }
            throw reason;
        });
    }

    function repairWorkspace(name) {
        if (!name) { return; }
        var generation = beginRefresh();
        setStage("Queueing repair for " + name + "...");
        cockpit.spawn([HELPER, "repair", "--name", name, "--json"], { superuser: "require", err: "message" }).then(function (output) {
            var payload = parsePayload(output);
            if (generation !== refreshGeneration) { return payload; }
            if (payload.ok !== true) { showPayloadError(payload); return; }
            showCloneResult(payload);
            return refreshWorkspaceList();
        }).catch(function (reason) {
            if (generation === refreshGeneration) { showSpawnError(reason); }
        });
    }

    function parseAssignee(value) {
        var separator = value.indexOf(":");
        var type = separator > 0 ? value.substring(0, separator) : "";
        var name = separator > 0 ? value.substring(separator + 1) : "";
        if (type === "USER") { return { flag: "--assign-user", name: name }; }
        if (type === "USER_GROUP") { return { flag: "--assign-group", name: name }; }
        return null;
    }

    function loadWorkspaceData() {
        var generation = beginRefresh();
        setStage("Loading templates and assignees...");
        setProgress();
        cockpit.spawn([HELPER, "list", "--json"], { superuser: "require", err: "message" }).then(function (output) {
            var payload = parsePayload(output);
            if (generation !== refreshGeneration) { return; }
            if (payload.ok !== true) { showPayloadError(payload); return; }
            populateOptions(payload);
            renderRecent(payload.workspaces || payload.clones);
            setStage("Ready to create a workspace.");
            scheduleRefresh(payload, generation);
        }).catch(function (reason) {
            if (generation === refreshGeneration) { showSpawnError(reason); }
        });
    }

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        clearError();
        clearResult();
        if (!form.reportValidity()) { return; }
        var name = nameInput.value.trim();
        var assignee = parseAssignee(assigneeSelect.value);
        if (!assignee) { showError("ASSIGNEE_INVALID", "Select a valid user or group."); return; }
        setRunning(true);
        var generation = beginRefresh();
        setProgress(PROGRESS_STAGES.map(function (stageName, index) { return { stage: stageName, status: index === 0 ? "running" : "pending" }; }));
        setStage("Queueing workspace creation...");
        cockpit.spawn([
            HELPER, "start", "--name", name, assignee.flag, assignee.name,
            "--template", templateSelect.value, "--memory-mib", memoryInput.value,
            "--vcpus", vcpusInput.value, "--wait-rdp-minutes", "20", "--json"
        ], { superuser: "require", err: "message" }).then(function (output) {
            var payload = parsePayload(output);
            if (generation !== refreshGeneration) { return payload; }
            if (payload.ok !== true) { showPayloadError(payload); return; }
            showCloneResult(payload);
            return refreshWorkspaceList();
        }).catch(function (reason) {
            if (generation === refreshGeneration) { showSpawnError(reason); }
        }).then(function () { setRunning(false); });
    });

    templateSelect.addEventListener("change", updateSubmitState);
    assigneeSelect.addEventListener("change", updateSubmitState);
    setProgress();
    loadWorkspaceData();
}());
