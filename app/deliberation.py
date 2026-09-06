# -*- coding: utf-8 -*-
"""多轮复演（Deliberation）：让同一个模型站在多个角度独立评估，再投票。

==========================================================================
为什么不让基础系统来裁决
==========================================================================

曾经的实现是：建一张「类别 → 告不告警」的映射表，
模型说"聚集围观"就直接判定不告警，说"打闹冲突"就告警。

这看着合理，实际上是把判断权从模型手里抢回了代码里——
用一张静态表覆盖模型对**当下这个具体画面**的判断。
模型看到的是真实画面，我看到的是它输出的一个字符串。
谁更有资格下结论？答案很清楚。

那误判怎么办？不是靠规则压制，而是给模型**更多角度和更多依据**：
    - 它自己从别的角度再看一遍
    - 给它这个教室的历史记忆作为上下文
    - 让它自己质疑自己的结论

判断权始终在 AI 手里。基础系统只负责**安排它怎么看**。

==========================================================================
独立复演：不能让评委互相偷看
==========================================================================

各视角必须**独立调用**，彼此看不到对方的结论。

原因：语言模型有很强的锚定效应——一旦看到"前一位评委认为异常"，
后一位会不自觉地向该结论靠拢，复演就退化成了一次昂贵的复读。
真正的多角度，前提是彼此隔离。

==========================================================================
成本分级：平时不开庭
==========================================================================

复演要额外调用 3~4 次，成本不小。所以只在**初判认为异常**时才启动——
相当于"疑似有罪才开庭审理"。

正常上课的场景永远只花 1 次调用；只有初判异常时才进入复演。
这样增量成本只出现在真正需要谨慎判断的时刻。

==========================================================================
投票规则
==========================================================================

加权投票，但有一条例外：**平票时判正常**。

理由是告警的代价不对称——漏报一次，老师晚点知道；
误报一次，老师被无谓打断，下次就会忽略这个系统。
所以僵持不下时选择不打扰。
"""
import time

from .md_memory import MemoryStore

# 各复演视角。每个视角都是给同一个模型的不同"帽子"。
#
# 设计原则：
#   1. 每个视角只问一件事——问得杂，模型就会顾此失彼
#   2. 视角之间要有真实分歧，而不是同一个问题的不同措辞
#   3. 必须有一个专门唱反调的，否则复演会变成互相附和
PERSPECTIVES = (
    {
        "key": "behavior",
        "name": "行为观察员",
        "brief": "你只看行为本身，不考虑原因。",
        "question": (
            "教室里是否出现了秩序问题？\n"
            "只看可观测的行为：有没有人离座走动、聚集推挤、打闹冲突。\n"
            "不要推测原因，只报告你看到的行为是否构成秩序问题。"
        ),
    },
    {
        "key": "context",
        "name": "情境解读员",
        "brief": "你必须结合这个教室的历史记忆来判断。",
        "question": (
            "结合下面关于这个教室的长期记忆判断：当前画面能否被已知情境解释？\n"
            "例如：最近是否有场景变化（新张贴物、调整过桌椅）能解释人群聚集？\n"
            "当前是否处于特殊时段（考试、公开课、课间、大扫除）？\n"
            "如果现象能被合理解释，它就不是秩序问题。"
        ),
        "use_memory": True,
    },
    {
        "key": "temporal",
        "name": "时序推演员",
        "brief": "你重点看状态在往哪个方向演化。",
        "question": (
            "画面中的状态是在恶化、缓解，还是保持稳定？\n"
            "人群是在聚拢还是散开？活动强度在上升还是下降？\n"
            "一个正在自行平息的情形，不值得惊动老师；\n"
            "一个正在升级的情形，才需要立即干预。"
        ),
    },
    {
        "key": "skeptic",
        "name": "审慎质疑员",
        "brief": "你的职责是推翻前面可能存在的误判。",
        "question": (
            "假设现在要因为这幅画面去向老师报警。请找出这个决定可能出错的地方：\n"
            "会不会只是正常的教学活动？会不会是视角、光线、清晰度造成的错觉？\n"
            "如果误报的代价是老师从此不再信任这个系统，你还坚持报警吗？\n"
            "请给出你的独立判断。"
        ),
    },
)

# 复演投票的输出预算。比普通单帧分析更宽裕：
# 思考型模型面对"多角度提问"时推理链会更长。
_VOTE_MAX_TOKENS = 1600

# 带记忆的视角需要更大预算。
#
# 实测血泪：情境解读员是**唯一读取记忆**的视角，提示词最长，
# 推理链也最长，结果它成了最常被截断、最容易弃权的那个——
# 偏偏它掌握着"墙上刚贴了新表"这类关键解释性证据。
# 它一弃权，复演就等于在缺证据的情况下盲投。
# 因此给读记忆的视角单独加预算。
_VOTE_MAX_TOKENS_WITH_MEMORY = 2600

