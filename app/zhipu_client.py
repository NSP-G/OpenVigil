# -*- coding: utf-8 -*-
"""智谱视觉模型 API 客户端（requests 直连官方端点）。
Copyright (c) 2026 CaspianFlow. 版权所有。

图片以 base64 内联在请求体中发送，不经过任何云存储/中转服务，
符合“除 API 之外均不上云”的约束。失败时抛出带明确信息的异常。
"""
import base64
import io
import json
import re
import time

import requests
from PIL import Image


class ZhipuError(Exception):
    """智谱 API 调用错误，携带错误码与可读信息。"""
    def __init__(self, message, status_code=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _encode_image(image, max_side=1280, quality=85):
    """把 PIL.Image 转成 data URL 形式的内联图片。
    先等比缩放到 max_side 以内，再按 quality 压缩为 JPEG，控制上传体积。
    """
    img = image.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _coerce_bool(value, default=False):
    """把模型可能返回的各种布尔表示规范化为 bool。
    接受 True/False、1/0、"true"/"false"（大小写不敏感）、"是"/"否"。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y", "是", "有"):
            return True
        if v in ("false", "0", "no", "n", "否", "无", ""):
            return False
    return default


def _coerce_confidence(value, default=0.5):
    """把模型返回的置信度规范化到 [0,1] 区间。
    兼容字符串数字、百分制（>1 时按 0-100 换算）；无法解析时返回默认值。
    """
    if value is None:
        return default
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return default
    if conf > 1.0:  # 模型按百分制返回（如 80）
        conf = conf / 100.0
    if conf < 0.0:
        return 0.0
    if conf > 1.0:
        return 1.0
    return conf


class ZhipuVisionClient:
    """智谱视觉模型客户端。
    参数：
        api_key: 智谱开放平台 API Key
        api_base: 请求端点（默认官方 OpenAI 兼容端点）
        model: 默认模型名
        timeout: 请求超时秒数
    """
    def __init__(self, api_key, api_base=None, model=None, timeout=60):
        if not api_key:
            raise ValueError("api_key 不能为空")
        self.api_key = api_key
        self.api_base = api_base or "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        self.model = model
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def close(self):
        """关闭底层连接池（停止监控时调用，避免连接泄漏）。"""
        try:
            self.session.close()
        except Exception:
            pass

    def analyze(self, image, prompt, model=None, temperature=0.1, max_tokens=800):
        """对单张图片做视觉分析，返回模型文本回复。
        参数：
            image: PIL.Image
            prompt: 给模型的文字指令
            model: 模型名，缺省用构造时指定的默认模型
        """
        model = model or self.model
        if not model:
            raise ValueError("未指定模型名")
        data_url = _encode_image(image)
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # 指数退避重试：针对 429 限流和 5xx 临时错误，最多重试 3 次
        max_retries = 3
        resp = None
        for attempt in range(max_retries):
            try:
                resp = self.session.post(self.api_base, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise ZhipuError(f"网络请求失败：{e}") from e
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
            break
        if resp is None:
            raise ZhipuError("网络请求失败：重试后仍无响应")
        if resp.status_code != 200:
            raise ZhipuError(
                self._parse_error(resp),
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise ZhipuError(f"响应解析失败：{resp.text[:500]}") from e

    @staticmethod
    def _parse_error(resp):
        """尽量从响应体里提取智谱返回的错误信息。"""
        try:
            body = resp.json()
            err = body.get("error") or {}
            code = err.get("code")
            msg = err.get("message") or body.get("msg") or resp.text[:300]
            if code:
                return f"智谱API错误[{code}]：{msg}"
            return f"智谱API错误：{msg}"
        except Exception:
            return f"HTTP {resp.status_code}：{resp.text[:300]}"


# ---------- 文本启发式判定词表 ----------
# 明确的“正常/无异常”收束表述（优先级高于行为词，用于拦截“有个别走动但整体正常”）
_NEG_PHRASES = [
    "没有异常", "无异常", "未发现异常", "未检测到异常", "不存在异常",
    "没什么异常", "一切正常", "画面正常", "均正常", "都正常", "属于正常",
    "整体正常", "基本正常", "大体正常", "秩序正常", "状态正常", "情况正常",
    "无需处理", "不需要处理", "没有明显异常",
]
# 异常宣告词（出现即倾向判异常）
_ABNORMAL_PHRASES = [
    "有异常", "发现异常", "检测到异常", "存在异常", "出现异常",
    "有情况", "异常情况", "明显异常",
]
# 具体可见的行为描述词：即使模型没说“异常”二字，只要描述了这些形态也应判异常。
# 这是修复“班级很乱却不报告”的关键——模型常把混乱描述成行为细节而不下结论。
_BEHAVIOR_PHRASES = [
    "打闹", "打架", "追逐", "推搡", "推打", "扭打", "肢体冲突",
    "离座", "离开座位", "离开座位", "离开自己", "擅自离", "擅自离开",
    "聚集", "围观", "围聚", "扎堆", "围在一起",
    "多人走动", "多名学生走动", "学生在走动", "在教室走动", "走来走去",
    "站立", "站起来", "离开座位走动", "下位", "下座位",
    "趴桌", "趴在桌", "大面积趴", "倒地",
    "乱", "混乱", "吵闹", "喧哗", "嘈杂", "骚动", "哄闹",
    "空无一人", "空教室", "人员骤减", "大量缺", "缺勤",
]
# 异常词的否定修饰。注意：这里刻意不放单字“不”，
# 否则“不少学生离开座位”“不但没人管”会被误判成否定语境而漏报。
_NEGATIONS = (
    "没有", "没人", "没发现", "没检测到", "没看到", "未发现", "未检测到", "未见", "未",
    "无", "并非", "并不", "不是", "不存在", "不算", "不构成", "不属于", "不至于",
)
# 子句分隔符：否定判定只在同一个子句内生效，
# 避免出现“没有学生打闹，但后排有人聚集”被整体误判为否定。
_CLAUSE_SEPS = "，。；！？、,.;!?\n:：（）()"
# “不正常”类表述：含“正常”二字但实际表示异常，必须最先判断
_NOT_NORMAL_PHRASES = ["不正常", "不太正常", "很不正常", "有些不正常"]
# 弱信号
_WEAK_PHRASES = ("需要注意", "值得注意", "需要关注", "建议查看", "建议人工")
# 弱信号专用否定词：短语结构固定、否定词紧邻，
# 这里可以安全使用单字“不”（“不需要注意”这类表述很常见），
# 不会像通用行为词那样被“不少学生”误伤。
_WEAK_NEGATIONS = ("不", "无需", "不需要", "不必", "没", "未", "无", "不存在")
# 宽泛全局否定
_GLOBAL_NEG = ["未发现", "没有发现", "未见", "未检测", "没检测", "不需要", "无需", "不必"]


def _extract_json_objects(text):
    """从模型回复中提取所有可解析的 JSON 对象（按括号平衡扫描）。

    支持以下真实回复形态：
      - 纯 JSON
      - 前后带说明文字
      - ```json 代码块包裹
      - 多个 JSON 片段（逐个尝试，交给调用方挑选）
    """
    candidates = []
    # 优先处理 ```json / ``` 代码块
    for block in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S):
        candidates.append(block.strip())
    # 括号平衡扫描：从每个 { 出发找完整闭合
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    candidates.append(text[start:i + 1])
                    start = -1
    # 长片段优先（通常是主结论），但代码块已排在前面
    objs = []
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def _negated(text, phrase, negations=None):
    """判断 phrase 在 text 中的每次出现是否都被否定修饰。

    否定只在「同一个子句」内生效，且不再要求否定词紧邻行为词——
    像“没有学生打闹”中间隔着“学生”，按固定长度前缀判断会漏掉否定，
    必须回溯到最近的子句边界再检查。
    """
    negations = negations or _NEGATIONS
    idx = text.find(phrase)
    if idx == -1:
        return None  # 未出现
    while idx != -1:
        # 回溯到最近的子句分隔符，得到本次出现所在的子句起点
        start = 0
        for pos in range(idx - 1, -1, -1):
            if text[pos] in _CLAUSE_SEPS:
                start = pos + 1
                break
        clause_prefix = text[start:idx]
        if not any(neg in clause_prefix for neg in negations):
            return False  # 至少有一次未被否定
        idx = text.find(phrase, idx + 1)
    return True  # 全部出现都被否定


# 思维链标签：思考型模型会把推理过程包在这些标签里，判定前必须剥掉
_THINK_TAGS = r"(?:think|thinking|reasoning|thought|reflection)"
_THINK_BLOCK_RE = re.compile(rf"<{_THINK_TAGS}\s*>.*?</{_THINK_TAGS}\s*>", re.S | re.I)
_THINK_OPEN_RE = re.compile(rf"<{_THINK_TAGS}\s*>", re.I)
_THINK_CLOSE_RE = re.compile(rf"</{_THINK_TAGS}\s*>", re.I)
_ANSWER_RE = re.compile(r"<answer\s*>(.*?)</answer\s*>", re.S | re.I)


def _extract_answer(text):
    """剥掉模型的思维链，只保留最终结论。

    思考型模型（glm-4.1v-thinking-flash 等）的回复形如：
        <think>……判断过程中会枚举"打闹/聚集/离岗"等词……</think><answer>正常</answer>
    若直接对整段文本做关键词匹配，思维链里枚举的异常词会被当成结论，
    导致"模型说正常、程序判异常"的误报。因此必须先取出真正的回答。
    """
    # 1) 优先取 <answer> 标签内的内容（这才是模型的正式结论）
    m = _ANSWER_RE.search(text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    # 2) 没有 answer 标签时，删掉成对的思维链块，用剩余部分判定
    stripped = _THINK_BLOCK_RE.sub("", text)
    # 3) 思维链未闭合（输出被截断）：只保留它之前的内容
    if _THINK_OPEN_RE.search(stripped) and not _THINK_CLOSE_RE.search(stripped):
        stripped = stripped[:_THINK_OPEN_RE.search(stripped).start()]
    stripped = stripped.strip()
    return stripped or text.strip()


# 模型「看不清画面」的典型措辞。这类回复意味着画面质量问题
# （模糊、过暗、遮挡、分辨率过低），而不是「画面很乱」，
# 应与异常判定区分开，避免伪装成告警。
_UNCLEAR_PHRASES = (
    "看不清", "看不清楚", "无法判断", "无法识别", "无法确认", "无法确定",
    "难以判断", "难以识别", "不清晰", "画面不清", "模糊", "太暗", "太黑",
    "分辨率", "画质", "无法分辨", "看不出",
)


def is_unclear_reply(text):
    """判断模型回复是否表示「看不清画面」。

    仅用于模型判正常时的二次确认：模型判异常说明它确实看到了东西，
    不存在看不清的问题。判定前请务必先剥掉思维链，
    否则推理过程中枚举的"无法判断"会被误当成结论。
    """
    t = str(text or "")
    if not t.strip():
        return False
    return any(p in t for p in _UNCLEAR_PHRASES)


def parse_verdict(text):
    """解析模型回复，提取结构化判定。
    返回 (abnormal: bool, info: dict)。

    解析顺序（针对真实模型回复形态设计）：
      1. 提取 JSON（支持代码块、多片段、前后带说明文字）
      2. 无 JSON 时走文本启发式：「不正常」→ 明确正常收束 → 行为描述 → 异常宣告 → 弱信号
    """
    text = (text or "").strip()
    if not text:
        return False, {}
    # 剥掉思维链，只保留模型的正式结论（详见 _extract_answer 说明）
    text = _extract_answer(text)
    if not text:
        return False, {}

    # ---- 1. JSON 优先 ----
    for obj in _extract_json_objects(text):
        if "abnormal" not in obj:
            continue
        abnormal = _coerce_bool(obj.get("abnormal", False))
        obj["abnormal"] = abnormal
        obj["confidence"] = _coerce_confidence(
            obj.get("confidence"), 0.65 if abnormal else 0.5
        )
        return abnormal, obj

    # ---- 2. 文本启发式 ----
    # 2.1 “不正常/不太正常”必须最先判断，否则会被后面的“正常”子串吃掉
    for phrase in _NOT_NORMAL_PHRASES:
        if phrase in text:
            return True, {"detail": text, "confidence": 0.65}

    # 2.2 明确的正常收束语（如“整体正常”），避免被个别行为词误伤
    if any(p in text for p in _NEG_PHRASES):
        return False, {"detail": text, "confidence": 0.7}

    # 2.3 具体行为描述：模型描述了混乱形态却没下“异常”结论时要能抓住
    behavior_all_negated = False
    for phrase in _BEHAVIOR_PHRASES:
        neg = _negated(text, phrase)
        if neg is False:
            return True, {"detail": text, "confidence": 0.6, "behavior_hit": phrase}
        if neg is True:
            behavior_all_negated = True

    # 2.4 异常宣告词（排除否定修饰）
    abnormal_all_negated = False
    for phrase in _ABNORMAL_PHRASES:
        neg = _negated(text, phrase)
        if neg is False:
            return True, {"detail": text, "confidence": 0.65}
        if neg is True:
            abnormal_all_negated = True

    # 2.4.1 行为词/异常词都出现过，但全都处在否定语境中
    # （如“没有学生打闹”），说明模型在明确否定异常，不能落到兜底判异常
    if behavior_all_negated or abnormal_all_negated:
        return False, {"detail": text, "confidence": 0.7}

    # 2.5 弱信号
    for phrase in _WEAK_PHRASES:
        if _negated(text, phrase, _WEAK_NEGATIONS) is False:
            return True, {"detail": text, "confidence": 0.6}

    # 2.6 宽泛否定 → 正常
    if any(p in text for p in _GLOBAL_NEG):
        return False, {"detail": text, "confidence": 0.6}
    if "正常" in text:
        return False, {"detail": text, "confidence": 0.6}
    # 无法判断时保守按异常（低置信度，交由上层时序/活动度决定）
    return True, {"detail": text, "confidence": 0.4}
