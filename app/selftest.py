# -*- coding: utf-8 -*-
"""自检模块：一键诊断"画面很乱却不报告"到底卡在哪一环。

Copyright (c) 2026 CaspianFlow. 版权所有。

报警链路有六个环节，任何一个出问题都会表现为"不报警"：
    1. 配置       —— API Key、模型名、告警阈值是否配置正确
    2. 连通性     —— Key 是否有效、模型是否可用、网络是否可达
    3. 模型判定力 —— 模型面对混乱画面会不会如实报异常
    4. 模型克制力 —— 模型面对安静画面会不会保持判正常（不误报）
    5. 本地活动度 —— 画面持续剧烈变化时，本地检测器能不能感知到
    6. 端到端链路 —— 从抓帧到告警的完整决策链，最终会不会真的告警

自检逐项跑这六项，把每一步的原始输出都摊开给用户看，
并针对失败项给出可操作的修复建议。
"""
import os
import shutil
import tempfile
import time
import traceback

from PIL import Image, ImageDraw

from . import config as config_mod
from . import diff_detect, notifier, window_capture
from .monitor import Monitor
from .zhipu_client import ZhipuVisionClient, ZhipuError, parse_verdict

# 端到端自检：先跑多少帧正常画面建立基线，再跑多少帧混乱画面
_E2E_BASELINE_FRAMES = 14
_E2E_FRAMES = 8
# 判定"模型具备判定力"所需的最低置信度
_MIN_USABLE_CONFIDENCE = 0.4


def make_classroom_frame(frame_idx=0, chaos=0.0, seed=0, size=(640, 400)):
    """生成一张模拟教室监控画面（纯本地合成，不上传任何数据）。

    chaos=0   安静自习：学生整齐就座
    chaos>0   混乱程度：学生离座走动；>0.5 时额外出现聚集人群

    说明：这是形态模拟，不等于真实监控画面，
    但足以验证"模型能否从画面里看出混乱"这一判定能力。
    """
    w, h = size
    img = Image.new("RGB", (w, h), (232, 228, 220))
    d = ImageDraw.Draw(img)
    # 讲台与地板，给画面一点结构层次
    d.rectangle([0, 0, w, 60], fill=(205, 200, 190))
    d.rectangle([0, h - 70, w, h], fill=(215, 210, 200))

    state = [(seed * 1000 + frame_idx) or 1]

    def rnd():
        state[0] = (1103515245 * state[0] + 12345) % (2 ** 31)
        return state[0] / (2 ** 31)

    for i in range(24):
        col, row = i % 8, i // 8
        x = 70 + col * 68
        y = 120 + row * 70
        if rnd() < chaos:
            x += int(rnd() * 90) - 45
            y += int(rnd() * 95) - 40
        d.ellipse([x, y, x + 22, y + 22], fill=(60, 55, 50))          # 头
        d.rectangle([x + 2, y + 22, x + 20, y + 48], fill=(70, 90, 140))  # 身体
    if chaos > 0.5:
        for _ in range(int(chaos * 8)):
            x = 300 + int(rnd() * 120) - 60
            y = 250 + int(rnd() * 80) - 40
            d.ellipse([x, y, x + 24, y + 24], fill=(70, 60, 55))
            d.rectangle([x + 2, y + 24, x + 22, y + 52], fill=(150, 70, 70))
    return img


def _clip(text, limit=180):
    """截断长文本，避免模型长回复把界面撑爆。"""
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


class _FakeWindow:
    """自检用的伪窗口对象（不依赖真实窗口句柄）。"""
    def __init__(self):
        self.hwnd = 0
        self.title = "自检·合成画面"
        self.cls_name = "selftest"


