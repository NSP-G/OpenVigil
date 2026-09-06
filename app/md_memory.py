# -*- coding: utf-8 -*-
"""外部 Markdown 记忆系统。

==========================================================================
为什么是 Markdown 而不是 YAML / JSON
==========================================================================

记忆的最终读者**是模型自己**。

模型读 Markdown 的能力远好于读结构化数据——训练语料里满是 Markdown，
它对标题、列表、表格的理解是原生的。而 YAML/JSON 的缩进、引号、
嵌套对模型来说是额外的解析负担，还容易在生成时写坏格式。

更关键的一点：模型要**写**记忆。让它输出一段 Markdown 续写，
比让它在严格的 schema 里插入字段要可靠得多。写坏一个 JSON 会让整个
文件解析失败；写坏一行 Markdown，最坏只是那句话不通顺。

==========================================================================
持续推演进化：记忆不是流水账，是会变的知识
==========================================================================

日志（memory.ObservationLog）记的是"发生了什么"，只增不改。
这里记的是"由此我们知道什么"——**会变、会被推翻、会被合并**。

分歧处理原则：新观察与旧结论冲突时，不直接覆盖，而是：
    旧结论降权 → 记录冲突 → 冲突积累到一定次数才改写
一次反例不足以推翻长期规律，但足以让人多看一眼。

==========================================================================
上下文预算：这是硬约束
==========================================================================

每个文件都有字符上限。超限就压缩——不是简单截断（那样会丢掉最早、
也往往是最基础的认知），而是让模型自己把旧条目合并成更凝练的摘要。

截断 = 失忆；压缩 = 提炼。系统要的是后者。

==========================================================================
零标注基线
==========================================================================

所有记忆文件都可以是空的。空记忆 = 系统不具备先验，全部判断依赖
当前画面 + 模型常识。这不影响系统运行，只是没有"这个教室特有的认知"。

老师随时可以手写 Markdown 补充（比如把座位表写进 people.md），
但不是必须——这是「零标注基线 + 增量增强」原则在记忆层的体现。
"""
import os
import re
import time


# 每个记忆文件的字符上限。
# 粗略折算：1 个汉字 ≈ 1.5 token，4000 字符 ≈ 2500 token。
# 全部记忆同时注入提示词时占用约 1 万 token，对于视觉模型来说
# 是笔不小的开销，但换来的场景理解能力值得——前提是**有上限**。
DEFAULT_MAX_CHARS = 4000

# 记忆种类：文件名 → 用途说明（同时作为给模型看的目录）
MEMORY_KINDS = {
    "scene": "场景常态：教室里哪里有什么、平时是什么样子、光线和视角特点",
    "patterns": "经验模式：什么样的情形容易误判、什么样的信号才可靠",
    "changes": "环境变化史：新出现了什么、消失了什么、什么时候变的",
    "people": "人员档案：各位置的长期表现（可为空，靠位置编号即可运行）",
}


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def _today():
    return time.strftime("%Y-%m-%d")


