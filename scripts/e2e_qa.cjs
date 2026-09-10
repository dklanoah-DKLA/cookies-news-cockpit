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

async function apiJson(page, endpoint) {
  return page.evaluate(async ({ endpoint, token }) => {
    const response = await fetch(endpoint, { headers: { "X-Cockpit-Token": token } });
    return { status: response.status, body: await response.json() };
  }, { endpoint, token: sessionToken });
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

    await runCheck("浏览器流程无真实外网请求", async () => {
      assert.deepEqual(report.externalRequests, []);
      return "所有请求仅访问 127.0.0.1";
    });

    await page.screenshot({ path: path.join(outputDir, "e2e-final.png"), fullPage: true });
    await context.close();
    report.status = "passed";
    report.baseUrl = baseUrl;
  } catch (error) {
    report.status = "failed";
    report.error = error.stack || error.message;
    report.serverOutput = serverOutput.join("").slice(-4000);
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server && server.exitCode === null) server.kill();
    if (process.env.KEEP_E2E_DATA !== "1") {
      const resolved = path.resolve(dataHome);
      const tempRoot = path.resolve(os.tmpdir());
      const basename = path.basename(resolved);
      if (path.dirname(resolved) === tempRoot && basename.startsWith("cookies-news-cockpit-e2e-")) {
        fs.rmSync(resolved, { recursive: true, force: true });
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
