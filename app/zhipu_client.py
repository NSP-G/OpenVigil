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


# ---- 输出长度预算 ----
# 思考型模型（glm-4.1v-thinking-flash 等）会先输出大段 <think> 推理
# 再输出 <answer> 结论。预算给小了，输出会在推理中途被切断，
# 结论永远出不来——实测 15 次调用中 7 次被截断（预算 800/900）。
# 结论区本身不长（一个 JSON，约 200~400 tokens），
# 真正的开销在推理过程，因此预算需要留足。
_MAX_TOKENS_SINGLE = 1600     # 单帧分析
_MAX_TOKENS_MULTI = 2500      # 多帧分析（要描述帧间变化，推理更长）
_MAX_TOKENS_DIFF = 1200       # 场景差异描述（任务简单，输出短）


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

    def _post(self, content_parts, model=None, temperature=0.1,
              max_tokens=_MAX_TOKENS_SINGLE):
        """发送请求体（content 片段由调用方组装，支持单图/多图混排）。"""
        model = model or self.model
        if not model:
            raise ValueError("未指定模型名")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content_parts}],
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

    # ---- 三种分析入口 ----
    def chat(self, prompt, model=None, temperature=0.1, max_tokens=800):
        """纯文本对话（不带图片）。

        用于记忆压缩、记忆进化这类"只和文字打交道"的任务——
        没必要为它们付出图片的 token 开销。
        """
        return self._post([{"type": "text", "text": prompt}],
                          model=model, temperature=temperature,
                          max_tokens=max_tokens)

    def analyze(self, image, prompt, model=None, temperature=0.1,
                max_tokens=_MAX_TOKENS_SINGLE):
        """对单张图片做视觉分析，返回模型文本回复。"""
        parts = [
            {"type": "image_url", "image_url": {"url": _encode_image(image)}},
            {"type": "text", "text": prompt},
        ]
        return self._post(parts, model=model, temperature=temperature,
                          max_tokens=max_tokens)

    def analyze_multi(self, frames, prompt, model=None, temperature=0.1,
                      max_tokens=_MAX_TOKENS_MULTI):
        """对多帧序列做时序分析。

        单帧里的「聚集」是静态名词，多帧里的「聚集」是有方向的动词——
        只有看到多帧才能判断人群是在往一起聚，还是正在散开。

        注意：面对多张图，思考型模型的推理链会显著变长，
        token 预算必须给足，否则输出会被截断在半句话上（实测踩过这个坑）。
        因此用 _MAX_TOKENS_MULTI，高于单帧的 _MAX_TOKENS_SINGLE。

        参数：
            frames —— PIL.Image 列表，按时间先后排列（建议 3 帧）
        """
        if not frames:
            raise ValueError("frames 不能为空")
        parts = []
        n = len(frames)
        for i, img in enumerate(frames):
            parts.append({
                "type": "image_url",
                "image_url": {"url": _encode_image(img, max_side=640)},
            })
            parts.append({
                "type": "text",
                "text": "[第 %d/%d 帧]" % (i + 1, n),
            })
        parts.append({"type": "text", "text": prompt})
        return self._post(parts, model=model, temperature=temperature,
                          max_tokens=max_tokens)

    def analyze_diff(self, reference, current, prompt, model=None,
                     temperature=0.1, max_tokens=_MAX_TOKENS_DIFF):
        """让模型描述两张图之间的差异，而非从零描述整个场景。

        别问「画面里有什么」，改成问「这两张之间发生了什么变化」——
        任务难度骤降，准确率大幅提升。
        用于识别「墙上新贴了一张分组表」这类场景持久性变化。
        """
        parts = [
            {"type": "image_url", "image_url": {"url": _encode_image(reference, max_side=768)}},
            {"type": "text", "text": "[图一：此前的常态画面]"},
            {"type": "image_url", "image_url": {"url": _encode_image(current, max_side=768)}},
            {"type": "text", "text": "[图二：当前画面]"},
            {"type": "text", "text": prompt},
        ]
        return self._post(parts, model=model, temperature=temperature,
                          max_tokens=max_tokens)