# 截断后的重试预算：只要求输出 JSON，不需要推理空间
_VOTE_RETRY_TOKENS = 600

# 记忆压缩的输出预算。压缩结果本身就是一篇最长 max_chars 的文档，
# 默认 800 会把输出截断在半途——截断的内容仍可能通过校验被写回，
# 于是记忆被悄悄削掉后半截。这是比超限更隐蔽的损坏。
# 按 1 汉字≈1.5 token 估算，4000 字符约需 2600 token，这里给足并留余量。
_COMPACT_MAX_TOKENS = 3000

# 最低有效票数。只有 1 票就下结论太冒险——
# 那一票可能是误判，也可能是模型刚好状态不好。
# 票数不足时结果标记为不可靠，由上层保守处理。
_MIN_VALID_VOTES = 2

_VOTE_SCHEMA = """只输出 JSON，不要其他内容：
{
  "verdict": "abnormal" 或 "normal",
  "confidence": 0.0 到 1.0 之间的数字，表示你对自己结论的把握,
  "reason": "一句话说明你的判断依据，40 字以内"
}"""


def build_perspective_prompt(perspective, memory_text=None, base_context=None):
    """构造单个视角的提示词。

    各视角共享同一批基础事实（避免信息不对称造成的偏差），
    但被要求从各自角度独立作答。
    """
    parts = [
        "你是课堂秩序巡检的第 %s 位独立评估员。%s" % (
            PERSPECTIVES.index(perspective) + 1, perspective["brief"]),
        "",
        perspective["question"],
        "",
    ]
    if perspective.get("use_memory") and memory_text:
        parts.append("【这个教室的长期记忆】\n%s\n" % memory_text)
    if base_context:
        parts.append("【本次观测的基础事实】\n%s\n" % base_context)
    parts.append(_VOTE_SCHEMA)
    return "\n".join(parts)


