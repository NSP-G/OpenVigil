/* ============================================================
   Vigil · 前端逻辑
   通过 pywebview 的 window.pywebview.api 与 Python 后端通信
   ============================================================ */
(function () {
  "use strict";

  // 注意：不能在脚本顶层直接取 window.pywebview.api。
  // pywebview 是在「导航完成之后」才注入 window.pywebview 的，
  // 此处立即取值必然为 undefined，会被误判成「后端未连接」。
  // 真正的赋值在 waitForBackend() 里，等 pywebviewready 事件后再进行。
  var api = null;

  var state = {
    windows: [],
    selected: null,        // {hwnd, title}
    // 捕获源：window（窗口句柄）| region（屏幕区域）
    // region 是窗口枚举的兜底通道——监控软件无标题栏时列表里找不到，
    // 但画面在屏幕上可见，框选即可，不依赖枚举结果。
    source: "window",
    region: null,          // {left, top, width, height} 屏幕坐标
    screenRect: null,      // 当前预览对应的显示器矩形，用于坐标换算
    running: false,
    selftestRunning: false,
    activeTab: "control",
    theme: "auto",
    stats: { frames: 0, calls: 0, skips: 0, alerts: 0 }
  };

  var els = {
    // ---- 捕获源 ----
    sourceSwitch: document.getElementById("sourceSwitch"),
    srcWindow: document.getElementById("srcWindow"),
    srcRegion: document.getElementById("srcRegion"),
    monitorSelect: document.getElementById("monitorSelect"),
    btnGrabScreen: document.getElementById("btnGrabScreen"),
    btnDiagnose: document.getElementById("btnDiagnose"),
    regionPicker: document.getElementById("regionPicker"),
    regionCanvas: document.getElementById("regionCanvas"),
    screenPreview: document.getElementById("screenPreview"),
    regionMarquee: document.getElementById("regionMarquee"),
    regionReadout: document.getElementById("regionReadout"),
    btnClearRegion: document.getElementById("btnClearRegion"),

    winList: document.getElementById("winList"),
    winFilter: document.getElementById("winFilter"),
    chkHidden: document.getElementById("chkHidden"),
    btnRefresh: document.getElementById("btnRefresh"),
    btnStart: document.getElementById("btnStart"),
    btnStop: document.getElementById("btnStop"),
    btnTest: document.getElementById("btnTest"),
    selInfo: document.getElementById("selInfo"),
    previewImg: document.getElementById("previewImg"),
    previewEmpty: document.getElementById("previewEmpty"),
    statusDot: document.getElementById("statusDot"),
    statusText: document.getElementById("statusText"),
    eventList: document.getElementById("eventList"),
    eventCount: document.getElementById("eventCount"),
    statFrames: document.getElementById("statFrames"),
    statCalls: document.getElementById("statCalls"),
    statSkips: document.getElementById("statSkips"),
    statAlerts: document.getElementById("statAlerts"),
    btnSettings: document.getElementById("btnSettings"),
    settingsOverlay: document.getElementById("settingsOverlay"),
    btnSettingsClose: document.getElementById("btnSettingsClose"),
    btnSettingsCancel: document.getElementById("btnSettingsCancel"),
    btnSettingsSave: document.getElementById("btnSettingsSave"),
    btnToggleKey: document.getElementById("btnToggleKey"),
    cfgApiKey: document.getElementById("cfgApiKey"),
    cfgModelDaily: document.getElementById("cfgModelDaily"),
    cfgModelRecheck: document.getElementById("cfgModelRecheck"),
    cfgInterval: document.getElementById("cfgInterval"),
    cfgDiffThreshold: document.getElementById("cfgDiffThreshold"),
    cfgAlertConf: document.getElementById("cfgAlertConf"),
    cfgMinAlert: document.getElementById("cfgMinAlert"),
    cfgSaveHint: document.getElementById("cfgSaveHint"),
    cfgKeyHint: document.getElementById("cfgKeyHint"),
    cfgAlertDir: document.getElementById("cfgAlertDir"),

    // ---- 标签页 ----
    monitorTabs: document.getElementById("monitorTabs"),
    monitorTabTitle: document.getElementById("monitorTabTitle"),
    tabControl: document.getElementById("tabControl"),
    tabHistory: document.getElementById("tabHistory"),

    // ---- 告警历史 ----
    historyList: document.getElementById("historyList"),
    historyTotal: document.getElementById("historyTotal"),
    historyPath: document.getElementById("historyPath"),
    historyBadge: document.getElementById("historyBadge"),
    btnHistoryRefresh: document.getElementById("btnHistoryRefresh"),
    btnHistoryClear: document.getElementById("btnHistoryClear"),
    // 历史标签页打开时的自动刷新定时器
    historyTimer: null,

    // ---- 自检 ----
    btnSelfTest: document.getElementById("btnSelfTest"),
    selftestOverlay: document.getElementById("selftestOverlay"),
    btnSelfTestClose: document.getElementById("btnSelfTestClose"),
    btnSelfTestCancel: document.getElementById("btnSelfTestCancel"),
    btnSelfTestRun: document.getElementById("btnSelfTestRun"),
    selftestFull: document.getElementById("selftestFull"),
    selftestStatus: document.getElementById("selftestStatus"),
    selftestSteps: document.getElementById("selftestSteps"),
    selftestSummary: document.getElementById("selftestSummary"),

    // ---- 主题 ----
    themeRadios: document.getElementsByName("theme"),
    themeHint: document.getElementById("themeHint")
  };

  /* ---------- 工具 ---------- */
  /** 安全绑定事件：元素缺失时只记一条警告，不中断后续绑定。
      此前 20 多个监听器是连续裸调 addEventListener 的，
      只要有一个 id 改了名或没渲染出来，就会抛 TypeError，
      导致它后面的**所有**监听器都注册不上——界面半失灵，
      而控制台之外没有任何提示，排查起来极其费劲。 */
  function on(el, evt, handler) {
    if (!el) {
      console.warn("[Vigil] 事件绑定失败：元素不存在", evt);
      return false;
    }
    el.addEventListener(evt, handler);
    return true;
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  /** 读取当前主题下某个 CSS 变量的真实值（供 GSAP 等需要具体色值的场合）。 */
  function cssVar(name) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v || "").trim();
  }
  function nowTime() {
    var d = new Date();
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  function addEvent(msg, type) {
    type = type || "info";
    var item = document.createElement("div");
    item.className = "event-item t-" + type;
    var time = document.createElement("span");
    time.className = "event-time";
    time.textContent = nowTime();
    var text = document.createElement("span");
    text.className = "event-msg";
    text.textContent = msg;
    item.appendChild(time);
    item.appendChild(text);
    els.eventList.appendChild(item);
    var items = els.eventList.querySelectorAll(".event-item");
    els.eventCount.textContent = items.length;
    // 只保留最近 200 条
    while (els.eventList.children.length > 200) {
      els.eventList.removeChild(els.eventList.firstChild);
    }
    els.eventList.scrollTop = els.eventList.scrollHeight;
    return item;
  }

  function setStatus(mode, text) {
    els.statusDot.className = "status-dot" + (mode ? " " + mode : "");
    els.statusText.textContent = text;
  }

  function renderStats() {
    els.statFrames.textContent = state.stats.frames;
    els.statCalls.textContent = state.stats.calls;
    els.statSkips.textContent = state.stats.skips;
    els.statAlerts.textContent = state.stats.alerts;
    // 数字轻微跳动反馈（GSAP，尊重 reduced-motion 由 CSS 处理）
    if (window.matchMedia && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      try {
        gsap.fromTo("#statFrames, #statCalls, #statSkips, #statAlerts",
          { scale: 1.12, transformOrigin: "left center" },
          { scale: 1, duration: 0.25, ease: "power2.out" });
      } catch (e) { /* GSAP 不可用时静默降级 */ }
    }
  }

  /* ---------- 窗口列表 ---------- */
  function renderWindows(filter) {
    filter = (filter || "").trim().toLowerCase();
    var list = state.windows;
    if (filter) {
      list = list.filter(function (w) {
        // 无标题窗口必须也能被搜到——监控软件恰恰没有标题，
        // 只能靠类名或程序名（display 里已兜底）检索
        return (w.display || "").toLowerCase().indexOf(filter) !== -1 ||
               (w.title || "").toLowerCase().indexOf(filter) !== -1 ||
               (w.cls || "").toLowerCase().indexOf(filter) !== -1 ||
               (w.exe || "").toLowerCase().indexOf(filter) !== -1 ||
               String(w.hwnd).indexOf(filter) !== -1;
      });
    }
    els.winList.innerHTML = "";
    if (!list.length) {
      var hint = document.createElement("div");
      hint.className = "empty-hint";
      hint.textContent = filter ? "没有匹配的窗口" : "没有可用的窗口";
      els.winList.appendChild(hint);
      // 空列表是最让人绝望的状态：用户不知道是程序坏了、
      // 还是软件不支持，只能反复点刷新。
      // 这里必须给出明确出路——屏幕区域不依赖枚举，永远可选。
      var alt = document.createElement("button");
      alt.className = "btn-ghost empty-action";
      alt.type = "button";
      alt.textContent = "改用屏幕区域框选 →";
      alt.addEventListener("click", function () { switchSource("region"); });
      els.winList.appendChild(alt);
      if (!filter && els.chkHidden && !els.chkHidden.checked) {
        var alt2 = document.createElement("button");
        alt2.className = "btn-ghost empty-action";
        alt2.type = "button";
        alt2.textContent = "显示隐藏窗口";
        alt2.addEventListener("click", function () {
          els.chkHidden.checked = true;
          refreshWindows();
        });
        els.winList.appendChild(alt2);
      }
      return;
    }
    list.forEach(function (w, i) {
      var item = document.createElement("div");
      item.className = "win-item";
      if (state.source === "window" && state.selected &&
          state.selected.hwnd === w.hwnd) item.classList.add("selected");
      item.dataset.hwnd = w.hwnd;
      item.dataset.title = w.title || "";

      var idx = document.createElement("span");
      idx.className = "win-idx";
      idx.textContent = String(i + 1).padStart(2, "0");

      var title = document.createElement("span");
      title.className = "win-title";
      // 用后端算好的 display：无标题时退化为类名/程序名，
      // 避免列表里出现一堆无法区分的"(无标题)"
      title.textContent = w.display || w.title || "(无标题)";
      title.title = [w.title, w.cls, w.exe].filter(Boolean).join("\n");

      var hwnd = document.createElement("span");
      hwnd.className = "win-hwnd";
      hwnd.textContent = "#" + w.hwnd;

      item.appendChild(idx);
      item.appendChild(title);
      item.appendChild(hwnd);

      // 标出"为什么这个窗口容易被别的工具漏掉"。
      // 监控软件常常同时具备"无标题栏 + 工具窗口 + 隐藏"几个特征，
      // 老师在几十个条目里靠这些标记才能认出目标。
      if (w.flags && w.flags.length) {
        var fl = document.createElement("span");
        fl.className = "win-flags";
        fl.textContent = w.flags.join("·");
        fl.title = "该窗口具备这些特征，因此容易被其他工具过滤";
        item.appendChild(fl);
      }
      if (w.visible === false) {
        var hid = document.createElement("span");
        hid.className = "win-flags win-hidden";
        hid.textContent = "隐藏";
        item.appendChild(hid);
      }

      item.addEventListener("click", function () {
        if (state.running) return;
        state.source = "window";
        state.selected = { hwnd: w.hwnd, title: w.display || w.title || "(无标题)" };
        renderWindows(els.winFilter.value);
        updateSelInfo();
        els.btnStart.disabled = false;
        els.btnTest.disabled = false;
      });
      els.winList.appendChild(item);
    });
  }

  function updateSelInfo() {
    if (state.source === "region" && state.region) {
      var r = state.region;
      els.selInfo.textContent =
        "屏幕区域 " + r.width + "×" + r.height + " @(" + r.left + "," + r.top + ")";
    } else if (state.selected) {
      els.selInfo.textContent = state.selected.title;
    } else {
      els.selInfo.textContent = "未选择监控画面";
    }
  }

  function hasSelection() {
    return state.source === "region"
      ? !!(state.region && state.region.width > 0 && state.region.height > 0)
      : !!state.selected;
  }

  /* ---------- 屏幕区域选择 ---------- */

  function switchSource(src) {
    state.source = src;
    var tabs = els.sourceSwitch
      ? els.sourceSwitch.querySelectorAll(".src-tab") : [];
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle("active", tabs[i].dataset.src === src);
    }
    els.srcWindow.hidden = (src !== "window");
    els.srcRegion.hidden = (src !== "region");
    if (src === "region" && els.monitorSelect &&
        els.monitorSelect.options.length === 0) {
      loadMonitors();
    }
    updateSelInfo();
    // 切换源后按钮可用性要跟着变，否则会出现"选了区域却点不了开始"
    els.btnStart.disabled = state.running || !hasSelection();
    els.btnTest.disabled = state.running || !hasSelection();
  }

  async function loadMonitors() {
    if (!api) return;
    try {
      var mons = await api.list_monitors();
      els.monitorSelect.innerHTML = "";
      if (!mons || !mons.length) {
        els.monitorSelect.innerHTML = '<option value="0">默认显示器</option>';
        return;
      }
      mons.forEach(function (m, i) {
        var opt = document.createElement("option");
        opt.value = String(i);
        opt.textContent = (m.primary ? "主显示器" : "显示器 " + (i + 1)) +
          " · " + m.width + "×" + m.height;
        els.monitorSelect.appendChild(opt);
      });
    } catch (e) {
      addEvent("获取显示器失败：" + e, "error");
    }
  }

  async function grabScreen() {
    if (!api) return;
    var idx = els.monitorSelect ? parseInt(els.monitorSelect.value, 10) : 0;
    els.btnGrabScreen.disabled = true;
    addEvent("正在抓取屏幕…", "info");
    try {
      var res = await api.capture_screen_preview(idx || 0);
      if (!res || res.ok === false) {
        addEvent("抓屏失败：" + ((res && res.error) || "未知原因"), "error");
        return;
      }
      els.screenPreview.src = res.dataUrl;
      // 记下这张预览图对应的真实屏幕矩形，用于把框选坐标换算回去
      state.screenRect = res.screen;
      els.regionPicker.hidden = false;
      addEvent("已抓取屏幕，请在图上拖框选中监控画面", "info");
    } catch (e) {
      addEvent("抓屏失败：" + e, "error");
    } finally {
      els.btnGrabScreen.disabled = false;
    }
  }

  function clearRegion() {
    state.region = null;
    els.regionMarquee.hidden = true;
    els.regionReadout.textContent = "尚未选择区域";
    updateSelInfo();
    els.btnStart.disabled = true;
    els.btnTest.disabled = true;
  }

  function startMarquee(ev) {
    ev.preventDefault();
    if (!state.screenRect) return;
    var canvas = els.regionCanvas;
    var rect = canvas.getBoundingClientRect();
    var x0 = ev.clientX - rect.left;
    var y0 = ev.clientY - rect.top;
    var marquee = els.regionMarquee;

    function onMove(e) {
      var x1 = e.clientX - rect.left;
      var y1 = e.clientY - rect.top;
      var left = Math.max(0, Math.min(x0, x1));
      var top = Math.max(0, Math.min(y0, y1));
      var w = Math.abs(x1 - x0);
      var h = Math.abs(y1 - y0);
      marquee.hidden = false;
      marquee.style.left = left + "px";
      marquee.style.top = top + "px";
      marquee.style.width = w + "px";
      marquee.style.height = h + "px";
    }

    function onUp(e) {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      var x1 = e.clientX - rect.left;
      var y1 = e.clientY - rect.top;
      var px = Math.max(0, Math.min(x0, x1));
      var py = Math.max(0, Math.min(y0, y1));
      var pw = Math.abs(x1 - x0);
      var ph = Math.abs(y1 - y0);
      if (pw < 8 || ph < 8) {
        addEvent("框选区域过小，请重新拖框", "warn");
        return;
      }
      // 预览图是屏幕等比缩放后的结果，必须换算回真实屏幕坐标，
      // 否则抓帧会偏离实际位置（高分屏尤其明显）
      var sr = state.screenRect;
      var sx = pw / rect.width;
      var sy = ph / rect.height;
      var rx = px / rect.width;
      var ry = py / rect.height;
      state.region = {
        left: Math.round(sr.left + rx * sr.width),
        top: Math.round(sr.top + ry * sr.height),
        width: Math.max(1, Math.round(sx * sr.width)),
        height: Math.max(1, Math.round(sy * sr.height)),
      };
      els.regionReadout.textContent =
        state.region.width + "×" + state.region.height +
        " @(" + state.region.left + "," + state.region.top + ")";
      updateSelInfo();
      els.btnStart.disabled = state.running;
      els.btnTest.disabled = state.running;
      addEvent("已选择屏幕区域，可开始巡检", "info");
    }

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  async function runDiagnose() {
    if (!api) return;
    els.btnDiagnose.disabled = true;
    addEvent("正在生成窗口枚举诊断报告…", "info");
    try {
      var res = await api.run_capture_diagnose();
      if (!res || res.ok === false) {
        addEvent("诊断失败：" + ((res && res.error) || "未知原因"), "error");
        return;
      }
      var missed = (res.report && res.report.missed_by_legacy) || [];
      addEvent("诊断完成，报告：" + res.path, "info");
      if (missed.length) {
        // 直接把"被旧逻辑吞掉的窗口"列出来——
        // 老师看到的就是"这个平时不在列表里"，一眼能对上
        addEvent("发现 " + missed.length +
          " 个此前被过滤掉的窗口（可能就是监控软件）", "warn");
        missed.slice(0, 5).forEach(function (w) {
          addEvent("  · " + (w.title || "(无标题)") +
            " · " + (w.cls || "?") +
            (w.exe ? " · " + w.exe.split(/[\\/]/).pop() : ""), "info");
        });
      } else {
        addEvent("未发现被过滤的窗口；若仍找不到，建议改用「屏幕区域」", "info");
      }
    } catch (e) {
      addEvent("诊断失败：" + e, "error");
    } finally {
      els.btnDiagnose.disabled = false;
    }
  }

  async function refreshWindows() {
    if (!api) return;
    try {
      els.winList.innerHTML = '<div class="empty-hint">加载中…</div>';
      var incHidden = !!(els.chkHidden && els.chkHidden.checked);
      state.windows = await api.list_windows(incHidden);
      renderWindows(els.winFilter.value);
    } catch (e) {
      addEvent("刷新窗口失败：" + e, "error");
    }
  }

  if (els.chkHidden) {
    els.chkHidden.addEventListener("change", function () {
      refreshWindows();
    });
  }

  /* ---------- 状态回调（Python → JS） ---------- */
  function onStatus(payload) {
    var status = (payload && payload.status) || "info";
    var detail = (payload && payload.detail) || "";

    switch (status) {
      case "start":
        state.running = true;
        setStatus("running", "巡检中");
        els.btnStart.disabled = true;
        els.btnStop.disabled = false;
        els.btnTest.disabled = true;
        els.previewEmpty.hidden = true;
        els.previewImg.hidden = false;
        addEvent(detail, "start");
        break;
      case "stop":
        state.running = false;
        setStatus("", "已停止");
        els.btnStart.disabled = false;
        els.btnStop.disabled = true;
        els.btnTest.disabled = false;
        addEvent(detail, "info");
        break;
      case "info":
        addEvent(detail, "info");
        break;
      case "error":
        setStatus("alert", "异常");
        addEvent(detail, "error");
        break;
      case "alert":
        state.stats.alerts += 1;
        renderStats();
        setStatus("alert", "有告警");
        addEvent(detail, "warn");
        // 历史栏目若正打开则实时刷新，否则只更新徽标数字
        if (state.activeTab === "history") loadHistory();
        else refreshHistoryBadge();
        // 告警脉冲（GSAP）。颜色从当前主题变量取，
        // 否则切换浅色后这里仍会闪一下深色的告警红。
        if (window.matchMedia && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
          try {
            gsap.fromTo("#statusDot",
              { scale: 1.6, backgroundColor: cssVar("--alert") },
              { scale: 1, duration: 0.6, ease: "power3.out" });
            gsap.fromTo(".event-item.t-warn:last-child",
              { backgroundColor: cssVar("--alert-dim") },
              { backgroundColor: "rgba(0,0,0,0)", duration: 1.2, ease: "power2.out" });
          } catch (e) { /* 降级 */ }
        }
        break;
      case "apply_theme":
        // Python 在页面加载完成后下发保存的主题，
        // 用于在 CSS 就绪的第一时间定色，避免启动闪一下反色。
        applyTheme((detail && detail.theme) || "auto");
        break;
      case "selftest_step":
        appendSelfTestStep(detail || {});
        break;
      case "selftest_done":
        finishSelfTest(detail || {});
        break;
      case "frame":
        if (detail && detail.dataUrl) {
          els.previewImg.src = detail.dataUrl;
        }
        // 用 != null 判断，避免统计值为 0 时被错误保留为旧值
        if (detail.frames != null) state.stats.frames = detail.frames;
        if (detail.calls != null) state.stats.calls = detail.calls;
        if (detail.skips != null) state.stats.skips = detail.skips;
        if (detail.alerts != null) state.stats.alerts = detail.alerts;
        renderStats();
        break;
      default:
        addEvent(detail || status, "info");
    }
  }

  /* ---------- 控制 ---------- */
  async function startMonitor() {
    if (!api || !hasSelection()) return;
    try {
      // 区域模式：hwnd 传 0，区域作为参数。
      // 后端据此改用屏幕抓帧，完全不经过窗口枚举——
      // 这正是"列表里找不到监控软件"时的兜底路径。
      var res = state.source === "region"
        ? await api.start_monitor(0, [
            state.region.left, state.region.top,
            state.region.width, state.region.height])
        : await api.start_monitor(state.selected.hwnd);
      if (res && res.ok === false) {
        addEvent("启动失败：" + (res.error || "未知原因"), "error");
      }
    } catch (e) {
      addEvent("启动失败：" + e, "error");
    }
  }

  async function stopMonitor() {
    if (!api) return;
    try {
      var res = await api.stop_monitor();
      if (res && res.ok === false) {
        addEvent("停止失败：" + (res.error || "未知原因"), "error");
      }
    } catch (e) {
      addEvent("停止失败：" + e, "error");
    }
  }

  async function testOnce() {
    if (!api || !state.selected) return;
    // 分析期间禁用按钮，防止重复点击产生并发调用
    els.btnTest.disabled = true;
    addEvent("正在抓帧分析一次…", "info");
    try {
      var result = await api.test_once(state.selected.hwnd);
      if (!result) {
        addEvent("测试失败：后端无响应", "error");
      } else if (result.ok === false) {
        addEvent(result.error || "测试失败", "error");
      } else if (result.api_error) {
        addEvent("API 调用失败：" + (result.detail || ""), "error");
      } else if (result.abnormal) {
        onStatus({ status: "alert", detail: "测试发现异常：" + (result.detail || result.type || "") });
      } else {
        addEvent("测试完成，未发现异常。", "ok");
      }
    } catch (e) {
      addEvent("测试失败：" + e, "error");
    } finally {
      if (!state.running) els.btnTest.disabled = false;
    }
  }

  /* ---------- 设置面板 ---------- */
  var keyVisible = false;
  var themeBeforeEdit = null;   // 进入设置前的主题，用于取消时还原
  function openSettings() {
    // 记住进入时的主题，点「取消」或「✕」时还原，避免预览后没保存却变了色
    themeBeforeEdit = currentTheme();
    els.settingsOverlay.hidden = false;
    loadSettings();
    if (window.gsap && window.matchMedia && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      try {
        gsap.fromTo("#settingsModal", { scale: 0.96, opacity: 0 }, { scale: 1, opacity: 1, duration: 0.22, ease: "power2.out" });
      } catch (e) {}
    }
  }
  function closeSettings(restore) {
    els.settingsOverlay.hidden = true;
    els.cfgSaveHint.textContent = "";
    // restore=true 表示放弃修改（取消/✕/点遮罩），主题回到进入设置前的状态
    if (restore && themeBeforeEdit !== null) {
      applyTheme(themeBeforeEdit);
      themeBeforeEdit = null;
    }
  }
  async function loadSettings() {
    if (!api) {
      els.cfgSaveHint.textContent = "后端未连接，无法加载配置。";
      return;
    }
    try {
      var res = await api.get_config();
      if (res && res.ok && res.config) {
        var c = res.config;
        els.cfgApiKey.value = c.api_key || "";
        els.cfgApiKey.type = "password";
        keyVisible = false;
        els.btnToggleKey.textContent = "显示";
        els.cfgModelDaily.value = c.model_daily || "glm-4.1v-thinking-flash";
        els.cfgModelRecheck.value = c.model_recheck || "glm-4.1v-thinking-flash";
        els.cfgInterval.value = c.capture_interval_sec != null ? c.capture_interval_sec : 5;
        els.cfgDiffThreshold.value = c.diff_threshold != null ? c.diff_threshold : 0.004;
        els.cfgAlertConf.value = c.alert_confidence != null ? c.alert_confidence : 0.6;
        els.cfgMinAlert.value = c.min_alert_interval_sec != null ? c.min_alert_interval_sec : 60;
        els.cfgAlertDir.value = c.alert_image_dir || "alerts";
        // 主题：以配置为准，未配置（老版本 config.json）时按跟随系统处理
        applyTheme(c.theme || "auto");
        if (res.config_path) {
          els.cfgKeyHint.textContent = "配置文件：" + res.config_path + "（仅保存在本地）";
        }
      }
    } catch (e) {
      els.cfgSaveHint.textContent = "加载配置失败：" + e;
    }
  }
  async function saveSettings() {
    if (!api) return;
    els.cfgSaveHint.textContent = "正在保存…";
    var data = {
      api_key: els.cfgApiKey.value.trim(),
      model_daily: els.cfgModelDaily.value,
      model_recheck: els.cfgModelRecheck.value,
      capture_interval_sec: parseFloat(els.cfgInterval.value) || 5,
      diff_threshold: parseFloat(els.cfgDiffThreshold.value) || 0.004,
      alert_confidence: parseFloat(els.cfgAlertConf.value) || 0.6,
      min_alert_interval_sec: parseFloat(els.cfgMinAlert.value) || 60,
      alert_image_dir: els.cfgAlertDir.value.trim() || "alerts",
      theme: currentTheme()
    };
    try {
      var res = await api.save_config(data);
      if (res && res.ok) {
        els.cfgSaveHint.textContent = res.message || "已保存。";
        addEvent("设置已保存" + (res.api_key_set ? "" : "（尚未填写 API Key）"), "ok");
        // 主题已确认保存，无需还原
        themeBeforeEdit = null;
        setTimeout(function () { closeSettings(false); }, 800);
      } else {
        els.cfgSaveHint.textContent = (res && res.error) || "保存失败。";
      }
    } catch (e) {
      els.cfgSaveHint.textContent = "保存失败：" + e;
    }
  }
  function toggleKeyVisibility() {
    keyVisible = !keyVisible;
    els.cfgApiKey.type = keyVisible ? "text" : "password";
    els.btnToggleKey.textContent = keyVisible ? "隐藏" : "显示";
  }

  /* ---------- 主题 ----------
     mode: "auto"（跟随系统）/ "light" / "dark"
     auto 模式下监听系统偏好变化，实时切换，无需重启。   */
  function systemPrefersDark() {
    return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }

  function resolveTheme(mode) {
    if (mode === "light" || mode === "dark") return mode;
    return systemPrefersDark() ? "dark" : "light";  // auto 或未识别值
  }

  function applyTheme(mode) {
    state.theme = mode || "auto";
    var resolved = resolveTheme(state.theme);
    document.documentElement.setAttribute("data-theme", resolved);
    // 同步原生控件配色（滚动条、下拉箭头等）
    document.documentElement.style.colorScheme = resolved;
    // 同步设置面板里的选中项（面板可能尚未初始化）
    if (els.themeRadios && els.themeRadios.length) {
      for (var i = 0; i < els.themeRadios.length; i++) {
        els.themeRadios[i].checked = (els.themeRadios[i].value === state.theme);
      }
    }
    if (els.themeHint) {
      els.themeHint.textContent = (state.theme === "auto")
        ? ("跟随系统（当前为" + (resolved === "dark" ? "深色" : "浅色") + "）")
        : (resolved === "dark" ? "深色" : "浅色");
    }
  }

  function watchSystemTheme() {
    if (!window.matchMedia) return;
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    var handler = function () {
      // 仅 auto 模式需要对系统变化做出响应
      if (state.theme === "auto") applyTheme("auto");
    };
    if (mq.addEventListener) mq.addEventListener("change", handler);
    else if (mq.addListener) mq.addListener(handler);  // 旧版 WebView2 兼容
  }

  function currentTheme() {
    return state.theme || "auto";
  }

  /* ---------- 标签页切换 ---------- */
  // 告警历史自动刷新间隔（毫秒）。巡检在后台线程写历史，
  // 界面靠这个轮询保证新告警能及时出现，而不是要手动点刷新。
  var HISTORY_POLL_MS = 2000;

  function startHistoryPolling() {
    stopHistoryPolling();
    // 先立即拉一次，保证切过去就能看到最新记录
    loadHistory();
    els.historyTimer = setInterval(loadHistory, HISTORY_POLL_MS);
  }

  function stopHistoryPolling() {
    if (els.historyTimer !== null) {
      clearInterval(els.historyTimer);
      els.historyTimer = null;
    }
  }

  function switchTab(name) {
    state.activeTab = name;
    var tabs = els.monitorTabs.querySelectorAll(".tab");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle("active", tabs[i].dataset.tab === name);
    }
    els.tabControl.hidden = (name !== "control");
    els.tabHistory.hidden = (name !== "history");
    els.monitorTabTitle.textContent = (name === "history") ? "告警历史" : "2 · 巡检控制";
    els.selInfo.hidden = (name === "history");
    // 停在历史页时自动轮询；切走就停掉，避免无谓的读写开销
    if (name === "history") startHistoryPolling();
    else stopHistoryPolling();
  }

  /* ---------- 告警历史 ---------- */
  function confLevel(c) {
    if (c >= 0.75) return "high";
    if (c >= 0.5) return "mid";
    return "low";
  }

  function renderHistory(records) {
    els.historyList.innerHTML = "";
    if (!records || !records.length) {
      var hint = document.createElement("div");
      hint.className = "empty-hint";
      hint.textContent = "暂无告警记录";
      els.historyList.appendChild(hint);
      return;
    }
    records.forEach(function (r) {
      var card = document.createElement("div");
      card.className = "alert-card";
      if (r.local_fallback) card.classList.add("is-local");

      // 缩略图（有截图时可点击放大查看）
      var thumb = document.createElement("div");
      thumb.className = "alert-thumb";
      if (r.image) {
        var img = document.createElement("img");
        // 加时间戳避免同名缓存导致画面不刷新
        img.src = "file:///" + r.image.replace(/\\/g, "/") + "?t=" + encodeURIComponent(r.ts || "");
        img.alt = "异常截图";
        img.loading = "lazy";
        thumb.appendChild(img);
        thumb.title = "点击用系统默认程序打开原图";
        thumb.addEventListener("click", function () { openAlertImage(r.image); });
      } else {
        thumb.textContent = "无截图";
        thumb.classList.add("no-image");
      }

      var main = document.createElement("div");
      main.className = "alert-main";

      var line1 = document.createElement("div");
      line1.className = "alert-line1";
      var typeEl = document.createElement("span");
      typeEl.className = "alert-type";
      typeEl.textContent = r.type || "未分类";
      var confEl = document.createElement("span");
      confEl.className = "alert-conf c-" + confLevel(r.confidence || 0);
      confEl.textContent = "置信度 " + Math.round((r.confidence || 0) * 100) + "%";
      line1.appendChild(typeEl);
      if (r.local_fallback) {
        var lf = document.createElement("span");
        lf.className = "alert-tag";
        lf.textContent = "本地兜底";
        lf.title = "视觉模型未报异常，由本地活动度检测触发";
        line1.appendChild(lf);
      }
      line1.appendChild(confEl);

      var detail = document.createElement("div");
      detail.className = "alert-detail";
      detail.textContent = r.detail || "（无描述）";
      detail.title = r.detail || "";

      var meta = document.createElement("div");
      meta.className = "alert-meta";
      meta.textContent = (r.time || "") + (r.window ? " · 窗口：" + r.window : "");

      main.appendChild(line1);
      main.appendChild(detail);
      main.appendChild(meta);
      card.appendChild(thumb);
      card.appendChild(main);
      els.historyList.appendChild(card);
    });
  }

  async function loadHistory() {
    if (!api) return;
    try {
      var res = await api.get_alert_history(200);
      if (!res || res.ok === false) {
        els.historyList.innerHTML = '<div class="empty-hint">读取失败：'
          + ((res && res.error) || "未知原因") + '</div>';
        return;
      }
      renderHistory(res.records);
      els.historyTotal.textContent = "共 " + (res.total || 0) + " 条";
      els.historyPath.textContent = res.warning
        ? ("历史文件读取异常：" + res.warning)
        : ("历史文件：" + (res.path || ""));
      updateHistoryBadge(res.total || 0);
    } catch (e) {
      els.historyList.innerHTML = '<div class="empty-hint">读取失败：' + e + '</div>';
    }
  }

  async function refreshHistoryBadge() {
    if (!api) return;
    try {
      var res = await api.get_alert_history(1);
      if (res && res.ok) updateHistoryBadge(res.total || 0);
    } catch (e) { /* 徽标刷新失败不影响主流程 */ }
  }

  function updateHistoryBadge(total) {
    if (total > 0) {
      els.historyBadge.hidden = false;
      els.historyBadge.textContent = total > 99 ? "99+" : String(total);
    } else {
      els.historyBadge.hidden = true;
    }
  }

  async function clearHistory() {
    if (!api) return;
    var records = els.historyList.querySelectorAll(".alert-card").length;
    if (records > 0) {
      var ok = window.confirm("确定清空全部 " + records + " 条告警历史？\n（已保存的异常截图文件不会被删除）");
      if (!ok) return;
    }
    try {
      var res = await api.clear_alert_history();
      if (res && res.ok) {
        addEvent(res.message || "告警历史已清空。", "ok");
        await loadHistory();
      } else {
        addEvent("清空失败：" + ((res && res.error) || "未知原因"), "error");
      }
    } catch (e) {
      addEvent("清空失败：" + e, "error");
    }
  }

  async function openAlertImage(path) {
    if (!api) return;
    try {
      var res = await api.open_alert_image(path);
      if (res && res.ok === false) addEvent("打开截图失败：" + (res.error || ""), "warn");
    } catch (e) {
      addEvent("打开截图失败：" + e, "warn");
    }
  }

  /* ---------- 自检 ---------- */
  var STEP_ICON = { pass: "✓", fail: "✕", warn: "!" };
  var STEP_LABEL = { pass: "通过", fail: "未通过", warn: "需注意" };

  function openSelfTest() {
    els.selftestOverlay.hidden = false;
    if (window.gsap && window.matchMedia && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      try {
        gsap.fromTo("#selftestModal", { scale: 0.96, opacity: 0 },
          { scale: 1, opacity: 1, duration: 0.22, ease: "power2.out" });
      } catch (e) {}
    }
  }

  function closeSelfTest() {
    els.selftestOverlay.hidden = true;
  }

  function resetSelfTestView() {
    els.selftestSteps.innerHTML = '<div class="empty-hint">正在自检…</div>';
    els.selftestSummary.hidden = true;
    els.selftestSummary.className = "selftest-summary";
    els.selftestSummary.textContent = "";
  }

  function appendSelfTestStep(step) {
    // 清掉占位提示
    var placeholder = els.selftestSteps.querySelector(".empty-hint");
    if (placeholder) els.selftestSteps.removeChild(placeholder);

    var row = document.createElement("div");
    row.className = "st-step s-" + (step.status || "info");

    var icon = document.createElement("span");
    icon.className = "st-icon";
    icon.textContent = STEP_ICON[step.status] || "·";

    var body = document.createElement("div");
    body.className = "st-body";

    var head = document.createElement("div");
    head.className = "st-head";
    var title = document.createElement("span");
    title.className = "st-title";
    title.textContent = step.title || step.key || "检查项";
    var badge = document.createElement("span");
    badge.className = "st-badge";
    badge.textContent = STEP_LABEL[step.status] || step.status || "";
    head.appendChild(title);
    head.appendChild(badge);

    body.appendChild(head);

    if (step.detail) {
      var detail = document.createElement("div");
      detail.className = "st-detail";
      detail.textContent = step.detail;
      body.appendChild(detail);
    }
    if (step.hint) {
      var hint = document.createElement("div");
      hint.className = "st-hint";
      hint.textContent = "建议：" + step.hint;
      body.appendChild(hint);
    }

    row.appendChild(icon);
    row.appendChild(body);
    els.selftestSteps.appendChild(row);
    els.selftestSteps.scrollTop = els.selftestSteps.scrollHeight;

    if (window.gsap && window.matchMedia && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      try {
        gsap.fromTo(row, { opacity: 0, y: 6 }, { opacity: 1, y: 0, duration: 0.25, ease: "power2.out" });
      } catch (e) {}
    }
  }

  function finishSelfTest(report) {
    state.selftestRunning = false;
    els.btnSelfTestRun.disabled = false;
    els.btnSelfTestRun.textContent = "重新自检";
    els.selftestStatus.textContent = "已完成（耗时 " + (report.elapsed || 0) + "s）";

    els.selftestSummary.hidden = false;
    els.selftestSummary.className = "selftest-summary overall-" + (report.overall || "info");
    els.selftestSummary.textContent = report.summary || "";

    var verb = { pass: "全部通过", warn: "存在需注意项", fail: "发现问题" };
    addEvent("自检完成：" + (verb[report.overall] || report.overall)
      + " —— " + (report.summary || ""), report.overall === "fail" ? "error" :
      (report.overall === "warn" ? "warn" : "ok"));
  }

  async function runSelfTest() {
    if (!api) {
      addEvent("后端未连接，无法自检。", "error");
      return;
    }
    state.selftestRunning = true;
    els.btnSelfTestRun.disabled = true;
    els.selftestStatus.textContent = "正在自检（会调用 API，请稍候）…";
    resetSelfTestView();
    try {
      var res = await api.run_selftest(els.selftestFull.checked);
      if (res && res.ok === false) {
        state.selftestRunning = false;
        els.btnSelfTestRun.disabled = false;
        els.selftestStatus.textContent = "启动失败";
        addEvent("自检启动失败：" + (res.error || "未知原因"), "error");
      }
      // 成功时结果由 selftest_step / selftest_done 事件流推送
    } catch (e) {
      state.selftestRunning = false;
      els.btnSelfTestRun.disabled = false;
      els.selftestStatus.textContent = "启动失败";
      addEvent("自检启动失败：" + e, "error");
    }
  }

  /* ---------- 事件绑定 ---------- */
  on(btnRefresh, "click", refreshWindows);
  on(btnStart, "click", startMonitor);
  on(btnStop, "click", stopMonitor);
  on(btnTest, "click", testOnce);
  on(winFilter, "input", function () { renderWindows(els.winFilter.value); });

  // ---- 捕获源：窗口 <-> 屏幕区域 ----
  if (els.sourceSwitch) {
    var srcTabs = els.sourceSwitch.querySelectorAll(".src-tab");
    for (var si = 0; si < srcTabs.length; si++) {
      srcTabs[si].addEventListener("click", function (e) {
        switchSource(e.currentTarget.dataset.src);
      });
    }
  }
  on(btnDiagnose, "click", runDiagnose);
  on(btnGrabScreen, "click", grabScreen);
  on(btnClearRegion, "click", clearRegion);
  // 框选：在预览图上拖拽画框
  if (els.regionCanvas) {
    els.regionCanvas.addEventListener("mousedown", startMarquee);
  }
  on(btnSettings, "click", openSettings);
  // 关闭/取消均视为放弃修改，主题需还原；保存成功后由 saveSettings 主动置空
  on(btnSettingsClose, "click", function () { closeSettings(true); });
  on(btnSettingsCancel, "click", function () { closeSettings(true); });
  on(btnSettingsSave, "click", saveSettings);
  on(btnToggleKey, "click", toggleKeyVisibility);
  on(settingsOverlay, "click", function (e) {
    if (e.target === els.settingsOverlay) closeSettings(true);
  });
  // 主题：选了就立刻切换，所见即所得
  for (var ti = 0; ti < els.themeRadios.length; ti++) {
    els.themeRadios[ti].addEventListener("change", function (e) {
      applyTheme(e.target.value);
    });
  }
  // 跟随系统模式下，系统切换深浅色时实时响应
  watchSystemTheme();

  // 标签页
  on(monitorTabs, "click", function (e) {
    var tab = e.target.closest ? e.target.closest(".tab") : null;
    if (tab && tab.dataset.tab) switchTab(tab.dataset.tab);
  });
  // 告警历史
  on(btnHistoryRefresh, "click", loadHistory);
  on(btnHistoryClear, "click", clearHistory);
  // 自检
  on(btnSelfTest, "click", openSelfTest);
  on(btnSelfTestRun, "click", runSelfTest);
  on(btnSelfTestClose, "click", closeSelfTest);
  on(btnSelfTestCancel, "click", closeSelfTest);
  on(selftestOverlay, "click", function (e) {
    if (e.target === els.selftestOverlay) closeSelfTest();
  });

  /* ---------- 等待 Python 后端就绪 ----------
     pywebview 在页面导航完成后注入 window.pywebview，并调用 _createApi()
     填充 api，最后派发 'pywebviewready'。因此必须等这个事件，
     否则 api 恒为 null，界面会一直提示「未检测到 Python 后端」。   */
  function backendReady() {
    return !!(
      window.pywebview &&
      window.pywebview.api &&
      Object.keys(window.pywebview.api).length > 0
    );
  }

  function waitForBackend(callback, timeoutMs) {
    timeoutMs = timeoutMs || 10000;
    var start = Date.now();
    var settled = false;
    var timer = null;

    function done(ok) {
      if (settled) return;
      settled = true;
      window.removeEventListener("pywebviewready", onReady);
      if (timer) clearInterval(timer);
      api = ok ? window.pywebview.api : null;
      callback(ok);
    }

    function onReady() { done(backendReady()); }

    if (backendReady()) { done(true); return; }

    window.addEventListener("pywebviewready", onReady);
    // 兜底轮询：万一事件监听注册晚于派发，也能兜住
    timer = setInterval(function () {
      if (backendReady()) done(true);
      else if (Date.now() - start > timeoutMs) done(false);
    }, 50);
  }

  /* ---------- 初始化 ---------- */
  async function boot() {
    waitForBackend(async function (ok) {
      if (!ok) {
        setStatus("", "后端未连接");
        addEvent("未检测到 Python 后端（请通过 python gui/main_gui.py 启动）", "error");
        els.winList.innerHTML = '<div class="empty-hint">后端未连接</div>';
        return;
      }
      await bootWithBackend();
    });
  }

  async function bootWithBackend() {
    await refreshWindows();
    refreshHistoryBadge();  // 不阻塞启动，仅更新徽标数字
    // 检查 API Key，未填写时自动弹出设置面板
    try {
      var cfgRes = await api.get_config();
      if (cfgRes && cfgRes.ok) {
        // 兜底：Python 侧的 loaded 事件若未送达，这里仍能拿到已保存的主题
        applyTheme(cfgRes.config.theme || "auto");
        if (!cfgRes.config.api_key) {
          addEvent("尚未配置 API Key，请在设置中填写。", "warn");
          openSettings();
        } else {
          addEvent("就绪，选择窗口后开始巡检。", "ok");
        }
      } else {
        addEvent("就绪，选择窗口后开始巡检。", "ok");
      }
    } catch (e) {
      addEvent("就绪，选择窗口后开始巡检。", "ok");
    }

    // GSAP 入场编排：仅一次
    if (window.matchMedia && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      try {
        gsap.from("#topbar", { y: -16, opacity: 0, duration: 0.4, ease: "power2.out" });
        gsap.from("#panelWindows", { x: -14, opacity: 0, duration: 0.45, delay: 0.08, ease: "power2.out" });
        gsap.from("#panelMonitor", { x: 14, opacity: 0, duration: 0.45, delay: 0.16, ease: "power2.out" });
        gsap.from("#eventbar", { y: 16, opacity: 0, duration: 0.4, delay: 0.24, ease: "power2.out" });
      } catch (e) { /* 降级 */ }
    }
  }

  // 暴露给 pywebview 的回调注入点
  window.monitorBridge = {
    onStatus: onStatus
  };

  document.addEventListener("DOMContentLoaded", boot);
})();
