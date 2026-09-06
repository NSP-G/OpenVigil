# -*- coding: utf-8 -*-
"""观测日志（episodic memory）：只增不改的证据链。

Copyright (c) 2026 CaspianFlow. 版权所有。

============================================================================
设计原则：日志记「发生了什么」，环境文件记「由此我们知道什么」
============================================================================

日志是不可变证据链，环境文件是可修订的知识库。两者必须分开，
否则时间一长就分不清「系统实际看到了什么」与「系统自己总结成了什么」，
一旦总结错了还无法回到原始记录去纠偏。分离是纠错能力的根基。

四层结构，每层解决一类问题：

  frame      帧级观测   谁在哪、在干什么（姿势/朝向）——纯传感器式记录
  event      事件级     一次有意义的聚合（聚集/移动/离座）
  alert      告警级     真正推送给老师的，必带证据链引用
  correction 纠正级     老师的人工反馈，永久留痕

============================================================================
铁律：日志里只写观测，不写推断
============================================================================

  ✓ "位置(3,2)持续低头约240秒，未见书写动作"
  ✗ "位置(3,2)学生在偷看小说"

后者是定性指控，属于推断，只能出现在环境文件与查询回答里，
且必须附带置信度。日志保持纯净，才能在任何时刻被重新解读。

============================================================================
隐私约束
============================================================================

- 原始截图原则上不落盘，只存结构化特征与 scene_hash
- 日志按滚动窗口保留（默认 90 天），过期自动清理
- 学生的可识别细节支持一键导出与删除
"""
import json
import os
import threading
import time
from collections import deque

# 日志层级
LEVEL_FRAME = "frame"
LEVEL_EVENT = "event"
LEVEL_ALERT = "alert"
LEVEL_CORRECTION = "correction"

# 默认保留天数
DEFAULT_RETENTION_DAYS = 90


