/* LLM Hub app. Plain JS, no build step. Every API call goes to the local server, which checks the session cookie. */
"use strict";

// ---------- helpers ----------

const ICONS = {
  logo: '<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#534AB7"/><path d="M9 11h14v8h-6l-4 4v-4H9z" fill="#fff"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m6 9 6 6 6-6"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5 9-10"/></svg>',
  ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>',
  warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17v.5"/></svg>',
  fail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/></svg>',
  skip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>',
  gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
  back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m15 18-6-6 6-6"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18"/></svg>',
};

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "html") el.innerHTML = value; // only ever trusted markup: icons or sanitized markdown
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const icon = (name) => h("span", { class: "icon", html: ICONS[name], "aria-hidden": "true" });

function markdown(text) {
  const div = h("div", { class: "md" });
  if (window.LLMHubMarkdown && window.marked && window.DOMPurify) {
    div.innerHTML = window.LLMHubMarkdown.render(text || ""); // sanitized; formulas rendered by KaTeX (markdown.js)
    div.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
  } else {
    div.textContent = text || "";
  }
  return div;
}

function nameOf(agent) {
  return { claude: "Claude", gpt: "GPT", human: "You", none: "Nobody" }[agent] || agent.charAt(0).toUpperCase() + agent.slice(1);
}

function waitingLabel(thread) {
  if (thread.status === "resolved") return "Closed";
  if (thread.awaiting === "human") return "Waiting for you";
  if (thread.awaiting === "none" || !thread.awaiting) return "Nothing pending";
  return `Waiting for ${nameOf(thread.awaiting)}`;
}

function ago(ts) {
  if (!ts) return "";
  const seconds = (Date.now() - new Date(ts).getTime()) / 1000;
  if (!isFinite(seconds)) return "";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  if (seconds < 7 * 86400) return `${Math.floor(seconds / 86400)} d ago`;
  return new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function when(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function toast(message, kind = "") {
  const el = h("div", { class: `toast ${kind}` }, message);
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), kind === "bad" ? 7000 : 4000);
}

class ApiError extends Error {
  constructor(status, data) {
    super((data && data.error) || `Something went wrong (${status}).`);
    this.status = status;
    this.data = data || {};
  }
}

async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, { error: "Can't reach the LLM Hub app. Open it again from your Applications folder." });
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

async function busy(button, fn) {
  button.setAttribute("aria-busy", "true");
  try { return await fn(); } finally { button.removeAttribute("aria-busy"); }
}

// ---------- state ----------

const state = {
  hub: null,
  agents: [],
  postTypes: [],
  recent: [],
  threads: [],
  query: "",
  current: null,        // the open thread (full)
  view: "thread",       // thread | health
  drafts: {},           // thread id -> { body, handTo, type, re }
  health: null,
  healthLoading: false,
  setupSteps: null,
  connected: true,
};

const app = () => document.getElementById("app");

// ---------- boot and routing ----------

async function boot() {
  let s;
  try {
    s = await api("/api/state");
  } catch (err) {
    if (err.status === 401) return renderSignedOut();
    return renderFatal(err.message);
  }
  state.recent = s.recent || [];
  if (!s.hub) return renderWelcome();
  state.hub = s.hub;
  state.agents = s.hub.agents;
  state.postTypes = s.post_types;
  renderShell();
  await loadThreads();
  connectEvents();
  loadHealth();
  route();
}

