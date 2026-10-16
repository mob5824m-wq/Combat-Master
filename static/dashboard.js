/* Combat Master dashboard.
 *
 * Plain JavaScript, no build step. Every request is relative (api/...) so the
 * page works behind a path prefix or a reverse proxy. Nothing from the server
 * is written with innerHTML: all text goes through textContent.
 */
(() => {
  "use strict";

  const state = {
    csrf: null,
    demo: false,
    guilds: [],
    guildId: null,
    snapshot: null,
    settings: null,
    timezone: "UTC",
    tab: "overview",
    templateName: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  // ------------------------------------------------------------------ //
  // Small helpers
  // ------------------------------------------------------------------ //
  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of [].concat(children)) {
      if (child === undefined || child === null) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function notice(message, kind = "ok") {
    const box = $("#notice");
    box.textContent = message;
    box.className = `notice ${kind === "err" ? "err" : "ok"}`;
    box.hidden = false;
    clearTimeout(notice.timer);
    notice.timer = setTimeout(() => { box.hidden = true; }, kind === "err" ? 10000 : 5000);
  }

  // Form values: an empty field is sent as null so the server clears it.
  function opt(formData, key) {
    const value = String(formData.get(key) ?? "").trim();
    return value === "" ? null : value;
  }

  function optNumber(formData, key) {
    const value = opt(formData, key);
    return value === null ? null : Number(value);
  }

  function withWarnings(message, result) {
    const warnings = (result && result.warnings) || [];
    return [message, ...warnings].join(" ");
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    try {
      return new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: state.timezone,
      }).format(date);
    } catch (_) {
      return date.toLocaleString();
    }
  }

  function humanInterval(seconds) {
    for (const [name, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
      if (seconds >= size && seconds % size === 0) {
        const count = seconds / size;
        return `${count} ${name}${count === 1 ? "" : "s"}`;
      }
    }
    return `${seconds} seconds`;
  }

  function channelName(id) {
    if (!id || !state.snapshot) return String(id || "");
    const channel = state.snapshot.channels.find((c) => c.id === String(id));
    return channel ? `#${channel.name}` : `#${id}`;
  }

  function truncate(text, length = 300) {
    const value = String(text || "");
    return value.length > length ? `${value.slice(0, length)}…` : value;
  }

  function renderList(container, rows, rowFn, emptyText) {
    if (!rows || rows.length === 0) {
      container.replaceChildren(el("li", { class: "empty", text: emptyText }));
      return;
    }
    container.replaceChildren(...rows.map(rowFn));
  }

  // Run one action: disable its button, show the outcome, refresh the view.
  async function runAction(button, work, message, after) {
    if (button) button.disabled = true;
    try {
      const result = await work();
      if (message) notice(typeof message === "function" ? message(result) : message, "ok");
      if (after) await after(result);
      return result;
    } catch (err) {
      notice(err.message || "Something went wrong.", "err");
      return null;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function submitForm(form, work, message, after) {
    return runAction(form.querySelector('button[type="submit"]'), work, message, after);
  }

  // ------------------------------------------------------------------ //
  // API
  // ------------------------------------------------------------------ //
  async function api(path, { method = "GET", body } = {}) {
    const headers = { Accept: "application/json" };
    const init = { method, headers, credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    if (method !== "GET") headers["X-CSRF-Token"] = state.csrf || "";

    let response;
    try {
      response = await fetch(`api/${path}`, init);
    } catch (_) {
      throw new Error("Could not reach the bot. Check that it is still running.");
    }
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }
    if (response.status === 401 && path !== "login") {
      showLogin();
      throw new Error("Your session has ended. Sign in again.");
    }
    if (!response.ok) {
      throw new Error((data && data.error) || `Request failed (${response.status}).`);
    }
    return data;
  }

  // ------------------------------------------------------------------ //
  // Login, session, servers
  // ------------------------------------------------------------------ //
  function showLogin() {
    state.csrf = null;
    state.guildId = null;
    $("#app-view").hidden = true;
    $("#logout-btn").hidden = true;
    $("#guild-select").replaceChildren();
    $("#guild-select").disabled = true;
    $("#login-view").hidden = false;
    $("#token").focus();
  }

  function showApp() {
    $("#login-view").hidden = true;
    $("#app-view").hidden = false;
    $("#logout-btn").hidden = false;
  }

  async function boot() {
    const session = await api("session");
    state.demo = Boolean(session.demo);
    $("#demo-badge").hidden = !state.demo;
    $("#version").textContent = session.version ? `v${session.version}` : "";
    if (session.authenticated) {
      state.csrf = session.csrf;
      showApp();
      await loadGuilds();
    } else {
      showLogin();
    }
  }

  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    const errorBox = $("#login-error");
    errorBox.hidden = true;
    button.disabled = true;
    try {
      const data = await api("login", { method: "POST", body: { token: $("#token").value } });
      state.csrf = data.csrf;
      state.demo = Boolean(data.demo);
      $("#demo-badge").hidden = !state.demo;
      $("#token").value = "";
      showApp();
      await loadGuilds();
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.hidden = false;
    } finally {
      button.disabled = false;
    }
  });

  $("#logout-btn").addEventListener("click", async () => {
    try {
      await api("logout", { method: "POST" });
    } catch (_) {
      // Logging out locally is enough even if the request fails.
    }
    showLogin();
  });

  async function loadGuilds() {
    state.guilds = await api("guilds");
    const select = $("#guild-select");
    select.replaceChildren(...state.guilds.map((g) => el("option", { value: g.id, text: g.name })));
    select.disabled = state.guilds.length === 0;
    if (state.guilds.length === 0) {
      notice("The bot is not in any server yet. Invite it to a server, then reload this page.", "err");
      return;
    }
    await selectGuild(state.guilds[0].id);
  }

  $("#guild-select").addEventListener("change", (event) => selectGuild(event.currentTarget.value));

  async function selectGuild(guildId) {
    state.guildId = String(guildId);
    $("#guild-select").value = state.guildId;
    try {
      const [snapshot, settings] = await Promise.all([
        api(`guilds/${state.guildId}/snapshot`),
        api(`guilds/${state.guildId}/settings`),
      ]);
      state.snapshot = snapshot;
      state.settings = settings;
      state.timezone = settings.timezone || "UTC";
      fillSelects();
      fillSettingsForm();
      fillWelcomeForm();
      await refreshTab();
    } catch (err) {
      notice(err.message, "err");
    }
  }

  async function reloadSettings() {
    state.settings = await api(`guilds/${state.guildId}/settings`);
    state.timezone = state.settings.timezone || "UTC";
    fillSettingsForm();
    fillWelcomeForm();
  }

  // ------------------------------------------------------------------ //
  // Dropdowns and form fill-in
  // ------------------------------------------------------------------ //
  function blankLabel(form, kind) {
    const id = form ? form.id : "";
    if (id === "welcome-form") return "Choose a channel";
    if (id === "settings-form") return "Not set";
    return kind === "role" ? "No role" : "Server default";
  }

  function fillSelects() {
    const snapshot = state.snapshot;
    for (const select of $$("select.channel-select")) {
      const form = select.closest("form");
      const options = [el("option", { value: "", text: blankLabel(form, "channel") })];
      for (const channel of snapshot.channels) {
        options.push(el("option", {
          value: channel.id,
          text: `#${channel.name}${channel.news ? " (announcements)" : ""}`,
        }));
      }
      select.replaceChildren(...options);
    }
    for (const select of $$("select.role-select")) {
      const form = select.closest("form");
      const options = [el("option", { value: "", text: blankLabel(form, "role") })];
      for (const role of snapshot.roles) {
        options.push(el("option", { value: role.id, text: `@${role.name}` }));
      }
      select.replaceChildren(...options);
    }
  }

  function fillSettingsForm() {
    const form = $("#settings-form");
    const s = state.settings;
    if (!s) return;
    form.elements.staff_role_id.value = s.staff_role_id ? String(s.staff_role_id) : "";
    form.elements.announcement_channel_id.value = s.announcement_channel_id ? String(s.announcement_channel_id) : "";
    form.elements.staff_channel_id.value = s.staff_channel_id ? String(s.staff_channel_id) : "";
    form.elements.timezone.value = s.timezone || "";
  }

  function fillWelcomeForm() {
    const form = $("#welcome-form");
    const s = state.settings;
    if (!s) return;
    form.elements.channel_id.value = s.welcome_channel_id ? String(s.welcome_channel_id) : "";
    form.elements.message.value = s.welcome_message || "";
    form.elements.message.placeholder = s.default_welcome_message || "";
    $("#welcome-status").textContent = s.welcome_channel_id
      ? `Welcome messages are on in ${channelName(s.welcome_channel_id)}.`
      : "Welcome messages are off. Choose a channel and save to turn them on.";
    $("#welcome-placeholders").textContent =
      `Placeholders: ${(s.placeholders || []).join("  ")}. Leave the message empty to use the default: ${s.default_welcome_message || ""}`;
  }

  // ------------------------------------------------------------------ //
  // Tabs
  // ------------------------------------------------------------------ //
  $("#tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (button) showTab(button.dataset.tab);
  });

  function showTab(name) {
    state.tab = name;
    for (const button of $$("#tabs button")) {
      button.classList.toggle("active", button.dataset.tab === name);
    }
    for (const panel of $$("[data-panel]")) {
      panel.hidden = panel.dataset.panel !== name;
    }
    refreshTab();
  }

  const LOADERS = {
    overview: loadOverview,
    announce: loadAnnounceHistory,
    scheduled: loadScheduled,
    templates: loadTemplates,
    polls: loadPolls,
    events: loadEvents,
    welcome: async () => {},
    settings: async () => {},
    audit: loadAudit,
  };

  async function refreshTab() {
    if (!state.guildId) return;
    try {
      await LOADERS[state.tab]();
    } catch (err) {
      notice(err.message, "err");
    }
  }

  // ------------------------------------------------------------------ //
  // Overview
  // ------------------------------------------------------------------ //
  async function loadOverview() {
    const data = await api(`guilds/${state.guildId}/overview`);
    const s = data.stats;
    const cards = [
      ["Scheduled", s.scheduled],
      ["Repeating", s.repeats],
      ["Templates", s.templates],
      ["Open polls", s.polls_open],
      ["Upcoming events", s.events_upcoming],
      ["Welcome", s.welcome_enabled ? "On" : "Off"],
    ];
    $("#stats").replaceChildren(...cards.map(([label, value]) =>
      el("div", { class: "stat" }, [
        el("div", { class: "value", text: value }),
        el("div", { class: "label", text: label }),
      ])));
    renderList($("#overview-recent"), data.recent, announcementRow, "Nothing has been posted yet.");
  }

  function announcementRow(row) {
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: row.title }),
        el("div", {
          class: "meta",
          text: `${fmtTime(row.created_at)} · ${row.source} · ${channelName(row.channel_id)} · by ${row.author_name || "staff"}`,
        }),
        el("div", { class: "body", text: truncate(row.body) }),
      ]),
    ]);
  }

  // ------------------------------------------------------------------ //
  // Announce now
  // ------------------------------------------------------------------ //
  $("#announce-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/announce`, {
        method: "POST",
        body: {
          title: opt(f, "title"),
          message: f.get("message"),
          channel_id: opt(f, "channel_id"),
          mention_role_id: opt(f, "mention_role_id"),
          publish: f.has("publish"),
        },
      }),
      (r) => withWarnings(`Posted${r.jump_url ? `: ${r.jump_url}` : "."}`, r),
      async () => { form.reset(); fillSelects(); await loadAnnounceHistory(); },
    );
  });

  async function loadAnnounceHistory() {
    const rows = await api(`guilds/${state.guildId}/history?limit=10`);
    renderList($("#announce-history"), rows, announcementRow, "Nothing has been posted yet.");
  }

  // ------------------------------------------------------------------ //
  // Scheduled and repeating
  // ------------------------------------------------------------------ //
  $("#schedule-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/scheduled`, {
        method: "POST",
        body: {
          when: f.get("when"),
          repeat_every: opt(f, "repeat_every"),
          title: opt(f, "title"),
          message: f.get("message"),
          channel_id: opt(f, "channel_id"),
          mention_role_id: opt(f, "mention_role_id"),
          publish: f.has("publish"),
        },
      }),
      (r) => withWarnings(
        r.repeat_seconds
          ? `Repeating #${r.id} starts ${fmtTime(r.send_at)}.`
          : `Scheduled #${r.id} for ${fmtTime(r.send_at)}.`,
        r,
      ),
      async () => { form.reset(); fillSelects(); await loadScheduled(); },
    );
  });

  async function loadScheduled() {
    const rows = await api(`guilds/${state.guildId}/scheduled`);
    renderList($("#scheduled-list"), rows, scheduledRow, "Nothing is waiting to go out.");
  }

  function scheduledRow(row) {
    const kind = row.interval_seconds
      ? `every ${humanInterval(row.interval_seconds)} · posted ${row.runs} time(s) so far`
      : "once";
    const button = el("button", { type: "button", class: "danger small", text: "Cancel" });
    button.addEventListener("click", () => {
      if (!window.confirm(`Cancel #${row.id} “${row.title}”?`)) return;
      runAction(button, () => api(`guilds/${state.guildId}/scheduled/${row.id}`, { method: "DELETE" }),
        `Cancelled #${row.id}.`, loadScheduled);
    });
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: `#${row.id} · ${row.title}` }),
        el("div", {
          class: "meta",
          text: `${kind} · next ${fmtTime(row.send_at)} · ${channelName(row.channel_id)}${row.publish ? " · publishes" : ""}`,
        }),
        el("div", { class: "body", text: truncate(row.body) }),
      ]),
      button,
    ]);
  }

  // ------------------------------------------------------------------ //
  // Templates
  // ------------------------------------------------------------------ //
  $("#template-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/templates`, {
        method: "POST",
        body: { name: f.get("name"), title: opt(f, "title"), message: f.get("message") },
      }),
      (r) => `${r.updated ? "Updated" : "Saved"} template “${r.name}”.`,
      async () => { form.reset(); await loadTemplates(); },
    );
  });

  async function loadTemplates() {
    const rows = await api(`guilds/${state.guildId}/templates`);
    renderList($("#template-list"), rows, templateRow, "No templates yet. Save one above.");
  }

  function templateRow(row) {
    const useButton = el("button", { type: "button", class: "small", text: "Post…" });
    useButton.addEventListener("click", () => openTemplateUse(row.name));
    const deleteButton = el("button", { type: "button", class: "danger small", text: "Delete" });
    deleteButton.addEventListener("click", () => {
      if (!window.confirm(`Delete template “${row.name}”?`)) return;
      runAction(deleteButton, () => api(`guilds/${state.guildId}/templates/${encodeURIComponent(row.name)}`, { method: "DELETE" }),
        `Deleted template “${row.name}”.`, loadTemplates);
    });
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: row.name }),
        el("div", { class: "meta", text: row.title }),
        el("div", { class: "body", text: truncate(row.body) }),
      ]),
      useButton,
      deleteButton,
    ]);
  }

  function openTemplateUse(name) {
    state.templateName = name;
    $("#template-use-name").textContent = name;
    const form = $("#template-use-form");
    form.hidden = false;
    form.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  $("#template-use-cancel").addEventListener("click", () => {
    $("#template-use-form").hidden = true;
    state.templateName = null;
  });

  $("#template-use-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    const name = state.templateName;
    if (!name) return;
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/templates/${encodeURIComponent(name)}/use`, {
        method: "POST",
        body: {
          channel_id: opt(f, "channel_id"),
          mention_role_id: opt(f, "mention_role_id"),
          publish: f.has("publish"),
        },
      }),
      (r) => withWarnings(`Posted “${name}”${r.jump_url ? `: ${r.jump_url}` : "."}`, r),
      () => { form.reset(); form.hidden = true; state.templateName = null; fillSelects(); },
    );
  });

  // ------------------------------------------------------------------ //
  // Polls
  // ------------------------------------------------------------------ //
  $("#poll-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    const options = String(f.get("options") || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/polls`, {
        method: "POST",
        body: {
          question: f.get("question"),
          options,
          duration: opt(f, "duration"),
          channel_id: opt(f, "channel_id"),
        },
      }),
      (r) => `Poll #${r.id} posted.${r.ends_at ? ` It closes ${fmtTime(r.ends_at)}.` : ""}`,
      async () => { form.reset(); fillSelects(); await loadPolls(); },
    );
  });

  async function loadPolls() {
    const rows = await api(`guilds/${state.guildId}/polls`);
    renderList($("#poll-list"), rows, pollRow, "No polls yet.");
  }

  function pollRow(row) {
    const options = row.options || [];
    const results = row.results || [];
    const detail = row.status === "closed"
      ? options.map((choice, i) => `${choice}: ${results[i] ?? 0}`).join(" · ")
      : options.join(" · ");
    const children = [
      el("div", { class: "grow" }, [
        el("strong", { text: `#${row.id} · ${row.question}` }),
        el("div", {
          class: "meta",
          text: `${row.status}${row.ends_at && row.status === "open" ? ` · closes ${fmtTime(row.ends_at)}` : ""} · ${channelName(row.channel_id)}`,
        }),
        el("div", { class: "body", text: detail }),
      ]),
    ];
    if (row.status === "open") {
      const button = el("button", { type: "button", class: "small", text: "Close now" });
      button.addEventListener("click", () => runAction(
        button,
        () => api(`guilds/${state.guildId}/polls/${row.id}/close`, { method: "POST" }),
        (r) => `Closed poll #${r.id}: ${r.question}`,
        loadPolls,
      ));
      children.push(button);
    }
    return el("li", {}, children);
  }

  // ------------------------------------------------------------------ //
  // Events
  // ------------------------------------------------------------------ //
  $("#event-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/events`, {
        method: "POST",
        body: {
          title: f.get("title"),
          starts_when: f.get("starts_when"),
          description: opt(f, "description"),
          channel_id: opt(f, "channel_id"),
          remind_minutes: optNumber(f, "remind_minutes"),
        },
      }),
      (r) => `Event #${r.id} posted for ${fmtTime(r.starts_at)}.${r.reminder_at ? ` Reminder ${fmtTime(r.reminder_at)}.` : ""}`,
      async () => { form.reset(); fillSelects(); await loadEvents(); },
    );
  });

  async function loadEvents() {
    const rows = await api(`guilds/${state.guildId}/events`);
    renderList($("#event-list"), rows, eventRow, "No upcoming events.");
  }

  function eventRow(row) {
    const going = row.going === null || row.going === undefined ? "unknown" : String(row.going);
    const button = el("button", { type: "button", class: "danger small", text: "Cancel event" });
    button.addEventListener("click", () => {
      if (!window.confirm(`Cancel event “${row.title}”?`)) return;
      runAction(button, () => api(`guilds/${state.guildId}/events/${row.id}/cancel`, { method: "POST" }),
        `Cancelled event #${row.id}.`, loadEvents);
    });
    return el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: `#${row.id} · ${row.title}` }),
        el("div", {
          class: "meta",
          text: `${fmtTime(row.starts_at)} · going: ${going} · ${channelName(row.channel_id)}${row.remind_minutes ? ` · reminder ${row.remind_minutes} min before` : ""}`,
        }),
        el("div", { class: "body", text: truncate(row.description, 200) }),
      ]),
      button,
    ]);
  }

  // ------------------------------------------------------------------ //
  // Welcome
  // ------------------------------------------------------------------ //
  $("#welcome-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/welcome`, {
        method: "POST",
        body: { channel_id: opt(f, "channel_id"), message: f.get("message") ?? "" },
      }),
      "Welcome messages saved.",
      reloadSettings,
    );
  });

  $("#welcome-off").addEventListener("click", () => {
    if (!window.confirm("Turn off welcome messages?")) return;
    runAction(
      $("#welcome-off"),
      () => api(`guilds/${state.guildId}/welcome`, { method: "DELETE" }),
      "Welcome messages are off.",
      reloadSettings,
    );
  });

  $("#welcome-test").addEventListener("click", () => {
    runAction(
      $("#welcome-test"),
      () => api(`guilds/${state.guildId}/welcome/test`, { method: "POST" }),
      (r) => `Sample welcome posted in ${channelName(r.channel_id)}.`,
    );
  });

  // ------------------------------------------------------------------ //
  // Settings
  // ------------------------------------------------------------------ //
  $("#settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const f = new FormData(form);
    submitForm(
      form,
      () => api(`guilds/${state.guildId}/settings`, {
        method: "PUT",
        body: {
          staff_role_id: opt(f, "staff_role_id"),
          announcement_channel_id: opt(f, "announcement_channel_id"),
          staff_channel_id: opt(f, "staff_channel_id"),
          timezone: opt(f, "timezone"),
        },
      }),
      "Settings saved.",
      reloadSettings,
    );
  });

  // ------------------------------------------------------------------ //
  // Audit log
  // ------------------------------------------------------------------ //
  $("#audit-refresh").addEventListener("click", () => loadAudit());

  async function loadAudit() {
    const rows = await api(`guilds/${state.guildId}/audit?limit=50`);
    renderList($("#audit-list"), rows, (row) => el("li", {}, [
      el("div", { class: "grow" }, [
        el("strong", { text: `${row.action} ${row.detail || ""}`.trim() }),
        el("div", { class: "meta", text: `${fmtTime(row.created_at)} · ${row.actor_name} (${row.source})` }),
      ]),
    ]), "Nothing has been recorded yet.");
  }

  // ------------------------------------------------------------------ //
  // Start
  // ------------------------------------------------------------------ //
  boot().catch((err) => notice(err.message || "Could not load the dashboard.", "err"));
})();
