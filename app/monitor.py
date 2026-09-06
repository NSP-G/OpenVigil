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

from . import config as config_mod
from . import diff_detect, notifier, window_capture
from .alert_history import AlertHistory
from .local_guard import LocalGuard, SpatialActivityAnalyzer
from .window_capture import WindowClosedError
from .zhipu_client import (
    ZhipuVisionClient,
    ZhipuError,
    _extract_answer,
    is_unclear_reply,
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


class Monitor:
    """Vigil 主巡检器。"""
    def __init__(self, cfg, window_info, verbose=True, preview_cb=None,
                 alert_notify=True):
        self.cfg = cfg
        self.window = window_info
        self.verbose = verbose
        self.preview_cb = preview_cb  # 可选：回调(frame, stats) 用于界面预览
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
        # 本地活动度分析器（时间维度 + 空间结构 + 自适应基线）
        self.analyzer = SpatialActivityAnalyzer(diff_detect.ActivityAnalyzer())
        # 本地兜底守卫：取代旧的「活动度过阈值即告警」判据
        self.guard = LocalGuard(cfg)
        # 异常图片保存目录（相对路径解析到项目根/exe 旁）
        alert_dir = cfg.get("alert_image_dir") or "alerts"
        if not os.path.isabs(alert_dir):
            alert_dir = os.path.join(config_mod._project_root(), alert_dir)
        self.alert_image_dir = alert_dir
        # 告警历史（供界面「告警历史」栏目展示，写入失败不影响巡检）
        history_file = cfg.get("alert_history_file") or "alert_history.json"
        if not os.path.isabs(history_file):
            history_file = os.path.join(config_mod._project_root(), history_file)
        self.history = AlertHistory(history_file)
        self.prev_frame = None
        self.last_alert_ts = 0.0
        self.frame_no = 0
        self._running = False
        # 时序判定历史：记录最近若干帧是否判异常（用于累积置信度与本地兜底）
        self._verdict_history = []
        self._frames_since_analyze = 0
        self.stats = {"frames": 0, "calls": 0, "skips": 0, "alerts": 0}

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

    def stop(self):
        self._running = False

    def close(self):
        """释放底层资源（API 连接池）。"""
        try:
            self.client.close()
        except Exception:
            pass

    def analyze_once(self):
        """立即抓帧并分析一次（供手动触发 / 单次测试用）。"""
        return self._tick(on_status=None, force_analyze=True)

    # ---- 内部实现 ----
    def _tick(self, on_status, force_analyze=False):
        self.frame_no += 1
        self.stats["frames"] = self.frame_no
        frame = window_capture.capture_window(self.window.hwnd)
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
            self._report(on_status, "info", "已抓取首帧基线，开始监测画面变化")
            return {"abnormal": False, "reason": "first_frame", "activity": signals["activity"]}
        self.prev_frame = frame

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
                    primary["detail"] = second.get("detail") or primary.get("detail")
                    primary["type"] = second.get("type") or primary.get("type")
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

        if final_abnormal and final_confidence >= self.alert_confidence:
            self._fire_alert(primary, frame, on_status)
        elif final_abnormal:
            self._report(
                on_status,
                "info",
                f"异常置信度 {final_confidence:.2f} 未达阈值 {self.alert_confidence}，仅记录不告警。",
            )
        return primary

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
        self.stats["calls"] += 1
        base_prompt = self.cfg.get("prompt") or config_mod.DEFAULT_PROMPT
        if is_recheck:
            # 复核定位为「独立第二意见」：不预设结论、不诱导否定，
            # 只要求它独立观察并给出自己的判断。
            prompt = (
                base_prompt
                + "\n\n【独立复核】请忽略任何已有的判断结论，把这当作一次全新的观察，"
                  "独立给出你自己的判断。你的结论允许与他人不同——如果画面里确实能看到"
                  "多人离座走动、聚集、打闹等情形，就如实判为异常；如果画面确实平稳，"
                  "就判为正常。请不要为了让结论与他人一致而修改自己的判断。"
            )
        else:
            prompt = base_prompt
        try:
            text = self.client.analyze(frame, prompt, model=model)
        except ZhipuError as e:
            notifier.get_logger().error("API 调用失败：%s", e)
            # 错误必须透传到前端（GUI 事件流），不能静默吞掉
            self._report(on_status, "error", f"API 调用失败：{e}")
            return {"abnormal": False, "detail": str(e), "api_error": True}
        abnormal, info = parse_verdict(text)
        info["abnormal"] = abnormal
        info["model"] = model
        info["frame_no"] = self.frame_no
        # 「看不清画面」只在模型判正常时才有意义——
        # 判异常本身就说明模型看到了东西，谈不上看不清。
        # 判定前先剥思维链，避免推理中枚举的"无法判断"被当成结论。
        info["unclear"] = (not abnormal) and is_unclear_reply(_extract_answer(text))
        notifier.get_logger().info("模型判定[%s]：%s", model, text)
        return info

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