function route() {
  const hash = location.hash || "";
  const match = hash.match(/^#\/t\/(T-\d+)/i);
  if (hash === "#/health") return showHealth();
  if (hash === "#/new") { history.replaceState(null, "", "#/"); return openNewThread(); }
  if (match) return openThread(match[1].toUpperCase());
  const first = state.threads.find((t) => t.status === "open" && t.awaiting === "human") || state.threads[0];
  if (first) return openThread(first.id, { replace: true });
  renderEmptyMain();
}

window.addEventListener("hashchange", () => { if (state.hub) route(); });

// ---------- screens without a hub ----------

function renderSignedOut() {
  app().replaceChildren(h("div", { class: "welcome" },
    h("span", { html: ICONS.logo }),
    h("h1", {}, "Open LLM Hub from your Mac"),
    h("p", {}, "For your security, this page only works when it's opened by the LLM Hub app. Open “LLM Hub” from your Applications folder (or Spotlight), and it will bring you back here signed in."),
  ));
}

function renderFatal(message) {
  app().replaceChildren(h("div", { class: "welcome" },
    h("span", { html: ICONS.logo }),
    h("h1", {}, "LLM Hub isn't responding"),
    h("p", {}, message),
    h("div", { class: "actions-row" }, h("button", { class: "btn", onclick: () => location.reload() }, "Try again")),
  ));
}

function renderWelcome() {
  const pick = h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, () => chooseHub({ pick: true })) },
    icon("folder"), "Choose a project folder");
  app().replaceChildren(h("div", { class: "welcome" },
    h("span", { html: ICONS.logo }),
    h("h1", {}, "Welcome to LLM Hub"),
    h("p", {}, "Claude, GPT and you discuss work in threads that live inside a project folder. Pick the folder of the project you want to discuss. LLM Hub adds an “llm-hub” folder to it."),
    h("div", { class: "actions-row" }, pick),
    state.recent.length ? h("div", { class: "recent-label" }, "Recently used") : null,
    state.recent.length ? h("ul", { class: "recent" }, state.recent.map(recentRow)) : null,
  ));
}

function recentRow(path) {
  return h("li", {},
    h("span", { class: "path" }, path),
    h("button", { class: "btn small", onclick: (e) => busy(e.currentTarget, () => chooseHub({ path })) }, "Open"));
}

async function chooseHub(body) {
  try {
    const result = await api("/api/hubs", { method: "POST", body });
    if (result.cancelled) return;
    toast(`Now using ${result.root.split("/").slice(-2, -1)[0] || result.root}`);
    state.current = null;
    location.hash = "#/";
    await boot();
  } catch (err) {
    toast(err.message, "bad");
  }
}

// ---------- the shell ----------

function renderShell() {
  const hubButton = h("button", { class: "btn small", "aria-haspopup": "menu", onclick: toggleHubMenu },
    icon("folder"), state.hub.repo, icon("down"));
  app().replaceChildren(h("div", { class: "shell" },
    h("header", { class: "topbar" },
      h("div", { class: "brand" }, h("span", { html: ICONS.logo }), h("span", { class: "brand-name" }, "LLM Hub")),
      h("div", { class: "hub-picker", id: "hub-picker" }, hubButton),
      h("span", { class: "spacer" }),
      h("span", { id: "conn", class: "conn hidden" }, "Reconnecting…"),
      h("button", { id: "health-chip", class: "chip link", onclick: () => { location.hash = "#/health"; } }, "Checking apps…"),
      h("button", { class: "btn ghost small", title: "Setup and health", "aria-label": "Setup and health", onclick: () => { location.hash = "#/health"; } }, icon("gear")),
    ),
    h("div", { class: "body", id: "body" },
      h("aside", { class: "sidebar" },
        h("div", { class: "sidebar-top" },
          h("button", { class: "btn primary block", onclick: openNewThread }, icon("plus"), "New thread"),
          h("input", { type: "search", id: "search", placeholder: "Search threads", "aria-label": "Search threads", oninput: onSearch }),
        ),
        h("nav", { class: "list", id: "list", "aria-label": "Threads" }),
      ),
      h("main", { class: "main", id: "main" }),
    ),
  ));
}

function toggleHubMenu(event) {
  event.stopPropagation();
  const holder = document.getElementById("hub-picker");
  const existing = holder.querySelector(".menu");
  if (existing) return existing.remove();
  const others = state.recent.filter((p) => p !== state.hub.root);
  const menu = h("div", { class: "menu", role: "menu" },
    h("div", { class: "label" }, "Current hub"),
    h("div", { class: "label path" }, state.hub.root),
    h("div", { class: "sep" }),
    others.length ? h("div", { class: "label" }, "Recent") : null,
    others.map((path) => h("button", { role: "menuitem", onclick: () => chooseHub({ path }) }, icon("folder"), path.split("/").slice(-2, -1)[0] || path)),
    others.length ? h("div", { class: "sep" }) : null,
    h("button", { role: "menuitem", onclick: () => chooseHub({ pick: true }) }, icon("plus"), "Open another project folder…"),
  );
  holder.append(menu);
  setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }));
}

