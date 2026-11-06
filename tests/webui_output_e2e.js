// Browser test of the result folder on the settings page of an exported application.
//
//   python tests/webui_output_server.py --dir WORK --port 9910 --opener   (prints one JSON line: launch, base, ...)
//   NODE_PATH=<node_modules with playwright> node tests/webui_output_e2e.js '<that JSON line>' WORK SHOTS_DIR
//
// Checks: the card shows the folder in effect without overflowing (also a long path, also at phone width), the folder
// browser lists folder names only and marks protected folders, a new folder can be created, a chosen (long, missing)
// folder is created after consent, saved and used by the next run, it survives a reload, "Open folder" reaches the
// server, Escape closes the dialog and gives the focus back, and "Restore default" switches back. Screenshots in
// light and dark, German and English, desktop and phone width.
"use strict";
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const [infoText, work, shots] = process.argv.slice(2);
const info = JSON.parse(infoText);
const problems = [];
const check = (ok, what) => { if (!ok) problems.push(what); console.log((ok ? "ok   " : "FAIL ") + what); };
const LONG = path.join(work, "Kundenprojekte Mandant Nordwest – Gesellschaft für Datenverarbeitung und Archivierung mbH", "Ergebnisse", "2026", "November", "Automatisch erzeugte Workflow-Ergebnisse der Quartalsberichte");

function runFolders(folder) {
  try { return fs.readdirSync(folder, { withFileTypes: true }).filter((entry) => entry.isDirectory()).map((entry) => entry.name); } catch (e) { return []; }
}

async function overflow(page) {
  return page.evaluate(() => {
    const card = document.getElementById("output-dir").closest(".card");
    const box = card.getBoundingClientRect();
    const inside = [...card.querySelectorAll("*")].every((node) => {
      const r = node.getBoundingClientRect();
      return r.width === 0 || (r.left >= box.left - 1 && r.right <= box.right + 1);
    });
    return { page: document.documentElement.scrollWidth > window.innerWidth, card: !inside };
  });
}

async function runOnce(page, label) {
  await page.click("#nav-run");
  await page.fill("#field-input", "Bericht " + label);
  await page.click("#start");
  await page.waitForSelector("#run-status.succeeded, #run-status.failed", { timeout: 60000 });
  check((await page.getAttribute("#run-status", "class")).includes("succeeded"), `run ${label} succeeded`);
  await page.waitForTimeout(600);
}

async function shot(page, file, fullPage) {
  // The short notice at the bottom would cover the card in a full-page picture.
  await page.evaluate(() => { document.getElementById("notice").hidden = true; });
  await page.screenshot({ path: file, fullPage });
}

async function theme(page, value) {
  await page.evaluate((v) => document.documentElement.setAttribute("data-theme", v), value);
}

async function language(page, code) {
  await page.selectOption("#language", code);
  await page.waitForFunction((l) => document.documentElement.lang === l, code);
  await page.waitForTimeout(300);
}

