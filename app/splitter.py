"""ASC X12 004010 X12 810 批量交换报文拆分（POST /v1/split-810 核心逻辑）。

入口：split_batch(edi_text) -> list[SplitInvoice]。

接收**单份 ISA/IEA、单组 GS/GE、内含 1～50 个连续 ST…SE 810 交易集**的
交换报文（纯 ASCII，不超过 1 MiB）。分隔符按 ISA 固定位置读取，规则与
verifier 完全一致（字节偏移 3 为元素分隔符、104 为组件分隔符、105 为段
终止符；ISA 固定 106 字节）。

批外壳校验：
  * ISA12 == "00401"；ISA 之后恰为一个 GS（GS08 == "004010"），
    GS 之后为连续的 1～50 个 ST…SE 交易集，随后恰为 GE、IEA；
  * 控制号配对：ISA13 == IEA02、GS06 == GE02、各 ST02 == SE02；
  * GE01 等于实际交易集数量；IEA01 == 1；
  * 各交易集 ST01 == "810"、ST02 非空且在本批内不重复；
    ST 与 SE 之间不允许再出现信封段（ISA/GS/ST/GE/IEA）；
  * 报文以段终止符正常收尾，不允许空段或 IEA 之后的多余段/数据。

为每个交易集**保留原业务段（ST…SE 原文片段）与原控制号**，重建为
ISA → GS → ST…SE（原文）→ GE(GE01=1, GE02=GS06) → IEA(IEA01=1,
IEA02=ISA13) 的单张报文，并调用与 /v1/verify 完全相同的 verify_text
逐张核验（同时保证每份为纯 ASCII 且 ≤64 KiB）。

任一条失败均整批抛 SplitError（HTTP 422），错误码复用 /v1/verify 的
核验错误码（批外壳专属错误使用 SPLIT_*/GE01_COUNT_MISMATCH），并指出
原报文段序号 seg（ISA = 1）、交易集序号 transaction_set（从 1 开始；
ISA/GS/GE/IEA 等信封级错误为 None）；不返回任何部分结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .verifier import MAX_BYTES, X12Error, verify_text

MAX_BATCH_BYTES = 1024 * 1024   # 批量交换报文上限：1 MiB
MAX_TRANSACTION_SETS = 50

_DIGITS = "0123456789"

# ST…SE 交易集内不允许再出现的信封段标识（SE 为收尾段，不在此列）
_ENVELOPE_TAGS = frozenset({"ISA", "GS", "ST", "GE", "IEA"})


class SplitError(Exception):
    """批量报文拆分/核验失败；HTTP 固定 422。

    seg 为原报文中从 1 开始的段序号（无法定位时为 None）；
    transaction_set 为批内交易集序号（从 1 开始，信封级错误为 None）。
    """

    def __init__(self, code: str, message: str,
                 seg: Optional[int] = None, segment: Optional[str] = None,
                 transaction_set: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.seg = seg
        self.segment = segment
        self.transaction_set = transaction_set


@dataclass(frozen=True)
class SplitInvoice:
    index: int             # 批内序号（从 0 开始）
    st02: str              # ST02 交易集控制号（原文保留）
    invoice_number: str    # BIG02 发票号
    edi_text: str          # 重建的单张报文（可通过 /v1/verify，≤64 KiB）


@dataclass(frozen=True)
class _ParsedSeg:
    idx: int           # 原报文中从 1 开始的段序号（ISA = 1）
    tag: str
    fields: list[str]
    fragment: str      # 去掉段终止符（及其后 CR/LF 排版空白）的原文片段


@dataclass(frozen=True)
class _TransactionSet:
    st_idx: int
    se_idx: int
    st02: str
    fragments: tuple[str, ...]   # ST…SE（含两端）的原文片段，按原顺序


def _err(code: str, message: str,
         seg: Optional[int] = None, segment: Optional[str] = None,
         transaction_set: Optional[int] = None) -> SplitError:
    return SplitError(code, message, seg, segment, transaction_set)


def _min_elements(tag: str, fields: list[str], n: int,
                  seg_idx: int,
                  transaction_set: Optional[int] = None) -> None:
    """段必须至少包含 n 个元素（含段标识本身）。"""
    if len(fields) < n:
        raise _err(
            "SEGMENT_TOO_FEW_ELEMENTS",
            f"{tag} 段至少需要 {n} 个元素，实际 {len(fields)} 个",
            seg_idx, tag, transaction_set)


def _segmentize(raw: bytes, element_sep: str,
                segment_term: str) -> list[_ParsedSeg]:
    """与 verifier 完全相同的切段/尾随数据规则，额外保留各段原文片段。"""
    remainder = raw[106:].decode("ascii")
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
            if fragments and fragments[-1] == "":
                fragments.pop()

    segments: list[_ParsedSeg] = []
    for i, fragment in enumerate(fragments, start=2):  # ISA 占段序号 1
        if fragment == "":
            raise _err("EMPTY_SEGMENT", "出现空段", i)
        fields = fragment.split(element_sep)
        if fields[0] == "":
            raise _err("EMPTY_SEGMENT", "缺少段标识", i)
        segments.append(_ParsedSeg(i, fields[0], fields, fragment))

    if trailing_data is not None:
        raise _err("TRAILING_DATA",
                   f"IEA 段终止符之后存在多余数据: {trailing_data[:20]!r}",
                   len(segments) + 2)
    return segments


def _map_seg(seg: Optional[int], st_idx: int, se_idx: int) -> Optional[int]:
    """把重建单张报文的段序号映射回原批量报文的段序号。

    重建报文：ISA(1) → GS(2) → ST…SE(3..) → GE → IEA。
    ISA/GS 在两份报文中同为段 1/2；ST 起逐段一一对应（重建 ST=3 对应
    原报文 st_idx）；重建的 GE/IEA 为重新生成、理论上不会核验失败。
    """
    if seg is None:
        return None
    if seg in (1, 2):
        return seg
    rebuilt_se = se_idx - st_idx + 3
    if 3 <= seg <= rebuilt_se + 2:
        return seg - 3 + st_idx
    return None


def split_batch(edi_text: str) -> list[SplitInvoice]:
    """拆分并逐张核验批量 810 交换报文；任一失败抛 SplitError（整批 422）。"""
    raw = edi_text.encode("utf-8")
    if not raw:
        raise _err("EMPTY_INPUT", "edi_text 为空")
    try:
        raw.decode("ascii")
    except UnicodeDecodeError:
        raise _err("NOT_ASCII", "edi_text 必须为纯 ASCII")
    if len(raw) > MAX_BATCH_BYTES:
        raise _err(
            "PAYLOAD_TOO_LARGE",
            f"edi_text 大小 {len(raw)} 字节超过 {MAX_BATCH_BYTES} 字节上限")

    # ---- ISA：与 verifier 完全相同的固定位置分隔符/结构规则 -------------
    if not raw.startswith(b"ISA"):
        raise _err("ISA_NOT_FIRST", "报文必须以 ISA 段开头")
    if len(raw) < 106:
        raise _err("ISA_TOO_SHORT",
                   f"ISA 段必须为固定 106 字节，实际 {len(raw)} 字节")

    element_sep = chr(raw[3])
    component_sep = chr(raw[104])
    segment_term = chr(raw[105])

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

    isa_fields = raw[:105].decode("ascii").split(element_sep)
    if len(isa_fields) != 17:
        raise _err("ISA_MALFORMED",
                   f"ISA 段必须包含 16 个元素分隔符，"
                   f"实际 {len(isa_fields) - 1} 个")
    if any(f == "" for f in isa_fields):
        raise _err("ISA_MALFORMED", "ISA 段不允许空元素")
    if isa_fields[12] != "00401":  # ISA12
        raise _err("ISA12_BAD_VERSION",
                   f'ISA12 必须为 "00401"，实际为 {isa_fields[12]!r}',
                   1, "ISA")

    isa_ctrl = isa_fields[13]       # ISA13
    isa_segment = raw[:106].decode("ascii")  # 含段终止符，重建时原样复用

    segments = _segmentize(raw, element_sep, segment_term)
    cursor = 0

    # ---- GS：单功能组 ---------------------------------------------------
    if cursor >= len(segments):
        raise _err("MISSING_SEGMENT", "报文缺少 GS 段，未正常闭合")
    gs = segments[cursor]
    if gs.tag != "GS":
        raise _err("UNEXPECTED_SEGMENT",
                   f"ISA 之后必须是 GS，实际为 {gs.tag}", gs.idx, gs.tag)
    _min_elements("GS", gs.fields, 9, gs.idx)
    if gs.fields[8] != "004010":  # GS08
        raise _err("GS08_BAD_VERSION",
                   f'GS08 必须为 "004010"，实际为 {gs.fields[8]!r}',
                   gs.idx, "GS")
    gs_ctrl = gs.fields[6]         # GS06
    cursor += 1

    # ---- 连续的 ST…SE 交易集（1～50 个）--------------------------------
    sets: list[_TransactionSet] = []
    seen_st02: set[str] = set()
    while cursor < len(segments) and segments[cursor].tag == "ST":
        ts_no = len(sets) + 1
        if ts_no > MAX_TRANSACTION_SETS:
            raise _err("SPLIT_TOO_MANY_TRANSACTION_SETS",
                       f"交易集数量超过上限 {MAX_TRANSACTION_SETS}",
                       segments[cursor].idx, "ST", ts_no)

        st = segments[cursor]
        _min_elements("ST", st.fields, 3, st.idx, ts_no)
        if st.fields[1] != "810":  # ST01
            raise _err("ST01_NOT_810",
                       f"ST01 必须为 810，实际为 {st.fields[1]!r}",
                       st.idx, "ST", ts_no)
        st_ctrl = st.fields[2]     # ST02
        if st_ctrl == "":
            raise _err("ST02_EMPTY", "ST02 交易集控制号不能为空",
                       st.idx, "ST", ts_no)
        if st_ctrl in seen_st02:
            raise _err("ST02_DUPLICATE",
                       f"ST02 交易集控制号重复: {st_ctrl!r}",
                       st.idx, "ST", ts_no)
        seen_st02.add(st_ctrl)
        cursor += 1

        # 交易集正文：直到本交易集的 SE；其间不允许信封段
        body_start = cursor
        while True:
            if cursor >= len(segments):
                raise _err("MISSING_SEGMENT",
                           f"第 {ts_no} 个交易集缺少 SE 段，未正常闭合",
                           None, None, ts_no)
            current = segments[cursor]
            if current.tag == "SE":
                break
            if current.tag in _ENVELOPE_TAGS:
                if current.tag in ("GE", "IEA"):
                    # 交易集未以 SE 收尾就遇到组/交换收尾段
                    raise _err("MISSING_SEGMENT",
                               f"第 {ts_no} 个交易集缺少 SE 段，未正常闭合",
                               current.idx, current.tag, ts_no)
                raise _err(
                    "UNEXPECTED_SEGMENT",
                    f"第 {ts_no} 个交易集的 ST…SE 之间不允许出现信封段 "
                    f"{current.tag}", current.idx, current.tag, ts_no)
            cursor += 1

        se = segments[cursor]
        _min_elements("SE", se.fields, 3, se.idx, ts_no)
        se_ctrl = se.fields[2]     # SE02
        if se_ctrl != st_ctrl:
            raise _err("ST_SE_CONTROL_MISMATCH",
                       f"ST02 {st_ctrl!r} 与 SE02 {se_ctrl!r} 不一致",
                       se.idx, "SE", ts_no)

        fragments = tuple(s.fragment
                          for s in segments[body_start - 1:cursor + 1])
        sets.append(_TransactionSet(st.idx, se.idx, st_ctrl, fragments))
        cursor += 1

    if not sets:
        # GS 之后直接是 GE/IEA/其他段，或报文就此结束
        at = segments[cursor] if cursor < len(segments) else None
        raise _err("SPLIT_NO_TRANSACTION_SETS",
                   "GS 与 GE 之间至少需要 1 个 ST…SE 810 交易集",
                   at.idx if at else None, at.tag if at else None)

    # ---- GE：单组收尾，GE01 等于交易集数，GE02=GS06 ---------------------
    if cursor >= len(segments):
        raise _err("MISSING_SEGMENT", "报文缺少 GE 段，未正常闭合")
    ge = segments[cursor]
    if ge.tag != "GE":
        raise _err("UNEXPECTED_SEGMENT",
                   f"最后一个 SE 之后必须是 GE，实际为 {ge.tag}",
                   ge.idx, ge.tag)
    _min_elements("GE", ge.fields, 3, ge.idx)
    if not all(ch in _DIGITS for ch in ge.fields[1]):
        raise _err("GE01_INVALID",
                   f"GE01 必须为非负整数: {ge.fields[1]!r}", ge.idx, "GE")
    ge_count = int(ge.fields[1])
    if ge_count != len(sets):
        raise _err("GE01_COUNT_MISMATCH",
                   f"GE01 声明 {ge_count} 个交易集，实际为 {len(sets)} 个",
                   ge.idx, "GE")
    if ge.fields[2] != gs_ctrl:
        raise _err("GS_GE_CONTROL_MISMATCH",
                   f"GS06 {gs_ctrl!r} 与 GE02 {ge.fields[2]!r} 不一致",
                   ge.idx, "GE")
    cursor += 1

    # ---- IEA：单交换收尾，IEA01=1，IEA02=ISA13 --------------------------
    if cursor >= len(segments):
        raise _err("MISSING_SEGMENT", "报文缺少 IEA 段，未正常闭合")
    iea = segments[cursor]
    if iea.tag != "IEA":
        raise _err("UNEXPECTED_SEGMENT",
                   f"GE 之后必须是 IEA，实际为 {iea.tag}", iea.idx, iea.tag)
    _min_elements("IEA", iea.fields, 3, iea.idx)
    if not all(ch in _DIGITS for ch in iea.fields[1]):
        raise _err("IEA01_INVALID",
                   f"IEA01 必须为非负整数: {iea.fields[1]!r}",
                   iea.idx, "IEA")
    if int(iea.fields[1]) != 1:
        raise _err("IEA01_NOT_ONE",
                   f"IEA01 必须为 1，实际为 {iea.fields[1]}",
                   iea.idx, "IEA")
    if iea.fields[2] != isa_ctrl:
        raise _err("ISA_IEA_CONTROL_MISMATCH",
                   f"ISA13 {isa_ctrl!r} 与 IEA02 {iea.fields[2]!r} 不一致",
                   iea.idx, "IEA")
    cursor += 1

    if cursor < len(segments):
        extra = segments[cursor]
        raise _err("UNEXPECTED_SEGMENT",
                   f"IEA 之后不允许再出现段: {extra.tag}",
                   extra.idx, extra.tag)

    # ---- 逐张重建并用 /v1/verify 同一规则核验 ----------------------------
    e = element_sep
    t = segment_term
    gs_prefix = gs.fragment + t
    ge_fragment = e.join(["GE", "1", gs_ctrl]) + t
    iea_fragment = e.join(["IEA", "1", isa_ctrl]) + t

    results: list[SplitInvoice] = []
    for pos, ts in enumerate(sets):
        ts_no = pos + 1
        rebuilt = (
            isa_segment
            + gs_prefix
            + "".join(fragment + t for fragment in ts.fragments)
            + ge_fragment
            + iea_fragment
        )
        try:
            invoice = verify_text(rebuilt)
        except X12Error as exc:
            # 大小上限（>64 KiB）等无段错误 seg 为 None，同样归因到该交易集
            raise _err(
                exc.code, exc.message,
                _map_seg(exc.seg, ts.st_idx, ts.se_idx),
                exc.segment, ts_no) from exc

        # 重建报文必然为 ASCII（输入已校验）；大小由 verify_text 按 64 KiB 核验
        assert len(rebuilt.encode("ascii")) <= MAX_BYTES
        results.append(SplitInvoice(
            index=pos, st02=ts.st02,
            invoice_number=invoice.invoice_number, edi_text=rebuilt))

    return results
