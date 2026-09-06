# -*- coding: utf-8 -*-
"""本地兜底守卫（LocalGuard）专项测试。

这些用例存在的意义：把「用空间结构 + 相对基线判断，而不是用活动度阈值」
这一设计决策固化下来。若哪天有人为了"提高灵敏度"把判据改回固定阈值，
这组测试会立刻失败。

运行：python -m pytest tests/test_local_guard.py -v
"""
import os
import random
import sys

import pytest
from PIL import Image, ImageDraw, ImageEnhance

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import diff_detect
from app.local_guard import (
    AdaptiveBaseline,
    LocalGuard,
    SpatialActivityAnalyzer,
    _gini,
    compute_spatial_features,
)


# ==================== 测试素材 ====================

def scene_video(i):
    """放教学视频：整屏内容大幅变化，但完全正常。"""
    random.seed(i * 7)
    img = Image.new("RGB", (640, 400))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 640, 400],
                fill=(random.randint(40, 200), random.randint(40, 200),
                      random.randint(60, 220)))
    for _ in range(30):
        x, y = random.randint(0, 600), random.randint(0, 360)
        d.rectangle([x, y, x + random.randint(40, 120), y + random.randint(30, 80)],
                    fill=(random.randint(0, 255), random.randint(0, 255),
                          random.randint(0, 255)))
    return img


def scene_exposure(i):
    """开灯 / 摄像头自动曝光：整幅图同向明暗变化。"""
    base = Image.new("RGB", (640, 400), (232, 228, 220))
    d = ImageDraw.Draw(base)
    for r in range(4):
        for c in range(8):
            x, y = 70 + c * 68, 120 + r * 70
            d.ellipse([x, y, x + 22, y + 22], fill=(60, 55, 50))
            d.rectangle([x + 2, y + 22, x + 20, y + 48], fill=(70, 90, 140))
    return ImageEnhance.Brightness(base).enhance(0.6 + 0.8 * ((i % 12) / 12))


def scene_normal_class(i):
    """正常上课：学生就座轻微晃动，老师在讲台前持续走动。"""
    random.seed(i)
    img = Image.new("RGB", (640, 400), (232, 228, 220))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 640, 60], fill=(205, 200, 190))
    for r in range(4):
        for c in range(8):
            x, y = 70 + c * 68, 120 + r * 70
            j = random.randint(-2, 2)
            d.ellipse([x + j, y, x + 22 + j, y + 22], fill=(60, 55, 50))
            d.rectangle([x + 2 + j, y + 22, x + 20 + j, y + 48], fill=(70, 90, 140))
    tx = 120 + (i * 22) % 380
    d.rectangle([tx, 30, tx + 38, 95], fill=(45, 45, 50))
    return img


def scene_chaos(i):
    """真正的混乱：多人离座、聚集。"""
    from app.selftest import make_classroom_frame
    return make_classroom_frame(frame_idx=i, chaos=0.85, seed=21)


def mixed_normal_then_chaos(i, switch_at=25):
    """先正常后混乱，模拟真实巡检过程。"""
    return scene_normal_class(i) if i < switch_at else scene_chaos(i)


def run_sequence(frames, model_confidence=0.85, model_clear=True,
                 total=None, generator=None):
    """驱动完整决策链，返回 (触发次数, 守卫对象)。"""
    analyzer = SpatialActivityAnalyzer(diff_detect.ActivityAnalyzer())
    guard = LocalGuard({})
    fired = 0
    n = total or len(frames)
    for i in range(n):
        frame = generator(i) if generator else frames[i]
        signals = analyzer.update(frame)
        result = guard.evaluate(
            verdict=False,
            signals=signals,
            model_confidence=model_confidence,
            model_clear=model_clear,
        )
        if result is not None:
            fired += 1
        # 与 Monitor 中一致：正常且未偏离常态的帧才纳入基线
        if model_clear and signals.get("activity_z", 0.0) < 1.5:
            analyzer.learn_baseline(signals.get("activity", 0.0))
    return fired, guard


# ==================== 空间特征 ====================