# ============================================================================
# 判定类别词表：用类别选择取代二值判定
# ============================================================================
# 「异常 / 正常」丢掉了太多信息，误判时无从分辨。
# 改成类别后，"看通知"和"打闹"就从同一个标签下的混淆，
# 变成了两个不同标签的区分——难度完全不同。

CATEGORY_NORMAL_CLASS = "normal_class"      # 正常上课
CATEGORY_ORDERLY = "orderly"                # 有序活动（课间、收发作业）
CATEGORY_GATHERING = "gathering"            # 聚集围观
CATEGORY_SCUFFLE = "scuffle"                # 打闹冲突
CATEGORY_LEAVE_SEAT = "leave_seat"          # 离座走动
CATEGORY_HEAD_DOWN = "head_down"            # 长时间低头
CATEGORY_ATTENTION_DROP = "attention_drop"  # 群体注意力下降
CATEGORY_EMPTY = "empty"                    # 空场 / 人员骤减
CATEGORY_OTHER = "other"                    # 其他

# 需要老师关注的类别（其余视为正常或仅需记录）
ALERT_CATEGORIES = (
    CATEGORY_SCUFFLE,
    CATEGORY_LEAVE_SEAT,
    CATEGORY_ATTENTION_DROP,
    CATEGORY_EMPTY,
)
# 需要记录但通常不告警的类别（聚集围观往往是响应性行为，非混乱）
WATCH_CATEGORIES = (
    CATEGORY_GATHERING,
    CATEGORY_HEAD_DOWN,
)

CATEGORY_LABELS = {
    CATEGORY_NORMAL_CLASS: "正常上课",
    CATEGORY_ORDERLY: "有序活动",
    CATEGORY_GATHERING: "聚集围观",
    CATEGORY_SCUFFLE: "打闹冲突",
    CATEGORY_LEAVE_SEAT: "离座走动",
    CATEGORY_HEAD_DOWN: "持续低头",
    CATEGORY_ATTENTION_DROP: "注意力涣散",
    CATEGORY_EMPTY: "空场缺勤",
    CATEGORY_OTHER: "其他",
}

_CATEGORY_ALIASES = {
    "正常上课": CATEGORY_NORMAL_CLASS, "上课": CATEGORY_NORMAL_CLASS,
    "正常": CATEGORY_NORMAL_CLASS, "normal": CATEGORY_NORMAL_CLASS,
    "有序活动": CATEGORY_ORDERLY, "有序": CATEGORY_ORDERLY,
    "聚集围观": CATEGORY_GATHERING, "聚集": CATEGORY_GATHERING,
    "围观": CATEGORY_GATHERING, "gathering": CATEGORY_GATHERING,
    "打闹冲突": CATEGORY_SCUFFLE, "打闹": CATEGORY_SCUFFLE,
    "冲突": CATEGORY_SCUFFLE, "scuffle": CATEGORY_SCUFFLE,
    "离座走动": CATEGORY_LEAVE_SEAT, "离座": CATEGORY_LEAVE_SEAT,
    "走动": CATEGORY_LEAVE_SEAT, "leave_seat": CATEGORY_LEAVE_SEAT,
    "持续低头": CATEGORY_HEAD_DOWN, "低头": CATEGORY_HEAD_DOWN,
    "head_down": CATEGORY_HEAD_DOWN,
    "注意力涣散": CATEGORY_ATTENTION_DROP, "涣散": CATEGORY_ATTENTION_DROP,
    "注意力下降": CATEGORY_ATTENTION_DROP, "attention_drop": CATEGORY_ATTENTION_DROP,
    "空场缺勤": CATEGORY_EMPTY, "空场": CATEGORY_EMPTY,
    "缺勤": CATEGORY_EMPTY, "empty": CATEGORY_EMPTY,
    "其他": CATEGORY_OTHER, "other": CATEGORY_OTHER,
}


def normalize_category(value, default=CATEGORY_OTHER):
    """把模型输出的类别规范化到标准词表（兼容中英文与别名）。"""
    if not value:
        return default
    key = str(value).strip().lower()
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    # 别名表里没有时做包含匹配（模型常写「疑似打闹」这类）
    for alias, std in _CATEGORY_ALIASES.items():
        if alias in key:
            return std
    return default


