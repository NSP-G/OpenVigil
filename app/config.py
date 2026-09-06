# -*- coding: utf-8 -*-
"""配置加载模块。
Copyright (c) 2026 CaspianFlow. 版权所有。
"""
import json
import os
import sys

DEFAULT_CONFIG = {
    "api_key": "",
    "api_base": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "model_daily": "glm-4.1v-thinking-flash",
    "model_recheck": "glm-4.1v-thinking-flash",
    "capture_interval_sec": 5,
    "diff_threshold": 0.004,
    "alert_confidence": 0.45,
    "min_alert_interval_sec": 20,
    "activity_trigger": 0.12,
    "heartbeat_frames": 20,
    "keep_frames": False,
    # 本地兜底守卫：模型判正常、但画面相对本场景基线显著异常时的兜底提示
    "local_guard_enabled": True,
    "local_guard_cooldown_sec": 300,
    "alert_image_dir": "alerts",
    "alert_history_file": "alert_history.json",
    "alert_history_max": 500,
    "theme": "auto",
    "log_dir": "logs",
    # 多帧定向确认：仅在判为聚集/打闹时追问"在聚还是在散"。
    # 不作为常规路径——多图会让思考型模型推理链暴涨导致输出截断。
    "multi_frame_confirm": True,
    # 多轮复演：初判异常时，让同一模型从多个角度独立评估再投票。
    # 判断权在模型手里，基础系统只安排它怎么看，不替它下结论。
    "deliberation_enabled": True,
    # 离座走动的持续帧数门槛：低于此值只记录不告警。
    # 交作业、扔垃圾都会造成短暂离座，一帧就告警会搅得课堂不得安宁。
    # 此前这项只存在于 Monitor 的 cfg.get 默认值里，没进 DEFAULT_CONFIG，
    # 导致写在 config.json 里也会被 load_config 的遍历逻辑忽略，改了没用。
    "leave_seat_sustain": 3,
    # 外部 Markdown 记忆：供模型读写的长期认知，会随观察不断进化。
    # max_chars 是单个文件的字符上限，超限交给模型自己压缩。
    "memory_root": "memory",
    "memory_max_chars": 4000,
    "prompt": "",
}

# 界面主题的合法取值
THEMES = ("auto", "light", "dark")
# 主题解析不成 auto/light/dark 时的回退值
DEFAULT_THEME = "auto"

# 需要数值类型的配置项及其合法范围（防止手改 config.json 写入脏值导致运行时崩溃）
_NUMERIC_FIELDS = {
    "capture_interval_sec": (0.5, 3600.0),
    "diff_threshold": (0.0, 1.0),
    "alert_confidence": (0.0, 1.0),
    "min_alert_interval_sec": (0.0, 86400.0),
    "activity_trigger": (0.0, 1.0),
    "heartbeat_frames": (1.0, 10000.0),
    "leave_seat_sustain": (1.0, 1000.0),
    "alert_history_max": (10.0, 100000.0),
    "local_guard_cooldown_sec": (0.0, 86400.0),
    # 记忆文件上限：太小会频繁压缩（丢细节），太大会挤爆上下文窗口
    "memory_max_chars": (500.0, 40000.0),
}

DEFAULT_PROMPT = (
    "你是学校值班老师的人工智能助手，正在巡检教室监控画面截图。\n"
    "请只输出一个 JSON，不要输出任何其他内容，格式如下：\n"
    '{"abnormal": true 或 false, "type": "异常类型", "detail": "客观描述", '
    '"confidence": 0到1之间的数字, "evidence": ["依据1", "依据2"]}\n'
    "\n"
    "【判定为异常（abnormal: true）的标准，满足任一即可】\n"
    "1. 多人离开座位、在教室内走动、站立或下位（自习/上课期间应安静就座）\n"
    "2. 出现打闹、追逐、推搡、打架等肢体冲突\n"
    "3. 三人以上异常聚集、围观、扎堆\n"
    "4. 大面积趴桌、倒地，或人员数量明显异常减少\n"
    "5. 教室空无一人或几乎无人\n"
    "\n"
    "【判定为正常（abnormal: false）的情形】\n"
    "学生基本都在座位上，只有轻微动作（写字、翻书、调整坐姿、个别抬头）\n"
    "\n"
    "【重要】\n"
    "- 监控画面常常不够清晰、角度较远，这是常态。只要能看出\"多人离座/走动/聚集\"的"
    "整体形态，就应当判为异常；不要因为看不清人脸、分不清具体是谁就判为正常。\n"
    "- 请基于画面中实际可见的内容判断，不要臆测看不到的细节，但也不要以\"看不清\"为由回避判断。\n"
    "- detail 请写你从画面里看到的具体情形，不超过 50 字。\n"
    "- confidence 是你对自己判断的把握程度。"
)


def _project_root():
    if getattr(sys, "frozen", False):
        # PyInstaller 冻结：exe 所在目录（config.json 放旁边可编辑）
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def writable_config_path():
    """用户可写的配置文件路径（冻结时在 exe 旁，源码时在项目根）。"""
    return os.path.join(_project_root(), "config.json")


