# -*- coding: utf-8 -*-
"""主题（浅色/深色/跟随系统）功能测试。

运行：python -m pytest tests/test_theme.py -v
"""
import json
import os
import re
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import config as config_mod
from gui import main_gui


CSS_PATH = os.path.join(PROJECT_ROOT, "gui", "web", "css", "style.css")
JS_PATH = os.path.join(PROJECT_ROOT, "gui", "web", "js", "app.js")
HTML_PATH = os.path.join(PROJECT_ROOT, "gui", "web", "index.html")


# ==================== 配置层 ====================

class TestThemeConfig:
    def test_theme_in_defaults(self):
        assert "theme" in config_mod.DEFAULT_CONFIG

    def test_default_is_auto(self):
        """默认应跟随系统，而不是硬钉在深色。"""
        assert config_mod.DEFAULT_CONFIG["theme"] == "auto"

    def test_themes_constant(self):
        assert set(config_mod.THEMES) == {"auto", "light", "dark"}

    def test_valid_values_kept(self):
        for v in ("auto", "light", "dark"):
            assert config_mod._coerce_theme(v) == v

    def test_case_insensitive(self):
        """手改配置写成 "Dark" 也应被接受，而不是静默回退。"""
        assert config_mod._coerce_theme("Dark") == "dark"
        assert config_mod._coerce_theme(" LIGHT ") == "light"

    def test_invalid_values_fall_back(self):
        for v in ("深色", "blue", "", None, 123, ["dark"]):
            assert config_mod._coerce_theme(v) == "auto", f"{v!r} 应回退为 auto"

    def test_invalid_theme_reports_error(self, tmp_path):
        """非法值应给出明确提示，让用户知道配置没生效。"""
        path = tmp_path / "config.json"
        data = dict(config_mod.DEFAULT_CONFIG)
        data["theme"] = "深色"
        data["api_key"] = "k"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        cfg, errors = config_mod.load_config(str(path))
        assert cfg["theme"] == "auto"
        assert any("theme" in e for e in errors)

    def test_theme_roundtrip(self, tmp_path):
        path = str(tmp_path / "config.json")
        for v in ("auto", "light", "dark"):
            cfg = dict(config_mod.DEFAULT_CONFIG)
            cfg["theme"] = v
            config_mod.save_config(cfg, path)
            loaded, _ = config_mod.load_config(path)
            assert loaded["theme"] == v, f"{v} 未能持久化"

    def test_missing_theme_defaults_auto(self, tmp_path):
        """老版本 config.json 没有 theme 字段时，应平滑升级为跟随系统。"""
        path = tmp_path / "config.json"
        data = dict(config_mod.DEFAULT_CONFIG)
        data.pop("theme")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        loaded, _ = config_mod.load_config(str(path))
        assert loaded["theme"] == "auto"


# ==================== 后端主题解析 ====================

class TestResolveTheme:
    def test_explicit_values(self, monkeypatch):
        monkeypatch.setattr(main_gui, "detect_system_theme", lambda: "light")
        assert main_gui.resolve_theme({"theme": "dark"}) == "dark"
        assert main_gui.resolve_theme({"theme": "light"}) == "light"

    def test_auto_follows_system(self, monkeypatch):
        monkeypatch.setattr(main_gui, "detect_system_theme", lambda: "dark")
        assert main_gui.resolve_theme({"theme": "auto"}) == "dark"
        monkeypatch.setattr(main_gui, "detect_system_theme", lambda: "light")
        assert main_gui.resolve_theme({"theme": "auto"}) == "light"

    def test_missing_key_defaults_auto(self, monkeypatch):
        monkeypatch.setattr(main_gui, "detect_system_theme", lambda: "light")
        assert main_gui.resolve_theme({}) == "light"

    def test_bogus_value_falls_back_to_dark(self, monkeypatch):
        monkeypatch.setattr(main_gui, "detect_system_theme", lambda: "light")
        assert main_gui.resolve_theme({"theme": "blue"}) == "dark"

    def test_detect_system_theme_never_crashes(self):
        """注册表/命令不可用时也必须返回一个合法值。"""
        result = main_gui.detect_system_theme()
        assert result in ("dark", "light")


class TestWindowBackground:
    def test_bg_matches_css(self):
        """窗口底色必须与 CSS 的 --bg 一致，否则启动时才会闪色。"""
        css = open(CSS_PATH, encoding="utf-8").read()
        dark_block = css[css.index('html[data-theme="dark"]'):]
        dark_block = dark_block[:dark_block.index("}")]
        light_block = css[css.index('html[data-theme="light"]'):]
        light_block = light_block[:light_block.index("}")]

        dark_bg = re.search(r"--bg:\s*(#[0-9A-Fa-f]{6})", dark_block).group(1)
        light_bg = re.search(r"--bg:\s*(#[0-9A-Fa-f]{6})", light_block).group(1)

        assert main_gui._WINDOW_BG["dark"].upper() == dark_bg.upper(), \
            f"窗口深色底 {main_gui._WINDOW_BG['dark']} 与 CSS --bg {dark_bg} 不一致"
        assert main_gui._WINDOW_BG["light"].upper() == light_bg.upper(), \
            f"窗口浅色底 {main_gui._WINDOW_BG['light']} 与 CSS --bg {light_bg} 不一致"

    def test_bg_values_are_hex(self):
        for v in main_gui._WINDOW_BG.values():
            assert re.fullmatch(r"#[0-9A-Fa-f]{6}", v), v


