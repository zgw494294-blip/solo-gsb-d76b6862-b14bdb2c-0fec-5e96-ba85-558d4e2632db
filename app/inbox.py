"""收件库：810 发票的幂等收件、重发识别与持久化。

收件键：(ISA06, GS02, BIG02) —— 发送方标识 + 应用发送方代码 + 发票号。

业务指纹（SHA-256）只取业务内容，信封控制号（ISA13/GS06/ST02/SE02/GE02/
IEA02）与 IT1 行顺序均不参与：

  * BIG 段全部元素（原文）；
  * 各 IT1 段全部元素，按 IT101 行号排序；其中 IT102 数量规范为 10^-3 的
    整数（千分位），IT104 单价规范为 10^-4 的整数（万分位），因此
    "1.5" 与 "1.50"、"19.99" 与 "19.9900" 指纹相同；
  * TDS 总额（整数分）。

receipt_id 由收件键与业务指纹共同哈希确定，因此同一收件内容无论何时提交、
无论服务是否重启，得到的 receipt_id 都固定不变。

持久化使用 SQLite（单写者，进程内锁串行化读-改-写；唯一索引兜底）：
首次提交原子写入原文与核验结果并返回 replayed=false；同键同指纹重发返回
原 receipt_id 与 replayed=true；同键不同指纹抛 InboxConflict（HTTP 409），
原记录保持不变。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass

from .verifier import Invoice

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    receipt_id  TEXT PRIMARY KEY,
    isa06       TEXT NOT NULL,
    gs02        TEXT NOT NULL,
    big02       TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    edi_text    TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS inbox_key ON inbox (isa06, gs02, big02);
"""


class InboxConflict(Exception):
    """同收件键已存在不同业务指纹的记录（HTTP 409），原记录不改动。"""

    def __init__(self, key: tuple[str, str, str]) -> None:
        isa06, gs02, big02 = key
        self.message = (
            f"收件键 (ISA06={isa06!r}, GS02={gs02!r}, BIG02={big02!r}) "
            "已保存过业务内容不同的发票，本次提交被拒绝，原记录未改动")
        super().__init__(self.message)
        self.code = "INBOX_CONFLICT"


class ReceiptNotFound(Exception):
    """receipt_id 不存在（HTTP 404）。"""

    def __init__(self, receipt_id: str) -> None:
        self.message = f"receipt_id 不存在: {receipt_id!r}"
        super().__init__(self.message)
        self.code = "RECEIPT_NOT_FOUND"


def _scale_int(raw: str, scale: int) -> int:
    """把已核验的十进制原文规范为 10**-scale 的整数（去掉前导零与取值无关的尾零）。

    调用前报文已通过 /v1/verify 核验，格式必然合法（小数位不超过 scale）。
    """
    whole, dot, frac = raw.partition(".")
    if not dot:
        return int(whole) * 10 ** scale
    return int(whole + frac.ljust(scale, "0"))


def inbox_key(invoice: Invoice) -> tuple[str, str, str]:
    """收件键：(ISA06, GS02, BIG02)。"""
    return (invoice.isa06, invoice.gs02, invoice.invoice_number)


def business_fingerprint(invoice: Invoice) -> str:
    """业务指纹：BIG 段 + 按 IT101 排序的 IT1 段（IT102/IT104 规范化）+ TDS 整数分。"""
    it1 = []
    for fields in invoice.it1_segments:
        canon = [fields[0], fields[1],
                 str(_scale_int(fields[2], 3)),   # IT102 -> 千分位整数
                 fields[3],
                 str(_scale_int(fields[4], 4))]   # IT104 -> 万分位整数
        canon.extend(fields[5:])
        it1.append(canon)
    it1.sort(key=lambda f: f[1])                  # 行顺序不参与指纹
    payload = {
        "big": list(invoice.big_segment),
        "it1": it1,
        "tds": invoice.total_amount,              # TDS 整数分
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def receipt_id_for(key: tuple[str, str, str], fingerprint: str) -> str:
    """固定 receipt_id：由收件键与业务指纹确定，重启、重发均不变。"""
    blob = json.dumps({"key": list(key), "fingerprint": fingerprint},
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InboxRecord:
    receipt_id: str
    edi_text: str
    result: dict          # 首次保存的核验结果（invoice_number/lines/total_amount）
    replayed: bool        # 本次提交是否为同键同指纹的重发


class InboxStore:
    """SQLite 收件库。单连接 + 进程内锁；唯一索引 (isa06, gs02, big02) 兜底并发。"""

    def __init__(self, db_path: str) -> None:
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)  # 数据目录不存在时自动创建
        self._conn = sqlite3.connect(db_path, check_same_thread=False,
                                     isolation_level=None)  # 自动提交，单语句即原子
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def submit(self, key: tuple[str, str, str], fingerprint: str,
               receipt_id: str, edi_text: str, result: dict) -> InboxRecord:
        """原子收件：首次保存返回 replayed=False；同键同指纹返回原记录
        （replayed=True）；同键不同指纹抛 InboxConflict，原记录不改动。"""
        result_json = json.dumps(result, ensure_ascii=False)
        isa06, gs02, big02 = key
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO inbox"
                " (receipt_id, isa06, gs02, big02, fingerprint, edi_text, result_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (receipt_id, isa06, gs02, big02, fingerprint,
                 edi_text, result_json))
            inserted = cur.rowcount == 1
            row = self._conn.execute(
                "SELECT receipt_id, fingerprint, edi_text, result_json"
                " FROM inbox WHERE isa06 = ? AND gs02 = ? AND big02 = ?",
                key).fetchone()
        assert row is not None  # INSERT 被忽略说明同键记录必然已存在
        if row[1] != fingerprint:
            raise InboxConflict(key)
        if inserted:
            return InboxRecord(receipt_id=receipt_id, edi_text=edi_text,
                               result=result, replayed=False)
        return InboxRecord(receipt_id=row[0], edi_text=row[2],
                           result=json.loads(row[3]), replayed=True)

    def get(self, receipt_id: str) -> InboxRecord:
        """按 receipt_id 取首次保存的原文与核验结果；不存在抛 ReceiptNotFound。"""
        row = self._conn.execute(
            "SELECT edi_text, result_json FROM inbox WHERE receipt_id = ?",
            (receipt_id,)).fetchone()
        if row is None:
            raise ReceiptNotFound(receipt_id)
        return InboxRecord(receipt_id=receipt_id, edi_text=row[0],
                           result=json.loads(row[1]), replayed=False)
