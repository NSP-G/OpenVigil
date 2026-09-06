# -*- coding: utf-8 -*-
"""场景变化检测器专项测试。

这组用例的来历：真实模型测试暴露了一个隐蔽的失败——
用全局感知哈希做门控时，「墙上新贴一张分组表」的哈希距离（1）
居然小于正常画面抖动（3），门控会把真正的变化直接拦掉，
模型根本没机会看到画面。

根因是全局哈希被课桌和人占据均值，一小块浅色张贴物撬不动全局阈值。
改为「持续性（同一块区域持续在变）+ 紧凑度（变化集中成块）」双判据后
才真正区分开。本文件把这条结论固化，防止有人改回全局哈希。

同时守住另一个数学陷阱：累加器 p ← p×decay+1 的稳态上限是 1/(1-decay)，
need 若设得高于上限，条件永远无法满足，功能静默失效且不报错。

运行：python -m pytest tests/test_scene_change.py -v
"""
import os
import random
import sys

import pytest
from PIL import Image, ImageDraw

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app.diff_detect import SceneChangeDetector


# ==================== 测试素材 ====================

def classroom(seed=0):
    """整齐就座的教室。"""
    img = Image.new("RGB", (640, 400), (232, 228, 220))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 640, 60], fill=(205, 200, 190))
    for r in range(3):
        for c in range(8):
            x, y = 70 + c * 68, 120 + r * 70
            d.ellipse([x, y, x + 22, y + 22], fill=(60, 55, 50))
            d.rectangle([x + 2, y + 22, x + 20, y + 48], fill=(70, 90, 140))
    return img


def classroom_with_poster():
    """左墙贴一张分组表。"""
    img = classroom()
    d = ImageDraw.Draw(img)
    d.rectangle([10, 95, 62, 210], fill=(252, 250, 244))
    d.rectangle([10, 95, 62, 210], outline=(80, 80, 80), width=2)
    for i in range(7):
        d.line([14, 100 + i * 16, 58, 100 + i * 16], fill=(150, 150, 150))
    return img


def jittered(idx):
    """每帧位置都不同的轻微抖动。"""
    random.seed(idx * 977)
    img = classroom()
    d = ImageDraw.Draw(img)
    for _ in range(40):     # 随机噪点，位置每帧不同
        d.point((random.randint(0, 639), random.randint(0, 399)),
                fill=(random.randint(200, 255),) * 3)
    return img


def chaotic(idx, amount=0.8):
    """学生离座走动（差异铺开一大片）。"""
    random.seed(idx * 31 + 7)
    img = Image.new("RGB", (640, 400), (232, 228, 220))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 640, 60], fill=(205, 200, 190))
    for i in range(24):
        col, row = i % 8, i // 8
        x, y = 70 + col * 68, 120 + row * 70
        if random.random() < amount:
            x += random.randint(-45, 45)
            y += random.randint(-40, 40)
        d.ellipse([x, y, x + 22, y + 22], fill=(60, 55, 50))
        d.rectangle([x + 2, y + 22, x + 20, y + 48], fill=(70, 90, 140))
    return img


def feed(detector, frames):
    """连续喂帧，返回最后一次非空的变化描述（或 None）。"""
    last = None
    for f in frames:
        r = detector.update(f)
        if r:
            last = r
    return last


# ==================== 参数合法性 ====================

class TestParameterSanity:
    def test_need_below_steady_state(self):
        """need 必须低于累加器稳态上限，否则条件永远无法满足。

        这是一个静默失效陷阱：不报错、不崩溃，功能却永久关闭。
        """
        d = SceneChangeDetector()
        assert d.need < d.steady_state_max, (
            f"need={d.need} 高于稳态上限 {d.steady_state_max:.2f}，"
            "该条件永远不可能被满足")

    def test_steady_state_formula(self):
        d = SceneChangeDetector(decay=0.5)
        assert abs(d.steady_state_max - 2.0) < 1e-6
        d2 = SceneChangeDetector(decay=0.8)
        assert abs(d2.steady_state_max - 5.0) < 1e-6

    def test_accumulator_reaches_need_when_persistent(self):
        """持续变化必须真的能把累加器推到 need 以上。"""
        d = SceneChangeDetector()
        d.update(classroom())
        for _ in range(10):
            d.update(classroom_with_poster())
        assert max(d.persistence) >= d.need


