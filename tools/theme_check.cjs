/**
 * 主题功能 DOM 层验证（jsdom）
 *
 * 在真实 DOM 环境里执行 gui/web/js/app.js 的主题相关逻辑，验证：
 *   1. 三档主题能正确写到 <html data-theme>
 *   2. auto 模式跟随系统偏好，且系统切换时实时响应
 *   3. 设置面板「取消」能还原主题
 *   4. 保存设置时带上当前主题
 *
 * 用法：node tools/theme_check.js
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "gui/web/index.html"), "utf8");
const CSS = fs.readFileSync(path.join(ROOT, "gui/web/css/style.css"), "utf8");
const APP_JS = fs.readFileSync(path.join(ROOT, "gui/web/js/app.js"), "utf8");

let failures = 0;
function check(name, cond, extra) {
  if (cond) {
    console.log(`  ✓ ${name}`);
  } else {
    failures++;
    console.log(`  ✗ ${name}${extra ? "  → " + extra : ""}`);
  }
}

/**
 * 构建一个可控的 DOM 环境。
 * prefersDark 控制 matchMedia("(prefers-color-scheme: dark)") 的返回值，
 * 可动态修改并触发监听器，用于模拟系统主题切换。
 */
function buildDom(prefersDark) {
  const listeners = [];
  const dom = new JSDOM(HTML, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    url: "file:///vigil/index.html",
  });
  const win = dom.window;

  // mock matchMedia：支持 addEventListener + 动态触发
  const state = { dark: prefersDark };
  win.matchMedia = (query) => {
    const mql = {
      media: query,
      matches: /prefers-color-scheme:\s*dark/.test(query) ? state.dark : false,
      addEventListener: (_type, fn) => listeners.push(fn),
      removeEventListener: (_type, fn) => {
        const i = listeners.indexOf(fn);
        if (i >= 0) listeners.splice(i, 1);
      },
      addListener: (fn) => listeners.push(fn),
      removeListener: (fn) => {
        const i = listeners.indexOf(fn);
        if (i >= 0) listeners.splice(i, 1);
      },
    };
    return mql;
  };
  // 暴露触发开关，用于模拟系统主题变化
  win.__setSystemDark = (v) => {
    state.dark = v;
    listeners.forEach((fn) => fn({ matches: v }));
  };

  // GSAP 未加载，用空操作替代
  win.gsap = { fromTo: () => {}, from: () => {} };
  win.confirm = () => true;

  // 注入 CSS 变量（jsdom 不解析外链样式表，手动注入以便 cssVar 取到值）
  const style = win.document.createElement("style");
  style.textContent = CSS;
  win.document.head.appendChild(style);

  return { dom, win, state };
}

/** 在 DOM 中执行 app.js，并取出内部主题函数供测试调用。 */
function loadApp(win) {
  // app.js 整体是一个 IIFE，内部函数对外不可见。
  // 把导出语句注入到 IIFE 内部（结尾 `})();` 之前）才能取到它们。
  const EXPORT_SNIPPET = `
    window.__api = {
      applyTheme: applyTheme,
      resolveTheme: resolveTheme,
      currentTheme: currentTheme,
      state: state,
      openSettings: openSettings,
      closeSettings: closeSettings,
      loadSettings: loadSettings,
      saveSettings: saveSettings,
      onStatus: onStatus,
      bootWithBackend: bootWithBackend,
      els: els
    };
  `;
  const marker = "})();";
  const cut = APP_JS.lastIndexOf(marker);
  if (cut === -1) throw new Error("未找到 app.js 的 IIFE 结尾，无法注入导出");
  const patched = APP_JS.slice(0, cut) + EXPORT_SNIPPET + APP_JS.slice(cut);

  win.eval(patched);
  if (!win.__api) throw new Error("主题函数导出失败");
  return win.__api;
}