# 旧版提示词里的格式指令，构造新提示词时必须剔除。
# 否则模型会同时收到两套 JSON 格式说明（旧：abnormal/type/detail/evidence；
# 新：observation/category/description…），它会选择先出现的那套，
# 导致新字段全部丢失、类别退化为「其他」——这是实测出来的真问题。
_LEGACY_SCHEMA_MARKERS = (
    "请只输出一个 JSON",
    '"abnormal"',
    "格式如下",
)


def _strip_legacy_schema(base_prompt):
    """剔除基础提示词里的旧格式指令，只保留判定标准等业务知识。

    逐行过滤：命中旧格式特征的行整行丢弃。
    这样既能复用原有的领域规则（哪些情形算异常），
    又不会让模型面对两套互相冲突的输出格式。
    """
    if not base_prompt:
        return ""
    kept = []
    for line in base_prompt.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        # 旧 schema 的 JSON 示例行 / 格式声明行 → 丢弃
        if any(m in stripped for m in _LEGACY_SCHEMA_MARKERS):
            continue
        # 针对旧字段 detail 的填写说明 → 丢弃（新格式已无此字段）。
        # 基础提示词里这行以列表项形式出现（"- detail 请写…"），
        # 只判断 startswith("detail") 会漏掉它，导致提示词里残留对
        # 已删除字段的要求，模型输出就会多出一个没人解析的 detail。
        if "detail" in stripped and ("请写" in stripped or "不超过" in stripped):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def build_observe_prompt(base_prompt, is_recheck=False, scene_context=None,
                         recent_changes=None, multi_frame=False):
    """构造「先描述、再判断」的结构化提示词。

    这是降低误判的核心技巧：强制模型先说「我看到了什么」，
    再基于这个描述下结论。
    如果模型编不出合理的异常解释，它自己就会判正常——
    等于给模型装了一道自证关卡。

    返回的 JSON schema 刻意把 observation（纯观测）
    与 description（推断解读）分成两个字段，
    从源头落实「观察与推断分离」。
    """
    categories = " / ".join(
        "%s(%s)" % (CATEGORY_LABELS[c], c) for c in (
            CATEGORY_NORMAL_CLASS, CATEGORY_ORDERLY, CATEGORY_GATHERING,
            CATEGORY_SCUFFLE, CATEGORY_LEAVE_SEAT, CATEGORY_HEAD_DOWN,
            CATEGORY_ATTENTION_DROP, CATEGORY_EMPTY, CATEGORY_OTHER)
    )

    schema = """【输出格式】请只输出下面这一个 JSON，不要输出任何其他内容：
{
  "observation": "客观描述你在画面中实际看到的事实（人在哪里、在做什么、朝向哪里）。只写看得见的，不写猜测。",
  "category": "从下列类别中选一个：%s",
  "description": "基于上述观察给出你的解读（可含判断），面向查看监控的老师。",
  "confidence": 0.0 到 1.0 之间的数字，表示你对 category 判断的把握,
  "postures": [{"region": "位置编号如3-2", "posture": "head_up/head_down/standing/turning/lying", "facing": "front/desk/window/other"}],
  "facing_consistency": 0.0 到 1.0 之间的数字，表示在场人员朝向的一致程度（1=全部朝同一方向，0=各朝各的）,
  "abnormal": true 或 false
}""" % categories

    head = ("你是课堂秩序巡检助手。请观察画面，先客观描述所见，再判断类别。\n\n"
            + schema + "\n\n")

    # 多帧说明：明确区分「正常的小幅调整」与「真正的离座走动」。
    # 没有这段，模型会把帧间细微的位置变化当成学生离开座位——
    # 实测中「安静自习」就被这样误判成了离座走动并告警。
    if multi_frame:
        head += (
            "【你会看到多帧画面】请重点看人群的**变化趋势**：是在往一起聚，还是在散开。\n"
            "注意：学生写字、翻书、调整坐姿造成的小幅位置变化属于正常，"
            "不算离座走动；只有确实离开座位站立或走动才算。\n\n"
        )

    # 场景上下文：告诉模型这个场景平时长什么样，以及最近确认过哪些变化
    if scene_context:
        head += "【本场景的常态】%s\n\n" % scene_context
    if recent_changes:
        head += ("【近期已确认的场景变化】%s\n"
                 "注意：如果人群聚集是**响应这些变化**（例如在看新张贴的内容），"
                 "应判为聚集围观而非打闹冲突。\n\n" % recent_changes)

    # 基础提示词里原有的领域规则（哪些情形算异常）——去掉旧格式后接在最后
    criteria = _strip_legacy_schema(base_prompt)
    if criteria:
        head += "【判定参考】\n" + criteria + "\n\n"

    if is_recheck:
        head += ("【独立复核】请忽略任何已有的判断结论，把这当作一次全新的观察，"
                 "独立给出你自己的判断。你的结论允许与他人不同。")

    return head


