# -*- coding: utf-8 -*-
"""Vigil 报警系统 · 真实 API 自检脚本。

用途：用真实的智谱视觉模型跑一遍报警判定链路，把每个环节都打印出来，
     用于确认"班级很乱到底会不会报警"，以及看清模型到底回了什么。

用法：
    python tools/selftest_api.py                      # 用 config.json 里的 key
    python tools/selftest_api.py --key 你的key         # 临时指定 key
    python tools/selftest_api.py --images 目录         # 用自己的真实截图测试
    python tools/selftest_api.py --window 窗口标题关键字 # 直接抓真实窗口

输出：每个场景的原始模型回复、解析结果、置信度、最终是否告警。
"""
import argparse
import os
import sys

# 允许直接以脚本方式运行时也能 import app 包
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from PIL import Image, ImageDraw  # noqa: E402

from app import config as config_mod  # noqa: E402
from app.zhipu_client import (  # noqa: E402
    ZhipuVisionClient, ZhipuError, parse_verdict,
)
from app.monitor import Monitor  # noqa: E402


SEP = "=" * 74
SUB = "-" * 74


def make_classroom(frame_idx=0, chaos=0.0, seed=0):
    """生成模拟教室画面。chaos 越大越乱。"""
    w, h = 640, 400
    img = Image.new("RGB", (w, h), (232, 228, 220))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 60], fill=(205, 200, 190))
    d.rectangle([0, h - 70, w, h], fill=(215, 210, 200))

    state = [seed * 1000 + frame_idx or 1]

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
        d.ellipse([x, y, x + 22, y + 22], fill=(60, 55, 50))
        d.rectangle([x + 2, y + 22, x + 20, y + 48], fill=(70, 90, 140))
    if chaos > 0.5:
        for _ in range(int(chaos * 8)):
            x = 300 + int(rnd() * 120) - 60
            y = 250 + int(rnd() * 80) - 40
            d.ellipse([x, y, x + 24, y + 24], fill=(70, 60, 55))
            d.rectangle([x + 2, y + 24, x + 22, y + 52], fill=(150, 70, 70))
    return img


def print_verdict(tag, text):
    """打印一次模型回复及其解析结果。"""
    abnormal, info = parse_verdict(text)
    print(f"    [{tag}] 原始回复：")
    for line in str(text).splitlines():
        print(f"        {line}")
    print(f"    [{tag}] 解析 → abnormal={abnormal} "
          f"confidence={info.get('confidence')} type={info.get('type', '-')}")
    return abnormal, info