# ==================== 前端资源一致性 ====================

class TestFrontendWiring:
    def test_theme_radios_exist(self):
        html = open(HTML_PATH, encoding="utf-8").read()
        for v in ("auto", "light", "dark"):
            assert f'name="theme" value="{v}"' in html, f"缺少 {v} 选项"

    def test_default_checked_is_auto(self):
        html = open(HTML_PATH, encoding="utf-8").read()
        auto_block = re.search(
            r'<input type="radio" name="theme" value="auto"([^>]*)>', html)
        assert auto_block, "未找到 auto 选项"
        assert "checked" in auto_block.group(1), "auto 应为默认选中"

    def test_theme_hint_element(self):
        html = open(HTML_PATH, encoding="utf-8").read()
        assert 'id="themeHint"' in html

    def test_js_reads_theme_elements(self):
        js = open(JS_PATH, encoding="utf-8").read()
        assert 'getElementsByName("theme")' in js
        assert 'getElementById("themeHint")' in js

    def test_js_has_theme_functions(self):
        js = open(JS_PATH, encoding="utf-8").read()
        for fn in ("applyTheme", "resolveTheme", "systemPrefersDark",
                   "watchSystemTheme", "currentTheme"):
            assert re.search(rf"function {fn}\s*\(", js), f"缺少函数 {fn}"

    def test_theme_sent_on_save(self):
        """保存设置时必须带上主题，否则重启后主题丢失。"""
        js = open(JS_PATH, encoding="utf-8").read()
        save_block = js[js.index("async function saveSettings"):]
        save_block = save_block[:save_block.index("try {")]
        assert "theme:" in save_block

    def test_theme_loaded_in_settings(self):
        js = open(JS_PATH, encoding="utf-8").read()
        load_block = js[js.index("async function loadSettings"):]
        load_block = load_block[:load_block.index("} catch (e) {")]
        assert "applyTheme" in load_block

    def test_apply_theme_event_handled(self):
        js = open(JS_PATH, encoding="utf-8").read()
        assert 'case "apply_theme"' in js

    def test_system_theme_watched(self):
        js = open(JS_PATH, encoding="utf-8").read()
        assert "watchSystemTheme()" in js
        assert "prefers-color-scheme" in js


# ==================== CSS 变量完整性 ====================