// ---------- thread list ----------

let searchTimer;
function onSearch(event) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.query = event.target.value.trim(); loadThreads(); }, 200);
}

async function loadThreads() {
  try {
    const q = state.query ? `?q=${encodeURIComponent(state.query)}` : "";
    state.threads = (await api(`/api/threads${q}`)).threads;
    renderList();
  } catch (err) {
    if (err.status === 401) return renderSignedOut();
    toast(err.message, "bad");
  }
}

function renderList() {
  const list = document.getElementById("list");
  if (!list) return;
  const waiting = state.threads.filter((t) => t.status === "open" && t.awaiting === "human");
  const going = state.threads.filter((t) => t.status === "open" && t.awaiting !== "human");
  const done = state.threads.filter((t) => t.status !== "open");
  const groups = [["Waiting for you", waiting], ["In progress", going], ["Closed", done]];
  const children = [];
  for (const [label, items] of groups) {
    if (!items.length) continue;
    children.push(h("div", { class: "group" }, `${label} · ${items.length}`));
    children.push(...items.map(listItem));
  }
  if (!children.length) {
    children.push(h("p", { class: "empty-list" }, state.query ? "No threads match your search." : "No threads yet. Start one, or ask Claude or GPT to open a thread with the other."));
  }
  list.replaceChildren(...children);
}

function listItem(thread) {
  const meta = thread.status !== "open" ? `Closed · ${ago(thread.updated)}`
    : `${waitingLabel(thread)} · ${ago(thread.updated)}`;
  const unread = thread.unread > 0 ? `${thread.unread} new · ` : "";
  return h("button", {
    class: `item ${thread.status !== "open" ? "done" : ""}`,
    "aria-current": state.current && state.current.id === thread.id ? "true" : "false",
    onclick: () => { location.hash = `#/t/${thread.id}`; document.getElementById("body")?.classList.remove("list-open"); },
  },
    h("div", { class: "title" }, h("span", {}, thread.title), thread.unread > 0 ? h("i", { class: "dot", "aria-label": "unread" }) : null),
    h("div", { class: "meta" }, unread + meta));
}

// ---------- one thread ----------

async function openThread(id, { replace = false } = {}) {
  if (replace) history.replaceState(null, "", `#/t/${id}`);
  state.view = "thread";
  try {
    state.current = await api(`/api/threads/${id}`);
  } catch (err) {
    if (err.status === 401) return renderSignedOut();
    toast(err.message, "bad");
    return renderEmptyMain();
  }
  const item = state.threads.find((t) => t.id === id);
  if (item) item.unread = 0;
  renderList();
  renderThread();
}

function renderEmptyMain() {
  state.current = null;
  const main = document.getElementById("main");
  if (!main) return;
  main.replaceChildren(h("div", { class: "empty-state" },
    h("h2", {}, "Start your first thread"),
    h("p", {}, "Ask a question, and Claude or GPT answers it the next time you tell them to “check the hub”."),
    h("button", { class: "btn primary", onclick: openNewThread }, icon("plus"), "New thread")));
}

function draftFor(thread) {
  if (!state.drafts[thread.id]) {
    const lastAgent = [...thread.posts].reverse().find((p) => p.author !== "human");
    let handTo = thread.awaiting && thread.awaiting !== "human" && thread.awaiting !== "none" ? thread.awaiting : null;
    handTo = handTo || (lastAgent && state.agents.includes(lastAgent.author) ? lastAgent.author : state.agents[0] || "none");
    state.drafts[thread.id] = { body: "", handTo, type: "note", re: null, more: false };
  }
  return state.drafts[thread.id];
}

