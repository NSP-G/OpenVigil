# -*- coding: utf-8 -*-
"""本地兜底守卫（LocalGuard）：判断「模型说正常」这件事到底可不可信。

Copyright (c) 2026 CaspianFlow. 版权所有。

============================================================================
为什么重写：旧设计的根本缺陷
============================================================================

旧判据是「画面活动度 >= 固定阈值」，即用「画面变了多少」去推断「画面乱不乱」。
实测证明这个特征根本不成立（正常与混乱完全重叠）：

    放教学视频（正常）         活动度 0.979
    曝光/开灯渐变（正常）      活动度 0.815
    正常上课老师走动（正常）   活动度 0.467
    多人离座打闹（真混乱）     活动度 0.953

正常场景与混乱场景的活动度数值域**完全重叠**，无论把阈值调到多少，
都必然在「误报正常场景」与「漏报真混乱」之间二选一。
所以这不是灵敏度问题，是特征选错了——调参无法解决。

============================================================================
新设计：从「变化量判据」改为「结构化异常判据」
============================================================================

本地信号能可靠测量的只有「画面怎么变」，测不了「画面乱不乱」。
因此兜底的正确定位不是「替模型判乱」，而是「发现模型可能失灵」。

三重过滤，必须全部满足才提示：

  ① 空间结构关 —— 变化是否集中在局部区域？
     用基尼系数衡量运动量的空间分布。
     整屏均匀变化（放视频、噪点、压缩伪影）基尼≈0.2；
     局部人群活动基尼>0.5。呈均匀分布直接排除。

  ② 光照排除关 —— 整幅图是否同向变亮/变暗？
     用 |净偏移| / 平均绝对差 衡量。
     光照与曝光渐变接近 1.0；真实物体运动有增有减，接近 0。
     命中即排除（实测：曝光渐变 1.000，其余场景 <0.02）。

  ③ 相对基线关 —— 是否显著偏离「本场景自己的正常水平」？
     维护滑动窗口的均值与标准差，用 z-score 判定。
     老师持续走动 → 被基线吸收为常态，不再触发；
     突然打闹     → z 骤升，立刻捕捉（实测 z: 0.5 vs 4.8）。

再加两道闸门：

  ④ 模型可信度反向调节 —— 模型越确信「正常」，兜底门槛越高。
     用户反馈「视觉模型其实是对的」，所以模型明确判正常时必须极度克制；
     反之模型含糊其辞时才值得提醒。这是抑制误报的关键一环。

  ⑤ 冷却与连续帧确认 —— 触发后进入冷却期，避免持续刷屏。

============================================================================
明确不做的事
============================================================================

模型回复「画面不清 / 无法判断」属于「模型看不清」，不是「画面很乱」。
这种情况应当提示用户检查画面质量，而不是伪装成告警。
由上层单独处理为信息性提示，不写入告警历史。
"""
import time
from collections import deque

from PIL import Image

# ---- 空间特征 ----
_GRID = 8                    # 运动分布划分网格（8×8=64 格）
_CELL = 64 // _GRID          # 每格边长（基于 64×64 缩放图）

# ---- 各道闸门的阈值 ----
_CONCENTRATION_MIN = 0.34    # 空间聚集度下限：低于此值视为整屏均匀变化
_ILLUMINATION_MAX = 0.55     # 光照同向比上限：高于此值视为光照/曝光变化
_BASE_STD_FLOOR = 0.02       # 基线标准差下限：防止极稳场景下 z 值被放大到失真

# ---- 基线 ----
_BASELINE_WINDOW = 40        # 基线滑动窗口帧数
_BASELINE_MIN_SAMPLES = 12   # 至少积累这么多正常帧才允许兜底判定

# ---- 触发条件 ----
_STREAK_REQUIRED = 3         # 连续多少帧满足条件才触发
_BASE_Z_THRESHOLD = 2.5      # 模型毫无把握时的 z 阈值下限
_BELIEF_Z_SCALE = 2.2        # 模型越确信正常，z 阈值抬得越高


def _to_gray_array(image, size=(64, 64)):
    """转成 64×64 灰度的像素列表。"""
    return list(image.convert("L").resize(size, Image.Resampling.BILINEAR).getdata())


def _gini(values):
    """基尼系数：0=完全均匀，1=完全集中。

    用于衡量运动在画面上的分布形态——
    均匀分布意味着光照、噪点或整屏视频；集中分布意味着局部有人群活动。
    """
    n = len(values)
    total = sum(values)
    if n == 0 or total <= 0:
        return 0.0
    ordered = sorted(values)
    weighted = sum((i + 1) * v for i, v in enumerate(ordered))
    g = (2 * weighted) / (n * total) - (n + 1) / n
    return min(1.0, max(0.0, g))


