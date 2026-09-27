"""X12 810 (Invoice, ASC X12/004010) 报文解析与核验核心逻辑。

报文外壳顺序固定为：ISA -> GS -> ST -> BIG -> IT1(1..100) -> TDS -> SE -> GE -> IEA，
仅允许一份交换/功能组/交易集（ISA12 == "00401"，GS08 == "004010"）。

分隔符取自 106 字节 ISA 段：
  * 字节偏移 3（ISA 字段 1）为元素分隔符；
  * 字节偏移 104（ISA 字段 16）为组件分隔符（本 API 不使用，仅做合法性检查）；
  * 字节偏移 105 为段终止符。

金额规则：IT102（数量，正，至多 3 位小数）* IT104（单价，非负，至多 4 位小数）
逐行以整数定点数精确相乘（乘积标度至多 7），按 ROUND_HALF_UP 取整到整数分（0.01）；
各行整数分之和必须等于 TDS01（N2 隐含两位小数，也接受显式小数，至多两位）。

全程使用 Python 任意精度整数运算，不经过 Decimal 上下文，因此结果不受默认
28 位有效数字精度影响（64 KiB 报文、1～100 行范围内的任意位数金额均可精确核验）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

MAX_BYTES = 64 * 1024
MAX_IT1 = 100

# 至多 n 位小数的无符号十进制数（允许前导 0、无小数部分）
_QTY_RE = re.compile(r"^(\d+)(?:\.(\d{1,3}))?$")   # IT102: Nn 数量，至多 3 位
_UNIT_RE = re.compile(r"^(\d+)(?:\.(\d{1,4}))?$")  # IT104: 单价，至多 4 位
_TDS_RE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$")   # TDS01 显式小数，至多 2 位
_DIGITS_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class _FixedPoint:
    """无符号定点十进制数：value = mantissa / 10**scale，全程用整数表示。"""

    mantissa: int
    scale: int


def _parse_amount(raw: str, pattern: re.Pattern[str], code: str, tag: str,
                  element: str, seg_idx: int) -> _FixedPoint:
    m = pattern.fullmatch(raw)
    if not m:
        raise _err(code, f"{element} 不是合法的金额/数值: {raw!r}", seg_idx, tag)
    whole, frac = m.group(1), m.group(2)
    frac = frac or ""
    mantissa = int(whole + frac) if frac else int(whole)
    return _FixedPoint(mantissa, len(frac))


def _line_to_cents(qty: _FixedPoint, unit: _FixedPoint) -> int:
    """数量 * 单价，按 ROUND_HALF_UP 取整到整数分（0.01）。

    乘积定点标度为 qty.scale + unit.scale（≤ 7）。换算成分时余数做
    非负数意义上的四舍五入（ROUND_HALF_UP）：余数的半个单位向上进一。
    """
    product = qty.mantissa * unit.mantissa
    product_scale = qty.scale + unit.scale
    # 把乘积从 10**-product_scale 换算到 10**-2（分）
    if product_scale <= 2:
        return product * 10 ** (2 - product_scale)
    factor = 10 ** (product_scale - 2)
    return (2 * product + factor) // (2 * factor)


def _tds_to_cents(raw: str, seg_idx: int) -> int:
    """TDS01 -> 整数分。

    X12 TDS01 类型为 N2（隐含两位小数）：无小数点时按 /100 解析（"1234" -> 12.34）；
    出现小数点时按显式金额解析，且小数位至多两位。整数运算，无精度上限。
    """
    if _DIGITS_RE.fullmatch(raw):
        return int(raw)  # N2：数字本身即“分”
    m = _TDS_RE.fullmatch(raw)
    if not m:
        raise _err("TDS01_INVALID", f"TDS01 不是合法金额: {raw!r}", seg_idx, "TDS")
    whole, frac = m.group(1), m.group(2)
    frac = (frac or "").ljust(2, "0")
    return int(whole + frac)


class X12Error(Exception):
    """核验失败；http_status 固定 422，seg 为从 1 开始的段序号（无法定位时为 None）。"""

    def __init__(
        self,
        code: str,
        message: str,
        seg: Optional[int] = None,
        segment: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.seg = seg
        self.segment = segment


@dataclass(frozen=True)
class Line:
    line_id: str          # IT101 分配的行号
    quantity: str         # IT102
    unit_price: str       # IT104
    line_amount: int      # 行金额，整数分


@dataclass(frozen=True)
class Invoice:
    invoice_number: str   # BIG02
    lines: list[Line]
    total_amount: int     # TDS01，整数分（等于各行整数分之和）
    # 以下为收件库（/v1/inbox）所需的信封/原文信息，不影响核验结果本身
    isa06: str = ""                            # ISA06 发送方标识（原文，15 字符定长）
    gs02: str = ""                             # GS02 应用发送方代码（原文）
    big_segment: tuple[str, ...] = ()          # BIG 段全部元素（含段标识）
    it1_segments: tuple[tuple[str, ...], ...] = ()  # 各 IT1 段全部元素（含段标识），按报文顺序


def _err(
    code: str,
    message: str,
    seg: Optional[int] = None,
    segment: Optional[str] = None,
) -> X12Error:
    return X12Error(code=code, message=message, seg=seg, segment=segment)


def _min_len(tag: str, fields: list[str], n: int, seg_idx: int) -> None:
    """段必须至少包含 n 个元素（含段标识本身）。"""
    if len(fields) < n:
        raise _err(
            "SEGMENT_TOO_FEW_ELEMENTS",
            f"{tag} 段至少需要 {n} 个元素，实际 {len(fields)} 个",
            seg_idx,
            tag,
        )


def verify(edi_text: str) -> Invoice:
    """核验一份 X12 810 报文；失败抛 X12Error，成功返回 Invoice。"""

    raw = edi_text.encode("latin-1", errors="replace")  # 调用方已完成 ASCII 校验
    if not raw:
        raise _err("EMPTY_INPUT", "edi_text 为空")
    if not raw.startswith(b"ISA"):
        raise _err("ISA_NOT_FIRST", "报文必须以 ISA 段开头")
    if len(raw) < 106:
        raise _err("ISA_TOO_SHORT",
                   f"ISA 段必须为固定 106 字节，实际 {len(raw)} 字节")

    element_sep = chr(raw[3])
    component_sep = chr(raw[104])
    segment_term = chr(raw[105])

    # ---- 分隔符合法性 -------------------------------------------------
    if element_sep.isalnum():
        raise _err("INVALID_ELEMENT_SEPARATOR",
                   f"元素分隔符 {element_sep!r} 不能为字母或数字")
    if element_sep in (" ", "\r", "\n", "\t"):
        raise _err("INVALID_ELEMENT_SEPARATOR",
                   "元素分隔符不能为空白字符")
    if segment_term.isalnum() or segment_term in (" ", "\t"):
        raise _err("INVALID_SEGMENT_TERMINATOR",
                   f"段终止符 {segment_term!r} 不合法")
    if segment_term == element_sep:
        raise _err("SEPARATOR_CONFLICT", "元素分隔符与段终止符不能相同")
    if component_sep.isalnum() or component_sep in (" ", "\r", "\n", "\t"):
        raise _err("INVALID_COMPONENT_SEPARATOR",
                   f"ISA16 组件分隔符 {component_sep!r} 不合法")
    if component_sep in (element_sep, segment_term):
        raise _err("SEPARATOR_CONFLICT",
                   "组件分隔符不能与元素分隔符或段终止符相同")

    isa_fragment = raw[:105].decode("ascii", errors="replace")
    isa_fields = isa_fragment.split(element_sep)
    if len(isa_fields) != 17:
        raise _err("ISA_MALFORMED",
                   f"ISA 段必须包含 16 个元素分隔符，实际 {len(isa_fields) - 1} 个")
    if any(f == "" for f in isa_fields):
        raise _err("ISA_MALFORMED", "ISA 段不允许空元素")

    if isa_fields[12] != "00401":  # ISA12 互换版本号
        raise _err("ISA12_BAD_VERSION",
                   f'ISA12 必须为 "00401"，实际为 {isa_fields[12]!r}', 1, "ISA")

    isa_ctrl = isa_fields[13]      # ISA13 交换控制号
    isa06 = isa_fields[6]          # ISA06 发送方标识（收件键组成部分）

    # ---- 切段 ---------------------------------------------------------
    remainder = raw[106:].decode("ascii", errors="replace")
    fragments = remainder.split(segment_term)
    # CR/LF 仅在不充当段终止符时，才允许作为段后的排版空白被剥离
    if segment_term not in ("\r", "\n"):
        fragments = [f.strip("\r\n") for f in fragments]

    trailing_data: Optional[str] = None
    if fragments:
        if fragments[-1] == "":
            fragments.pop()           # 正常：以段终止符结尾
        else:
            trailing_data = fragments.pop()  # 终止符之后仍有数据
            # 尾随内容前仍应以终止符正常收尾
            if fragments and fragments[-1] == "":
                fragments.pop()

    segments: list[tuple[int, str, list[str]]] = []  # (段序号, 段标识, 元素)
    for i, fragment in enumerate(fragments, start=2):
        if fragment == "":
            raise _err("EMPTY_SEGMENT", "出现空段", i)
        fields = fragment.split(element_sep)
        tag = fields[0]
        if tag == "":
            raise _err("EMPTY_SEGMENT", "缺少段标识", i)
        segments.append((i, tag, fields))

    # ---- 外壳顺序状态机 -------------------------------------------------
    state = "GS"
    line_ids: set[str] = set()
    lines: list[Line] = []
    total_cents = 0

    gs_idx: Optional[int] = None
    se_idx: Optional[int] = None
    ge_idx: Optional[int] = None
    iea_idx: Optional[int] = None
    gs_ctrl = st_ctrl = se_count = ge_ctrl = None
    ge_count: Optional[int] = None
    iea_count: Optional[int] = None
    invoice_number: Optional[str] = None
    tds_idx: Optional[int] = None
    gs02 = ""
    big_segment: tuple[str, ...] = ()
    it1_segments: list[tuple[str, ...]] = []

    for idx, tag, fields in segments:
        if state == "GS":
            if tag != "GS":
                raise _err("UNEXPECTED_SEGMENT",
                           f"ISA 之后必须是 GS，实际为 {tag}", idx, tag)
            _min_len(tag, fields, 9, idx)
            if fields[8] != "004010":  # GS08
                raise _err("GS08_BAD_VERSION",
                           f'GS08 必须为 "004010"，实际为 {fields[8]!r}',
                           idx, tag)
            gs_ctrl = fields[6]        # GS06
            gs02 = fields[2]           # GS02 应用发送方代码（收件键组成部分）
            gs_idx = idx
            state = "ST"

        elif state == "ST":
            if tag != "ST":
                raise _err("UNEXPECTED_SEGMENT",
                           f"GS 之后必须是 ST，实际为 {tag}", idx, tag)
            _min_len(tag, fields, 3, idx)
            if fields[1] != "810":    # ST01
                raise _err("ST01_NOT_810",
                           f"ST01 必须为 810，实际为 {fields[1]!r}", idx, tag)
            st_ctrl = fields[2]       # ST02
            state = "BIG"

        elif state == "BIG":
            if tag != "BIG":
                raise _err("UNEXPECTED_SEGMENT",
                           f"ST 之后必须是 BIG，实际为 {tag}", idx, tag)
            _min_len(tag, fields, 3, idx)
            if fields[2] == "":       # BIG02 发票号
                raise _err("BIG02_EMPTY", "BIG02 发票号不能为空", idx, tag)
            invoice_number = fields[2]
            big_segment = tuple(fields)
            state = "IT1"

        elif state == "IT1":
            if tag == "IT1":
                _min_len(tag, fields, 5, idx)
                line_id = fields[1]
                if line_id == "":
                    raise _err("IT101_EMPTY", "IT101 行号不能为空", idx, tag)
                if line_id in line_ids:
                    raise _err("IT101_DUPLICATE",
                               f"IT101 行号重复: {line_id!r}", idx, tag)
                line_ids.add(line_id)
                if len(lines) >= MAX_IT1:
                    raise _err("IT1_TOO_MANY",
                               f"IT1 段数量超过上限 {MAX_IT1}", idx, tag)

                qty_raw = fields[2]    # IT102
                unit_raw = fields[4]   # IT104
                qty = _parse_amount(qty_raw, _QTY_RE, "IT102_INVALID",
                                    tag, "IT102 数量", idx)
                if qty.mantissa <= 0:
                    raise _err("IT102_INVALID",
                               f"IT102 数量必须为正数: {qty_raw!r}", idx, tag)
                unit = _parse_amount(unit_raw, _UNIT_RE, "IT104_INVALID",
                                     tag, "IT104 单价", idx)

                line_cents = _line_to_cents(qty, unit)
                total_cents += line_cents
                lines.append(Line(line_id, qty_raw, unit_raw, line_cents))
                it1_segments.append(tuple(fields))

            elif tag == "TDS":
                _min_len(tag, fields, 2, idx)
                if fields[1] == "":
                    raise _err("TDS01_INVALID", "TDS01 不能为空", idx, tag)
                tds_cents = _tds_to_cents(fields[1], idx)
                if tds_cents != total_cents:
                    raise _err(
                        "TDS_MISMATCH",
                        f"TDS01 总额 {tds_cents} 分与各行金额之和 "
                        f"{total_cents} 分不一致",
                        idx, tag,
                    )
                tds_idx = idx
                state = "SE"
            else:
                raise _err("UNEXPECTED_SEGMENT",
                           f"BIG 之后只允许 IT1 或 TDS，实际为 {tag}", idx, tag)

        elif state == "SE":
            if tag != "SE":
                raise _err("UNEXPECTED_SEGMENT",
                           f"TDS 之后必须是 SE，实际为 {tag}", idx, tag)
            _min_len(tag, fields, 3, idx)
            if not _DIGITS_RE.fullmatch(fields[1]):
                raise _err("SE01_INVALID",
                           f"SE01 必须为非负整数: {fields[1]!r}", idx, tag)
            se_count = int(fields[1])  # SE01
            se_ctrl = fields[2]        # SE02
            if se_ctrl != st_ctrl:
                raise _err("ST_SE_CONTROL_MISMATCH",
                           f"ST02 {st_ctrl!r} 与 SE02 {se_ctrl!r} 不一致",
                           idx, tag)
            se_idx = idx
            state = "GE"

        elif state == "GE":
            if tag != "GE":
                raise _err("UNEXPECTED_SEGMENT",
                           f"SE 之后必须是 GE，实际为 {tag}", idx, tag)
            _min_len(tag, fields, 3, idx)
            if not _DIGITS_RE.fullmatch(fields[1]):
                raise _err("GE01_INVALID",
                           f"GE01 必须为非负整数: {fields[1]!r}", idx, tag)
            ge_count = int(fields[1])  # GE01
            ge_ctrl = fields[2]        # GE02
            if ge_ctrl != gs_ctrl:
                raise _err("GS_GE_CONTROL_MISMATCH",
                           f"GS06 {gs_ctrl!r} 与 GE02 {ge_ctrl!r} 不一致",
                           idx, tag)
            ge_idx = idx
            state = "IEA"

        elif state == "IEA":
            if tag != "IEA":
                raise _err("UNEXPECTED_SEGMENT",
                           f"GE 之后必须是 IEA，实际为 {tag}", idx, tag)
            _min_len(tag, fields, 3, idx)
            if not _DIGITS_RE.fullmatch(fields[1]):
                raise _err("IEA01_INVALID",
                           f"IEA01 必须为非负整数: {fields[1]!r}", idx, tag)
            iea_count = int(fields[1])  # IEA01
            iea_ctrl = fields[2]        # IEA02
            if iea_ctrl != isa_ctrl:
                raise _err("ISA_IEA_CONTROL_MISMATCH",
                           f"ISA13 {isa_ctrl!r} 与 IEA02 {iea_ctrl!r} 不一致",
                           idx, tag)
            iea_idx = idx
            state = "DONE"

        else:  # DONE
            raise _err("UNEXPECTED_SEGMENT",
                       f"IEA 之后不允许再出现段: {tag}", idx, tag)

    # ---- 收尾交叉校验 ---------------------------------------------------
    if state != "DONE":
        missing_names = {
            "GS": "GS",
            "ST": "ST",
            "BIG": "BIG",
            "IT1": "IT1/TDS",
            "SE": "SE",
            "GE": "GE",
            "IEA": "IEA",
        }
        raise _err("MISSING_SEGMENT",
                   f"报文缺少 {missing_names[state]} 段或其后继段，未正常闭合")

    if trailing_data is not None:
        raise _err("TRAILING_DATA",
                   f"IEA 段终止符之后存在多余数据: {trailing_data[:20]!r}",
                   len(segments) + 2)

    if ge_count != 1:
        raise _err("GE01_NOT_ONE", f"GE01 必须为 1，实际为 {ge_count}",
                   ge_idx, "GE")
    if iea_count != 1:
        raise _err("IEA01_NOT_ONE", f"IEA01 必须为 1，实际为 {iea_count}",
                   iea_idx, "IEA")

    # SE01 = ST..SE 的段数（含 ST 与 SE）
    st_idx = 3  # ISA=1, GS=2, ST=3
    expected_count = se_idx - st_idx + 1
    if se_count != expected_count:
        raise _err("SE01_WRONG_COUNT",
                   f"SE01 声明 {se_count} 段，ST..SE 实际为 {expected_count} 段",
                   se_idx, "SE")

    if not lines:
        # 报文在状态 IT1 直接到达 TDS（即 0 个 IT1 段）时会走到这里。
        raise _err("IT1_MISSING", "至少需要 1 个 IT1 段", tds_idx, "TDS")

    assert invoice_number is not None and tds_idx is not None
    return Invoice(invoice_number=invoice_number,
                   lines=lines, total_amount=total_cents,
                   isa06=isa06, gs02=gs02, big_segment=big_segment,
                   it1_segments=tuple(it1_segments))


def verify_text(edi_text: str) -> Invoice:
    """/v1/verify 的完整核验入口：ASCII/大小预检 + 报文核验。

    与 HTTP 层共享同一份实现，/v1/reconcile 逐张核验发票时行为完全一致。
    """
    raw_bytes = edi_text.encode("utf-8")
    if not raw_bytes:
        raise _err("EMPTY_INPUT", "edi_text 为空")
    try:
        raw_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise _err("NOT_ASCII", "edi_text 必须为纯 ASCII")
    if len(raw_bytes) > MAX_BYTES:
        raise _err(
            "PAYLOAD_TOO_LARGE",
            f"edi_text 大小 {len(raw_bytes)} 字节超过 {MAX_BYTES} 字节上限",
        )
    return verify(edi_text)