function renderThread({ keepScroll = false, keepComposer = false } = {}) {
  const thread = state.current;
  const main = document.getElementById("main");
  if (!main || !thread) return;
  const oldScroll = main.querySelector(".scroll");
  const scrollTop = oldScroll ? oldScroll.scrollTop : 0;
  const atBottom = oldScroll ? oldScroll.scrollHeight - oldScroll.scrollTop - oldScroll.clientHeight < 40 : false;

  const statusClass = thread.status !== "open" ? "ok" : thread.awaiting === "human" ? "warn" : thread.awaiting === "none" ? "" : thread.awaiting;
  const head = h("div", { class: "thread-head" },
    h("div", { class: "row" },
      h("button", { class: "btn ghost small back-to-list", "aria-label": "All threads", onclick: () => document.getElementById("body").classList.add("list-open") }, icon("back")),
      h("h1", {}, thread.title),
      thread.status === "open"
        ? h("button", { class: "btn small", onclick: () => openResolve(thread) }, icon("check"), "Close thread")
        : null),
    h("div", { class: "chips" },
      h("span", { class: `chip ${statusClass}` }, waitingLabel(thread)),
      thread.status === "open" ? h("span", { class: "chip", title: "Agent posts since you last posted. At the limit, the turn comes to you." }, `${thread.turns} of ${thread.max_turns} turns used`) : null,
      thread.tags.length ? h("span", { class: "chip" }, thread.tags.join(" · ")) : null,
      h("span", { class: "chip" }, thread.id)));

  const body = [];
  if (thread.status !== "open") {
    const by = thread.resolved_by ? ` by ${nameOf(thread.resolved_by)}` : "";
    body.push(h("div", { class: "banner ok" }, icon("ok"),
      h("div", {}, h("b", {}, thread.decision ? `Closed${by} · decision` : `Closed${by}`),
        thread.decision ? markdown(thread.decision) : h("div", { class: "md" }, "Reply below to reopen it."))));
  }
  if (thread.summary) {
    body.push(h("section", { class: "summary" }, h("h2", {}, `Summary${thread.summary_by ? ` · by ${nameOf(thread.summary_by)}` : ""}`), markdown(thread.summary)));
  }
  for (const post of thread.posts) {
    if (post.id === thread.first_unread && post.id !== thread.posts[0].id) body.push(h("div", { class: "new-divider" }, "New"));
    body.push(renderPost(post, thread));
  }
  if (thread.status === "open" && thread.awaiting !== "human" && thread.awaiting !== "none") {
    body.push(h("div", { class: "next-up" }, h("div", { class: `avatar ${thread.awaiting}` }, nameOf(thread.awaiting).charAt(0)),
      `${nameOf(thread.awaiting)}'s turn. Tell ${nameOf(thread.awaiting)} to “check the hub”, or reply below to add something first.`));
  }

  const scroll = h("div", { class: "scroll", id: "posts" }, body);
  // Keep the reply box as it is when only the posts changed, so live updates don't steal the caret.
  // Moving a focused element blurs it, so swap the header and posts around it instead.
  const existing = main.querySelector(".composer");
  if (keepComposer && existing && existing.dataset.thread === thread.id && existing.dataset.status === thread.status) {
    main.querySelector(".thread-head").replaceWith(head);
    main.querySelector(".scroll").replaceWith(scroll);
  } else {
    main.replaceChildren(head, scroll, renderComposer(thread));
  }
  if (keepScroll && !atBottom) scroll.scrollTop = scrollTop;
  else if (thread.first_unread && thread.first_unread !== thread.posts[0].id) document.getElementById(`post-${thread.first_unread}`)?.scrollIntoView({ block: "start" });
  else scroll.scrollTop = scroll.scrollHeight;
}

function renderPost(post, thread) {
  const to = post.hand_to && post.hand_to !== "none" ? ` → ${nameOf(post.hand_to)}` : "";
  const re = post.re ? h("a", { href: `#post-${post.re}`, onclick: (e) => { e.preventDefault(); flash(post.re); } }, `re ${post.re}`) : null;
  return h("article", { class: "post", id: `post-${post.id}` },
    h("div", { class: `avatar ${post.author}` }, nameOf(post.author).charAt(0)),
    h("div", { class: "content" },
      h("div", { class: "head" },
        h("b", {}, nameOf(post.author)),
        h("span", { class: "to" }, to),
        h("span", {}, `· ${post.type}`),
        re ? h("span", {}, "· ", re) : null,
        h("span", { title: post.ts }, `· ${when(post.ts)}`),
        h("span", { class: "actions" }, h("button", { class: "btn ghost small", onclick: () => replyTo(thread, post) }, "Reply"))),
      markdown(post.body)));
}