class MemoryStore:
    """Markdown 记忆仓库。

    每个 kind 对应一个 .md 文件。核心操作：
        read(kind)              读取（供注入提示词）
        append(kind, text)      追加一条记忆（模型自己写的）
        compact(kind)          超限压缩（交给模型做，保证不丢基础认知）
        needs_compaction(kind)  是否需要压缩
    """

    def __init__(self, root="memory", max_chars=DEFAULT_MAX_CHARS):
        self.root = root
        self.max_chars = max_chars
        os.makedirs(root, exist_ok=True)

    # ---------- 路径 ----------

    def path_of(self, kind):
        return os.path.join(self.root, "%s.md" % kind)

    # ---------- 读 ----------

    def read(self, kind):
        """读取记忆内容；文件不存在返回空串（零标注基线：空记忆是合法状态）。"""
        p = self.path_of(kind)
        if not os.path.exists(p):
            return ""
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def read_all(self):
        """读取全部记忆，拼成供模型阅读的片段。

        只在有内容时才输出对应小节——空文件不占位，
        避免提示词里塞满"暂无记录"这种噪音。
        """
        parts = []
        for kind, desc in MEMORY_KINDS.items():
            content = self.read(kind)
            if content:
                parts.append("## %s（%s）\n%s" % (kind, desc, content))
        return "\n\n".join(parts)

    # ---------- 写 ----------

    def append(self, kind, text, date=None):
        """追加一条记忆，按日期归组。

        同一天的条目归在同一标题下，避免每次都新建一个日期标题——
        记忆文件要的是密度，不是流水账。
        """
        text = (text or "").strip()
        if not text:
            return False
        p = self.path_of(kind)
        day = date or _today()
        marker = "### %s" % day

        existing = ""
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                existing = f.read()

        # 必须用「行首精确匹配」判断分组是否存在。
        # 用 `marker in existing` 做子串匹配会误判：模型写的某条记忆里
        # 若恰好含 "### 2026-09-06" 这样的文本，就会被当成已有分组，
        # 新条目被插到错误位置，文件结构逐渐错乱。
        has_group = any(
            ln.strip().startswith(marker) for ln in existing.splitlines())
        if has_group:
            # 已有今天的分组 → 在其末尾追加
            lines = existing.rstrip().splitlines()
            out, in_group = [], False
            for ln in lines:
                if ln.startswith(marker):
                    in_group = True
                    out.append(ln)
                    continue
                if in_group and ln.startswith("### "):
                    out.append("- %s" % text)
                    in_group = False
                out.append(ln)
            if in_group:
                out.append("- %s" % text)
            content = "\n".join(out) + "\n"
        else:
            header = "# %s\n\n" % MEMORY_KINDS.get(kind, kind) if not existing else ""
            content = existing.rstrip() + ("\n\n" if existing else "")
            content += header + "%s\n- %s\n" % (marker, text)

        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return True

    # ---------- 容量管理 ----------

    def size_of(self, kind):
        return len(self.read(kind))

    def needs_compaction(self, kind):
        """是否超过容量上限。"""
        return self.size_of(kind) > self.max_chars

    def overload_ratio(self, kind):
        """超限倍数，供日志观察增长速度。"""
        size = self.size_of(kind)
        return round(size / float(self.max_chars), 2) if self.max_chars else 0.0

    # ---------- 压缩 ----------

    def build_compaction_prompt(self, kind):
        """生成压缩提示词：让模型自己提炼，而不是机械截断。

        这里刻意要求保留「最早形成的认知」——那些是理解这个场景的
        地基，被截掉就等于失忆。
        """
        return (
            "以下是关于「%s」的长期记忆，已经超出容量上限，需要压缩。\n\n"
            "请把它重写为更凝练的版本，要求：\n"
            "1. 保留所有**仍然成立**的结论，尤其是最早形成的、关于这个场景的基础认知\n"
            "2. 合并重复或相近的条目\n"
            "3. 已经被后续事实推翻的旧结论直接删除，不要保留\n"
            "4. 不确定的判断标注「(待验证)」\n"
            "5. 控制在 %d 字符以内（这是硬上限，超出会被拒绝）\n"
            "6. 只输出压缩后的 Markdown 正文，不要任何解释、不要代码块\n\n"
            "----\n%s\n----\n"
        ) % (MEMORY_KINDS.get(kind, kind), self.max_chars, self.read(kind))

    def replace(self, kind, content):
        """整体替换（用于压缩后的写回）。

        压缩可能失败（模型返回空、返回解释文字、返回代码块），
        因此做基本校验：明显不合格就拒绝写入，宁可让它继续超限，
        也不能用一段废话覆盖掉多年积累的认知。
        """
        content = (content or "").strip()
        if not content:
            return False, "压缩结果为空，拒绝写入"
        # 模型有时会把整段包在代码块里
        if content.startswith("```") and content.endswith("```"):
            content = content.strip("`").strip()
            content = re.sub(r"^\w*\n", "", content).strip()
        if len(content) < 20:
            return False, "压缩结果过短，疑似无效，拒绝写入"
        # 上限就是 max_chars，不能放宽到 1.5 倍。
        # 放宽意味着「压缩完仍然超限」会被接受，下一轮又触发压缩，
        # 于是每次复演都白白多调一次 API，文件却始终超限——
        # 一个自我维持的死循环。压缩不达标就拒绝，宁可让它继续超限。
        if len(content) > self.max_chars:
            return False, ("压缩结果 %d 字符仍超过上限 %d，拒绝写入"
                           % (len(content), self.max_chars))

        with open(self.path_of(kind), "w", encoding="utf-8") as f:
            f.write(content + "\n")
        return True, "已压缩至 %d 字符" % len(content)

    # ---------- 维护 ----------

    def stats(self):
        """各文件的容量状态，供界面展示与自检。"""
        out = {}
        for kind in MEMORY_KINDS:
            size = self.size_of(kind)
            out[kind] = {
                "chars": size,
                "max": self.max_chars,
                "ratio": round(size / float(self.max_chars), 2) if self.max_chars else 0,
                "needs_compaction": size > self.max_chars,
            }
        return out

    def clear(self, kind=None):
        """清空记忆（毕业/换教室时用）。"""
        if kind:
            p = self.path_of(kind)
            if os.path.exists(p):
                os.remove(p)
        else:
            for k in MEMORY_KINDS:
                p = self.path_of(k)
                if os.path.exists(p):
                    os.remove(p)