def build_diff_prompt():
    """构造差异描述提示词：只描述两张图之间发生了什么变化。"""
    return """请对比图一（此前的常态画面）与图二（当前画面），
只描述**场景本身发生了哪些变化**（例如新增/移除了什么物品、张贴了什么、
桌椅布局是否改变、人员分布是否改变）。

要求：
1. 只描述确实存在的、持续性的变化，忽略临时经过的人影等瞬时差异。
2. 如果两张图在场景层面基本一致，请明确回答"无变化"。
3. 不要对变化做价值判断，只描述事实。

请按以下 JSON 输出：
{
  "changed": true或false,
  "changes": ["变化1", "变化2"],
  "confidence": 0.0到1.0之间的数字
}"""


def parse_observation(text):
    """解析「先描述再判断」格式的结构化输出。

    返回 dict，字段包括：
        observation  纯观测描述（进日志）
        category     类别（替二值判定）
        description  推断性描述（面向老师）
        confidence   置信度
        postures     各位置姿态（用于个体基线）
        facing_consistency  朝向一致度（区分围观与冲突的关键）
        abnormal     是否异常
        undetermined 输出不完整，无法判定（True 时不得据此告警）

    兼容三种退化情况：
      1. 模型只回旧格式（abnormal + detail）
      2. 模型完全没回 JSON（走文本启发式兜底）
      3. 输出被截断（见下）
    """
    raw = (text or "").strip()
    if not raw:
        # 空回复不是「正常」，而是「模型什么都没说」。
        # 必须与截断分支同样标记 unreliable，否则上层会把沉默当成
        # "没发现问题"，在模型故障时静默放行整个课堂。
        return {"abnormal": False, "observation": "", "description": "",
                "category": CATEGORY_OTHER,
                "category_label": CATEGORY_LABELS[CATEGORY_OTHER],
                "confidence": 0.0, "postures": [],
                "facing_consistency": None,
                "unreliable": True, "reason": "empty"}

    answer = _extract_answer(raw)
    if not answer:
        answer = raw

    info = None
    for obj in _extract_json_objects(answer):
        if "abnormal" in obj or "category" in obj or "observation" in obj:
            info = dict(obj)
            break

    # 兼容判定：模型完全可能不按新格式回（尤其是换了模型或提示词被截断时）。
    # 此时必须回落到旧字段名（detail / type），否则 observation 与
    # description 会双双变成空串，导致告警正文为空——日志里只会留下一句
    # 「告警触发[其他] confidence=0.84：」而没有任何可读内容。
    if info is not None:
        has_new_fields = any(
            info.get(k) for k in ("observation", "description", "category"))
        if not has_new_fields:
            has_legacy_fields = any(info.get(k) for k in ("detail", "type"))
            if has_legacy_fields:
                info = None

    if info is None:
        # 拿不到结构化结果时，先判断是不是被截断了。
        # 截断意味着模型话没说完，此时任何"保守判异常"的兜底都是危险的——
        # 它会在安静的教室里凭空制造告警。这种情况必须让上层跳过该帧重来。
        if _looks_truncated(answer) or _looks_truncated(raw):
            return {
                "abnormal": False,
                "observation": "",
                "description": "",
                "category": None,
                "category_label": None,
                "confidence": 0.0,
                "postures": [],
                "facing_consistency": None,
                "unreliable": True,
                "reason": "truncated",
            }
        # 退化：走原有的文本启发式解析
        abnormal, legacy = parse_verdict(raw)
        return {
            "abnormal": abnormal,
            "observation": legacy.get("detail", ""),
            "description": legacy.get("detail", ""),
            "category": CATEGORY_OTHER,
            "category_label": legacy.get("type") or CATEGORY_LABELS[CATEGORY_OTHER],
            "confidence": legacy.get("confidence", 0.5),
            "postures": [],
            "facing_consistency": None,
            "legacy": True,
        }

    category = normalize_category(info.get("category"))
    observation = _clean_field(info.get("observation"))
    description = _clean_field(info.get("description"))

    # abnormal 字段优先信模型；缺失时由类别推导
    if "abnormal" in info:
        abnormal = _coerce_bool(info.get("abnormal"), False)
    else:
        abnormal = category in ALERT_CATEGORIES

    # 关键修正：类别判为「正常上课/有序」时，不允许 abnormal 为真。
    # 模型偶尔会在 observation 里写了正常内容却又因措辞被打上异常，
    # 这里以类别为准做一次收束，避免自相矛盾的输出。
    if category in (CATEGORY_NORMAL_CLASS, CATEGORY_ORDERLY):
        abnormal = False

    conf = _coerce_confidence(info.get("confidence"),
                              0.65 if abnormal else 0.5)

    # 朝向一致度：区分「聚集围观」与「打闹冲突」的最强特征。
    # 所有人朝向一致（都朝墙、朝黑板）→ 注意力集中，是好事；
    # 朝向混乱、有肢体接触、位置快速互换 → 才是混乱。
    facing = info.get("facing_consistency")
    facing = _coerce_confidence(facing, None) if facing is not None else None

    postures = info.get("postures")
    if not isinstance(postures, list):
        postures = []

    return {
        "abnormal": abnormal,
        "observation": observation,
        "description": description or observation,
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, category),
        "confidence": conf,
        "postures": postures,
        "facing_consistency": facing,
        "unreliable": False,
    }