class TestSpatialFeatures:
    def test_gini_uniform_is_low(self):
        """完全均匀分布 → 基尼接近 0。"""
        assert _gini([1] * 64) < 0.01

    def test_gini_concentrated_is_high(self):
        """运动集中在一格 → 基尼接近 1。"""
        vals = [0.0] * 64
        vals[0] = 100.0
        assert _gini(vals) > 0.9

    def test_gini_empty(self):
        assert _gini([]) == 0.0
        assert _gini([0] * 64) == 0.0

    def test_illumination_detects_global_brightening(self):
        """整幅同向变亮 → 光照比接近 1。"""
        prev = [100] * 4096
        curr = [150] * 4096
        _, illum = compute_spatial_features(prev, curr)
        assert illum > 0.95, f"全局变亮应被识别为光照变化，实际 {illum:.3f}"

    def test_illumination_low_for_local_motion(self):
        """局部有增有减 → 光照比接近 0。"""
        prev = [100] * 4096
        curr = list(prev)
        for i in range(0, 512):
            curr[i] = 200   # 一部分变亮
        for i in range(512, 1024):
            curr[i] = 20    # 一部分变暗（有增有减）
        _, illum = compute_spatial_features(prev, curr)
        assert illum < 0.2, f"局部运动不应被判为光照变化，实际 {illum:.3f}"

    def test_concentration_distinguishes_layout(self):
        """整屏均匀变化 vs 局部集中变化，聚集度应显著不同。"""
        prev = [100] * 4096
        uniform = [140] * 4096
        local = list(prev)
        for y in range(24, 40):
            for x in range(24, 40):
                local[y * 64 + x] = 220

        c_uniform, _ = compute_spatial_features(prev, uniform)
        c_local, _ = compute_spatial_features(prev, local)
        assert c_local > c_uniform + 0.3, \
            f"局部变化应比均匀变化更集中：{c_local:.3f} vs {c_uniform:.3f}"

    def test_identical_frames(self):
        c, illum = compute_spatial_features([100] * 4096, [100] * 4096)
        assert c == 0.0 and illum == 0.0


# ==================== 自适应基线 ====================

class TestAdaptiveBaseline:
    def test_not_ready_until_min_samples(self):
        bl = AdaptiveBaseline(min_samples=5)
        for i in range(4):
            bl.learn(0.1 * i)
        assert not bl.ready
        bl.learn(0.1)
        assert bl.ready

    def test_z_zero_before_ready(self):
        """基线未就绪时一律返回 0，避免开局乱报。"""
        bl = AdaptiveBaseline(min_samples=10)
        bl.learn(0.1)
        assert bl.z_score(5.0) == 0.0

    def test_stable_scene_produces_low_z(self):
        """持续稳定的活动水平 → 后续同水平帧的 z 很低。"""
        bl = AdaptiveBaseline(min_samples=10)
        for _ in range(20):
            bl.learn(0.50)
        assert abs(bl.z_score(0.50)) < 0.3

    def test_spike_produces_high_z(self):
        """突然大幅偏离 → z 显著升高。"""
        bl = AdaptiveBaseline(min_samples=10)
        for _ in range(20):
            bl.learn(0.50)
        assert bl.z_score(0.98) > 2.5

    def test_std_floor_prevents_division_blowup(self):
        """完全恒定的基线：标准差为 0 时不能被放大成无穷大。"""
        bl = AdaptiveBaseline(min_samples=10)
        for _ in range(20):
            bl.learn(0.30)
        z = bl.z_score(0.32)
        assert abs(z) < 5, f"零标准差场景 z 值失控: {z}"

    def test_window_sliding(self):
        bl = AdaptiveBaseline(window=5, min_samples=3)
        for v in [0.1, 0.1, 0.1, 0.1, 0.1]:
            bl.learn(v)
        assert len(bl._values) == 5
        for v in [0.9, 0.9, 0.9, 0.9, 0.9]:
            bl.learn(v)
        assert bl.mean > 0.8, "滑动窗口应淘汰旧样本"

    def test_reset(self):
        bl = AdaptiveBaseline(min_samples=5)
        for _ in range(5):
            bl.learn(0.1)
        assert bl.ready
        bl.reset()
        assert not bl.ready

    def test_min_samples_has_floor(self):
        """min_samples 有下限保护，避免配置成 1~2 就草率判定。"""
        bl = AdaptiveBaseline(min_samples=1)
        bl.learn(0.1)
        assert not bl.ready, "样本下限过低会导致开局乱报"


# ==================== 核心：不误报正常场景 ====================

