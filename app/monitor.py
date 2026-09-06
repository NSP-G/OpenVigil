# -*- coding: utf-8 -*-
"""主巡检循环：抓帧 → 本地活动度分析 → 双意见视觉判定 → 证据融合 → 告警。
Copyright (c) 2026 CaspianFlow. 版权所有。
模型策略（全免费）：日常与复核均使用 glm-4.1v-thinking-flash。

报警系统设计要点（针对"画面很乱却不报告"的漏报问题重构）：
1. 本地活动度先行：瞬时运动 / 相对参考帧漂移 / 近期活跃帧比例三路信号，
   避免"混乱中恰好静止的一帧"被当成无变化直接跳过。
2. 双意见投票取代一票否决：初判报异常后，复核改为「独立第二意见」，
   不再用"看不清就判正常"的批判性提示词去否定初判。
   两次都异常 → 确认；一次异常 → 由本地活动度裁决；两次都正常 → 正常。
3. 时序累积：连续多帧判异常会累加置信度，持续混乱必然越过告警线；
   偶发单帧抖动则被平滑掉，不会误报。
4. 本地兜底告警：模型连续判正常但画面持续剧烈活动时，
   触发"画面持续异常活动，请人工确认"的本地告警，模型失效也不漏。
5. 告警触发与通知节流分离：触发即计数/存图/推事件流，
   Toast 弹窗才按最小间隔节流。
"""
import os
import time
from collections import deque

from . import config as config_mod
from . import diff_detect, notifier, window_capture
from .alert_history import AlertHistory
from .environment import Environment
from .indexer import LogIndexer
from .local_guard import LocalGuard, SpatialActivityAnalyzer
from .memory import EVENT_KINDS, EventTracker, ObservationLog
from .window_capture import WindowClosedError, WindowMinimizedError
from .zhipu_client import (
    ALERT_CATEGORIES,
    CATEGORY_ATTENTION_DROP,
    CATEGORY_EMPTY,
    CATEGORY_GATHERING,
    CATEGORY_HEAD_DOWN,
    CATEGORY_LEAVE_SEAT,
    CATEGORY_NORMAL_CLASS,
    CATEGORY_ORDERLY,
    CATEGORY_SCUFFLE,
    WATCH_CATEGORIES,
    ZhipuVisionClient,
    ZhipuError,
    _extract_answer,
    build_diff_prompt,
    build_observe_prompt,
    is_unclear_reply,
    parse_diff,
    parse_observation,
    parse_verdict,
    _coerce_confidence,
)


# ---- 证据融合相关常量 ----
_AGREEMENT_BONUS = 0.12      # 两次独立判定都异常时的一致性加成
_DISAGREEMENT_PENALTY = 0.15  # 仅一次判异常时，交由活动度裁决前的折扣
_TEMPORAL_BOOST_PER_STREAK = 0.08  # 每多一帧连续异常的置信度加成
_TEMPORAL_BOOST_MAX = 0.25   # 时序加成上限
_ACTIVITY_BOOST_MAX = 0.10   # 本地活动度对置信度的最大加成
_HISTORY_SIZE = 8            # 时序判定历史窗口大小
# 活动度统计至少积累这么多帧，才允许用"画面平稳"去否决模型分歧判定
_MIN_FRAMES_FOR_CALM_RULING = 3

# ---- v0.3 时空逻辑相关常量 ----
_MULTI_FRAME_COUNT = 4       # 多帧分析时喂给模型的帧数（看得出"在聚"还是"在散"）
_SCENE_MIN_INTERVAL = 60.0   # 场景变化检测的最小间隔（秒），避免频繁调用
# 场景变化检测的最小间隔（秒）。配合 SceneChangeDetector 内部的持续性累积，
# 一次真实变化需要连续多轮才会被确认——这是有意的：
# 宁可晚几分钟确认，也不愿把一次路过遮挡当成环境变化。
_POSTURE_MIN_CONFIDENCE = 0.5  # 低于此置信度的姿态观测不写入个体档案

# 朝向一致性判据（区分「聚集围观」与「打闹冲突」）
_FACING_CALM_THRESHOLD = 0.75    # 高于此值：大家在朝同一处看 → 响应性聚集，降权
_FACING_CALM_RELIEF = 0.25       # 响应性聚集的置信度减免
_FACING_CHAOS_THRESHOLD = 0.35   # 低于此值：朝向混乱 → 符合打闹特征，加权
_FACING_CHAOS_BOOST = 0.15

# 个体差异基线：偏离自身常态多少个标准差才算异常
_INDIVIDUAL_DEVIATION_THRESHOLD = 1.8


