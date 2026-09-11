/* Isolated browser acceptance test for Cookies News Cockpit. */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const projectRoot = path.resolve(__dirname, "..");
const playwrightModule = process.env.CODEX_PLAYWRIGHT_ROOT || "playwright";
const { chromium } = require(playwrightModule);
const outputDir = path.resolve(process.env.E2E_QA_OUTPUT || path.join(projectRoot, "artifacts", "e2e-qa"));
const sessionToken = `e2e-${process.pid}-${Date.now()}`;
const report = { status: "running", checks: [], externalRequests: [], requestFailures: [], consoleErrors: [], pageErrors: [] };

function check(name, detail = "pass") {
  report.checks.push({ name, status: "pass", detail });
}

function fail(name, error) {
  report.checks.push({ name, status: "fail", detail: error.message });
}

function pythonExecutable() {
  if (process.env.E2E_PYTHON) return process.env.E2E_PYTHON;
  const candidates = process.platform === "win32"
    ? [path.join(projectRoot, ".venv", "Scripts", "python.exe"), "python"]
    : [path.join(projectRoot, ".venv", "bin", "python"), "python3", "python"];
  return candidates.find((candidate) => candidate === "python" || candidate === "python3" || fs.existsSync(candidate));
}

function browserOptions() {
  const configured = process.env.CHROME_PATH;
  const windowsChrome = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
  const executablePath = configured || (fs.existsSync(windowsChrome) ? windowsChrome : undefined);
  return executablePath ? { executablePath, headless: true } : { headless: true };
}

async function openPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close((error) => (error ? reject(error) : resolve(port)));
    });
  });
}

