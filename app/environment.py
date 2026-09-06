# -*- coding: utf-8 -*-
"""环境文件（semantic memory）：由日志沉淀而来的知识库。

Copyright (c) 2026 CaspianFlow. 版权所有。

============================================================================
日志是流水，环境文件是沉积岩
============================================================================

    日志只增不改  → 不可变证据链，出错可回溯
    环境文件持续修订 → 可错知识库，会被推翻、合并、置信度稀释

============================================================================
零标注基线：不依赖任何人工输入即可运行
============================================================================

学生档案为空时，全部退化为位置编号（如 "3-2" 表示三排二座）。
系统照样记录、照样统计、照样能回答"哪个位置行为异常"。
老师的名字关联是**增量增强**，绝不是启动前提。

============================================================================
个体差异基线：根治误判的正解
============================================================================

判断异常不再用全局固定阈值，而是「这个位置偏离了它自己的常态」。
李雷本来就爱低头，那低头就不算异常；
王芳一向专注突然趴下，这才是告警。
注意：这个能力**不依赖认人**——位置本身也有历史常态。

============================================================================
必须防止的风险：长期记忆 ≠ 长期正确
============================================================================

系统跑三个月后，环境文件里全是它早期粗糙判断的自我强化。
破法：定期用最新、最清晰的观测重新校验旧结论，
置信度低的主动降级。知识必须可老化，否则记忆会变成偏见。
"""
import os
import time

import yaml

# 场景指纹至少需要连续多少帧一致，才认定为持久性变化
_SCENE_CHANGE_MIN_FRAMES = 4
# 结论老化：超过这个天数未获新证据，置信度开始衰减
_STALE_AFTER_DAYS = 7
# 衰减到这个阈值以下，结论不再作为判据使用
_RETIRE_CONFIDENCE = 0.25


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp01(v):
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else float(v))


class Baseline:
    """个体（或位置）的行为基线：滑动窗口的均值与标准差。

    只收「正常」样本——混乱期的观测绝不纳入基线，
    否则打闹久了会被当成常态，反而造成漏报。
    """

    def __init__(self, mean=0.0, std=0.0, samples=0):
        self.mean = _safe_float(mean, 0.0)
        self.std = max(0.0, _safe_float(std, 0.0))
        self.samples = max(0, int(samples))

    def update(self, value, learning_rate=0.05):
        """用新样本增量更新基线（Welford 式的简化递推）。"""
        v = _safe_float(value, 0.0)
        if self.samples == 0:
            self.mean = v
            self.std = 0.0
            self.samples = 1
            return
        delta = v - self.mean
        self.mean += learning_rate * delta
        # 标准差按绝对偏差递推，避免平方放大噪声
        self.std = (1 - learning_rate) * self.std + learning_rate * abs(delta)
        self.samples += 1

    def deviation(self, value, std_floor=0.08):
        """当前值偏离基线多少个标准差。

        std_floor 防止极稳场景下标准差趋零，把微小波动放大成巨大 z 值。
        """
        if self.samples < 3:
            return 0.0
        std = max(self.std, std_floor)
        return (_safe_float(value, 0.0) - self.mean) / std

    def to_dict(self):
        return {"mean": round(self.mean, 4), "std": round(self.std, 4),
                "samples": self.samples}

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        return cls(d.get("mean", 0.0), d.get("std", 0.0), d.get("samples", 0))