class Monitor:
    """Vigil 主巡检器。"""
    def __init__(self, cfg, window_info, verbose=True, preview_cb=None,
                 alert_notify=True, root=None):
        self.cfg = cfg
        self.window = window_info
        self.verbose = verbose
        self.preview_cb = preview_cb  # 可选：回调(frame, stats) 用于界面预览
        # 工作目录：缺省用项目根（exe 旁），测试可显式指定以隔离文件写入
        self.root = root or config_mod._project_root()
        # 关闭时仍会计数、存图、记历史，只是不弹系统通知；
        # 自检等内部演练场景需要静默，避免打扰用户。
        self.alert_notify = bool(alert_notify)
        if not cfg.get("api_key"):
            raise ValueError("api_key 为空，无法初始化")
        self.client = ZhipuVisionClient(
            api_key=cfg["api_key"],
            api_base=cfg.get("api_base"),
            model=cfg.get("model_daily"),
        )
        self.model_daily = cfg.get("model_daily") or "glm-4.1v-thinking-flash"
        self.model_recheck = cfg.get("model_recheck") or "glm-4.1v-thinking-flash"
        self.threshold = _safe_float(cfg.get("diff_threshold"), 0.004)
        self.alert_confidence = _safe_float(cfg.get("alert_confidence"), 0.45)
        self.min_alert_interval = _safe_float(cfg.get("min_alert_interval_sec"), 20)
        self.keep_frames = bool(cfg.get("keep_frames", False))
        # 活动度门控：低于该值且未到心跳周期时跳过 API（省钱）
        self.activity_trigger = _safe_float(cfg.get("activity_trigger"), 0.12)
        # 心跳：连续多少帧没做视觉分析就强制分析一次（防慢速变化漏检）
        self.heartbeat_frames = max(1, int(_safe_float(cfg.get("heartbeat_frames"), 20)))
        # 多帧定向确认：仅在判为「聚集/打闹」时，追问人群是在聚拢还是散开。
        # 不作为常规路径——多图会让思考型模型推理链暴涨导致输出截断。
        self.multi_frame_confirm = bool(cfg.get("multi_frame_confirm", True))
        # 多轮复演：初判异常时，让模型从多个角度独立评估再投票。
        # 判断权在模型手里，基础系统只负责安排它怎么看。
        self.deliberation_enabled = bool(cfg.get("deliberation_enabled", True))
        # 必须与日志目录同源。此前这里是裸的相对路径 "memory"，
        # 会落在**当前工作目录**而非 self.root：exe 双击启动时 cwd 可能是
        # 任意位置，记忆文件会散落到奇怪的地方甚至静默丢失。
        memory_dir = cfg.get("memory_root") or "memory"
        if not os.path.isabs(memory_dir):
            memory_dir = os.path.join(self.root, memory_dir)
        self.memory_root = memory_dir
        self.memory_max_chars = int(
            _safe_float(cfg.get("memory_max_chars"), 4000))
        self._deliberation = None
        # 窗口最小化时的轮询间隔（秒）：暂停期间不必高频空转
        self.minimized_poll_interval = max(
            1.0, _safe_float(cfg.get("minimized_poll_interval_sec"), 10.0))
        # 离座走动的持续帧数门槛：低于此值只记录不告警。
        # 交作业、扔垃圾、去讲台拿粉笔都会造成短暂离座，
        # 一帧就告警会把正常课堂搅得不得安宁。
        self.leave_seat_sustain = max(
            1, int(_safe_float(cfg.get("leave_seat_sustain"), 3)))
        # 本地活动度分析器（时间维度 + 空间结构 + 自适应基线）
        self.analyzer = SpatialActivityAnalyzer(diff_detect.ActivityAnalyzer())
        # 本地兜底守卫：取代旧的「活动度过阈值即告警」判据
        self.guard = LocalGuard(cfg)
        # 异常图片保存目录（相对路径解析到项目根/exe 旁）
        alert_dir = cfg.get("alert_image_dir") or "alerts"
        if not os.path.isabs(alert_dir):
            alert_dir = os.path.join(self.root, alert_dir)
        self.alert_image_dir = alert_dir
        # 告警历史（供界面「告警历史」栏目展示，写入失败不影响巡检）
        history_file = cfg.get("alert_history_file") or "alert_history.json"
        if not os.path.isabs(history_file):
            history_file = os.path.join(self.root, history_file)
        self.history = AlertHistory(history_file)

        # ---- 记忆系统（v0.3）----
        # 日志：只增不改的证据链；环境文件：由日志沉淀的可修订知识库
        log_dir = os.path.join(self.root, "memory")
        self.log = ObservationLog(
            log_dir,
            retention_days=int(_safe_float(cfg.get("log_retention_days"), 90)),
        )
        self.env = Environment(os.path.join(self.root, "environment.yml"))
        self.indexer = LogIndexer()
        self.tracker = EventTracker()
        # 场景常态参考帧：用于「差异描述」——给模型两张图问"变了什么"，
        # 比让它从零理解整个教室难度低得多，准确率也高得多
        self._scene_reference = None
        self._scene_detector = diff_detect.SceneChangeDetector()
        # need 必须低于累加器的数学上限，否则条件永远不可满足、功能静默失效。
        # 这个坑真实发生过（need=3.0 vs 上限 2.5），注释里写了却没人检查，
        # 这里在启动时把不可达配置显式暴露出来。
        try:
            if self._scene_detector.need >= self._scene_detector.steady_state_max:
                notifier.get_logger().warning(
                    "场景变化检测器的 need=%.2f 已达到累加器上限 %.2f，"
                    "该条件永远无法满足，场景变化检测将完全失效。",
                    self._scene_detector.need,
                    self._scene_detector.steady_state_max)
        except Exception:
            pass
        self._last_scene_check = 0.0
        # 多帧缓冲：让模型看到「在聚」还是「在散」，而非静态一瞬
        self._frame_buffer = deque(maxlen=_MULTI_FRAME_COUNT)
        # 上次确认过的场景变化，作为上下文注入提示词，
        # 让模型知道聚集可能是响应新贴的通知，而非打闹
        self._recent_change_note = None

        # 知识老化：启动时先跑一次，之后每天一次。
        # 这套机制此前只有测试调用，生产代码从未触发——
        # 于是「防止长期记忆固化成偏见」只是注释里的承诺，
        # 系统跑得越久，早期粗糙判断越被自我强化。
        self._last_aging_ts = 0.0
        try:
            retired = self.env.age_conclusions()
            if retired:
                self.env.flush()
        except Exception:
            pass

        # 抓帧函数可注入。自检在后台线程运行时，若用猴子补丁替换
        # window_capture.capture_window，会连**正在进行的真实巡检**一起污染
        # ——巡检线程会抓到自检的合成画面。改为实例级替换即可彻底隔离。
        self._capture_fn = None
        self.prev_frame = None
        self.last_alert_ts = 0.0
        # 最近一次告警的日志 ID：老师的纠正要挂在这条记录上，
        # 未初始化会导致纠正功能在首次告警前访问时直接抛 AttributeError。
        self._last_alert_id = None
        self.frame_no = 0
        self._running = False
        # 时序判定历史：记录最近若干帧是否判异常（用于累积置信度与本地兜底）
        self._verdict_history = []
        self._frames_since_analyze = 0
        self.stats = {"frames": 0, "calls": 0, "skips": 0, "alerts": 0}

        # 启动时异步重建索引（不阻塞界面与巡检）
        try:
            self.indexer.rebuild_async(self.log)
        except Exception:
            pass

    # ---- 对外接口 ----
    def run_forever(self, stop_event=None, on_status=None):
        """持续巡检，直到 stop_event 被置位或用户 Ctrl+C。
        stop_event: threading.Event（可选），置位即停止。
        on_status: 回调(status: str, detail: str)（可选），用于界面刷新。
        """
        self._running = True
        self._report(on_status, "start", f"开始巡检窗口：{self.window.title}")
        logger = notifier.get_logger()
        interval = max(0.5, _safe_float(self.cfg.get("capture_interval_sec"), 5))
        while self._running:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                self._tick(on_status)
            except WindowClosedError as e:
                self._report(on_status, "error", f"目标窗口已关闭：{e}")
                logger.error("目标窗口已关闭：%s", e)
                self._running = False
                break
            except WindowMinimizedError as e:
                # 最小化时 PrintWindow 只能抓到黑屏。此前这类异常落到通用的
                # except Exception，导致每 5 秒刷一条"巡检出错"却永不停止——
                # 用户最小化窗口去看别处，回来一看满屏报错，系统却还在空转。
                # 正确做法是暂停等待，窗口恢复后自动继续。
                self._report(
                    on_status, "info",
                    f"{e} 已暂停抓取，窗口恢复显示后会自动继续。")
                logger.info("窗口已最小化，暂停抓取：%s", e)
                self._wait_while(stop_event, self.minimized_poll_interval,
                                 lambda: self._still_minimized())
                if stop_event is not None and stop_event.is_set():
                    break
                continue
            except Exception as e:
                self._report(on_status, "error", f"巡检出错：{e}")
                logger.exception("巡检异常")
            # 可被 stop_event 立即唤醒的等待，避免点停止后还要睡满一个间隔
            if stop_event is not None:
                if stop_event.wait(interval):
                    break
            else:
                time.sleep(interval)
        self._report(on_status, "stop", "巡检已停止。")

    def _still_minimized(self):
        """目标窗口是否仍处于最小化状态（查询失败时按已恢复处理）。"""
        try:
            import win32gui
            return bool(win32gui.IsIconic(self.window.hwnd))
        except Exception:
            return False

    @staticmethod
    def _wait_while(stop_event, interval, predicate, max_wait=3600.0):
        """当 predicate 为真时周期性等待，期间可被 stop_event 打断。"""
        waited = 0.0
        interval = max(1.0, float(interval))
        while predicate() and waited < max_wait:
            if stop_event is not None and stop_event.wait(interval):
                return False
            if stop_event is None:
                time.sleep(interval)
            waited += interval
        return True

    def stop(self):
        self._running = False

    def close(self):
        """释放底层资源（API 连接池）。"""
        try:
            self.client.close()
        except Exception:
            pass

    def shutdown(self):
        """停止后台工作线程并落盘，供短生命周期实例（如单次测试）调用。

        只调 close() 不会停掉后台索引线程：它是 daemon，
        进程退出前会一直空转，反复创建 Monitor 就会反复堆积线程。
        """
        try:
            self.indexer.stop_async()
        except Exception:
            pass
        try:
            self.env.flush()
        except Exception:
            pass
        self.close()

    def analyze_once(self):
        """立即抓帧并分析一次（供手动触发 / 单次测试用）。"""
        return self._tick(on_status=None, force_analyze=True)

    # ---- 内部实现 ----
    def _tick(self, on_status, force_analyze=False):
        self.frame_no += 1
        self.stats["frames"] = self.frame_no
        capture = self._capture_fn or window_capture.capture_window
        frame = capture(self.window.hwnd)
        # 推送预览帧（供 GUI 显示实时画面）；回调异常不得影响巡检主循环
        if self.preview_cb is not None:
            try:
                self.preview_cb(frame, dict(self.stats))
            except Exception:
                notifier.get_logger().debug("预览回调异常", exc_info=True)

        # 第一帧：仅建立基线，不调用 API（force_analyze 时跳过，用于单次测试）
        signals = self.analyzer.update(frame)
        if self.frame_no == 1 and not force_analyze:
            self.prev_frame = frame
            # 首帧即作为正常样本纳入基线
            self._maybe_learn_baseline(False, signals, {})
            # 首帧确立场景常态参考（后续据此检测"墙上新贴了东西"这类变化）
            self._scene_reference = frame
            self._scene_detector.update(frame)
            self._report(on_status, "info", "已抓取首帧基线，开始监测画面变化")
            return {"abnormal": False, "reason": "first_frame", "activity": signals["activity"]}
        self.prev_frame = frame

        # 每日一次知识老化 + 过期日志清理。
        # 后者此前从未被调用：日志只增不减，
        # 而"90 天滚动保留"是面向未成年人画像的合规底线。
        try:
            self._maybe_age_conclusions()
            self.log.purge_if_due()
        except Exception:
            pass

        # 场景持久性变化检测（放教学视频、开灯等不影响，只认布局级变化）
        try:
            self._check_scene_change(frame, on_status)
        except Exception as e:
            notifier.get_logger().debug("场景变化检测异常：%s", e)

        # 决定是否值得调用视觉模型：活动度达标 / 强制 / 心跳兜底
        self._frames_since_analyze += 1
        due_heartbeat = self._frames_since_analyze >= self.heartbeat_frames
        worth_analyzing = (
            force_analyze
            or due_heartbeat
            or signals["activity"] >= self.activity_trigger
            or signals["drift"] >= self.activity_trigger
        )
        if not worth_analyzing:
            self.stats["skips"] += 1
            self._verdict_history.append(False)
            self._trim_history()
            # 画面平稳同样要学基线：静止帧正是「本场景正常水平」最典型的样本。
            # 若在此处提前返回而跳过学习，安静教室里基线将永远为空，
            # 后续真混乱时无从比较，本地兜底形同虚设。
            self._maybe_learn_baseline(False, signals, {})
            self._report(
                on_status,
                "info",
                f"[帧{self.frame_no}] 画面平稳（活动度 {signals['activity']:.2f}），跳过 API 调用",
            )
            return {
                "abnormal": False,
                "reason": "low_activity",
                "activity": signals["activity"],
            }
        self._frames_since_analyze = 0

        # ---- 视觉判定：初判 +（必要时）独立二次意见 ----
        self._report(
            on_status, "info",
            f"[帧{self.frame_no}] 活动度 {signals['activity']:.2f}，调用视觉模型判定…",
        )
        primary = self._analyze_frame(frame, self.model_daily, on_status=on_status)
        if primary.get("api_error"):
            # API 故障：不参与时序统计，直接返回，等下一帧重试
            return primary
        if primary.get("unreliable"):
            # 模型回复被截断（多图 + 思考链导致 token 耗尽）时，它等于什么都没说。
            # 这种情况必须当成本帧无效跳过，绝不能走进判异常的路径——
            # 那会在安静的教室里凭空制造告警。
            self._report(on_status, "info",
                         "本帧模型回复不完整（疑似超出长度限制），已跳过，等待下一帧重试。")
            primary["reason"] = "unreliable"
            return primary
        confidence = _coerce_confidence(primary.get("confidence"), 0.5)
        verdict = bool(primary.get("abnormal"))
        agreed = None

        if verdict:
            # 初判异常 → 取独立第二意见（中性提示词，不诱导否定初判）
            self._report(on_status, "info", "初判疑似异常，取独立第二意见复核…")
            second = self._analyze_frame(
                frame, self.model_recheck, is_recheck=True, on_status=on_status
            )
            if second.get("api_error"):
                # 复核失败时不否决初判，按初判结果继续（宁可多提醒，不可漏报）
                self._report(on_status, "info", "复核调用失败，按初判结果处理。")
            else:
                agreed = bool(second.get("abnormal"))
                if agreed:
                    # 两次独立都判异常 → 确认异常，取较高置信度并加一致性加成
                    confidence = min(
                        1.0,
                        max(confidence, _coerce_confidence(second.get("confidence"), 0.5))
                        + _AGREEMENT_BONUS,
                    )
                    # 描述：初判的观测 + 复核的解读，两者互补
                    primary["observation"] = (
                        primary.get("observation") or second.get("observation") or "")
                    primary["description"] = (
                        second.get("description") or primary.get("description") or "")
                    primary["type"] = (second.get("category_label")
                                       or primary.get("category_label")
                                       or primary.get("type"))
                else:
                    # 意见分歧 → 默认保留为「疑似异常」（安全侧），
                    # 只有在画面确实持续平稳、且活动度统计已积累到可信程度时才否决。
                    # 注意：巡检刚开始的前几帧活动度天然为 0，不能用它来否决异常。
                    calm_enough = (
                        self.analyzer.frame_count >= _MIN_FRAMES_FOR_CALM_RULING
                        and signals["activity"] < self.activity_trigger
                    )
                    if calm_enough:
                        confidence = 0.0
                        verdict = False
                        self._report(
                            on_status, "info",
                            f"两次判定不一致，且画面平稳（活动度 {signals['activity']:.2f}），"
                            "判定为正常（误报已拦截）。",
                        )
                    else:
                        confidence = max(0.0, confidence - _DISAGREEMENT_PENALTY)
                        self._report(
                            on_status, "info",
                            f"两次判定不一致，画面活动度 {signals['activity']:.2f}，"
                            "按疑似异常保留。",
                        )
        primary["confidence"] = confidence
        primary["abnormal"] = verdict
        primary["agreed"] = agreed
        primary["activity"] = signals["activity"]

        # ---- 多帧定向确认：只在聚集/打闹类判定上追问"在聚还是在散" ----
        try:
            self._confirm_with_multiframe(frame, primary, on_status)
        except Exception as e:
            notifier.get_logger().debug("多帧确认异常：%s", e)

        # ---- 时空上下文调节：朝向一致性 + 个体差异基线 ----
        confidence = self._apply_context_adjustment(primary, confidence, on_status)
        primary["confidence"] = confidence

        # ---- 时序累积：持续异常会不断抬高置信度，偶发抖动被平滑掉 ----
        confidence = self._apply_temporal_boost(confidence, verdict, signals)
        primary["confidence"] = confidence

        # ---- 本地兜底：模型说正常，但画面相对本场景基线显著异常 ----
        local_alert = self._check_local_guard(verdict, signals, primary, on_status)
        if local_alert is not None:
            primary = local_alert
        # 本帧确认为正常且画面无疑点时，纳入基线样本（混乱帧绝不进入基线）
        self._maybe_learn_baseline(verdict, signals, primary)

        final_abnormal = bool(primary.get("abnormal"))
        final_confidence = _coerce_confidence(primary.get("confidence"), 0.5)

        # ---- 多轮复演：初判异常才开庭 ----
        # 判断权始终在模型手里。基础系统不裁决"该不该报"，
        # 而是安排模型从行为、情境、时序、质疑四个角度各看一遍再投票。
        # 正常场景永远只花 1 次调用，额外成本只出现在需要谨慎的时刻。
        # 本地兜底的特殊性：它的前提就是「模型判正常但画面异常」。
        # 若再拿去让模型投票，等于请那个刚刚漏判的模型否决掉最后一道防线——
        # 兜底将永远不可能生效。因此兜底结果直接进入告警，不参与复演。
        is_local_fallback = bool(primary.get("local_fallback"))
        if (final_abnormal and final_confidence >= self.alert_confidence
                and not is_local_fallback):
            deliberation = self._run_deliberation(primary, frame, on_status)
            if deliberation is not None:
                final_abnormal = deliberation["abnormal"]
                final_confidence = deliberation["confidence"]
                primary["deliberation"] = {
                    "votes": deliberation["votes"],
                    "agreement": deliberation["agreement"],
                    "changed": deliberation["changed"],
                }
                primary["confidence"] = final_confidence
                primary["abnormal"] = final_abnormal
        elif is_local_fallback:
            self._report(on_status, "info",
                         "本次为本地兜底提示，不再交由模型复演（其前提即模型判正常）。")

        # ---- 记忆写入：观测入日志，统计入环境文件 ----
        self._record_observation(primary, signals, frame)

        if final_abnormal and final_confidence >= self.alert_confidence:
            self._fire_alert(primary, frame, on_status)
        elif final_abnormal:
            self._report(
                on_status,
                "info",
                f"异常置信度 {final_confidence:.2f} 未达阈值 {self.alert_confidence}，仅记录不告警。",
            )
        return primary

    def _run_deliberation(self, primary, frame, on_status=None):
        """启动多轮复演，并在结束后推动记忆进化。

        任何一步失败都不影响主流程——复演是增强，不是前提。
        复演挂了就退回初判，绝不能因为复演失败而不告警或乱告警。
        """
        if not self.deliberation_enabled:
            return None
        if self._deliberation is None:
            try:
                from .deliberation import Deliberation
                self._deliberation = Deliberation(
                    self.client, self.model_daily,
                    memory_root=self.memory_root,
                    max_chars=self.memory_max_chars,
                    logger=notifier.get_logger(),
                )
            except Exception as e:
                notifier.get_logger().warning("复演模块初始化失败：%s", e)
                return None

        self._report(on_status, "info", "初判为异常，启动多轮复演（四位评估员独立投票）…")
        try:
            result = self._deliberation.deliberate(
                frame, primary,
                on_report=lambda lvl, msg: self._report(on_status, lvl, msg),
            )
        except Exception as e:
            notifier.get_logger().warning("复演异常，退回初判：%s", e)
            return None

        if result.get("note"):
            self._report(on_status, "info", result["note"])
        if result["changed"]:
            self._report(
                on_status, "info",
                f"复演推翻初判：{'异常' if result['abnormal'] else '正常'}"
                f"（一致度 {result['agreement']:.2f}），已按投票结果处理。")

        # 记忆进化：让模型自己决定这次有没有值得沉淀的东西
        try:
            self._deliberation.evolve_memory(
                result, on_report=lambda lvl, msg: self._report(on_status, lvl, msg))
        except Exception as e:
            notifier.get_logger().debug("记忆进化失败：%s", e)

        return result

    def _record_observation(self, primary, signals, frame=None):
        """把一次判定沉淀到记忆系统。

        三层落点：
          1. 日志：帧级观测（纯事实，不含推断）
          2. 环境文件：个体/区域基线、行为统计（可修订）
          3. 索引：增量更新，供后续查询

        任何一步失败都不影响巡检主流程——记忆是增强，不是前提。
        """
        try:
            category = primary.get("category") or ""
            verdict = bool(primary.get("abnormal"))
            confidence = _coerce_confidence(primary.get("confidence"), 0.5)

            # 1) 帧级日志：只写观测事实
            postures = primary.get("postures") or []
            entities = []
            for p in postures:
                if not isinstance(p, dict):
                    continue
                region = p.get("region")
                if not region:
                    continue
                entities.append({
                    "region": str(region),
                    "posture": p.get("posture"),
                    "facing": p.get("facing"),
                })
            frame_id = self.log.frame(
                entities=entities,
                scene_hash=diff_detect.scene_hash(frame) if frame is not None else None,
                activity=signals.get("activity"),
            )

            # 2) 环境文件：个体基线 + 行为统计
            #    零标注时 region 就是位置编号（"3-2"），系统照样能建立常态
            for p in (postures or []):
                if not isinstance(p, dict) or not p.get("region"):
                    continue
                if confidence < _POSTURE_MIN_CONFIDENCE:
                    continue
                region = str(p["region"])
                # 注意力：低头/趴桌为 0，抬头为 1，其余居中
                posture = str(p.get("posture") or "")
                if posture in ("head_down", "lying"):
                    attention = 0.0
                elif posture in ("head_up",):
                    attention = 1.0
                else:
                    attention = 0.5
                # 只在判为正常时更新基线——混乱期的观测不进基线，
                # 否则打闹久了会被学成常态，反而造成漏报
                self.env.observe_posture(
                    region, posture, attention=attention, is_normal=not verdict)

            # 3) 事件追踪：聚集/打闹等有生命周期，持续时长本身就是判据
            if category in (CATEGORY_GATHERING, CATEGORY_SCUFFLE,
                            CATEGORY_LEAVE_SEAT, CATEGORY_ATTENTION_DROP):
                region = None
                for p in (postures or []):
                    if isinstance(p, dict) and p.get("region"):
                        region = str(p["region"])
                        break
                track = self.tracker.update(
                    kind=category, region=region, present=verdict,
                    confidence=confidence,
                    detail=primary.get("observation") or "",
                )
                # 回写到判定结果：告警环节要靠持续时长区分
                # 「短暂离座交作业」与「全班下位乱跑」
                ev = track.get("event")
                primary["event_status"] = track.get("status")
                primary["event_hits"] = (ev or {}).get("hits", 1)
                primary["event_duration_sec"] = (ev or {}).get("duration_sec")
                # 事件结束时写入日志，带上完整生命周期信息
                if track["status"] == "ended" and ev:
                    self.log.event(
                        kind=category, region=region,
                        evidence_frame_ids=[frame_id],
                        duration_sec=ev.get("duration_sec"),
                        detail=ev.get("detail"),
                        confidence=ev.get("peak_confidence"),
                    )
                    if region:
                        self.env.count_region_event(region, category)

            # 4) 增量索引
            self.indexer.index_record({
                "id": frame_id, "level": "frame",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "entities": entities, "region": None,
            })
            self.env.bump_stat("frames")
        except Exception as e:
            # 记忆写入失败不能拖垮巡检，但必须留下可见痕迹——
            # 否则会出现"系统看起来在跑，实际什么都没记住"的静默失败。
            notifier.get_logger().warning("记忆写入异常：%s", e, exc_info=True)

    def _confirm_with_multiframe(self, frame, primary, on_status=None):
        """对「聚集/打闹」类判定做多帧定向确认。

        单帧只能看到"一群人凑在一起"，看不出是在聚拢还是散开——
        而这正是区分「围观新贴的通知」与「打闹冲突」的关键。
        所以只在已经判为这类类别时，才额外花一次调用问"在聚还是在散"。

        返回 True 表示确认结果已并入 primary。
        """
        if not self.multi_frame_confirm:
            return False
        category = primary.get("category")
        if category not in (CATEGORY_GATHERING, CATEGORY_SCUFFLE):
            return False
        frames = self._collect_frames(frame)
        if len(frames) < 2:
            return False

        prompt = (
            "以下是同一场景按时间先后排列的 %d 帧画面。\n"
            "请重点判断：画面中的人群是在**逐渐聚拢**，还是在**逐渐散开**？\n"
            "如果大家朝同一方向观看、位置趋于稳定，那是响应性聚集；\n"
            "如果相互推挤、位置快速互换、朝向杂乱，那是打闹冲突。\n\n"
            "只输出 JSON：\n"
            '{"moving":"gathering/dispersing/stable",'
            '"facing_consistency":0.0到1.0之间的数字,'
            '"category":"gathering/scuffle/other",'
            '"confidence":0.0到1.0之间的数字}'
        ) % len(frames)
        try:
            text = self.client.analyze_multi(frames, prompt, model=self.model_daily)
        except ZhipuError as e:
            self._report(on_status, "info", f"多帧确认调用失败，沿用单帧结论：{e}")
            return False

        info = parse_observation(text)
        if info.get("unreliable"):
            # 确认失败不影响原结论，多帧只是增强而非前提
            self._report(on_status, "info", "多帧确认回复不完整，沿用单帧结论。")
            return False

        facing = info.get("facing_consistency")
        if facing is not None:
            primary["facing_consistency"] = _safe_float(facing, 0.5)
        new_cat = info.get("category")
        if new_cat in (CATEGORY_GATHERING, CATEGORY_SCUFFLE):
            primary["category"] = new_cat
            # info["category_label"] 在解析失败时可能是 None，
            # 直接赋值会把原有的可读标签抹成 None，界面上一片空白。
            new_label = info.get("category_label")
            if new_label:
                primary["category_label"] = new_label
        self._report(
            on_status, "info",
            f"多帧确认完成（朝向一致度 {primary.get('facing_consistency')}，"
            f"类别 {primary.get('category_label')}）。")
        return True

    def _apply_context_adjustment(self, primary, confidence, on_status=None):
        """用时空上下文调节置信度。

        两条判据：

        ① 朝向一致性——区分「聚集围观」与「打闹冲突」的最强特征。
           所有人朝向一致（都朝墙、朝黑板）→ 注意力集中，是响应性聚集；
           朝向混乱、位置快速互换 → 才是真混乱。
           这个特征在低分辨率下依然稳，因为它只需要人群的整体矢量方向。

        ② 个体差异基线——相对**自己的常态**判异常，而非全局阈值。
           李雷本来就爱低头，低头就不算异常；
           王芳一向专注突然趴下，才是告警。
           注意：这不依赖认人——位置本身也有自己的历史常态。
        """
        category = primary.get("category")
        adjusted = confidence

        # ① 朝向一致性
        facing = primary.get("facing_consistency")
        if facing is not None:
            facing = _safe_float(facing, 0.5)
            primary["facing_consistency"] = facing
            if category in (CATEGORY_GATHERING, CATEGORY_SCUFFLE):
                if facing >= _FACING_CALM_THRESHOLD:
                    # 朝向高度一致 → 大家在朝同一处看，是响应性聚集
                    adjusted = max(0.0, adjusted - _FACING_CALM_RELIEF)
                    self._report(
                        on_status, "info",
                        f"人群朝向一致（{facing:.2f}），倾向响应性聚集而非混乱，"
                        f"置信度下调至 {adjusted:.2f}。",
                    )
                elif facing <= _FACING_CHAOS_THRESHOLD:
                    # 朝向混乱 → 符合打闹特征，适度上调
                    adjusted = min(1.0, adjusted + _FACING_CHAOS_BOOST)
                    self._report(
                        on_status, "info",
                        f"人群朝向混乱（{facing:.2f}），符合打闹特征，"
                        f"置信度上调至 {adjusted:.2f}。",
                    )

        # ② 个体差异基线：找出偏离自身常态最远的位置
        worst = None
        for p in (primary.get("postures") or []):
            if not isinstance(p, dict) or not p.get("region"):
                continue
            region = str(p["region"])
            posture = str(p.get("posture") or "")
            attention = 0.0 if posture in ("head_down", "lying") else (
                1.0 if posture == "head_up" else 0.5)
            dev = self.env.observe_posture(
                region, posture, attention=attention,
                is_normal=not bool(primary.get("abnormal")))
            # 负偏离 = 比平时差；只有明显变差才值得提示
            if dev <= -_INDIVIDUAL_DEVIATION_THRESHOLD:
                if worst is None or dev < worst[1]:
                    worst = (region, dev, posture)

        if worst:
            region, dev, posture = worst
            primary["deviating_region"] = region
            primary["deviation"] = round(dev, 2)
            # 个体偏离不直接抬高告警置信度（那会造成对特定学生的偏见），
            # 而是作为描述的一部分，让老师知道"这个人今天不对劲"
            note = "%s 较其平时状态明显异常（偏离 %.1f 个标准差，当前：%s）" % (
                region, abs(dev), posture)
            primary["individual_note"] = note
            self.env.note_behavior(region, note, confidence=0.5)

        return adjusted

    def _apply_temporal_boost(self, confidence, verdict, signals):
        """把当前判定并入时序历史，并按连续异常帧数加成置信度。"""
        self._verdict_history.append(bool(verdict))
        self._trim_history()
        if not verdict:
            return confidence
        # 统计历史窗口末尾的连续异常帧数
        streak = 0
        for v in reversed(self._verdict_history):
            if v:
                streak += 1
            else:
                break
        boost = min(_TEMPORAL_BOOST_MAX, max(0, streak - 1) * _TEMPORAL_BOOST_PER_STREAK)
        activity_boost = _ACTIVITY_BOOST_MAX * signals["activity"]
        return min(1.0, confidence + boost + activity_boost)

    def _check_local_guard(self, verdict, signals, primary, on_status):
        """本地兜底守卫：判断「模型说正常」是否可信。

        旧实现用「活动度 >= 固定阈值」判乱，实测正常场景与混乱场景的活动度
        完全重叠（0.47~0.98 vs 0.95），调任何阈值都必然误报。
        新实现改用空间结构 + 光照排除 + 相对基线三重过滤，
        并随模型确信度反向调节门槛。详见 app/local_guard.py。
        """
        # 模型看不清画面 ≠ 画面很乱：做信息性提示，不伪装成告警
        if not verdict and primary.get("unclear"):
            self._report(
                on_status, "info",
                "模型表示看不清画面，请检查监控画面质量"
                "（分辨率过低、模糊或遮挡会影响判定）。",
            )
        model_confidence = _coerce_confidence(primary.get("confidence"), 0.5)
        model_clear = not bool(primary.get("unclear"))
        result = self.guard.evaluate(
            verdict=verdict,
            signals=signals,
            model_confidence=model_confidence,
            model_clear=model_clear,
        )
        if result is not None:
            self._report(
                on_status, "info",
                f"本地检测到画面相对本场景正常水平显著异常"
                f"（活动度 {result['activity']:.2f}，偏离基线 "
                f"{result['activity_z']:.1f} 个标准差），模型判为正常，"
                f"已给出待确认提示。",
            )
        return result

    def _maybe_learn_baseline(self, verdict, signals, primary):
        """把确认为正常的帧纳入基线，供后续 z-score 判定使用。

        只收「模型判正常且看得清」且「当前帧本身不异常」的样本，
        避免混乱期的帧污染基线、把打闹学成常态。
        """
        if verdict or primary.get("unclear") or primary.get("api_error"):
            return
        z = _safe_float(signals.get("activity_z"), 0.0)
        if z >= 1.5:
            return  # 本帧已偏离常态，不纳入基线
        self.analyzer.learn_baseline(signals.get("activity", 0.0))

    def _trim_history(self):
        if len(self._verdict_history) > _HISTORY_SIZE:
            self._verdict_history = self._verdict_history[-_HISTORY_SIZE:]

    def _analyze_frame(self, frame, model, is_recheck=False, on_status=None):
        """调用视觉模型做一次结构化观测。

        v0.3 关键升级：先描述再判断 + 类别选择替二值。

        多帧输入不作为常规路径：实测发现面对多张图时，思考型模型的推理链
        会暴涨并撞上 token 上限，输出被截断在半句上，解析失败后落到兜底逻辑，
        反而让安静的教室产生误报。改为仅在判定结果为「聚集/打闹」这类
        需要看变化方向时，才额外做一次多帧定向确认。
        """
        self.stats["calls"] += 1
        base_prompt = self.cfg.get("prompt") or config_mod.DEFAULT_PROMPT

        # 场景上下文：告诉模型这个场景平时什么样、最近确认过哪些变化。
        # 这是让模型能区分「响应性聚集」与「混乱」的关键信息。
        prompt = build_observe_prompt(
            base_prompt,
            is_recheck=is_recheck,
            scene_context=self._scene_context_text(),
            recent_changes=self._recent_change_note,
        )

        # 默认走单帧。多帧不作为常规路径——实测面对多张图时思考型模型的
        # 推理链会暴涨，输出常被截断，反而导致安静教室被误报。
        # 多帧只在需要判断"人群是在聚拢还是散开"时作为**定向确认**使用。
        try:
            text = self.client.analyze(frame, prompt, model=model)
        except ZhipuError as e:
            notifier.get_logger().error("API 调用失败：%s", e)
            # 错误必须透传到前端（GUI 事件流），不能静默吞掉
            self._report(on_status, "error", f"API 调用失败：{e}")
            return {"abnormal": False, "detail": str(e), "api_error": True}

        info = parse_observation(text)
        info["model"] = model
        info["frame_no"] = self.frame_no
        # 兼容旧字段：下游（历史、通知、告警）仍在用 type/detail
        info["type"] = info.get("category_label") or "未分类"
        info["detail"] = info.get("description") or info.get("observation") or ""

        # 「看不清画面」只在模型判正常时才有意义——
        # 判异常本身就说明模型看到了东西，谈不上看不清。
        # 判定前先剥思维链，避免推理中枚举的"无法判断"被当成结论。
        info["unclear"] = (not info["abnormal"]) and is_unclear_reply(
            _extract_answer(text))
        notifier.get_logger().info("模型观测[%s]：%s", model, text)
        return info

    def _collect_frames(self, current):
        """收集用于多帧分析的帧序列（按时间先后）。

        单帧里的「聚集」是静态名词，多帧里的「聚集」是有方向的动词——
        只有看到多帧，模型才能判断人群是在往一起聚还是正在散开。
        """
        self._frame_buffer.append(current)
        if len(self._frame_buffer) < 2:
            return [current]
        return list(self._frame_buffer)

    # ---- 场景变化检测 ----
    def _check_scene_change(self, frame, on_status=None):
        """检测环境的持久性变化（墙上新贴了东西、桌椅挪动等）。

        这是「新贴分组表导致聚集被误判为打闹」的根治手段之一：
        系统先知道场景变了，才能把之后的聚集理解为响应性行为。

        流程：持续性局域变化检测 → 调模型描述变化 → 写入环境文件并注入提示词。
        检测器只负责回答「值得问模型一次吗」，最终裁决交给模型。
        """
        now = time.time()
        if now - self._last_scene_check < _SCENE_MIN_INTERVAL:
            return
        self._last_scene_check = now

        # 首帧确立基准
        if self._scene_reference is None:
            self._scene_reference = frame
            self._scene_detector.update(frame)
            return

        # 告警活跃期不做场景变化检测：此时画面处处在变，
        # 既检测不出有意义的环境变化，又白白消耗一次 API。
        if self._recent_alert_active():
            return

        change = self._scene_detector.update(frame)
        if not change:
            return
        self._describe_scene_change(frame, change, on_status)

    def _maybe_age_conclusions(self, interval_sec=86400.0):
        """每隔约一天跑一次知识老化，避免陈旧结论被无限自我强化。"""
        now = time.time()
        if now - self._last_aging_ts < interval_sec:
            return 0
        self._last_aging_ts = now
        retired = self.env.age_conclusions()
        if retired:
            self.env.flush()
            notifier.get_logger().info("知识老化：%d 条陈旧结论已退休", retired)
        return retired

    def _recent_alert_active(self):
        """最近一帧是否判定为异常（用于跳过混乱期的场景检测）。"""
        return bool(self._verdict_history) and bool(self._verdict_history[-1])

    def _describe_scene_change(self, frame, change, on_status=None):
        """调用模型描述场景变化，并写入环境文件与日志。

        用「差异描述」而非「绝对描述」：给模型常态图 + 当前图，
        问"变了什么"，比让它从零理解整个教室难度低得多。
        """
        if self._scene_reference is None:
            return
        try:
            text = self.client.analyze_diff(
                self._scene_reference, frame, build_diff_prompt(),
                model=self.model_daily,
            )
            changed, changes, conf = parse_diff(text)
        except ZhipuError as e:
            notifier.get_logger().warning("场景变化描述失败：%s", e)
            # 调用失败时同样要推进基准。此前只在"模型说没变"时才推进，
            # 一旦 API 连续不可用，检测器会每 60 秒重复提交同一处变化，
            # 白白消耗调用次数，而基准始终停在旧状态。
            self._scene_detector.reset_reference(frame)
            self._scene_reference = frame
            return
        except Exception as e:
            notifier.get_logger().warning("场景变化描述异常：%s", e)
            self._scene_detector.reset_reference(frame)
            self._scene_reference = frame
            return

        if not changed or not changes:
            # 模型说没变 → 检测器误触发，重置基准避免反复调用。
            # 这是自我纠正：检测器宁可多触发一次，最终裁决交给模型。
            self._scene_detector.reset_reference(frame)
            self._scene_reference = frame
            return

        note = "；".join(changes)
        # 必须把当前场景指纹传进去，否则环境文件里记不下这次变化
        self.env.confirm_scene_change(
            note, confidence=conf,
            scene_hash=diff_detect.scene_hash(frame) or diff_detect.scene_hash(
                self._scene_reference),
        )
        self._recent_change_note = note
        # 事件级日志：场景变化本身就是要记录的事件
        self.log.event(
            kind="scene_change", region=None,
            detail=note, confidence=conf,
        )
        self.indexer.index_record({
            "id": "sc-%s" % time.strftime("%Y%m%d%H%M%S"),
            "level": "event", "kind": "scene_change",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "detail": note, "region": None,
        })
        self.env.flush()
        self._report(
            on_status, "info",
            f"检测到场景变化：{note}（后续聚集将据此判别是否为响应性行为）")

        # 基准推进到当前状态，避免同一变化被反复描述
        self._scene_detector.reset_reference(frame)
        self._scene_reference = frame

    def _scene_context_text(self):
        """生成场景常态描述，注入提示词供模型对比参考。"""
        changes = self.env.recent_scene_changes(3)
        if not changes:
            return None
        parts = []
        for c in changes:
            desc = c.get("description")
            if desc:
                parts.append(str(desc))
        if not parts:
            return None
        return "；".join(parts[-3:])

    def _fire_alert(self, info, frame=None, on_status=None):
        now = time.time()
        etype = info.get("type", "未分类") or "未分类"
        detail = info.get("detail", "") or ""
        confidence = _coerce_confidence(info.get("confidence"), 0.5)
        # 每次确认异常都计数（与前端 alert 事件保持一致），通知节流只控制 Toast
        self.stats["alerts"] += 1
        # 每次异常都保存当前帧（不受最小通知间隔限制），便于事后回溯
        image_path = self._save_alert_frame(etype, frame)
        notifier.get_logger().warning(
            "告警触发[%s] confidence=%.2f：%s", etype, confidence, detail
        )
        # 记入告警历史，供界面「告警历史」栏目展示
        try:
            self.history.add(
                etype, detail, confidence,
                image_path=image_path,
                window_title=getattr(self.window, "title", ""),
                local_fallback=bool(info.get("local_fallback")),
            )
        except Exception as e:
            notifier.get_logger().warning("写入告警历史失败：%s", e)

        # 记入观测日志（alert 级）：observation 与 description 分开存，
        # 前者是纯观测事实，后者是面向老师的推断解读。
        # 这条链路让每条告警都能追溯到原始观测，也是老师纠正的锚点。
        try:
            # 把本轮已结束的事件 ID 一并写入，构成「告警→事件→帧」证据链。
            # 此前从不传 event_ids，导致 alert 记录的 events 恒为空数组，
            # 「告警必带证据链」的设计名存实亡。
            event_ids = []
            for ev in self.tracker.recent_finished(5):
                if ev.get("kind"):
                    event_ids.append("%s@%s" % (ev.get("kind"), ev.get("region")))
            self._last_alert_id = self.log.alert(
                event_ids=event_ids,
                category=info.get("category"),
                confidence=confidence,
                observation=info.get("observation") or detail,
                description=info.get("description") or detail,
                notified=bool(self.alert_notify),
            )
            self.indexer.index_record({
                "id": self._last_alert_id, "level": "alert",
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "category": info.get("category"),
                "description": detail, "observation": info.get("observation") or "",
                "region": None,
            })
            self.env.bump_stat("alerts")
        except Exception as e:
            notifier.get_logger().debug("告警日志写入异常：%s", e)
        # 向前端报告告警状态（更新计数、事件流、状态点）
        alert_detail = f"{etype}：{detail}" if detail else etype
        if info.get("local_fallback"):
            alert_detail = f"［本地兜底］{alert_detail}"
        self._report(on_status, "alert", alert_detail)
        # 最小通知间隔：避免短时间内连续弹 Toast 打扰
        if now - self.last_alert_ts < self.min_alert_interval:
            self._report(on_status, "info", "距上次告警过近，本次只记录不重复弹通知。")
            return
        self.last_alert_ts = now
        if not self.alert_notify:
            return  # 静默模式（自检演练等）：记录已留痕，不弹系统通知
        title = f"Vigil 监控告警：{etype}"
        message = f"{detail}\n置信度 {confidence:.0%} · 窗口：{self.window.title}"
        notifier.notify(title, message, image_path=image_path)

    def _save_alert_frame(self, etype, frame=None):
        """把异常帧保存到用户配置的目录，返回图片路径（失败返回 None）。
        文件名：alert_YYYYMMDD_HHMMSS_帧号_异常类型.jpg（帧号防止同秒覆盖）
        """
        try:
            os.makedirs(self.alert_image_dir, exist_ok=True)
            frame = frame or self.prev_frame
            if frame is None:
                return None
            # 异常类型清理为安全文件名
            safe_type = "".join(
                c if c.isalnum() or c in "_-" else "_" for c in (etype or "unknown")
            )[:30]
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(
                self.alert_image_dir,
                f"alert_{ts}_f{self.frame_no:06d}_{safe_type}.jpg",
            )
            frame.save(path, "JPEG", quality=85)
            notifier.get_logger().info("异常帧已保存：%s", path)
            return path
        except Exception as e:
            notifier.get_logger().warning("保存异常帧失败：%s", e)
            return None

    def _report(self, on_status, status, detail):
        logger = notifier.get_logger()
        if self.verbose or status in ("start", "stop", "error"):
            logger.info("%s：%s", status, detail)
        if on_status is not None:
            try:
                on_status(status, detail)
            except Exception:
                logger.debug("状态回调异常", exc_info=True)


def _safe_float(value, default):
    """把配置/模型返回值安全转成 float，失败时用默认值，杜绝脏配置导致崩溃。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