class TestNoFalseAlarm:
    """这几条是本次重构的核心诉求。

    旧判据「活动度 >= 0.35」在这些场景下的实测活动度分别为
    0.979 / 0.815 / 0.467，均会误报；新设计必须全部拦下。
    """

    @pytest.mark.parametrize("name,generator", [
        ("放教学视频", scene_video),
        ("曝光/开灯渐变", scene_exposure),
        ("正常上课老师走动", scene_normal_class),
    ])
    def test_normal_scene_never_fires(self, name, generator):
        fired, guard = run_sequence([], generator=generator, total=30)
        assert fired == 0, \
            f"{name} 属于正常场景，不应触发兜底（守卫判定：{guard.last_reason}）"

    def test_video_rejected_by_concentration(self):
        fired, guard = run_sequence([], generator=scene_video, total=30)
        assert "均匀" in guard.last_reason or "视频" in guard.last_reason

    def test_exposure_rejected_by_illumination(self):
        fired, guard = run_sequence([], generator=scene_exposure, total=30)
        assert "光照" in guard.last_reason

    def test_steady_activity_absorbed_by_baseline(self):
        """老师持续走动：被基线吸收为常态，不会一直触发。"""
        fired, guard = run_sequence([], generator=scene_normal_class, total=40)
        assert fired == 0
        assert "基线" in guard.last_reason or "z=" in guard.last_reason


# ==================== 核心：真混乱要能捕获 ====================

class TestCatchesRealChaos:
    def test_normal_then_chaos_fires(self):
        """先正常建立基线，再突然混乱 → 必须触发。"""
        fired, _ = run_sequence([], generator=mixed_normal_then_chaos, total=35)
        assert fired >= 1, "真混乱必须被捕获"

    def test_cooldown_prevents_spamming(self):
        """同一波混乱不应连续刷屏。"""
        fired, _ = run_sequence([], generator=mixed_normal_then_chaos, total=35)
        assert fired <= 2, f"冷却失效，连续触发 {fired} 次"

    def test_chaos_only_without_baseline_stays_silent(self):
        """一上来就是混乱（无基线可依）→ 按设计静默，不瞎报。

        这是有意的权衡：宁可先观察十来帧摸清正常水平，
        也不愿在开局就凭空告警。
        """
        fired, guard = run_sequence([], generator=scene_chaos, total=8)
        assert fired == 0
        assert "基线" in guard.last_reason


# ==================== 模型可信度反向调节 ====================

class TestModelBeliefModulation:
    def test_higher_belief_raises_threshold(self):
        """模型越确信正常，要求的 z 偏离越高。"""
        # 构造一个刚好在边界附近的偏离度
        signals = {
            "baseline_ready": True,
            "activity": 0.9,
            "activity_z": 3.0,
            "concentration": 0.6,
            "illumination": 0.05,
        }
        weak = LocalGuard({})
        strong = LocalGuard({})
        r_weak = weak.evaluate(False, signals, model_confidence=0.1, model_clear=True)
        # 提高确信度后，同样的信号应更难触发
        r_strong = strong.evaluate(False, signals, model_confidence=1.0, model_clear=True)
        # 两者都需连续 3 帧才触发，此处只看是否被门槛拦下
        assert r_weak is None and r_strong is None  # 首帧都只是累计 streak
        assert weak.streak >= strong.streak

    def test_low_belief_allows_firing(self):
        """模型含糊时（确信度低），门槛较低，更容易触发。"""
        signals = {
            "baseline_ready": True,
            "activity": 0.9,
            "activity_z": 3.0,
            "concentration": 0.6,
            "illumination": 0.05,
        }
        guard = LocalGuard({})
        results = [guard.evaluate(False, signals, model_confidence=0.0, model_clear=True)
                   for _ in range(3)]
        assert results[-1] is not None, "模型毫无把握时，显著偏离应触发"

    def test_high_belief_suppresses(self):
        """模型非常确信正常时，同样的偏离应被抑制。"""
        signals = {
            "baseline_ready": True,
            "activity": 0.9,
            "activity_z": 3.0,
            "concentration": 0.6,
            "illumination": 0.05,
        }
        guard = LocalGuard({})
        results = [guard.evaluate(False, signals, model_confidence=1.0, model_clear=True)
                   for _ in range(3)]
        assert results[-1] is None, "模型确信正常时应尊重模型判断"


# ==================== 模型看不清 ≠ 画面乱 ====================

class TestUnclearModel:
    def test_unclear_never_fires(self):
        fired, guard = run_sequence([], generator=mixed_normal_then_chaos,
                                    total=35, model_clear=False)
        assert fired == 0, "模型看不清属于画面质量问题，不应伪装成告警"
        assert "看不清" in guard.last_reason

    def test_unclear_resets_streak(self):
        guard = LocalGuard({})
        signals = {"baseline_ready": True, "activity": 0.9, "activity_z": 5.0,
                   "concentration": 0.6, "illumination": 0.05}
        guard.evaluate(False, signals, 0.5, model_clear=True)
        assert guard.streak == 1
        guard.evaluate(False, signals, 0.5, model_clear=False)
        assert guard.streak == 0, "看不清应清零连续计数"