class TestCssThemes:
    @classmethod
    @pytest.fixture(scope="class")
    def css(cls):
        return open(CSS_PATH, encoding="utf-8").read()

    def _block(self, css, marker):
        i = css.index(marker)
        depth = 0
        for j in range(i, len(css)):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    return css[i:j]
        raise AssertionError(f"未找到 {marker} 的闭合")

    def test_both_themes_define_same_vars(self, css):
        dark = set(re.findall(r"(--[a-z0-9-]+)\s*:", self._block(css, 'html[data-theme="dark"]')))
        light = set(re.findall(r"(--[a-z0-9-]+)\s*:", self._block(css, 'html[data-theme="light"]')))
        assert dark == light, f"深浅变量不一致: {dark ^ light}"

    def test_all_used_vars_defined(self, css):
        used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
        missing = used - defined
        assert not missing, f"使用了未定义的变量: {missing}"

    def test_no_hardcoded_colors(self, css):
        """主题色必须全部走变量，否则切换主题时会有残留色块。"""
        offenders = []
        for i, line in enumerate(css.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("--"):
                continue          # 变量定义处允许字面量
            if re.search(r"(?:^|[:\s])(?:color|background|background-color|border(?:-color)?)\s*:\s*[^;]*(?:#[0-9A-Fa-f]{3,8}|rgba?\()", stripped):
                offenders.append(f"第{i}行: {stripped}")
        assert not offenders, "存在硬编码颜色:\n" + "\n".join(offenders)

    def test_color_scheme_declared(self, css):
        """color-scheme 让原生滚动条/下拉跟随主题。"""
        assert 'color-scheme: dark' in css
        assert 'color-scheme: light' in css

    def test_light_theme_text_is_dark(self, css):
        """浅色主题下正文必须是深色，否则会白底白字。"""
        light = self._block(css, 'html[data-theme="light"]')
        text = re.search(r"--text:\s*#([0-9A-Fa-f]{6})", light).group(1)
        bg = re.search(r"--bg:\s*#([0-9A-Fa-f]{6})", light).group(1)
        lum = lambda h: sum(int(h[i:i + 2], 16) for i in (0, 2, 4)) / 3
        assert lum(text) < 128, f"浅色主题正文不够深: #{text}"
        assert lum(bg) > 128, f"浅色主题背景不够亮: #{bg}"

    def test_dark_theme_text_is_light(self, css):
        dark = self._block(css, 'html[data-theme="dark"]')
        text = re.search(r"--text:\s*#([0-9A-Fa-f]{6})", dark).group(1)
        lum = lambda h: sum(int(h[i:i + 2], 16) for i in (0, 2, 4)) / 3
        assert lum(text) > 128, f"深色主题正文不够亮: #{text}"

    def test_accent_contrast_in_light(self, css):
        """浅底上的强调色需压暗，否则主按钮文字看不清。"""
        light = self._block(css, 'html[data-theme="light"]')
        accent = re.search(r"--accent:\s*#([0-9A-Fa-f]{6})", light).group(1)
        lum = lambda h: sum(int(h[i:i + 2], 16) for i in (0, 2, 4)) / 3
        assert lum(accent) < 160, f"浅色主题强调色过亮: #{accent}"


# ==================== 对比度可达性 ====================

def _hex(v):
    return v.strip().lstrip("#")


def _rel_luminance(hex_color):
    """WCAG 相对亮度。"""
    h = _hex(hex_color)
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(fg, bg):
    """WCAG 对比度比值（1~21）。"""
    l1, l2 = _rel_luminance(fg), _rel_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


class TestContrast:
    """深浅两套主题下，关键文字/背景组合都要读得清。

    阈值参考 WCAG AA：正文 4.5:1，次要文字（时间戳、说明）放宽到 3:1。
    """

    @classmethod
    @pytest.fixture(scope="class")
    def themes(cls):
        css = open(CSS_PATH, encoding="utf-8").read()

        def block(marker):
            i = css.index(marker)
            return css[i:css.index("}", i)]

        out = {}
        for name, marker in (("dark", 'html[data-theme="dark"]'),
                             ("light", 'html[data-theme="light"]')):
            b = block(marker)
            # 去掉 "--" 前缀，便于用 ("text", "bg") 这样的简写索引
            out[name] = {k[2:]: v.strip() for k, v in
                         re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})", b)}
        return out

    # (前景变量, 背景变量, 最低对比度, 说明)
    # 背景同时覆盖 panel 与 panel-2：告警卡片、主题选项等都用次级面板底色
    COMBINATIONS = [
        ("text", "bg", 4.5, "正文 / 窗口底色"),
        ("text", "panel", 4.5, "正文 / 面板"),
        ("text", "panel-2", 4.5, "正文 / 次级面板"),
        ("text-dim", "panel", 4.5, "次要文字 / 面板"),
        ("text-dim", "panel-2", 4.5, "次要文字 / 次级面板"),
        ("text-dim", "bg", 4.5, "次要文字 / 窗口底色"),
        ("text-faint", "panel", 3.0, "弱化文字 / 面板"),
        ("text-faint", "panel-2", 3.0, "弱化文字 / 次级面板"),
        ("accent", "panel", 3.0, "强调色文字 / 面板"),
        ("accent", "panel-2", 3.0, "强调色文字 / 次级面板"),
        ("alert", "panel", 3.0, "告警文字 / 面板"),
        ("alert", "panel-2", 3.0, "告警文字 / 次级面板"),
        ("ok", "panel", 3.0, "正常状态文字 / 面板"),
        ("ok", "panel-2", 3.0, "正常状态文字 / 次级面板"),
        ("info", "panel", 3.0, "提示文字 / 面板"),
        ("info", "panel-2", 3.0, "提示文字 / 次级面板"),
        ("on-accent", "accent", 4.5, "主按钮文字 / 主按钮底色"),
    ]

    @pytest.mark.parametrize("fg,bg,minimum,desc", COMBINATIONS)
    @pytest.mark.parametrize("theme", ["dark", "light"])
    def test_contrast_ratio(self, themes, theme, fg, bg, minimum, desc):
        colors = themes[theme]
        assert fg in colors and bg in colors, f"{theme} 主题缺少变量 {fg}/{bg}"
        ratio = _contrast(colors[fg], colors[bg])
        assert ratio >= minimum, (
            f"{theme} 主题 {desc}（--{fg} on --{bg}）对比度仅 {ratio:.2f}:1，"
            f"低于要求 {minimum}:1（前景 {colors[fg]} / 背景 {colors[bg]}）"
        )

    @pytest.mark.parametrize("theme", ["dark", "light"])
    def test_report_all_ratios(self, themes, theme):
        """输出全部组合的实测对比度，便于人工复核配色观感。"""
        colors = themes[theme]
        print(f"\n  [{theme}] 对比度实测：")
        for fg, bg, minimum, desc in self.COMBINATIONS:
            ratio = _contrast(colors[fg], colors[bg])
            flag = "✓" if ratio >= minimum else "✗"
            print(f"    {flag} {desc:<22} {ratio:5.2f}:1  (要求 ≥{minimum})")
        assert True  # 实际断言由 test_contrast_ratio 承担


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