# ==================== 判别能力 ====================

class TestDiscrimination:
    def test_still_scene_no_change(self):
        """完全静止 → 不得触发。"""
        assert feed(SceneChangeDetector(), [classroom()] * 10) is None

    def test_frame_jitter_no_change(self):
        """每帧位置不同的抖动 → 不得触发（差异不持续）。"""
        frames = [classroom()] + [jittered(i) for i in range(1, 12)]
        assert feed(SceneChangeDetector(), frames) is None

    def test_new_poster_detected(self):
        """核心场景：墙上新贴分组表 → 必须检出。"""
        frames = [classroom()] + [classroom_with_poster()] * 8
        r = feed(SceneChangeDetector(), frames)
        assert r is not None, "新增张贴物未被检出"
        assert r["cells"] >= 12

    def test_poster_change_is_compact(self):
        """张贴物的变化应当集中成块，紧凑度高于阈值。"""
        frames = [classroom()] + [classroom_with_poster()] * 8
        r = feed(SceneChangeDetector(), frames)
        assert r["fill"] >= 0.45, f"紧凑度仅 {r['fill']}"

    def test_chaos_not_treated_as_scene_change(self):
        """学生离座走动 → 差异铺开一大片，不应算环境变化。"""
        frames = [classroom()] + [chaotic(i, 0.5) for i in range(1, 10)]
        assert feed(SceneChangeDetector(), frames) is None

    def test_heavy_chaos_not_treated_as_scene_change(self):
        """大面积混乱 → 同理不得触发。"""
        frames = [classroom()] + [chaotic(i, 0.9) for i in range(1, 10)]
        assert feed(SceneChangeDetector(), frames) is None

    def test_gradual_moderate_change(self):
        """中度混乱（约七成学生位移）也不应触发。"""
        frames = [classroom()] + [chaotic(i, 0.7) for i in range(1, 10)]
        assert feed(SceneChangeDetector(), frames) is None


# ==================== 基准重置 ====================

class TestReferenceReset:
    def test_reset_clears_persistence(self):
        d = SceneChangeDetector()
        d.update(classroom())
        for _ in range(8):
            d.update(classroom_with_poster())
        assert max(d.persistence) > 0
        d.reset_reference(classroom_with_poster())
        assert max(d.persistence) == 0.0

    def test_reset_prevents_repeat_trigger(self):
        """确认过一次后重置基准，同一变化不应反复触发。"""
        d = SceneChangeDetector()
        d.update(classroom())
        for _ in range(8):
            d.update(classroom_with_poster())
        d.reset_reference(classroom_with_poster())
        assert feed(d, [classroom_with_poster()] * 8) is None

    def test_reset_count_increments(self):
        d = SceneChangeDetector()
        d.update(classroom())
        assert d.reset_count == 0
        d.reset_reference(classroom())
        assert d.reset_count == 1

    def test_new_change_after_reset_still_detected(self):
        """重置后若又出现新变化，必须还能检出。"""
        d = SceneChangeDetector()
        d.update(classroom())
        for _ in range(8):
            d.update(classroom_with_poster())
        d.reset_reference(classroom_with_poster())
        # 再贴一张（换个位置）
        def two_posters():
            img = classroom_with_poster()
            dr = ImageDraw.Draw(img)
            dr.rectangle([570, 95, 625, 210], fill=(250, 246, 236))
            dr.rectangle([570, 95, 625, 210], outline=(70, 70, 70), width=2)
            return img
        assert feed(d, [two_posters()] * 8) is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
