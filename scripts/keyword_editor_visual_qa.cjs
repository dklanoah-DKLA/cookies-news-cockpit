/* Local fixture only: keyword editor responsive, focus, contrast, and naming checks. */
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

if (!process.env.CODEX_PLAYWRIGHT_ROOT) throw new Error("Set CODEX_PLAYWRIGHT_ROOT.");
const { chromium } = require(process.env.CODEX_PLAYWRIGHT_ROOT);
const cockpitUrl = process.env.COCKPIT_URL || "http://127.0.0.1:8765/#token=visual-test-token";
if (!["127.0.0.1", "localhost", "[::1]"].includes(new URL(cockpitUrl).hostname)) {
  throw new Error("Keyword visual QA accepts only a local fixture server.");
}
const windowsChrome = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const chromePath = process.env.CHROME_PATH || (fs.existsSync(windowsChrome) ? windowsChrome : undefined);
const outputDir = path.resolve(process.env.KEYWORD_VISUAL_OUTPUT || "artifacts/keyword-editor/visual-qa");
const previewUrl = pathToFileURL(path.resolve("tests/fixtures/keyword-editor.preview.html")).href;
const viewports = [
  { name: "mobile-320", width: 320, height: 900 },
  { name: "mobile-375", width: 375, height: 900 },
  { name: "mobile-414", width: 414, height: 900 },
  { name: "tablet-768", width: 768, height: 1024 },
  { name: "laptop-1280", width: 1280, height: 800 },
  { name: "macbook-1440", width: 1440, height: 900 },
];

// Runs inside the page; measurements concern the component, not a full WCAG audit.
function inspectEditors() {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  const rgba = (color) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, 1, 1);
    return [...ctx.getImageData(0, 0, 1, 1).data].map((value, i) => i === 3 ? value / 255 : value);
  };
  const composite = (front, back) => {
    const alpha = front[3] + back[3] * (1 - front[3]);
    if (!alpha) return [0, 0, 0, 0];
    return [...front.slice(0, 3).map((value, i) => (value * front[3] + back[i] * back[3] * (1 - front[3])) / alpha), alpha];
  };
  const background = (node) => {
    let color = rgba(getComputedStyle(node).backgroundColor);
    for (let parent = node.parentElement; parent && color[3] < 1; parent = parent.parentElement) {
      color = composite(color, rgba(getComputedStyle(parent).backgroundColor));
    }
    return color;
  };
  const luminance = (color) => color.slice(0, 3).map((value) => {
    const component = value / 255;
    return component <= 0.04045 ? component / 12.92 : ((component + 0.055) / 1.055) ** 2.4;
  }).reduce((sum, value, i) => sum + value * [0.2126, 0.7152, 0.0722][i], 0);
  const contrast = (foreground, surface) => {
    const a = luminance(foreground), b = luminance(surface);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const visible = (node) => node.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
  const label = (node) => node.getAttribute("aria-label") || [...(node.labels || [])].map((el) => el.textContent.trim()).join(" ") || node.textContent.trim() || node.id;
  return [...document.querySelectorAll(".keyword-editor")].map((editor) => {
    const controls = [...editor.querySelectorAll("button, textarea, input, summary")].filter(visible);
    const measurements = controls.map((node) => {
      const box = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      const result = { label: label(node), width: box.width, height: box.height, disabled: node.disabled || false, transition: style.transitionDuration, transitionProperty: style.transitionProperty, animation: style.animationName };
      if (node.matches("button, summary")) result.singleLine = style.whiteSpace === "nowrap";
      if (node.matches("input, textarea")) result.visibleLabel = [...(node.labels || [])].some(visible);
      return result;
    });
    const samples = [...editor.querySelectorAll("h3, label, p, button, summary, input, textarea, .field-count")]
      .filter(visible).filter((node) => !node.disabled && node.getAttribute("aria-disabled") !== "true")
      .map((node) => {
        const style = getComputedStyle(node);
        const surface = background(node);
        const foreground = rgba(style.color);
        return { label: label(node), ratio: contrast(foreground, surface), large: parseFloat(style.fontSize) >= 24 || (parseFloat(style.fontSize) >= 18.66 && parseInt(style.fontWeight, 10) >= 700) };
      });
    for (const node of controls.filter((node) => node.matches("input, textarea") && !node.disabled && !node.value)) {
      samples.push({ label: `${node.id} placeholder`, ratio: contrast(rgba(getComputedStyle(node, "::placeholder").color), background(node)), large: false });
    }
    const focus = controls.filter((node) => node.matches(":focus-visible, .is-focus")).map((node) => {
      const style = getComputedStyle(node);
      return { label: label(node), width: parseFloat(style.outlineWidth), style: style.outlineStyle, ratio: contrast(rgba(style.outlineColor), background(node)), pageRatio: contrast(rgba(style.outlineColor), background(document.body)), transition: style.transitionDuration };
    });
    const tokenTitles = [...editor.querySelectorAll(".keyword-token__edit")].map((node) => ({ label: label(node), title: node.title, ellipsis: getComputedStyle(node).textOverflow === "ellipsis" }));
    return {
      id: editor.id,
      state: editor.dataset.state || "default",
      labelled: Boolean(document.getElementById(editor.getAttribute("aria-labelledby"))),
      overflow: editor.scrollWidth > editor.clientWidth + 1,
      controls: measurements,
      contrast: samples,
      focus,
      tokenTitles,
      status: Boolean(editor.querySelector('[role="status"]')),
    };
  });
}

