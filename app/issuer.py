"""结构化发票 -> ASC X12 004010 810 报文生成。

入口：issue(...) -> (edi_text, total_amount)，total_amount 为 TDS01 整数分。

分隔符固定：元素 `*`、组件 `:`、段终止符 `~`；ISA 段固定 106 字节
（ISA06/ISA08 右侧补空格至 15 字符）。控制号两两配对
（ISA13=IEA02、GS06=GE02、ST02=SE02），SE01 为 ST..SE（含两端）段数，
GE01/IEA01 均为 1。生成的报文不超过 64 KiB，且返回前经 verify_text 自检，
保证可通过 /v1/verify。

金额规则与 verifier 完全一致：IT102（数量，正，至多 3 位小数）* IT104
（单价，非负，至多 4 位小数）逐行以整数定点数精确相乘，按 ROUND_HALF_UP
取整到整数分；各行整数分之和写入 TDS01（N2 隐含两位小数）。

输入校验（失败抛 IssueError，HTTP 422，错误体带 field 与 code）：
  * 所有字段必须为纯 ASCII，不得包含分隔符（* : ~）或控制字符；
  * 各字段长度不得超出 X12 004010 元素上限（见 issue 内的逐项校验）；
  * ISA13 恰好 9 位数字；ISA09/GS04/BIG01 为合法日历日期，
    ISA10/GS05 为合法时间；
  * 发票行 1～100 行，IT101 行号非空且全局唯一；
  * IT102 为正且至多 3 位小数，IT104 非负且至多 4 位小数。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional, Protocol

from .verifier import MAX_BYTES, verify_text

MAX_LINES = 100

ELEMENT_SEP = "*"
COMPONENT_SEP = ":"
SEGMENT_TERM = "~"

# 与 verifier 中 IT102 / IT104 相同的十进制格式约束
_QTY_RE = re.compile(r"^(\d+)(?:\.(\d{1,3}))?$")   # IT102: 数量，至多 3 位
_UNIT_RE = re.compile(r"^(\d+)(?:\.(\d{1,4}))?$")  # IT104: 单价，至多 4 位
_NINE_DIGITS_RE = re.compile(r"^\d{9}$")           # ISA13

_FORBIDDEN = (ELEMENT_SEP, COMPONENT_SEP, SEGMENT_TERM)

# 行内字段长度上限：IT101(350) 20、IT102(358) 15、IT104(639) 17
_IT101_MAX = 20
_IT102_MAX = 15
_IT104_MAX = 17


class IssueError(Exception):
    """发票生成输入非法；HTTP 422，错误体带 field 与 code。"""

    def __init__(self, code: str, message: str,
                 field: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class IssueLineIn(Protocol):
    """发票行输入（与 HTTP 层解耦，仅要求这三个属性）。"""

    it101: str
    it102: str
    it104: str


def _check_ascii(field: str, value: str) -> None:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise IssueError("NOT_ASCII",
                         f"字段 {field} 必须为纯 ASCII: {value!r}", field)


def _check_text(field: str, value: str, min_len: int, max_len: int) -> None:
    """通用文本校验：ASCII、非空、长度范围、分隔符/控制字符注入。"""
    _check_ascii(field, value)
    if value == "":
        raise IssueError("FIELD_EMPTY", f"字段 {field} 不能为空", field)
    if len(value) > max_len:
        raise IssueError("FIELD_TOO_LONG",
                         f"字段 {field} 长度 {len(value)} 超过上限 {max_len}",
                         field)
    if len(value) < min_len:
        raise IssueError("FIELD_TOO_SHORT",
                         f"字段 {field} 长度 {len(value)} 不足 {min_len}",
                         field)
    if any(ch in value for ch in _FORBIDDEN) or any(
            ord(ch) < 0x20 or ch == "\x7f" for ch in value):
        raise IssueError("SEPARATOR_INJECTION",
                         f"字段 {field} 含分隔符或控制字符: {value!r}", field)


def _check_date(field: str, value: str, year_digits: int) -> None:
    """ISA09（YYMMDD）或 GS04/BIG01（CCYYMMDD）：定长数字 + 合法日历日期。"""
    digits = year_digits + 4
    _check_text(field, value, digits, digits)
    if not value.isdigit():
        raise IssueError(
            "DATE_INVALID",
            f"字段 {field} 必须为 {digits} 位数字日期: {value!r}", field)
    if year_digits == 2:
        year = 2000 + int(value[0:2])
        month, day = int(value[2:4]), int(value[4:6])
    else:
        year = int(value[0:4])
        month, day = int(value[4:6]), int(value[6:8])
    try:
        date(year, month, day)
    except ValueError:
        raise IssueError("DATE_INVALID",
                         f"字段 {field} 不是合法日历日期: {value!r}", field)


def _check_time(field: str, value: str, min_len: int, max_len: int) -> None:
    """ISA10（HHMM）或 GS05（HHMM[SS[DD]]）：数字 + 合法时分秒。"""
    _check_text(field, value, min_len, max_len)
    if not value.isdigit() or len(value) not in (4, 6, 8):
        raise IssueError(
            "TIME_INVALID",
            f"字段 {field} 必须为 4/6/8 位数字时间: {value!r}", field)
    hh, mm = int(value[0:2]), int(value[2:4])
    ss = int(value[4:6]) if len(value) >= 6 else 0
    if hh > 23 or mm > 59 or ss > 59:
        raise IssueError("TIME_INVALID",
                         f"字段 {field} 不是合法时间: {value!r}", field)


def _parse_decimal(raw: str, pattern: re.Pattern[str]) -> Optional[tuple[int, int]]:
    """十进制原文 -> (尾数, 标度)；格式不符返回 None。"""
    m = pattern.fullmatch(raw)
    if not m:
        return None
    whole, frac = m.group(1), m.group(2) or ""
    return (int(whole + frac), len(frac))


def _line_to_cents(qty: tuple[int, int], unit: tuple[int, int]) -> int:
    """数量 * 单价，按 ROUND_HALF_UP 取整到整数分（与 verifier 相同规则）。

    乘积定点标度为 qty 与 unit 的标度之和（≤ 7），换算成分时余数按
    非负数意义上的四舍五入（ROUND_HALF_UP）处理。
    """
    product = qty[0] * unit[0]
    product_scale = qty[1] + unit[1]
    if product_scale <= 2:
        return product * 10 ** (2 - product_scale)
    factor = 10 ** (product_scale - 2)
    return (2 * product + factor) // (2 * factor)


def issue(
    *,
    isa05: str, isa06: str, isa07: str, isa08: str,
    isa09: str, isa10: str, isa13: str,
    gs02: str, gs03: str, gs04: str, gs05: str, gs06: str,
    st02: str, big01: str, big02: str,
    lines: list[IssueLineIn],
) -> tuple[str, int]:
    """由结构化发票生成 810 报文；输入非法抛 IssueError。

    返回 (edi_text, total_amount)：edi_text 为完整 004010 X12 810 报文，
    total_amount 为 TDS01 总额（整数分）。
    """
    # ---- 文本字段：ASCII、非空、长度（X12 004010 元素上限）、分隔符注入 --
    _check_text("isa05", isa05, 2, 2)    # I05  交换发送方限定符
    _check_text("isa06", isa06, 1, 15)   # I06  交换发送方标识（补空格至 15）
    _check_text("isa07", isa07, 2, 2)    # I05  交换接收方限定符
    _check_text("isa08", isa08, 1, 15)   # I07  交换接收方标识（补空格至 15）
    _check_text("gs02", gs02, 1, 15)     # 142  应用发送方代码
    _check_text("gs03", gs03, 1, 15)     # 124  应用接收方代码
    _check_text("gs06", gs06, 1, 9)      # 28   组控制号
    _check_text("st02", st02, 1, 9)      # 329  交易集控制号
    _check_text("big02", big02, 1, 22)   # 76   发票号

    _check_ascii("isa13", isa13)
    if not _NINE_DIGITS_RE.fullmatch(isa13):
        raise IssueError("ISA13_INVALID",
                         f"ISA13 必须为恰好 9 位数字: {isa13!r}", "isa13")

    _check_date("isa09", isa09, year_digits=2)
    _check_time("isa10", isa10, min_len=4, max_len=4)
    _check_date("gs04", gs04, year_digits=4)
    _check_time("gs05", gs05, min_len=4, max_len=8)
    _check_date("big01", big01, year_digits=4)

    # ---- 发票行 --------------------------------------------------------
    if not lines:
        raise IssueError("LINES_EMPTY", "至少需要 1 行发票行", "lines")
    if len(lines) > MAX_LINES:
        raise IssueError("LINES_TOO_MANY",
                         f"发票行数 {len(lines)} 超过上限 {MAX_LINES}",
                         "lines")

    line_ids: set[str] = set()
    total_cents = 0
    for pos, line in enumerate(lines):
        # field 定位到具体行（下标从 0 开始，与 JSON 数组一致）
        f101 = f"lines[{pos}].it101"
        f102 = f"lines[{pos}].it102"
        f104 = f"lines[{pos}].it104"

        _check_text(f101, line.it101, 1, _IT101_MAX)
        if line.it101 in line_ids:
            raise IssueError("IT101_DUPLICATE",
                             f"IT101 行号重复: {line.it101!r}", f101)
        line_ids.add(line.it101)

        _check_text(f102, line.it102, 1, _IT102_MAX)
        qty = _parse_decimal(line.it102, _QTY_RE)
        if qty is None or qty[0] <= 0:
            raise IssueError(
                "IT102_INVALID",
                f"IT102 数量必须为正的十进制数（至多 3 位小数）: "
                f"{line.it102!r}", f102)

        _check_text(f104, line.it104, 1, _IT104_MAX)
        unit = _parse_decimal(line.it104, _UNIT_RE)
        if unit is None:
            raise IssueError(
                "IT104_INVALID",
                f"IT104 单价必须为非负十进制数（至多 4 位小数）: "
                f"{line.it104!r}", f104)

        total_cents += _line_to_cents(qty, unit)

    # ---- 组装报文：分隔符固定 * : ~，ISA 固定 106 字节 ------------------
    isa_fields = [
        "00", "          ",        # ISA01/02
        "00", "          ",        # ISA03/04
        isa05, isa06.ljust(15),    # ISA05/06
        isa07, isa08.ljust(15),    # ISA07/08
        isa09, isa10,              # ISA09/10
        "U", "00401",              # ISA11/12
        isa13,                     # ISA13
        "0",                       # ISA14
        "P",                       # ISA15
        COMPONENT_SEP,             # ISA16
    ]
    isa_segment = ("ISA" + ELEMENT_SEP + ELEMENT_SEP.join(isa_fields)
                   + SEGMENT_TERM)
    assert len(isa_segment) == 106  # ISA 段固定 106 字节

    def seg(tag: str, *elements: str) -> str:
        return ELEMENT_SEP.join([tag, *elements]) + SEGMENT_TERM

    body = [
        seg("GS", "IN", gs02, gs03, gs04, gs05, gs06, "X", "004010"),
        seg("ST", "810", st02),
        seg("BIG", big01, big02),
        *(seg("IT1", line.it101, line.it102, "EA", line.it104)
          for line in lines),
        seg("TDS", str(total_cents)),           # N2：整数分
        seg("SE", str(len(lines) + 4), st02),   # SE01 = ST..SE（含两端）段数
        seg("GE", "1", gs06),
        seg("IEA", "1", isa13),
    ]
    edi_text = isa_segment + "".join(body)

    if len(edi_text.encode("ascii")) > MAX_BYTES:
        raise IssueError("PAYLOAD_TOO_LARGE", "生成的报文超过 64 KiB 上限")

    # 自检：生成的报文必须能通过 /v1/verify；总额以核验结果为准
    invoice = verify_text(edi_text)
    return edi_text, invoice.total_amount
