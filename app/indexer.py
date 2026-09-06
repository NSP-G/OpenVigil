# -*- coding: utf-8 -*-
"""日志索引器：时间 / 空间 / 语义三层异步索引。

Copyright (c) 2026 CaspianFlow. 版权所有。

============================================================================
为什么需要索引
============================================================================

老师问「上周三第二节课谁说话最多」，系统必须在秒级回答。
如果每次查询都全量扫描几个月累积的 JSONL，延迟会大到不可用。

    时间索引   →  按 ts 范围快速定位
    空间索引   →  "讲台左侧 2 米内"
    语义索引   →  "说话""低头""数学课"

这就是"了然于心"的工程实现——不是每次现算，而是平时就建好了账。

============================================================================
异步构建
============================================================================

索引构建放在后台线程，日志落盘后异步进行，
绝不阻塞巡检主循环——抓帧与判定的实时性优先于索引新鲜度。
"""
import os
import re
import threading
import time

from .memory import (
    LEVEL_ALERT,
    LEVEL_CORRECTION,
    LEVEL_EVENT,
    LEVEL_FRAME,
    ObservationLog,
)


def _ts_key(iso_str):
    """把 ISO 时间戳转成可比较的排序键（YYYYMMDDHHMMSS）。"""
    return re.sub(r"[^0-9]", "", str(iso_str or "")).ljust(14, "0")[:14]


def _is_cjk(text):
    """判断字符串是否主要由中日韩文字构成。"""
    if not text:
        return False
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk * 2 >= len(text)


class TimeIndex:
    """时间索引：按天分桶，桶内有序。

    查询时先定位到相关日期桶，再做范围过滤，
    避免扫描无关日期的全部记录。
    """

    def __init__(self):
        self._buckets = {}   # day -> [record, ...]（按 ts 升序）
        self._lock = threading.Lock()

    def add(self, record):
        ts = record.get("ts", "")
        day = ts[:10].replace("-", "")
        with self._lock:
            bucket = self._buckets.setdefault(day, [])
            bucket.append(record)
            # 保持有序（大多数情况下新记录追加在末尾）
            if len(bucket) > 1 and _ts_key(bucket[-2].get("ts")) > _ts_key(ts):
                bucket.sort(key=lambda r: _ts_key(r.get("ts")))

    def range(self, start_iso, end_iso):
        """查询时间范围内的记录，按时间升序。"""
        s, e = _ts_key(start_iso), _ts_key(end_iso)
        if s > e:
            s, e = e, s
        start_day, end_day = s[:8], e[:8]
        out = []
        with self._lock:
            for day in sorted(self._buckets):
                if not (start_day <= day <= end_day):
                    continue
                for rec in self._buckets[day]:
                    k = _ts_key(rec.get("ts"))
                    if s <= k <= e:
                        out.append(rec)
        return out

    def clear(self):
        with self._lock:
            self._buckets.clear()

    @property
    def size(self):
        return sum(len(v) for v in self._buckets.values())


class SpaceIndex:
    """空间索引：网格分区。

    区域标识形如 "3-2"（三排二座）或区域名（"front"/"rear"）。
    网格索引让「讲台左侧」「后排」这类空间查询不必全表扫描。
    """

    def __init__(self):
        self._grid = {}      # region -> [record, ...]
        self._lock = threading.Lock()

    @staticmethod
    def _regions_of(record):
        """从记录中提取所有相关的区域标识。"""
        out = set()
        region = record.get("region")
        if region:
            out.add(str(region))
        for item in (record.get("involved") or []):
            out.add(str(item))
        for ent in (record.get("entities") or []):
            if isinstance(ent, dict) and ent.get("region"):
                out.add(str(ent["region"]))
        return out

    def add(self, record):
        regions = self._regions_of(record)
        with self._lock:
            for r in regions:
                self._grid.setdefault(r, []).append(record)

    def query(self, regions):
        """查询命中任一指定区域的记录（去重后按时间升序）。"""
        wanted = {str(r) for r in (regions or [])}
        seen = set()
        out = []
        with self._lock:
            for r in wanted:
                for rec in self._grid.get(r, []):
                    rid = rec.get("id")
                    if rid in seen:
                        continue
                    seen.add(rid)
                    out.append(rec)
        out.sort(key=lambda r: _ts_key(r.get("ts")))
        return out

    def clear(self):
        with self._lock:
            self._grid.clear()

    @property
    def regions(self):
        with self._lock:
            return sorted(self._grid.keys())


