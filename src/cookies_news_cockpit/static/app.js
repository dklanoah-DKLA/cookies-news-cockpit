(() => {
  "use strict";

  const ICONS = {
    arrow: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5"></path></svg>',
    check: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"></path></svg>',
    cookie: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 1 1-9-9c0 2.4 1.6 4 4 4 0 2.4 1.6 4 4 4 .35 0 .68-.04 1-.12V12Z"></path><path d="M8.5 9h.01M8 15h.01M14 14h.01"></path></svg>',
    edit: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m4 20 4.2-1 10.6-10.6-3.2-3.2L5 15.8 4 20Z"></path><path d="m14.5 6.3 3.2 3.2"></path></svg>',
    external: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 5h5v5m0-5-9 9"></path><path d="M19 14v5H5V5h5"></path></svg>',
    file: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h8l4 4v14H6V3Z"></path><path d="M14 3v5h5M9 13h6M9 17h4"></path></svg>',
    heart: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.8 5.9a5.4 5.4 0 0 0-7.6 0L12 7.1l-1.2-1.2a5.4 5.4 0 0 0-7.6 7.6L12 22l8.8-8.5a5.4 5.4 0 0 0 0-7.6Z"></path></svg>',
    info: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 11v5m0-8h.01"></path></svg>',
    link: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.1.1l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1"></path><path d="M14 11a5 5 0 0 0-7.1-.1l-2 2A5 5 0 0 0 12 20l1.1-1.1"></path></svg>',
    plus: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg>',
    refresh: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7"></path><path d="M20 5v6h-6"></path></svg>',
    search: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.4-3.4"></path></svg>',
    spark: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 1.3 4.2L17.5 9l-4.2 1.8L12 15l-1.3-4.2L6.5 9l4.2-1.8L12 3Z"></path><path d="m19 15 .7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7L19 15Z"></path></svg>',
    trash: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3m3 0-1 14H7L6 7"></path><path d="M10 11v6m4-6v6"></path></svg>',
    warning: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2.5 20h19L12 3Z"></path><path d="M12 9v5m0 3h.01"></path></svg>'
  };

  const ACTIVE_RUN_STATES = new Set(["queued", "running"]);
  const FINAL_RUN_STATES = new Set(["complete", "degraded", "failed"]);
  const sessionToken = captureSessionToken();

  const state = {
    online: false,
    product: {},
    settings: {
      default_threshold: 60,
      default_article_limit: 20,
      refresh_minutes: 60,
      scheduler_enabled: true,
      extract_full_text: true
    },
    deepseek: { configured: false, model: "deepseek-chat" },
    topics: [],
    sources: [],
    currentRun: null,
    latestReport: null,
    history: [],
    historyNextOffset: null,
    historyTotal: 0,
    activeTopicId: null,
    reportFilter: "all",
    calibration: null,
    pollTimer: null,
    heartbeatTimer: null,
    commandIndex: 0,
    commandItems: []
  };

  const refs = {};

  function captureSessionToken() {
    const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const query = new URLSearchParams(window.location.search);
    const incoming = hash.get("token") || query.get("token");
    if (incoming) {
      window.sessionStorage.setItem("cookies-cockpit-token", incoming);
    }
    if (incoming || window.location.hash || query.has("token")) {
      window.history.replaceState(null, "", "/");
    }
    return incoming || window.sessionStorage.getItem("cookies-cockpit-token") || "";
  }

  function cacheRefs() {
    [
      "offline-banner", "retry-bootstrap", "service-label", "metric-topics", "metric-sources",
      "metric-last-run", "metric-run-state", "topic-count", "topic-rail-list", "show-all-topics",
      "latest-summary", "latest-list", "run-progress", "run-progress-title", "run-progress-detail",
      "progress-fill", "run-badge", "run-mode", "run-model", "run-cap", "run-dedupe", "run-retention",
      "topic-sheet", "source-sheet", "deepseek-badge", "deepseek-action", "history-filter",
      "history-query", "history-topic", "history-favorite", "history-list", "history-load-more", "dock-status", "dock-note",
      "start-run", "search-dialog", "command-query", "command-results", "topic-dialog", "topic-form",
      "topic-dialog-title", "topic-id", "topic-name", "topic-keywords", "topic-threshold", "topic-limit",
      "topic-threshold-default", "topic-limit-default", "topic-source-options", "topic-enabled", "source-dialog", "source-form", "source-dialog-title",
      "source-id", "source-name", "source-url", "source-enabled", "source-validation",
      "validate-source-draft", "settings-dialog", "settings-form", "setting-scheduler", "setting-threshold",
      "setting-article-limit", "setting-refresh", "setting-extract", "deepseek-dialog", "deepseek-form",
      "key-status", "deepseek-model", "deepseek-key", "toggle-key", "delete-key", "calibration-dialog",
      "calibration-body", "confirm-calibration", "toast-region", "open-search", "open-settings",
      "add-topic-rail", "add-topic", "add-source", "open-deepseek", "export-report", "shutdown-app"
    ].forEach((id) => {
      refs[toCamel(id)] = document.getElementById(id);
    });
  }

  function toCamel(value) {
    return value.replace(/-([a-z])/g, (_, character) => character.toUpperCase());
  }

  function node(tag, attributes = {}, children = []) {
    const element = document.createElement(tag);
    Object.entries(attributes).forEach(([key, value]) => {
      if (value === undefined || value === null || value === false) return;
      if (key === "className") element.className = value;
      else if (key === "text") element.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") {
        element.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key in element && !key.startsWith("aria")) {
        try { element[key] = value; } catch (_) { element.setAttribute(key, value); }
      } else {
        element.setAttribute(key, String(value));
      }
    });
    const normalized = Array.isArray(children) ? children : [children];
    normalized.forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return element;
  }

  function icon(name) {
    const holder = document.createElement("span");
    holder.innerHTML = ICONS[name] || ICONS.info;
    return holder.firstElementChild;
  }

  function safeExternalUrl(raw) {
    try {
      const parsed = new URL(raw);
      return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "#";
    } catch (_) {
      return "#";
    }
  }

  function externalLink(url, label, className = "") {
    const href = safeExternalUrl(url);
    const link = node("a", { href, className, text: label });
    if (href !== "#") {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    return link;
  }

  function describeError(error) {
    if (!error) return "发生未知错误";
    if (typeof error === "string") return error;
    if (Array.isArray(error.detail)) {
      return error.detail.map((item) => item.msg || "输入不符合要求").join("；");
    }
    return error.detail || error.message || "请求没有完成";
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), options.timeout || 15000);
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (sessionToken) headers.set("X-Cockpit-Token", sessionToken);
    if (options.body !== undefined && !(options.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }

    try {
      const response = await fetch(path, {
        method: options.method || "GET",
        headers,
        body: options.body === undefined
          ? undefined
          : options.body instanceof FormData
            ? options.body
            : JSON.stringify(options.body),
        signal: controller.signal,
        credentials: "same-origin"
      });
      if (!response.ok) {
        let payload = {};
        try { payload = await response.json(); } catch (_) { payload = { detail: `请求失败（${response.status}）` }; }
        const failure = new Error(describeError(payload));
        failure.status = response.status;
        failure.detail = payload.detail;
        throw failure;
      }
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) return response.json();
      return response;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("本地服务响应超时，请稍后重试");
      if (error instanceof TypeError) setConnection(false);
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function setConnection(online) {
    state.online = online;
    document.body.dataset.connection = online ? "online" : "offline";
    refs.offlineBanner.hidden = online;
    refs.serviceLabel.textContent = online
      ? "配置与历史保存在本机；启用 DeepSeek 时分析内容会发送至其云端"
      : "本地服务暂未连接；应用已退出时请重新打开 App";
    if (online) {
      refs.dockStatus.textContent = ACTIVE_RUN_STATES.has(state.currentRun?.status) ? "正在抓取" : "准备就绪";
    } else {
      refs.dockStatus.textContent = "等待服务";
    }
  }

  function setButtonState(button, value, label) {
    if (!button) return;
    if (value) button.dataset.state = value;
    else delete button.dataset.state;
    button.disabled = value === "loading";
    if (label) {
      const target = button.querySelector(".button__label") || button;
      target.textContent = label;
    }
  }

  async function runButtonTask(button, task, labels = {}) {
    const labelTarget = button.querySelector(".button__label");
    const original = labelTarget?.textContent || button.textContent;
    setButtonState(button, "loading", labels.loading || "处理中");
    try {
      const result = await task();
      setButtonState(button, "success", labels.success || "已完成");
      window.setTimeout(() => setButtonState(button, null, original), 900);
      return result;
    } catch (error) {
      setButtonState(button, "error", labels.error || "请重试");
      notify(describeError(error), "error");
      window.setTimeout(() => setButtonState(button, null, original), 1400);
      throw error;
    } finally {
      window.setTimeout(() => { button.disabled = false; }, 900);
    }
  }

  function notify(message, status = "info", action = null) {
    const toast = node("div", { className: "toast", role: "status", "data-state": status });
    toast.append(icon(status === "error" ? "warning" : status === "success" ? "check" : "info"));
    toast.append(node("span", { text: message }));
    if (action) {
      toast.dataset.action = "true";
      const actionButton = node("button", {
        className: "toast__action",
        type: "button",
        text: action.label,
        onclick: async () => {
          actionButton.disabled = true;
          try {
            await action.callback();
            removeToast(toast);
          } catch (error) {
            actionButton.disabled = false;
            notify(describeError(error), "error");
          }
        }
      });
      toast.append(actionButton);
    }
    refs.toastRegion.append(toast);
    window.setTimeout(() => removeToast(toast), action ? 8000 : 3600);
  }

  function removeToast(toast) {
    if (!toast?.isConnected) return;
    toast.classList.add("is-leaving");
    window.setTimeout(() => toast.remove(), 240);
  }

  function dateLabel(value, includeTime = true) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", includeTime
      ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
      : { year: "numeric", month: "short", day: "numeric" }
    ).format(date);
  }

  function effectiveThreshold(topic) {
    return topic.threshold ?? state.settings.default_threshold ?? 60;
  }

  function effectiveLimit(topic) {
    return topic.article_limit ?? state.settings.default_article_limit ?? 20;
  }

  function activeArticles() {
    const articles = Array.isArray(state.latestReport?.articles) ? state.latestReport.articles : [];
    return articles.filter((article) => {
      if (state.activeTopicId && article.topic_id !== state.activeTopicId) return false;
      if (state.reportFilter === "favorite" && !article.favorite) return false;
      return true;
    });
  }

  async function refreshBootstrap({ quiet = false } = {}) {
    try {
      const payload = await api("/api/bootstrap");
      state.product = payload.product || {};
      state.settings = { ...state.settings, ...(payload.settings || {}) };
      state.deepseek = { ...state.deepseek, ...(payload.deepseek || {}) };
      state.topics = Array.isArray(payload.topics) ? payload.topics : [];
      state.sources = Array.isArray(payload.sources) ? payload.sources : [];
      state.currentRun = payload.current_run || null;
      state.latestReport = payload.latest_report || null;
      if (state.activeTopicId && !state.topics.some((topic) => topic.id === state.activeTopicId)) {
        state.activeTopicId = null;
      }
      setConnection(true);
      renderAll();
      await loadHistory({ quiet: true });
      manageRunPolling();
      return payload;
    } catch (error) {
      setConnection(false);
      renderAll();
      if (!quiet) notify(describeError(error), "error");
      throw error;
    }
  }

  function renderAll() {
    renderMetrics();
    renderTopicRail();
    renderLatest();
    renderRunLane();
    renderTopicSheet();
    renderSourceSheet();
    renderDeepSeek();
    renderHistoryTopicOptions();
    renderHistory();
    renderTopicSourceOptions();
    populateSettingsForm();
  }

  function renderMetrics() {
    const enabledTopics = state.topics.filter((topic) => topic.enabled).length;
    const enabledSources = state.sources.filter((source) => source.enabled).length;
    refs.metricTopics.textContent = String(enabledTopics);
    refs.metricSources.textContent = String(enabledSources);
    refs.metricLastRun.textContent = state.currentRun ? dateLabel(state.currentRun.finished_at || state.currentRun.started_at) : "尚未运行";
    refs.metricRunState.textContent = runStatusLabel(state.currentRun?.status);
    refs.topicCount.textContent = String(state.topics.length);
    refs.addTopic.disabled = state.topics.length >= 10;
    refs.addTopicRail.disabled = state.topics.length >= 10;
  }

  function runStatusLabel(status) {
    return ({
      queued: "排队中",
      running: "抓取中",
      complete: "已完成",
      degraded: "部分完成",
      failed: "运行失败"
    })[status] || "待机";
  }

  function renderTopicRail() {
    refs.topicRailList.replaceChildren();
    if (!state.topics.length) {
      refs.topicRailList.append(node("p", { className: "rail-empty", text: "还没有主题。" }));
    } else {
      state.topics.forEach((topic) => {
        const active = state.activeTopicId === topic.id;
        const button = node("button", {
          className: `topic-rail-button${active ? " is-active" : ""}`,
          type: "button",
          text: topic.name,
          "aria-pressed": String(active),
          onclick: () => {
            state.activeTopicId = topic.id;
            state.reportFilter = "all";
            updateReportFilterButtons();
            renderTopicRail();
            renderLatest();
          }
        });
        if (!topic.enabled) button.dataset.disabledTopic = "true";
        refs.topicRailList.append(button);
      });
    }
    const allActive = state.activeTopicId === null;
    refs.showAllTopics.classList.toggle("is-active", allActive);
    refs.showAllTopics.setAttribute("aria-pressed", String(allActive));
  }

  function renderLatest() {
    const report = state.latestReport;
    const articles = activeArticles();
    refs.latestList.replaceChildren();

    if (report?.run) {
      const sourceErrors = Array.isArray(report.source_errors) ? report.source_errors.length : 0;
      const suffix = sourceErrors ? `，${sourceErrors} 个来源异常` : "";
      const duplicates = Number(report.run.duplicates_skipped || 0);
      const duplicateSuffix = duplicates ? `，已拦截 ${duplicates} 条近 7 天重复` : "";
      refs.latestSummary.textContent = `${dateLabel(report.run.finished_at || report.run.started_at)} · ${report.articles?.length || 0} 条${suffix}${duplicateSuffix}`;
    } else {
      refs.latestSummary.textContent = "等待第一次抓取。";
    }

    if (!articles.length) {
      const filtered = Boolean(state.activeTopicId || state.reportFilter === "favorite");
      refs.latestList.append(emptyState(
        filtered ? "这个视图里还没有内容" : "第一份报告在等你",
        filtered ? "换一个主题或查看全部新闻。" : "先添加新闻来源和主题，然后开始抓取。",
        filtered ? "查看全部" : state.topics.length ? "开始抓取" : "新建主题",
        () => {
          if (filtered) {
            state.activeTopicId = null;
            state.reportFilter = "all";
            updateReportFilterButtons();
            renderTopicRail();
            renderLatest();
          } else if (state.topics.length) {
            startRun();
          } else {
            openTopicDialog();
          }
        }
      ));
      return;
    }

    articles.forEach((article) => refs.latestList.append(articleRow(article, "story")));
  }

  function articleRow(article, variant = "story") {
    const body = node("div", { className: `${variant}-row__body` });
    const meta = node("div", { className: `${variant}-row__meta` });
    if (article.topic_name) meta.append(node("span", { className: "topic-label", text: article.topic_name }));
    if (article.source_name) meta.append(node("span", { text: article.source_name }));
    if (article.published_at) meta.append(node("span", { text: dateLabel(article.published_at) }));
    if (Number.isFinite(Number(article.score))) meta.append(node("span", { className: "score-label", text: `${article.score} 分` }));
    body.append(meta);

    const heading = node("h3");
    heading.append(externalLink(article.url, article.title || "未命名新闻"));
    body.append(heading);
    if (article.excerpt) body.append(node("p", { className: `${variant}-row__excerpt`, text: article.excerpt }));
    const analysis = article.analysis || article.summary;
    if (analysis) body.append(node("p", { className: "analysis-note", text: analysis }));

    const favorite = node("button", {
      className: "favorite-button",
      type: "button",
      title: article.favorite ? "取消收藏" : "收藏",
      "aria-label": article.favorite ? `取消收藏：${article.title}` : `收藏：${article.title}`,
      "aria-pressed": String(Boolean(article.favorite)),
      disabled: !article.id,
      onclick: () => toggleFavorite(article, favorite)
    }, icon("heart"));
    return node("article", { className: `${variant}-row` }, [body, favorite]);
  }

  function emptyState(title, copy, actionLabel, action) {
    const wrapper = node("div", { className: "empty-state" });
    wrapper.append(node("span", { className: "empty-state__symbol" }, icon("cookie")));
    wrapper.append(node("h3", { text: title }));
    wrapper.append(node("p", { text: copy }));
    if (actionLabel && action) {
      wrapper.append(node("button", { className: "button button--secondary", type: "button", onclick: action }, [icon("arrow"), node("span", { className: "button__label", text: actionLabel })]));
    }
    return wrapper;
  }

  function renderRunLane() {
    const run = state.currentRun;
    const status = run?.status;
    refs.runBadge.textContent = runStatusLabel(status);
    refs.runBadge.dataset.state = ACTIVE_RUN_STATES.has(status) ? "running" : status === "complete" ? "success" : ["degraded", "failed"].includes(status) ? "error" : "idle";
    refs.runMode.textContent = state.settings.scheduler_enabled ? `自动 · ${state.settings.refresh_minutes} 分钟` : "仅手动";
    refs.runModel.textContent = state.deepseek.model || "deepseek-chat";
    refs.runCap.textContent = "500 次分析";
    const duplicates = Number(run?.duplicates_skipped || 0);
    refs.runDedupe.textContent = duplicates ? `近 7 天 · 拦截 ${duplicates} 条` : "默认 · 近 7 天";
    refs.runRetention.textContent = "30 天";
    refs.metricRunState.textContent = runStatusLabel(status);
    document.body.dataset.runState = ACTIVE_RUN_STATES.has(status) ? "running" : "idle";

    const active = ACTIVE_RUN_STATES.has(status);
    setButtonState(refs.startRun, active ? "loading" : null, active ? "正在抓取" : "开始抓取");
    refs.startRun.disabled = active || !state.online;
    refs.runProgress.hidden = !active;
    if (active) {
      refs.runProgressTitle.textContent = status === "queued" ? "任务已进入队列" : "正在抓取并分析";
      refs.runProgressDetail.textContent = run?.warning || "来源失败不会阻断其他来源。";
      const progress = status === "queued" ? 0.22 : 0.62;
      refs.progressFill.parentElement.dataset.phase = status === "queued" ? "queued" : "running";
      refs.progressFill.parentElement.setAttribute("aria-valuenow", String(Math.round(progress * 100)));
      refs.progressFill.parentElement.setAttribute("aria-valuetext", status === "queued" ? "排队中" : "运行中");
      refs.dockStatus.textContent = status === "queued" ? "等待运行" : "正在抓取";
      refs.dockNote.textContent = "可以继续浏览；完成后这里会自动刷新。";
    } else if (status === "failed") {
      refs.dockStatus.textContent = "上次运行失败";
      refs.dockNote.textContent = run.error || "旧报告已安全保留，可以重试。";
    } else if (status === "degraded") {
      refs.dockStatus.textContent = "上次部分完成";
      refs.dockNote.textContent = run.warning || "部分来源未成功，已保留可用结果。";
    } else if (status === "complete" && Number(run?.article_count || 0) === 0 && duplicates > 0) {
      refs.dockStatus.textContent = "重复审核完成";
      refs.dockNote.textContent = `近 7 天内无新增；已拦截 ${duplicates} 条重复新闻。`;
    } else {
      refs.dockStatus.textContent = state.online ? "准备就绪" : "等待服务";
      refs.dockNote.textContent = "选择好主题后，一键抓取并分析。";
    }
  }

  function renderTopicSheet() {
    refs.topicSheet.replaceChildren();
    if (!state.topics.length) {
      refs.topicSheet.append(emptyState("先创建一个主题", "主题决定抓什么、筛多严、每次保留多少条。", "新建主题", () => openTopicDialog()));
      return;
    }

    state.topics.forEach((topic) => {
      const toggle = node("button", {
        className: "mini-toggle",
        type: "button",
        role: "switch",
        title: topic.enabled ? "停用主题" : "启用主题",
        "aria-label": `${topic.enabled ? "停用" : "启用"}主题：${topic.name}`,
        "aria-checked": String(Boolean(topic.enabled)),
        "aria-pressed": String(Boolean(topic.enabled)),
        onclick: async () => {
          try {
            await api(`/api/topics/${encodeURIComponent(topic.id)}`, { method: "PUT", body: { enabled: !topic.enabled } });
            topic.enabled = !topic.enabled;
            renderAll();
          } catch (error) { notify(describeError(error), "error"); }
        }
      });

      const title = node("div", { className: "data-row__title" }, [toggle, node("span", { text: topic.name })]);
      const keywords = node("div", { className: "data-row__keywords", text: topic.keywords?.length ? topic.keywords.join(" · ") : "未设置关键词" });
      const threshold = node("div", { text: `${effectiveThreshold(topic)} 分` });
      const limit = node("div", { text: `${effectiveLimit(topic)} 条` });
      const actions = node("div", { className: "row-actions" }, [
        rowAction("spark", "AI 校准", (button) => calibrateTopic(topic, button)),
        rowAction("edit", "编辑", () => openTopicDialog(topic)),
        rowAction("trash", "移除", () => deleteTopic(topic), true)
      ]);
      refs.topicSheet.append(node("div", { className: "data-row topic-grid" }, [title, keywords, threshold, limit, actions]));
    });
  }

  function renderSourceSheet() {
    refs.sourceSheet.replaceChildren();
    if (!state.sources.length) {
      refs.sourceSheet.append(emptyState("还没有新闻来源", "添加一个 RSS 或 Atom 地址，驾驶舱才知道去哪里找新闻。", "添加来源", () => openSourceDialog()));
      return;
    }

    state.sources.forEach((source) => {
      const toggle = node("button", {
        className: "mini-toggle",
        type: "button",
        role: "switch",
        title: source.enabled ? "停用来源" : "启用来源",
        "aria-label": `${source.enabled ? "停用" : "启用"}来源：${source.name}`,
        "aria-checked": String(Boolean(source.enabled)),
        "aria-pressed": String(Boolean(source.enabled)),
        onclick: async () => {
          try {
            await api(`/api/sources/${encodeURIComponent(source.id)}`, { method: "PUT", body: { enabled: !source.enabled } });
            source.enabled = !source.enabled;
            renderAll();
          } catch (error) { notify(describeError(error), "error"); }
        }
      });
      const title = node("div", { className: "data-row__title" }, [toggle, node("span", { text: source.name })]);
      const url = node("div", { className: "data-row__url" }, externalLink(source.url, source.url || "—"));
      const type = node("div", {}, node("span", { className: "type-label", text: "RSS / Atom" }));
      const health = sourceHealth(source);
      const badge = node("span", { className: "state-badge", text: health.label, "data-state": health.state, title: source.last_error || "" });
      const actions = node("div", { className: "row-actions" }, [
        rowAction("refresh", "验证", (button) => validateSource(source, button)),
        rowAction("edit", "编辑", () => openSourceDialog(source)),
        rowAction("trash", "移除", () => deleteSource(source), true)
      ]);
      refs.sourceSheet.append(node("div", { className: "data-row source-grid" }, [title, url, type, badge, actions]));
    });
  }

  function sourceHealth(source) {
    if (source.last_status === "ok") return { label: "正常", state: "success" };
    if (source.last_status === "error") return { label: "异常", state: "error" };
    return { label: "未验证", state: "idle" };
  }

  function rowAction(iconName, label, handler, danger = false) {
    return node("button", {
      className: `row-action${danger ? " row-action--danger" : ""}`,
      type: "button",
      onclick: (event) => handler(event.currentTarget)
    }, [icon(iconName), node("span", { text: label })]);
  }

  function renderDeepSeek() {
    const configured = Boolean(state.deepseek.configured);
    refs.deepseekBadge.textContent = configured ? "已配置" : "未配置";
    refs.deepseekBadge.dataset.state = configured ? "success" : "error";
    refs.deepseekAction.querySelector(".button__label").textContent = configured ? "管理密钥" : "配置密钥";
    refs.keyStatus.dataset.state = configured ? "success" : "error";
    refs.keyStatus.replaceChildren();
    const dot = node("span", { className: `status-dot status-dot--${configured ? "success" : "error"}`, "aria-hidden": "true" });
    refs.keyStatus.append(dot, node("span", { text: configured ? "密钥已安全保存在 macOS 钥匙串。" : "尚未配置密钥；抓取仍可运行，但只做关键词初筛。" }));
    refs.deepseekModel.value = state.deepseek.model || "deepseek-chat";
    refs.deleteKey.disabled = !configured;
  }

  function renderHistoryTopicOptions() {
    const selected = refs.historyTopic.value;
    refs.historyTopic.replaceChildren(node("option", { value: "", text: "全部主题" }));
    state.topics.forEach((topic) => refs.historyTopic.append(node("option", { value: topic.id, text: topic.name })));
    if ([...refs.historyTopic.options].some((option) => option.value === selected)) refs.historyTopic.value = selected;
  }

  async function loadHistory({ quiet = false, append = false } = {}) {
    const params = new URLSearchParams();
    const q = refs.historyQuery?.value?.trim();
    const topicId = refs.historyTopic?.value;
    if (q) params.set("q", q);
    if (topicId) params.set("topic_id", topicId);
    if (refs.historyFavorite?.checked) params.set("favorite", "true");
    params.set("limit", "50");
    params.set("offset", String(append ? state.historyNextOffset ?? 0 : 0));
    try {
      const payload = await api(`/api/history${params.size ? `?${params}` : ""}`);
      const incoming = Array.isArray(payload.articles) ? payload.articles : [];
      if (append) {
        const seen = new Set(state.history.map((article) => article.id || article.url));
        state.history.push(...incoming.filter((article) => !seen.has(article.id || article.url)));
      } else {
        state.history = incoming;
      }
      state.historyNextOffset = Number.isInteger(payload.next_offset) ? payload.next_offset : null;
      state.historyTotal = Number.isInteger(payload.total) ? payload.total : state.history.length;
      renderHistory();
    } catch (error) {
      if (!quiet) notify(describeError(error), "error");
      if (append) throw error;
    }
  }

  function renderHistory() {
    refs.historyList.replaceChildren();
    refs.historyLoadMore.hidden = state.historyNextOffset === null;
    if (!state.history.length) {
      refs.historyList.append(emptyState("没有找到历史内容", "完成一次抓取后，报告与收藏会出现在这里。", null, null));
      return;
    }
    state.history.forEach((article) => refs.historyList.append(articleRow(article, "history")));
  }

  function renderTopicSourceOptions(selectedIds = null) {
    if (!refs.topicSourceOptions) return;
    const chosen = new Set(selectedIds || checkedSourceIds() || []);
    refs.topicSourceOptions.replaceChildren();
    if (!state.sources.length) {
      refs.topicSourceOptions.append(node("p", { className: "helper-copy", text: "先添加新闻来源；留空时主题会使用全部启用来源。" }));
      return;
    }
    state.sources.forEach((source) => {
      const input = node("input", { type: "checkbox", value: source.id, checked: chosen.has(source.id) });
      refs.topicSourceOptions.append(node("label", { className: "check-option" }, [input, node("span", { text: source.name })]));
    });
  }

  function checkedSourceIds() {
    if (!refs.topicSourceOptions) return [];
    return [...refs.topicSourceOptions.querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value);
  }

  function populateSettingsForm() {
    refs.settingScheduler.checked = Boolean(state.settings.scheduler_enabled);
    refs.settingThreshold.value = state.settings.default_threshold ?? 60;
    refs.settingArticleLimit.value = state.settings.default_article_limit ?? 20;
    const refresh = String(state.settings.refresh_minutes ?? 60);
    if (![...refs.settingRefresh.options].some((option) => option.value === refresh)) {
      refs.settingRefresh.append(node("option", { value: refresh, text: `每 ${refresh} 分钟` }));
    }
    refs.settingRefresh.value = refresh;
    refs.settingExtract.checked = Boolean(state.settings.extract_full_text);
  }

  function openDialog(dialog, focusTarget = null) {
    if (!dialog || dialog.open) return;
    dialog.showModal();
    window.setTimeout(() => (focusTarget || dialog.querySelector("input, button, select, textarea"))?.focus(), 30);
  }

  function closeDialog(dialog) {
    if (dialog?.open) dialog.close();
  }

  function openTopicDialog(topic = null) {
    refs.topicForm.reset();
    refs.topicId.value = topic?.id || "";
    refs.topicDialogTitle.textContent = topic ? "编辑主题" : "新建主题";
    refs.topicName.value = topic?.name || "";
    refs.topicKeywords.value = topic?.keywords?.join(", ") || "";
    refs.topicThreshold.value = topic?.threshold ?? state.settings.default_threshold ?? 60;
    refs.topicLimit.value = topic?.article_limit ?? state.settings.default_article_limit ?? 20;
    refs.topicThresholdDefault.checked = topic ? topic.threshold === null : true;
    refs.topicLimitDefault.checked = topic ? topic.article_limit === null : true;
    syncTopicDefaultControls();
    refs.topicEnabled.checked = topic ? Boolean(topic.enabled) : true;
    renderTopicSourceOptions(topic?.source_ids || []);
    openDialog(refs.topicDialog, refs.topicName);
  }

  async function submitTopic(event) {
    event.preventDefault();
    if (!refs.topicForm.reportValidity()) return;
    const id = refs.topicId.value;
    const payload = {
      name: refs.topicName.value.trim(),
      keywords: splitKeywords(refs.topicKeywords.value),
      threshold: refs.topicThresholdDefault.checked ? null : Number(refs.topicThreshold.value),
      article_limit: refs.topicLimitDefault.checked ? null : Number(refs.topicLimit.value),
      source_ids: checkedSourceIds(),
      enabled: refs.topicEnabled.checked
    };
    const submit = refs.topicForm.querySelector('[type="submit"]');
    try {
      await runButtonTask(submit, () => api(id ? `/api/topics/${encodeURIComponent(id)}` : "/api/topics", { method: id ? "PUT" : "POST", body: payload }), {
        loading: "保存中", success: "已保存"
      });
      closeDialog(refs.topicDialog);
      await refreshBootstrap({ quiet: true });
    } catch (_) { /* error surfaced by runButtonTask */ }
  }

  function splitKeywords(value) {
    const seen = new Set();
    return value.split(/[，,\n]/).map((item) => item.trim()).filter((item) => {
      const key = item.toLocaleLowerCase();
      if (!item || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function syncTopicDefaultControls() {
    refs.topicThreshold.disabled = refs.topicThresholdDefault.checked;
    refs.topicLimit.disabled = refs.topicLimitDefault.checked;
    if (refs.topicThresholdDefault.checked) refs.topicThreshold.value = state.settings.default_threshold ?? 60;
    if (refs.topicLimitDefault.checked) refs.topicLimit.value = state.settings.default_article_limit ?? 20;
  }

  async function deleteTopic(topic) {
    try {
      await api(`/api/topics/${encodeURIComponent(topic.id)}`, { method: "DELETE" });
      state.topics = state.topics.filter((item) => item.id !== topic.id);
      if (state.activeTopicId === topic.id) state.activeTopicId = null;
      renderAll();
      notify(`已移除主题“${topic.name}”`, "success", {
        label: "撤销",
        callback: async () => {
          await api("/api/topics", { method: "POST", body: topicPayload(topic) });
          await refreshBootstrap({ quiet: true });
        }
      });
    } catch (error) { notify(describeError(error), "error"); }
  }

  function topicPayload(topic) {
    return {
      name: topic.name,
      keywords: topic.keywords || [],
      threshold: topic.threshold,
      article_limit: topic.article_limit,
      source_ids: topic.source_ids || [],
      enabled: topic.enabled !== false
    };
  }

  function openSourceDialog(source = null) {
    refs.sourceForm.reset();
    refs.sourceId.value = source?.id || "";
    refs.sourceDialogTitle.textContent = source ? "编辑来源" : "添加来源";
    refs.sourceName.value = source?.name || "";
    refs.sourceUrl.value = source?.url || "";
    refs.sourceEnabled.checked = source ? Boolean(source.enabled) : true;
    refs.sourceValidation.hidden = true;
    refs.validateSourceDraft.disabled = !source;
    refs.validateSourceDraft.querySelector(".button__label").textContent = source ? "验证来源" : "保存后可验证";
    openDialog(refs.sourceDialog, refs.sourceName);
  }

  async function submitSource(event) {
    event.preventDefault();
    if (!refs.sourceForm.reportValidity()) return;
    const id = refs.sourceId.value;
    const payload = {
      name: refs.sourceName.value.trim(),
      url: refs.sourceUrl.value.trim(),
      enabled: refs.sourceEnabled.checked
    };
    const submit = refs.sourceForm.querySelector('[type="submit"]');
    try {
      await runButtonTask(submit, () => api(id ? `/api/sources/${encodeURIComponent(id)}` : "/api/sources", { method: id ? "PUT" : "POST", body: payload }), {
        loading: "保存中", success: "已保存"
      });
      closeDialog(refs.sourceDialog);
      await refreshBootstrap({ quiet: true });
    } catch (_) { /* error surfaced by runButtonTask */ }
  }

  async function validateSource(source, button) {
    try {
      const result = await runButtonTask(button, () => api(`/api/sources/${encodeURIComponent(source.id)}/validate`, { method: "POST" }), { loading: "验证中", success: "正常" });
      notify(result?.sample_title ? `验证成功：${result.sample_title}` : "来源验证成功", "success");
      await refreshBootstrap({ quiet: true });
    } catch (error) {
      if (!button) notify(describeError(error), "error");
    }
  }

  async function validateDraftSource() {
    const id = refs.sourceId.value;
    if (!id) return;
    refs.sourceValidation.hidden = false;
    refs.sourceValidation.dataset.state = "running";
    refs.sourceValidation.textContent = "正在读取订阅地址…";
    try {
      const result = await runButtonTask(refs.validateSourceDraft, () => api(`/api/sources/${encodeURIComponent(id)}/validate`, { method: "POST" }), { loading: "验证中", success: "验证通过" });
      refs.sourceValidation.dataset.state = "success";
      refs.sourceValidation.textContent = result?.sample_title ? `读取成功：${result.sample_title}` : "读取成功，这个来源可以使用。";
      await refreshBootstrap({ quiet: true });
    } catch (error) {
      refs.sourceValidation.dataset.state = "error";
      refs.sourceValidation.textContent = describeError(error);
    }
  }

  async function deleteSource(source) {
    try {
      await api(`/api/sources/${encodeURIComponent(source.id)}`, { method: "DELETE" });
      state.sources = state.sources.filter((item) => item.id !== source.id);
      state.topics.forEach((topic) => { topic.source_ids = (topic.source_ids || []).filter((id) => id !== source.id); });
      renderAll();
      notify(`已移除来源“${source.name}”`, "success", {
        label: "撤销",
        callback: async () => {
          await api("/api/sources", { method: "POST", body: { name: source.name, url: source.url, enabled: source.enabled !== false } });
          await refreshBootstrap({ quiet: true });
        }
      });
    } catch (error) { notify(describeError(error), "error"); }
  }

  async function calibrateTopic(topic, button) {
    if (!state.deepseek.configured) {
      notify("请先配置 DeepSeek API Key", "error");
      openDeepSeekDialog();
      return;
    }
    try {
      const request = () => api(`/api/topics/${encodeURIComponent(topic.id)}/calibrate`, {
        method: "POST",
        body: {
          goal: `围绕“${topic.name}”提升相关性，减少泛化和广告内容。`,
          positive_examples: [],
          negative_examples: []
        },
        timeout: 45000
      });
      const payload = await runButtonTask(button, request, { loading: "分析中", success: "建议已生成" });
      state.calibration = { topic, ...(payload.calibration || {}) };
      renderCalibration();
      openDialog(refs.calibrationDialog, refs.confirmCalibration);
    } catch (error) {
      if (!button) notify(describeError(error), "error");
    }
  }

  function renderCalibration() {
    refs.calibrationBody.replaceChildren();
    const proposal = state.calibration?.proposal || {};
    const current = state.calibration?.topic || {};
    const rows = [
      ["当前关键词", current.keywords?.join(" · ") || "—"],
      ["建议关键词", proposal.keywords?.join(" · ") || "—"],
      ["当前门槛", `${effectiveThreshold(current)} 分`],
      ["建议门槛", Number.isFinite(Number(proposal.threshold)) ? `${proposal.threshold} 分` : "—"],
      ["建议理由", proposal.rationale || "—"]
    ];
    const list = node("dl", { className: "calibration-list" });
    rows.forEach(([term, value]) => list.append(node("div", { className: "calibration-row" }, [node("dt", { text: term }), node("dd", { text: value })])));
    refs.calibrationBody.append(list);
  }

  async function confirmCalibration() {
    const calibration = state.calibration;
    if (!calibration?.id || !calibration?.topic?.id) return;
    try {
      await runButtonTask(refs.confirmCalibration, () => api(`/api/topics/${encodeURIComponent(calibration.topic.id)}/calibrate/confirm`, {
        method: "POST",
        body: { calibration_id: calibration.id }
      }), { loading: "应用中", success: "已应用" });
      closeDialog(refs.calibrationDialog);
      state.calibration = null;
      await refreshBootstrap({ quiet: true });
    } catch (_) { /* surfaced */ }
  }

  async function toggleFavorite(article, button) {
    if (!article.id) return;
    const next = !article.favorite;
    article.favorite = next;
    button.setAttribute("aria-pressed", String(next));
    button.setAttribute("aria-label", next ? `取消收藏：${article.title}` : `收藏：${article.title}`);
    try {
      const payload = await api(`/api/articles/${encodeURIComponent(article.id)}/favorite`, { method: "PUT", body: { favorite: next } });
      syncFavoriteAcrossViews(article.id, payload.article || { favorite: next });
      renderLatest();
      if (refs.historyFavorite.checked) await loadHistory({ quiet: true, append: false });
      else renderHistory();
    } catch (error) {
      syncFavoriteAcrossViews(article.id, { favorite: !next });
      button.setAttribute("aria-pressed", String(!next));
      renderLatest();
      renderHistory();
      notify(describeError(error), "error");
    }
  }

  function syncFavoriteAcrossViews(articleId, update) {
    const reportArticles = Array.isArray(state.latestReport?.articles) ? state.latestReport.articles : [];
    [...reportArticles, ...state.history].forEach((item) => {
      if (item.id === articleId) Object.assign(item, update);
    });
  }

  async function startRun() {
    const enabledTopics = state.topics.filter((topic) => topic.enabled);
    const enabledSources = state.sources.filter((source) => source.enabled);
    if (!enabledTopics.length) {
      notify("请先启用至少一个主题", "error");
      openTopicDialog(state.topics[0] || null);
      return;
    }
    if (!enabledSources.length) {
      notify("请先启用至少一个新闻来源", "error");
      openSourceDialog(state.sources[0] || null);
      return;
    }
    const body = state.activeTopicId ? { topic_ids: [state.activeTopicId] } : {};
    try {
      const payload = await api("/api/runs", { method: "POST", body });
      state.currentRun = payload.run || null;
      renderRunLane();
      manageRunPolling();
    } catch (error) { notify(describeError(error), "error"); }
  }

  function manageRunPolling() {
    window.clearTimeout(state.pollTimer);
    if (!ACTIVE_RUN_STATES.has(state.currentRun?.status)) return;
    state.pollTimer = window.setTimeout(pollRun, 2200);
  }

  async function pollRun() {
    try {
      const payload = await api("/api/runs/current", { timeout: 8000 });
      const previous = state.currentRun?.status;
      state.currentRun = payload.run || null;
      renderRunLane();
      if (FINAL_RUN_STATES.has(state.currentRun?.status)) {
        const reportPayload = await api("/api/reports/latest");
        state.latestReport = reportPayload.report || null;
        renderLatest();
        await loadHistory({ quiet: true });
        if (state.currentRun.status !== previous) {
          const duplicateOnly = state.currentRun.status === "complete"
            && Number(state.currentRun.article_count || 0) === 0
            && Number(state.currentRun.duplicates_skipped || 0) > 0;
          notify(
            duplicateOnly
              ? "重复审核完成：近 7 天内没有新增新闻"
              : state.currentRun.status === "complete"
                ? "本次报告已完成"
                : state.currentRun.status === "degraded"
                  ? "本次抓取部分完成，旧报告保护已生效"
                  : "本次运行失败，旧报告已保留",
            duplicateOnly ? "info" : state.currentRun.status === "complete" ? "success" : "error"
          );
        }
      }
    } catch (_) { /* connection state is handled by api */ }
    manageRunPolling();
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (!refs.settingsForm.reportValidity()) return;
    const payload = {
      scheduler_enabled: refs.settingScheduler.checked,
      default_threshold: Number(refs.settingThreshold.value),
      default_article_limit: Number(refs.settingArticleLimit.value),
      refresh_minutes: Number(refs.settingRefresh.value),
      extract_full_text: refs.settingExtract.checked
    };
    const submit = refs.settingsForm.querySelector('[type="submit"]');
    try {
      const response = await runButtonTask(submit, () => api("/api/settings", { method: "PUT", body: payload }), { loading: "保存中", success: "已保存" });
      state.settings = { ...state.settings, ...(response.settings || payload) };
      closeDialog(refs.settingsDialog);
      renderMetrics();
      renderRunLane();
    } catch (_) { /* surfaced */ }
  }

  function openDeepSeekDialog() {
    refs.deepseekKey.value = "";
    refs.deepseekKey.type = "password";
    renderDeepSeek();
    openDialog(refs.deepseekDialog, refs.deepseekKey);
  }

  async function saveDeepSeek(event) {
    event.preventDefault();
    const key = refs.deepseekKey.value.trim();
    if (!key) {
      closeDialog(refs.deepseekDialog);
      return;
    }
    const submit = refs.deepseekForm.querySelector('[type="submit"]');
    try {
      const payload = await runButtonTask(submit, () => api("/api/settings/deepseek-key", { method: "PUT", body: { api_key: key } }), { loading: "保存中", success: "已保存" });
      state.deepseek = { ...state.deepseek, ...payload };
      refs.deepseekKey.value = "";
      closeDialog(refs.deepseekDialog);
      renderDeepSeek();
    } catch (_) { /* surfaced */ }
  }

  async function deleteDeepSeekKey() {
    try {
      const payload = await runButtonTask(refs.deleteKey, () => api("/api/settings/deepseek-key", { method: "DELETE" }), { loading: "删除中", success: "已删除" });
      state.deepseek = { ...state.deepseek, ...payload };
      renderDeepSeek();
    } catch (_) { /* surfaced */ }
  }

  async function exportData() {
    try {
      await runButtonTask(refs.exportReport, async () => {
        const response = await api("/api/export", { timeout: 30000 });
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = node("a", { href: url, download: "cookies-news-cockpit-export.zip" });
        document.body.append(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      }, { loading: "打包中", success: "已导出" });
      notify("导出包已保存；其中不包含 API Key", "success");
    } catch (_) { /* surfaced */ }
  }

  async function shutdownApp() {
    try {
      await api("/api/shutdown", { method: "POST" });
      notify("应用正在安全退出", "success");
      window.setTimeout(() => window.close(), 350);
    } catch (error) { notify(describeError(error), "error"); }
  }

  function openCommandSearch() {
    refs.commandQuery.value = "";
    renderCommandResults("");
    openDialog(refs.searchDialog, refs.commandQuery);
  }

  function commandCatalog(query) {
    const normalized = query.trim().toLocaleLowerCase();
    const candidates = [];
    state.topics.forEach((topic) => candidates.push({
      group: "主题", title: topic.name, subtitle: topic.keywords?.join(" · ") || "未设置关键词", icon: "spark",
      action: () => { state.activeTopicId = topic.id; renderTopicRail(); renderLatest(); document.getElementById("latest").scrollIntoView(); }
    }));
    state.sources.forEach((source) => candidates.push({
      group: "来源", title: source.name, subtitle: source.url, icon: "link",
      action: () => openSourceDialog(source)
    }));
    const seen = new Set();
    [...activeArticles(), ...state.history].forEach((article) => {
      const key = article.id || article.url;
      if (seen.has(key)) return;
      seen.add(key);
      candidates.push({
        group: "新闻", title: article.title || "未命名新闻", subtitle: article.source_name || article.excerpt || "", icon: "file",
        action: () => {
          const url = safeExternalUrl(article.url);
          if (url !== "#") window.open(url, "_blank", "noopener,noreferrer");
        }
      });
    });
    if (!normalized) return candidates.slice(0, 8);
    return candidates.filter((item) => `${item.title} ${item.subtitle} ${item.group}`.toLocaleLowerCase().includes(normalized)).slice(0, 30);
  }

  function renderCommandResults(query) {
    const items = commandCatalog(query);
    state.commandItems = items;
    state.commandIndex = 0;
    refs.commandResults.replaceChildren();
    if (!items.length) {
      refs.commandResults.append(node("div", { className: "empty-state empty-state--compact" }, [node("h2", { text: "没有匹配结果" }), node("p", { text: "换一个关键词试试。" })]));
      return;
    }
    let lastGroup = null;
    items.forEach((item, index) => {
      if (item.group !== lastGroup) {
        refs.commandResults.append(node("p", { className: "command-group-label", text: item.group }));
        lastGroup = item.group;
      }
      const button = node("button", {
        className: `command-item${index === 0 ? " is-active" : ""}`,
        type: "button",
        role: "option",
        "aria-selected": String(index === 0),
        onclick: () => {
          closeDialog(refs.searchDialog);
          item.action();
        }
      }, [
        icon(item.icon),
        node("span", { className: "command-item__copy" }, [node("span", { text: item.title }), node("small", { text: item.subtitle || item.group })]),
        icon("arrow")
      ]);
      refs.commandResults.append(button);
    });
  }

  function moveCommandSelection(delta) {
    const buttons = [...refs.commandResults.querySelectorAll(".command-item")];
    if (!buttons.length) return;
    state.commandIndex = (state.commandIndex + delta + buttons.length) % buttons.length;
    buttons.forEach((button, index) => {
      const active = index === state.commandIndex;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
      if (active) button.scrollIntoView({ block: "nearest" });
    });
  }

  function updateReportFilterButtons() {
    document.querySelectorAll("[data-report-filter]").forEach((button) => {
      const active = button.dataset.reportFilter === state.reportFilter;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function bindEvents() {
    refs.retryBootstrap.addEventListener("click", () => refreshBootstrap().catch(() => {}));
    refs.openSearch.addEventListener("click", openCommandSearch);
    refs.openSettings.addEventListener("click", () => { populateSettingsForm(); openDialog(refs.settingsDialog); });
    refs.addTopicRail.addEventListener("click", () => openTopicDialog());
    refs.addTopic.addEventListener("click", () => openTopicDialog());
    refs.addSource.addEventListener("click", () => openSourceDialog());
    refs.openDeepseek.addEventListener("click", openDeepSeekDialog);
    refs.deepseekAction.addEventListener("click", openDeepSeekDialog);
    refs.exportReport.addEventListener("click", exportData);
    refs.shutdownApp.addEventListener("click", shutdownApp);
    refs.startRun.addEventListener("click", startRun);
    refs.showAllTopics.addEventListener("click", () => {
      state.activeTopicId = null;
      renderTopicRail();
      renderLatest();
    });
    document.querySelectorAll("[data-report-filter]").forEach((button) => button.addEventListener("click", () => {
      state.reportFilter = button.dataset.reportFilter;
      updateReportFilterButtons();
      renderLatest();
    }));

    refs.topicForm.addEventListener("submit", submitTopic);
    refs.topicThresholdDefault.addEventListener("change", syncTopicDefaultControls);
    refs.topicLimitDefault.addEventListener("change", syncTopicDefaultControls);
    refs.sourceForm.addEventListener("submit", submitSource);
    refs.settingsForm.addEventListener("submit", saveSettings);
    refs.deepseekForm.addEventListener("submit", saveDeepSeek);
    refs.historyFilter.addEventListener("submit", (event) => { event.preventDefault(); loadHistory({ append: false }); });
    refs.historyLoadMore.addEventListener("click", async () => {
      try {
        await runButtonTask(refs.historyLoadMore, () => loadHistory({ quiet: true, append: true }), { loading: "加载中", success: "已加载" });
      } catch (_) { /* surfaced by runButtonTask */ }
    });
    refs.validateSourceDraft.addEventListener("click", validateDraftSource);
    refs.confirmCalibration.addEventListener("click", confirmCalibration);
    refs.deleteKey.addEventListener("click", deleteDeepSeekKey);
    refs.toggleKey.addEventListener("click", () => {
      refs.deepseekKey.type = refs.deepseekKey.type === "password" ? "text" : "password";
      refs.toggleKey.setAttribute("aria-label", refs.deepseekKey.type === "password" ? "显示密钥" : "隐藏密钥");
    });

    document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => closeDialog(button.closest("dialog"))));
    document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => {
      if (event.target === dialog) closeDialog(dialog);
    }));

    refs.commandQuery.addEventListener("input", () => renderCommandResults(refs.commandQuery.value));
    refs.commandQuery.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeDialog(refs.searchDialog);
        return;
      }
      if (event.key === "ArrowDown") { event.preventDefault(); moveCommandSelection(1); }
      if (event.key === "ArrowUp") { event.preventDefault(); moveCommandSelection(-1); }
      if (event.key === "Enter") {
        const active = refs.commandResults.querySelector(".command-item.is-active");
        if (active) { event.preventDefault(); active.click(); }
      }
    });
    document.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        refs.searchDialog.open ? closeDialog(refs.searchDialog) : openCommandSearch();
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) sendHeartbeat();
    });
  }

  async function sendHeartbeat() {
    if (document.hidden) return;
    try {
      const heartbeat = await api("/api/heartbeat", { method: "POST", timeout: 6000 });
      if (!state.online) setConnection(true);
      const previousId = state.currentRun?.id;
      const previousStatus = state.currentRun?.status;
      const currentPayload = await api("/api/runs/current", { timeout: 6000 });
      state.currentRun = currentPayload.run || null;
      const changed = previousId !== state.currentRun?.id || previousStatus !== state.currentRun?.status;
      renderRunLane();
      if (changed && FINAL_RUN_STATES.has(state.currentRun?.status)) {
        const reportPayload = await api("/api/reports/latest", { timeout: 6000 });
        state.latestReport = reportPayload.report || null;
        renderLatest();
        await loadHistory({ quiet: true });
      }
      if (heartbeat.run_active || ACTIVE_RUN_STATES.has(state.currentRun?.status)) manageRunPolling();
    } catch (_) { /* connection state handled by api */ }
  }

  function startHeartbeat() {
    window.clearInterval(state.heartbeatTimer);
    sendHeartbeat();
    state.heartbeatTimer = window.setInterval(sendHeartbeat, 30000);
  }

  async function init() {
    cacheRefs();
    bindEvents();
    renderAll();
    if (!sessionToken) {
      setConnection(false);
      refs.offlineBanner.querySelector("span:nth-child(2)").textContent = "缺少本地会话令牌，请从应用重新打开驾驶舱。";
    }
    try { await refreshBootstrap({ quiet: true }); } catch (_) { /* empty-state fallback remains usable */ }
    startHeartbeat();
  }

  init();
})();