def compute_spatial_features(prev_gray, curr_gray):
    """计算两帧之间的空间结构特征。

    返回 (concentration, illumination)：
      concentration：运动量的空间聚集度（基尼系数）
      illumination：光照同向比（1=整幅同向变化，0=有增有减）
    """
    n = len(prev_gray)
    diffs = [0] * n
    net_sum = 0
    abs_sum = 0
    for i in range(n):
        d = curr_gray[i] - prev_gray[i]
        ad = d if d >= 0 else -d
        diffs[i] = ad
        net_sum += d
        abs_sum += ad
    abs_mean = abs_sum / n
    illumination = abs(net_sum / n) / (abs_mean + 1e-6)

    # 分块聚合各格运动量（索引 i = 行号 * 64 + 列号）
    cells = [0.0] * (_GRID * _GRID)
    for gy in range(_GRID):
        for gx in range(_GRID):
            s = 0
            for y in range(gy * _CELL, (gy + 1) * _CELL):
                base = y * 64 + gx * _CELL
                for x in range(_CELL):
                    s += diffs[base + x]
            cells[gy * _GRID + gx] = s
    return _gini(cells), illumination


class AdaptiveBaseline:
    """自适应基线：学习「本场景的正常活动水平」。

    只收录被判定为正常的帧——混乱期的样本绝不纳入基线，
    否则打闹久了会被当成常态，反而造成漏报。
    """

    def __init__(self, window=_BASELINE_WINDOW, min_samples=_BASELINE_MIN_SAMPLES):
        self.window = max(4, int(window))
        self.min_samples = max(4, int(min_samples))
        self._values = deque(maxlen=self.window)

    def learn(self, value):
        """纳入一个正常帧的活动度样本。"""
        try:
            self._values.append(float(value))
        except (TypeError, ValueError):
            pass

    def reset(self):
        self._values.clear()

    @property
    def ready(self):
        """样本是否足够支撑判定（样本不足时一律不兜底，避免开局乱报）。"""
        return len(self._values) >= self.min_samples

    @property
    def samples(self):
        return len(self._values)

    @property
    def mean(self):
        if not self._values:
            return 0.0
        return sum(self._values) / len(self._values)

    @property
    def std(self):
        if len(self._values) < 2:
            return 0.0
        m = self.mean
        return (sum((v - m) ** 2 for v in self._values) / len(self._values)) ** 0.5

    def z_score(self, value):
        """当前值偏离基线多少个标准差。基线未就绪时返回 0（即不触发）。"""
        if not self.ready:
            return 0.0
        std = max(self.std, _BASE_STD_FLOOR)
        return (value - self.mean) / std


class SpatialActivityAnalyzer:
    """在原有时间维度活动度之外，补充空间结构特征与自适应基线。

    包装既有的 ActivityAnalyzer，保持其 motion/drift/activity 等字段不变，
    额外提供 concentration / illumination / activity_z / baseline_ready。
    """

    def __init__(self, base_analyzer, baseline=None):
        self._base = base_analyzer
        self.baseline = baseline or AdaptiveBaseline()
        self._prev_gray = None

    def __getattr__(self, name):
        """未定义的属性转发给底层分析器（frame_count、reset 等）。"""
        return getattr(self._base, name)

    def update(self, frame):
        """喂入新帧，返回扩展后的信号字典。"""
        signals = self._base.update(frame)

        curr_gray = _to_gray_array(frame)
        prev_gray = self._prev_gray
        self._prev_gray = curr_gray

        if prev_gray is None:
            signals["concentration"] = 0.0
            signals["illumination"] = 0.0
        else:
            concentration, illumination = compute_spatial_features(prev_gray, curr_gray)
            signals["concentration"] = concentration
            signals["illumination"] = illumination

        activity = signals.get("activity", 0.0)
        signals["activity_z"] = self.baseline.z_score(activity)
        signals["baseline_ready"] = self.baseline.ready
        signals["baseline_mean"] = self.baseline.mean
        signals["baseline_std"] = self.baseline.std
        signals["baseline_samples"] = self.baseline.samples
        return signals

    def learn_baseline(self, activity):
        """确认当前帧属于正常状态后，纳入基线样本。"""
        self.baseline.learn(activity)

    def reset(self):
        self._base.reset()
        self.baseline.reset()
        self._prev_gray = None

    @property
    def frame_count(self):
        return self._base.frame_count


