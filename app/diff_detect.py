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


def scene_hash(image, size=(16, 16)):
    """计算场景感知哈希，用于检测环境的**持久性**变化。

    与 mean_abs_diff 的区别：
        mean_abs_diff 衡量「画面变了多少」——有人走过就跳；
        scene_hash    衡量「场景布局是不是同一个」——人走过不影响，
                      但墙上新贴一张分组表、桌椅被挪动就会改变。

    实现为简化版感知哈希：缩到 16×16 灰度，以均值为基准逐位二值化，
    得到 256 bit 指纹。纯本地计算，零成本。
    """
    try:
        gray = image.convert("L").resize(size, Image.Resampling.BILINEAR)
        pixels = list(gray.getdata())
        avg = sum(pixels) / len(pixels)
        bits = 0
        for p in pixels:
            bits = (bits << 1) | (1 if p > avg else 0)
        return "%016x" % bits
    except Exception:
        return None


def hamming_distance(hash_a, hash_b):
    """两个场景哈希的汉明距离（0=完全相同，越大差异越明显）。"""
    if not hash_a or not hash_b or len(hash_a) != len(hash_b):
        return None
    try:
        return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")
    except (ValueError, TypeError):
        return None


class SceneChangeDetector:
    """持续性局域变化检测器：识别「墙上新贴了东西」这类环境变化。

    ==========================================================================
    为什么不用全局感知哈希
    ==========================================================================

    实测结论：全局感知哈希（无论 aHash 还是 dHash、无论 16×16 还是 64×64）
    都对「墙上多一张分组表」极不敏感——贴表造成的哈希距离（1~12）
    甚至小于正常画面抖动（3~10），因为整幅图的均值被课桌和人占据，
    一小块浅色张贴物根本撬不动全局阈值。

    ==========================================================================
    真正的区分点：持续性 + 空间集中
    ==========================================================================

        贴了一张表   → 同一小块区域，每一帧都在变（持续、集中）
        画面抖动     → 每帧变的地方都不一样（随机、分散）
        学生离座     → 座位空出来，差异铺满整个 seating 区（持续但分散）

    于是判据有两条：
        ① 持续性：同一格连续多帧都被判为「有差异」
        ② 紧凑度：被标记的区域集中成一小块，而不是铺开一大片

    ==========================================================================
    一个容易踩的数学坑
    ==========================================================================

    累加器 p ← p×decay + 1 的稳态上限是 1/(1-decay)。
    若 decay=0.6，上限仅 2.5——把 need 设成 3.0 的话，
    这个条件**永远不可能被满足**，功能静默失效且毫无报错。
    因此 need 必须显著低于稳态上限（这里取 2.0 vs 上限 2.5）。
    """

    GRID_W = 64
    GRID_H = 40

    def __init__(self, cell_threshold=8, decay=0.6, need=2.0,
                 min_cells=12, min_fill=0.45):
        self.cell_threshold = cell_threshold   # 单格亮度差超过多少算「有变化」
        self.decay = decay                     # 历史持续度的衰减系数
        self.need = need                       # 达到多少才算「持续变化」
        self.min_cells = min_cells             # 至少多少格同时命中
        self.min_fill = min_fill               # 紧凑度下限（命中格数 / 外接框面积）
        self.reference = None                  # 基准网格
        self.persistence = None                # 每格的持续度
        self.reset_count = 0

    @property
    def steady_state_max(self):
        """累加器的数学上限，用于自检 need 是否设得不可达。"""
        return 1.0 / (1.0 - self.decay) if self.decay < 1.0 else float("inf")

    def _grid(self, image):
        """把画面降采样成亮度网格。"""
        gray = image.convert("L").resize(
            (self.GRID_W, self.GRID_H), Image.Resampling.BILINEAR)
        return list(gray.getdata())

    def update(self, image):
        """上报一帧，返回 None 或变化描述。

        返回示例：
            {"cells": 71, "fill": 0.78, "bbox": (x, y, w, h)}
        """
        grid = self._grid(image)
        if self.reference is None:
            self.reference = grid
            self.persistence = [0.0] * len(grid)
            return None

        self.persistence = [
            p * self.decay + (1.0 if abs(a - b) >= self.cell_threshold else 0.0)
            for p, a, b in zip(self.persistence, grid, self.reference)
        ]

        idx = [i for i, p in enumerate(self.persistence) if p >= self.need]
        if len(idx) < self.min_cells:
            return None

        # 紧凑度：命中格集中成块（贴表）还是铺满一片（人员流动）
        xs = [i % self.GRID_W for i in idx]
        ys = [i // self.GRID_W for i in idx]
        bw = max(xs) - min(xs) + 1
        bh = max(ys) - min(ys) + 1
        fill = len(idx) / float(bw * bh)
        if fill < self.min_fill:
            return None

        return {
            "cells": len(idx),
            "fill": round(fill, 3),
            "bbox": (min(xs), min(ys), bw, bh),
        }

    def reset_reference(self, image=None):
        """重置基准。

        两种时机：
          1. 变化已被模型描述并记入环境文件——基准推进到当前状态
          2. 模型说「无变化」——说明是误触发，重置避免反复调用
        """
        if image is not None:
            self.reference = self._grid(image)
            self.persistence = [0.0] * len(self.reference)
        elif self.reference is not None:
            self.persistence = [0.0] * len(self.reference)
        self.reset_count += 1


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