def save_config(cfg, path=None):
    """保存配置到可写路径，返回写入的文件路径。"""
    path = path or writable_config_path()
    data = dict(cfg)
    if not data.get("prompt"):
        data.pop("prompt", None)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def ensure_config():
    """确保可写配置文件存在；不存在则用默认值创建。返回路径。"""
    path = writable_config_path()
    if not os.path.exists(path):
        try:
            save_config(dict(DEFAULT_CONFIG), path)
        except Exception:
            pass  # 只读目录（如 Program Files）下失败，load_config 回退默认
    return path


def _coerce_theme(value):
    """校验主题取值，非法值一律回退到跟随系统。

    手改 config.json 可能写入 "Dark"、"深色" 等非法值，
    若直接下发给前端，界面会永远停在回退主题上。
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in THEMES:
            return v
    return DEFAULT_THEME


# 开关型配置项（手改配置时可能被写成 "true"/"1"/"开" 等）。
# 此前这些新项走的是 else 分支直接赋值，任何脏值都会被原样接受。
_BOOL_FIELDS = (
    "keep_frames", "local_guard_enabled", "multi_frame_confirm",
    "deliberation_enabled",
)
# 路径型配置项：必须是非空字符串，否则拼接时会崩溃
_PATH_FIELDS = ("memory_root", "log_dir", "alert_image_dir",
                "alert_history_file")


def _coerce_bool(value):
    """把各种开关写法规范成 bool；无法识别时返回 None（调用方保留默认值）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y", "on", "开", "是"):
            return True
        if v in ("false", "0", "no", "n", "off", "关", "否"):
            return False
    return None


def _coerce_numeric(key, value):
    """数值配置项做类型转换与范围钳制；无法转换时返回 None（调用方保留默认值）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    low, high = _NUMERIC_FIELDS[key]
    return min(max(num, low), high)


def load_config(path=None):
    """加载配置文件；缺失或损坏时回退默认值，并返回 (config, errors)。"""
    if path is None:
        path = ensure_config()
    cfg = dict(DEFAULT_CONFIG)
    errors = []
    if not os.path.exists(path):
        errors.append("未找到 config.json，已使用默认配置。请在设置面板中填入 API Key。")
        _fill_prompt(cfg)
        return cfg, errors
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config.json 顶层必须是 JSON 对象")
    except Exception as e:
        errors.append(f"config.json 解析失败（{e}），已使用默认配置。")
        _fill_prompt(cfg)
        return cfg, errors
    for key in cfg:
        if key not in data or data[key] is None or data[key] == "":
            continue
        value = data[key]
        if key in _NUMERIC_FIELDS:
            coerced = _coerce_numeric(key, value)
            if coerced is None:
                errors.append(f"配置项 {key} 的值 {value!r} 不是合法数字，已回退默认值。")
                continue
            cfg[key] = coerced
        elif key == "theme":
            coerced = _coerce_theme(value)
            if coerced != value:
                errors.append(
                    f"配置项 theme 的值 {value!r} 非法（可选：{'/'.join(THEMES)}），已回退为 {DEFAULT_THEME}。"
                )
            cfg[key] = coerced
        elif key == "keep_frames":
            cfg[key] = bool(value) if isinstance(value, (bool, int)) else cfg[key]
        elif key == "local_guard_enabled":
            if isinstance(value, (bool, int)):
                cfg[key] = bool(value)
            elif isinstance(value, str):
                # 手改配置时可能写成 "true"/"false"/"0"/"1"
                v = value.strip().lower()
                if v in ("true", "1", "yes", "on"):
                    cfg[key] = True
                elif v in ("false", "0", "no", "off"):
                    cfg[key] = False
                else:
                    errors.append(
                        f"配置项 local_guard_enabled 的值 {value!r} 无法识别，已保持默认值。"
                    )
            else:
                errors.append(
                    f"配置项 local_guard_enabled 的值 {value!r} 非法，已保持默认值。"
                )
        elif key in _BOOL_FIELDS:
            parsed = _coerce_bool(value)
            if parsed is None:
                errors.append(
                    f"配置项 {key} 的值 {value!r} 不是合法开关值，已保持默认值。")
            else:
                cfg[key] = parsed
        elif key in _PATH_FIELDS:
            # 路径类必须是非空字符串。手改配置写成数字的话，
            # 后面 os.path.join(123, ...) 会直接抛 TypeError 崩溃。
            if isinstance(value, str) and value.strip():
                cfg[key] = value.strip()
            else:
                errors.append(
                    f"配置项 {key} 的值 {value!r} 不是合法路径，已保持默认值。")
        elif isinstance(cfg[key], str) and not isinstance(value, str):
            errors.append(f"配置项 {key} 应为文本，已忽略非法值。")
        else:
            cfg[key] = value
    if not cfg["api_key"]:
        errors.append("尚未填写智谱 API Key，请在设置面板中填入。")
    _fill_prompt(cfg)
    return cfg, errors


def _fill_prompt(cfg):
    if not cfg.get("prompt"):
        cfg["prompt"] = DEFAULT_PROMPT


def ensure_dirs(cfg):
    """确保日志目录存在。"""
    log_dir = cfg.get("log_dir") or "logs"
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(_project_root(), log_dir)
    os.makedirs(log_dir, exist_ok=True)
    return log_dir
