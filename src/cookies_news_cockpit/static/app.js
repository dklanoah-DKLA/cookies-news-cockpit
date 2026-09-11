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
  const FINAL_RUN_STATES = new Set(["complete", "degraded", "failed", "cancelled"]);
  // Server allows 45 s transfer + 5 s entry validation. Leave response margin.
  const SOURCE_VALIDATION_TIMEOUT = 60000;
  const sessionToken = captureSessionToken();

  const state = {
    online: false,
    product: {},
    settings: {
      default_threshold: 60,
      default_article_limit: 20,
      freshness_days: 7,
      refresh_minutes: 60,
      scheduler_enabled: true,
      extract_full_text: true,
      semantic_fallback_enabled: true,
      semantic_fallback_limit: 20
    },
    deepseek: { configured: false, model: "deepseek-v4-flash", status: "unconfigured" },
    topics: [],
    sources: [],
    archivedSources: [],
    sourceScopeWarning: null,
    currentSourceErrors: [],
    sourcePresets: [],
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
    commandItems: [],
    runStarting: false,
    runCancelling: false,
    favoritePending: new Set(),
    topicSuggestion: null,
    topicDraftBaseline: "",
    topicDialogVersion: 0,
    topicSaving: false,
    importPreview: null,
    onboardingStep: 0,
    onboardingPresented: false,
    onboardingBusy: false
  };

  const refs = {};
  let keywordEditor;
  let excludeEditor;

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
      "latest-summary", "latest-list", "report-context", "run-progress", "run-progress-title", "run-progress-detail",
      "progress-fill", "run-funnel", "funnel-breakdown", "funnel-fetched", "funnel-fresh", "funnel-matched", "funnel-unique", "funnel-fulltext", "funnel-fulltext-success",
      "funnel-semantic-initial", "funnel-semantic-additional", "funnel-semantic", "funnel-duplicates", "funnel-history-duplicates", "funnel-run-duplicates",
      "funnel-ai", "funnel-ai-success", "funnel-ai-failure", "funnel-core", "funnel-supplement", "funnel-below-supplement", "funnel-threshold", "funnel-budget", "funnel-kept", "cancel-run", "source-errors",
      "run-badge", "run-mode", "run-analysis-mode", "run-model", "run-cap", "run-dedupe", "run-retention",
      "topic-sheet", "source-sheet", "source-search", "source-category-filter", "source-filter-count", "deepseek-badge", "deepseek-action", "history-filter",
      "history-query", "history-topic", "history-favorite", "history-list", "history-load-more", "dock-status", "dock-note",
      "start-run", "source-scope-warning", "search-dialog", "command-query", "command-results", "topic-dialog", "topic-form",
      "topic-dialog-title", "topic-id", "topic-name", "topic-keywords", "topic-keyword-count", "topic-keyword-chips", "topic-excludes", "topic-exclude-count", "topic-exclude-chips", "topic-save-error", "topic-draft-status",
      "topic-threshold", "topic-limit", "suggest-topic", "topic-suggestion", "topic-suggestion-copy", "topic-suggestion-chips", "apply-topic-suggestion",
      "topic-threshold-default", "topic-limit-default", "topic-source-options", "topic-enabled", "source-dialog", "source-form", "source-dialog-title",
      "source-id", "source-name", "source-homepage", "source-url", "source-category", "source-language", "source-preset", "source-terms", "source-enabled", "source-validation",
      "validate-source-draft", "settings-dialog", "settings-form", "setting-scheduler", "setting-threshold",
      "setting-article-limit", "setting-freshness", "setting-refresh", "setting-extract", "setting-semantic", "setting-semantic-limit", "deepseek-dialog", "deepseek-form",
      "key-status", "deepseek-model", "deepseek-key", "toggle-key", "delete-key", "test-key", "deepseek-test-result", "calibration-dialog",
      "calibration-body", "confirm-calibration", "toast-region", "open-search", "open-settings",
      "add-topic-rail", "add-topic", "add-source", "open-deepseek", "export-report", "import-report", "import-file", "shutdown-app", "shutdown-state", "shutdown-copy",
      "onboarding-dialog", "onboarding-step-label", "onboarding-title", "onboarding-copy", "skip-onboarding", "wizard-source-categories", "wizard-topic-name", "wizard-topic-keywords",
      "wizard-deepseek-key", "wizard-key-result", "wizard-test-key", "wizard-back", "wizard-skip-step", "wizard-next", "import-dialog", "import-preview", "import-portable-settings", "apply-import"
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

  function isCompatibilityError(error) {
    if (![404, 405, 422].includes(Number(error?.status))) return false;
    const detail = describeError(error).toLocaleLowerCase();
    return error.status !== 422 || /extra|unexpected|unknown|not permitted|不允许|字段/.test(detail);
  }

  function normalizeDeepSeekStatus(value = state.deepseek) {
    const status = value?.status || value?.deepseek_status;
    if (["unconfigured", "saved_unverified", "connected", "error"].includes(status)) return status;
    if (value?.error || value?.deepseek_error) return "error";
    return value?.configured ? "saved_unverified" : "unconfigured";
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), options.timeout || 15000);
    const headers = new Headers(options.headers || {});
    const binaryBody = options.body instanceof Blob || options.body instanceof ArrayBuffer || ArrayBuffer.isView(options.body);
    headers.set("Accept", "application/json");
    if (sessionToken) headers.set("X-Cockpit-Token", sessionToken);
    if (options.body !== undefined && !(options.body instanceof FormData) && !binaryBody) {
      headers.set("Content-Type", "application/json");
    }

    try {
      const response = await fetch(path, {
        method: options.method || "GET",
        headers,
        body: options.body === undefined
          ? undefined
          : options.body instanceof FormData || binaryBody
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

  async function apiWithLegacyBody(path, options, legacyBody) {
    try {
      return await api(path, options);
    } catch (error) {
      if (!isCompatibilityError(error) || legacyBody === undefined) throw error;
      return api(path, { ...options, body: legacyBody });
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
    if (value === "loading") button.disabled = true;
    if (label) {
      const target = button.querySelector(".button__label") || button;
      target.textContent = label;
    }
  }

  async function runButtonTask(button, task, labels = {}) {
    const labelTarget = button.querySelector(".button__label");
    const original = labelTarget?.textContent || button.textContent;
    const taskSequence = (button._taskSequence || 0) + 1;
    button._taskSequence = taskSequence;
    setButtonState(button, "loading", labels.loading || "处理中");
    try {
      const result = await task();
      setButtonState(button, "success", labels.success || "已完成");
      window.setTimeout(() => {
        if (button._taskSequence === taskSequence && button.dataset.state === "success") setButtonState(button, null, original);
      }, 900);
      return result;
    } catch (error) {
      setButtonState(button, "error", labels.error || "请重试");
      if (error && typeof error === "object") error.cockpitNotified = true;
      notify(describeError(error), "error");
      window.setTimeout(() => {
        if (button._taskSequence === taskSequence && button.dataset.state === "error") setButtonState(button, null, original);
      }, 1400);
      throw error;
    } finally {
      window.setTimeout(() => {
        if (button._taskSequence === taskSequence && button.dataset.state !== "loading") button.disabled = Boolean(labels.disabledAfter);
      }, 900);
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

  function firstNumeric(record, ...keys) {
    for (const key of keys) {
      const candidate = record?.[key];
      if (candidate !== "" && candidate !== null && candidate !== undefined && Number.isFinite(Number(candidate))) {
        return Number(candidate);
      }
    }
    return null;
  }

  function funnelNumber(run, ...keys) {
    return firstNumeric(run?.funnel || {}, ...keys) ?? firstNumeric(run || {}, ...keys);
  }

  function duplicateCount(run) {
    const legacyTotal = funnelNumber(run, "duplicates_skipped", "duplicate_count");
    if (legacyTotal !== null) return legacyTotal;
    const history = funnelNumber(run, "history_duplicates");
    const current = funnelNumber(run, "run_duplicates");
    return history !== null || current !== null ? (history || 0) + (current || 0) : null;
  }

  function topicFunnelFor(report, article) {
    const funnel = report?.run?.funnel || report?.funnel || {};
    const collection = funnel.per_topic || funnel.by_topic || report?.run?.topic_funnels || report?.topic_funnels;
    const topicId = article?.topic_id;
    if (Array.isArray(collection)) {
      const direct = collection.find((item) => (item.topic_id || item.id) === topicId);
      if (direct) return direct;
    } else if (collection && typeof collection === "object") {
      const direct = topicId && Object.prototype.hasOwnProperty.call(collection, topicId)
        ? collection[topicId]
        : null;
      if (direct && typeof direct === "object") return direct;
    }

    const topicName = typeof article?.topic_name === "string"
      ? article.topic_name.trim().toLocaleLowerCase()
      : "";
    if (!topicName) return null;
    const snapshots = Array.isArray(collection)
      ? collection
      : collection && typeof collection === "object"
        ? Object.values(collection)
        : [];
    const matches = snapshots.filter((snapshot) => {
      if (!snapshot || typeof snapshot !== "object") return false;
      const name = typeof snapshot.name === "string" ? snapshot.name : snapshot.topic_name;
      return typeof name === "string" && name.trim().toLocaleLowerCase() === topicName;
    });
    return matches.length === 1 ? matches[0] : null;
  }

  function articleTier(article, report) {
    const snapshot = topicFunnelFor(report, article);
    const coreThreshold = firstNumeric(snapshot, "core_threshold", "threshold");
    const supplementThreshold = firstNumeric(snapshot, "supplement_threshold", "extended_threshold");
    const score = firstNumeric(article, "score");
    if (score === null || coreThreshold === null || supplementThreshold === null) return "core";
    if (score >= coreThreshold) return "core";
    if (score >= supplementThreshold) return "supplement";
    return "supplement";
  }

  function runTimestamp(run) {
    return run?.finished_at || run?.completed_at || run?.started_at || run?.created_at || null;
  }

  function reportTimestamp(report) {
    return report?.generated_at || report?.created_at || runTimestamp(report?.run) || null;
  }

  function retainedReportContext(report) {
    const current = state.currentRun;
    const reportRun = report?.run;
    if (!current || !reportRun || !FINAL_RUN_STATES.has(current.status)) return null;
    const selected = funnelNumber(current, "selected", "kept", "stored_count") ?? firstNumeric(current, "article_count") ?? 0;
    const differentIds = Boolean(current.id && reportRun.id && current.id !== reportRun.id);
    const currentTime = runTimestamp(current);
    const effectiveTime = reportTimestamp(report);
    const differentTimes = Boolean(currentTime && effectiveTime && currentTime !== effectiveTime);
    if (!differentIds && !differentTimes) return null;
    return { currentTime, effectiveTime, selected };
  }

  function renderReportContext(report) {
    const context = retainedReportContext(report);
    refs.reportContext.replaceChildren();
    refs.reportContext.hidden = !context && report?.run?.status !== "degraded";
    if (!context && report?.run?.status === "degraded") {
      refs.reportContext.append(
        node("strong", { text: state.currentRun?.id === report.run.id ? "本次部分完成 · 已展示可用新结果" : "当前报告部分完成 · 已展示可用结果" }),
        node("span", { text: report.run.warning || "部分来源或 AI 未成功，已保留并展示可用文章。" })
      );
      return;
    }
    if (!context) return;
    refs.reportContext.append(
      node("strong", { text: context.selected === 0 ? "本次新增 0 条 · 当前为上次有效报告" : "当前为上次有效报告 · 本次结果可在历史中查看" }),
      node("span", {
        text: `本次任务：${dateLabel(context.currentTime)} · 当前报告：${dateLabel(context.effectiveTime)}`
      })
    );
  }

  function reportTier(title, copy, articles, tier, report) {
    const section = node("section", { className: "report-tier", "data-tier": tier });
    const heading = node("div", { className: "report-tier__heading" }, [
      node("div", {}, [node("h3", { text: title }), node("p", { text: copy })]),
      node("span", { className: "report-tier__count", text: `${articles.length} 条` })
    ]);
    const list = node("div", { className: "report-tier__list" });
    articles.forEach((article) => list.append(articleRow(article, "story", tier)));
    section.append(heading, list);
    return section;
  }

  async function refreshBootstrap({ quiet = false } = {}) {
    try {
      const payload = await api("/api/bootstrap");
      state.product = payload.product || {};
      state.settings = { ...state.settings, ...(payload.settings || {}) };
      const deepseekPayload = payload.deepseek || {};
      const mergedDeepSeek = { ...state.deepseek, ...deepseekPayload };
      if (!deepseekPayload.status && payload.settings?.deepseek_status) mergedDeepSeek.status = payload.settings.deepseek_status;
      state.deepseek = {
        ...mergedDeepSeek,
        status: normalizeDeepSeekStatus(mergedDeepSeek)
      };
      state.sourcePresets = Array.isArray(payload.source_presets || payload.presets) ? (payload.source_presets || payload.presets) : [];
      state.topics = Array.isArray(payload.topics) ? payload.topics : [];
      state.sources = Array.isArray(payload.sources) ? payload.sources : [];
      state.archivedSources = Array.isArray(payload.archived_sources) ? payload.archived_sources : [];
      state.sourceScopeWarning = payload.source_scope_warning || null;
      state.currentRun = payload.current_run || null;
      state.currentSourceErrors = Array.isArray(payload.current_source_errors) ? payload.current_source_errors : [];
      state.latestReport = payload.latest_report || null;
      if (state.activeTopicId && !state.topics.some((topic) => topic.id === state.activeTopicId)) {
        state.activeTopicId = null;
      }
      setConnection(true);
      renderAll();
      await loadHistory({ quiet: true });
      manageRunPolling();
      maybeOpenOnboarding(payload);
      return payload;
    } catch (error) {
      setConnection(false);
      renderAll();
      if (!quiet) notify(describeError(error), "error");
      throw error;
    }
  }

  function renderAll() {
    renderSourceScopeWarning();
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
      failed: "运行失败",
      cancelled: "已取消"
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
            renderRunLane();
          }
        });
        if (!topic.enabled) {
          button.dataset.disabledTopic = "true";
          button.title = "此主题已停用，仅可查看历史，不能单独运行";
        }
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
    renderSourceErrors(report);
    renderReportContext(report);

    if (report?.run) {
      const sourceErrors = Array.isArray(report.source_errors) ? report.source_errors.length : 0;
      const suffix = sourceErrors ? `，${sourceErrors} 个来源异常` : "";
      const duplicates = duplicateCount(report.run);
      const duplicateSuffix = duplicates ? `，已拦截 ${duplicates} 条近 7 天重复` : "";
      refs.latestSummary.textContent = `报告时间 ${dateLabel(reportTimestamp(report))} · ${report.articles?.length || 0} 条${suffix}${duplicateSuffix}`;
    } else {
      refs.latestSummary.textContent = "等待第一次抓取。";
    }

    if (!articles.length) {
      const filtered = Boolean(state.activeTopicId || state.reportFilter === "favorite");
      const outcome = report?.run?.outcome || state.currentRun?.outcome;
      const duplicateOnly = (duplicateCount(report?.run) ?? duplicateCount(state.currentRun) ?? 0) > 0;
      const outcomeMessages = {
        cancelled: ["任务已取消", "已保留上一份有效报告；可以随时重新运行。"],
        all_sources_failed: ["来源暂时都无法读取", "查看来源错误，修复地址或稍后重试；上一份有效报告未被覆盖。"],
        no_fresh_articles: ["所选时效内没有可用新闻", "可以检查来源日期，或在设置里调整新闻时效。"],
        no_keyword_match: ["近期新闻未命中主题", "可以扩展关键词、检查排除词，或启用结果不足时智能补充。"],
        no_relevant_after_ai: ["智能复核后仍没有相关新闻", "已检查本次候选，但没有足够证据与主题相关；可以补充更具体的关键词、别名或可靠来源后再试。"],
        no_match: ["本次没有新闻达到门槛", "可以降低入选分数、扩展关键词，或检查排除词。"],
        no_matches: ["本次没有新闻达到门槛", "可以降低入选分数、扩展关键词，或检查排除词。"],
        below_threshold: ["候选新闻都低于门槛", "可以降低入选分数，或继续保持严格筛选。"],
        no_new_after_dedup: ["近 7 天没有新增新闻", "重复审核已完成，上一份有效报告继续保留。"]
      };
      const message = filtered
        ? ["这个视图里还没有内容", "换一个主题、取消收藏筛选，或查看全部新闻。"]
        : outcomeMessages[outcome]
          || (duplicateOnly ? outcomeMessages.no_new_after_dedup : ["第一份报告在等你", "先添加新闻来源和主题，然后开始抓取。"]);
      const [emptyTitle, emptyCopy] = message;
      refs.latestList.append(emptyState(
        emptyTitle,
        emptyCopy,
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

    const core = [];
    const supplement = [];
    articles.forEach((article) => {
      (articleTier(article, report) === "supplement" ? supplement : core).push(article);
    });
    if (core.length) {
      refs.latestList.append(reportTier(
        "核心新闻",
        "达到该主题本次核心门槛，优先阅读。",
        core,
        "core",
        report
      ));
    }
    if (supplement.length) {
      refs.latestList.append(reportTier(
        "补充阅读",
        "达到补充线但未达到核心门槛，用于补足背景和弱信号。",
        supplement,
        "supplement",
        report
      ));
    }
  }

  function renderSourceScopeWarning() {
    const warning = state.sourceScopeWarning;
    const topics = state.topics.filter((topic) => (warning?.topic_ids || []).includes(topic.id));
    refs.sourceScopeWarning.replaceChildren();
    refs.sourceScopeWarning.hidden = !topics.length;
    if (!topics.length) return;
    refs.sourceScopeWarning.append(
      node("strong", { text: "请确认升级前的主题来源" }),
      node("p", { text: warning.message || "旧版移除来源后，部分主题的范围需要重新确认。" }),
      node("button", { type: "button", className: "button button--secondary", text: "检查来源", onclick: () => openTopicDialog(topics[0]) })
    );
  }

  function renderSourceErrors(report) {
    const errors = state.currentRun ? state.currentSourceErrors : (Array.isArray(report?.source_errors) ? report.source_errors : []);
    refs.sourceErrors.replaceChildren();
    refs.sourceErrors.hidden = !errors.length;
    if (!errors.length) return;
    refs.sourceErrors.append(node("strong", { text: `本次 ${errors.length} 个来源需要检查` }));
    const list = node("ul");
    errors.forEach((error) => {
      const item = typeof error === "string" ? { error } : error;
      const source = [...state.sources, ...state.archivedSources].find((candidate) => candidate.id === item.source_id);
      list.append(node("li", { text: `${source?.name || item.source_name || "未知来源"}：${item.error || item.message || "读取失败"}` }));
    });
    refs.sourceErrors.append(list);
  }

  function articleRow(article, variant = "story", tier = null) {
    const body = node("div", { className: `${variant}-row__body` });
    const meta = node("div", { className: `${variant}-row__meta` });
    if (tier) {
      meta.append(node("span", {
        className: "selection-label",
        text: tier === "supplement" ? "补充" : "核心",
        "data-tier": tier
      }));
    }
    if (article.topic_name) meta.append(node("span", { className: "topic-label", text: article.topic_name }));
    if (article.source_name) meta.append(node("span", { text: article.source_name }));
    if (article.published_at) meta.append(node("span", { text: dateLabel(article.published_at) }));
    if (Number.isFinite(Number(article.score))) meta.append(node("span", { className: "score-label", text: `${article.score} 分` }));
    const analysisMode = article.analysis_mode || article.mode || (article.model ? "ai" : "rules");
    const usedAi = String(analysisMode).startsWith("ai") || analysisMode === "deepseek";
    const modeText = usedAi
      ? `AI${String(analysisMode).includes("semantic") ? " 语义" : ""}${String(analysisMode).includes("cache") ? " · 缓存" : article.model ? ` · ${article.model}` : ""}`
      : String(analysisMode).includes("fallback") || String(analysisMode).includes("breaker")
        ? "规则降级"
        : "规则筛选";
    meta.append(node("span", { className: "mode-label", text: modeText, "data-mode": usedAi ? "ai" : "rules" }));
    body.append(meta);

    const heading = node("h3");
    heading.append(externalLink(article.url, article.title || "未命名新闻"));
    body.append(heading);
    if (article.excerpt) body.append(node("p", { className: `${variant}-row__excerpt`, text: article.excerpt }));
    const analysis = article.analysis || article.summary;
    if (analysis) body.append(node("p", { className: "analysis-note", text: analysis }));
    if (Array.isArray(article.matched_keywords) && article.matched_keywords.length) {
      body.append(node("p", { className: "match-note", text: `命中：${article.matched_keywords.join(" · ")}${Array.isArray(article.matched_fields) && article.matched_fields.length ? `（${article.matched_fields.join("、")}）` : ""}` }));
    }

    const favorite = node("button", {
      className: "favorite-button",
      type: "button",
      title: article.favorite ? "取消收藏" : "收藏",
      "aria-label": article.favorite ? `取消收藏：${article.title}` : `收藏：${article.title}`,
      "aria-pressed": String(Boolean(article.favorite)),
      disabled: !article.id || state.favoritePending.has(article.id),
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
    const trigger = run?.trigger || (["scheduler", "manual"].includes(run?.mode) ? run.mode : null);
    refs.runMode.textContent = trigger === "scheduler" ? "自动任务" : trigger === "manual" ? "手动" : state.settings.scheduler_enabled ? `自动 · ${state.settings.refresh_minutes} 分钟` : "仅手动";
    const reportedAnalysisMode = run?.analysis_mode || (!["scheduler", "manual"].includes(run?.mode) ? run?.mode : null);
    const aiRequests = Number(run?.ai_requests ?? run?.analysis_count);
    const aiSuccess = Number(run?.ai_success);
    const aiFailure = Number(run?.ai_failure);
    const explicitMode = String(reportedAnalysisMode || "");
    refs.runAnalysisMode.textContent = explicitMode === "mixed" || (aiSuccess > 0 && aiFailure > 0)
      ? "AI + 规则"
      : explicitMode.includes("fallback") || (aiFailure > 0 && !(aiSuccess > 0))
        ? "规则降级"
        : explicitMode.startsWith("ai") || aiSuccess > 0 || aiRequests > 0
          ? "AI 分析"
          : state.deepseek.status === "connected" && ACTIVE_RUN_STATES.has(status)
            ? "AI 已就绪"
            : "规则筛选";
    refs.runModel.textContent = state.deepseek.model || "deepseek-v4-flash";
    const analyses = Number(run?.analyses ?? run?.analysis_count);
    refs.runCap.textContent = Number.isFinite(analyses) ? `${analyses} / 500 次分析` : "500 次分析";
    const duplicates = duplicateCount(run);
    refs.runDedupe.textContent = duplicates ? `近 7 天 · 拦截 ${duplicates} 条` : "默认 · 近 7 天";
    refs.runRetention.textContent = "30 天";
    refs.metricRunState.textContent = runStatusLabel(status);
    document.body.dataset.runState = ACTIVE_RUN_STATES.has(status) ? "running" : "idle";

    const active = ACTIVE_RUN_STATES.has(status) || state.runStarting;
    const selectedTopic = state.activeTopicId ? state.topics.find((topic) => topic.id === state.activeTopicId) : null;
    const selectedTopicDisabled = Boolean(selectedTopic && !selectedTopic.enabled);
    setButtonState(refs.startRun, active ? "loading" : null, state.runStarting ? "正在启动" : active ? "正在抓取" : selectedTopicDisabled ? "主题已停用" : "开始抓取");
    refs.startRun.disabled = active || !state.online || selectedTopicDisabled;
    refs.runProgress.hidden = !(active || run);
    refs.runProgress.dataset.state = active ? "running" : "finished";
    refs.cancelRun.hidden = !ACTIVE_RUN_STATES.has(status);
    refs.cancelRun.disabled = state.runCancelling;
    refs.importReport.disabled = active;
    if (refs.importDialog.open) refs.applyImport.disabled = active || !(state.importPreview?.import_id || state.importPreview?.id);
    renderRunFunnel(run);
    if (active || run) {
      const progress = runProgress(run, status);
      refs.progressFill.parentElement.dataset.progress = String(Math.round(progress / 10) * 10);
      refs.progressFill.parentElement.dataset.phase = run?.phase || status || "queued";
      refs.progressFill.parentElement.setAttribute("aria-valuenow", String(progress));
      refs.progressFill.parentElement.setAttribute("aria-valuetext", `${phaseLabel(run?.phase, status)}，${progress}%`);
      if (!active) {
        refs.runProgressTitle.textContent = status === "failed" ? "本次运行失败" : status === "degraded" ? "本次部分完成" : status === "cancelled" ? "本次任务已取消" : "本次处理完成";
        refs.runProgressDetail.textContent = run?.warning || run?.error || ({
          no_fresh_articles: "所选时效内没有可用新闻。",
          no_keyword_match: "近期新闻未命中主题关键词。",
          no_new_after_dedup: "重复审核完成，近 7 天内没有新增新闻。",
          below_threshold: "候选新闻均低于当前入选门槛。"
        })[run?.outcome] || `最终入选 ${run?.funnel?.selected ?? run?.article_count ?? 0} 条新闻。`;
      }
    }
    if (active) {
      refs.runProgressTitle.textContent = state.runCancelling ? "正在取消任务" : phaseLabel(run?.phase, status);
      refs.runProgressDetail.textContent = run?.warning || "来源失败不会阻断其他来源。";
      refs.dockStatus.textContent = status === "queued" ? "等待运行" : "正在抓取";
      refs.dockNote.textContent = "可以继续浏览；完成后这里会自动刷新。";
    } else if (status === "failed") {
      refs.dockStatus.textContent = "上次运行失败";
      refs.dockNote.textContent = run.error || "旧报告已安全保留，可以重试。";
    } else if (status === "degraded") {
      refs.dockStatus.textContent = "上次部分完成";
      refs.dockNote.textContent = run.warning || "部分来源未成功，已保留可用结果。";
    } else if (status === "cancelled") {
      refs.dockStatus.textContent = "上次任务已取消";
      refs.dockNote.textContent = "旧报告已保留，可以重新运行。";
    } else if (status === "complete" && Number(run?.article_count || 0) === 0 && duplicates > 0) {
      refs.dockStatus.textContent = "重复审核完成";
      refs.dockNote.textContent = `近 7 天内无新增；已拦截 ${duplicates} 条重复新闻。`;
    } else {
      refs.dockStatus.textContent = state.online ? "准备就绪" : "等待服务";
      refs.dockNote.textContent = "选择好主题后，一键抓取并分析。";
    }
  }

  function phaseLabel(phase, status) {
    return ({
      queued: "任务已进入队列",
      fetching: "正在抓取来源",
      filtering: "正在筛选时效与关键词",
      deduplicating: "正在审核重复新闻",
      matching: "正在匹配关键词",
      analyzing: "正在进行 AI 审核",
      saving: "正在保存报告",
      persisting: "正在安全写入报告",
      finished: "报告已完成",
      complete: "报告已完成",
      cancelled: "任务已取消"
    })[phase] || (status === "queued" ? "任务已进入队列" : "正在抓取并分析");
  }

  function runProgress(run, status) {
    const explicit = Number(run?.progress_percent ?? run?.progress);
    if (Number.isFinite(explicit)) return Math.max(0, Math.min(100, explicit <= 1 ? Math.round(explicit * 100) : Math.round(explicit)));
    if (FINAL_RUN_STATES.has(status)) return 100;
    return ({ queued: 8, fetching: 24, filtering: 52, deduplicating: 46, matching: 61, analyzing: 78, saving: 92, persisting: 94, finished: 100, complete: 100, cancelled: 100 })[run?.phase || status] || 16;
  }

  function renderRunFunnel(run) {
    const value = (...keys) => {
      const candidate = funnelNumber(run, ...keys);
      return candidate === null ? "—" : String(candidate);
    };
    const historyDuplicates = funnelNumber(run, "history_duplicates");
    const runDuplicates = funnelNumber(run, "run_duplicates");
    const duplicateTotal = duplicateCount(run);
    const budget = funnelNumber(run, "semantic_budget_exhausted", "budget_exhausted");

    refs.funnelFetched.textContent = value("feed_items", "fetched", "fetched_count", "candidate_count");
    refs.funnelFresh.textContent = value("fresh", "fresh_count");
    refs.funnelMatched.textContent = value("keyword_hits", "matched", "keyword_matched", "matched_count");
    refs.funnelUnique.textContent = value("unique_candidates", "unique", "deduped_candidates");
    refs.funnelFulltext.textContent = value("fulltext_fetches", "fulltext_count");
    refs.funnelFulltextSuccess.textContent = value("fulltext_success", "fulltext_succeeded");
    refs.funnelSemanticInitial.textContent = value("semantic_initial_reviewed", "semantic_fallback");
    refs.funnelSemanticAdditional.textContent = value("semantic_additional_reviewed", "semantic_expansion");
    refs.funnelSemantic.textContent = value("semantic_reviewed", "semantic_fallback");
    refs.funnelAi.textContent = value("ai_requests", "ai_reviewed", "analysis_count");
    refs.funnelAiSuccess.textContent = value("ai_success");
    refs.funnelAiFailure.textContent = value("ai_failure");
    refs.funnelHistoryDuplicates.textContent = historyDuplicates === null ? "—" : String(historyDuplicates);
    refs.funnelRunDuplicates.textContent = runDuplicates === null ? "—" : String(runDuplicates);
    refs.funnelDuplicates.textContent = duplicateTotal === null ? "—" : String(duplicateTotal);
    refs.funnelCore.textContent = value("core_selected");
    refs.funnelSupplement.textContent = value("supplement_selected", "extended_selected");
    refs.funnelBelowSupplement.textContent = value("below_supplement", "below_extended");
    refs.funnelThreshold.textContent = value("threshold_rejected", "below_threshold");
    refs.funnelBudget.textContent = budget === null ? "—" : budget > 0 ? "是" : "否";
    refs.funnelKept.textContent = value("selected", "kept", "article_count", "stored_count");
    renderFunnelBreakdown(run);
  }

  function funnelCollection(run, ...keys) {
    const funnel = run?.funnel || {};
    for (const key of keys) {
      const candidate = funnel[key] ?? run?.[key];
      if (Array.isArray(candidate)) return candidate;
      if (candidate && typeof candidate === "object") {
        return Object.entries(candidate).map(([id, item]) => ({ id, ...(item || {}) }));
      }
    }
    return [];
  }

  function breakdownReasonRows(item) {
    const aliases = [
      ["date_missing", "缺少发布日期"],
      ["date_invalid", "发布日期无效"],
      ["date_stale", "超出新闻时效"],
      ["date_future", "日期明显来自未来"],
      ["exclusion_rejected", "命中排除词"],
      ["reason_off_topic", "语义判断不相关"],
      ["reason_exclusion_match", "语义判断命中排除条件"],
      ["reason_insufficient_evidence", "相关证据不足"],
      ["below_supplement", "低于补充阅读线", "below_extended"],
      ["history_duplicates", "近 7 天历史重复"],
      ["run_duplicates", "本次任务内重复"]
    ];
    const rows = aliases.flatMap(([key, label, legacy]) => {
      const value = firstNumeric(item, key, legacy);
      return value && value > 0 ? [[label, value]] : [];
    });
    const extra = item.rejection_reasons || item.rejected_by_reason || item.reasons;
    if (extra && typeof extra === "object" && !Array.isArray(extra)) {
      Object.entries(extra).forEach(([reason, count]) => {
        if (Number.isFinite(Number(count)) && Number(count) > 0) rows.push([reason, Number(count)]);
      });
    }
    return rows;
  }

  function renderBreakdownGroup(title, items, kind) {
    const section = node("section", { className: "funnel-breakdown__group" });
    section.append(node("strong", { text: title }));
    items.forEach((item) => {
      const id = item.topic_id || item.source_id || item.id;
      const fallback = kind === "topic"
        ? state.topics.find((topic) => topic.id === id)?.name
        : state.sources.find((source) => source.id === id)?.name;
      const name = item.name || item.topic_name || item.source_name || fallback || "未命名";
      const selected = firstNumeric(item, "selected", "kept");
      const detail = node("details", { className: "funnel-detail" });
      const summaryCopy = kind === "topic"
        ? `核心线 ${firstNumeric(item, "core_threshold", "threshold") ?? "—"} · 补充线 ${firstNumeric(item, "supplement_threshold", "extended_threshold") ?? "—"}`
        : `入选 ${selected ?? 0} 条`;
      detail.append(node("summary", {}, [
        node("span", { text: name }),
        node("small", { text: summaryCopy })
      ]));
      const definitions = kind === "topic"
        ? [
          ["发现", ["discovered", "feed_items"]],
          ["去重候选", ["unique_candidates"]],
          ["语义复核", ["semantic_reviewed", "semantic_fallback"]],
          ["核心", ["core_selected"]],
          ["补充", ["supplement_selected", "extended_selected"]],
          ["最终入选", ["selected", "kept"]]
        ]
        : [
          ["抓取", ["feed_items", "fetched"]],
          ["时效内", ["fresh"]],
          ["去重候选", ["unique_candidates"]],
          ["全文成功", ["fulltext_success"]],
          ["最终入选", ["selected", "kept"]]
        ];
      const metrics = node("dl", { className: "funnel-detail__metrics" });
      definitions.forEach(([label, keys]) => {
        const count = firstNumeric(item, ...keys);
        if (count !== null) metrics.append(node("div", {}, [node("dt", { text: label }), node("dd", { text: String(count) })]));
      });
      if (metrics.childElementCount) detail.append(metrics);
      const reasons = breakdownReasonRows(item);
      if (reasons.length) {
        const list = node("ul", { className: "funnel-reasons" });
        reasons.forEach(([label, count]) => list.append(node("li", { text: `${label}：${count}` })));
        detail.append(node("p", { className: "funnel-reasons__title", text: "处理与淘汰原因" }), list);
      }
      section.append(detail);
    });
    return section;
  }

  function renderFunnelBreakdown(run) {
    const topics = funnelCollection(run, "per_topic", "by_topic", "topic_funnels");
    const sources = funnelCollection(run, "per_source", "by_source", "source_funnels");
    refs.funnelBreakdown.replaceChildren();
    refs.funnelBreakdown.hidden = !(topics.length || sources.length);
    if (topics.length) refs.funnelBreakdown.append(renderBreakdownGroup("按主题展开", topics, "topic"));
    if (sources.length) refs.funnelBreakdown.append(renderBreakdownGroup("按来源展开", sources, "source"));
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
      const keywordText = topic.keywords?.length ? topic.keywords.join(" · ") : "未设置关键词";
      const exclusions = topic.exclusion_keywords || topic.exclude_keywords || [];
      const keywords = node("div", { className: "data-row__keywords" }, [node("span", { text: keywordText })]);
      if (exclusions.length) keywords.append(node("small", { text: `排除：${exclusions.join(" · ")}` }));
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
    const bankingCategories = new Set(["central_bank", "bank_regulation", "banking", "fintech"]);
    const selectedCategory = refs.sourceCategoryFilter.value;
    const categories = [...new Set(state.sources.map((source) => source.category || "general"))];
    refs.sourceCategoryFilter.replaceChildren(
      node("option", { value: "", text: "全部分类" }),
      node("option", { value: "banking_all", text: "银行与金融 · 全部" }),
      ...categories.map((category) => node("option", { value: category, text: sourceCategoryLabel(category) }))
    );
    refs.sourceCategoryFilter.value = selectedCategory === "banking_all" || categories.includes(selectedCategory) ? selectedCategory : "";
    const category = refs.sourceCategoryFilter.value;
    const query = refs.sourceSearch.value.trim().toLocaleLowerCase();
    const visibleSources = state.sources.filter((source) => {
      const key = source.category || "general";
      const categoryMatches = !category || (category === "banking_all" ? bankingCategories.has(key) : key === category);
      const searchable = `${source.name} ${source.url} ${sourceCategoryLabel(key)} ${source.language || ""}`.toLocaleLowerCase();
      return categoryMatches && (!query || searchable.includes(query));
    });
    refs.sourceFilterCount.textContent = `显示 ${visibleSources.length} / ${state.sources.length} 个订阅 · 已启用 ${state.sources.filter((source) => source.enabled).length} 个`;
    if (!state.sources.length) {
      refs.sourceSheet.append(emptyState("还没有新闻来源", "添加一个 RSS 或 Atom 地址，驾驶舱才知道去哪里找新闻。", "添加来源", () => openSourceDialog()));
      return;
    }
    if (!visibleSources.length) {
      refs.sourceSheet.append(node("p", { className: "helper-copy", text: "没有符合筛选条件的来源，请调整分类或搜索词。" }));
      return;
    }

    visibleSources.forEach((source) => {
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
      const presetNote = source.preset_id ? ` · 内置 v${source.preset_version || 1}${source.user_modified ? " · 已调整" : ""}` : "";
      const titleCopy = node("span", {}, [node("span", { text: source.name }), node("small", { text: `${sourceCategoryLabel(source.category)} · ${source.language || "zh"}${presetNote}` })]);
      const title = node("div", { className: "data-row__title" }, [toggle, titleCopy]);
      const url = node("div", { className: "data-row__url" }, [externalLink(source.url, source.url || "—")]);
      if (source.homepage) url.append(externalLink(source.homepage, "网站首页", "source-home-link"));
      if (source.terms) url.append(externalLink(source.terms, "条款与声明", "source-home-link"));
      const type = node("div", {}, node("span", { className: "type-label", text: source.preset_id ? "预置 RSS" : "RSS / Atom" }));
      const health = sourceHealth(source);
      const badge = node("div", { className: "source-health" }, node("span", { className: "state-badge", text: health.label, "data-state": health.state }));
      if (source.last_error) badge.append(node("small", { className: "source-error", text: source.last_error }));
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

  function sourceCategoryLabel(category) {
    return ({ general: "综合", world: "国际", business: "商业", technology: "科技", science: "科学", policy: "政策", industry: "行业", climate: "气候", central_bank: "央行与货币政策", bank_regulation: "银行监管与合规", banking: "银行业动态", fintech: "支付与金融科技", custom: "其他" })[category] || category || "综合";
  }

  function rowAction(iconName, label, handler, danger = false) {
    return node("button", {
      className: `row-action${danger ? " row-action--danger" : ""}`,
      type: "button",
      onclick: (event) => handler(event.currentTarget)
    }, [icon(iconName), node("span", { className: "button__label", text: label })]);
  }

  function renderDeepSeek() {
    const configured = Boolean(state.deepseek.configured);
    const status = normalizeDeepSeekStatus(state.deepseek);
    const presentation = {
      unconfigured: { label: "未配置", state: "idle", copy: "尚未配置密钥；抓取仍可运行，但只做规则初筛。" },
      saved_unverified: { label: "待测试", state: "running", copy: "密钥已保存在 macOS 钥匙串，建议测试连接。" },
      connected: { label: "连接正常", state: "success", copy: "DeepSeek 连接已验证；密钥保存在 macOS 钥匙串。" },
      error: { label: "连接异常", state: "error", copy: state.deepseek.error || state.deepseek.last_error || "上次测试失败；请检查密钥或网络。" }
    }[status];
    refs.deepseekBadge.textContent = presentation.label;
    refs.deepseekBadge.dataset.state = presentation.state;
    refs.deepseekAction.querySelector(".button__label").textContent = configured ? "管理密钥" : "配置密钥";
    refs.keyStatus.dataset.state = presentation.state;
    refs.keyStatus.replaceChildren();
    const dot = node("span", { className: `status-dot status-dot--${presentation.state}`, "aria-hidden": "true" });
    refs.keyStatus.append(dot, node("span", { text: presentation.copy }));
    refs.deepseekModel.value = state.deepseek.model || "deepseek-v4-flash";
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
      const filtered = Boolean(refs.historyQuery.value.trim() || refs.historyTopic.value || refs.historyFavorite.checked);
      refs.historyList.append(emptyState(
        filtered ? "没有符合筛选的历史内容" : "还没有历史内容",
        filtered ? "尝试清空关键词、更换主题或取消“只看收藏”。" : "完成一次抓取后，报告与收藏会出现在这里。",
        null,
        null
      ));
      return;
    }
    state.history.forEach((article) => refs.historyList.append(articleRow(article, "history")));
  }

  function renderTopicSourceOptions(selectedIds = null) {
    if (!refs.topicSourceOptions) return;
    const chosen = new Set(selectedIds || checkedSourceIds() || []);
    refs.topicSourceOptions.replaceChildren();
    const sources = [...state.sources, ...state.archivedSources.filter((source) => chosen.has(source.id))];
    if (!sources.length) {
      refs.topicSourceOptions.append(node("p", { className: "helper-copy", text: "先添加新闻来源；留空时主题会使用全部启用来源。" }));
      return;
    }
    sources.forEach((source) => {
      const selected = chosen.has(source.id);
      const input = node("input", { type: "checkbox", value: source.id, checked: selected, disabled: state.topicSaving || (!source.enabled && !selected) });
      const label = source.archived ? `${source.name}（已移除，保留绑定）` : source.enabled ? source.name : `${source.name}（已停用）`;
      refs.topicSourceOptions.append(node("label", { className: `check-option${source.enabled ? "" : " is-disabled"}` }, [input, node("span", { text: label })]));
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
    refs.settingFreshness.value = state.settings.freshness_days == null ? "" : String(state.settings.freshness_days);
    const refresh = String(state.settings.refresh_minutes ?? 60);
    if (![...refs.settingRefresh.options].some((option) => option.value === refresh)) {
      refs.settingRefresh.append(node("option", { value: refresh, text: `每 ${refresh} 分钟` }));
    }
    refs.settingRefresh.value = refresh;
    refs.settingExtract.checked = Boolean(state.settings.extract_full_text);
    refs.settingSemantic.checked = state.settings.semantic_fallback_enabled !== false;
    refs.settingSemanticLimit.value = state.settings.semantic_fallback_limit ?? 20;
    syncSemanticControls();
  }

  function syncSemanticControls() {
    refs.settingSemanticLimit.disabled = !refs.settingSemantic.checked;
  }

  function openDialog(dialog, focusTarget = null) {
    if (!dialog || dialog.open) return;
    dialog.showModal();
    (focusTarget || dialog.querySelector("input:not([type=hidden]), button, select, textarea:not([hidden])"))?.focus();
  }

  function closeDialog(dialog, force = false) {
    if (dialog === refs.topicDialog && !force) {
      if (state.topicSaving) return;
      if (topicDraftChanged() && !window.confirm("当前主题有未保存修改，确认放弃这些修改？")) return;
    }
    if (dialog === refs.topicDialog) state.topicDialogVersion += 1;
    if (dialog?.open) dialog.close();
  }

  function topicDraftSnapshot() {
    return JSON.stringify({
      fields: [...refs.topicForm.querySelectorAll("input, select")].map((input) => [input.id, input.value, input.checked]),
      keywords: keywordEditor.snapshot(), excludes: excludeEditor.snapshot()
    });
  }

  function topicDraftChanged() {
    return refs.topicDialog.open && state.topicDraftBaseline !== topicDraftSnapshot();
  }

  function updateTopicDraftStatus() {
    refs.topicDraftStatus.textContent = topicDraftChanged()
      ? "有未保存修改；保存只影响当前主题。"
      : "尚未修改；保存只影响当前主题。";
  }

  function topicSaveError(message) {
    refs.topicSaveError.textContent = message;
    refs.topicSaveError.hidden = !message;
    if (message) refs.topicSaveError.focus();
  }

  function openTopicDialog(topic = null) {
    if (state.topicSaving) return;
    if (refs.topicDialog.open) {
      closeDialog(refs.topicDialog);
      if (refs.topicDialog.open) return;
    }
    state.topicDialogVersion += 1;
    refs.topicForm.reset();
    refs.topicId.value = topic?.id || "";
    refs.topicDialogTitle.textContent = topic ? "编辑主题" : "新建主题";
    refs.topicName.value = topic?.name || "";
    keywordEditor.setValues(topic?.keywords || []);
    excludeEditor.setValues(topic?.exclusion_keywords || topic?.exclude_keywords || []);
    refs.topicThreshold.value = topic?.threshold ?? state.settings.default_threshold ?? 60;
    refs.topicLimit.value = topic?.article_limit ?? state.settings.default_article_limit ?? 20;
    refs.topicThresholdDefault.checked = topic ? topic.threshold === null : true;
    refs.topicLimitDefault.checked = topic ? topic.article_limit === null : true;
    syncTopicDefaultControls();
    refs.topicEnabled.checked = topic ? Boolean(topic.enabled) : true;
    renderTopicSourceOptions(topic?.source_ids || []);
    state.topicSuggestion = null;
    refs.topicSuggestion.hidden = true;
    topicSaveError("");
    const submit = refs.topicForm.querySelector('[type="submit"]');
    submit._taskSequence = (submit._taskSequence || 0) + 1;
    submit.disabled = false;
    setButtonState(submit, null, "保存当前主题");
    state.topicDraftBaseline = topicDraftSnapshot();
    openDialog(refs.topicDialog, refs.topicName);
    updateTopicDraftStatus();
  }

  async function submitTopic(event) {
    event.preventDefault();
    if (state.topicSaving) return;
    topicSaveError("");
    if (!keywordEditor.commitAll() || !excludeEditor.commitAll()) return;
    if (!refs.topicForm.reportValidity()) return;
    const id = refs.topicId.value;
    const name = refs.topicName.value.trim();
    if (!name) {
      topicSaveError("请填写主题名称");
      refs.topicName.focus();
      return;
    }
    const keywords = [...keywordEditor.values];
    if (!keywords.length) {
      keywordEditor.feedback("请至少填写一个包含关键词", "error");
      return;
    }
    if (keywords.length > 40) {
      keywordEditor.feedback("每个主题最多 40 个包含关键词", "error");
      return;
    }
    const exclusionKeywords = [...excludeEditor.values];
    if (exclusionKeywords.length > 40) {
      excludeEditor.feedback("每个主题最多 40 个排除词", "error");
      return;
    }
    const payload = {
      name,
      keywords,
      exclusion_keywords: exclusionKeywords,
      threshold: refs.topicThresholdDefault.checked ? null : Number(refs.topicThreshold.value),
      article_limit: refs.topicLimitDefault.checked ? null : Number(refs.topicLimit.value),
      source_ids: checkedSourceIds(),
      enabled: refs.topicEnabled.checked
    };
    const previousTopic = state.topics.find((topic) => topic.id === id);
    // Avoid re-normalizing untouched legacy terms, e.g. stored compatibility punctuation.
    if (previousTopic && JSON.stringify(keywords) === JSON.stringify(previousTopic.keywords)) delete payload.keywords;
    if (previousTopic && JSON.stringify(exclusionKeywords) === JSON.stringify(previousTopic.exclusion_keywords || previousTopic.exclude_keywords || [])) delete payload.exclusion_keywords;
    if (previousTopic?.source_ids?.length && !payload.source_ids.length &&
        !window.confirm("清空来源会改为使用全部启用来源，而不是停止抓取。确认扩大到全部启用来源？如需暂停，请取消并关闭“启用主题”。")) return;
    const submit = refs.topicForm.querySelector('[type="submit"]');
    state.topicSaving = true;
    refs.topicForm.setAttribute("aria-busy", "true");
    const controls = [...refs.topicForm.querySelectorAll("input, textarea, select, button")];
    const disabledBeforeSave = controls.map((control) => control.disabled);
    controls.forEach((control) => { control.disabled = true; });
    keywordEditor.feedback("正在保存当前主题…", "loading");
    excludeEditor.feedback("正在保存当前主题…", "loading");
    try {
      const legacyPayload = { ...payload };
      delete legacyPayload.exclusion_keywords;
      await runButtonTask(submit, () => apiWithLegacyBody(id ? `/api/topics/${encodeURIComponent(id)}` : "/api/topics", { method: id ? "PUT" : "POST", body: payload }, legacyPayload), {
        loading: "保存中", success: "已保存"
      });
      state.topicDraftBaseline = topicDraftSnapshot();
      closeDialog(refs.topicDialog, true);
      await refreshBootstrap({ quiet: true });
    } catch (error) {
      topicSaveError(`${describeError(error)}。草稿仍保留，请修改后重试。`);
    } finally {
      state.topicSaving = false;
      refs.topicForm.setAttribute("aria-busy", "false");
      controls.forEach((control, index) => { control.disabled = disabledBeforeSave[index]; });
      renderTopicSourceOptions();
      keywordEditor.feedback();
      excludeEditor.feedback();
      updateTopicDraftStatus();
    }
  }

  function splitKeywords(value) {
    const seen = new Set();
    return value.split(/[，,、;；\r\n]/).map((item) => item.trim()).filter((item) => {
      const key = item.toLocaleLowerCase();
      if (!item || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  async function suggestTopicKeywords() {
    if (state.topicSaving || !keywordEditor.commitAll() || !excludeEditor.commitAll()) return;
    if (!state.deepseek.configured) {
      notify("请先配置并测试 DeepSeek API Key", "error");
      openDeepSeekDialog();
      return;
    }
    const name = refs.topicName.value.trim();
    const keywords = [...keywordEditor.values];
    if (!name || !keywords.length) {
      topicSaveError("先填写主题名称和至少一个关键词");
      (name ? keywordEditor.entry : refs.topicName).focus();
      return;
    }
    const body = {
      name,
      keywords,
      exclusion_keywords: [...excludeEditor.values],
      goal: `扩展“${name}”的检索关键词，同时保持主题边界。`
    };
    const dialogVersion = state.topicDialogVersion;
    const draftSnapshot = topicDraftSnapshot();
    try {
      const payload = await runButtonTask(refs.suggestTopic, async () => {
        try {
          return await api("/api/topics/suggest", { method: "POST", body, timeout: 45000 });
        } catch (error) {
          const topicId = refs.topicId.value;
          if (!topicId || ![404, 405].includes(Number(error.status))) throw error;
          return api(`/api/topics/${encodeURIComponent(topicId)}/calibrate`, { method: "POST", body: { goal: body.goal, positive_examples: [], negative_examples: [] }, timeout: 45000 });
        }
      }, { loading: "生成中", success: "建议已生成" });
      if (!refs.topicDialog.open || dialogVersion !== state.topicDialogVersion) return;
      if (draftSnapshot !== topicDraftSnapshot()) {
        topicSaveError("生成期间主题内容已修改，请重新生成建议，避免混入旧主题词。");
        return;
      }
      const proposal = payload?.suggestion || payload?.proposal || payload?.calibration?.proposal || payload || {};
      const suggested = splitKeywords(Array.isArray(proposal.keywords) ? proposal.keywords.join(",") : String(proposal.keywords || ""));
      const newKeywords = suggested.filter((value) => !keywords.some((current) => current.toLocaleLowerCase() === value.toLocaleLowerCase()));
      if (!newKeywords.length) {
        notify("没有发现需要新增的关键词", "info");
        return;
      }
      const availableSlots = Math.max(0, 40 - keywords.length);
      if (!availableSlots) {
        notify("当前主题已有 40 个关键词，请先删除一些再应用建议", "info");
        return;
      }
      state.topicSuggestion = { keywords: newKeywords.slice(0, availableSlots), rationale: proposal.rationale || proposal.reason || "", draftSnapshot };
      refs.topicSuggestionCopy.textContent = state.topicSuggestion.rationale || "以下关键词尚未加入主题，请确认。";
      refs.topicSuggestionChips.replaceChildren(...state.topicSuggestion.keywords.map((value) => node("span", { className: "keyword-chip", text: value })));
      refs.topicSuggestion.hidden = false;
    } catch (error) {
      if (refs.topicDialog.open && dialogVersion === state.topicDialogVersion) topicSaveError(describeError(error));
    }
  }

  function applyTopicSuggestion() {
    if (!state.topicSuggestion?.keywords?.length) return;
    if (state.topicSuggestion.draftSnapshot !== topicDraftSnapshot()) {
      state.topicSuggestion = null;
      refs.topicSuggestion.hidden = true;
      topicSaveError("主题草稿已修改，旧关键词建议不再适用；请重新生成建议。");
      return;
    }
    if (!keywordEditor.merge(state.topicSuggestion.keywords)) return;
    state.topicSuggestion = null;
    refs.topicSuggestion.hidden = true;
    updateTopicDraftStatus();
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
      await loadHistory({ quiet: true, append: false });
      notify(`已移除主题“${topic.name}”`, "success", {
        label: "撤销",
        callback: async () => {
          await api(`/api/topics/${encodeURIComponent(topic.id)}/restore`, { method: "POST" });
          await refreshBootstrap({ quiet: true });
        }
      });
    } catch (error) { notify(describeError(error), "error"); }
  }

  function openSourceDialog(source = null) {
    refs.sourceForm.reset();
    refs.sourceId.value = source?.id || "";
    refs.sourceDialogTitle.textContent = source ? "编辑来源" : "添加来源";
    refs.sourceName.value = source?.name || "";
    refs.sourceHomepage.value = source?.homepage || "";
    refs.sourceUrl.value = source?.url || "";
    // Imported/custom values are valid API data, even when absent from this
    // version's built-in choices. Preserve them when editing unrelated fields.
    for (const [select, value] of [[refs.sourceCategory, source?.category || "general"], [refs.sourceLanguage, source?.language || "zh"]]) {
      select.querySelectorAll("option[data-existing-value]").forEach((option) => option.remove());
      if (![...select.options].some((option) => option.value === value)) {
        select.append(node("option", { value, text: `保留原值：${value}`, "data-existing-value": "true" }));
      }
      select.value = value;
    }
    refs.sourcePreset.value = source?.preset_id
      ? `内置 v${source.preset_version || 1}${source.user_modified ? " · 已调整" : ""}`
      : "自定义";
    refs.sourceTerms.value = source?.terms || "";
    refs.sourceEnabled.checked = source ? Boolean(source.enabled) : true;
    refs.sourceValidation.hidden = true;
    syncSourceDraftValidation();
    refs.validateSourceDraft.querySelector(".button__label").textContent = "验证草稿地址";
    openDialog(refs.sourceDialog, refs.sourceName);
  }

  function sourceDraftPayload() {
    const current = refs.sourceId.value ? state.sources.find((source) => source.id === refs.sourceId.value) : null;
    return {
      name: refs.sourceName.value.trim() || "待验证来源",
      url: refs.sourceUrl.value.trim(),
      homepage: refs.sourceHomepage.value.trim() || null,
      category: refs.sourceCategory.value,
      language: refs.sourceLanguage.value,
      terms: refs.sourceTerms.value.trim() || null,
      preset_id: current?.preset_id || null,
      preset_version: current?.preset_version || null,
      enabled: refs.sourceEnabled.checked
    };
  }

  function syncSourceDraftValidation() {
    refs.validateSourceDraft.disabled = !refs.sourceUrl.value.trim() || !refs.sourceUrl.validity.valid;
  }

  async function submitSource(event) {
    event.preventDefault();
    if (!refs.sourceForm.reportValidity()) return;
    if (!refs.sourceName.value.trim()) {
      notify("请填写来源名称", "error");
      refs.sourceName.focus();
      return;
    }
    const id = refs.sourceId.value;
    const payload = sourceDraftPayload();
    const submit = refs.sourceForm.querySelector('[type="submit"]');
    try {
      const legacyPayload = { name: payload.name, url: payload.url, enabled: payload.enabled };
      await runButtonTask(submit, () => apiWithLegacyBody(id ? `/api/sources/${encodeURIComponent(id)}` : "/api/sources", { method: id ? "PUT" : "POST", body: payload }, legacyPayload), {
        loading: "保存中", success: "已保存"
      });
      closeDialog(refs.sourceDialog);
      await refreshBootstrap({ quiet: true });
    } catch (_) { /* error surfaced by runButtonTask */ }
  }

  async function validateSource(source, button) {
    try {
      const result = await runButtonTask(button, () => api(`/api/sources/${encodeURIComponent(source.id)}/validate`, { method: "POST", timeout: SOURCE_VALIDATION_TIMEOUT }), { loading: "验证中", success: "正常" });
      const sample = result?.sample_title || result?.feed_title || result?.title;
      notify(sample ? `验证成功：${sample}` : `来源验证成功${Number.isFinite(Number(result?.entry_count)) ? ` · ${result.entry_count} 条` : ""}`, "success");
      await refreshBootstrap({ quiet: true });
    } catch (error) {
      if (!button) notify(describeError(error), "error");
      await refreshBootstrap({ quiet: true }).catch(() => {});
    }
  }

  async function validateDraftSource() {
    if (!refs.sourceUrl.value.trim() || !refs.sourceUrl.validity.valid) {
      refs.sourceUrl.reportValidity();
      return;
    }
    const id = refs.sourceId.value;
    const draft = sourceDraftPayload();
    refs.sourceValidation.hidden = false;
    refs.sourceValidation.dataset.state = "running";
    refs.sourceValidation.textContent = "正在读取订阅地址…";
    try {
      const result = await runButtonTask(refs.validateSourceDraft, async () => {
        try {
          return await api("/api/sources/validate", { method: "POST", body: draft, timeout: SOURCE_VALIDATION_TIMEOUT });
        } catch (error) {
          if (!id || ![404, 405].includes(Number(error.status))) throw error;
          return api(`/api/sources/${encodeURIComponent(id)}/validate`, { method: "POST", timeout: SOURCE_VALIDATION_TIMEOUT });
        }
      }, { loading: "验证中", success: "验证通过" });
      refs.sourceValidation.dataset.state = "success";
      const sample = result?.sample_title || result?.feed_title || result?.title;
      refs.sourceValidation.textContent = sample
        ? `读取成功：${sample}`
        : `读取成功${Number.isFinite(Number(result?.entry_count)) ? ` · ${result.entry_count} 条` : ""}，这个来源可以使用。`;
      await refreshBootstrap({ quiet: true });
    } catch (error) {
      refs.sourceValidation.dataset.state = "error";
      refs.sourceValidation.textContent = describeError(error);
    }
  }

  async function deleteSource(source) {
    const affectedTopics = state.topics
      .filter((topic) => (topic.source_ids || []).includes(source.id))
      .map((topic) => ({ id: topic.id, source_ids: [...topic.source_ids] }));
    try {
      await api(`/api/sources/${encodeURIComponent(source.id)}`, { method: "DELETE" });
      state.sources = state.sources.filter((item) => item.id !== source.id);
      state.archivedSources.push({ ...source, archived: true, enabled: false });
      renderAll();
      notify(`已移除来源“${source.name}”`, "success", {
        label: "撤销",
        callback: async () => {
          try {
            await api(`/api/sources/${encodeURIComponent(source.id)}/restore`, { method: "POST" });
          } catch (error) {
            if (!isCompatibilityError(error)) throw error;
            const restoredPayload = {
              name: source.name,
              url: source.url,
              homepage: source.homepage || null,
              category: source.category || "general",
              language: source.language || "zh",
              terms: source.terms || null,
              preset_id: source.preset_id || null,
              enabled: source.enabled !== false
            };
            const legacy = { name: source.name, url: source.url, enabled: source.enabled !== false };
            const response = await apiWithLegacyBody("/api/sources", { method: "POST", body: restoredPayload }, legacy);
            const restoredId = response?.source?.id || source.id;
            await Promise.all(affectedTopics.map((topic) => api(`/api/topics/${encodeURIComponent(topic.id)}`, {
              method: "PUT",
              body: { source_ids: topic.source_ids.map((id) => id === source.id ? restoredId : id) }
            })));
          }
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
    if (!article.id || state.favoritePending.has(article.id)) return;
    state.favoritePending.add(article.id);
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
    } finally {
      state.favoritePending.delete(article.id);
      renderLatest();
      renderHistory();
    }
  }

  function syncFavoriteAcrossViews(articleId, update) {
    const reportArticles = Array.isArray(state.latestReport?.articles) ? state.latestReport.articles : [];
    [...reportArticles, ...state.history].forEach((item) => {
      if (item.id === articleId) Object.assign(item, update);
    });
  }

  async function startRun() {
    if (state.runStarting || ACTIVE_RUN_STATES.has(state.currentRun?.status)) return;
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
    const selectedTopic = state.activeTopicId ? state.topics.find((topic) => topic.id === state.activeTopicId) : null;
    if (selectedTopic && !selectedTopic.enabled) {
      notify("当前主题已停用，请先启用或选择“查看全部主题”", "error");
      return;
    }
    const runTopics = selectedTopic ? [selectedTopic] : enabledTopics;
    const enabledSourceIds = new Set(enabledSources.map((source) => source.id));
    const unusable = runTopics.filter((topic) => (topic.source_ids || []).length && !(topic.source_ids || []).some((id) => enabledSourceIds.has(id)));
    if (unusable.length) {
      notify(`主题“${unusable[0].name}”没有可用来源，请先调整来源绑定`, "error");
      openTopicDialog(unusable[0]);
      return;
    }
    const body = state.activeTopicId ? { topic_ids: [state.activeTopicId] } : {};
    state.runStarting = true;
    renderRunLane();
    try {
      const payload = await api("/api/runs", { method: "POST", body });
      state.currentRun = payload.run || null;
      state.currentSourceErrors = [];
      renderRunLane();
      manageRunPolling();
    } catch (error) {
      if (!error.cockpitNotified) notify(describeError(error), "error");
    } finally {
      state.runStarting = false;
      renderRunLane();
    }
  }

  async function cancelRun() {
    const run = state.currentRun;
    if (!run?.id || !ACTIVE_RUN_STATES.has(run.status) || state.runCancelling) return;
    state.runCancelling = true;
    renderRunLane();
    try {
      const payload = await api(`/api/runs/${encodeURIComponent(run.id)}/cancel`, { method: "POST", body: {} });
      state.currentRun = payload.run || { ...run, status: "cancelled", outcome: "cancelled", phase: "cancelled" };
      notify("任务已取消，上一份有效报告继续保留", "info");
      await refreshBootstrap({ quiet: true });
    } catch (error) {
      notify(describeError(error), "error");
    } finally {
      state.runCancelling = false;
      renderRunLane();
      manageRunPolling();
    }
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
      state.currentSourceErrors = Array.isArray(payload.source_errors) ? payload.source_errors : [];
      renderRunLane();
      renderSourceErrors(state.latestReport);
      if (FINAL_RUN_STATES.has(state.currentRun?.status)) {
        await refreshBootstrap({ quiet: true });
        if (state.currentRun.status !== previous) {
          const duplicateOnly = state.currentRun.status === "complete"
            && Number(state.currentRun.article_count || 0) === 0
            && (duplicateCount(state.currentRun) || 0) > 0;
          notify(
            duplicateOnly
              ? "重复审核完成：近 7 天内没有新增新闻"
              : state.currentRun.status === "complete"
                ? "本次报告已完成"
                : state.currentRun.status === "cancelled"
                  ? "本次任务已取消，旧报告已保留"
                : state.currentRun.status === "degraded"
                  ? "本次抓取部分完成，旧报告保护已生效"
                  : "本次运行失败，旧报告已保留",
            duplicateOnly || state.currentRun.status === "cancelled" ? "info" : state.currentRun.status === "complete" ? "success" : "error"
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
      freshness_days: refs.settingFreshness.value === "" ? null : Number(refs.settingFreshness.value),
      refresh_minutes: Number(refs.settingRefresh.value),
      extract_full_text: refs.settingExtract.checked,
      semantic_fallback_enabled: refs.settingSemantic.checked,
      semantic_fallback_limit: Number(refs.settingSemanticLimit.value)
    };
    const legacyPayload = { ...payload };
    delete legacyPayload.freshness_days;
    delete legacyPayload.semantic_fallback_enabled;
    delete legacyPayload.semantic_fallback_limit;
    const submit = refs.settingsForm.querySelector('[type="submit"]');
    try {
      const response = await runButtonTask(submit, () => apiWithLegacyBody("/api/settings", { method: "PUT", body: payload }, legacyPayload), { loading: "保存中", success: "已保存" });
      state.settings = { ...state.settings, ...(response.settings || payload) };
      closeDialog(refs.settingsDialog);
      renderMetrics();
      renderRunLane();
    } catch (_) { /* surfaced */ }
  }

  function openDeepSeekDialog() {
    refs.deepseekKey.value = "";
    refs.deepseekKey.type = "password";
    refs.toggleKey.setAttribute("aria-label", "显示密钥");
    refs.deepseekTestResult.hidden = true;
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
      state.deepseek.status = normalizeDeepSeekStatus({ ...state.deepseek, ...payload, status: payload.status || "saved_unverified" });
      refs.deepseekKey.value = "";
      closeDialog(refs.deepseekDialog);
      renderDeepSeek();
    } catch (_) { /* surfaced */ }
  }

  async function deleteDeepSeekKey() {
    try {
      const payload = await runButtonTask(refs.deleteKey, () => api("/api/settings/deepseek-key", { method: "DELETE" }), { loading: "删除中", success: "已删除", disabledAfter: true });
      state.deepseek = { ...state.deepseek, ...payload };
      state.deepseek.status = "unconfigured";
      refs.deepseekTestResult.hidden = true;
      renderDeepSeek();
    } catch (_) { /* surfaced */ }
  }

  async function testDeepSeek(button = refs.testKey, keyInput = refs.deepseekKey, resultBox = refs.deepseekTestResult) {
    const key = keyInput?.value?.trim() || "";
    if (!key && !state.deepseek.configured) {
      notify("请先输入 DeepSeek API Key", "error");
      keyInput?.focus();
      return false;
    }
    resultBox.hidden = false;
    resultBox.dataset.state = "running";
    resultBox.textContent = "正在测试 DeepSeek 连接…";
    try {
      const body = key ? { api_key: key } : {};
      const payload = await runButtonTask(button, async () => {
        const result = await api("/api/settings/deepseek/test", { method: "POST", body, timeout: 45000 });
        if (result?.status === "error" || result?.ok === false) {
          const failure = new Error(result.error || "DeepSeek 连接测试失败");
          failure.detail = result.error || failure.message;
          failure.payload = result;
          throw failure;
        }
        return result;
      }, { loading: "测试中", success: "连接正常" });
      state.deepseek = { ...state.deepseek, ...payload, configured: payload.configured ?? state.deepseek.configured, status: payload.status || "connected", error: null };
      resultBox.dataset.state = "success";
      resultBox.textContent = `连接正常 · ${payload.model || "deepseek-v4-flash"}${key ? " · 密钥已安全保存" : ""}`;
      if (keyInput) keyInput.value = "";
      renderDeepSeek();
      return true;
    } catch (error) {
      const failurePayload = error.payload || {};
      if (key) {
        const persistedStatus = failurePayload.persisted_status;
        if (["unconfigured", "saved_unverified", "connected", "error"].includes(persistedStatus)) {
          state.deepseek = {
            ...state.deepseek,
            configured: failurePayload.configured ?? state.deepseek.configured,
            status: persistedStatus,
            error: persistedStatus === "error" ? failurePayload.persisted_error || state.deepseek.error : null
          };
        }
      } else {
        state.deepseek = {
          ...state.deepseek,
          configured: failurePayload.configured ?? state.deepseek.configured,
          model: failurePayload.model || state.deepseek.model,
          last_tested_at: failurePayload.last_tested_at ?? state.deepseek.last_tested_at,
          status: "error",
          error: describeError(error)
        };
      }
      resultBox.dataset.state = "error";
      resultBox.textContent = describeError(error);
      renderDeepSeek();
      return false;
    }
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

  function chooseImportFile() {
    if (ACTIVE_RUN_STATES.has(state.currentRun?.status) || state.runStarting) {
      notify("新闻任务运行中，请完成或取消后再导入", "error");
      return;
    }
    refs.importFile.value = "";
    refs.importFile.click();
  }

  async function previewImport() {
    const file = refs.importFile.files?.[0];
    if (!file) return;
    if (!file.name.toLocaleLowerCase().endsWith(".zip")) {
      notify("请选择 Cookies News Cockpit 导出的 ZIP 备份", "error");
      return;
    }
    if (file.size > 100 * 1024 * 1024) {
      notify("备份文件不能超过 100 MiB", "error");
      return;
    }
    try {
      const payload = await runButtonTask(refs.importReport, () => api("/api/import/preview", {
        method: "POST",
        body: file,
        headers: {
          "Content-Type": file.type || "application/octet-stream",
          "X-Import-Filename": encodeURIComponent(file.name)
        },
        timeout: 60000
      }), { loading: "检查中", success: "可以导入" });
      state.importPreview = payload;
      refs.importPortableSettings.checked = false;
      renderImportPreview();
      openDialog(refs.importDialog, refs.applyImport);
    } catch (_) { /* surfaced by runButtonTask */ }
  }

  function renderImportPreview() {
    const preview = state.importPreview || {};
    const summary = preview.summary || preview.counts || {};
    const rows = [
      ["新增主题", summary.topics_new ?? summary.topics ?? 0],
      ["新增来源", summary.sources_new ?? summary.sources ?? 0],
      ["新增历史", summary.articles_new ?? summary.articles ?? 0],
      ["新增任务记录", summary.runs_new ?? summary.runs ?? 0],
      ["冲突项", summary.conflicts ?? preview.conflicts?.length ?? 0]
    ];
    const list = node("dl", { className: "import-summary" });
    rows.forEach(([term, value]) => list.append(node("div", {}, [node("dt", { text: term }), node("dd", { text: String(value) })])));
    refs.importPreview.replaceChildren(list);
    const conflicts = Array.isArray(preview.conflicts) ? preview.conflicts : [];
    if (conflicts.length) {
      refs.importPreview.append(node("strong", { text: "冲突处理" }));
      const conflictList = node("ul");
      conflicts.slice(0, 20).forEach((item) => conflictList.append(node("li", { text: `${item.name || item.kind || "未命名项"}：${item.resolution || "保留本机版本"}` })));
      refs.importPreview.append(conflictList);
    }
    const warnings = Array.isArray(preview.warnings) ? preview.warnings : [];
    warnings.forEach((warning) => refs.importPreview.append(node("p", { className: "import-warning", text: warning })));
    refs.applyImport.disabled = !(preview.import_id || preview.id) || preview.valid === false;
  }

  async function applyImport() {
    const previewId = state.importPreview?.import_id || state.importPreview?.id;
    if (!previewId) return;
    if (ACTIVE_RUN_STATES.has(state.currentRun?.status) || state.runStarting) {
      notify("新闻任务运行中，请完成或取消后再导入", "error");
      return;
    }
    try {
      const result = await runButtonTask(refs.applyImport, () => api(`/api/import/${encodeURIComponent(previewId)}/apply`, {
        method: "POST",
        body: {
          strategy: "merge_keep_local",
          import_portable_settings: refs.importPortableSettings.checked
        },
        timeout: 60000
      }), { loading: "合并中", success: "导入完成" });
      closeDialog(refs.importDialog);
      state.importPreview = null;
      await refreshBootstrap({ quiet: true });
      notify(
        result?.warning || `备份已安全合并；冲突项保留本机版本${result?.automatic_backup ? `，导入前备份：${result.automatic_backup}` : ""}`,
        result?.warning ? "info" : "success"
      );
    } catch (_) { /* surfaced */ }
  }

  async function shutdownApp() {
    try {
      await api("/api/shutdown", { method: "POST" });
      notify("应用正在安全退出", "success");
      refs.shutdownState.hidden = false;
      refs.shutdownCopy.textContent = "正在尝试自动关闭页面…";
      window.setTimeout(() => {
        window.close();
        window.setTimeout(() => {
          refs.shutdownCopy.textContent = "浏览器没有允许自动关闭。应用服务已经退出，请手动关闭这个标签页。";
        }, 500);
      }, 350);
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
      action: () => {
        state.activeTopicId = topic.id;
        state.reportFilter = "all";
        updateReportFilterButtons();
        renderTopicRail();
        renderLatest();
        renderRunLane();
        document.getElementById("latest").scrollIntoView();
      }
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

  function onboardingWasDismissed() {
    try { return window.localStorage.getItem("cookies-cockpit-onboarding-complete") === "1"; } catch (_) { return false; }
  }

  function maybeOpenOnboarding(payload = {}) {
    if (state.onboardingPresented || refs.onboardingDialog.open) return;
    const completed = payload.settings?.onboarding_completed ?? state.settings.onboarding_completed;
    const shouldOpen = completed === false || (completed === undefined && !state.topics.length && onboardingWasDismissed() === false);
    if (!shouldOpen) return;
    state.onboardingPresented = true;
    state.onboardingStep = 0;
    renderWizardSources();
    renderWizard();
    openDialog(refs.onboardingDialog, refs.wizardNext);
  }

  function renderWizardSources() {
    const categories = new Map();
    const available = state.sources.length ? state.sources : state.sourcePresets;
    available.forEach((source) => {
      const key = source.category || "general";
      const current = categories.get(key) || { key, count: 0, enabled: false };
      current.count += 1;
      current.enabled ||= Boolean(source.enabled);
      categories.set(key, current);
    });
    refs.wizardSourceCategories.replaceChildren();
    if (!categories.size) {
      refs.wizardSourceCategories.append(node("p", { className: "helper-copy", text: "当前版本没有内置来源，可跳过并在驾驶舱中添加 RSS。" }));
      return;
    }
    categories.forEach((category) => {
      const input = node("input", { type: "checkbox", value: category.key, checked: category.enabled || category.key === "general" });
      refs.wizardSourceCategories.append(node("label", { className: "check-option" }, [input, node("span", { text: `${sourceCategoryLabel(category.key)} · ${category.count} 个来源` })]));
    });
  }

  function renderWizard() {
    const content = [
      ["先挑选新闻来源", "按分类启用来源，也可以稍后在驾驶舱里调整。"],
      ["设置第一个主题", "用一个短名称和一组关键词划出新闻边界。"],
      ["连接 DeepSeek", "测试成功后再运行；也可以跳过并使用规则初筛。"],
      ["运行第一份报告", "设置已经就绪，开始后可在驾驶舱查看真实进度。"]
    ][state.onboardingStep];
    refs.onboardingStepLabel.textContent = `第 ${state.onboardingStep + 1} 步，共 4 步`;
    refs.onboardingTitle.textContent = content[0];
    refs.onboardingCopy.textContent = content[1];
    document.querySelectorAll("[data-wizard-step]").forEach((step) => { step.hidden = Number(step.dataset.wizardStep) !== state.onboardingStep; });
    document.querySelectorAll("[data-wizard-marker]").forEach((marker) => {
      const position = Number(marker.dataset.wizardMarker);
      marker.classList.toggle("is-active", position <= state.onboardingStep);
      if (position === state.onboardingStep) marker.setAttribute("aria-current", "step");
      else marker.removeAttribute("aria-current");
    });
    refs.wizardBack.disabled = state.onboardingBusy || state.onboardingStep === 0;
    refs.wizardSkipStep.disabled = state.onboardingBusy;
    refs.skipOnboarding.disabled = state.onboardingBusy;
    refs.wizardTestKey.disabled = state.onboardingBusy;
    delete refs.wizardNext.dataset.state;
    refs.wizardNext.disabled = state.onboardingBusy;
    refs.wizardNext.querySelector(".button__label").textContent = state.onboardingStep === 3 ? "开始运行" : "下一步";
    refs.wizardSkipStep.querySelector(".button__label").textContent = state.onboardingStep === 3 ? "稍后运行" : "跳过此步";
  }

  async function saveWizardSourceChoices() {
    const options = [...refs.wizardSourceCategories.querySelectorAll('input[type="checkbox"]')];
    const selected = new Set(options.filter((input) => input.checked).map((input) => input.value));
    const presets = state.sources.filter((source) => source.preset_id);
    const changes = presets.filter((source) => selected.has(source.category || "general") !== Boolean(source.enabled));
    await Promise.all(changes.map((source) => api(`/api/sources/${encodeURIComponent(source.id)}`, {
      method: "PUT", body: { enabled: selected.has(source.category || "general") }
    })));
    return true;
  }

  async function saveWizardTopic() {
    const name = refs.wizardTopicName.value.trim();
    const keywords = splitKeywords(refs.wizardTopicKeywords.value);
    if (!name || !keywords.length) {
      notify("请填写主题名称和至少一个关键词，或选择跳过", "error");
      (name ? refs.wizardTopicKeywords : refs.wizardTopicName).focus();
      return false;
    }
    const body = { name, keywords: keywords.slice(0, 40), exclusion_keywords: [], source_ids: [], threshold: null, article_limit: null, enabled: true };
    const legacy = { ...body };
    delete legacy.exclusion_keywords;
    await apiWithLegacyBody("/api/topics", { method: "POST", body }, legacy);
    return true;
  }

  async function saveAndTestWizardKey() {
    const key = refs.wizardDeepseekKey.value.trim();
    if (!key) return true;
    return testDeepSeek(refs.wizardTestKey, refs.wizardDeepseekKey, refs.wizardKeyResult);
  }

  async function advanceWizard({ skip = false } = {}) {
    if (state.onboardingBusy) return;
    state.onboardingBusy = true;
    renderWizard();
    try {
      if (state.onboardingStep === 3) {
        await completeOnboarding();
        closeDialog(refs.onboardingDialog);
        if (!skip) await startRun();
        return;
      }
      if (!skip && state.onboardingStep === 0) {
        const options = [...refs.wizardSourceCategories.querySelectorAll('input[type="checkbox"]')];
        if (options.length && !options.some((input) => input.checked)) {
          notify("请至少选择一个来源分类，或跳过此步保留默认来源", "error");
          options[0].focus();
          return;
        }
        await runButtonTask(refs.wizardNext, saveWizardSourceChoices, { loading: "保存中", success: "已选择" });
      }
      if (!skip && state.onboardingStep === 1) {
        const saved = await runButtonTask(refs.wizardNext, saveWizardTopic, { loading: "保存中", success: "已保存" });
        if (!saved) return;
      }
      if (!skip && state.onboardingStep === 2) {
        const connected = await saveAndTestWizardKey();
        if (!connected) return;
      }
      state.onboardingStep += 1;
      await refreshBootstrap({ quiet: true });
      renderWizard();
    } catch (error) {
      if (!error.cockpitNotified) notify(describeError(error), "error");
    } finally {
      state.onboardingBusy = false;
      if (refs.onboardingDialog.open) renderWizard();
    }
  }

  async function completeOnboarding() {
    try { window.localStorage.setItem("cookies-cockpit-onboarding-complete", "1"); } catch (_) { /* storage may be unavailable */ }
    state.settings.onboarding_completed = true;
    try {
      await api("/api/settings", { method: "PUT", body: { onboarding_completed: true } });
    } catch (error) {
      if (!isCompatibilityError(error)) throw error;
    }
  }

  async function skipOnboarding() {
    if (state.onboardingBusy) return;
    state.onboardingBusy = true;
    renderWizard();
    try {
      await completeOnboarding();
    } catch (error) {
      notify(describeError(error), "error");
    } finally {
      state.onboardingBusy = false;
      closeDialog(refs.onboardingDialog);
    }
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
    refs.sourceSearch.addEventListener("input", renderSourceSheet);
    refs.sourceCategoryFilter.addEventListener("change", renderSourceSheet);
    refs.openDeepseek.addEventListener("click", openDeepSeekDialog);
    refs.deepseekAction.addEventListener("click", openDeepSeekDialog);
    refs.exportReport.addEventListener("click", exportData);
    refs.importReport.addEventListener("click", chooseImportFile);
    refs.importFile.addEventListener("change", previewImport);
    refs.applyImport.addEventListener("click", applyImport);
    refs.shutdownApp.addEventListener("click", shutdownApp);
    refs.startRun.addEventListener("click", startRun);
    refs.cancelRun.addEventListener("click", cancelRun);
    refs.showAllTopics.addEventListener("click", () => {
      state.activeTopicId = null;
      renderTopicRail();
      renderLatest();
      renderRunLane();
    });
    document.querySelectorAll("[data-report-filter]").forEach((button) => button.addEventListener("click", () => {
      state.reportFilter = button.dataset.reportFilter;
      updateReportFilterButtons();
      renderLatest();
    }));

    refs.topicForm.addEventListener("submit", submitTopic);
    refs.topicForm.addEventListener("input", updateTopicDraftStatus);
    refs.topicForm.addEventListener("change", updateTopicDraftStatus);
    refs.topicForm.addEventListener("keyword-change", updateTopicDraftStatus);
    refs.topicDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      if (!keywordEditor.composing && !excludeEditor.composing) closeDialog(refs.topicDialog);
    });
    window.addEventListener("beforeunload", (event) => {
      if (topicDraftChanged()) { event.preventDefault(); event.returnValue = ""; }
    });
    refs.suggestTopic.addEventListener("click", suggestTopicKeywords);
    refs.applyTopicSuggestion.addEventListener("click", applyTopicSuggestion);
    refs.topicThresholdDefault.addEventListener("change", syncTopicDefaultControls);
    refs.topicLimitDefault.addEventListener("change", syncTopicDefaultControls);
    refs.sourceForm.addEventListener("submit", submitSource);
    refs.sourceUrl.addEventListener("input", syncSourceDraftValidation);
    refs.settingsForm.addEventListener("submit", saveSettings);
    refs.settingSemantic.addEventListener("change", syncSemanticControls);
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
    refs.testKey.addEventListener("click", () => testDeepSeek());
    refs.toggleKey.addEventListener("click", () => {
      refs.deepseekKey.type = refs.deepseekKey.type === "password" ? "text" : "password";
      refs.toggleKey.setAttribute("aria-label", refs.deepseekKey.type === "password" ? "显示密钥" : "隐藏密钥");
    });
    refs.skipOnboarding.addEventListener("click", skipOnboarding);
    refs.wizardSkipStep.addEventListener("click", () => advanceWizard({ skip: true }));
    refs.wizardNext.addEventListener("click", () => advanceWizard());
    refs.wizardBack.addEventListener("click", () => {
      if (state.onboardingStep > 0) state.onboardingStep -= 1;
      renderWizard();
    });
    refs.wizardTestKey.addEventListener("click", async () => {
      if (!refs.wizardDeepseekKey.value.trim()) {
        notify("请先输入 DeepSeek API Key", "error");
        refs.wizardDeepseekKey.focus();
        return;
      }
      if (state.onboardingBusy) return;
      state.onboardingBusy = true;
      renderWizard();
      try {
        await saveAndTestWizardKey();
      } finally {
        state.onboardingBusy = false;
        if (refs.onboardingDialog.open) renderWizard();
      }
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
      state.currentSourceErrors = Array.isArray(currentPayload.source_errors) ? currentPayload.source_errors : [];
      const changed = previousId !== state.currentRun?.id || previousStatus !== state.currentRun?.status;
      renderRunLane();
      if (changed && FINAL_RUN_STATES.has(state.currentRun?.status)) {
        await refreshBootstrap({ quiet: true });
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
    keywordEditor = new window.CockpitKeywordEditor({ prefix: "topic-keyword", canonicalId: "topic-keywords", label: "关键词" });
    excludeEditor = new window.CockpitKeywordEditor({ prefix: "topic-exclude", canonicalId: "topic-excludes", label: "排除词" });
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
