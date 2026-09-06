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
import shutil
import sys
import tempfile

# 允许直接以脚本方式运行时也能 import app 包
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from PIL import Image, ImageDraw  # noqa: E402

from app import config as config_mod  # noqa: E402
from app import diff_detect  # noqa: E402
from app.deliberation import Deliberation  # noqa: E402
from app.md_memory import MemoryStore  # noqa: E402
from app.zhipu_client import (  # noqa: E402
    ZhipuVisionClient, ZhipuError, parse_verdict,
    build_diff_prompt, build_observe_prompt, parse_diff, parse_observation,
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


def make_classroom_with_poster(gather=False):
    """生成「左墙新贴了一张分组表」的教室画面。

    gather=True 时，一群学生聚集在表格前围观——
    这是用户提出的真实痛点场景：画面上是"一群人凑在一起"，
    单帧看与打闹几乎无异，但实质是响应新张贴内容的正常聚集。
    """
    img = make_classroom(chaos=0.0)
    d = ImageDraw.Draw(img)
    # 左墙贴一张表格
    d.rectangle([10, 95, 62, 210], fill=(252, 250, 244))
    d.rectangle([10, 95, 62, 210], outline=(80, 80, 80), width=2)
    d.text((16, 100), "分组表", fill=(40, 40, 40))
    for i in range(1, 7):
        y = 100 + i * 17
        d.line([14, y, 58, y], fill=(150, 150, 150))
    if gather:
        # 六名学生围在表格前，全部朝向墙面（左侧）
        for i in range(6):
            x = 70 + (i % 3) * 30
            y = 110 + (i // 3) * 55
            d.ellipse([x, y, x + 22, y + 22], fill=(60, 55, 50))
            d.rectangle([x + 2, y + 22, x + 20, y + 48], fill=(70, 90, 140))
    return img


def print_observation(tag, text):
    """打印结构化观测的解析结果（v0.3 新格式）。"""
    info = parse_observation(text)
    print(f"    [{tag}] 原始回复：")
    for line in str(text).splitlines()[:12]:
        print(f"        {line}")
    print(f"    [{tag}] 解析 → category={info.get('category')} "
          f"abnormal={info.get('abnormal')} "
          f"confidence={info.get('confidence')}")
    if info.get("facing_consistency") is not None:
        print(f"    [{tag}] 朝向一致度 = {info['facing_consistency']}")
    if info.get("observation"):
        print(f"    [{tag}] 观测：{info['observation']}")
    if info.get("description"):
        print(f"    [{tag}] 解读：{info['description']}")
    return info


def run_structured_scenario(client, cfg, name, images, expect_category,
                            expect_alert, use_multi=False, memory_seed=None):
    """跑一个 v0.3 结构化观测场景（先描述再判断 + 类别选择）。

    use_multi=False 走单帧主路径（与 Monitor 默认一致）；
    use_multi=True 单独验证多帧输入是否可用。
    memory_seed 预置记忆，模拟系统已积累的场景认知。
    """
    print(SUB)
    tag = "多帧" if use_multi else "单帧"
    print(f"场景：{name}（{tag}）")
    print(f"预期类别：{expect_category}｜预期：{'应告警' if expect_alert else '不应告警'}")
    prompt = build_observe_prompt(
        cfg.get("prompt") or config_mod.DEFAULT_PROMPT,
        multi_frame=use_multi,
    )
    base_conf = float(cfg.get("alert_confidence", 0.45))

    try:
        if use_multi:
            text = client.analyze_multi(images, prompt, model=cfg.get("model_daily"))
        else:
            text = client.analyze(images[0], prompt, model=cfg.get("model_daily"))
    except ZhipuError as e:
        print(f"    API 调用失败：{e}")
        return None
    info = print_observation(f"{tag}观测", text)

    # 截断的回复按设计应跳过该帧，而不是走进保守判异常的兜底
    if info.get("unreliable"):
        print("    ⚠ 回复被截断（unreliable）→ 按设计跳过本帧，不告警")
        ok = (expect_alert is False)
        print(f"    → 告警判定{'正确 ✓' if ok else '不符 ✗'}")
        return ok

    got_category = info.get("category")
    abnormal = bool(info.get("abnormal"))
    confidence = float(info.get("confidence", 0.5))

    # 朝向一致性调节：与 Monitor._apply_context_adjustment 保持一致
    facing = info.get("facing_consistency")
    if facing is not None and got_category in ("gathering", "scuffle"):
        if facing >= 0.75:
            confidence = max(0.0, confidence - 0.25)
            print(f"    朝向一致（{facing:.2f}）→ 判为响应性聚集，置信度降至 {confidence:.2f}")
        elif facing <= 0.35:
            confidence = min(1.0, confidence + 0.15)
            print(f"    朝向混乱（{facing:.2f}）→ 符合打闹特征，置信度升至 {confidence:.2f}")

    # 告警判定直接采用模型结论。
    #
    # 这里刻意**不做**任何类别→是否告警的规则映射：
    # 那类硬编码裁决层已被 architecture 守卫明确否决（见 tests/test_architecture_guard.py）——
    # 判断权属于模型，基础系统只负责安排它怎么看（多轮复演 + 外部记忆）。
    will_alert = abnormal and confidence >= base_conf

    # 多轮复演：与生产一致，只在"即将告警"时才开庭。
    # 曾经本函数跳过复演直接下结论，而生产环境是会过复演的——
    # 结果"学生围观新贴的分组表"被判为告警，而生产里复演会用记忆把它解释掉。
    if will_alert and cfg.get("deliberation_enabled", True):
        tmpdir = tempfile.mkdtemp(prefix="vigil_ds_")
        try:
            store = MemoryStore(tmpdir, max_chars=int(cfg.get("memory_max_chars", 4000)))
            for kind, text in (memory_seed or {}).items():
                store.append(kind, text)
            delib = Deliberation(client, cfg.get("model_daily"),
                                 memory_root=tmpdir,
                                 max_chars=int(cfg.get("memory_max_chars", 4000)))
            print("    → 初判将触发告警，启动多轮复演…")
            result = delib.deliberate(
                images[0],
                {"observation": info.get("observation") or "",
                 "category": got_category,
                 "category_label": info.get("category_label") or "",
                 "abnormal": True, "confidence": confidence},
                on_report=lambda lvl, msg: print(f"    [{lvl}] {msg}"))
            print(f"    → 投票数 {len(result['votes'])}"
                  f"｜一致度 {result['agreement']:.2f}"
                  f"｜出席率 {result.get('attendance', 0):.2f}")
            if result.get("abstained"):
                print(f"    → 弃权：{'、'.join(result['abstained'])}")
            if result.get("note"):
                print(f"    → {result['note']}")
            confidence = result["confidence"]
            will_alert = result["abnormal"] and confidence >= base_conf
            if result["changed"]:
                print(f"    → 复演{'维持' if result['abnormal'] else '推翻'}了初判")
        except Exception as e:
            print(f"    复演异常，沿用初判结论：{e}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"    → 融合置信度 {confidence:.2f} / 告警阈值 {base_conf:.2f}")
    print(f"    → 最终判定：{'【告警】' if will_alert else '不告警'}")

    cat_ok = (got_category == expect_category)
    alert_ok = (will_alert == expect_alert)
    print(f"    → 类别{'正确 ✓' if cat_ok else '不符 ✗（实际 %s）' % got_category}")
    print(f"    → 告警判定{'正确 ✓' if alert_ok else '不符 ✗'}")
    return cat_ok and alert_ok


def run_deliberation_test(client, cfg, frame, primary, expect_abnormal,
                          memory_seed=None, note=None):
    """测试多轮复演：初判异常后，让模型从四个角度独立投票。

    这是验证「判断权在模型手里」的关键场景。
    曾经的做法是用一张硬编码的「类别→告不告警」规则表压制误判，
    那是拿静态规则覆盖模型对当下画面的判断。这里换成正路：
    给模型更多角度和依据，让它自己推翻自己的初判。

    参数：
        primary       —— 初判结果（模拟单帧主路径的输出）
        memory_seed   —— 预置记忆，模拟系统已积累的场景认知
    """
    print(SUB)
    print("场景：多轮复演")
    if note:
        print(f"    {note}")
    print(f"初判：{'异常' if primary.get('abnormal') else '正常'}"
          f"（置信度 {primary.get('confidence', 0.5):.2f}）"
          f"｜预期复演后：{'异常' if expect_abnormal else '正常'}")

    tmpdir = tempfile.mkdtemp(prefix="vigil_delib_")
    try:
        # 预置记忆，模拟系统已经"记住"了墙上新贴的表
        store = MemoryStore(tmpdir, max_chars=int(cfg.get("memory_max_chars", 4000)))
        for kind, text in (memory_seed or {}).items():
            store.append(kind, text)
        if memory_seed:
            print(f"    预置记忆：{memory_seed}")

        delib = Deliberation(client, cfg.get("model_daily"),
                             memory_root=tmpdir,
                             max_chars=int(cfg.get("memory_max_chars", 4000)))

        def report(level, msg):
            print(f"    [{level}] {msg}")

        result = delib.deliberate(frame, primary, on_report=report)

        print(f"    → 投票数 {len(result['votes'])}"
              f"｜一致度 {result['agreement']:.2f}"
              f"｜出席率 {result.get('attendance', 0):.2f}")
        if result.get("abstained"):
            print(f"    → 弃权：{'、'.join(result['abstained'])}")
        if result.get("note"):
            print(f"    → {result['note']}")
        print(f"    → 复演结论：{'异常' if result['abnormal'] else '正常'}"
              f"（置信度 {result['confidence']:.2f}）")
        if result["changed"]:
            print("    → 复演推翻了初判")

        # 这里断言的是**最终会不会告警**，而不是复演判没判异常。
        # 两者不同：复演可能因弃权而判"异常但置信度极低"，
        # 这时系统实际不会惊动老师——从用户角度看，目的已经达到。
        # 断言口径必须对齐真实行为，否则会误报这个测试项。
        base_conf = float(cfg.get("alert_confidence", 0.45))
        will_alert = result["abnormal"] and result["confidence"] >= base_conf
        expect_alert = expect_abnormal
        print(f"    → 是否触发告警：{'是' if will_alert else '否'}"
              f"（阈值 {base_conf:.2f}）｜预期：{'是' if expect_alert else '否'}")

        ok = (will_alert == expect_alert)
        print(f"    → 与预期{'一致 ✓' if ok else '不符 ✗'}")
        return ok
    except Exception as e:
        print(f"    复演测试异常：{e}")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_scene_change_test(client, cfg, before, after, expect_changed=True):
    """测试场景变化检测（差异描述）。

    注意：这里复现生产环境的完整链路——先过感知哈希门控，
    只有哈希差异超过容忍度才真正调用模型。
    跳过门控直接问模型"变了没有"是没有意义的：
    模型在两张几乎相同的图上也会凭空编出差异（实测如此），
    门控正是为了防止这类误报。
    """
    print(SUB)
    print("场景：场景持久性变化检测（左墙新增分组表）")
    print(f"预期：{'应检测到变化' if expect_changed else '不应检测到变化'}")

    # 第一道闸门：持续性局域变化检测（与 Monitor._check_scene_change 一致）。
    # 用检测器而非感知哈希——实测证明全局哈希对「墙上多一张表」极不敏感，
    # 会把真正的变化拦掉。检测器用「持续性 + 紧凑度」双判据，
    # 再让模型做最终裁决。
    det = diff_detect.SceneChangeDetector()
    det.update(before)
    gate = None
    for _ in range(8):
        r = det.update(after)
        if r:
            gate = r
    if gate is None:
        gated_ok = not expect_changed
        print("    → 检测器判定无持续变化，未调用 API")
        print(f"    → 与预期{'一致 ✓' if gated_ok else '不符 ✗'}")
        return gated_ok
    print(f"    → 检测器放行：持续格数={gate['cells']} 紧凑度={gate['fill']}")

    try:
        text = client.analyze_diff(before, after, build_diff_prompt(),
                                   model=cfg.get("model_daily"))
    except ZhipuError as e:
        print(f"    API 调用失败：{e}")
        return None
    print("    原始回复：")
    for line in str(text).splitlines()[:12]:
        print(f"        {line}")
    changed, changes, conf = parse_diff(text)
    print(f"    → changed={changed} confidence={conf:.2f}")
    for c in changes:
        print(f"    → 变化：{c}")
    ok = (changed == expect_changed)
    print(f"    → 与预期{'一致 ✓' if ok else '不符 ✗'}")
    return ok


def print_verdict(tag, text):
    """打印一次模型回复及其解析结果。"""
    abnormal, info = parse_verdict(text)
    print(f"    [{tag}] 原始回复：")
    for line in str(text).splitlines():
        print(f"        {line}")
    print(f"    [{tag}] 解析 → abnormal={abnormal} "
          f"confidence={info.get('confidence')} type={info.get('type', '-')}")
    return abnormal, info


def run_scenario(client, cfg, name, image, expect_alert, memory_seed=None):
    """跑一个场景：初判 + 复核 + 多轮复演，输出完整链路。

    这里刻意走与 Monitor 一致的完整链路：初判 → 复核 → 复演。
    曾经本函数只做初判+复核就下结论，而生产环境是会过复演的——
    结果测出来的"误报"在生产里可能压根不会告警。
    测试口径不对齐生产行为，测出来的东西就没有参考价值。
    """
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

    # 多轮复演：与生产一致，只在"即将告警"时才开庭
    if will_alert and cfg.get("deliberation_enabled", True):
        tmpdir = tempfile.mkdtemp(prefix="vigil_dl_")
        try:
            store = MemoryStore(tmpdir, max_chars=int(cfg.get("memory_max_chars", 4000)))
            for kind, text in (memory_seed or {}).items():
                store.append(kind, text)
            delib = Deliberation(client, cfg.get("model_daily"),
                                 memory_root=tmpdir,
                                 max_chars=int(cfg.get("memory_max_chars", 4000)))
            print(f"    → 初判将触发告警，启动多轮复演…")
            result = delib.deliberate(
                image,
                {"observation": i1.get("detail") or "",
                 "category_label": i1.get("type") or "",
                 "abnormal": True, "confidence": confidence},
                on_report=lambda lvl, msg: print(f"    [{lvl}] {msg}"))
            print(f"    → 投票数 {len(result['votes'])}"
                  f"｜一致度 {result['agreement']:.2f}"
                  f"｜出席率 {result.get('attendance', 0):.2f}")
            if result.get("abstained"):
                print(f"    → 弃权：{'、'.join(result['abstained'])}")
            if result.get("note"):
                print(f"    → {result['note']}")
            confidence = result["confidence"]
            verdict = result["abnormal"] and confidence >= base_conf
            will_alert = bool(verdict)
            if result["changed"]:
                print(f"    → 复演{'维持' if result['abnormal'] else '推翻'}了初判")
        except Exception as e:
            print(f"    复演异常，沿用复核结论：{e}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

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
        # (场景名, 图像, 是否应告警, 预置记忆)
        # 预置记忆模拟系统已积累的场景认知——真实使用中记忆就是这么长出来的。
        scenarios = [
            ("安静自习（应不告警）", make_classroom(chaos=0.0), False, None),
            ("局部轻微动作（应不告警）", make_classroom(frame_idx=3, chaos=0.05), False,
             {"scene": "本班常态：自习课偶有个别学生短暂离座，属正常现象"}),
            ("班级混乱·多人离座走动（应告警）",
             make_classroom(frame_idx=2, chaos=0.8, seed=7), True, None),
            ("班级混乱·聚集打闹（应告警）",
             make_classroom(frame_idx=5, chaos=1.0, seed=11), True, None),
        ]
        if args.save:
            os.makedirs(args.save, exist_ok=True)
            for name, img, _expect, _seed in scenarios:
                safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:40]
                img.save(os.path.join(args.save, f"{safe}.jpg"), "JPEG", quality=90)
            print(f"合成测试图已保存到：{args.save}")

    results = []
    try:
        for name, img, expect, seed in scenarios:
            ok = run_scenario(client, cfg, name, img, expect, memory_seed=seed)
            if ok is not None:
                results.append((name, ok))

        # ---- v0.3 结构化观测场景（多帧 + 先描述再判断 + 类别选择）----
        print(SEP)
        print("v0.3 结构化观测测试")
        # 主路径：单帧 + 结构化提示词（与 Monitor 默认一致）
        # (场景名, 帧列表, 预期类别, 是否应告警, 预置记忆)
        structured = [
            ("新贴分组表·学生围观（应判聚集围观，不告警）",
             [make_classroom_with_poster(gather=True)],
             "gathering", False,
             {"changes": "左墙新贴了一张分组表，学生陆续围观",
              "patterns": "本班在墙上新张贴物后常出现短暂围观，属正常响应"}),
            ("打闹冲突（应判打闹冲突，应告警）",
             [make_classroom(frame_idx=5, chaos=1.0, seed=11)],
             "scuffle", True, None),
            ("安静自习（应判正常上课，不告警）",
             [make_classroom(chaos=0.0)],
             "normal_class", False, None),
        ]
        for name, imgs, exp_cat, exp_alert, seed in structured:
            ok = run_structured_scenario(client, cfg, name, imgs, exp_cat,
                                         exp_alert, use_multi=False,
                                         memory_seed=seed)
            if ok is not None:
                results.append((name, ok))

        # 定向确认路径：仅在聚集/打闹类上追问"在聚还是在散"
        print(SEP)
        print("v0.3 多帧定向确认测试（仅在聚集/打闹类上启用）")
        multi = [
            ("新贴分组表·围观（多帧：应在聚拢/稳定，判聚集，不告警）",
             [make_classroom_with_poster(gather=True),
              make_classroom_with_poster(gather=True)],
             "gathering", False,
             {"changes": "左墙新贴了一张分组表，学生陆续围观"}),
            ("打闹冲突（多帧：应判打闹，应告警）",
             [make_classroom(frame_idx=2, chaos=0.9, seed=7),
              make_classroom(frame_idx=5, chaos=1.0, seed=11)],
             "scuffle", True, None),
        ]
        for name, imgs, exp_cat, exp_alert, seed in multi:
            ok = run_structured_scenario(client, cfg, name, imgs, exp_cat,
                                         exp_alert, use_multi=True,
                                         memory_seed=seed)
            if ok is not None:
                results.append((name + "·多帧", ok))

        # ---- v0.3 场景变化检测 ----
        print(SEP)
        print("v0.3 场景变化检测测试")
        ok = run_scene_change_test(
            client, cfg,
            make_classroom(chaos=0.0),              # 变化前
            make_classroom_with_poster(gather=False),  # 变化后：左墙多了张表
            expect_changed=True)
        if ok is not None:
            results.append(("场景变化检测（左墙新增分组表）", ok))

        # 对照组：两张一致的图不应报变化
        ok = run_scene_change_test(
            client, cfg,
            make_classroom(chaos=0.0),
            make_classroom(chaos=0.0),
            expect_changed=False)
        if ok is not None:
            results.append(("场景变化检测（无变化对照）", ok))

        # ---- 多轮复演：让模型自己从多个角度推翻误判 ----
        print(SEP)
        print("v0.3 多轮复演测试（判断权在模型手里，不靠规则表）")
        ok = run_deliberation_test(
            client, cfg,
            frame=make_classroom_with_poster(gather=True),
            primary={"observation": "左墙附近有多名学生聚集",
                     "category": "gathering",
                     "category_label": "聚集围观",
                     "abnormal": True, "confidence": 0.8},
            memory_seed={"changes": "左墙新贴了一张分组表，学生陆续围观"},
            expect_abnormal=False,
            note="初判异常（聚集围观），但情境解读员知道墙上刚贴了新表")
        if ok is not None:
            results.append(("多轮复演·新贴分组表围观（应推翻为正常）", ok))
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