def _looks_truncated(text):
    """判断模型回复是否因 token 上限被硬截断。

    思考型模型面对多帧输入时推理链会很长，极易撞上 max_tokens：
    输出停在句子中间，既没有闭合的 JSON，也没有闭合的 <answer> 标签。

    这属于「模型没说完」，是调用层面的失败，而不是模型的判断。
    绝不能落到「看不出结论就保守判异常」那条兜底路径上——
    那会让一间安静的教室凭空产生告警，是最糟糕的失败类型。
    """
    t = str(text or "").strip()
    if not t:
        return False
    # 有开标签却没有对应的闭标签
    if _THINK_OPEN_RE.search(t) and not _THINK_CLOSE_RE.search(t):
        return True
    if re.search(r"<answer\s*>", t, re.I) and not re.search(r"</answer\s*>", t, re.I):
        return True
    # 存在未闭合的 JSON 括号（去掉尾部空白后仍未收束）
    if t.count("{") > t.count("}"):
        return True
    return False


def _clean_field(value):
    """清洗字段值：剥掉模型误写进字段内部的思维链。

    实测中模型会把整段 <think>…</think> 原样写进 observation 的值里，
    导致日志中"纯观测事实"一栏塞满推理过程——
    既违反观察与推断分离的原则，也让存储体积白白膨胀。
    """
    s = str(value or "").strip()
    s = _THINK_BLOCK_RE.sub("", s)
    s = _THINK_OPEN_RE.sub("", s)
    s = _THINK_CLOSE_RE.sub("", s)
    return s.strip()


def parse_diff(text):
    """解析差异描述的输出，返回 (changed: bool, changes: list, confidence)。"""
    raw = (text or "").strip()
    if not raw:
        return False, [], 0.0
    answer = _extract_answer(raw) or raw
    for obj in _extract_json_objects(answer):
        if "changed" in obj or "changes" in obj:
            changed = _coerce_bool(obj.get("changed"), False)
            changes = obj.get("changes")
            if not isinstance(changes, list):
                changes = [str(changes)] if changes else []
            return changed, [str(c) for c in changes], _coerce_confidence(
                obj.get("confidence"), 0.6)
    # 无 JSON 时按关键词兜底
    if "无变化" in answer or "没有变化" in answer:
        return False, [], 0.6
    return ("变化" in answer or "新增" in answer or "张贴" in answer), \
        [answer[:200]], 0.4


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
    "离座", "离开座位", "离开自己", "擅自离", "擅自离开",
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