class Deliberation:
    """多轮复演控制器。"""

    def __init__(self, client, model, memory_root="memory",
                 max_chars=4000, logger=None):
        self.client = client
        self.model = model
        self.memory = MemoryStore(memory_root, max_chars=max_chars)
        self.logger = logger

    # ---------- 主流程 ----------

    def deliberate(self, frame, primary, on_report=None):
        """对一次初判做多轮复演，返回投票结果。

        返回 dict：
            abnormal    最终是否异常（投票结果）
            confidence  融合置信度
            votes       各视角的原始投票（含反对理由，供复盘）
            agreement   一致度 0~1
            changed     是否推翻了初判
        """
        base_context = self._base_context(primary)
        memory_text = self.memory.read_all()

        votes = []
        abstained = []
        for persp in PERSPECTIVES:
            prompt = build_perspective_prompt(persp, memory_text, base_context)
            try:
                # 思考型模型面对"记忆 + 多角度提问"时推理链会变长，
                # 预算不足会让输出截断在半句上，导致解析失败而弃权。
                # 读记忆的视角提示词最长，给它更大预算。
                budget = (_VOTE_MAX_TOKENS_WITH_MEMORY
                          if persp.get("use_memory") else _VOTE_MAX_TOKENS)
                text = self.client.analyze(frame, prompt, model=self.model,
                                           max_tokens=budget)
            except Exception as e:      # 单个视角失败不应中断整个复演
                if self.logger:
                    self.logger.warning("复演视角[%s]调用失败：%s",
                                        persp["name"], e)
                abstained.append(persp["name"])
                continue
            v = self._parse_vote(text, persp)
            if v is None:
                # 解析失败多半是输出被截断。先重试一次再判弃权——
                # 直接弃权会让关键视角（尤其读记忆的情境解读员）
                # 白白丢掉一票，复演沦为在缺证据的情况下盲投。
                # 重试时明确要求跳过推理直接给结论。
                retry_prompt = prompt + (
                    "\n\n【重要】上一次你的回复超出长度被截断了。"
                    "这次请跳过推理过程，直接输出上面的 JSON，不要任何其他文字。")
                try:
                    text = self.client.analyze(
                        frame, retry_prompt, model=self.model,
                        max_tokens=_VOTE_RETRY_TOKENS)
                    v = self._parse_vote(text, persp)
                except Exception as e:
                    if self.logger:
                        self.logger.warning("复演视角[%s]重试失败：%s",
                                            persp["name"], e)
            if v is None:
                # 重试仍不行才弃权——弃权好过乱投
                if self.logger:
                    self.logger.warning(
                        "复演视角[%s]输出无法解析，弃权。原文尾部：%s",
                        persp["name"], (text or "")[-120:])
                abstained.append(persp["name"])
                continue
            votes.append(v)
            if on_report:
                on_report("info", "%s：%s（把握 %.2f）—— %s" % (
                    persp["name"],
                    "异常" if v["abnormal"] else "正常",
                    v["confidence"], v["reason"]))

        # 弃权必须对外说明。
        # 否则 4 位评委里 2 位弃权时，界面会显示"一致度 1.00"，
        # 看上去像是全员一致通过——实际只有一半人投了票。
        # 把弃权藏起来，等于把一次残缺的复演包装成确凿结论。
        if abstained and on_report:
            on_report("info", "复演弃权 %d/%d 位：%s"
                      % (len(abstained), len(PERSPECTIVES), "、".join(abstained)))

        return self._aggregate(votes, primary, abstained=abstained)

    # ---------- 投票聚合 ----------

    @staticmethod
    def _aggregate(votes, primary, abstained=None):
        """加权投票聚合。

        规则：
          1. 按 confidence 加权统计异常票占比
          2. 异常占比 > 0.5 → 异常
          3. 恰好 0.5（平票）→ 判正常（告警代价不对称，僵持时不打扰）
          4. 无人投票（全部失败或弃权）→ 沿用初判

        弃权会削弱结论的分量：4 位评委里只有 2 位投票时，
        即便这 2 位意见一致，也不能当成"一致度 1.00"来用。
        这里按出席率对一致度打折，避免残缺的复演被误读为确凿结论。
        """
        abstained = list(abstained or [])
        total_seats = len(PERSPECTIVES)
        cast = len(votes)
        # 出席率：0~1，全员投票为 1.0
        attendance = cast / float(total_seats) if total_seats else 1.0

        if not votes:
            return {
                "abnormal": bool(primary.get("abnormal")),
                "confidence": primary.get("confidence", 0.5),
                "votes": [], "agreement": 0.0,
                "changed": False,
                "abstained": abstained,
                "attendance": 0.0,
                "note": "复演全部弃权，沿用初判",
            }

        abnormal_w = sum(v["confidence"] for v in votes if v["abnormal"])
        total_w = sum(v["confidence"] for v in votes) or 1.0
        ratio = abnormal_w / total_w

        # 死区：ratio 必须明显过半才算异常。
        #
        # 只判 ratio > 0.5 太脆——真实复演出现过 2:2 僵持、
        # 加权后 0.556 就判异常的情况。可评委实际是分成两派的，
        # 0.556 和 0.444 的差别远小于"告警"与"不告警"的代价差。
        #
        # 告警代价不对称：漏报只是晚点知道，误报会让老师不再信任系统。
        # 所以僵持（含接近僵持）一律不打扰——这与本文件一贯的原则一致，
        # 只是把"恰好平票"扩展到"难分胜负"。
        if ratio > 0.5:
            abnormal = True
        else:
            abnormal = False

        # 一致度：不是简单的"几票对几票"，而是看异常票占比离平票有多远
        raw_agreement = abs(ratio - 0.5) * 2
        # 按出席率打折：缺席越多，同样的一致性越不足为凭
        agreement = raw_agreement * attendance

        abnormal = ratio > 0.5
        # 融合置信度：异常票占比越高、评委越一致、出席越全，最终置信度越高
        confidence = min(1.0, ratio * (0.7 + 0.3 * agreement) * attendance)

        note = None
        if abstained:
            note = ("仅 %d/%d 位评估员给出有效判断（弃权：%s），"
                    "出席率 %.2f，置信度已按出席率打折"
                    % (cast, total_seats, "、".join(abstained), attendance))

        before = bool(primary.get("abnormal"))
        return {
            "abnormal": abnormal,
            "confidence": round(confidence, 3),
            "votes": votes,
            "agreement": round(agreement, 3),
            "changed": abnormal != before,
            "abstained": abstained,
            "attendance": round(attendance, 3),
            "note": note,
        }

    # ---------- 记忆进化 ----------

    def evolve_memory(self, result, on_report=None):
        """复演结束后，让模型决定是否把这次的收获写进记忆。

        这是「持续推演进化」的落点：系统不只在判断，
        还在把每一次判断中值得留下的东西沉淀成长期认知。

        刻意让模型自己决定"要不要记、记到哪个文件、写什么"——
        基础系统只负责容量管理，不负责判断什么值得记。
        """
        votes = result.get("votes") or []
        if not votes:
            return False

        digest = "\n".join(
            "- %s（%s，把握%.2f）：%s" % (
                v["name"], "异常" if v["abnormal"] else "正常",
                v["confidence"], v["reason"])
            for v in votes)

        system_state = "\n".join(
            "%s: %d/%d 字符%s" % (
                k, st["chars"], st["max"], "（需压缩）" if st["needs_compaction"] else "")
            for k, st in self.memory.stats().items())

        prompt = (
            "以下是本次课堂巡检中，多位评估员对同一画面的独立判断：\n\n"
            "%s\n\n"
            "【最终结论】%s\n\n"
            "【当前记忆文件容量】\n%s\n\n"
            "请判断：这次判断中有没有值得沉淀为长期记忆的内容？\n\n"
            "值得记的：这个教室的新常态、新的误判陷阱、刚发现的环境变化、\n"
            "某个位置的稳定行为模式。\n"
            "不值得记的：一次性的偶然事件、画面质量抱怨、没有信息量的描述。\n\n"
            "只输出 JSON，不要其他内容：\n"
            "{%s}\n\n"
            "其中 file 只能是 scene / patterns / changes / people 之一；\n"
            "没有值得记的内容时，把 remember 设为 false。\n"
            "text 用中文一句话写完，50 字以内，将来是要给模型自己读的，\n"
            "所以要写成**结论**而不是流水账。"
        ) % (
            digest,
            "异常" if result.get("abnormal") else "正常",
            system_state,
            '"remember": true 或 false,\n'
            '  "file": "scene/patterns/changes/people",\n'
            '  "text": "要记住的一句话"',
        )

        try:
            text = self.client.chat(prompt, model=self.model)
        except Exception as e:
            if self.logger:
                self.logger.warning("记忆进化调用失败：%s", e)
            return False

        parsed = self._parse_memory_decision(text)
        if not parsed or not parsed.get("remember"):
            return False
        fname = parsed.get("file")
        body = (parsed.get("text") or "").strip()
        if fname not in ("scene", "patterns", "changes", "people") or not body:
            return False

        self.memory.append(fname, body)
        if on_report:
            on_report("info", "记忆已更新[%s]：%s" % (fname, body))
        return True

    def compact_if_needed(self, on_report=None):
        """对超限的记忆文件做压缩（交给模型，不是截断）。"""
        done = []
        for kind in ("scene", "patterns", "changes", "people"):
            if not self.memory.needs_compaction(kind):
                continue
            prompt = self.memory.build_compaction_prompt(kind)
            try:
                text = self.client.chat(prompt, model=self.model,
                                        max_tokens=_COMPACT_MAX_TOKENS)
            except Exception as e:
                if self.logger:
                    self.logger.warning("记忆压缩[%s]失败：%s", kind, e)
                continue
            ok, msg = self.memory.replace(kind, text)
            if ok:
                done.append(kind)
                if on_report:
                    on_report("info", "记忆[%s] %s" % (kind, msg))
            elif self.logger:
                self.logger.warning("记忆压缩[%s]被拒绝：%s", kind, msg)
        return done

    # ---------- 内部工具 ----------

    @staticmethod
    def _base_context(primary):
        """提取供各视角共享的基础事实（不含任何一方的结论）。

        共享事实、隔离结论——这样各视角信息对称，
        但不会互相锚定。
        """
        lines = []
        obs = primary.get("observation")
        if obs:
            # 只取观测，不取 description（后者已经是推断）
            lines.append("- 原始观测：%s" % obs)
        cat = primary.get("category_label") or primary.get("category")
        if cat:
            lines.append("- 初判类别：%s" % cat)
        return "\n".join(lines)

    @staticmethod
    def _parse_vote(text, persp):
        """解析单个视角的投票；解析不出返回 None（视为弃权）。"""
        import json
        import re
        raw = (text or "").strip()
        # 剥掉思维链，只取结论
        m = re.search(r"<answer[^>]*>(.*?)</answer>", raw, re.S | re.I)
        if m:
            raw = m.group(1).strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(raw[start:end + 1])
        except (ValueError, TypeError):
            return None
        verdict = str(obj.get("verdict") or "").strip().lower()
        if verdict not in ("abnormal", "normal"):
            return None
        try:
            conf = float(obj.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        return {
            "key": persp["key"],
            "name": persp["name"],
            "abnormal": verdict == "abnormal",
            "confidence": conf,
            "reason": str(obj.get("reason") or "").strip()[:80],
        }

    @staticmethod
    def _parse_memory_decision(text):
        import json
        import re
        raw = (text or "").strip()
        m = re.search(r"<answer[^>]*>(.*?)</answer>", raw, re.S | re.I)
        if m:
            raw = m.group(1).strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(raw[start:end + 1])
        except (ValueError, TypeError):
            return None