class LocalGuard:
    """本地兜底决策器。

    用法：
        guard = LocalGuard(cfg)
        result = guard.evaluate(verdict=False, signals=signals,
                                model_confidence=0.8, model_clear=True)
        # result 为 None（不兜底）或一条兜底提示字典
    """

    def __init__(self, cfg=None, cooldown_sec=None, enabled=True):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("local_guard_enabled", enabled))
        # 冷却：默认 5 分钟，避免同一波混乱反复刷屏
        self.cooldown = _safe_float(
            cfg.get("local_guard_cooldown_sec"),
            300.0 if cooldown_sec is None else cooldown_sec,
        )
        self.streak = 0
        self.last_fired_ts = 0.0
        self.last_reason = ""   # 最近一次未触发的原因，便于排查

    def evaluate(self, verdict, signals, model_confidence=0.5, model_clear=True):
        """评估是否需要本地兜底。

        参数：
            verdict          —— 视觉模型最终判定（True=异常）
            signals          —— SpatialActivityAnalyzer.update() 的输出
            model_confidence —— 模型对「正常」的确信程度（0~1）
            model_clear      —— 模型是否看得清画面（False=回复含糊）

        返回 None 或兜底提示字典。
        """
        if not self.enabled:
            self.last_reason = "已禁用"
            return None

        # ① 模型已经判异常 → 不需要兜底，走正常告警链路
        if verdict:
            self._reset_streak("模型已判异常")
            return None

        # ② 模型看不清画面 → 属于「画面质量问题」，不是「画面很乱」，
        #    交给上层做信息性提示，不伪装成告警写进历史。
        if not model_clear:
            self._reset_streak("模型看不清画面（非混乱）")
            return None

        # ③ 基线尚未学够 → 还在了解本场景的正常水平，不能妄下判断
        if not signals.get("baseline_ready", False):
            self._reset_streak("基线学习中")
            return None

        # ④ 整幅同向变化 → 光照/曝光调整，直接排除
        illumination = _safe_float(signals.get("illumination"), 0.0)
        if illumination > _ILLUMINATION_MAX:
            self._reset_streak("疑似光照变化")
            return None

        # ⑤ 变化均匀铺满全屏 → 视频、噪点、压缩伪影，排除
        concentration = _safe_float(signals.get("concentration"), 0.0)
        if concentration < _CONCENTRATION_MIN:
            self._reset_streak("变化均匀分布（疑似视频/噪点）")
            return None

        # ⑥ 相对本场景基线是否显著异常
        z = _safe_float(signals.get("activity_z"), 0.0)
        activity = _safe_float(signals.get("activity"), 0.0)

        # 模型越确信「正常」，越不该被打脸 → 门槛同步抬高
        belief = min(1.0, max(0.0, _safe_float(model_confidence, 0.5)))
        required_z = _BASE_Z_THRESHOLD + belief * _BELIEF_Z_SCALE

        if z < required_z:
            self._reset_streak(f"未显著超基线 z={z:.2f}<{required_z:.2f}")
            return None

        self.streak += 1
        if self.streak < _STREAK_REQUIRED:
            self.last_reason = f"连续确认中 {self.streak}/{_STREAK_REQUIRED}"
            return None

        # ⑦ 冷却期内不重复触发
        now = time.time()
        if self.last_fired_ts and (now - self.last_fired_ts) < self.cooldown:
            self.last_reason = "冷却期内"
            return None

        self.last_fired_ts = now
        fired_streak = self.streak
        self.streak = 0
        self.last_reason = "已触发"

        return {
            "abnormal": True,
            "type": "画面持续异常活动（待人工确认）",
            "detail": (
                f"本地检测：画面连续 {fired_streak} 帧出现局部聚集性活动"
                f"（活动度 {activity:.2f}，超出本场景正常水平 {z:.1f} 个标准差），"
                f"而视觉模型判定为正常（确信度 {belief:.0%}）。"
                f"可能是模型漏判，建议人工确认。"
            ),
            # 置信度刻意压低：这是「怀疑」不是「判定」，不应与真实告警同等权重
            "confidence": min(0.65, 0.4 + 0.05 * fired_streak),
            "local_fallback": True,
            "activity": activity,
            "activity_z": z,
            "concentration": concentration,
            "local_guard": True,
        }

    def _reset_streak(self, reason):
        self.streak = 0
        self.last_reason = reason

    def note_normal_frame(self, activity):
        """确认一帧为正常状态后调用，用于学习基线。

        由上层在「模型判正常且画面无疑点」时调用，
        保证基线只由正常样本构成。
        """
        self.streak = 0

    def reset(self):
        self.streak = 0
        self.last_fired_ts = 0.0
        self.last_reason = ""


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