function flash(postId) {
  const el = document.getElementById(`post-${postId}`);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.add("target");
  setTimeout(() => el.classList.remove("target"), 1600);
}

function replyTo(thread, post) {
  const draft = draftFor(thread);
  draft.re = post.id;
  if (post.author !== "human" && state.agents.includes(post.author)) draft.handTo = post.author;
  renderThread({ keepScroll: true });
  document.getElementById("composer-text")?.focus();
}

function renderComposer(thread) {
  const draft = draftFor(thread);
  const text = h("textarea", {
    id: "composer-text",
    placeholder: thread.status === "open" ? "Write a reply…" : "Write a reply to reopen this thread…",
    "aria-label": "Your reply",
    oninput: (e) => { draft.body = e.target.value; error.textContent = ""; },
    onkeydown: (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send.click(); } },
  });
  text.value = draft.body;
  const error = h("div", { class: "error-text", role: "alert" });

  const options = [...state.agents, "none"];
  const seg = h("div", { class: "segmented", role: "group", "aria-label": "Who answers next?" },
    options.map((agent) => h("button", {
      type: "button",
      "aria-pressed": draft.handTo === agent ? "true" : "false",
      onclick: (e) => {
        draft.handTo = agent;
        seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false"));
        e.currentTarget.setAttribute("aria-pressed", "true");
      },
    }, nameOf(agent))));

  const typeSelect = h("select", { "aria-label": "Kind of post", onchange: (e) => { draft.type = e.target.value; } },
    state.postTypes.filter((t) => t !== "decision").map((t) => h("option", { value: t, selected: draft.type === t }, t)));
  const more = h("div", { class: `more ${draft.more ? "" : "hidden"}` },
    h("span", { class: "muted" }, "Kind of post"), typeSelect,
    h("span", { class: "hint" }, "Notes are fine for most replies. Questions and proposals help the agents answer the right way."));

  // Live updates reuse this box; the posts on screen are then newer than `thread`, so send against what's shown.
  const shown = () => (state.current && state.current.id === thread.id ? state.current : thread);
  const send = h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, () => sendReply(shown(), draft, error)) }, "Send");

  return h("div", { class: "composer", "data-thread": thread.id, "data-status": thread.status },
    draft.re ? h("div", { class: "replying" }, `Replying to ${draft.re}`,
      h("button", { class: "btn ghost small", "aria-label": "Don't reply to a specific post", onclick: () => { draft.re = null; renderThread({ keepScroll: true }); } }, icon("x"))) : null,
    text, error,
    h("div", { class: "bar" },
      h("span", { class: "muted small" }, "Who answers next?"), seg,
      h("button", { class: "btn ghost small", onclick: () => { draft.more = !draft.more; more.classList.toggle("hidden"); } }, "More options"),
      h("span", { class: "spacer" }),
      h("span", { class: "hint" }, "⌘ Enter to send"),
      send),
    more);
}

async function sendReply(thread, draft, error) {
  if (!draft.body.trim()) { error.textContent = "Write something first."; return; }
  try {
    const result = await api(`/api/threads/${thread.id}/posts`, {
      method: "POST",
      body: { body: draft.body, hand_to: draft.handTo, type: draft.type, re: draft.re, expected_revision: thread.revision },
    });
    delete state.drafts[thread.id];
    for (const note of result.notes || []) toast(note);
    toast(draft.handTo === "none" ? "Posted" : `Posted. Tell ${nameOf(draft.handTo)} to “check the hub”.`);
    await Promise.all([openThread(thread.id), loadThreads()]);
  } catch (err) {
    if (err.status === 409) {
      error.textContent = "";
      toast(err.message);
      const keep = draft.body;
      await openThread(thread.id);
      draftFor(state.current).body = keep;
      renderThread();
      return;
    }
    error.textContent = err.message;
  }
}

// ---------- dialogs ----------