(async () => {
  fs.mkdirSync(shots, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: "en-US" });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: info.base });
  const page = await context.newPage();
  // A 409 is the expected answer for a folder that does not exist yet (the page then asks before creating it).
  page.on("console", (m) => { if (m.type() === "error" && !/status of 409/.test(m.text())) problems.push("console: " + m.text()); });
  page.on("pageerror", (e) => problems.push("pageerror: " + e.message));
  page.on("dialog", (dialog) => { console.log("     confirm: " + dialog.message().replace(/\n/g, " | ")); dialog.accept(); });

  await page.goto(info.launch);
  await page.waitForSelector("#view-run:not([hidden])");
  await language(page, "en");

  // 1. Default folder, first run writes there.
  await runOnce(page, "1");
  check(runFolders(info.default).length === 1, "run 1 wrote into the default folder");
  await page.click("#nav-setup");
  await page.waitForSelector("#output-path");
  check((await page.textContent("#output-path")) === info.default, "card shows the default folder");
  check((await page.textContent("#output-source")).trim() === "Default", "badge says Default");
  check(!(await page.isVisible("#output-reset")), "no Restore default while the default is in effect");
  check(await page.isVisible("#output-open"), "Open folder offered to a browser on this computer");
  await theme(page, "light");
  await page.locator("#output-title").scrollIntoViewIfNeeded();
  await shot(page, `${shots}/01-en-light-default.png`, true);

  // 2. Folder browser: names only, protected folders marked, a new folder.
  await page.click("#output-change");
  await page.waitForSelector("#folder-dialog[open] .folder-item, #folder-dialog[open] .folder-empty");
  check(await page.evaluate(() => document.activeElement.id === "folder-path"), "focus moves into the dialog");
  await page.fill("#folder-path", info.project);
  await page.click("#folder-go");
  await page.waitForSelector("#folder-list .folder-item >> text=workflow");
  const listed = await page.$$eval("#folder-list .folder-item", (items) => items.map((i) => ({ name: i.querySelector(".folder-label").textContent, tag: !!i.querySelector(".tag") })));
  const names = listed.map((i) => i.name);
  check(names.includes("results") && names.includes("app") && names.includes("data"), "project folders listed: " + names.join(", "));
  check(!names.some((n) => n.includes(".")), "no files listed (" + names.join(", ") + ")");
  check(listed.find((i) => i.name === "app").tag && listed.find((i) => i.name === "data").tag, "app and data marked protected");
  check(!listed.find((i) => i.name === "results").tag, "results not marked");
  await page.click("#folder-list .folder-item >> text=data");
  await page.waitForFunction(() => document.getElementById("folder-path").value.endsWith("data"));
  check(await page.isDisabled("#folder-use"), "Use this folder disabled inside the data folder");
  check(await page.isVisible("#folder-note"), "explains why");
  await shot(page, `${shots}/02-en-light-browser-protected.png`, false);
  await page.click("#folder-up");
  await page.waitForFunction((p) => document.getElementById("folder-path").value === p, info.project);
  await page.fill("#folder-path", work);
  await page.click("#folder-go");
  await page.waitForSelector("#folder-list .folder-item >> text=export");
  await page.click("#folder-new");
  await page.fill("#folder-new-name", "Neuer Ordner");
  await page.click("#folder-new-create");
  await page.waitForFunction((p) => document.getElementById("folder-path").value === p, path.join(work, "Neuer Ordner"));
  check(fs.existsSync(path.join(work, "Neuer Ordner")), "new folder created on the server");
  await page.click("#folder-up");
  await page.waitForSelector("#folder-list .folder-item >> text=Neuer Ordner");
  await shot(page, `${shots}/03-en-light-browser.png`, false);
  // Escape closes and returns the focus.
  await page.keyboard.press("Escape");
  await page.waitForSelector("#folder-dialog:not([open])", { state: "attached" });
  check(await page.evaluate(() => document.activeElement.id === "output-change"), "Escape returns focus to Change folder");

  // 3. A long folder that does not exist yet: consent, creation, saved.
  await page.click("#output-change");
  await page.waitForSelector("#folder-dialog[open] .folder-item, #folder-dialog[open] .folder-empty");
  await page.fill("#folder-path", LONG);
  await page.click("#folder-use");
  await page.waitForSelector("#folder-dialog:not([open])", { state: "attached" });
  await page.waitForFunction((p) => (document.getElementById("output-path") || {}).textContent === p, LONG);
  check(fs.existsSync(LONG), "long folder created after consent");
  check((await page.textContent("#output-source")).trim() === "Your choice", "badge says Your choice");
  check(await page.isVisible("#output-reset"), "Restore default offered");
  let o = await overflow(page);
  check(!o.page && !o.card, "long path stays inside the card (desktop)");
  await shot(page, `${shots}/04-en-light-long-path.png`, true);
  await theme(page, "dark");
  await shot(page, `${shots}/05-en-dark-long-path.png`, true);
  await language(page, "de");
  await page.waitForSelector("#output-path");
  await shot(page, `${shots}/06-de-dark-long-path.png`, true);
  await theme(page, "light");
  await shot(page, `${shots}/07-de-light-long-path.png`, true);

  // 4. Open folder reaches the server (recorded by the test server instead of a file manager).
  await page.click("#output-open");
  await page.waitForTimeout(400);
  const opened = fs.existsSync(path.join(work, "opened.txt")) ? fs.readFileSync(path.join(work, "opened.txt"), "utf-8").trim() : "";
  check(opened === LONG, "Open folder asked the server to show the chosen folder");
  await page.click("#output-copy");
  check((await page.evaluate(() => navigator.clipboard.readText())) === LONG, "Copy path copies the full path");

  // 5. The next run writes into the chosen folder; the earlier run stays where it was.
  await runOnce(page, "2");
  check(runFolders(LONG).length === 1, "run 2 wrote into the chosen folder");
  check(runFolders(info.default).length === 1, "run 1 stayed in the default folder");
  const runFolder = path.join(LONG, runFolders(LONG)[0] || "-");
  check(fs.existsSync(path.join(runFolder, "Bericht.md")), "Bericht.md in the chosen folder");

  // 6. A reload (the server keeps the choice).
  await page.reload();
  await page.waitForSelector("#view-run:not([hidden])");
  await page.click("#nav-setup");
  await page.waitForSelector("#output-path");
  check((await page.textContent("#output-path")) === LONG, "choice kept after reload");

  // 7. Phone width, long path, German, both themes; the dialog too.
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: "de-DE", colorScheme: "dark" });
  await phone.addCookies(await context.cookies());
  const mobile = await phone.newPage();
  mobile.on("dialog", (dialog) => dialog.accept());
  await mobile.goto(info.base + "/");
  await mobile.waitForSelector("#view-run:not([hidden])");
  await language(mobile, "de");
  await mobile.click("#nav-setup");
  await mobile.waitForSelector("#output-path");
  o = await overflow(mobile);
  check(!o.page && !o.card, "long path stays inside the card (phone)");
  await mobile.locator("#output-title").scrollIntoViewIfNeeded();
  await shot(mobile, `${shots}/08-de-dark-phone.png`, true);
  await theme(mobile, "light");
  await shot(mobile, `${shots}/09-de-light-phone.png`, true);
  await mobile.click("#output-change");
  await mobile.waitForSelector("#folder-dialog[open] .folder-item, #folder-dialog[open] .folder-empty");
  const dialogOverflow = await mobile.evaluate(() => {
    const box = document.getElementById("folder-dialog").getBoundingClientRect();
    return box.left < 0 || box.right > window.innerWidth;
  });
  check(!dialogOverflow, "dialog fits the phone width");
  await shot(mobile, `${shots}/10-de-light-phone-browser.png`, false);
  await theme(mobile, "dark");
  await shot(mobile, `${shots}/11-de-dark-phone-browser.png`, false);
  await phone.close();

  // 8. Restore default; the next run writes to the default again, nothing is moved.
  await page.click("#output-reset");
  await page.waitForFunction((p) => (document.getElementById("output-path") || {}).textContent === p, info.default);
  check(!(await page.isVisible("#output-reset")), "Restore default gone after restoring");
  await runOnce(page, "3");
  check(runFolders(info.default).length === 2, "run 3 wrote into the default folder");
  check(runFolders(LONG).length === 1, "files of run 2 stay in the chosen folder");
  await page.click("#nav-setup");
  await page.waitForSelector("#output-path");
  await theme(page, "dark");
  await shot(page, `${shots}/12-de-dark-restored.png`, true);

  // 9. Every language renders the card (no missing keys shown as keys).
  for (const code of ["en", "es", "fr", "it", "pt-BR", "de"]) {
    await language(page, code);
    await page.waitForSelector("#output-path");
    const shown = await page.textContent("#output-dir");
    check(!/setup\.output|folder\./.test(shown), `${code}: no raw keys on the card`);
  }

  await browser.close();
  if (problems.length) {
    console.log("\nPROBLEMS:\n" + problems.join("\n"));
    process.exit(1);
  }
  console.log("\nall checks passed");
})().catch((error) => { console.error(error); process.exit(1); });
