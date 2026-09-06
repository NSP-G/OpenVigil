# -*- coding: utf-8 -*-
"""Vigil 核心逻辑单元测试。
运行：python -m pytest tests/ -v
"""
import json
import os
import sys
import tempfile
import time

import pytest
from PIL import Image

# 把项目根加入 path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import diff_detect
from app.zhipu_client import parse_verdict, _coerce_bool, _coerce_confidence
from app import config as config_mod


# ============ diff_detect 测试 ============

class TestDiffDetect:
    def _make_img(self, color, size=(128, 128)):
        return Image.new("RGB", size, color)

    def test_identical_frames_diff_zero(self):
        a = self._make_img((100, 100, 100))
        b = self._make_img((100, 100, 100))
        assert diff_detect.mean_abs_diff(a, b) == 0.0
        assert diff_detect.frame_changed(a, b, 0.001) is False

    def test_black_vs_white_diff_near_one(self):
        a = self._make_img((0, 0, 0))
        b = self._make_img((255, 255, 255))
        d = diff_detect.mean_abs_diff(a, b)
        assert 0.95 < d <= 1.0

    def test_slight_noise_below_threshold(self):
        a = self._make_img((100, 100, 100))
        b = self._make_img((101, 101, 101))  # 差 1 个灰度级
        d = diff_detect.mean_abs_diff(a, b)
        assert d < 0.01  # 64x64 缩放后差异很小

    def test_different_sizes_handled(self):
        a = self._make_img((100, 100, 100), size=(64, 64))
        b = self._make_img((200, 200, 200), size=(256, 256))
        d = diff_detect.mean_abs_diff(a, b)
        assert d > 0

    def test_threshold_boundary(self):
        a = self._make_img((100, 100, 100))
        b = self._make_img((100, 100, 100))
        # 完全相同的帧，任何正阈值都不应触发
        assert diff_detect.frame_changed(a, b, 0.0) is True  # 阈值 0 时 diff>=0 触发
        assert diff_detect.frame_changed(a, b, 0.0001) is False


# ============ parse_verdict 测试 ============

class TestParseVerdict:
    def test_normal_word(self):
        abnormal, info = parse_verdict("正常")
        assert abnormal is False

    def test_no_abnormal_phrase(self):
        for text in [
            "画面正常，没有异常",
            "未发现异常情况",
            "无异常，一切正常",
            "未检测到异常",
            "不存在异常",
            "没什么异常，学生都在认真自习",
            "所有学生均正常自习",
            "课堂秩序都正常",
            "学生走动属于正常情况",
        ]:
            abnormal, _ = parse_verdict(text)
            assert abnormal is False, f"应判正常: {text}"

    def test_abnormal_phrase(self):
        for text in [
            "有异常：学生在打闹",
            "发现异常情况，有学生离岗",
            "检测到异常：人员聚集",
            "存在异常，需要注意",
        ]:
            abnormal, _ = parse_verdict(text)
            assert abnormal is True, f"应判异常: {text}"

    def test_negated_abnormal_words_not_misjudged(self):
        """否定修饰的异常词不应误判（回归测试）。"""
        for text in [
            "未发现疑似打闹的情况",
            "没有发现异常行为",
            "不需要注意，画面平稳",
            "无需特别关注",
        ]:
            abnormal, _ = parse_verdict(text)
            assert abnormal is False, f"否定语境不应判异常: {text}"

    def test_not_normal_phrase_is_abnormal(self):
        """“不太正常/不正常”不能被“正常”子串判成正常。"""
        for text in ["画面看起来不太正常", "课堂状态不正常"]:
            abnormal, _ = parse_verdict(text)
            assert abnormal is True, f"应判异常: {text}"

    def test_json_abnormal(self):
        text = '{"abnormal": true, "type": "学生打闹", "detail": "两名学生在教室后排打架", "confidence": 0.85}'
        abnormal, info = parse_verdict(text)
        assert abnormal is True
        assert info.get("type") == "学生打闹"
        assert abs(info.get("confidence") - 0.85) < 1e-9

    def test_json_normal(self):
        text = '根据观察，画面正常。{"abnormal": false, "detail": "学生均在座位上"}'
        abnormal, info = parse_verdict(text)
        assert abnormal is False

    def test_json_string_bool_normalized(self):
        """字符串 "false" 不能被 bool() 误判为 True（回归测试）。"""
        abnormal, _ = parse_verdict('{"abnormal": "false"}')
        assert abnormal is False
        abnormal2, _ = parse_verdict('{"abnormal": "true"}')
        assert abnormal2 is True

    def test_json_percent_confidence_normalized(self):
        """百分制置信度 80 应换算为 0.8，而不是当成 80 导致永远告警。"""
        _, info = parse_verdict('{"abnormal": true, "confidence": 80}')
        assert abs(info["confidence"] - 0.8) < 1e-9

    def test_json_string_confidence_normalized(self):
        _, info = parse_verdict('{"abnormal": true, "confidence": "0.7"}')
        assert abs(info["confidence"] - 0.7) < 1e-9

    def test_json_bad_confidence_not_crash(self):
        """非法置信度文本不应让解析崩溃，回退默认值。"""
        abnormal, info = parse_verdict('{"abnormal": true, "confidence": "高"}')
        assert abnormal is True
        assert isinstance(info["confidence"], float)

    def test_empty_text(self):
        abnormal, info = parse_verdict("")
        assert abnormal is False

    def test_none_text(self):
        abnormal, info = parse_verdict(None)
        assert abnormal is False

    def test_ambiguous_conservative_abnormal(self):
        # 无法判断时保守按异常（宁可多提醒）
        abnormal, info = parse_verdict("画面有点模糊，看不太清")
        assert abnormal is True
        assert info.get("confidence", 1.0) <= 0.5

    def test_normal_in_long_text(self):
        abnormal, _ = parse_verdict("仔细观察了整个教室，所有学生都在自己的座位上认真学习，没有打闹或离岗情况，整体正常。")
        assert abnormal is False