function dialog(title, lead, content, actions) {
  content.addEventListener("input", () => content.querySelectorAll(".error-text").forEach((e) => { e.textContent = ""; }));
  const overlay = h("div", { class: "overlay", onclick: (e) => { if (e.target === overlay) close(); } });
  const close = () => { overlay.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  overlay.append(h("div", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-label": title },
    h("h2", {}, title), lead ? h("p", { class: "lead" }, lead) : null, content, h("div", { class: "foot" }, actions(close))));
  document.body.append(overlay);
  overlay.querySelector("input, textarea")?.focus();
  return close;
}

function openNewThread() {
  if (!state.hub) return;
  const title = h("input", { type: "text", placeholder: "Postgres or SQLite for about a million rows a day?" });
  const body = h("textarea", { rows: 6, placeholder: "The goal, what you already know, and what a good answer looks like." });
  const tags = h("input", { type: "text", placeholder: "database, storage" });
  let to = state.agents[0] || "claude";
  let type = "question";
  const error = h("div", { class: "error-text", role: "alert" });
  const who = h("div", { class: "segmented", role: "group", "aria-label": "Ask first" },
    state.agents.map((agent) => h("button", {
      type: "button", "aria-pressed": agent === to ? "true" : "false",
      onclick: (e) => { to = agent; who.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false")); e.currentTarget.setAttribute("aria-pressed", "true"); },
    }, nameOf(agent))));
  const kind = h("div", { class: "segmented", role: "group", "aria-label": "Kind" },
    [["question", "A question"], ["proposal", "A proposal to review"]].map(([value, label]) => h("button", {
      type: "button", "aria-pressed": value === type ? "true" : "false",
      onclick: (e) => { type = value; kind.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false")); e.currentTarget.setAttribute("aria-pressed", "true"); },
    }, label)));

  dialog("Start a thread", "Claude and GPT will see it the next time you ask them to “check the hub”.",
    h("div", {},
      h("label", { class: "field" }, h("span", {}, "What should they discuss?"), title),
      h("label", { class: "field" }, h("span", {}, "Details"), body),
      h("div", { class: "field" }, h("span", { class: "small muted" }, "Ask first"), h("div", {}, who)),
      h("div", { class: "field" }, h("span", { class: "small muted" }, "This is"), h("div", {}, kind)),
      h("label", { class: "field" }, h("span", {}, "Tags (optional)"), tags),
      error),
    (close) => [
      h("button", { class: "btn", onclick: close }, "Cancel"),
      h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, async () => {
        if (!title.value.trim()) { error.textContent = "Give the thread a title."; title.focus(); return; }
        if (!body.value.trim()) { error.textContent = "Add some details, so they know what you need."; body.focus(); return; }
        try {
          const result = await api("/api/threads", { method: "POST", body: { title: title.value, body: body.value, to, type, tags: tags.value } });
          close();
          toast(`Thread started. Tell ${nameOf(to)} to “check the hub”.`);
          await loadThreads();
          location.hash = `#/t/${result.id}`;
        } catch (err) { error.textContent = err.message; }
      }) }, "Start thread"),
    ]);
}

function openResolve(thread) {
  const decision = h("textarea", { rows: 4, placeholder: "Postgres, managed, with nightly backups." });
  const error = h("div", { class: "error-text", role: "alert" });
  dialog("Close thread", "Claude and GPT stop working on it. You can reopen it later by replying.",
    h("div", {}, h("label", { class: "field" }, h("span", {}, "What was decided? (optional)"), decision),
      h("p", { class: "small muted" }, "Writing it down helps anyone who reads this thread later."), error),
    (close) => [
      h("button", { class: "btn", onclick: close }, "Cancel"),
      h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, async () => {
        try {
          await api(`/api/threads/${thread.id}/resolve`, { method: "POST", body: { decision: decision.value, expected_revision: thread.revision } });
          close();
          toast("Thread closed");
          await Promise.all([openThread(thread.id), loadThreads()]);
        } catch (err) {
          if (err.status === 409) { close(); toast(err.message); await openThread(thread.id); return; }
          error.textContent = err.message;
        }
      }) }, "Close thread"),
    ]);
}

// ---------- setup and health ----------