class SemIndex:
    """语义索引：轻量倒排表。

    不做向量嵌入（本地无模型、且增加依赖），
    而是对事件类别、关键词、课程名等结构化字段建倒排。
    对本项目要支撑的查询类型，这比向量检索更准确也更省。
    """

    _TOKEN_RE = re.compile(r"[一-鿿]+|[A-Za-z]+|[0-9]+")

    def __init__(self):
        self._postings = {}   # token -> set(record_id)
        self._records = {}    # record_id -> record（统一取回，避免从倒排链反查）
        self._lock = threading.Lock()

    @classmethod
    def _tokenize(cls, text):
        """分词：中文按二元切分（bigram），英文数字按词。

        中文不能用整串做索引——「后排学生打闹推搡」若作为一个整体 token，
        查询「打闹」就永远命中不了。二元切分后得到
        「后排/排学/学生/生打/打闹/闹推/推搡」，查询词同样切分即可命中。
        这是无第三方分词依赖时检索中文的常用做法。
        """
        out = []
        for chunk in cls._TOKEN_RE.findall(str(text or "")):
            if _is_cjk(chunk):
                if len(chunk) == 1:
                    out.append(chunk)
                else:
                    out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
            elif len(chunk) > 1:
                out.append(chunk)
        return out

    @classmethod
    def _tokens_of(cls, record):
        parts = []
        for key in ("kind", "category", "detail", "description",
                    "observation", "reason", "note"):
            parts.append(str(record.get(key) or ""))
        for e in (record.get("entities") or []):
            if isinstance(e, dict):
                parts.append(str(e.get("posture") or ""))
        return cls._tokenize(" ".join(parts))

    def add(self, record):
        rid = record.get("id")
        if rid is None:
            return
        tokens = set(self._tokens_of(record))
        with self._lock:
            self._records[rid] = record
            for t in tokens:
                self._postings.setdefault(t, set()).add(rid)

    def query(self, text, limit=None):
        """按关键词查询，返回按命中词数排序的记录（多的在前）。"""
        tokens = set(self._tokenize(text))
        if not tokens:
            return []
        scores = {}
        with self._lock:
            for t in tokens:
                for rid in self._postings.get(t, ()):
                    scores[rid] = scores.get(rid, 0) + 1
            ranked = sorted(
                scores.items(),
                key=lambda kv: (-kv[1], _ts_key(self._records.get(kv[0], {}).get("ts"))))
            out = [self._records[rid] for rid, _ in ranked
                   if rid in self._records]
        return out[:limit] if limit else out

    def clear(self):
        with self._lock:
            self._postings.clear()
            self._records.clear()

    @property
    def vocabulary(self):
        with self._lock:
            return len(self._postings)