function faults(results) {
  return results.flatMap((row) => {
    const errors = [];
    if (row.overflow || !row.labelled || !row.status) errors.push(`${row.id}: overflow/label/status`);
    for (const control of row.controls) {
      if (!control.label || control.width < 43.9 || control.height < 43.9 || control.singleLine === false || control.visibleLabel === false) errors.push(`${row.id}: control ${control.label}`);
      if (control.animation !== "none" || (control.transitionProperty !== "none" && control.transition.split(",").some((value) => parseFloat(value) > 0))) errors.push(`${row.id}: motion ${control.label}`);
    }
    for (const sample of row.contrast) if (!Number.isFinite(sample.ratio) || sample.ratio < (sample.large ? 3 : 4.5)) errors.push(`${row.id}: contrast ${sample.label} ${sample.ratio.toFixed(2)}`);
    for (const sample of row.focus) if (sample.width < 2 || sample.style !== "solid" || !Number.isFinite(sample.ratio) || !Number.isFinite(sample.pageRatio) || sample.ratio < 3 || sample.pageRatio < 3) errors.push(`${row.id}: focus ${sample.label}`);
    for (const token of row.tokenTitles) if (!token.title || !token.ellipsis) errors.push(`${row.id}: long token ${token.label}`);
    return errors;
  });
}