# ==================== 其他闸门 ====================

class TestGates:
    def test_verdict_true_never_fires(self):
        """模型已判异常 → 无需兜底。"""
        guard = LocalGuard({})
        signals = {"baseline_ready": True, "activity": 0.9, "activity_z": 9.0,
                   "concentration": 0.8, "illumination": 0.0}
        assert guard.evaluate(True, signals, 0.5, True) is None
        assert guard.streak == 0

    def test_disabled(self):
        guard = LocalGuard({"local_guard_enabled": False})
        signals = {"baseline_ready": True, "activity": 0.9, "activity_z": 9.0,
                   "concentration": 0.8, "illumination": 0.0}
        results = [guard.evaluate(False, signals, 0.5, True) for _ in range(5)]
        assert all(r is None for r in results)

    def test_cooldown_blocks_repeat(self):
        guard = LocalGuard({"local_guard_cooldown_sec": 0.05})
        signals = {"baseline_ready": True, "activity": 0.9, "activity_z": 5.0,
                   "concentration": 0.6, "illumination": 0.05}
        first = [guard.evaluate(False, signals, 0.0, True) for _ in range(3)]
        assert first[-1] is not None, "首次应触发"
        again = [guard.evaluate(False, signals, 0.0, True) for _ in range(3)]
        assert again[-1] is None, "冷却期内不应重复触发"

    def test_streak_required(self):
        """单帧抖动不应立即触发，需连续多帧确认。"""
        guard = LocalGuard({})
        signals = {"baseline_ready": True, "activity": 0.9, "activity_z": 5.0,
                   "concentration": 0.6, "illumination": 0.05}
        r1 = guard.evaluate(False, signals, 0.0, True)
        r2 = guard.evaluate(False, signals, 0.0, True)
        assert r1 is None and r2 is None, "连续帧数不足时不应触发"

    def test_result_structure(self):
        guard = LocalGuard({})
        signals = {"baseline_ready": True, "activity": 0.9, "activity_z": 5.0,
                   "concentration": 0.6, "illumination": 0.05}
        result = None
        for _ in range(3):
            result = guard.evaluate(False, signals, 0.0, True)
        assert result is not None
        assert result["abnormal"] is True
        assert result["local_fallback"] is True
        # 置信度刻意压低：这是怀疑，不是判定
        assert result["confidence"] <= 0.65
        assert "人工确认" in result["detail"]

    def test_missing_signals_default_safe(self):
        """信号字段缺失时按最保守处理，不触发。"""
        guard = LocalGuard({})
        results = [guard.evaluate(False, {}, 0.0, True) for _ in range(5)]
        assert all(r is None for r in results)


# ==================== 分析器集成 ====================

class TestSpatialActivityAnalyzer:
    def test_extends_base_signals(self):
        """保留原有时间维度字段，并补充空间字段。"""
        a = SpatialActivityAnalyzer(diff_detect.ActivityAnalyzer())
        sig = a.update(scene_normal_class(0))
        for key in ("motion", "drift", "active_ratio", "activity"):
            assert key in sig, f"丢失原有字段 {key}"
        for key in ("concentration", "illumination", "activity_z", "baseline_ready"):
            assert key in sig, f"缺少新增字段 {key}"

    def test_first_frame_zero_features(self):
        a = SpatialActivityAnalyzer(diff_detect.ActivityAnalyzer())
        sig = a.update(scene_normal_class(0))
        assert sig["concentration"] == 0.0
        assert sig["illumination"] == 0.0

    def test_baseline_learning(self):
        a = SpatialActivityAnalyzer(diff_detect.ActivityAnalyzer())
        for i in range(20):
            sig = a.update(scene_normal_class(i))
            a.learn_baseline(sig["activity"])
        assert a.baseline.ready
        assert a.baseline.mean > 0

    def test_frame_count_delegated(self):
        a = SpatialActivityAnalyzer(diff_detect.ActivityAnalyzer())
        for i in range(5):
            a.update(scene_normal_class(i))
        assert a.frame_count == 5

    def test_reset(self):
        a = SpatialActivityAnalyzer(diff_detect.ActivityAnalyzer())
        for i in range(20):
            sig = a.update(scene_normal_class(i))
            a.learn_baseline(sig["activity"])
        a.reset()
        assert a.frame_count == 0
        assert not a.baseline.ready


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