# ============ 类型规范化函数测试 ============

class TestCoercers:
    def test_coerce_bool_variants(self):
        assert _coerce_bool(True) is True
        assert _coerce_bool(False) is False
        assert _coerce_bool(1) is True
        assert _coerce_bool(0) is False
        assert _coerce_bool("true") is True
        assert _coerce_bool("FALSE") is False
        assert _coerce_bool("是") is True
        assert _coerce_bool("否") is False
        assert _coerce_bool("乱七八糟", default=True) is True

    def test_coerce_confidence_variants(self):
        assert abs(_coerce_confidence(0.5) - 0.5) < 1e-9
        assert abs(_coerce_confidence("0.9") - 0.9) < 1e-9
        assert abs(_coerce_confidence(90) - 0.9) < 1e-9  # 百分制
        assert _coerce_confidence(-1) == 0.0  # 钳制下界
        assert abs(_coerce_confidence(5) - 0.05) < 1e-9  # 5 按百分制 → 0.05
        assert _coerce_confidence(150) == 1.0  # 百分制 150 → 1.5 → 钳制上界
        assert abs(_coerce_confidence("abc", 0.3) - 0.3) < 1e-9  # 非法回退
        assert abs(_coerce_confidence(None, 0.4) - 0.4) < 1e-9


# ============ config 读写测试 ============

class TestConfig:
    def test_default_config_has_all_keys(self):
        cfg = dict(config_mod.DEFAULT_CONFIG)
        required = ["api_key", "model_daily", "model_recheck", "capture_interval_sec",
                    "diff_threshold", "alert_confidence", "alert_image_dir"]
        for k in required:
            assert k in cfg, f"缺少配置项: {k}"

    def test_default_model_is_free(self):
        assert config_mod.DEFAULT_CONFIG["model_daily"] == "glm-4.1v-thinking-flash"
        assert config_mod.DEFAULT_CONFIG["model_recheck"] == "glm-4.1v-thinking-flash"

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "config.json")
            cfg = dict(config_mod.DEFAULT_CONFIG)
            cfg["api_key"] = "test-key-12345"
            cfg["alert_image_dir"] = "/tmp/test_alerts"
            cfg["capture_interval_sec"] = 10
            config_mod.save_config(cfg, path)

            loaded, errors = config_mod.load_config(path)
            assert loaded["api_key"] == "test-key-12345"
            assert loaded["alert_image_dir"] == "/tmp/test_alerts"
            assert loaded["capture_interval_sec"] == 10

    def test_ensure_config_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original = config_mod.writable_config_path
            config_mod.writable_config_path = lambda: os.path.join(tmpdir, "config.json")
            try:
                path = config_mod.ensure_config()
                assert os.path.exists(path)
                with open(path) as f:
                    data = json.load(f)
                assert "api_key" in data
            finally:
                config_mod.writable_config_path = original

    def test_load_missing_file_returns_default(self):
        loaded, errors = config_mod.load_config("/nonexistent/path/config.json")
        assert loaded["model_daily"] == "glm-4.1v-thinking-flash"
        assert len(errors) > 0

    def test_bad_numeric_value_falls_back(self):
        """手改 config.json 写入非法数字时应回退默认并报错，而不是运行时崩溃。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "config.json")
            data = dict(config_mod.DEFAULT_CONFIG)
            data["capture_interval_sec"] = "abc"
            data["api_key"] = "k"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            loaded, errors = config_mod.load_config(path)
            assert loaded["capture_interval_sec"] == config_mod.DEFAULT_CONFIG["capture_interval_sec"]
            assert any("capture_interval_sec" in e for e in errors)

    def test_out_of_range_numeric_clamped(self):
        """超范围数值应被钳制到合法区间。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "config.json")
            data = dict(config_mod.DEFAULT_CONFIG)
            data["alert_confidence"] = 5.0  # 超出 [0,1]
            data["api_key"] = "k"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            loaded, _ = config_mod.load_config(path)
            assert loaded["alert_confidence"] == 1.0

    def test_broken_json_returns_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ this is not valid json ]")
            loaded, errors = config_mod.load_config(path)
            assert loaded["model_daily"] == "glm-4.1v-thinking-flash"
            assert any("解析失败" in e for e in errors)


# ============ 异常帧文件名测试 ============

class TestAlertFrameNaming:
    def test_safe_type_chinese(self):
        etype = "学生打闹/聚集"
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in etype)[:30]
        assert "/" not in safe
        assert "\\" not in safe
        assert len(safe) <= 30

    def test_safe_type_special_chars(self):
        etype = "异常: <script> alert('xss') </script>"
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in etype)[:30]
        assert "<" not in safe
        assert ">" not in safe
        assert ":" not in safe

    def test_filename_format_with_frame_no(self):
        """新文件名格式含帧号，防止同秒两次异常互相覆盖。"""
        ts = time.strftime("%Y%m%d_%H%M%S")
        frame_no = 42
        safe_type = "学生离岗"
        filename = f"alert_{ts}_f{frame_no:06d}_{safe_type}.jpg"
        assert filename.startswith("alert_")
        assert filename.endswith(".jpg")
        assert ts in filename
        assert "f000042" in filename
        assert safe_type in filename

    def test_same_second_different_frame_no(self):
        """同秒不同帧号生成不同文件名。"""
        ts = "20260905_150000"
        names = [f"alert_{ts}_f{n:06d}_t.jpg" for n in (1, 2)]
        assert names[0] != names[1]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
