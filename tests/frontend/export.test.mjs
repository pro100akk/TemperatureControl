// Exercise the same ICU -> Marked -> XSS pipeline used by HA's frontend.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { test } from "node:test";
import { IntlMessageFormat } from "intl-messageformat";
import { marked } from "marked";
import xss from "xss";
import { chromium } from "playwright";

const translations = {};
for (const language of ["en", "ru"]) {
  translations[language] = JSON.parse(await readFile(
    new URL(`../../custom_components/apartment_comfort/translations/${language}.json`, import.meta.url), "utf8"));
}

function* strings(value, path = "") {
  if (typeof value === "string") yield [path, value];
  else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) yield* strings(item, `${path}.${key}`);
  }
}

test("all translation templates compile with HA's ICU formatter", () => {
  for (const [language, dictionary] of Object.entries(translations)) {
    for (const [path, text] of strings(dictionary)) {
      assert.doesNotThrow(() => new IntlMessageFormat(text, language), `${language}${path}`);
    }
  }
});

test("the previous literal HTML template reproduces INVALID_TAG", () => {
  assert.throws(() => new IntlMessageFormat('<a href="{download_url}" target="_blank">ZIP</a>', "en"),
                /INVALID_TAG/);
});

test("translated and sanitized export links download in a real browser", async () => {
  // A valid empty ZIP keeps this transport test independent of Python bundling,
  // which is covered by test_export.py with actual history and diagnostics.
  const zip = Buffer.from("504b0506000000000000000000000000000000000000", "hex");
  let requests = 0;
  let pageHtml = "";
  const server = createServer((request, response) => {
    if (request.url === "/api/apartment_comfort/history/test?authSig=signed&part=1") {
      requests++;
      response.writeHead(200, {"Content-Type": "application/zip",
        "Content-Disposition": 'attachment; filename="history.zip"'});
      response.end(zip);
    } else {
      response.writeHead(200, {"Content-Type": "text/html; charset=utf-8"});
      response.end(pageHtml);
    }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true,
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
        ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
    for (const [language, dictionary] of Object.entries(translations)) {
      const template = dictionary.options.step.export_ready.description;
      const formatted = new IntlMessageFormat(template, language).format({
        download_open: '<a href="/api/apartment_comfort/history/test?authSig=signed&amp;part=1" target="_blank">',
        download_close: "</a>",
      });
      assert.equal(typeof formatted, "string");
      const sanitized = xss(marked.parse(formatted));
      assert.match(sanitized, /target="_blank"/);
      pageHtml = `<!doctype html><html><body>${sanitized}<script>
        window.intercepted = false;
        // Same-origin navigation handling from HA isNavigationClick.
        document.addEventListener('click', e => {
          if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
          const a = e.composedPath().find(n => n.tagName === 'A');
          if (!a || a.target || a.hasAttribute('download') || a.getAttribute('rel') === 'external') return;
          if (new URL(a.href).origin !== location.origin) return;
          e.preventDefault(); window.intercepted = true; history.pushState({}, '', '/lovelace');
        });
      </script></body></html>`;
      const page = await browser.newPage({acceptDownloads: true});
      const url = `http://127.0.0.1:${server.address().port}/config`;
      await page.goto(url);
      const downloadEvent = page.waitForEvent("download", {timeout: 15000});
      await page.getByRole("link", {name: language === "ru" ? "Скачать ZIP" : "Download ZIP"}).click();
      const download = await downloadEvent;
      assert.deepEqual(await readFile(await download.path()), zip);
      assert.equal(await page.evaluate(() => window.intercepted), false);
      assert.equal(page.url(), url);
      await page.close();
    }
    assert.equal(requests, 2);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});