async function main() {
console.log("=".repeat(66));
console.log("Vigil 主题功能 · DOM 层验证");
console.log("=".repeat(66));

// ---------- 1. 基础三档切换 ----------
console.log("\n[1] 三档主题切换");
{
  const { win } = buildDom(true);
  const api = loadApp(win);
  const html = win.document.documentElement;

  api.applyTheme("dark");
  check('applyTheme("dark") → data-theme=dark', html.getAttribute("data-theme") === "dark",
    html.getAttribute("data-theme"));

  api.applyTheme("light");
  check('applyTheme("light") → data-theme=light', html.getAttribute("data-theme") === "light",
    html.getAttribute("data-theme"));

  api.applyTheme("auto");
  check("auto + 系统深色 → data-theme=dark", html.getAttribute("data-theme") === "dark",
    html.getAttribute("data-theme"));
  check("currentTheme() 仍为 auto（不丢失用户选择）", api.currentTheme() === "auto",
    api.currentTheme());

  check("colorScheme 同步为 dark", html.style.colorScheme === "dark", html.style.colorScheme);
}

// ---------- 2. auto 模式跟随系统 ----------
console.log("\n[2] 跟随系统（auto）");
{
  // 系统为浅色
  const { win } = buildDom(false);
  const api = loadApp(win);
  const html = win.document.documentElement;

  api.applyTheme("auto");
  check("auto + 系统浅色 → data-theme=light", html.getAttribute("data-theme") === "light",
    html.getAttribute("data-theme"));

  // 运行中系统切到深色，应实时响应
  win.__setSystemDark(true);
  check("系统切深色 → 界面实时变深色", html.getAttribute("data-theme") === "dark",
    html.getAttribute("data-theme"));

  win.__setSystemDark(false);
  check("系统切浅色 → 界面实时变浅色", html.getAttribute("data-theme") === "light",
    html.getAttribute("data-theme"));

  // 显式选深色后，不应再被系统变化带跑
  api.applyTheme("dark");
  win.__setSystemDark(false);
  check("已显式选深色 → 系统变浅也不跟随", html.getAttribute("data-theme") === "dark",
    html.getAttribute("data-theme"));
}

// ---------- 3. 设置面板：预览 + 取消还原 ----------
console.log("\n[3] 设置面板预览与取消还原");
{
  const { win } = buildDom(true);
  const api = loadApp(win);
  const html = win.document.documentElement;

  // 模拟后端：返回已保存的深色配置
  const saved = { theme: "dark" };
  let savePayload = null;
  win.pywebview = {
    api: {
      get_config: () => Promise.resolve({
        ok: true,
        config: { api_key: "***", theme: saved.theme },
        config_path: "config.json",
        warnings: [],
      }),
      save_config: (data) => {
        savePayload = data;
        return Promise.resolve({ ok: true, api_key_set: true, message: "已保存" });
      },
      list_windows: () => Promise.resolve([]),
      get_alert_history: () => Promise.resolve({ ok: true, records: [], total: 0 }),
    },
  };

  // 进入设置（原本是深色）
  api.applyTheme("dark");
  api.openSettings();
  check("进入设置时主题不变", html.getAttribute("data-theme") === "dark");

  // 用户点「浅色」应立刻预览
  const lightRadio = [...api.els.themeRadios].find((r) => r.value === "light");
  lightRadio.checked = true;
  lightRadio.dispatchEvent(new win.Event("change"));
  check("选浅色 → 立刻预览为浅色", html.getAttribute("data-theme") === "light",
    html.getAttribute("data-theme"));

  // 点取消 → 还原
  api.closeSettings(true);
  check("点取消 → 还原为进入前的深色", html.getAttribute("data-theme") === "dark",
    html.getAttribute("data-theme"));
  check("取消后 currentTheme 回到 dark", api.currentTheme() === "dark", api.currentTheme());
}

// ---------- 4. 保存主题 ----------
console.log("\n[4] 保存主题到配置");
{
  const { win } = buildDom(true);
  const api = loadApp(win);
  let savePayload = null;
  win.pywebview = {
    api: {
      get_config: () => Promise.resolve({
        ok: true, config: { api_key: "***", theme: "auto" }, warnings: [],
      }),
      save_config: (data) => {
        savePayload = data;
        return Promise.resolve({ ok: true, api_key_set: true, message: "已保存" });
      },
      list_windows: () => Promise.resolve([]),
      get_alert_history: () => Promise.resolve({ ok: true, records: [], total: 0 }),
    },
  };

  // 必须先走一遍后端就绪流程：app.js 内部的 api 变量
  // 是在 waitForBackend 里赋值的，未初始化时 saveSettings 会直接 return。
  await api.bootWithBackend();

  api.openSettings();
  const lightRadio = [...api.els.themeRadios].find((r) => r.value === "light");
  lightRadio.checked = true;
  lightRadio.dispatchEvent(new win.Event("change"));

  await api.saveSettings();
  check("保存时携带了 theme 字段", savePayload && "theme" in savePayload,
    JSON.stringify(savePayload && savePayload.theme));
  check("保存的主题值为 light", savePayload && savePayload.theme === "light",
    JSON.stringify(savePayload && savePayload.theme));
}

// ---------- 5. 主题下发事件 ----------
console.log("\n[5] Python 端主题下发（apply_theme 事件）");
{
  const { win } = buildDom(true);
  const api = loadApp(win);
  const html = win.document.documentElement;

  // 模拟 Python 在页面加载后下发保存的浅色主题
  win.monitorBridge.onStatus({ status: "apply_theme", detail: { theme: "light" } });
  check("apply_theme 事件 → 界面变浅色", html.getAttribute("data-theme") === "light",
    html.getAttribute("data-theme"));

  win.monitorBridge.onStatus({ status: "apply_theme", detail: { theme: "dark" } });
  check("下发 dark → 界面变深色", html.getAttribute("data-theme") === "dark",
    html.getAttribute("data-theme"));

  // 非法值不应让界面崩掉
  win.monitorBridge.onStatus({ status: "apply_theme", detail: { theme: "蓝色" } });
  check("非法主题值 → 回退处理不崩溃", ["light", "dark"].includes(html.getAttribute("data-theme")),
    html.getAttribute("data-theme"));
}

// ---------- 6. 提示文案 ----------
console.log("\n[6] 主题提示文案");
{
  const { win } = buildDom(true);
  const api = loadApp(win);

  api.applyTheme("auto");
  const autoHint = api.els.themeHint.textContent;
  check("auto 提示含「跟随系统」", autoHint.includes("跟随系统"), autoHint);
  check("auto 提示反映当前实际主题", /深色|浅色/.test(autoHint), autoHint);

  api.applyTheme("light");
  check("light 提示为「浅色」", api.els.themeHint.textContent === "浅色",
    api.els.themeHint.textContent);

  api.applyTheme("dark");
  check("dark 提示为「深色」", api.els.themeHint.textContent === "深色",
    api.els.themeHint.textContent);
}

// ---------- 7. 单选框状态同步 ----------
console.log("\n[7] 设置面板单选框回显");
{
  const { win } = buildDom(true);
  const api = loadApp(win);

  for (const v of ["auto", "light", "dark"]) {
    api.applyTheme(v);
    const checked = [...api.els.themeRadios].filter((r) => r.checked).map((r) => r.value);
    check(`applyTheme("${v}") → 单选框仅选中 ${v}`,
      checked.length === 1 && checked[0] === v, checked.join(","));
  }
}

console.log("\n" + "=".repeat(66));
if (failures === 0) {
  console.log(`全部通过 ✓`);
} else {
  console.log(`失败 ${failures} 项 ✗`);
}
console.log("=".repeat(66));
process.exit(failures === 0 ? 0 : 1);
}

main();