const CHECK_NAMES = {
  "llm-hub command": "LLM Hub installed",
  "server self-test": "Hub server",
  "current hub": "Your hub",
  "skill · Claude Code": "Claude Code skill",
  "skill · ChatGPT/Codex": "ChatGPT and Codex skill",
  "skill · Claude Chat tab": "Claude Chat skill",
  "MCP · Claude Code": "Claude Code",
  "MCP · Claude desktop": "Claude (Chat tab)",
  "MCP · ChatGPT/Codex": "ChatGPT and Codex",
};
const APP_CHECKS = ["MCP · Claude Code", "MCP · Claude desktop", "MCP · ChatGPT/Codex"];

async function loadHealth(fresh = false) {
  state.healthLoading = true;
  if (state.view === "health") renderHealth();
  try {
    state.health = (await api(`/api/health${fresh ? "?fresh=1" : ""}`)).checks;
  } catch (err) {
    state.health = null;
    if (err.status !== 501) toast(err.message, "bad");
  } finally {
    state.healthLoading = false;
  }
  renderHealthChip();
  if (state.view === "health") renderHealth();
}

function renderHealthChip() {
  const chip = document.getElementById("health-chip");
  if (!chip) return;
  if (!state.health) { chip.className = "chip link"; chip.textContent = "Setup"; return; }
  const apps = state.health.filter((c) => APP_CHECKS.includes(c.name) && c.status !== "skip");
  const trouble = state.health.filter((c) => c.status === "fail" || c.status === "warn");
  if (trouble.length) {
    chip.className = "chip link warn";
    chip.textContent = trouble.length === 1 ? "1 thing needs attention" : `${trouble.length} things need attention`;
  } else {
    chip.className = "chip link ok";
    chip.textContent = apps.length ? "Apps connected" : "All good";
  }
}

function showHealth() {
  state.view = "health";
  state.current = null;
  renderList();
  renderHealth();
  if (!state.health && !state.healthLoading) loadHealth();
}

function checkActions(check) {
  const acts = [];
  if (check.name === "skill · Claude Chat tab") {
    acts.push(h("button", { class: "btn small", onclick: (e) => busy(e.currentTarget, async () => {
      await api("/api/reveal", { method: "POST", body: { what: "claude-skill" } });
      toast("In Claude, open Settings → Capabilities → Skills and upload the file Finder is showing.");
    }) }, "Show file to upload"));
  }
  if (check.name === "MCP · Claude desktop" && check.status === "warn" && /Cmd\+Q/.test(check.detail + check.fix)) {
    acts.push(h("button", { class: "btn small", onclick: (e) => busy(e.currentTarget, async () => {
      if (!confirm("Quit Claude now? It reopens by itself a few seconds later with the hub connected. Anything you're typing in Claude may be lost.")) return;
      await api("/api/claude/quit", { method: "POST" });
      toast("Claude is restarting. Check again in a few seconds.");
      setTimeout(() => loadHealth(true), 8000);
    }) }, "Quit Claude to finish"));
  }
  if (check.name === "current hub" && check.status === "fail") {
    acts.push(h("button", { class: "btn small", onclick: () => chooseHub({ pick: true }) }, "Choose folder"));
  }
  return acts;
}