def run_scenario(client, cfg, name, image, expect_alert):
    """跑一个场景：初判 + 复核（独立第二意见），输出完整链路。"""
    print(SUB)
    print(f"场景：{name}")
    print(f"预期：{'应当告警' if expect_alert else '不应告警'}")
    prompt = cfg.get("prompt") or config_mod.DEFAULT_PROMPT
    base_conf = float(cfg.get("alert_confidence", 0.45))

    # 初判
    try:
        r1 = client.analyze(image, prompt, model=cfg.get("model_daily"))
    except ZhipuError as e:
        print(f"    [初判] API 调用失败：{e}")
        return None
    a1, i1 = print_verdict("初判", r1)

    verdict, confidence, agreed = a1, float(i1.get("confidence", 0.5)), None
    if a1:
        # 独立第二意见
        recheck_prompt = prompt + (
            "\n\n【独立复核】请忽略任何已有的判断结论，把这当作一次全新的观察，"
            "独立给出你自己的判断。你的结论允许与他人不同——如果画面里确实能看到"
            "多人离座走动、聚集、打闹等情形，就如实判为异常；如果画面确实平稳，"
            "就判为正常。请不要为了让结论与他人一致而修改自己的判断。"
        )
        try:
            r2 = client.analyze(image, recheck_prompt, model=cfg.get("model_recheck"))
        except ZhipuError as e:
            print(f"    [复核] API 调用失败：{e}")
            r2 = None
        if r2 is not None:
            a2, i2 = print_verdict("复核", r2)
            agreed = bool(a2)
            if a2:
                confidence = min(1.0, max(confidence, float(i2.get("confidence", 0.5))) + 0.12)
            else:
                confidence = max(0.0, confidence - 0.15)

    will_alert = bool(verdict) and confidence >= base_conf
    print(f"    → 融合置信度 {confidence:.2f} / 告警阈值 {base_conf:.2f}")
    print(f"    → 最终判定：{'【告警】' if will_alert else '不告警'}")

    ok = (will_alert == expect_alert)
    print(f"    → 与预期{'一致 ✓' if ok else '不符 ✗'}")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Vigil 报警系统真实 API 自检")
    ap.add_argument("--key", default=None, help="临时指定 API Key（不写入配置文件）")
    ap.add_argument("--images", default=None, help="用指定目录下的真实截图测试")
    ap.add_argument("--window", default=None, help="抓取标题含该关键字的真实窗口来测试")
    ap.add_argument("--save", default=None, help="把生成的合成测试图保存到该目录")
    args = ap.parse_args()

    cfg, errors = config_mod.load_config()
    for e in errors:
        print(f"[配置提示] {e}")
    if args.key:
        cfg["api_key"] = args.key
    if not cfg.get("api_key"):
        print("错误：未提供 API Key。请在 config.json 填写，或用 --key 传入。")
        return 1

    print(SEP)
    print("Vigil 报警系统 · 真实 API 自检")
    print(SEP)
    print(f"模型：{cfg.get('model_daily')} / 复核：{cfg.get('model_recheck')}")
    print(f"告警阈值：alert_confidence={cfg.get('alert_confidence')}")
    print(f"活动度门控：activity_trigger={cfg.get('activity_trigger')}")
    print(SEP)

    client = ZhipuVisionClient(
        api_key=cfg["api_key"],
        api_base=cfg.get("api_base"),
        model=cfg.get("model_daily"),
    )

    scenarios = []

    if args.window:
        # 真实窗口模式
        try:
            from app import window_capture
            win = window_capture.window_by_title(args.window)
            if win is None:
                print(f"未找到标题含「{args.window}」的窗口")
                return 1
            print(f"已定位窗口：{win.title}")
            img = window_capture.capture_window(win.hwnd)
            scenarios.append((f"真实窗口「{win.title}」", img, None))
        except Exception as e:
            print(f"抓取窗口失败：{e}")
            return 1
    elif args.images:
        # 真实截图目录
        if not os.path.isdir(args.images):
            print(f"目录不存在：{args.images}")
            return 1
        files = sorted(
            f for f in os.listdir(args.images)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
        )
        if not files:
            print(f"目录中没有图片：{args.images}")
            return 1
        for f in files:
            try:
                scenarios.append((f"图片 {f}", Image.open(os.path.join(args.images, f)), None))
            except Exception as e:
                print(f"读取 {f} 失败：{e}")
    else:
        # 合成图模式：覆盖典型场景
        scenarios = [
            ("安静自习（应不告警）", make_classroom(chaos=0.0), False),
            ("局部轻微动作（应不告警）", make_classroom(frame_idx=3, chaos=0.05), False),
            ("班级混乱·多人离座走动（应告警）", make_classroom(frame_idx=2, chaos=0.8, seed=7), True),
            ("班级混乱·聚集打闹（应告警）", make_classroom(frame_idx=5, chaos=1.0, seed=11), True),
        ]
        if args.save:
            os.makedirs(args.save, exist_ok=True)
            for name, img, _ in scenarios:
                safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:40]
                img.save(os.path.join(args.save, f"{safe}.jpg"), "JPEG", quality=90)
            print(f"合成测试图已保存到：{args.save}")

    results = []
    try:
        for name, img, expect in scenarios:
            ok = run_scenario(client, cfg, name, img, expect)
            if ok is not None:
                results.append((name, ok))
    finally:
        client.close()

    print(SEP)
    print("自检汇总")
    print(SEP)
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    passed = sum(1 for _, ok in results if ok)
    print(f"\n共 {len(results)} 项，符合预期 {passed} 项。")
    print(SEP)
    print("说明：合成图只是形态模拟，模型在合成图上的表现不能完全代表真实监控画面。")
    print("      要验证真实效果，请用 --window 窗口标题 或 --images 真实截图目录。")
    print(SEP)
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