def _now_iso():
    """当前时间的 ISO8601 字符串（本地时区，带秒）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ObservationLog:
    """观测日志写入器。

    按天分文件（vigil-log-YYYYMMDD.jsonl），避免单文件无限膨胀；
    写入走后台锁保护，巡检线程与界面线程可安全并发调用。

    用法：
        log = ObservationLog("/path/to/logs")
        log.frame(ts=..., entities=[...])      # 返回该条记录的 id
        log.event(kind="gathering", ...)
        log.alert(event_ids=[...], ...)
        log.correction(target_id="...", ...)
    """

    def __init__(self, directory, retention_days=DEFAULT_RETENTION_DAYS):
        self.directory = directory
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.Lock()
        self._seq = 0
        os.makedirs(self.directory, exist_ok=True)

    # ---- 内部 ----
    def _next_id(self, prefix):
        """生成单调递增的记录 ID：前缀-日期-序号。

        不用 uuid，便于人工阅读与时间排序；
        序号在进程内递增，配合日期可保证唯一性。
        """
        self._seq += 1
        # 序号在进程重启后从 0 开始，同一天重启两次会产生完全相同的 ID。
        # 混入进程标识消除这种冲突——ID 是索引与纠正引用的主键，重号会串数据。
        return "%s-%s-%06d-%s" % (prefix, time.strftime("%Y%m%d"), self._seq,
                                  os.getpid() % 10000)

    def _path_for(self, day=None):
        """某一天对应的日志文件路径。"""
        day = day or time.strftime("%Y%m%d")
        return os.path.join(self.directory, "vigil-log-%s.jsonl" % day)

    def _append(self, record):
        """原子追加一条记录（JSON 单行）。"""
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        path = self._path_for()
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                # 只 flush 到 OS 缓冲区，不做 fsync。
                # fsync 是强制落盘，机械盘上单次可达 10ms 级；
                # 巡检每 5 秒一帧都要付这个代价，且是同步阻塞的，
                # 会把整个巡检循环拖慢。观测日志不是金融流水，
                # 掉电丢几秒记录完全可以接受。
                f.flush()
        return record["id"]

    # ---- 四层写入接口 ----
    def frame(self, entities=None, scene_hash=None, activity=None,
              camera="default", ts=None, meta=None):
        """帧级观测：某一时刻画面里各实体的状态快照。

        参数：
            entities   —— 实体状态列表，每项形如
                          {"track": "P_003", "region": "3-2",
                           "posture": "head_down", "facing": "desk",
                           "bbox": [x1,y1,x2,y2]}
            scene_hash —— 场景指纹，用于检测持久性变化（墙上新贴了东西）
            activity   —— 本地活动度（可选，辅助检索）
        """
        record = {
            "id": self._next_id("fr"),
            "level": LEVEL_FRAME,
            "ts": ts or _now_iso(),
            "camera": camera,
            "scene_hash": scene_hash,
            "activity": _safe_float(activity, 0.0),
            "entities": entities or [],
        }
        if meta:
            record["meta"] = meta
        return self._append(record)

    def event(self, kind, involved=None, evidence_frame_ids=None,
              region=None, duration_sec=None, ts=None, detail=None,
              confidence=None):
        """事件级：一次有意义的聚合（聚集 / 移动 / 离座 / 场景变化）。

        参数：
            kind        —— 事件类别，使用统一词表（见 EVENT_KINDS）
            involved    —— 涉及的实体或区域标识
            evidence    —— 支撑该事件的帧级记录 ID 列表，构成证据链
            duration    —— 持续秒数，用于计算事件生命周期
        """
        record = {
            "id": self._next_id("ev"),
            "level": LEVEL_EVENT,
            "ts": ts or _now_iso(),
            "kind": kind,
            "region": region,
            "involved": involved or [],
            "evidence": evidence_frame_ids or [],
            "duration_sec": duration_sec,
        }
        if detail:
            record["detail"] = detail
        if confidence is not None:
            record["confidence"] = _safe_float(confidence, 0.5)
        return self._append(record)

    def alert(self, event_ids=None, category=None, description=None,
              confidence=None, observation=None, ts=None, notified=False):
        """告警级：真正推送给老师的条目，必须携带证据链。

        参数：
            observation —— 原始观测描述（模型看到的）
            description —— 推断性描述（面向老师的解读）
            两者分开存储，是实现「观察与推断分离」的关键。
        """
        record = {
            "id": self._next_id("al"),
            "level": LEVEL_ALERT,
            "ts": ts or _now_iso(),
            "category": category,
            "confidence": _safe_float(confidence, 0.5),
            "observation": observation or "",
            "description": description or "",
            "events": event_ids or [],
            "notified": bool(notified),
            "status": "pending",   # pending / confirmed / dismissed
        }
        return self._append(record)

    def correction(self, target_id, verdict, reason=None, corrected_by="teacher",
                   replacement=None, ts=None):
        """纠正级：老师的人工反馈，永久留痕。

        参数：
            target_id   —— 被纠正的告警 ID
            verdict     —— confirmed（确认无误）/ dismissed（误报）
            replacement —— 老师给出的正确描述（"重新描述"场景）
        """
        record = {
            "id": self._next_id("cr"),
            "level": LEVEL_CORRECTION,
            "ts": ts or _now_iso(),
            "target_id": target_id,
            "verdict": verdict,
            "corrected_by": corrected_by,
        }
        if reason:
            record["reason"] = reason
        if replacement:
            record["replacement"] = replacement
        return self._append(record)

    # ---- 读取 ----
    def iter_records(self, day=None, level=None, since=None):
        """遍历日志记录，可按天或层级过滤。返回 dict 生成器。"""
        if day:
            paths = [self._path_for(day)]
        else:
            paths = self._list_files()
        for path in paths:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if level and rec.get("level") != level:
                        continue
                    if since and rec.get("ts", "") < since:
                        continue
                    yield rec

    def _list_files(self):
        """列出所有日志文件，按日期升序。"""
        if not os.path.isdir(self.directory):
            return []
        names = [n for n in os.listdir(self.directory)
                 if n.startswith("vigil-log-") and n.endswith(".jsonl")]
        return [os.path.join(self.directory, n) for n in sorted(names)]

    def count(self, level=None):
        n = 0
        for _ in self.iter_records(level=level):
            n += 1
        return n

    # ---- 保留期管理 ----
    def purge_expired(self, now=None):
        """清理超过保留期的日志文件，返回被删除的文件名列表。

        对着未成年人建长期画像，数据最小化是合规底线而非可选项。
        """
        now = now or time.time()
        cutoff = now - self.retention_days * 86400
        removed = []
        for path in self._list_files():
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed.append(os.path.basename(path))
            except OSError:
                continue
        return removed

    def purge_if_due(self, now=None):
        """按需清理过期日志（默认每天最多一次）。

        对着未成年人建长期画像，90 天滚动保留是合规底线。
        但此前 purge_expired 从未被任何生产代码调用，
        日志只增不减——这条底线等于没写。
        """
        now = now or time.time()
        if getattr(self, "_last_purge_ts", 0.0) and now - self._last_purge_ts < 86400.0:
            return []
        self._last_purge_ts = now
        return self.purge_expired(now=now)

    def export_range(self, start_day, end_day, dest_path):
        """导出指定日期范围的日志（供老师下载或合规审计）。"""
        written = 0
        with open(dest_path, "w", encoding="utf-8") as out:
            for path in self._list_files():
                name = os.path.basename(path)
                day = name[len("vigil-log-"):-len(".jsonl")]
                if not (start_day <= day <= end_day):
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            out.write(line)
                            written += 1
        return written


# 事件类别词表：与模型输出的类别保持一致，便于统计与检索
EVENT_KINDS = (
    "gathering",       # 聚集
    "scuffle",         # 打闹冲突
    "leave_seat",      # 离座走动
    "head_down",       # 长时间低头
    "scene_change",    # 场景持久性变化（新贴了东西、桌椅挪动）
    "attention_drop",  # 群体注意力下降
    "orderly",         # 有序活动
    "empty",           # 空场 / 人员骤减
)


class EventTracker:
    """事件生命周期追踪器。

    真正的混乱不会维持十分钟还保持同一形态；
    而"看通知"有清晰的生命周期：形成 → 维持 → 有序解散。
    把「持续多久、往哪个方向演化」作为判据，能显著降低误判。
    """

    def __init__(self, max_events=200, max_active=64):
        self._active = {}       # kind+region -> 进行中的事件
        self._finished = deque(maxlen=max_events)
        # 活跃事件上限。key 由 kind+region 组成，而 region 直接来自模型输出，
        # 写法可能五花八门（"3-2"/"第3排"/"前排"）。不加限制的话，
        # 每换一种写法就多一个永不结束的事件，字典只增不减——内存缓慢泄漏。
        self.max_active = max(1, int(max_active))

    def update(self, kind, region, present, ts=None, confidence=None, detail=None):
        """上报一次观测结果，返回事件状态。

        返回 dict：
            {"status": "forming"/"ongoing"/"ended", "event": {...}}
        """
        key = "%s@%s" % (kind, region or "global")
        ts = ts or _now_iso()
        ev = self._active.get(key)

        if present:
            if ev is None:
                # 达到上限时丢弃最久未更新的活跃事件，给新事件腾位置
                if len(self._active) >= self.max_active:
                    oldest_key = min(
                        self._active,
                        key=lambda k: self._active[k].get("last_ts") or "")
                    del self._active[oldest_key]
                ev = {
                    "kind": kind, "region": region, "start_ts": ts,
                    "last_ts": ts, "hits": 1, "peak_confidence": confidence or 0.5,
                    "detail": detail,
                }
                self._active[key] = ev
                status = "forming"
            else:
                ev["last_ts"] = ts
                ev["hits"] += 1
                if confidence:
                    ev["peak_confidence"] = max(ev["peak_confidence"], confidence)
                if detail and not ev.get("detail"):
                    ev["detail"] = detail
                status = "ongoing"
            ev["status"] = status
            return {"status": status, "event": dict(ev)}

        # 事件消失
        if ev is None:
            return {"status": "none", "event": None}
        ev["end_ts"] = ts
        ev["duration_sec"] = self._elapsed(ev["start_ts"], ts)
        ev["status"] = "ended"
        del self._active[key]
        self._finished.append(dict(ev))
        return {"status": "ended", "event": dict(ev)}

    @staticmethod
    def _elapsed(start_iso, end_iso):
        """计算两个 ISO 时间戳之间的秒数；解析失败返回 None。"""
        try:
            t1 = time.mktime(time.strptime(start_iso[:19], "%Y-%m-%dT%H:%M:%S"))
            t2 = time.mktime(time.strptime(end_iso[:19], "%Y-%m-%dT%H:%M:%S"))
            return max(0.0, t2 - t1)
        except (ValueError, TypeError):
            return None

    def active_events(self):
        return [dict(e) for e in self._active.values()]

    def recent_finished(self, n=20):
        return list(self._finished)[-n:]

    def reset(self):
        self._active.clear()
        self._finished.clear()
