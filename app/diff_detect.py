# -*- coding: utf-8 -*-
"""本地帧差分与活动度分析模块。

Copyright (c) 2026 CaspianFlow. 版权所有。

设计目标：
1. 画面没有实质变化时，跳过 API 调用，把调用次数和费用降到最低。
2. 但绝不能因为"单帧差分很小"就漏掉真实混乱——
   班级乱起来的时候，画面可能恰好在某一瞬间相对静止，
   所以这里同时看「瞬时运动」「相对参考帧的累计漂移」「近期活跃帧比例」
   三个维度，综合成活动度评分供决策层使用。
"""
from PIL import Image, ImageFilter, ImageStat

# 活动度评分的权重（三者之和为 1）
_W_MOTION = 0.45      # 瞬时运动：相邻帧差
_W_DRIFT = 0.30       # 累计漂移：相对参考帧
_W_ACTIVE_RATIO = 0.25  # 近期活跃帧比例：是不是"一直在动"

# 运动量归一化基准：平均绝对差达到这个值即视为"满格运动"
_MOTION_FULL_SCALE = 0.06
_DRIFT_FULL_SCALE = 0.10

# 判定单帧"有运动"的阈值（比整体调用阈值更灵敏，用于统计活跃比例）
_FRAME_ACTIVE_THRESHOLD = 0.006


def _to_small_gray(image, size=(64, 64)):
    """转成小尺寸灰度图，用于快速比较。"""
    return image.convert("L").resize(size, Image.Resampling.BILINEAR)


def mean_abs_diff(img_a, img_b):
    """计算两帧平均绝对像素差（0~1）。

    值越小越相似；返回 0 表示完全相同。
    """
    a = _to_small_gray(img_a)
    b = _to_small_gray(img_b)
    pa = list(a.getdata())
    pb = list(b.getdata())
    total = 0
    n = len(pa)
    for i in range(n):
        total += abs(pa[i] - pb[i])
    return (total / n) / 255.0


def frame_changed(prev, curr, threshold):
    """判断当前帧相对上一帧是否有实质变化。

    直接用平均绝对差判定，阈值越小越敏感。
    """
    return mean_abs_diff(prev, curr) >= threshold


def motion_energy(prev, curr):
    """瞬时运动能量（0~1），已按经验基准归一化。"""
    if prev is None:
        return 0.0
    return _clamp01(mean_abs_diff(prev, curr) / _MOTION_FULL_SCALE)


def image_complexity(image):
    """画面复杂度（0~1）：边缘能量越高，画面内容越"杂"。

    用于辅助区分"空教室/整齐就座"与"多人走动聚集"的画面形态，
    也可用于识别纯色/黑屏等无效抓帧。
    """
    try:
        gray = image.convert("L").resize((128, 128), Image.Resampling.BILINEAR)
        edges = gray.filter(ImageFilter.FIND_EDGES)
        # 边缘图的标准差能反映画面结构丰富程度
        return _clamp01(ImageStat.Stat(edges).stddev[0] / 40.0)
    except Exception:
        return 0.0


def _clamp01(v):
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else float(v))


class ActivityAnalyzer:
    """本地活动度分析器（纯本地计算，不调用 API，零成本）。

    维护三个信号：
      - motion：相邻两帧的瞬时运动
      - drift：当前帧相对"参考帧"的累计漂移（参考帧会在画面长期稳定时自适应更新）
      - active_ratio：最近若干帧里有运动的帧占比（区分"一直在动"和"动了一下"）

    调用 update(frame) 返回一份信号字典，供上层的报警决策使用。
    """

    def __init__(self, history_size=12, stable_frames_to_reset=8):
        self.history_size = max(2, int(history_size))
        # 连续多少帧判定为"静止"后，把参考帧更新为当前画面（自适应环境变化）
        self.stable_frames_to_reset = max(2, int(stable_frames_to_reset))
        self.prev_frame = None
        self.reference_frame = None
        self._recent_active = []   # 最近 N 帧是否"有运动"
        self._stable_streak = 0    # 连续静止帧计数
        self._frame_count = 0

    def update(self, frame):
        """喂入新的一帧，返回活动度信号字典。"""
        self._frame_count += 1
        motion_raw = mean_abs_diff(self.prev_frame, frame) if self.prev_frame is not None else 0.0
        is_active = motion_raw >= _FRAME_ACTIVE_THRESHOLD and self.prev_frame is not None

        # 参考帧：首帧直接确立；画面长期稳定时自适应更新（适应光线/缓慢变化）
        if self.reference_frame is None:
            self.reference_frame = frame
            self._stable_streak = 0
        elif not is_active:
            self._stable_streak += 1
            if self._stable_streak >= self.stable_frames_to_reset:
                self.reference_frame = frame
                self._stable_streak = 0
        else:
            self._stable_streak = 0

        drift_raw = (
            mean_abs_diff(self.reference_frame, frame)
            if self.reference_frame is not None
            else 0.0
        )

        # 维护近期活跃帧滑动窗口
        self._recent_active.append(1 if is_active else 0)
        if len(self._recent_active) > self.history_size:
            self._recent_active.pop(0)
        active_ratio = (
            sum(self._recent_active) / len(self._recent_active)
            if self._recent_active
            else 0.0
        )

        self.prev_frame = frame

        motion_n = _clamp01(motion_raw / _MOTION_FULL_SCALE)
        drift_n = _clamp01(drift_raw / _DRIFT_FULL_SCALE)
        score = _clamp01(
            _W_MOTION * motion_n + _W_DRIFT * drift_n + _W_ACTIVE_RATIO * active_ratio
        )
        return {
            "motion": motion_n,
            "motion_raw": motion_raw,
            "drift": drift_n,
            "drift_raw": drift_raw,
            "active_ratio": active_ratio,
            "complexity": image_complexity(frame),
            "activity": score,
            "frame_active": is_active,
            "frame_count": self._frame_count,
        }

    @property
    def frame_count(self):
        """已处理的帧数，用于判断活动度统计是否已积累到可信程度。"""
        return self._frame_count

    def reset(self):
        """重置分析器（切换监控目标时调用）。"""
        self.prev_frame = None
        self.reference_frame = None
        self._recent_active = []
        self._stable_streak = 0
        self._frame_count = 0
