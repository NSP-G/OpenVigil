# -*- coding: utf-8 -*-
"""告警历史持久化模块。

Copyright (c) 2026 CaspianFlow. 版权所有。

把每次确认的告警记到本地 JSON 文件，供界面「告警历史」栏目展示。
设计约束：
  - 巡检线程与界面线程会并发访问，所有读写加锁。
  - 任何读写失败都不能影响巡检主循环（只读目录、磁盘满等场景）。
  - 记录数有上限，避免长期运行把文件撑大。
"""
import json
import os
import threading
import time


class AlertHistory:
    """告警历史记录（线程安全）。"""

    def __init__(self, path, max_records=500):
        self.path = path
        self.max_records = max(1, int(max_records))
        self._lock = threading.Lock()
        self._records = []
        self._loaded = False
        self._stamp = None
        self._load_error = None

    @property
    def load_error(self):
        """历史文件加载失败的原因（供界面提示），正常时为 None。"""
        return self._load_error

    def _ensure_loaded(self):
        """惰性加载，并在文件被外部改动时自动重载。

        两个关键点：
        1. 必须用显式标志而不是「列表为空就重新读」来判断——
           否则清空历史后的实例会把空列表当成「未加载」，再次读盘覆盖掉文件。
        2. 巡检线程持有的是另一个 AlertHistory 实例，它写盘后本实例的内存
           副本就过期了。若只在首次访问时读盘，界面永远看不到新告警。
           因此每次访问都比对文件 mtime，变化即重载。
        """
        if not self._loaded:
            self._load()
            self._loaded = True
            self._stamp = self._file_stamp()
            return
        stamp = self._file_stamp()
        if stamp != self._stamp:
            self._load()
            self._stamp = stamp

    def _file_stamp(self):
        """文件的当前指纹：(纳秒级修改时间, 字节大小)。

        用 st_mtime_ns 而非 st_mtime：后者是秒级精度，
        巡检线程写入与界面读取若落在同一秒内就会漏判，
        导致新告警在界面上「不实时刷新」。
        文件不存在时返回 None。
        """
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self):
        """从磁盘读取历史记录。"""
        try:
            if not os.path.exists(self.path):
                self._records = []
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._records = [r for r in data if isinstance(r, dict)]
            else:
                self._records = []
        except Exception as e:
            self._records = []
            self._load_error = str(e)

    def add(self, type_, detail, confidence, image_path=None,
            window_title=None, local_fallback=False):
        """追加一条告警记录，返回该记录（含生成的 id）。"""
        now = time.time()
        record = {
            "id": f"{int(now * 1000)}-{len(self._records)}",
            "ts": now,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "type": str(type_ or "未分类"),
            "detail": str(detail or ""),
            "confidence": _safe_confidence(confidence),
            "image": image_path or "",
            "window": window_title or "",
            "local_fallback": bool(local_fallback),
        }
        with self._lock:
            self._ensure_loaded()
            self._records.append(record)
            if len(self._records) > self.max_records:
                self._records = self._records[-self.max_records:]
            self._save_locked()
        return record

    def list(self, limit=None, offset=0):
        """返回历史记录（最新的在前）。"""
        with self._lock:
            self._ensure_loaded()
            records = list(reversed(self._records))
        if offset:
            records = records[offset:]
        if limit:
            records = records[:limit]
        return records

    def clear(self):
        """清空历史记录。"""
        with self._lock:
            self._records = []
            self._loaded = True
            self._load_error = None
            self._save_locked()

    def count(self):
        with self._lock:
            self._ensure_loaded()
            return len(self._records)

    def _save_locked(self):
        """落盘；失败时静默降级（界面仍能看到本次会话的历史）。"""
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._records, f, ensure_ascii=False, indent=1)
            # 自己刚写过的文件不应再触发重载
            self._stamp = self._file_stamp()
        except Exception:
            pass


def _safe_confidence(value):
    """把置信度安全规范到 [0,1]，脏数据不影响界面渲染。"""
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    if conf > 1.0:
        conf = conf / 100.0
    return min(1.0, max(0.0, conf))
