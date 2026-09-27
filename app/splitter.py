"""单交换 / 单功能组内含多份 810 交易集的批量拆分。

入口：split_text(edi_text) -> list[SplitInvoice]。

接收一份 ASCII、不超过 1 MiB 的 ASC X12 004010 交换报文，结构固定为：

    ISA -> GS -> (ST -> ... -> SE) × 1..50 -> GE -> IEA

分隔符按 ISA 固定位置读取（规则与 verifier 完全相同：字节偏移 3 为元素
分隔符、偏移 104 为组件分隔符、偏移 105 为段终止符）。

批量外壳校验（失败抛 SplitError，整批 422，不返回部分结果）：
  * ISA12 == "00401"，GS08 == "004010"；
  * 控制号配对：ISA13 == IEA02、GS06 == GE02，各 ST02 == 对应 SE02；
  * GE01 等于交易集数（1～50），IEA01 == 1；
  * 各 ST01 == "810"，ST02 非空且在本批内不重复。

每个交易集保留原业务段（ST 与 SE 之间的各段原文，含两端）与原控制号
（ISA13/GS06/ST02 等），仅把 SE01、GE01 重写为正确的单张计数，重建为
GE01=1 的单张报文，并用与 /v1/verify 完全相同的规则（verify_text）逐张
核验。任一张核验失败同样整批 422，错误定位回原报文：seg 为原报文内从
1 开始的段序号，set_index 为交易集序号（从 0 开始；交换/组级错误为
null）。重建的每张报文仍须 ≤ 64 KiB。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .verifier import MAX_BYTES, X12Error, verify_text

MAX_SPLIT_BYTES = 1024 * 1024  # 批量报文上限：1 MiB
MAX_TRANSACTIONS = 50
MIN_TRANSACTIONS = 1

_DIGITS_RE = re.compile(r"^\d+$")


class SplitError(Exception):
    """批量拆分/单张核验失败；HTTP 固定 422，整批不返回部分结果。

    seg 为原报文内从 1 开始的段序号（无法定位段时为 None）；
    set_index 为交易集序号（从 0 开始；交换/组级错误为 None）。
    """

    def __init__(
        self,
        code: str,
        message: str,
        seg: Optional[int] = None,
        segment: Optional[str] = None,
        set_index: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.seg = seg
        self.segment = segment
        self.set_index = set_index


@dataclass(frozen=True)
class SplitInvoice:
    set_index: int          # 交易集在原报文中的序号（从 0 开始）
    st02: str               # ST02 交易集控制号（原文）
    invoice_number: str     # BIG02 发票号
    edi_text: str           # 重建的单张 810 报文（可由 /v1/verify 接收）


@dataclass(frozen=True)
class _ParsedSet:
    """状态机解析出的一个交易集（ST..SE 原文片段）。"""

    index: int
    st_idx: int             # ST 段在原报文中的段序号（ISA = 1）
    st02: str
    fragments: list[str]    # ST..SE 各段的原文（不含段终止符），含 ST 与 SE


def _err(
    code: str,
    message: str,
    seg: Optional[int] = None,
    segment: Optional[str] = None,
    set_index: Optional[int] = None,
) -> SplitError:
    return SplitError(code=code, message=message, seg=seg,
                      segment=segment, set_index=set_index)


def split_text(edi_text: str) -> list[SplitInvoice]:
    """拆分一份含 1～50 个 810 交易集的交换报文并逐张核验。

    任一外壳或业务核验失败抛 SplitError；成功按原顺序返回各交易集的
    ST02、发票号与重建的单张报文。
    """
    # ---- ASCII / 1 MiB 预检（与 verify_text 同构，上限放宽到 1 MiB）----
    raw_bytes = edi_text.encode("utf-8")
    if not raw_bytes:
        raise _err("EMPTY_INPUT", "edi_text 为空")
    try:
        raw_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise _err("NOT_ASCII", "edi_text 必须为纯 ASCII")
    if len(raw_bytes) > MAX_SPLIT_BYTES:
        raise _err(
            "PAYLOAD_TOO_LARGE",
            f"edi_text 大小 {len(raw_bytes)} 字节超过 {MAX_SPLIT_BYTES} 字节上限")

    # ---- ISA：分隔符与 verifier 完全相同 ------------------------------
    if not raw_bytes.startswith(b"ISA"):
        raise _err("ISA_NOT_FIRST", "报文必须以 ISA 段开头")
    if len(raw_bytes) < 106:
        raise _err("ISA_TOO_SHORT",
                   f"ISA 段必须为固定 106 字节，实际 {len(raw_bytes)} 字节")

    element_sep = chr(raw_bytes[3])
    component_sep = chr(raw_bytes[104])
    segment_term = chr(raw_bytes[105])

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

    isa_fragment = raw_bytes[:105].decode("ascii")
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

    # ---- 切段：与 verifier 完全相同的尾随/空段处理 --------------------
    remainder = raw_bytes[106:].decode("ascii")
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

    segments: list[tuple[int, str, list[str], str]] = []  # (段序号, 段标识, 元素, 原文)
    for i, fragment in enumerate(fragments, start=2):
        if fragment == "":
            raise _err("EMPTY_SEGMENT", "出现空段", i)
        fields = fragment.split(element_sep)
        tag = fields[0]
        if tag == "":
            raise _err("EMPTY_SEGMENT", "缺少段标识", i)
        segments.append((i, tag, fields, fragment))

    # ---- 批量外壳状态机 -------------------------------------------------
    state = "GS"
    gs_ctrl: Optional[str] = None
    ge_idx: Optional[int] = None
    iea_idx: Optional[int] = None
    ge_count: Optional[int] = None
    iea_count: Optional[int] = None

    parsed_sets: list[_ParsedSet] = []
    seen_st02: set[str] = set()
    cur_fragments: Optional[list[str]] = None
    cur_st_idx: Optional[int] = None
    cur_st02: Optional[str] = None

    for idx, tag, fields, fragment in segments:
        if state == "GS":
            if tag != "GS":
                raise _err("UNEXPECTED_SEGMENT",
                           f"ISA 之后必须是 GS，实际为 {tag}", idx, tag)
            if len(fields) < 9:
                raise _err("SEGMENT_TOO_FEW_ELEMENTS",
                           f"GS 段至少需要 9 个元素，实际 {len(fields)} 个",
                           idx, tag)
            if fields[8] != "004010":  # GS08
                raise _err("GS08_BAD_VERSION",
                           f'GS08 必须为 "004010"，实际为 {fields[8]!r}',
                           idx, tag)
            gs_ctrl = fields[6]        # GS06
            state = "ST"

        elif state == "ST":
            # 允许的后继：ST（开始交易集）或 GE（空功能组，非法）
            if tag == "GE":
                raise _err("TS_MISSING",
                           "功能组内至少需要 1 个 ST 交易集，实际为 0 个",
                           idx, tag)
            if tag != "ST":
                raise _err("UNEXPECTED_SEGMENT",
                           f"GS 之后必须是 ST，实际为 {tag}", idx, tag)
            if len(fields) < 3:
                raise _err("SEGMENT_TOO_FEW_ELEMENTS",
                           f"ST 段至少需要 3 个元素，实际 {len(fields)} 个",
                           idx, tag, 0)
            if fields[1] != "810":    # ST01
                raise _err("ST01_NOT_810",
                           f"ST01 必须为 810，实际为 {fields[1]!r}",
                           idx, tag, len(parsed_sets))
            st_ctrl = fields[2]       # ST02
            if st_ctrl == "":
                raise _err("ST02_EMPTY", "ST02 交易集控制号不能为空",
                           idx, tag, len(parsed_sets))
            if st_ctrl in seen_st02:
                raise _err("ST02_DUPLICATE",
                           f"ST02 交易集控制号重复: {st_ctrl!r}",
                           idx, tag, len(parsed_sets))
            if len(parsed_sets) >= MAX_TRANSACTIONS:
                raise _err(
                    "TS_TOO_MANY",
                    f"交易集数量超过上限 {MAX_TRANSACTIONS} 个", idx, tag,
                    len(parsed_sets))
            seen_st02.add(st_ctrl)
            cur_fragments = [fragment]
            cur_st_idx = idx
            cur_st02 = st_ctrl
            state = "BODY"

        elif state == "BODY":
            # 交易集正文（ST 与 SE 之间）不允许再出现信封段
            if tag in ("ISA", "GS", "ST", "GE", "IEA"):
                raise _err(
                    "UNEXPECTED_SEGMENT",
                    f"交易集 {cur_st02!r} 的 ST 与 SE 之间不允许出现 {tag} 段",
                    idx, tag, len(parsed_sets))
            assert cur_fragments is not None
            cur_fragments.append(fragment)
            if tag == "SE":
                if len(fields) < 3:
                    raise _err("SEGMENT_TOO_FEW_ELEMENTS",
                               f"SE 段至少需要 3 个元素，实际 {len(fields)} 个",
                               idx, tag, len(parsed_sets))
                if not _DIGITS_RE.fullmatch(fields[1]):
                    raise _err("SE01_INVALID",
                               f"SE01 必须为非负整数: {fields[1]!r}",
                               idx, tag, len(parsed_sets))
                se_ctrl = fields[2]    # SE02
                if se_ctrl != cur_st02:
                    raise _err(
                        "ST_SE_CONTROL_MISMATCH",
                        f"ST02 {cur_st02!r} 与 SE02 {se_ctrl!r} 不一致",
                        idx, tag, len(parsed_sets))
                parsed_sets.append(_ParsedSet(
                    index=len(parsed_sets),
                    st_idx=cur_st_idx,
                    st02=cur_st02,
                    fragments=cur_fragments,
                ))
                cur_fragments = None
                cur_st_idx = None
                cur_st02 = None
                state = "AFTER_SE"

        elif state == "AFTER_SE":
            if tag == "ST":
                if len(fields) < 3:
                    raise _err("SEGMENT_TOO_FEW_ELEMENTS",
                               f"ST 段至少需要 3 个元素，实际 {len(fields)} 个",
                               idx, tag, len(parsed_sets))
                if fields[1] != "810":  # ST01
                    raise _err("ST01_NOT_810",
                               f"ST01 必须为 810，实际为 {fields[1]!r}",
                               idx, tag, len(parsed_sets))
                st_ctrl = fields[2]
                if st_ctrl == "":
                    raise _err("ST02_EMPTY", "ST02 交易集控制号不能为空",
                               idx, tag, len(parsed_sets))
                if st_ctrl in seen_st02:
                    raise _err("ST02_DUPLICATE",
                               f"ST02 交易集控制号重复: {st_ctrl!r}",
                               idx, tag, len(parsed_sets))
                if len(parsed_sets) >= MAX_TRANSACTIONS:
                    raise _err(
                        "TS_TOO_MANY",
                        f"交易集数量超过上限 {MAX_TRANSACTIONS} 个", idx, tag,
                        len(parsed_sets))
                seen_st02.add(st_ctrl)
                cur_fragments = [fragment]
                cur_st_idx = idx
                cur_st02 = st_ctrl
                state = "BODY"
            elif tag == "GE":
                if len(fields) < 3:
                    raise _err("SEGMENT_TOO_FEW_ELEMENTS",
                               f"GE 段至少需要 3 个元素，实际 {len(fields)} 个",
                               idx, tag)
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
            else:
                raise _err("UNEXPECTED_SEGMENT",
                           f"SE 之后必须是 ST 或 GE，实际为 {tag}", idx, tag,
                           len(parsed_sets) - 1)

        elif state == "IEA":
            if tag != "IEA":
                raise _err("UNEXPECTED_SEGMENT",
                           f"GE 之后必须是 IEA，实际为 {tag}", idx, tag)
            if len(fields) < 3:
                raise _err("SEGMENT_TOO_FEW_ELEMENTS",
                           f"IEA 段至少需要 3 个元素，实际 {len(fields)} 个",
                           idx, tag)
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
    if state == "GS":
        # ISA 之后直接结束：缺少 GS
        raise _err("MISSING_SEGMENT", "报文缺少 GS 段或其后继段，未正常闭合")
    if state == "ST":
        # GS 之后直接结束（无 ST、无 GE）：缺少交易集
        raise _err("MISSING_SEGMENT", "报文缺少 ST 交易集或其后继段，未正常闭合")
    if state == "BODY":
        raise _err("MISSING_SEGMENT",
                   f"交易集 {cur_st02!r} 缺少 SE 段或未正常闭合",
                   cur_st_idx, "SE", len(parsed_sets))
    if state in ("AFTER_SE", "IEA"):
        missing = "GE/IEA" if state == "AFTER_SE" else "IEA"
        raise _err("MISSING_SEGMENT",
                   f"报文缺少 {missing} 段或其后继段，未正常闭合")

    assert ge_idx is not None and iea_idx is not None
    assert ge_count is not None and iea_count is not None

    if trailing_data is not None:
        raise _err("TRAILING_DATA",
                   f"IEA 段终止符之后存在多余数据: {trailing_data[:20]!r}",
                   len(segments) + 2)

    if len(parsed_sets) < MIN_TRANSACTIONS:
        # 状态机中 GE 先于 ST 已拦截 0 交易集；此处为防御性兜底
        raise _err("TS_MISSING",
                   f"功能组内至少需要 {MIN_TRANSACTIONS} 个 ST 交易集，实际为 0 个",
                   ge_idx, "GE")
    if ge_count != len(parsed_sets):
        raise _err(
            "GE01_COUNT_MISMATCH",
            f"GE01 声明 {ge_count} 个交易集，实际为 {len(parsed_sets)} 个",
            ge_idx, "GE")
    if iea_count != 1:
        raise _err("IEA01_NOT_ONE",
                   f"IEA01 必须为 1，实际为 {iea_count}", iea_idx, "IEA")

    # ---- 逐张重建（GE01=1）并用 /v1/verify 规则核验 --------------------
    isa_segment = raw_bytes[:106].decode("ascii")
    gs_fragment = segments[0][3]  # 原 GS 段原文（GS 恒为 ISA 之后第 1 段）

    results: list[SplitInvoice] = []
    for parsed in parsed_sets:
        # 保留原 ST..SE 业务段，仅把 SE01 重写为正确的单张段数
        # （ST..SE 含两端；原批内 SE01 计数不参与校验，以重建结果为准）
        rebuilt_fragments: list[str] = []
        for pos, frag in enumerate(parsed.fragments):
            if pos == len(parsed.fragments) - 1:  # 末段即 SE
                se_fields = frag.split(element_sep)
                se_fields[1] = str(len(parsed.fragments))
                frag = element_sep.join(se_fields)
            rebuilt_fragments.append(frag)

        # 重建为单交换/单组/单交易集：GE01=1，IEA01=1，控制号沿用原文
        rebuilt = (
            isa_segment
            + gs_fragment + segment_term
            + segment_term.join(rebuilt_fragments) + segment_term
            + element_sep.join(("GE", "1", gs_ctrl)) + segment_term
            + element_sep.join(("IEA", "1", isa_ctrl)) + segment_term
        )

        if len(rebuilt.encode("ascii")) > MAX_BYTES:
            raise _err(
                "PAYLOAD_TOO_LARGE",
                f"交易集 {parsed.st02!r} 重建后的单张报文大小 "
                f"{len(rebuilt.encode('ascii'))} 字节超过 {MAX_BYTES} 字节上限",
                None, None, parsed.index)

        try:
            invoice = verify_text(rebuilt)
        except X12Error as exc:
            # 单张报文段序号回映原报文：重建报文 ISA=1、GS=2、ST=3；
            # 原批内该交易集 ST 位于 parsed.st_idx，偏移为 st_idx - 3。
            if exc.seg is not None and exc.seg >= 3:
                orig_seg = exc.seg + (parsed.st_idx - 3)
            else:
                orig_seg = exc.seg  # ISA/GS/交换级错误（seg 1/2/null）原样保留
            raise _err(exc.code, exc.message, orig_seg, exc.segment,
                       parsed.index) from exc

        results.append(SplitInvoice(
            set_index=parsed.index,
            st02=parsed.st02,
            invoice_number=invoice.invoice_number,
            edi_text=rebuilt,
        ))

    return results