function probe(url) {
  return new Promise((resolve) => {
    const request = http.get(url, { headers: { "X-Cockpit-Token": sessionToken } }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.once("error", () => resolve(false));
    request.setTimeout(500, () => {
      request.destroy();
      resolve(false);
    });
  });
}

async function waitForServer(url, child) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Fixture server exited with code ${child.exitCode}`);
    if (await probe(`${url}/api/bootstrap`)) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Fixture server did not become ready within 15 seconds");
}

async function stopFixtureServer(baseUrl, server) {
  if (!server || server.exitCode !== null) return;
  // A Windows venv launcher may be a parent of the Python service process.
  // Request ASGI shutdown first so ownership locks and the entire tree close.
  await new Promise((resolve) => {
    const request = http.request(`${baseUrl}/api/e2e/shutdown`, {
      method: "POST", headers: { "X-Cockpit-Token": sessionToken },
    }, (response) => { response.resume(); response.once("end", resolve); });
    request.once("error", resolve);
    request.setTimeout(2000, () => { request.destroy(); resolve(); });
    request.end();
  });
  if (server.exitCode === null) {
    await new Promise((resolve) => {
      const timeout = setTimeout(resolve, 10000);
      server.once("exit", () => { clearTimeout(timeout); resolve(); });
    });
  }
  if (server.exitCode === null) {
    server.kill();
    report.cleanupWarning = "Fixture did not exit gracefully; launcher termination was required";
  }
}

async function apiJson(page, endpoint) {
  return page.evaluate(async ({ endpoint, token }) => {
    const response = await fetch(endpoint, { headers: { "X-Cockpit-Token": token } });
    return { status: response.status, body: await response.json() };
  }, { endpoint, token: sessionToken });
}

async function apiRequest(page, method, endpoint, body) {
  const result = await page.evaluate(async ({ endpoint, method, body, token }) => {
    const response = await fetch(endpoint, {
      method,
      headers: { "X-Cockpit-Token": token, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: response.status, body: await response.json() };
  }, { endpoint, method, body, token: sessionToken });
  assert.ok(result.status >= 200 && result.status < 300, `${method} ${endpoint}: ${JSON.stringify(result)}`);
  return result.body;
}

async function waitForFinishedRun(page, previousId = null) {
  await page.waitForFunction(async ({ previousId, token }) => {
    const response = await fetch("/api/runs/current", { headers: { "X-Cockpit-Token": token } });
    const { run } = await response.json();
    return run && run.id !== previousId && ["complete", "degraded", "failed", "cancelled"].includes(run.status);
  }, { previousId, token: sessionToken }, { timeout: 15000 });
  return (await apiJson(page, "/api/runs/current")).body.run;
}

async function runCheck(name, action) {
  try {
    const detail = await action();
    check(name, detail || "pass");
  } catch (error) {
    fail(name, error);
    throw error;
  }
}

async function main() {
  fs.mkdirSync(outputDir, { recursive: true });
  const dataHome = fs.mkdtempSync(path.join(os.tmpdir(), "cookies-news-cockpit-e2e-"));
  const port = await openPort();
  const baseUrl = `http://127.0.0.1:${port}`;
  const serverOutput = [];
  let server;
  let browser;
  try {
    server = spawn(
      pythonExecutable(),
      [path.join(projectRoot, "scripts", "e2e_fixture_server.py"), "--home", dataHome, "--port", String(port), "--token", sessionToken],
      { cwd: projectRoot, stdio: ["ignore", "pipe", "pipe"] },
    );
    server.stdout.on("data", (chunk) => serverOutput.push(chunk.toString()));
    server.stderr.on("data", (chunk) => serverOutput.push(chunk.toString()));
    await waitForServer(baseUrl, server);
    browser = await chromium.launch(browserOptions());
    const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    page.on("console", (message) => {
      if (message.type() === "error") report.consoleErrors.push(message.text());
    });
    page.on("pageerror", (error) => report.pageErrors.push(error.message));
    page.on("request", (request) => {
      const hostname = new URL(request.url()).hostname;
      if (!["127.0.0.1", "localhost"].includes(hostname)) report.externalRequests.push(request.url());
    });
    page.on("response", (response) => {
      if (response.status() >= 400) report.requestFailures.push({ url: response.url(), status: response.status() });
    });

    await runCheck("会话令牌清除并连接本地服务", async () => {
      await page.goto(`${baseUrl}/#token=${encodeURIComponent(sessionToken)}`, { waitUntil: "networkidle" });
      try {
        await page.waitForFunction(() => document.body.dataset.connection === "online");
      } catch (error) {
        const state = await page.evaluate(() => ({
          connection: document.body.dataset.connection || null,
          readyState: document.readyState,
          tokenStored: Boolean(sessionStorage.getItem("cookies-cockpit-token")),
          offlineCopy: document.querySelector("#offline-banner")?.textContent?.replace(/\s+/g, " ").trim() || null
        }));
        throw new Error(`${error.message}; browser state=${JSON.stringify(state)}; pageErrors=${JSON.stringify(report.pageErrors)}`);
      }
      assert.equal(new URL(page.url()).hash, "");
      assert.equal(new URL(page.url()).search, "");
      assert.equal(await page.evaluate(() => sessionStorage.getItem("cookies-cockpit-token")), sessionToken);
      assert.match(await page.locator("#service-label").textContent(), /保存在本机/);
      return "URL 中 token 已清除，sessionStorage 会话及本地连接正常";
    });

    const suffix = `${process.pid}-${Date.now()}`;
    const onboardingTopic = `首次向导 ${suffix}`;
    await runCheck("首次向导完成来源、主题与稍后运行", async () => {
      const dialog = page.locator("#onboarding-dialog");
      await dialog.waitFor({ state: "visible" });
      assert.match(await page.locator("#onboarding-step-label").textContent(), /第 1 步，共 4 步/);
      assert.ok(await page.locator("#wizard-source-categories input:checked").count() >= 1);
      await page.locator("#wizard-next").click();
      await page.locator("#wizard-topic-name").fill(onboardingTopic);
      await page.locator("#wizard-topic-keywords").fill("机器人、robotics；具身智能");
      await page.locator("#wizard-next").click();
      await page.waitForFunction(() => document.querySelector("#onboarding-step-label")?.textContent.includes("第 3 步"));
      assert.match(await page.locator("#onboarding-step-label").textContent(), /第 3 步，共 4 步/);
      await page.locator("#wizard-skip-step").click();
      await page.waitForFunction(() => document.querySelector("#onboarding-step-label")?.textContent.includes("第 4 步"));
      assert.match(await page.locator("#onboarding-step-label").textContent(), /第 4 步，共 4 步/);
      await page.locator("#wizard-skip-step").click();
      await dialog.waitFor({ state: "hidden" });
      const settings = await apiJson(page, "/api/settings");
      const topics = await apiJson(page, "/api/topics");
      assert.equal(settings.body.settings.onboarding_completed, true);
      const savedTopic = topics.body.topics.find((item) => item.name === onboardingTopic);
      assert.ok(savedTopic);
      assert.deepEqual(savedTopic.keywords, ["机器人", "robotics", "具身智能"]);
      return "4 步向导可完成，主题和完成状态已写入本机";
    });

    await runCheck("默认重复新闻审核可见", async () => {
      assert.equal((await page.locator("#run-dedupe").textContent()).trim(), "默认 · 近 7 天");
      return "本次任务显示“默认 · 近 7 天”";
    });

    await runCheck("首次向导四步可跳过", async () => {
      const dialog = page.locator("#onboarding-dialog");
      if (await dialog.evaluate((element) => element.open)) {
        assert.equal(await dialog.locator("[data-wizard-marker]").count(), 4);
        await page.locator("#skip-onboarding").click();
        await dialog.waitFor({ state: "hidden" });
        return "来源→主题→DeepSeek→运行四步齐全，整段跳过可用";
      }
      return "已有设置，向导按约定不重复出现";
    });

    const topicName = `E2E 机器人 ${suffix}`;
    await runCheck("新建主题并保存全局默认 null", async () => {
      await page.locator("#add-topic").click();
      await page.locator("#topic-dialog").waitFor({ state: "visible" });
      assert.equal(await page.locator("#topic-threshold-default").isChecked(), true);
      assert.equal(await page.locator("#topic-limit-default").isChecked(), true);
      assert.equal(await page.locator("#topic-threshold").isDisabled(), true);
      assert.equal(await page.locator("#topic-limit").isDisabled(), true);
      await page.locator("#topic-name").fill(topicName);
      await page.locator("#topic-keywords").fill("机器人, 具身智能, robot");
      assert.equal(await page.locator("#topic-keywords").inputValue(), "机器人, 具身智能, robot");
      const formValidity = await page.locator("#topic-form").evaluate((form) => ({
        valid: form.checkValidity(),
        invalid: [...form.querySelectorAll(":invalid")].map((field) => ({
          id: field.id,
          value: field.value,
          message: field.validationMessage,
        })),
      }));
      assert.equal(formValidity.valid, true, JSON.stringify(formValidity.invalid));
      await page.locator("#topic-form [type=submit]").click();
      try {
        await page.locator("#topic-dialog").waitFor({ state: "hidden", timeout: 7000 });
      } catch (error) {
        const diagnostics = await page.evaluate(() => ({
          formValid: document.querySelector("#topic-form").checkValidity(),
          toast: document.querySelector("#toast-region").textContent.trim(),
          submitText: document.querySelector("#topic-form [type=submit]").textContent.trim(),
          submitDisabled: document.querySelector("#topic-form [type=submit]").disabled,
        }));
        throw new Error(`Topic create dialog stayed open: ${JSON.stringify(diagnostics)}; requests=${JSON.stringify(report.requestFailures)}; console=${JSON.stringify(report.consoleErrors)}`, { cause: error });
      }
      await page.locator("#topic-sheet").getByText(topicName, { exact: true }).waitFor();
      const payload = await apiJson(page, "/api/topics");
      assert.equal(payload.status, 200);
      const topic = payload.body.topics.find((item) => item.name === topicName);
      assert.ok(topic);
      assert.equal(topic.threshold, null);
      assert.equal(topic.article_limit, null);
      return "threshold 与 article_limit 均以 null 存储";
    });

    await runCheck("编辑主题自定义值并恢复全局默认", async () => {
      const topicRow = page.locator("#topic-sheet .topic-grid").filter({ hasText: topicName });
      await topicRow.getByRole("button", { name: "编辑" }).click();
      await page.locator("#topic-threshold-default").uncheck();
      await page.locator("#topic-limit-default").uncheck();
      await page.locator("#topic-threshold").fill("83");
      await page.locator("#topic-limit").fill("7");
      await page.locator("#topic-form [type=submit]").click();
      await page.locator("#topic-dialog").waitFor({ state: "hidden" });
      let payload = await apiJson(page, "/api/topics");
      let topic = payload.body.topics.find((item) => item.name === topicName);
      assert.equal(topic.threshold, 83);
      assert.equal(topic.article_limit, 7);

      await topicRow.getByRole("button", { name: "编辑" }).click();
      await page.locator("#topic-threshold-default").check();
      await page.locator("#topic-limit-default").check();
      assert.equal(await page.locator("#topic-threshold").isDisabled(), true);
      assert.equal(await page.locator("#topic-limit").isDisabled(), true);
      await page.locator("#topic-form [type=submit]").click();
      await page.locator("#topic-dialog").waitFor({ state: "hidden" });
      payload = await apiJson(page, "/api/topics");
      topic = payload.body.topics.find((item) => item.name === topicName);
      assert.equal(topic.threshold, null);
      assert.equal(topic.article_limit, null);
      return "83/7 可保存，重新勾选后 null 往返正常";
    });

    const sourceName = `E2E 来源 ${suffix}`;
    await runCheck("添加并保持来源为停用", async () => {
      await page.locator("#add-source").click();
      await page.locator("#source-name").fill(sourceName);
      await page.locator("#source-url").fill(`https://example.com/${suffix}.xml`);
      await page.locator("#source-enabled + .switch-ui").click();
      assert.equal(await page.locator("#source-enabled").isChecked(), false);
      await page.locator("#source-form [type=submit]").click();
      await page.locator("#source-dialog").waitFor({ state: "hidden" });
      const row = page.locator("#source-sheet .source-grid").filter({ hasText: sourceName });
      await row.waitFor();
      assert.equal(await row.locator('[role="switch"]').getAttribute("aria-checked"), "false");
      const payload = await apiJson(page, "/api/sources");
      const source = payload.body.sources.find((item) => item.name === sourceName);
      assert.ok(source);
      assert.equal(source.enabled, false);
      return "来源保存后 UI 与 API 均为 disabled";
    });

    await runCheck("银行来源分类与搜索只筛选显示，编辑保留分类且不误启用", async () => {
      const before = (await apiJson(page, "/api/sources")).body.sources;
      const topicsBefore = (await apiJson(page, "/api/topics")).body.topics;
      const bankCategories = new Set(["central_bank", "bank_regulation", "banking", "fintech"]);
      const bankSources = before.filter((source) => bankCategories.has(source.category));
      assert.ok(bankSources.length >= 25);
      await page.locator("#source-category-filter").selectOption("banking_all");
      assert.equal(await page.locator("#source-sheet .source-grid").count(), bankSources.length);
      const target = bankSources.find((source) => source.id === "preset-hkma-press-zh");
      assert.ok(target);
      await page.locator("#source-search").fill(target.name);
      assert.equal(await page.locator("#source-sheet .source-grid").count(), 1);
      const row = page.locator("#source-sheet .source-grid");
      await row.getByRole("button", { name: "编辑", exact: true }).click();
      assert.equal(await page.locator("#source-category").inputValue(), target.category);
      await page.locator("#source-form [type=submit]").click();
      await page.locator("#source-dialog").waitFor({ state: "hidden" });
      assert.equal(await page.locator("#source-sheet .source-grid").count(), 1);
      const after = (await apiJson(page, "/api/sources")).body.sources;
      assert.deepEqual(after.map((source) => [source.id, source.enabled]), before.map((source) => [source.id, source.enabled]));
      assert.equal(after.find((source) => source.id === target.id).category, target.category);
      assert.deepEqual((await apiJson(page, "/api/topics")).body.topics, topicsBefore);
      await page.locator("#source-search").fill("no-such-banking-source-xyz");
      assert.equal(await page.locator("#source-sheet .source-grid").count(), 0);
      assert.match(await page.locator("#source-sheet").textContent(), /没有符合筛选条件/);
      await page.locator("#source-search").fill("");
      await page.locator("#source-category-filter").selectOption("");
      assert.equal(await page.locator("#source-sheet .source-grid").count(), before.length);
      return `${bankSources.length} 个银行金融订阅可按分类和名称筛选，启用状态未改变`;
    });

    await runCheck("来源编辑保留导入的自定义分类与语言", async () => {
      const custom = (await apiRequest(page, "POST", "/api/sources", {
        name: `自定义银行来源 ${suffix}`, url: `https://fixture.invalid/custom-${suffix}.xml`,
        category: "my-credit-research", language: "zh-Hant", enabled: false,
      })).source;
      await page.reload({ waitUntil: "networkidle" });
      const row = page.locator("#source-sheet .source-grid").filter({ hasText: custom.name });
      await row.getByRole("button", { name: "编辑", exact: true }).click();
      assert.equal(await page.locator("#source-category").inputValue(), "my-credit-research");
      assert.equal(await page.locator("#source-language").inputValue(), "zh-Hant");
      await page.locator("#source-name").fill(`${custom.name} 已编辑`);
      await page.locator("#source-form [type=submit]").click();
      await page.locator("#source-dialog").waitFor({ state: "hidden" });
      const saved = (await apiJson(page, "/api/sources")).body.sources.find((source) => source.id === custom.id);
      assert.equal(saved.category, "my-credit-research");
      assert.equal(saved.language, "zh-Hant");
      assert.equal(saved.enabled, false);
      return "仅改名称后 my-credit-research / zh-Hant 及停用状态均保留";
    });

    await runCheck("Ctrl+K 打开并搜索驾驶舱", async () => {
      await page.keyboard.press("Control+K");
      assert.equal(await page.locator("#search-dialog").evaluate((dialog) => dialog.open), true);
      await page.locator("#command-query").fill(topicName);
      await page.locator("#command-results").getByText(topicName, { exact: true }).waitFor();
      await page.keyboard.press("Escape");
      assert.equal(await page.locator("#search-dialog").evaluate((dialog) => dialog.open), false);
      return "快捷键打开、结果过滤和单次 Esc 关闭均正常";
    });

    await runCheck("DeepSeek 隐私与费用披露", async () => {
      await page.locator("#open-deepseek").click();
      const dialog = page.locator("#deepseek-dialog");
      await dialog.waitFor({ state: "visible" });
      const text = (await dialog.textContent()).replace(/\s+/g, " ");
      assert.match(text, /macOS 钥匙串/);
      assert.match(text, /不会写进导出文件/);
      assert.match(text, /新闻标题、来源、摘要/);
      assert.match(text, /正文会发送至 DeepSeek 云端/);
      assert.match(text, /最多分析 500 条候选/);
      assert.match(text, /实际费用以 DeepSeek 账单为准/);
      await dialog.getByRole("button", { name: "取消" }).click();
      return "数据范围、云端传输、500 次技术上限与账单口径均明确";
    });

    await runCheck("1.2 分层报告、三段漏斗与智能补充文案", async () => {
      assert.deepEqual(
        await page.locator("#run-funnel .funnel-group > h3").allTextContents(),
        ["发现", "处理", "结果"],
      );
      assert.equal(await page.locator("#report-context").count(), 1);
      assert.equal(await page.locator("#funnel-breakdown").count(), 1);
      await page.locator("#open-settings").click();
      const dialog = page.locator("#settings-dialog");
      const text = (await dialog.textContent()).replace(/\s+/g, " ");
      assert.match(text, /结果不足时智能补充/);
      assert.match(text, /首批最多 20 条/);
      assert.match(text, /最多追加 15 条/);
      await dialog.getByRole("button", { name: "取消" }).click();
      return "发现/处理/结果三段结构与20+15受控补充说明均可见";
    });

    await runCheck("导入主题 ID 重映射后按唯一主题名恢复报告分层", async () => {
      const probe = await context.newPage();
      try {
        await probe.route("**/app.js", async (route) => {
          const response = await route.fetch();
          const source = await response.text();
          assert.match(source, /  init\(\);/);
          await route.fulfill({
            response,
            body: source.replace(
              "  init();",
              "  window.__tierProbe = { topicFunnelFor, articleTier };\n  init();",
            ),
          });
        });
        await probe.goto(`${baseUrl}/#token=${encodeURIComponent(sessionToken)}`, { waitUntil: "networkidle" });
        const tiers = await probe.evaluate(() => {
          const report = {
            run: {
              funnel: {
                per_topic: {
                  "incoming-topic-id": {
                    name: "ESG",
                    core_threshold: 70,
                    supplement_threshold: 50,
                  },
                },
              },
            },
          };
          const ambiguous = {
            run: {
              funnel: {
                per_topic: {
                  first: { name: "ESG", core_threshold: 70, supplement_threshold: 50 },
                  second: { name: "esg", core_threshold: 70, supplement_threshold: 50 },
                },
              },
            },
          };
          return {
            uniqueSupplement: window.__tierProbe.articleTier(
              { topic_id: "local-topic-id", topic_name: "eSg", score: 60 },
              report,
            ),
            uniqueCore: window.__tierProbe.articleTier(
              { topic_id: "local-topic-id", topic_name: "esg", score: 75 },
              report,
            ),
            ambiguousFallback: window.__tierProbe.articleTier(
              { topic_id: "local-topic-id", topic_name: "ESG", score: 60 },
              ambiguous,
            ),
          };
        });
        assert.deepEqual(tiers, {
          uniqueSupplement: "supplement",
          uniqueCore: "core",
          ambiguousFallback: "core",
        });
      } finally {
        await probe.close();
      }
      return "ID 不同但主题名唯一时恢复阈值；同名歧义时不猜测";
    });

    await runCheck("导出完整备份", async () => {
      const downloadPromise = page.waitForEvent("download");
      await page.locator("#export-report").click();
      const download = await downloadPromise;
      assert.equal(download.suggestedFilename(), "cookies-news-cockpit-export.zip");
      const downloadPath = path.join(outputDir, download.suggestedFilename());
      await download.saveAs(downloadPath);
      assert.ok(fs.statSync(downloadPath).size > 200);
      return `${download.suggestedFilename()} (${fs.statSync(downloadPath).size} bytes)`;
    });

    await runCheck("导入预览显示任务记录并显式恢复便携设置", async () => {
      const backupPath = path.join(outputDir, "cookies-news-cockpit-export.zip");
      await page.locator("#import-file").setInputFiles(backupPath);
      const dialog = page.locator("#import-dialog");
      await dialog.waitFor({ state: "visible" });
      await dialog.getByText("新增任务记录", { exact: true }).waitFor();
      const portable = page.locator("#import-portable-settings");
      assert.equal(await portable.isChecked(), false);
      assert.match((await dialog.textContent()).replace(/\s+/g, " "), /API Key 永远不会从备份导入/);
      await portable.check();
      await page.locator("#apply-import").click();
      await dialog.waitFor({ state: "hidden" });
      return "任务记录单列；便携设置默认关闭，显式勾选后可安全合并";
    });

    const regression = {};
    await runCheck("1.3 编辑其他主题字段保留已停用来源绑定", async () => {
      regression.disabled = (await apiJson(page, "/api/sources")).body.sources.find((item) => item.name === sourceName);
      regression.good = (await apiRequest(page, "POST", "/api/sources", {
        name: `E2E 稳定来源 ${suffix}`, url: `https://fixture.invalid/good-${suffix}.xml`, enabled: true,
      })).source;
      regression.bad = (await apiRequest(page, "POST", "/api/sources", {
        name: `E2E 异常来源 ${suffix}`, url: `https://fixture.invalid/bad-${suffix}.xml`, enabled: true,
      })).source;
      regression.topic = (await apiRequest(page, "POST", "/api/topics", {
        name: `E2E 绑定回归 ${suffix}`, keywords: ["ESG"], threshold: 40, article_limit: 2,
        source_ids: [regression.disabled.id],
      })).topic;
      await apiRequest(page, "PUT", "/api/settings", { scheduler_enabled: false, extract_full_text: false });
      await page.reload({ waitUntil: "networkidle" });
      await page.locator("#topic-sheet .topic-grid").filter({ hasText: regression.topic.name }).getByRole("button", { name: "编辑" }).click();
      const sourceOption = page.locator(`#topic-source-options input[value="${regression.disabled.id}"]`);
      assert.equal(await sourceOption.isChecked(), true);
      assert.equal(await sourceOption.isDisabled(), false);
      assert.match(await sourceOption.locator("..").textContent(), /已停用/);
      regression.topic.name = `E2E 已改名 ${suffix}`;
      await page.locator("#topic-name").fill(regression.topic.name);
      await page.locator("#topic-form [type=submit]").click();
      await page.locator("#topic-dialog").waitFor({ state: "hidden" });
      const saved = (await apiJson(page, "/api/topics")).body.topics.find((item) => item.id === regression.topic.id);
      assert.equal(saved.name, regression.topic.name);
      assert.deepEqual(saved.source_ids, [regression.disabled.id]);
      return "已停用的已选来源仍显示为勾选；仅修改名称后绑定不变";
    });

    await runCheck("1.3 移除最后绑定来源后保留范围，显式改选前不能运行", async () => {
      const row = page.locator("#source-sheet .source-grid").filter({ hasText: regression.disabled.name });
      await row.getByRole("button", { name: "移除" }).click();
      await row.waitFor({ state: "detached" });
      const before = (await apiJson(page, "/api/runs/current")).body.run;
      const callsBefore = (await apiJson(page, "/api/e2e/state")).body.fetch_calls.length;
      const topic = (await apiJson(page, "/api/topics")).body.topics.find((item) => item.id === regression.topic.id);
      assert.deepEqual(topic.source_ids, [regression.disabled.id]);
      await page.locator("#topic-rail-list").getByText(regression.topic.name, { exact: true }).click();
      await page.locator("#start-run").click();
      await page.locator("#topic-dialog").waitFor({ state: "visible" });
      const archivedOption = page.locator(`#topic-source-options input[value="${regression.disabled.id}"]`);
      assert.equal(await archivedOption.isChecked(), true);
      assert.match(await archivedOption.locator("..").textContent(), /已移除.*保留绑定/);
      assert.match(await page.locator("#toast-region").textContent(), /没有可用来源/);
      assert.equal((await apiJson(page, "/api/runs/current")).body.run?.id || null, before?.id || null);
      assert.equal((await apiJson(page, "/api/e2e/state")).body.fetch_calls.length, callsBefore);
      await page.locator("#topic-excludes").fill("广告");
      await page.locator("#topic-form [type=submit]").click();
      await page.locator("#topic-dialog").waitFor({ state: "hidden" });
      const preserved = (await apiJson(page, "/api/topics")).body.topics.find((item) => item.id === regression.topic.id);
      assert.deepEqual(preserved.source_ids, [regression.disabled.id]);
      await page.locator("#topic-sheet .topic-grid").filter({ hasText: regression.topic.name }).getByRole("button", { name: "编辑" }).click();
      await page.locator(`#topic-source-options input[value="${regression.disabled.id}"]`).uncheck();
      const confirmationPromise = page.waitForEvent("dialog");
      const emptyScopeSave = page.locator("#topic-form [type=submit]").click();
      const confirmation = await confirmationPromise;
      assert.equal(confirmation.type(), "confirm");
      assert.match(confirmation.message(), /全部.*来源/);
      await confirmation.dismiss();
      await emptyScopeSave;
      assert.equal(await page.locator("#topic-dialog").evaluate((dialog) => dialog.open), true);
      const afterCancelledScope = (await apiJson(page, "/api/topics")).body.topics.find((item) => item.id === regression.topic.id);
      assert.deepEqual(afterCancelledScope.source_ids, [regression.disabled.id]);
      await page.locator(`#topic-source-options input[value="${regression.good.id}"]`).check();
      await page.locator(`#topic-source-options input[value="${regression.bad.id}"]`).check();
      await page.locator("#topic-form [type=submit]").click();
      await page.locator("#topic-dialog").waitFor({ state: "hidden" });
      const explicit = (await apiJson(page, "/api/topics")).body.topics.find((item) => item.id === regression.topic.id);
      assert.deepEqual(new Set(explicit.source_ids), new Set([regression.good.id, regression.bad.id]));
      return "移除/无关编辑/取消全源确认均保留绑定；运行未发起，用户明确改选后范围才改变";
    });

    const configureFeed = (mode, title) => apiRequest(page, "POST", "/api/e2e/configure", {
      mode, title, good_source_id: regression.good.id, bad_source_id: regression.bad.id,
    });
    await runCheck("1.3 部分来源失败时首页展示本次可用新文章", async () => {
      regression.oldTitle = "E2E ESG 企业绿色投资增长10%";
      regression.newTitle = "E2E ESG 企业绿色投资增长25%";
      await configureFeed("success", regression.oldTitle);
      await page.locator("#start-run").click();
      regression.baselineRun = await waitForFinishedRun(page);
      assert.equal(regression.baselineRun.status, "complete");
      await page.locator("#latest-list").getByRole("link", { name: regression.oldTitle, exact: true }).waitFor({ timeout: 10000 });
      await configureFeed("partial", regression.newTitle);
      await page.locator("#start-run").click();
      regression.partialRun = await waitForFinishedRun(page, regression.baselineRun.id);
      assert.equal(regression.partialRun.status, "degraded");
      assert.equal(regression.partialRun.article_count, 1);
      await page.locator("#latest-list").getByRole("link", { name: regression.newTitle, exact: true }).waitFor({ timeout: 10000 });
      assert.equal(await page.locator("#latest-list").getByRole("link", { name: regression.oldTitle, exact: true }).count(), 0);
      assert.match(await page.locator("#report-context").textContent(), /本次部分完成.*已展示可用新结果/);
      assert.equal((await apiJson(page, "/api/reports/latest")).body.report.run.id, regression.partialRun.id);
      return "完整报告→部分完成：增长25%的新文立即替换旧10%报告并标注部分完成";
    });

    await runCheck("1.3 当前来源错误与健康随任务刷新，恢复后清除", async () => {
      const badRow = page.locator("#source-sheet .source-grid").filter({ hasText: regression.bad.name });
      const goodRow = page.locator("#source-sheet .source-grid").filter({ hasText: regression.good.name });
      await page.waitForFunction(() => document.querySelector("#source-errors")?.textContent.includes("E2E_CURRENT_PARTIAL_SOURCE_ERROR"));
      assert.match(await badRow.locator(".source-health").textContent(), /异常.*E2E_CURRENT_PARTIAL_SOURCE_ERROR/);
      await configureFeed("all_failed", "");
      await page.locator("#start-run").click();
      const failed = await waitForFinishedRun(page, regression.partialRun.id);
      assert.equal(failed.status, "failed");
      await page.waitForFunction(() => document.querySelector("#source-errors")?.textContent.includes("本次 2 个来源"));
      assert.match(await page.locator("#source-errors").textContent(), /E2E_CURRENT_ALL_FAILED_SOURCE_ERROR/);
      assert.doesNotMatch(await page.locator("#source-errors").textContent(), /E2E_CURRENT_PARTIAL_SOURCE_ERROR/);
      await page.waitForFunction((names) => names.every((name) => [...document.querySelectorAll("#source-sheet .source-grid")].some((row) => row.textContent.includes(name) && row.querySelector(".source-health")?.textContent.includes("E2E_CURRENT_ALL_FAILED_SOURCE_ERROR"))), [regression.good.name, regression.bad.name], { timeout: 7000 });
      assert.match(await goodRow.locator(".source-health").textContent(), /异常/);
      assert.match(await badRow.locator(".source-health").textContent(), /异常/);
      assert.equal((await apiJson(page, "/api/reports/latest")).body.report.run.id, regression.partialRun.id);
      regression.recoveryTitle = "E2E ESG 企业绿色投资增长40%";
      await configureFeed("success", regression.recoveryTitle);
      await page.locator("#start-run").click();
      regression.recoveryRun = await waitForFinishedRun(page, failed.id);
      assert.equal(regression.recoveryRun.status, "complete");
      await page.locator("#latest-list").getByRole("link", { name: regression.recoveryTitle, exact: true }).waitFor({ timeout: 10000 });
      await page.locator("#source-errors").waitFor({ state: "hidden" });
      assert.equal((await badRow.locator(".source-health").textContent()).trim(), "正常");
      assert.equal((await goodRow.locator(".source-health").textContent()).trim(), "正常");
      return "1个源错误→2个当前错误→恢复清除；来源健康同步变化，旧报告不会遮蔽当前故障";
    });

    await runCheck("1.3 取消运行后立即保留有效报告并显示两个时间", async () => {
      await configureFeed("slow", "");
      await page.locator("#start-run").click();
      await page.locator("#cancel-run").waitFor({ state: "visible" });
      await page.waitForFunction(async (token) => {
        const response = await fetch("/api/e2e/state", { headers: { "X-Cockpit-Token": token } });
        return (await response.json()).fetch_calls.length > 0;
      }, sessionToken);
      await page.locator("#cancel-run").click();
      await page.locator("#cancel-run").waitFor({ state: "hidden" });
      const cancelled = (await apiJson(page, "/api/runs/current")).body.run;
      assert.equal(cancelled.status, "cancelled");
      assert.match(await page.locator("#report-context").textContent(), /本次新增 0 条.*当前为上次有效报告/);
      assert.match(await page.locator("#report-context").textContent(), /本次任务：.+当前报告：/);
      assert.match(await page.locator("#run-badge").textContent(), /已取消/);
      assert.equal((await apiJson(page, "/api/reports/latest")).body.report.run.id, regression.recoveryRun.id);
      await page.locator("#latest-list").getByRole("link", { name: regression.recoveryTitle, exact: true }).waitFor();
      return "取消完成后无需刷新，旧报告、取消状态和本次/报告时间同时可见";
    });

    await runCheck("1.3 超过旧15秒超时的来源草稿及已保存验证均成功", async () => {
      await page.locator("#add-source").click();
      const slowName = `E2E 慢验证 ${suffix}`;
      await page.locator("#source-name").fill(slowName);
      await page.locator("#source-url").fill(`https://fixture.invalid/slow-validation-${suffix}.xml`);
      const draftStart = Date.now();
      await page.locator("#validate-source-draft").click();
      await page.waitForFunction(() => document.querySelector("#source-validation")?.dataset.state === "success", null, { timeout: 25000 });
      const draftElapsed = Date.now() - draftStart;
      assert.ok(draftElapsed >= 15500, `Expected real 16s validation; got ${draftElapsed}ms`);
      assert.match(await page.locator("#source-validation").textContent(), /读取成功/);
      await page.locator("#source-form [type=submit]").click();
      await page.locator("#source-dialog").waitFor({ state: "hidden" });
      const slowRow = page.locator("#source-sheet .source-grid").filter({ hasText: slowName });
      const savedStart = Date.now();
      await slowRow.getByRole("button", { name: "验证", exact: true }).click();
      await page.waitForFunction((name) => [...document.querySelectorAll("#source-sheet .source-grid")].some((row) => row.textContent.includes(name) && row.querySelector(".source-health")?.textContent.trim() === "正常"), slowName, { timeout: 25000 });
      const savedElapsed = Date.now() - savedStart;
      assert.ok(savedElapsed >= 15500, `Expected real 16s validation; got ${savedElapsed}ms`);
      assert.doesNotMatch(await page.locator("#toast-region").textContent(), /超时/);
      return `草稿验证${draftElapsed}ms、已保存验证${savedElapsed}ms均成功，未产生旧15秒误报`;
    });

    await runCheck("浏览器流程无真实外网请求", async () => {
      assert.deepEqual(report.externalRequests, []);
      assert.deepEqual(report.requestFailures, []);
      assert.deepEqual(report.consoleErrors, []);
      assert.deepEqual(report.pageErrors, []);
      return "所有请求仅访问 127.0.0.1；HTTP、控制台和页面异常均为空";
    });

    await page.screenshot({ path: path.join(outputDir, "e2e-final.png"), fullPage: true });
    await context.close();
    report.status = "passed";
    report.baseUrl = baseUrl;
  } catch (error) {
    report.status = "failed";
    report.error = error.stack || error.message;
    report.serverOutput = serverOutput.join("").slice(-4000);
    const failedPage = browser?.contexts()[0]?.pages()[0];
    if (failedPage) {
      await failedPage.screenshot({ path: path.join(outputDir, "e2e-failed.png"), fullPage: true }).catch(() => {});
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    await stopFixtureServer(baseUrl, server);
    if (process.env.KEEP_E2E_DATA !== "1") {
      const resolved = path.resolve(dataHome);
      const tempRoot = path.resolve(os.tmpdir());
      const basename = path.basename(resolved);
      if (path.dirname(resolved) === tempRoot && basename.startsWith("cookies-news-cockpit-e2e-")) {
        try {
          fs.rmSync(resolved, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
        } catch (error) {
          report.cleanupWarning = `Isolated fixture directory retained: ${resolved}; ${error.message}`;
        }
      }
    }
    fs.writeFileSync(path.join(outputDir, "report.json"), `${JSON.stringify(report, null, 2)}\n`, "utf8");
  }
}

main().then(() => {
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
