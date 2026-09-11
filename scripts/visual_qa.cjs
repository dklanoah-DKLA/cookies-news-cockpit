/* Local-only responsive and accessibility smoke test for the browser cockpit. */

const fs = require("node:fs");
const path = require("node:path");

const playwrightRoot = process.env.CODEX_PLAYWRIGHT_ROOT;
if (!playwrightRoot) {
  throw new Error("Set CODEX_PLAYWRIGHT_ROOT to the installed playwright package directory.");
}

const { chromium } = require(playwrightRoot);

const cockpitUrl = process.env.COCKPIT_URL || "http://127.0.0.1:8765/#token=visual-test-token";
const configuredChromePath = process.env.CHROME_PATH;
const windowsChrome = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const chromePath = configuredChromePath || (fs.existsSync(windowsChrome) ? windowsChrome : undefined);
const outputDir = path.resolve(process.env.VISUAL_QA_OUTPUT || "artifacts/visual-qa");
const viewports = [
  { name: "mobile-320", width: 320, height: 900 },
  { name: "mobile-375", width: 375, height: 900 },
  { name: "mobile-414", width: 414, height: 900 },
  { name: "tablet-768", width: 768, height: 1024 },
  { name: "laptop-1280", width: 1280, height: 800 },
  { name: "macbook-1440", width: 1440, height: 900 },
];

async function inspectViewport(browser, viewport) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto(cockpitUrl, { waitUntil: "networkidle" });
  await page.waitForSelector("#main-content");
  const onboarding = page.locator("#onboarding-dialog");
  if (await onboarding.evaluate((dialog) => dialog.open)) {
    await page.locator("#skip-onboarding").click();
    await onboarding.waitFor({ state: "hidden" });
  }
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({
    path: path.join(outputDir, `${viewport.name}.png`),
    fullPage: true,
  });
  await page.screenshot({
    path: path.join(outputDir, `${viewport.name}-fold.png`),
    fullPage: false,
  });

  const measurements = await page.evaluate(() => {
    const root = document.documentElement;
    const affordanceSelector = [
      "button",
      "nav a",
      ".brand",
      ".footer-button",
    ].join(",");
    const visible = (element) => {
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0;
    };
    const hasWrappedTextNode = (element) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        if (!walker.currentNode.textContent.trim()) continue;
        const range = document.createRange();
        range.selectNodeContents(walker.currentNode);
        const tops = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top)));
        if (tops.size > 1) return true;
      }
      return false;
    };
    const wrapping = [...document.querySelectorAll(affordanceSelector)]
      .filter(visible)
      .filter(hasWrappedTextNode)
      .map((element) => (element.textContent || element.getAttribute("aria-label") || element.tagName).trim());
    const undersized = [
      ...document.querySelectorAll(
        "button, input:not([type=checkbox]):not([type=radio]):not([type=hidden]), select, textarea",
      ),
    ]
      .filter(visible)
      .map((element) => {
        const box = element.getBoundingClientRect();
        return {
          label: element.getAttribute("aria-label") || element.textContent.trim() || element.id || element.tagName,
          className: element.className,
          width: box.width,
          height: box.height,
        };
      })
      .filter((item) => item.width < 44 || item.height < 44);
    const introBox = document.querySelector(".intro")?.getBoundingClientRect();
    const primaryBox = document.querySelector("#start-run")?.getBoundingClientRect();
    return {
      horizontalOverflow: root.scrollWidth > root.clientWidth,
      scrollWidth: root.scrollWidth,
      clientWidth: root.clientWidth,
      wrapping,
      undersized,
      heroFitsFold: Boolean(
        introBox &&
        primaryBox &&
        introBox.top >= 0 &&
        introBox.bottom <= window.innerHeight &&
        primaryBox.top >= 0 &&
        primaryBox.bottom <= window.innerHeight
      ),
      serviceLabel: document.querySelector("#service-label")?.textContent?.trim() || "",
      addressBarTokenRemoved: location.hash === "" && !location.search.includes("token="),
    };
  });

  await page.locator("#source-category-filter").selectOption("banking_all");
  await page.locator("#source-search").fill("香港");
  await page.locator("#sources").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(outputDir, `${viewport.name}-banking-sources.png`) });
  measurements.bankingFilters = await page.evaluate(() => {
    const selectors = ["#source-category-filter", "#source-search"];
    const controls = selectors.map((selector) => {
      const element = document.querySelector(selector);
      const box = element.getBoundingClientRect();
      return { id: element.id, labelled: Boolean(element.labels?.length), width: box.width, height: box.height };
    });
    return {
      horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      controls,
      results: document.querySelectorAll("#source-sheet .source-grid").length,
    };
  });

  await context.close();
  return { viewport, ...measurements, consoleErrors, pageErrors };
}

async function main() {
  fs.mkdirSync(outputDir, { recursive: true });
  const browser = await chromium.launch(
    chromePath ? { executablePath: chromePath, headless: true } : { headless: true },
  );
  try {
    const results = [];
    for (const viewport of viewports) {
      results.push(await inspectViewport(browser, viewport));
    }
    const reportPath = path.join(outputDir, "report.json");
    fs.writeFileSync(reportPath, `${JSON.stringify(results, null, 2)}\n`, "utf8");
    process.stdout.write(`${JSON.stringify(results, null, 2)}\n`);

    const failed = results.some(
      (result) =>
        result.horizontalOverflow ||
        result.wrapping.length > 0 ||
        result.undersized.length > 0 ||
        !result.heroFitsFold ||
        result.consoleErrors.length > 0 ||
        result.pageErrors.length > 0 ||
        result.bankingFilters.horizontalOverflow ||
        !result.bankingFilters.results ||
        result.bankingFilters.controls.some((control) => !control.labelled || control.width < 44 || control.height < 44) ||
        !result.addressBarTokenRemoved,
    );
    if (failed) process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
