/**
 * 捕获源 DOM 层验证（jsdom）
 *
 * 老师实测反馈「监控软件在窗口列表里找不到」。
 * 0.3 已有屏幕区域兜底，但如果界面不给出出路，
 * 用户看到空列表只会反复点刷新，不知道还有别的通道。
 *
 * 本脚本验证"找不到时一定看得到出路"：
 *   1. 存在「显示隐藏窗口」开关
 *   2. 窗口列表为空时渲染出可点击的出路按钮
 *   3. 点击出路能真的切到屏幕区域模式
 *   4. 窗口条目能显示可疑特征标记（无标题时靠它认人）
 *
 * 用法：node tools/capture_check.cjs
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

/** 构建 DOM 并注入 mock 后端，返回导出符号。 */
function buildEnv() {
  const dom = new JSDOM(HTML, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    url: "file:///vigil/index.html",
  });
  const win = dom.window;

  win.matchMedia = () => ({
    media: "", matches: false,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {},
  });
  win.gsap = { fromTo: () => {}, from: () => {} };
  win.confirm = () => true;

  const style = win.document.createElement("style");
  style.textContent = CSS;
  win.document.head.appendChild(style);

  const calls = { list_windows: [] };
  win.pywebview = {
    api: {
      list_windows: (inc) => {
        calls.list_windows.push(inc);
        return [];                      // 默认空列表：模拟"找不到监控软件"
      },
      list_monitors: () => [
        { index: 0, left: 0, top: 0, width: 1920, height: 1080 },
      ],
      capture_screen_preview: () => ({ ok: false, error: "no screen" }),
      get_config: () => ({ ok: true, config: { theme: "auto" } }),
      get_alert_history: () => ({ ok: true, records: [] }),
    },
  };

  const EXPORT = `
    window.__api = {
      state: state, els: els, renderWindows: renderWindows,
      refreshWindows: refreshWindows, switchSource: switchSource,
      boot: boot, addEvent: addEvent
    };
  `;
  const marker = "})();";
  const cut = APP_JS.lastIndexOf(marker);
  win.eval(APP_JS.slice(0, cut) + EXPORT + APP_JS.slice(cut));

  return { win, calls, exported: win.__api };
}

async function boot(env) {
  env.win.document.dispatchEvent(new env.win.Event("DOMContentLoaded"));
  env.win.dispatchEvent(new env.win.Event("pywebviewready"));
  await new Promise((r) => setTimeout(r, 120));
}

async function main() {
  console.log("=".repeat(58));
  console.log("捕获源 DOM 验证");
  console.log("=".repeat(58));

  // ① 隐藏窗口开关
  console.log("\n[1] 显示隐藏窗口开关");
  const env = buildEnv();
  await boot(env);
  check("存在 chkHidden 元素", !!env.win.document.getElementById("chkHidden"));
  check("已注册到 els", env.exported.els.chkHidden != null);

  // ② 空列表给出出路
  console.log("\n[2] 空列表必须给出出路");
  await env.exported.refreshWindows();
  await new Promise((r) => setTimeout(r, 60));
  const list = env.win.document.getElementById("winList");
  const actions = list.querySelectorAll(".empty-action");
  check("渲染出出路按钮", actions.length >= 1,
        "实际 " + actions.length + " 个");
  const texts = Array.from(actions).map((b) => b.textContent);
  check("含「改用屏幕区域」", texts.some((t) => /屏幕区域/.test(t)),
        texts.join(" | "));
  check("含「显示隐藏窗口」快捷入口",
        texts.some((t) => /显示隐藏窗口/.test(t)), texts.join(" | "));

  // ③ 点击真的能切过去
  console.log("\n[3] 点击出路能切到屏幕区域");
  const regionBtn = Array.from(actions).find((b) => /屏幕区域/.test(b.textContent));
  if (regionBtn) {
    regionBtn.dispatchEvent(new env.win.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 60));
    check("已切到 region 源", env.exported.state.source === "region",
          "当前 " + env.exported.state.source);
    const srcRegion = env.win.document.getElementById("srcRegion");
    check("屏幕区域面板可见", srcRegion && srcRegion.hidden === false);
  } else {
    check("找到屏幕区域按钮", false);
  }

  // ④ 勾选隐藏窗口会带上参数重新枚举
  console.log("\n[4] 勾选隐藏窗口后重新枚举");
  const env2 = buildEnv();
  await boot(env2);
  await env2.exported.refreshWindows();
  const before = env2.calls.list_windows.slice();
  check("首次枚举不含隐藏窗口", before.some((v) => v === false || v === undefined),
        JSON.stringify(before));
  const hiddenBtn = Array.from(
    env2.win.document.getElementById("winList").querySelectorAll(".empty-action")
  ).find((b) => /显示隐藏窗口/.test(b.textContent));
  if (hiddenBtn) {
    hiddenBtn.dispatchEvent(new env2.win.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 80));
    check("勾选后以 include_hidden=true 重新枚举",
          env2.calls.list_windows.some((v) => v === true),
          JSON.stringify(env2.calls.list_windows));
  } else {
    check("找到隐藏窗口入口", false);
  }

  // ⑤ 可疑特征标记
  console.log("\n[5] 窗口条目显示可疑标记");
  const env3 = buildEnv();
  await boot(env3);
  env3.exported.state.windows = [{
    hwnd: 99, title: "", cls: "Chrome_WidgetWin_1",
    exe: "C:\\NVR\\client.exe", pid: 42, visible: false,
    display: "[无标题] client.exe", flags: ["toolwindow"],
  }];
  env3.exported.renderWindows("");
  const item = env3.win.document.querySelector(".win-item");
  check("渲染出窗口条目", !!item);
  if (item) {
    check("显示程序名而非(无标题)",
          /client\.exe/.test(item.textContent), item.textContent);
    check("标出 toolwindow 特征", /toolwindow/.test(item.textContent),
          item.textContent);
    check("标出隐藏状态", /隐藏/.test(item.textContent), item.textContent);
  }

  console.log("\n" + "=".repeat(58));
  if (failures === 0) {
    console.log("✓ 全部通过");
  } else {
    console.log(`✗ ${failures} 项失败`);
  }
  console.log("=".repeat(58));
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error("验证脚本崩溃：", e);
  process.exit(2);
});