// The checks are shared with `llm-hub doctor`; here the buttons do what the terminal commands would.
function friendlyFix(fix) {
  return fix
    .replace(/Run `llm-hub setup`\./g, "Click Fix setup below.")
    .replace(/Create one: `llm-hub init <repo>` \(or `llm-hub use <repo>`\)\./g, "Choose a project folder below.")
    .replace(/`([^`]+)`/g, "$1");
}

function renderHealth() {
  const main = document.getElementById("main");
  if (!main || state.view !== "health") return;
  const checks = state.health;
  const rows = checks ? checks.map((check) => h("div", { class: "check" },
    h("span", { class: `state ${check.status}`, html: ICONS[{ ok: "ok", warn: "warn", info: "warn", fail: "fail", skip: "skip" }[check.status] || "skip"] }),
    h("div", { class: "what" },
      h("div", { class: "name" }, CHECK_NAMES[check.name] || check.name),
      h("div", { class: "detail" }, check.detail),
      check.fix && check.status !== "ok" ? h("div", { class: "fix" }, friendlyFix(check.fix)) : null),
    h("div", { class: "act" }, checkActions(check)))) : [h("div", { class: "check muted" }, state.healthLoading ? "Checking every app… this takes a few seconds." : "Health checks aren't available.")];

  const steps = state.setupSteps ? h("div", {},
    h("h2", {}, "What setup did"),
    h("div", { class: "checks" }, state.setupSteps.map((step) => h("div", { class: "check" },
      h("span", { class: `state ${step.status === "failed" ? "fail" : ["pending", "upload"].includes(step.status) ? "warn" : "ok"}`, html: ICONS[step.status === "failed" ? "fail" : ["pending", "upload"].includes(step.status) ? "warn" : "ok"] }),
      h("div", { class: "what" }, h("div", { class: "name" }, step.target), h("div", { class: "detail" }, `${step.status} · ${step.detail}`)))))) : null;

  main.replaceChildren(h("div", { class: "page" },
    h("h1", {}, "Setup and health"),
    h("p", { class: "lead" }, "Claude and GPT reach your hub through a small server on this Mac. Here you can see which apps are connected, and fix the ones that aren't."),
    h("div", { class: "checks" }, rows),
    h("div", { class: "actions-row" },
      h("button", { class: "btn", onclick: (e) => busy(e.currentTarget, () => loadHealth(true)) }, icon("refresh"), "Check again"),
      h("button", { class: "btn primary", onclick: (e) => busy(e.currentTarget, runSetup) }, "Fix setup")),
    steps,
    h("h2", {}, "Your hub"),
    h("div", { class: "hub-card" },
      h("div", { class: "name" }, state.hub.repo),
      h("div", { class: "path" }, state.hub.root),
      state.hub.repo_url ? h("div", { class: "small muted" }, "GPT can read the pushed code at ", h("a", { href: state.hub.repo_url, target: "_blank", rel: "noopener noreferrer" }, state.hub.repo_url)) : null,
      h("div", { class: "actions-row" }, h("button", { class: "btn", onclick: () => chooseHub({ pick: true }) }, icon("folder"), "Use another project folder…")),
      state.recent.filter((p) => p !== state.hub.root).length
        ? h("ul", { class: "recent" }, state.recent.filter((p) => p !== state.hub.root).map(recentRow)) : null)));
}

async function runSetup() {
  try {
    state.setupSteps = (await api("/api/setup", { method: "POST" })).steps;
    const pending = state.setupSteps.some((s) => s.status === "pending");
    const upload = state.setupSteps.some((s) => s.status === "upload");
    if (pending) toast("Almost done: quit Claude (Cmd+Q) and it reopens with the hub.");
    else if (upload) toast("Done. Upload the Claude Chat skill to finish.");
    else toast("Setup is up to date");
    await loadHealth(true);
  } catch (err) {
    toast(err.message, "bad");
  }
}

// ---------- live updates ----------

let events;
function connectEvents() {
  if (events) events.close();
  let failures = 0;
  events = new EventSource("/api/events");
  events.addEventListener("hello", () => { failures = 0; setConnected(true); });
  events.addEventListener("change", async (event) => {
    const data = JSON.parse(event.data || "{}");
    if (data.hub) return boot();
    await loadThreads();
    if (state.view === "thread" && state.current && (data.threads || []).includes(state.current.id)) {
      const before = state.current.posts.length;
      try {
        state.current = await api(`/api/threads/${state.current.id}`);
        renderThread({ keepScroll: true, keepComposer: true });
        const item = state.threads.find((t) => t.id === state.current.id);
        if (item) { item.unread = 0; renderList(); }
        const added = state.current.posts.slice(before).filter((p) => p.author !== "human");
        if (added.length) toast(`${nameOf(added[added.length - 1].author)} posted in this thread`);
      } catch { /* the next change retries */ }
    }
  });
  events.onerror = () => { failures += 1; if (failures > 2) setConnected(false); };
}

function setConnected(ok) {
  state.connected = ok;
  document.getElementById("conn")?.classList.toggle("hidden", ok);
}

// Refresh relative times now and then.
setInterval(() => { if (state.view === "thread" && state.hub) renderList(); }, 60000);

boot();