class SelfTest:
    """自检执行器。

    用法：
        st = SelfTest(cfg, progress_cb=fn)
        result = st.run()          # 同步跑完，返回结构化报告
    """

    def __init__(self, cfg, progress_cb=None, full=True):
        self.cfg = cfg
        self.progress_cb = progress_cb
        # full=False 时只跑不消耗 API 的本地项 + 一次最小连通性调用
        self.full = bool(full)
        self.steps = []
        self.client = None
        self.started_at = 0.0

    # ---- 主流程 ----
    def run(self):
        """按序执行全部自检项，返回汇总报告。"""
        self.started_at = time.time()
        try:
            # ①② 依赖配置与网络，失败则跳过其后的模型相关项
            if self._step_config() and self._step_connectivity():
                self._full_model_steps()
            # ⑤⑥ 中的本地项不依赖 API 结果，始终执行
            self._step_local_activity()
            self._step_end_to_end()
        except Exception as e:
            notifier.get_logger().exception("自检过程异常")
            self.steps.append({
                "key": "crash",
                "title": "自检执行异常",
                "status": "fail",
                "detail": f"{type(e).__name__}: {e}",
                "hint": "这是程序内部错误，请把 logs 目录下的日志反馈给开发者。",
            })
        finally:
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass

        failed = [s for s in self.steps if s["status"] == "fail"]
        warned = [s for s in self.steps if s["status"] == "warn"]
        overall = "fail" if failed else ("warn" if warned else "pass")
        return {
            "ok": True,
            "overall": overall,
            "elapsed": round(time.time() - self.started_at, 1),
            "steps": self.steps,
            "summary": self._summarize(overall, failed, warned),
        }

    def _summarize(self, overall, failed, warned):
        if overall == "pass":
            return "全部通过。报警链路正常，画面出现混乱时应当会告警。"
        if overall == "fail":
            names = "、".join(s["title"] for s in failed)
            return f"发现 {len(failed)} 项问题（{names}），请按下方建议处理。"
        names = "、".join(s["title"] for s in warned)
        return f"基本可用，但有 {len(warned)} 项需注意（{names}）。"

    # ---- 进度上报 ----
    def _emit(self, step):
        self.steps.append(step)
        if self.progress_cb is not None:
            try:
                self.progress_cb(step)
            except Exception:
                notifier.get_logger().debug("自检进度回调异常", exc_info=True)

    def _mark(self, key, title, status, detail, hint=""):
        self._emit({
            "key": key,
            "title": title,
            "status": status,
            "detail": _clip(detail),
            "hint": hint or "",
        })

    # ---- 各检查项 ----
    def _step_config(self):
        """第 1 项：配置检查（不消耗 API）。"""
        problems = []
        cfg = self.cfg
        if not cfg.get("api_key"):
            problems.append("未填写 API Key")
        conf = _as_float(cfg.get("alert_confidence"), 0.45)
        trigger = _as_float(cfg.get("activity_trigger"), 0.12)
        if conf > 0.8:
            problems.append(f"告警阈值 {conf} 过高，几乎不可能触发（建议 ≤0.6）")
        if trigger > 0.5:
            problems.append(f"活动度门控 {trigger} 过高，画面变化可能长期被跳过（建议 ≤0.3）")
        interval = _as_float(cfg.get("capture_interval_sec"), 5)
        if interval > 30:
            problems.append(f"抓帧间隔 {interval}s 过长，可能错过短暂混乱（建议 ≤10s）")

        detail = (
            f"模型：{cfg.get('model_daily')}　告警阈值：{conf}　"
            f"活动度门控：{trigger}　抓帧间隔：{interval}s"
        )
        if problems:
            self._mark("config", "① 配置检查", "fail", detail,
                       "；".join(problems) + "。请在「设置」中调整。")
            return False
        self._mark("config", "① 配置检查", "pass", detail, "")
        return True

    def _step_connectivity(self):
        """第 2 项：API 连通性（1 次最小调用）。"""
        title = "② API 连通性"
        try:
            self.client = ZhipuVisionClient(
                api_key=self.cfg["api_key"],
                api_base=self.cfg.get("api_base"),
                model=self.cfg.get("model_daily"),
            )
        except Exception as e:
            self._mark("connectivity", title, "fail", f"客户端初始化失败：{e}",
                       "请检查 config.json 中的 api_base 是否正确。")
            return False

        # 用一张极小的纯灰图做最小调用，只为验证 Key 与模型可用
        probe = Image.new("RGB", (96, 64), (190, 190, 185))
        t0 = time.time()
        try:
            text = self.client.analyze(
                probe, "这是一张纯色测试图，请只回复：正常",
                model=self.cfg.get("model_daily"),
            )
        except ZhipuError as e:
            hint = self._error_hint(e)
            self._mark("connectivity", title, "fail", f"调用失败：{e}", hint)
            return False
        except Exception as e:
            self._mark("connectivity", title, "fail", f"调用异常：{e}",
                       "网络不通或端点不可达，请检查网络与 api_base 配置。")
            return False

        cost = time.time() - t0
        self._mark("connectivity", title, "pass",
                   f"调用成功，耗时 {cost:.1f}s，模型回复：{_clip(text, 80)}", "")
        return True

    def _error_hint(self, err):
        """根据错误码给出可操作的修复建议。"""
        code = getattr(err, "status_code", None)
        msg = str(err)
        if code == 401 or "401" in msg or "鉴权" in msg or "Unauthorized" in msg:
            return "API Key 无效或已失效，请在 open.bigmodel.cn 重新生成并填入设置。"
        if code == 429 or "429" in msg or "限流" in msg:
            return "触发限流。免费模型有速率限制，可适当调大抓帧间隔。"
        if code == 403 or "403" in msg:
            return "访问被拒绝：请确认该 Key 有对应模型的访问权限。"
        if code and code >= 500:
            return "服务端临时故障，请稍后重试。"
        return "请检查网络连通性与 API Key 是否正确。"

    def _full_model_steps(self):
        """第 3、4 项：模型判定力与克制力（完整自检时执行）。"""
        if not self.full:
            return
        prompt = self.cfg.get("prompt") or config_mod.DEFAULT_PROMPT
        model = self.cfg.get("model_daily")

        # 第 3 项：混乱画面应判异常
        chaos_img = make_classroom_frame(frame_idx=2, chaos=0.85, seed=7)
        try:
            text = self.client.analyze(chaos_img, prompt, model=model)
        except Exception as e:
            self._mark("model_chaos", "③ 模型判定力（混乱画面）", "fail",
                       f"调用失败：{e}", self._error_hint(e))
        else:
            abnormal, info = parse_verdict(text)
            conf = _as_float(info.get("confidence"), 0.0)
            raw = _clip(text)
            if abnormal:
                self._mark("model_chaos", "③ 模型判定力（混乱画面）", "pass",
                           f"已识别为异常，置信度 {conf:.2f}。模型回复：{raw}", "")
            else:
                self._mark("model_chaos", "③ 模型判定力（混乱画面）", "fail",
                           f"画面明显混乱，模型却回复：{raw}",
                           "模型没能识别合成混乱图。若真实监控也如此，可尝试换用 "
                           "glm-4.1v-thinking-flash，或在设置中调低告警阈值。")

        # 第 4 项：安静画面应判正常
        calm_img = make_classroom_frame(chaos=0.0)
        try:
            text = self.client.analyze(calm_img, prompt, model=model)
        except Exception as e:
            self._mark("model_calm", "④ 模型克制力（安静画面）", "warn",
                       f"调用失败：{e}", self._error_hint(e))
        else:
            abnormal, info = parse_verdict(text)
            raw = _clip(text)
            if not abnormal:
                self._mark("model_calm", "④ 模型克制力（安静画面）", "pass",
                           f"正确判为正常。模型回复：{raw}", "")
            else:
                self._mark("model_calm", "④ 模型克制力（安静画面）", "warn",
                           f"安静画面被判为异常（会误报）。模型回复：{raw}",
                           "存在误报倾向，可适当调高告警阈值（如 0.6）。")

    def _step_local_activity(self):
        """第 5 项：本地活动度检测（纯本地，不消耗 API）。"""
        title = "⑤ 本地活动度检测"
        try:
            analyzer = diff_detect.ActivityAnalyzer()
            # 前几帧安静，建立基线
            for _ in range(3):
                analyzer.update(make_classroom_frame(chaos=0.0))
            # 随后持续混乱
            last = None
            for i in range(8):
                last = analyzer.update(
                    make_classroom_frame(frame_idx=i + 10, chaos=0.9, seed=11)
                )
            activity = last["activity"]
            trigger = _as_float(self.cfg.get("activity_trigger"), 0.12)
            if activity >= trigger:
                self._mark("local_activity", title, "pass",
                           f"持续混乱下活动度升至 {activity:.2f}（门控 {trigger:.2f}），"
                           f"活跃帧占比 {last['active_ratio']:.0%}", "")
            else:
                self._mark("local_activity", title, "fail",
                           f"持续混乱下活动度仅 {activity:.2f}，低于门控 {trigger:.2f}",
                           "本地检测器过于迟钝，画面变化会被整个跳过。请调低活动度门控。")
        except Exception as e:
            self._mark("local_activity", title, "fail",
                       f"检测异常：{type(e).__name__}: {e}",
                       f"{traceback.format_exc(limit=1)}")

    def _step_end_to_end(self):
        """第 6 项：端到端决策链（合成混乱序列驱动完整 Monitor）。"""
        title = "⑥ 端到端决策链"
        if not self.full:
            self._mark("e2e", title, "warn", "快速自检已跳过该项",
                       "勾选「完整自检」可验证从抓帧到告警的完整链路。")
            return
        if self.client is None:
            self._mark("e2e", title, "warn", "依赖 API 连通性，前序未通过已跳过", "")
            return

        # 端到端演练必须模拟真实过程：巡检开始时画面是正常的，
        # 跑一段时间后才突然混乱。本地兜底需要先从这些正常帧里
        # 学会「本场景的正常活动水平」，才能判断后续变化是否异常。
        # 若一上来就喂混乱帧，基线无从建立，兜底按设计是不会触发的。
        frames = [
            make_classroom_frame(frame_idx=i, chaos=0.0, seed=5)
            for i in range(_E2E_BASELINE_FRAMES)
        ] + [
            make_classroom_frame(frame_idx=i, chaos=0.9, seed=21)
            for i in range(_E2E_FRAMES + 2)
        ]
        cursor = {"i": 0}
        original_capture = window_capture.capture_window

        def fake_capture(hwnd):
            f = frames[min(cursor["i"], len(frames) - 1)]
            cursor["i"] += 1
            return f

        # 自检是演练，必须与用户数据完全隔离：
        #   - 异常截图写到临时目录，跑完即删
        #   - 告警历史也指向临时文件，避免把假告警混进用户的历史栏目
        #   - 关闭系统通知，否则一次自检会连弹好几条 Toast
        tmpdir = tempfile.mkdtemp(prefix="vigil_selftest_")
        cfg = dict(self.cfg)
        cfg["alert_image_dir"] = tmpdir
        cfg["alert_history_file"] = os.path.join(tmpdir, "alert_history.json")
        cfg["min_alert_interval_sec"] = 0  # 自检时不做通知节流

        mon = None
        try:
            window_capture.capture_window = fake_capture
            mon = Monitor(cfg, _FakeWindow(), verbose=False, alert_notify=False)
            mon.client = self.client  # 复用已验证可用的客户端
            last = None
            total = _E2E_BASELINE_FRAMES + _E2E_FRAMES
            for i in range(total):
                # 基线阶段不必强求每帧都调用模型，按需分析即可
                last = mon._tick(on_status=None, force_analyze=(i == 0))
            alerts = mon.stats["alerts"]
            if alerts >= 1:
                self._mark("e2e", title, "pass",
                           f"合成混乱序列触发了 {alerts} 次告警"
                           f"（共 {mon.stats['frames']} 帧、{mon.stats['calls']} 次模型调用）",
                           "")
            else:
                conf = _as_float((last or {}).get("confidence"), 0.0)
                threshold = _as_float(cfg.get("alert_confidence"), 0.45)
                reason = (last or {}).get("detail") or (last or {}).get("reason") or "无"
                self._mark("e2e", title, "fail",
                           f"先跑 {_E2E_BASELINE_FRAMES} 帧正常画面建立基线后，"
                           f"又跑 {_E2E_FRAMES} 帧混乱画面仍未告警"
                           f"（最终置信度 {conf:.2f} / 阈值 {threshold:.2f}，"
                           f"判定：{_clip(reason, 120)}）",
                           "这是「画面很乱却不报告」的直接原因。可依次尝试："
                           "调低告警阈值至 0.4 以下；换用 glm-4.1v-thinking-flash；"
                           "缩短抓帧间隔。若仍无效，请查看 logs 日志。")
        except Exception as e:
            self._mark("e2e", title, "fail",
                       f"执行异常：{type(e).__name__}: {e}",
                       _clip(traceback.format_exc(limit=2), 200))
        finally:
            window_capture.capture_window = original_capture
            if mon is not None:
                try:
                    mon.close()
                except Exception:
                    pass
            # 清理自检产生的临时截图
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