class LogIndexer:
    """三合一索引器，支持后台异步重建。

    用法：
        idx = LogIndexer()
        idx.index_record(record)        # 增量索引（写入路径调用）
        idx.rebuild_async(log)          # 后台全量重建（启动或定期调用）
        idx.stats()
    """

    def __init__(self):
        self.time = TimeIndex()
        self.space = SpaceIndex()
        self.sem = SemIndex()
        self._rebuild_thread = None
        self._stop_rebuild = False
        self._lock = threading.Lock()
        self._last_rebuild = None
        self._indexed_ids = set()

    def index_record(self, record):
        """增量索引一条记录（重复调用安全，按 id 去重）。"""
        rid = record.get("id")
        with self._lock:
            if rid in self._indexed_ids:
                return False
            self._indexed_ids.add(rid)
        self.time.add(record)
        self.space.add(record)
        self.sem.add(record)
        return True

    def rebuild_async(self, log):
        """在后台线程全量重建索引，不阻塞调用方。

        若上一次重建仍在进行，则直接返回，不叠加线程。
        """
        with self._lock:
            if self._rebuild_thread is not None and self._rebuild_thread.is_alive():
                return False
            self._stop_rebuild = False
            self._rebuild_thread = threading.Thread(
                target=self._rebuild_worker, args=(log,), daemon=True)
            self._rebuild_thread.start()
        return True

    def stop_async(self):
        """请求后台重建停止。

        重建线程是 daemon，进程不结束它就一直空转；
        日志积累几个月后一次全量重建可能跑很久。
        短生命周期的 Monitor（如"单次测试"）必须能把它收掉，
        否则每测一次就留一个线程。
        """
        with self._lock:
            self._stop_rebuild = True
        return True

    def _rebuild_worker(self, log):
        try:
            time_idx, space_idx, sem_idx = TimeIndex(), SpaceIndex(), SemIndex()
            ids = set()
            for rec in log.iter_records():
                # 每 200 条检查一次停止标志：遍历本身可能是分钟级操作，
                # 全程不检查的话 stop_async() 要等它自然跑完才生效。
                if len(ids) % 200 == 0 and self._stop_rebuild:
                    return
                rid = rec.get("id")
                if rid in ids:
                    continue
                ids.add(rid)
                time_idx.add(rec)
                space_idx.add(rec)
                sem_idx.add(rec)
            if self._stop_rebuild:
                return
            with self._lock:
                self.time, self.space, self.sem = time_idx, space_idx, sem_idx
                self._indexed_ids = ids
                self._last_rebuild = time.time()
        except Exception:
            pass   # 索引失败不应影响巡检主流程

    def wait_for_rebuild(self, timeout=10.0):
        """等待后台重建完成（测试与启动场景使用）。"""
        t = self._rebuild_thread
        if t is not None and t.is_alive():
            t.join(timeout)

    # ---- 组合查询 ----
    def query(self, start=None, end=None, regions=None, text=None,
              level=None, limit=200):
        """组合查询：时间 ∩ 空间 ∩ 语义 ∩ 层级。

        各条件为 None 时表示该维度不设限。
        """
        if start and end:
            results = self.time.range(start, end)
        elif text:
            results = self.sem.query(text, limit=limit * 3)
        elif regions:
            results = self.space.query(regions)
        else:
            results = self.sem.query("", limit=limit) or []

        if level:
            results = [r for r in results if r.get("level") == level]
        if regions:
            wanted = {str(r) for r in regions}
            results = [r for r in results if wanted & self._record_regions(r)]
        if start and end and text:
            hits = {r.get("id") for r in self.sem.query(text, limit=limit * 3)}
            results = [r for r in results if r.get("id") in hits]
        results.sort(key=lambda r: _ts_key(r.get("ts")))
        return results[:limit]

    @staticmethod
    def _record_regions(record):
        out = set()
        if record.get("region"):
            out.add(str(record["region"]))
        for item in (record.get("involved") or []):
            out.add(str(item))
        for ent in (record.get("entities") or []):
            if isinstance(ent, dict) and ent.get("region"):
                out.add(str(ent["region"]))
        return out

    def stats(self):
        return {
            "records": len(self._indexed_ids),
            "time_buckets": len(self.time._buckets),
            "regions": len(self.space.regions),
            "vocabulary": self.sem.vocabulary,
            "last_rebuild": self._last_rebuild,
        }

    def clear(self):
        with self._lock:
            self.time.clear()
            self.space.clear()
            self.sem.clear()
            self._indexed_ids.clear()


# ---- 常用聚合查询 ----

def rank_by_event(records, key_func, event_kind=None, top_k=10):
    """按事件次数给主体排名。

    这是「谁在哪个老师的课上说话最多」这类查询的底层实现。
    返回 [(key, count), ...]，按次数降序。
    """
    counts = {}
    for r in records:
        if r.get("level") != LEVEL_EVENT:
            continue
        if event_kind and r.get("kind") != event_kind:
            continue
        for k in key_func(r):
            counts[k] = counts.get(k, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[:top_k]


def subject_event_rank(indexer, start, end, kind=None, top_k=10):
    """统计指定时间范围内，各位置/学生的某类事件次数排名。

    零标注时 key 就是位置编号（"3-2"），有姓名关联后可由调用方翻译成姓名。
    """
    records = indexer.query(start=start, end=end, level=LEVEL_EVENT)
    def keys_of(rec):
        out = []
        for item in (rec.get("involved") or []):
            out.append(str(item))
        if rec.get("region"):
            out.append(str(rec["region"]))
        return out or []
    return rank_by_event(records, keys_of, event_kind=kind, top_k=top_k)
