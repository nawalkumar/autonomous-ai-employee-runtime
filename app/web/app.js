/* Live dashboard for the Autonomous AI Employee Runtime.
 *
 * Every value rendered here comes from the HTTP API. Nothing is simulated in the
 * browser: task IDs, statuses, events, timestamps, approvals and verification
 * results are the persisted ones. Rendering uses textContent/createElement only.
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 1000;
  var POLL_MAX_ATTEMPTS = 120;
  var HEALTH_INTERVAL_MS = 30000;
  var REQUEST_TIMEOUT_MS = 15000;
  var TASK_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

  var STAGES = ["Goal", "Understand", "Plan", "Execute", "Observe", "Adapt", "Verify", "Complete"];
  var STAGE_LABELS = {
    pending: "pending",
    active: "running",
    success: "done",
    warning: "warning",
    failure: "failed",
    approval: "awaiting approval",
    skipped: "not needed"
  };
  var TERMINAL = { completed: true, failed: true, aborted: true };
  var FAIL_EVENTS = {
    TOOL_FAILED: 1, STEP_FAILED: 1, TASK_FAILED: 1, "failure.detected": 1,
    "verification.failed": 1, "approval.rejected": 1, "recovery.exhausted": 1
  };
  var WARN_EVENTS = {
    "recovery.evaluated": 1, "recovery.retry_started": 1, "demo.failure_injected": 1, "step.resumed": 1
  };
  var OK_EVENTS = {
    TOOL_SUCCEEDED: 1, STEP_SUCCEEDED: 1, TASK_COMPLETED: 1, "verification.passed": 1,
    "recovery.retry_succeeded": 1, "approval.approved": 1
  };

  function $(id) { return document.getElementById(id); }

  // --- DOM helpers ---------------------------------------------------------------

  function el(tag, opts, children) {
    var node = document.createElement(tag);
    opts = opts || {};
    if (opts.className) node.className = opts.className;
    if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (key) { node.setAttribute(key, opts.attrs[key]); });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function show(node, visible) { node.hidden = !visible; }
  function pretty(value) { return JSON.stringify(value, null, 2); }
  function compact(value) { return JSON.stringify(value); }
  function str(value) { return value === undefined || value === null || value === "" ? "—" : String(value); }
  function sentence(value) {
    var text = str(value);
    return /[.!?]$/.test(text) ? text : text + ".";
  }

  function pad(n, width) {
    var s = String(n);
    while (s.length < (width || 2)) s = "0" + s;
    return s;
  }

  function fmtTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) + "." + pad(d.getMilliseconds(), 3);
  }

  function fmtDateTime(iso) {
    var d = new Date(iso);
    return isNaN(d.getTime()) ? "—" : d.toLocaleString();
  }

  function setMsg(node, text, kind) {
    node.textContent = text || "";
    node.className = "form-msg" + (kind ? " " + kind : "");
  }

  function kvRows(container, rows) {
    clear(container);
    rows.forEach(function (row) {
      container.appendChild(el("div", { className: "k", text: row[0] }));
      container.appendChild(el("div", { className: "v", text: str(row[1]), attrs: { "data-field": row[0] } }));
    });
  }

  function details(summary, value) {
    return el("details", null, [
      el("summary", { text: summary }),
      el("pre", { className: "json", text: pretty(value) })
    ]);
  }

  function newKey() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID().replace(/[^A-Za-z0-9_-]/g, "");
    }
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.prototype.map.call(bytes, function (b) { return pad(b.toString(16)); }).join("");
  }

  function normalizeGoal(text) {
    var normalized = String(text || "").replace(/\u2019/g, "'").replace(/\s+/g, " ").trim().toLowerCase();
    if (normalized.slice(-1) === ".") normalized = normalized.slice(0, -1).trim();
    return normalized;
  }

  // --- API client ------------------------------------------------------------------

  function ApiError(status, code, message, retryAfter) {
    this.status = status;
    this.code = code;
    this.message = message;
    this.retryAfter = retryAfter;
  }

  async function request(method, path, body, options) {
    options = options || {};
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS);
    if (options.signal) {
      if (options.signal.aborted) controller.abort();
      else options.signal.addEventListener("abort", function () { controller.abort(); });
    }
    var headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
    var response;
    try {
      response = await fetch(path, {
        method: method,
        headers: headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal
      });
    } catch (error) {
      clearTimeout(timer);
      if (options.signal && options.signal.aborted) throw new ApiError(0, "aborted", "Request cancelled.");
      throw new ApiError(0, "network", "The backend is unreachable.");
    }
    var text;
    try {
      text = await response.text();
    } finally {
      clearTimeout(timer);
    }
    var data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (e) { data = null; }
    }
    if (!response.ok) {
      var err = data && data.error ? data.error : {};
      throw new ApiError(
        response.status,
        err.code || "http_" + response.status,
        err.message || "Request failed with HTTP " + response.status + ".",
        response.headers.get("Retry-After")
      );
    }
    return { status: response.status, data: data };
  }

  var api = {
    health: function (signal) { return request("GET", "/api/v1/health", undefined, { signal: signal }); },
    scenarios: function () { return request("GET", "/api/v1/scenarios"); },
    session: function () { return request("GET", "/api/v1/session"); },
    createSession: function (code) {
      return request("POST", "/api/v1/session", code ? { access_code: code } : {});
    },
    createTask: function (goal, inject, key) {
      var body = { goal: goal };
      if (inject) body.inject_failure = inject;
      return request("POST", "/api/v1/tasks", body, { idempotencyKey: key });
    },
    tasks: function () { return request("GET", "/api/v1/tasks?limit=25"); },
    task: function (id, signal) { return request("GET", "/api/v1/tasks/" + encodeURIComponent(id), undefined, { signal: signal }); },
    history: function (id, signal) {
      return request("GET", "/api/v1/tasks/" + encodeURIComponent(id) + "/history", undefined, { signal: signal });
    },
    verification: function (id, signal) {
      return request("GET", "/api/v1/tasks/" + encodeURIComponent(id) + "/verification", undefined, { signal: signal });
    },
    approve: function (id, stepId) {
      return request("POST", "/api/v1/tasks/" + encodeURIComponent(id) + "/approve", { step_id: stepId, confirm: true });
    },
    reject: function (id, stepId, reason) {
      return request("POST", "/api/v1/tasks/" + encodeURIComponent(id) + "/reject", { step_id: stepId, reason: reason, confirm: true });
    },
    resume: function (id) {
      return request("POST", "/api/v1/tasks/" + encodeURIComponent(id) + "/resume", { confirm: true });
    },
    world: function (tab) {
      var paths = {
        employees: "/api/v1/company/employees",
        tickets: "/api/v1/company/tickets",
        customers: "/api/v1/company/customers",
        files: "/api/v1/workspace/files"
      };
      return request("GET", paths[tab]);
    },
    file: function (path) { return request("GET", "/api/v1/workspace/file?path=" + encodeURIComponent(path)); }
  };

  // --- State -----------------------------------------------------------------------

  var state = {
    health: null,
    session: null,
    scenarios: null,
    online: true,
    ready: false,
    taskId: null,
    task: null,
    events: [],
    renderedEvents: {},
    verification: null,
    poll: null,
    submitting: false,
    acting: false,
    pendingSubmit: null,
    worldTab: "employees",
    worldSeq: 0
  };

  var els = {};
  [
    "modeNotice", "connectionBanner", "modeBadge", "versionBadge", "activeStageLabel", "statusPill",
    "sessionCard", "sessionText", "sessionForm", "accessCodeRow", "accessCode", "sessionBtn", "sessionMsg",
    "dashboard", "goalForm", "scenarioButtons", "goalInput", "goalHint", "injectRow", "injectToggle",
    "runBtn", "newTaskBtn", "formMsg", "mRecovery", "mVerify", "mStatus", "mEvidence", "explainBox",
    "pipeline", "approvalCard", "apStep", "apOperation", "apRisk", "apReason", "apArgs", "rejectReason",
    "approveBtn", "rejectBtn", "approvalMsg", "timeline", "eventCount", "planList", "runtimeState",
    "refreshTaskBtn", "resumeBtn", "taskMsg", "verificationPanel", "interpretedGoal", "taskList",
    "refreshListBtn", "worldPanel", "refreshWorldBtn", "confirmDialog", "confirmTitle", "confirmBody",
    "confirmDetail", "confirmCancel"
  ].forEach(function (id) { els[id] = $(id); });

  // --- Connection and session ------------------------------------------------------

  function showBanner(text, kind) {
    els.connectionBanner.textContent = text;
    els.connectionBanner.className = "banner " + (kind || "error");
    show(els.connectionBanner, true);
  }

  function hideBanner() { show(els.connectionBanner, false); }

  function setOnline(online) {
    state.online = online;
    if (!online) {
      els.modeBadge.textContent = "Backend unavailable";
      els.modeBadge.className = "badge down";
      showBanner("Backend unavailable. The dashboard will keep retrying; nothing shown below is being updated.");
    } else if (state.health) {
      renderModeBadge();
    }
    updateControls();
  }

  function renderModeBadge() {
    var publicMode = state.health.mode === "public";
    els.modeBadge.textContent = publicMode ? "Live · public demo" : "Live · local mode";
    els.modeBadge.className = "badge " + (publicMode ? "public" : "live");
    els.versionBadge.textContent = "v" + state.health.version;
    show(els.versionBadge, true);
  }

  function isPublic() { return !!(state.health && state.health.mode === "public"); }
  function hasAccess() { return !isPublic() || !!(state.session && state.session.authenticated); }

  function renderSession() {
    var s = state.session;
    els.modeNotice.textContent = s.notice;
    els.modeNotice.className = "disclaimer" + (s.mode === "public" ? " public" : "");
    var needSession = s.mode === "public" && !s.authenticated;
    show(els.sessionCard, needSession);
    show(els.dashboard, !needSession);
    if (needSession) {
      els.sessionText.textContent = s.notice;
      show(els.accessCodeRow, !!s.access_code_required);
    }
  }

  function sessionLost(message) {
    stopPolling();
    state.taskId = null;
    state.task = null;
    state.session = Object.assign({}, state.session || {}, { authenticated: false });
    if (state.session.mode) renderSession();
    setMsg(els.sessionMsg, message || "Your demo session ended. Start a new one.", "error");
    renderIdle();
  }

  async function onSessionSubmit(event) {
    event.preventDefault();
    els.sessionBtn.disabled = true;
    setMsg(els.sessionMsg, "Starting session…");
    try {
      var res = await api.createSession(els.accessCode.value.trim());
      els.accessCode.value = "";
      state.session = res.data;
      setMsg(els.sessionMsg, "");
      renderSession();
      await afterAccess();
    } catch (err) {
      setMsg(els.sessionMsg, err.message, "error");
    } finally {
      els.sessionBtn.disabled = false;
    }
  }

  async function checkHealth() {
    try {
      var res = await api.health();
      var previous = state.health;
      state.health = res.data;
      if (!state.online) hideBanner();
      setOnline(true);
      if (previous && previous.boot_id !== res.data.boot_id) {
        if (res.data.mode === "public") {
          sessionLost("The demo server restarted. All public demo state was erased; start a new session.");
          showBanner("The demo server restarted (for example after sleeping). All public demo tasks and data were erased.", "warn");
          var session = await api.session();
          state.session = session.data;
          renderSession();
        } else {
          showBanner("The server restarted. Local tasks are persisted; showing the stored state.", "warn");
          if (state.taskId) selectTask(state.taskId);
          loadTaskList();
        }
      }
      return true;
    } catch (err) {
      setOnline(false);
      return false;
    }
  }

  function scheduleHealth() {
    setTimeout(async function () {
      await checkHealth();
      scheduleHealth();
    }, state.online ? HEALTH_INTERVAL_MS : 5000);
  }

  // --- Scenarios and goal form -------------------------------------------------------

  function renderScenarios() {
    clear(els.scenarioButtons);
    state.scenarios.scenarios.forEach(function (scenario) {
      scenario.goals.forEach(function (goal, index) {
        var button = el("button", {
          className: "ghost",
          text: scenario.title + (index > 0 ? " + note" : ""),
          attrs: { type: "button", "aria-pressed": "false", "data-goal": goal, title: scenario.description }
        });
        button.addEventListener("click", function () {
          els.goalInput.value = goal;
          els.injectToggle.checked = false;
          onGoalInput();
          els.goalInput.focus();
        });
        els.scenarioButtons.appendChild(button);
      });
    });
  }

  function matchScenario(text) {
    if (!state.scenarios) return null;
    var normalized = normalizeGoal(text);
    var found = null;
    state.scenarios.scenarios.forEach(function (scenario) {
      scenario.goals.forEach(function (goal) {
        if (normalizeGoal(goal) === normalized) found = scenario;
      });
    });
    return found;
  }

  function onGoalInput() {
    var text = els.goalInput.value;
    var scenario = matchScenario(text);
    Array.prototype.forEach.call(els.scenarioButtons.querySelectorAll("button"), function (button) {
      var pressed = normalizeGoal(button.getAttribute("data-goal")) === normalizeGoal(text);
      button.setAttribute("aria-pressed", pressed ? "true" : "false");
    });
    if (!text.trim()) {
      els.goalHint.textContent = "Pick a scenario above or type one of the supported goal formats.";
      els.goalHint.className = "hint";
    } else if (scenario) {
      els.goalHint.textContent = scenario.description;
      els.goalHint.className = "hint";
    } else {
      els.goalHint.textContent =
        "Not one of the listed goals. The planner only accepts its supported formats and will reject anything else without creating a task.";
      els.goalHint.className = "hint warn";
    }
    var injectable = !!(scenario && scenario.injectable_operation && state.scenarios.failure_injection_enabled);
    show(els.injectRow, injectable);
    if (!injectable) els.injectToggle.checked = false;
  }

  function updateControls() {
    var blocked = !state.online || !hasAccess();
    els.runBtn.disabled = blocked || state.submitting;
    var pending = state.task && state.task.pending_action;
    els.approveBtn.disabled = blocked || state.acting || !pending;
    els.rejectBtn.disabled = blocked || state.acting || !pending;
    els.resumeBtn.disabled = blocked || state.acting;
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (state.submitting) return;
    var goal = els.goalInput.value.trim();
    if (!goal) {
      setMsg(els.formMsg, "Enter a goal first.", "error");
      els.goalInput.focus();
      return;
    }
    var inject = !els.injectRow.hidden && els.injectToggle.checked ? "create_ticket" : null;
    var fingerprint = goal + "\u0000" + inject;
    if (!state.pendingSubmit || state.pendingSubmit.fingerprint !== fingerprint) {
      state.pendingSubmit = { fingerprint: fingerprint, key: newKey() };
    }
    state.submitting = true;
    updateControls();
    setMsg(els.formMsg, "Submitting to the runtime…");
    try {
      var res = await api.createTask(goal, inject, state.pendingSubmit.key);
      state.pendingSubmit = null;
      var task = res.data;
      setMsg(
        els.formMsg,
        (res.status === 200 ? "This request was already accepted; showing task " : "Task created: ") +
          task.task_id + " (initial status " + task.status + ").",
        "ok"
      );
      selectTask(task.task_id, task);
      loadTaskList();
    } catch (err) {
      if (err.code === "network") {
        setMsg(els.formMsg, "The backend did not answer. Press Run Employee again to retry the same request safely; it will not create a duplicate task.", "error");
      } else {
        state.pendingSubmit = null;
        if (err.status === 401) sessionLost(err.message);
        setMsg(els.formMsg, err.message, "error");
      }
    } finally {
      state.submitting = false;
      updateControls();
    }
  }

  // --- Task selection and polling ---------------------------------------------------------

  function stopPolling() {
    if (!state.poll) return;
    clearTimeout(state.poll.timer);
    state.poll.controller.abort();
    state.poll = null;
  }

  function startPolling() {
    stopPolling();
    var poll = { attempts: 0, timer: null, controller: new AbortController() };
    state.poll = poll;

    async function tick() {
      if (state.poll !== poll) return;
      poll.attempts += 1;
      var settled = await refreshTask(poll.controller.signal);
      if (state.poll !== poll) return;
      if (settled) {
        stopPolling();
        return;
      }
      if (poll.attempts >= POLL_MAX_ATTEMPTS) {
        stopPolling();
        setMsg(els.taskMsg, "Automatic refresh stopped after " + POLL_MAX_ATTEMPTS + " checks. Press Refresh to check again.", "error");
        return;
      }
      poll.timer = setTimeout(tick, POLL_INTERVAL_MS);
    }
    tick();
  }

  function selectTask(taskId, initial) {
    if (!TASK_ID_RE.test(taskId)) return;
    stopPolling();
    var changed = state.taskId !== taskId;
    state.taskId = taskId;
    if (changed) {
      state.events = [];
      state.renderedEvents = {};
      state.verification = null;
      clear(els.timeline);
      setMsg(els.approvalMsg, "");
    }
    setMsg(els.taskMsg, "");
    if (location.hash !== "#task=" + taskId) history.replaceState(null, "", "#task=" + taskId);
    if (initial) {
      state.task = initial;
      renderTask();
    } else if (changed) {
      state.task = null;
      els.statusPill.textContent = "loading";
      els.statusPill.className = "status-pill idle loading";
    }
    highlightTaskList();
    startPolling();
  }

  async function refreshTask(signal) {
    var id = state.taskId;
    if (!id) return true;
    try {
      // Task first, then history: once the task reports it is no longer executing,
      // a history read that starts afterwards includes every event of that run.
      var task = await api.task(id, signal);
      var history = await api.history(id, signal);
      if (state.taskId !== id) return true;
      state.task = task.data;
      state.events = history.data.events;
      if (!state.online) setOnline(true);
      var t = state.task;
      var settled = !t.executing && (!!TERMINAL[t.status] || t.status === "needs_approval" || t.resumable);
      if (settled) {
        var verification = await api.verification(id, signal);
        if (state.taskId !== id) return true;
        state.verification = verification.data;
      }
      renderTask();
      if (settled) {
        loadTaskList();
        loadWorld();
      }
      return settled;
    } catch (err) {
      if (err.code === "aborted") return true;
      if (err.status === 401) {
        sessionLost(err.message);
        return true;
      }
      if (err.status === 404) {
        setMsg(els.taskMsg, "Task " + id + " was not found here.", "error");
        return true;
      }
      if (err.code === "network") {
        setOnline(false);
        return false;
      }
      setMsg(els.taskMsg, err.message, "error");
      return false;
    }
  }

  // --- Rendering: task ----------------------------------------------------------------

  function renderIdle() {
    els.statusPill.textContent = "idle";
    els.statusPill.className = "status-pill idle";
    els.activeStageLabel.textContent = "Idle";
    renderPipeline(null);
    els.mRecovery.textContent = "—";
    els.mRecovery.className = "v";
    els.mVerify.textContent = "—";
    els.mVerify.className = "v";
    els.mStatus.textContent = "IDLE";
    els.mStatus.className = "v";
    els.mEvidence.textContent = "—";
    els.mEvidence.className = "v";
    els.explainBox.textContent =
      "Choose a scenario and click Run Employee. The task runs on the real runtime; this page shows the persisted state.";
    clear(els.timeline);
    els.timeline.appendChild(el("div", { className: "empty", text: "No task selected." }));
    els.eventCount.textContent = "";
    clear(els.planList);
    els.planList.appendChild(el("li", { className: "empty", text: "The plan appears after the planner accepts a goal." }));
    kvRows(els.runtimeState, [["task_id", null], ["status", null]]);
    clear(els.verificationPanel);
    els.verificationPanel.appendChild(el("div", { className: "empty", text: "Run or select a task to see its verification." }));
    kvRows(els.interpretedGoal, [["objective", null], ["planner", "mock (deterministic; two supported goal formats)"]]);
    els.approvalCard.classList.remove("visible");
    show(els.resumeBtn, false);
    els.refreshTaskBtn.disabled = true;
    highlightTaskList();
    updateControls();
  }

  function renderTask() {
    var t = state.task;
    if (!t) return;
    els.statusPill.textContent = t.status;
    els.statusPill.className = "status-pill " + t.status;
    els.refreshTaskBtn.disabled = false;
    renderMetrics(t);
    renderPipeline(t);
    renderTimeline();
    renderPlan(t);
    renderRuntimeState(t);
    renderApproval(t);
    renderVerification(t);
    renderInterpreted(t);
    renderExplain(t);
    show(els.resumeBtn, !!t.resumable);
    updateControls();
  }

  function renderMetrics(t) {
    els.mRecovery.textContent = String(t.retry_count);
    els.mRecovery.className = "v" + (t.retry_count > 0 ? " warn" : "");
    var verification = t.verification_status.toUpperCase();
    if (t.verification_checks_total) {
      verification += " " + t.verification_checks_passed + "/" + t.verification_checks_total;
    }
    els.mVerify.textContent = verification;
    els.mVerify.className = "v" + (t.verification_status === "passed" ? " ok" : t.verification_status === "failed" ? " bad" : "");
    els.mStatus.textContent = t.status.toUpperCase();
    els.mStatus.className =
      "v" + (t.status === "completed" ? " ok" : t.status === "failed" || t.status === "aborted" ? " bad" : t.status === "needs_approval" ? " warn" : "");
    els.mEvidence.textContent = t.evidence_count + " item" + (t.evidence_count === 1 ? "" : "s");
    els.mEvidence.className = "v" + (t.evidence_count ? " ok" : "");
  }

  function countTypes(events) {
    var counts = {};
    events.forEach(function (e) { counts[e.event_type] = (counts[e.event_type] || 0) + 1; });
    return counts;
  }

  // Each stage is derived from persisted task state and audit events only.
  function deriveStages(t, events) {
    var c = countTypes(events);
    var s = {};
    var labels = {};
    s.Goal = "success";
    s.Understand = t.interpreted_goal ? "success" : "pending";
    s.Plan = t.plan.length ? "success" : "pending";
    var failedStep = t.plan.some(function (p) { return p.status === "failed"; });
    var allDone = t.plan.length > 0 && t.plan.every(function (p) { return p.status === "completed"; });
    if (t.status === "needs_approval") s.Execute = "approval";
    else if (t.approval_status === "rejected") {
      s.Execute = "failure";
      labels.Execute = "write rejected";
    } else if (failedStep || (t.status === "aborted" && !allDone)) s.Execute = "failure";
    else if (allDone) s.Execute = "success";
    else if (c.TOOL_CALLED || t.executing) s.Execute = TERMINAL[t.status] ? "failure" : "active";
    else s.Execute = "pending";
    if (c.TOOL_FAILED) {
      s.Observe = "warning";
      labels.Observe = c.TOOL_FAILED + " failure" + (c.TOOL_FAILED > 1 ? "s" : "") + " observed";
    } else {
      s.Observe = c.TOOL_SUCCEEDED ? "success" : "pending";
    }
    if (c["recovery.exhausted"]) s.Adapt = "failure";
    else if (c["recovery.retry_succeeded"]) {
      s.Adapt = "success";
      labels.Adapt = "recovered";
    } else if (c["recovery.retry_started"]) s.Adapt = TERMINAL[t.status] ? "failure" : "active";
    else if (c["recovery.evaluated"]) {
      s.Adapt = "failure";
      labels.Adapt = "no retry";
    } else s.Adapt = TERMINAL[t.status] ? "skipped" : "pending";
    if (c["verification.passed"]) s.Verify = "success";
    else if (c["verification.failed"]) s.Verify = "failure";
    else if (c["verification.started"]) s.Verify = "active";
    else s.Verify = TERMINAL[t.status] ? "skipped" : "pending";
    if (t.status === "completed") s.Complete = "success";
    else if (t.status === "failed" || t.status === "aborted") {
      s.Complete = "failure";
      labels.Complete = t.status;
    } else s.Complete = "pending";
    return { states: s, labels: labels };
  }

  function renderPipeline(t) {
    var derived = t ? deriveStages(t, state.events) : { states: {}, labels: {} };
    clear(els.pipeline);
    var activeName = "Idle";
    STAGES.forEach(function (name, i) {
      var st = derived.states[name] || "pending";
      if (st === "active" || st === "approval") activeName = name + (st === "approval" ? " (awaiting approval)" : "");
      else if (activeName.indexOf("(") === -1 && (st === "success" || st === "failure" || st === "warning")) activeName = name;
      var label = derived.labels[name] || STAGE_LABELS[st];
      els.pipeline.appendChild(
        el("div", { className: "stage " + st, attrs: { "data-stage": name, "data-state": st } }, [
          el("div", { className: "idx", text: "0" + (i + 1) }),
          el("div", { className: "name", text: name }),
          el("div", null, [el("span", { className: "dot" }), el("span", { className: "state-label", text: label })])
        ])
      );
    });
    els.activeStageLabel.textContent = activeName;
  }

  function eventClass(type) {
    if (type === "approval.requested") return "approval";
    if (FAIL_EVENTS[type]) return "fail";
    if (WARN_EVENTS[type]) return "warn";
    if (OK_EVENTS[type]) return "ok";
    return "";
  }

  function summarizeData(data) {
    if (!data) return "";
    if (data.ticket) return "ticket " + data.ticket.ticket_id + " for " + data.ticket.customer_id + (data.idempotent_replay ? " (idempotent replay)" : "");
    if (data.employee) return "employee " + data.employee.employee_id + " title '" + data.employee.title + "'";
    if (data.customers) return data.count + " customer match(es)" + (data.customers[0] ? ": " + data.customers[0].customer_id : "");
    if (data.path) return "file " + data.path + (data.bytes_written !== undefined ? " (" + data.bytes_written + " bytes)" : "");
    return "";
  }

  function eventBrief(e) {
    var r = e.result || {};
    var f = e.failure || {};
    var a = e.arguments || {};
    var o = e.observation || {};
    switch (e.event_type) {
      case "TASK_CREATED": return "goal accepted; " + str(r.plan_steps) + " planned step(s)";
      case "demo.failure_injected": return "SIMULATED: " + str(r.message) + " will hit the next company_api." + str(r.operation) + " call";
      case "PLAN_STARTED": return "execution started";
      case "STEP_STARTED": return str(a.step_id) + ": " + str(a.description);
      case "step.resumed": return "resumed " + str(r.step_id) + " after an interrupted call";
      case "TOOL_CALLED": return str(r.step_id) + " · attempt " + str(r.attempt);
      case "TOOL_SUCCEEDED": return summarizeData(r.data);
      case "TOOL_FAILED": return str(f.failure_type) + ": " + str(f.message) + " (attempt " + str(f.attempt) + ")";
      case "STEP_SUCCEEDED":
      case "STEP_FAILED": return str(o.summary);
      case "failure.detected": return "classified " + str(f.failure_type) + " on " + str(f.step_id);
      case "recovery.evaluated": return str(r.action) + " — " + str(r.reason) + " (retry budget " + str(r.max_recovery_attempts) + ")";
      case "recovery.retry_started": return "retrying " + str(r.step_id) + ": attempt " + str(r.next_attempt);
      case "recovery.retry_succeeded": return str(r.step_id) + " succeeded on attempt " + str(r.attempt);
      case "recovery.exhausted": return str(f.reason);
      case "approval.requested": return str(r.step_id) + " · risk " + str(r.risk) + " · " + str(r.reason);
      case "approval.approved": return str(r.step_id) + " approved by " + (r.approver || "an unrecorded approver");
      case "approval.rejected": return str(r.step_id) + " rejected by " + (r.approver || "an unrecorded approver") + ": " + str(r.reason);
      case "verification.started": return "independent checks against the database and workspace";
      case "verification.check": return (r.passed ? "PASS " : "FAIL ") + str(r.name) + " — " + str(r.message);
      case "evidence.recorded": return str(r.type) + ": " + str(r.description);
      case "verification.passed": return str(r.summary);
      case "verification.failed":
      case "TASK_FAILED": return str(f.message);
      case "TASK_COMPLETED": return str(r.summary);
      default: return "";
    }
  }

  function eventTag(e) {
    if (!e.tool) return null;
    var op = (e.arguments && e.arguments.operation) || (e.result && e.result.operation);
    return e.tool + (op ? "." + op : "");
  }

  function renderTimeline() {
    var events = state.events;
    if (Object.keys(state.renderedEvents).length > events.length) {
      state.renderedEvents = {};
      clear(els.timeline);
    }
    var empty = els.timeline.querySelector(".empty");
    if (empty && events.length) els.timeline.removeChild(empty);
    var added = false;
    events.forEach(function (e) {
      if (state.renderedEvents[e.event_id]) return;
      state.renderedEvents[e.event_id] = true;
      added = true;
      var tag = eventTag(e);
      var payload = {};
      ["actor", "arguments", "result", "observation", "failure", "verification_result"].forEach(function (key) {
        if (e[key] !== null && e[key] !== undefined) payload[key] = e[key];
      });
      var simulated = e.event_type === "demo.failure_injected" ||
        (e.failure && typeof e.failure.message === "string" && e.failure.message.indexOf("simulated") !== -1);
      els.timeline.appendChild(
        el("div", { className: "tl-item " + eventClass(e.event_type), attrs: { "data-event-type": e.event_type, "data-event-id": e.event_id } }, [
          el("div", { className: "t", text: fmtTime(e.timestamp), attrs: { title: e.timestamp } }),
          el("div", { className: "m" }, [
            el("span", { className: "et", text: e.event_type }),
            tag ? el("span", { className: "tag", text: tag }) : null,
            simulated ? el("span", { className: "tag sim", text: "simulated" }) : null,
            el("span", { className: "brief", text: eventBrief(e) }),
            details("details", payload)
          ])
        ])
      );
    });
    if (!events.length && !els.timeline.firstChild) {
      els.timeline.appendChild(el("div", { className: "empty", text: "No events recorded yet." }));
    }
    els.eventCount.textContent = events.length + " event" + (events.length === 1 ? "" : "s");
    if (added) els.timeline.scrollTop = els.timeline.scrollHeight;
  }

  function renderPlan(t) {
    clear(els.planList);
    var pendingStep = t.pending_action && t.pending_action.step_id;
    t.plan.forEach(function (step, i) {
      var cls = { running: "active", completed: "done", failed: "failed" }[step.status] || "";
      var status = step.status;
      if (step.step_id === pendingStep) {
        cls = "blocked";
        status = "awaiting approval";
      } else if (t.status === "aborted" && step.status === "pending") {
        status = "not executed";
      }
      els.planList.appendChild(
        el("li", { className: cls, attrs: { "data-step-id": step.step_id, "data-status": step.status } }, [
          el("div", { className: "step-badge", text: String(i + 1) }),
          el("div", null, [
            el("div", { className: "title", text: step.step_id + ": " + step.description }),
            el("div", {
              className: "meta",
              text: str(step.tool) + " / " + str(step.operation) + " · " + status.toUpperCase() +
                " · " + step.attempts + " attempt" + (step.attempts === 1 ? "" : "s")
            }),
            details("arguments", step.arguments)
          ])
        ])
      );
    });
    if (!t.plan.length) els.planList.appendChild(el("li", { className: "empty", text: "No plan steps." }));
  }

  function renderRuntimeState(t) {
    var current = t.plan[t.current_step];
    kvRows(els.runtimeState, [
      ["task_id", t.task_id],
      ["status", t.status],
      ["current_step", current ? current.step_id : t.terminal ? "done" : "—"],
      ["retry_count", t.retry_count],
      ["approval_status", t.approval_status],
      ["verification", t.verification_status],
      ["executing", t.executing ? "yes (this server)" : "no"],
      ["created_at", fmtDateTime(t.created_at)],
      ["updated_at", fmtDateTime(t.updated_at)],
      ["tool_calls", t.tool_calls.length],
      ["failures", t.failures.length]
    ]);
  }

  function renderApproval(t) {
    var pending = t.status === "needs_approval" ? t.pending_action : null;
    els.approvalCard.classList.toggle("visible", !!pending);
    if (!pending) return;
    els.apStep.textContent = str(pending.step_id) + " · " + pending.tool;
    els.apOperation.textContent = str(pending.operation);
    els.apRisk.textContent = pending.risk;
    els.apReason.textContent = str(pending.reason);
    els.apArgs.textContent = pretty(pending.arguments);
  }

  function renderVerification(t) {
    var panel = els.verificationPanel;
    clear(panel);
    var v = state.verification && state.verification.task_id === t.task_id ? state.verification : null;
    if (!v || v.status === "pending") {
      var why = t.status === "aborted"
        ? "Verification did not run: the task was aborted before completion."
        : t.status === "needs_approval"
          ? "Verification runs after every step has completed; this task is waiting for approval."
          : TERMINAL[t.status]
            ? "Verification did not run for this task."
            : "Verification runs after execution finishes.";
      panel.appendChild(el("div", { className: "empty", text: why }));
      if (t.status === "failed" && t.completion_summary) {
        panel.appendChild(el("div", { className: "section-label", text: "Failure reason" }));
        panel.appendChild(el("div", { className: "ev-row", text: t.completion_summary }));
      }
      return;
    }
    panel.appendChild(
      el("div", { className: "kv" }, [
        el("div", { className: "k", text: "result" }),
        el("div", { className: "v " + (v.status === "passed" ? "ok" : "bad"), text: v.status.toUpperCase() + " · " + v.checks_passed + "/" + v.checks_total + " checks", attrs: { "data-field": "verification_result" } }),
        el("div", { className: "k", text: "summary" }),
        el("div", { className: "v", text: str(v.summary) })
      ])
    );
    panel.appendChild(el("div", { className: "section-label", text: "Checks (against actual database and workspace state)" }));
    var list = el("ul", { className: "checks" });
    v.checks.forEach(function (check) {
      list.appendChild(
        el("li", { className: check.passed ? "pass" : "fail", attrs: { "data-check-name": check.name, "data-passed": String(check.passed) } }, [
          el("span", { className: "mark", text: check.passed ? "PASS" : "FAIL" }),
          el("strong", { text: check.name }),
          " — " + check.message,
          details("expected / actual", { expected: check.expected, actual: check.actual })
        ])
      );
    });
    panel.appendChild(list);
    if (v.failure_reasons.length) {
      panel.appendChild(el("div", { className: "section-label", text: "Failure reasons" }));
      v.failure_reasons.forEach(function (reason) {
        panel.appendChild(el("div", { className: "ev-row", text: reason }));
      });
    }
    panel.appendChild(el("div", { className: "section-label", text: "Evidence (" + v.evidence.length + ")" }));
    var evidence = el("div", { className: "evidence-list" });
    v.evidence.forEach(function (item) {
      evidence.appendChild(
        el("div", { className: "ev-row", attrs: { "data-evidence-type": item.type } }, [
          el("span", { className: "k", text: item.type }),
          " ",
          el("span", { className: "d", text: item.description }),
          details("data", item.data)
        ])
      );
    });
    panel.appendChild(evidence);
  }

  function renderInterpreted(t) {
    var g = t.interpreted_goal;
    if (!g) {
      kvRows(els.interpretedGoal, [["goal", t.user_goal], ["objective", null]]);
      return;
    }
    var entities = Object.keys(g.entities).map(function (k) { return k + "=" + compact(g.entities[k]); }).join("; ");
    var criteria = g.success_criteria.map(function (c) { return c.criterion_type + ": " + c.description; }).join(" | ");
    kvRows(els.interpretedGoal, [
      ["goal", t.user_goal],
      ["objective", g.objective],
      ["entities", entities],
      ["required_info", g.required_information.join("; ")],
      ["success_criteria", criteria],
      ["planner", "mock (deterministic; two supported goal formats)"]
    ]);
  }

  function renderExplain(t) {
    var text;
    if (t.status === "needs_approval") {
      text = str(t.completion_summary) + " Review the pending action below, then approve or reject it.";
    } else if (t.resumable) {
      text = "This task is " + t.status + " but is not executing on this server (for example after a restart or a CLI run that stopped). Resume continues from the persisted state.";
    } else if (t.status === "queued" || t.status === "running") {
      text = "Executing on the runtime. This view refreshes from the API every second while the task is active.";
    } else if (t.status === "completed") {
      text = str(t.completion_summary);
    } else if (t.status === "aborted") {
      text = "Aborted: " + sentence(t.completion_summary) +
        (t.approval_status === "rejected" ? " The pending write was never executed." : "");
    } else {
      text = "Failed: " + str(t.completion_summary);
    }
    els.explainBox.textContent = text;
  }

  // --- Confirmation dialog ------------------------------------------------------------

  function confirmAction(title, body, detail) {
    return new Promise(function (resolve) {
      var dialog = els.confirmDialog;
      if (typeof dialog.showModal !== "function") {
        resolve(window.confirm(title + "\n\n" + body));
        return;
      }
      els.confirmTitle.textContent = title;
      els.confirmBody.textContent = body;
      els.confirmDetail.textContent = detail || "";
      show(els.confirmDetail, !!detail);
      dialog.returnValue = "";
      dialog.addEventListener("close", function onClose() {
        dialog.removeEventListener("close", onClose);
        resolve(dialog.returnValue === "confirm");
      });
      dialog.showModal();
      els.confirmCancel.focus();
    });
  }

  // --- Approval, rejection, resume ----------------------------------------------------

  function afterAction(task, message, msgNode) {
    state.task = task;
    state.verification = null;
    renderTask();
    setMsg(msgNode || els.approvalMsg, message, "ok");
    startPolling();
  }

  async function onApprove() {
    var t = state.task;
    if (!t || !t.pending_action || state.acting) return;
    var pending = t.pending_action;
    var ok = await confirmAction(
      "Approve this write?",
      "The server will execute " + pending.tool + "." + pending.operation + " for " + pending.step_id +
        " in task " + t.task_id + ", then continue the plan and verify the outcome.",
      pretty(pending.arguments)
    );
    if (!ok) return;
    state.acting = true;
    updateControls();
    setMsg(els.approvalMsg, "Sending approval…");
    try {
      var res = await api.approve(t.task_id, pending.step_id);
      afterAction(res.data, "Approved. Resulting task status: " + res.data.status + ".");
    } catch (err) {
      if (err.status === 401) sessionLost(err.message);
      setMsg(els.approvalMsg, err.message, "error");
      refreshTask();
    } finally {
      state.acting = false;
      updateControls();
    }
  }

  async function onReject() {
    var t = state.task;
    if (!t || !t.pending_action || state.acting) return;
    var reason = els.rejectReason.value.trim();
    if (!reason) {
      setMsg(els.approvalMsg, "Enter a rejection reason.", "error");
      els.rejectReason.focus();
      return;
    }
    var pending = t.pending_action;
    var ok = await confirmAction(
      "Reject this write?",
      "The task will be aborted and " + pending.tool + "." + pending.operation + " will not run. Reason: " + reason
    );
    if (!ok) return;
    state.acting = true;
    updateControls();
    setMsg(els.approvalMsg, "Sending rejection…");
    try {
      var res = await api.reject(t.task_id, pending.step_id, reason);
      afterAction(res.data, "Rejected. Resulting task status: " + res.data.status + ".");
    } catch (err) {
      if (err.status === 401) sessionLost(err.message);
      setMsg(els.approvalMsg, err.message, "error");
      refreshTask();
    } finally {
      state.acting = false;
      updateControls();
    }
  }

  async function onResume() {
    var t = state.task;
    if (!t || !t.resumable || state.acting) return;
    var ok = await confirmAction(
      "Resume this task?",
      "The server will continue task " + t.task_id + " from its persisted state. Interrupted calls are retried only within the retry budget and with the same idempotency key."
    );
    if (!ok) return;
    state.acting = true;
    updateControls();
    setMsg(els.taskMsg, "Resuming…");
    try {
      var res = await api.resume(t.task_id);
      afterAction(res.data, "Resumed. Resulting task status: " + res.data.status + ".", els.taskMsg);
    } catch (err) {
      if (err.status === 401) sessionLost(err.message);
      setMsg(els.taskMsg, err.message, "error");
    } finally {
      state.acting = false;
      updateControls();
    }
  }

  // --- Task history -------------------------------------------------------------------

  async function loadTaskList() {
    if (!hasAccess()) return;
    try {
      var res = await api.tasks();
      renderTaskList(res.data);
    } catch (err) {
      if (err.status === 401) return sessionLost(err.message);
      clear(els.taskList);
      els.taskList.appendChild(el("li", { className: "empty", text: "Could not load tasks: " + err.message }));
    }
  }

  function renderTaskList(data) {
    clear(els.taskList);
    if (!data.tasks.length) {
      els.taskList.appendChild(el("li", { className: "empty", text: "No tasks yet. Submit a goal to create one." }));
      return;
    }
    data.tasks.forEach(function (task) {
      var button = el("button", { attrs: { type: "button", "data-task-id": task.task_id } }, [
        el("span", { className: "goal", text: task.readable ? task.user_goal : "Unreadable task snapshot" }),
        el("span", { className: "status-pill small " + task.status, text: task.status }),
        el("span", {
          className: "sub",
          text: task.task_id + " · " + fmtDateTime(task.created_at) +
            (task.readable ? " · " + task.steps_completed + "/" + task.steps_total + " steps" : "")
        })
      ]);
      button.addEventListener("click", function () { selectTask(task.task_id); });
      els.taskList.appendChild(el("li", null, [button]));
    });
    if (data.total > data.tasks.length) {
      els.taskList.appendChild(el("li", { className: "empty", text: "Showing the newest " + data.tasks.length + " of " + data.total + " tasks." }));
    }
    highlightTaskList();
  }

  function highlightTaskList() {
    Array.prototype.forEach.call(els.taskList.querySelectorAll("button[data-task-id]"), function (button) {
      button.setAttribute("aria-current", button.getAttribute("data-task-id") === state.taskId ? "true" : "false");
    });
  }

  // --- Company and workspace -------------------------------------------------------

  function table(columns, rows) {
    var head = el("tr", null, columns.map(function (c) { return el("th", { text: c[0], attrs: { scope: "col" } }); }));
    var body = rows.map(function (row) {
      return el("tr", null, columns.map(function (c) {
        return el("td", { className: c[2] ? "mono" : "", text: str(row[c[1]]), attrs: { "data-col": c[1] } });
      }));
    });
    return el("div", { className: "table-wrap" }, [el("table", { className: "data" }, [el("thead", null, [head]), el("tbody", null, body)])]);
  }

  async function loadWorld() {
    if (!hasAccess()) return;
    var tab = state.worldTab;
    var seq = ++state.worldSeq;
    var panel = els.worldPanel;
    try {
      var res = await api.world(tab);
      if (seq !== state.worldSeq) return;
      clear(panel);
      var data = res.data;
      if (tab === "employees") {
        panel.appendChild(table([["ID", "employee_id", true], ["Name", "name"], ["Title", "title"], ["Department", "department"], ["Updated", "updated_at", true]], data.employees.map(withLocalTime("updated_at"))));
      } else if (tab === "tickets") {
        if (!data.tickets.length) panel.appendChild(el("div", { className: "empty", text: "No tickets yet." }));
        else panel.appendChild(table([["ID", "ticket_id", true], ["Customer", "customer_id", true], ["Subject", "subject"], ["Priority", "priority"], ["Status", "status"], ["Created", "created_at", true]], data.tickets.map(withLocalTime("created_at"))));
      } else if (tab === "customers") {
        panel.appendChild(table([["ID", "customer_id", true], ["Name", "name"], ["Company", "company"], ["Email", "email", true]], data.customers));
      } else {
        renderFiles(panel, data.files);
      }
    } catch (err) {
      if (seq !== state.worldSeq) return;
      if (err.status === 401) return sessionLost(err.message);
      clear(panel);
      panel.appendChild(el("div", { className: "empty", text: "Could not load data: " + err.message }));
    }
  }

  function withLocalTime(field) {
    return function (row) {
      var copy = Object.assign({}, row);
      copy[field] = fmtDateTime(row[field]);
      return copy;
    };
  }

  function renderFiles(panel, files) {
    if (!files.length) {
      panel.appendChild(el("div", { className: "empty", text: "The workspace has no files yet." }));
      return;
    }
    var viewer = el("div", { className: "file-view" });
    var list = el("ul", { className: "checks" });
    files.forEach(function (file) {
      var button = el("button", { className: "small", text: "View", attrs: { type: "button", "aria-label": "View " + file.path } });
      button.addEventListener("click", async function () {
        clear(viewer);
        viewer.appendChild(el("div", { className: "empty loading", text: "Loading " + file.path }));
        try {
          var res = await api.file(file.path);
          clear(viewer);
          viewer.appendChild(el("div", { className: "section-label", text: res.data.path + " (" + res.data.size_bytes + " bytes)" }));
          viewer.appendChild(el("pre", { className: "json", text: res.data.content, attrs: { "data-file-content": res.data.path } }));
        } catch (err) {
          clear(viewer);
          viewer.appendChild(el("div", { className: "empty", text: err.message }));
        }
      });
      list.appendChild(el("li", { attrs: { "data-file": file.path } }, [el("strong", { text: file.path }), " · " + file.size_bytes + " bytes ", button]));
    });
    panel.appendChild(list);
    panel.appendChild(viewer);
  }

  function selectTab(tab) {
    state.worldTab = tab;
    Array.prototype.forEach.call(document.querySelectorAll(".tabs button"), function (button) {
      button.setAttribute("aria-selected", button.getAttribute("data-tab") === tab ? "true" : "false");
    });
    clear(els.worldPanel);
    els.worldPanel.appendChild(el("div", { className: "empty loading", text: "Loading" }));
    loadWorld();
  }

  // --- Startup ----------------------------------------------------------------------

  async function afterAccess() {
    updateControls();
    loadTaskList();
    selectTab(state.worldTab);
    var match = /^#task=([A-Za-z0-9_-]{1,128})$/.exec(location.hash);
    if (match) selectTask(match[1]);
    else renderIdle();
  }

  function bind() {
    els.goalForm.addEventListener("submit", onSubmit);
    els.goalInput.addEventListener("input", onGoalInput);
    els.sessionForm.addEventListener("submit", onSessionSubmit);
    els.newTaskBtn.addEventListener("click", function () {
      stopPolling();
      state.taskId = null;
      state.task = null;
      history.replaceState(null, "", location.pathname);
      setMsg(els.formMsg, "");
      renderIdle();
      els.goalInput.focus();
    });
    els.approveBtn.addEventListener("click", onApprove);
    els.rejectBtn.addEventListener("click", onReject);
    els.resumeBtn.addEventListener("click", onResume);
    els.refreshTaskBtn.addEventListener("click", function () { if (state.taskId) selectTask(state.taskId); });
    els.refreshListBtn.addEventListener("click", loadTaskList);
    els.refreshWorldBtn.addEventListener("click", loadWorld);
    Array.prototype.forEach.call(document.querySelectorAll(".tabs button"), function (button) {
      button.addEventListener("click", function () { selectTab(button.getAttribute("data-tab")); });
    });
    window.addEventListener("pagehide", stopPolling);
  }

  async function init() {
    bind();
    renderIdle();
    var healthy = await checkHealth();
    while (!healthy) {
      await new Promise(function (resolve) { setTimeout(resolve, 3000); });
      healthy = await checkHealth();
    }
    try {
      var results = await Promise.all([api.scenarios(), api.session()]);
      state.scenarios = results[0].data;
      state.session = results[1].data;
    } catch (err) {
      showBanner("Could not load dashboard configuration: " + err.message);
      return;
    }
    state.ready = true;
    renderScenarios();
    renderSession();
    onGoalInput();
    scheduleHealth();
    if (hasAccess()) await afterAccess();
    updateControls();
  }

  init();
})();