async function inspectProduction(browser, viewport) {
  const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.goto(cockpitUrl, { waitUntil: "networkidle" });
  const onboarding = page.locator("#onboarding-dialog");
  if (await onboarding.evaluate((node) => node.open)) await page.locator("#skip-onboarding").click();
  await page.locator("#add-topic").click();
  const dialog = page.locator("#topic-dialog");
  await dialog.waitFor({ state: "visible" });
  // Wait for the modal's deliberate initial-focus target before entering a draft.
  await page.waitForFunction(() => document.activeElement?.id === "topic-name");
  await page.locator("#topic-name").fill("银行动态测试草稿");
  const entry = page.locator("#topic-keyword-entry");
  await entry.fill("银行，央行、跨境银行风险管理与审慎监管资本流动性长期跟踪关键词");
  await page.locator("#topic-keyword-add").click();
  await page.locator("#topic-exclude-entry").fill("招聘、广告");
  await page.locator("#topic-exclude-add").click();
  await page.evaluate(() => document.fonts.ready);
  await page.locator("#topic-form").evaluate((node) => { node.scrollTop = 0; });
  await page.screenshot({ path: path.join(outputDir, `${viewport.name}-dialog-top.png`), fullPage: false });
  await entry.focus();
  const base = await page.evaluate(inspectEditors);
  if (!base.some((row) => row.focus.length)) errors.push("Missing keyboard-visible focus ring");
  errors.push(...faults(base));
  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    form: document.querySelector("#topic-form").scrollWidth > document.querySelector("#topic-form").clientWidth + 1,
  }));
  if (overflow.document || overflow.form) errors.push("Horizontal overflow in topic dialog");

  await page.locator("#topic-keyword-chips .keyword-token__edit").first().click();
  await entry.fill("央行");
  await page.locator("#topic-keyword-add").click();
  const invalid = await entry.getAttribute("aria-invalid");
  const errorState = await page.locator("#topic-keyword-editor").getAttribute("data-state");
  if (invalid !== "true" || errorState !== "error") errors.push("Duplicate edit error not exposed");
  const errorMeasurements = await page.evaluate(inspectEditors);
  errors.push(...faults(errorMeasurements));
  await page.screenshot({ path: path.join(outputDir, `${viewport.name}-edit-error.png`), fullPage: false });
  await page.locator("#topic-keyword-cancel-edit").click();
  await page.locator("#topic-keyword-chips .keyword-token__remove").first().click();
  const undo = page.locator("#topic-keyword-message").getByRole("button", { name: "撤销" });
  await undo.waitFor({ state: "visible" });
  const undoMeasurements = await page.evaluate(inspectEditors);
  errors.push(...faults(undoMeasurements));
  await undo.click();
  await page.locator("#topic-keyword-bulk-toggle summary").click();
  await page.locator("#topic-keyword-bulk").fill("数字银行\nBank of America");
  const bulkMeasurements = await page.evaluate(inspectEditors);
  errors.push(...faults(bulkMeasurements));
  await page.screenshot({ path: path.join(outputDir, `${viewport.name}-bulk-entry.png`), fullPage: false });
  await context.close();
  return { viewport, overflow, base, errorMeasurements, undoMeasurements, bulkMeasurements, errors };
}

async function inspectPreview(browser, viewport) {
  const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(previewUrl);
  await page.waitForFunction(() => window.previewReady === true);
  await page.evaluate(() => document.fonts.ready);
  const measurements = await page.evaluate(inspectEditors);
  errors.push(...faults(measurements));
  if (measurements.length !== 8) errors.push("Eight state previews not rendered");
  for (const state of ["default", "hover", "focus", "active", "disabled", "loading", "error", "success"]) {
    await page.locator(`#${state}-sample`).screenshot({ path: path.join(outputDir, `${viewport.name}-state-${state}.png`) });
  }
  await context.close();
  return { viewport, measurements, errors };
}

async function main() {
  fs.mkdirSync(outputDir, { recursive: true });
  const browser = await chromium.launch(chromePath ? { executablePath: chromePath, headless: true } : { headless: true });
  try {
    const results = { production: [], preview: [] };
    for (const viewport of viewports) {
      results.production.push(await inspectProduction(browser, viewport));
      if (viewport.width <= 768) results.preview.push(await inspectPreview(browser, viewport));
    }
    const failed = [...results.production, ...results.preview].flatMap((row) => row.errors);
    results.summary = { checkedAt: new Date().toISOString(), errors: failed, passed: failed.length === 0, scope: "Component smoke checks, not a complete WCAG certification" };
    fs.writeFileSync(path.join(outputDir, "report.json"), `${JSON.stringify(results, null, 2)}\n`, "utf8");
    process.stdout.write(`${JSON.stringify(results.summary, null, 2)}\n`);
    if (failed.length) process.exitCode = 1;
  } finally { await browser.close(); }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