class Environment:
    """环境知识库：场景布局、区域档案、实体档案、课程表。

    存储格式 YAML（人类可读、可手工修订、可进版本控制）。
    所有写操作都在内存完成后统一落盘，避免高频 IO。
    """

    # 档案条目上限。region/subject 的键直接来自模型输出的 region 字段，
    # 而模型可能返回 "3-2"、"第3排"、"前排"、"3排2座" 等任意写法。
    # 不加限制的话，每换一种写法就新建一个档案，
    # environment.yml 会随运行时间无限膨胀。
    _MAX_SUBJECTS = 200
    _MAX_REGIONS = 100

    def __init__(self, path):
        self.path = path
        self.data = self._default()
        self._dirty = False
        self._load_error = None
        self.load()

    # ---- 结构 ----
    @staticmethod
    def _default():
        return {
            "version": 1,
            "updated_at": None,
            "scene": {
                "hash": None,            # 当前场景指纹
                "hash_streak": 0,        # 该指纹连续出现的帧数
                "changes": [],           # 已确认的持久性变化
                "regions": {},           # 区域档案
            },
            "subjects": {},              # 实体档案（位置为主键，名字可选）
            "timetable": [],             # 课程表
            "stats": {"frames": 0, "events": 0, "alerts": 0},
        }

    @staticmethod
    def _evict_oldest(mapping):
        """淘汰最久未见（last_seen / first_seen 最早）的一个条目。"""
        def sort_key(kv):
            d = kv[1] if isinstance(kv[1], dict) else {}
            return str(d.get("last_seen") or d.get("first_seen") or "")
        oldest = min(mapping.items(), key=sort_key, default=None)
        if oldest is not None:
            del mapping[oldest[0]]

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                # 与默认结构合并，容忍旧版本缺失字段
                merged = self._default()
                for k, v in loaded.items():
                    if k in ("scene", "subjects", "stats") and isinstance(v, dict):
                        merged[k].update(v)
                    else:
                        merged[k] = v
                self.data = merged
        except Exception as e:
            # 文件损坏时回退到默认结构，绝不因读档失败拖垮巡检。
            # 但**不能静默**：老师积累的全部环境认知就此消失，
            # 而界面上没有任何提示，等于数据无声蒸发。
            # 这里留下面包屑，并把损坏文件改名保留，留给人工抢救。
            self.data = self._default()
            self._load_error = str(e)
            try:
                if os.path.exists(self.path):
                    os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        self.data["updated_at"] = _now_iso()
        # 先写临时文件再原子替换，避免写一半崩溃导致知识库损坏
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.data, f, allow_unicode=True,
                           default_flow_style=False, sort_keys=True)
        os.replace(tmp, self.path)
        self._dirty = False

    def flush(self):
        if self._dirty:
            self.save()

    # ---- 场景基线：哪里有什么东西 ----
    def observe_scene(self, scene_hash, frame_no=None):
        """上报当前场景指纹，检测持久性变化。

        返回 dict：
            {"changed": bool, "previous": ..., "current": ...}

        「墙上新贴了一张分组表」这类变化，只有在**持续多帧一致**时
        才被认定为真实变化——单帧抖动（有人路过遮挡）不应触发。
        """
        scene = self.data["scene"]
        prev = scene.get("hash")
        if scene_hash == prev:
            scene["hash_streak"] = int(scene.get("hash_streak") or 0) + 1
            return {"changed": False, "previous": prev, "current": scene_hash}

        # 指纹变化：先记录，等连续若干帧确认
        scene["pending_hash"] = scene_hash
        scene["pending_streak"] = 1
        scene["hash_streak"] = 0
        return {"changed": False, "previous": prev, "current": scene_hash,
                "pending": True}

    def confirm_scene_change(self, description, confidence=0.6, frame_no=None,
                             scene_hash=None):
        """确认一次场景持久性变化（由模型描述后调用）。

        scene_hash 必须显式传入：历史上这里靠 pending_hash 取值，
        而 pending_hash 只由 observe_scene() 写入，该方法早已无人调用
        （Monitor 改用 diff_detect.SceneChangeDetector）。
        结果 pending_hash 恒为 None，本方法直接 return None，
        场景变化记录**一条都没写进环境文件**，
        recent_scene_changes() 永远是空列表，
        提示词里「本场景的常态」一栏因此永远为空。
        这是「新贴分组表被误判为打闹」防线上最致命的一处失效。
        """
        scene = self.data["scene"]
        # 清掉历史遗留的 pending 状态，避免脏数据残留
        scene.pop("pending_hash", None)
        scene.pop("pending_streak", None)
        new_hash = scene_hash or scene.get("hash")
        if new_hash is None:
            return None
        record = {
            "ts": _now_iso(),
            "from_hash": scene.get("hash"),
            "to_hash": new_hash,
            "description": description,
            "confidence": _clamp01(confidence),
            "confirmed_by": [],
        }
        scene["hash"] = new_hash
        scene["changes"] = (scene.get("changes") or [])[-19:] + [record]
        self._dirty = True
        return record

    def pending_change_streak(self):
        return int(self.data["scene"].get("pending_streak") or 0)

    def bump_pending_streak(self):
        scene = self.data["scene"]
        scene["pending_streak"] = int(scene.get("pending_streak") or 0) + 1
        return scene["pending_streak"]

    def recent_scene_changes(self, n=5):
        return (self.data["scene"].get("changes") or [])[-n:]

    # ---- 区域档案 ----
    def region(self, key):
        """获取区域档案，不存在则创建空档案。"""
        regions = self.data["scene"].setdefault("regions", {})
        if key not in regions:
            if len(regions) >= self._MAX_REGIONS:
                self._evict_oldest(regions)
            regions[key] = {
                "label": key,
                "notes": [],
                "activity_baseline": Baseline().to_dict(),
                "event_counts": {},
                "first_seen": _now_iso(),
            }
            self._dirty = True
        return regions[key]

    def record_region_activity(self, key, activity, is_normal=True):
        """记录某区域的活动度样本，维护其正常基线。"""
        reg = self.region(key)
        base = Baseline.from_dict(reg.get("activity_baseline"))
        if is_normal:
            base.update(activity)
            reg["activity_baseline"] = base.to_dict()
            self._dirty = True
        return base.deviation(activity)

    def count_region_event(self, key, kind):
        reg = self.region(key)
        counts = reg.setdefault("event_counts", {})
        counts[kind] = int(counts.get(kind, 0)) + 1
        reg["last_event_ts"] = _now_iso()
        self._dirty = True

    # ---- 实体档案（位置本位）----
    def subject(self, key):
        """获取实体档案，不存在则创建。

        key 默认是位置编号（如 "3-2"）；
        老师关联姓名后可附加 name 字段，但**位置始终是主键**，
        这样每周换座位也不会导致档案错乱。
        """
        subjects = self.data["subjects"]
        if key not in subjects:
            # 达到上限时淘汰最久未见的档案，而非拒绝新建——
            # 换座位后旧位置会自然失活，新位置才有价值。
            if len(subjects) >= self._MAX_SUBJECTS:
                self._evict_oldest(subjects)
            subjects[key] = {
                "key": key,
                "name": None,            # 老师关联才有，不关联则为 None
                "name_confidence": 0.0,
                "first_seen": _now_iso(),
                "last_seen": _now_iso(),
                "attention_baseline": Baseline().to_dict(),
                "posture_histogram": {},
                "behavior_notes": [],     # 长期行为观察（带置信度）
                "event_counts": {},
            }
            self._dirty = True
        return subjects[key]

    def link_name(self, key, name, confidence=0.8):
        """老师把名字关联到位置（一次性操作，不是每周重标）。"""
        sub = self.subject(key)
        sub["name"] = name
        sub["name_confidence"] = _clamp01(confidence)
        self._dirty = True
        return sub

    def unlink_name(self, key):
        """解除名字关联（换座位后可清空，等待重新关联）。"""
        sub = self.subject(key)
        sub["name"] = None
        sub["name_confidence"] = 0.0
        self._dirty = True
        return sub

    def observe_posture(self, key, posture, attention=None, is_normal=True):
        """记录某个位置的姿势与注意力样本。

        返回该位置相对自身常态的偏离度（0 表示与平时无异）。
        """
        sub = self.subject(key)
        sub["last_seen"] = _now_iso()
        hist = sub.setdefault("posture_histogram", {})
        hist[posture] = int(hist.get(posture, 0)) + 1

        if attention is not None:
            base = Baseline.from_dict(sub.get("attention_baseline"))
            if is_normal:
                base.update(attention)
                sub["attention_baseline"] = base.to_dict()
            self._dirty = True
            return base.deviation(attention)
        self._dirty = True
        return 0.0

    def note_behavior(self, key, note, confidence=0.5):
        """追加一条长期行为观察（如「数学课持续书写」）。

        这些笔记就是系统「了然于心」的来源。
        """
        sub = self.subject(key)
        notes = sub.setdefault("behavior_notes", [])
        # 同内容不重复追加，只更新置信度与最后观察时间
        for n in notes:
            if n.get("note") == note:
                n["confidence"] = max(n.get("confidence", 0.0), _clamp01(confidence))
                n["last_seen"] = _now_iso()
                n["hits"] = int(n.get("hits", 1)) + 1
                self._dirty = True
                return n
        record = {
            "note": note,
            "confidence": _clamp01(confidence),
            "first_seen": _now_iso(),
            "last_seen": _now_iso(),
            "hits": 1,
            "superseded_by": None,
        }
        notes.append(record)
        # 超限淘汰时删的是**置信度最低**的，不是最旧的。
        # 最早形成的认知（"这个位置一向专注"）是理解个体的地基，
        # 按时间淘汰会把地基挖掉，却留下一堆鸡毛蒜皮的近期观察。
        if len(notes) > 30:
            notes.sort(key=lambda n: (
                float(n.get("confidence", 0.0)),
                str(n.get("first_seen") or ""),
            ))
            del notes[0]
        self._dirty = True
        return record

    def count_subject_event(self, key, kind):
        sub = self.subject(key)
        counts = sub.setdefault("event_counts", {})
        counts[kind] = int(counts.get(kind, 0)) + 1
        self._dirty = True

    def subject_summary(self, key):
        """生成某个位置的自然语言摘要（供查询接口使用）。"""
        sub = self.data["subjects"].get(key)
        if not sub:
            return None
        name = sub.get("name")
        label = ("%s（%s）" % (name, key)) if name else key
        conf = sub.get("name_confidence", 0.0)
        if name and conf < 0.6:
            label = "%s（推测，置信度 %.2f）" % (label, conf)
        hist = sub.get("posture_histogram", {})
        top = sorted(hist.items(), key=lambda kv: kv[1], reverse=True)[:3]
        return {
            "label": label,
            "key": key,
            "name": name,
            "name_confidence": conf,
            "first_seen": sub.get("first_seen"),
            "last_seen": sub.get("last_seen"),
            "top_postures": top,
            "behaviors": [b["note"] for b in (sub.get("behavior_notes") or [])[-5:]],
            "event_counts": sub.get("event_counts", {}),
        }

    # ---- 课程表 ----
    def set_timetable(self, entries):
        """设置课程表。

        entries 每项：{"weekday":1, "start":"08:00", "end":"08:45",
                       "course":"数学", "teacher":"王老师"}
        """
        self.data["timetable"] = entries or []
        self._dirty = True

    def current_lesson(self, ts=None):
        """查询当前处于哪一节课（无课程表时返回 None）。

        课程分段是「谁在哪个老师的课上说话最多」这类查询的基础。
        零标注时课程表为空，系统退化为纯时间段查询，功能不受影响。
        """
        entries = self.data.get("timetable") or []
        if not entries:
            return None
        now = ts or time.localtime()
        weekday = now.tm_wday + 1          # Python: 周一起 0 → 转成 1~7
        hm = "%02d:%02d" % (now.tm_hour, now.tm_min)
        for e in entries:
            if int(e.get("weekday", 0)) != weekday:
                continue
            if e.get("start", "") <= hm <= e.get("end", "99:99"):
                return e
        return None

    # ---- 知识老化：防止记忆变成偏见 ----
    def age_conclusions(self, now=None):
        """让陈旧结论随时间衰减，低于阈值则退休。

        系统跑久了，早期粗糙判断会被不断自我强化。
        必须定期用最新观测重校验，置信度低的主动降级，
        否则长期记忆会固化成偏见。
        """
        now = now or time.time()
        retired = 0
        for sub in self.data.get("subjects", {}).values():
            for note in (sub.get("behavior_notes") or []):
                if note.get("superseded_by"):
                    continue
                last = note.get("last_seen") or note.get("first_seen")
                age_days = self._days_since(last, now)
                if age_days is None:
                    continue
                if age_days > _STALE_AFTER_DAYS:
                    decay = 0.5 ** ((age_days - _STALE_AFTER_DAYS) / 7.0)
                    note["confidence"] = round(note.get("confidence", 0.5) * decay, 3)
                    if note["confidence"] < _RETIRE_CONFIDENCE:
                        note["superseded_by"] = "aged_out"
                        retired += 1
        if retired:
            self._dirty = True
        return retired

    @staticmethod
    def _days_since(iso_str, now):
        if not iso_str:
            return None
        try:
            t = time.mktime(time.strptime(str(iso_str)[:19], "%Y-%m-%dT%H:%M:%S"))
            return max(0.0, (now - t) / 86400.0)
        except (ValueError, TypeError):
            return None

    def supersede_note(self, key, old_note, new_note, confidence=0.6):
        """用新结论替换旧结论，旧结论不删除而是标记 superseded。

        完整的修正时间线本身就是价值极高的数据。
        """
        sub = self.subject(key)
        for n in (sub.get("behavior_notes") or []):
            if n.get("note") == old_note and not n.get("superseded_by"):
                n["superseded_by"] = new_note
                break
        self.note_behavior(key, new_note, confidence)
        self._dirty = True

    # ---- 统计 ----
    def bump_stat(self, name, delta=1):
        stats = self.data.setdefault("stats", {})
        stats[name] = int(stats.get(name, 0)) + delta
        self._dirty = True

    def summary(self):
        """生成环境知识库的整体摘要。"""
        subjects = self.data.get("subjects", {})
        named = sum(1 for s in subjects.values() if s.get("name"))
        return {
            "updated_at": self.data.get("updated_at"),
            "subjects_total": len(subjects),
            "subjects_named": named,
            "regions": len(self.data.get("scene", {}).get("regions", {}) or {}),
            "scene_changes": len(self.data.get("scene", {}).get("changes") or []),
            "timetable_entries": len(self.data.get("timetable") or []),
            "stats": self.data.get("stats", {}),
        }
