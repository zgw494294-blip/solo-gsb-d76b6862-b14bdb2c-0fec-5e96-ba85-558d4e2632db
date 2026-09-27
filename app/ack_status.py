"""ASC X12 004010 997 Functional Acknowledgment（功能回执）状态解析。

入口：parse_ack_status(edi_text, gs06, st02_list) -> list[ItemStatus]。

请求侧给出本次发送清单：一个组控制号 gs06 与 1～100 个唯一非空的交易集
控制号 st02。对一份 004010 997 回执做严格结构核验：

  * 与 /v1/verify 相同的 ASCII/大小预检与 ISA 分隔符解析
    （106 字节、三种分隔符合法且互不冲突、ISA12 == "00401"）；
  * 限单交换/单功能组/单交易集，信封顺序固定为
    ISA -> GS -> ST -> AK1 -> (AK2 -> AK5)* -> AK9 -> SE -> GE -> IEA，
    即正文为一个 AK1、零至多个 AK2/AK5 对、一个 AK9；
  * GS01 == "FA"、GS08 == "004010"、ST01 == "997"；
  * 控制号配对：ISA13 == IEA02、GS06 == GE02、ST02 == SE02；
    GE01 == IEA01 == 1；SE01 等于 ST..SE 的段数；
  * AK102 必须引用清单 gs06；每个 AK201 必须为 "810"、AK202 必须引用
    清单中的 st02 且不得重复；每个 AK2 必须紧跟一个 AK5，AK501 仅 A/R；
  * AK9 恰好四个计数元素：状态*总数*收到数*接受数。状态仅 A/R/P；
    总数与收到数均必须等于清单长度；A 须接受数等于清单长度（全接受）、
    R 须接受数为 0（全拒绝）、P 须 0 < 接受数 < 清单长度（部分接受）。

未在 AK2 中列出的清单项（未列项）状态由接受数扣除明细后推断：
剩余接受数 == 未列数则未列项全部 A，== 0 则全部 R，其余全部 U（未知）。
计数矛盾（接受数超出明细之和或与 A/R/P 语义冲突、剩余接受数不在
[0, 未列数] 内）或任何结构非法均返回 422，错误体带段序号与错误码。

返回结果严格按清单顺序给出每一项的 A/R/U 状态。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .verifier import MAX_BYTES, X12Error

_DIGITS_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class ItemStatus:
    st02: str
    status: str   # "A" 接受 / "R" 拒绝 / "U" 未知（未列项且接受数未明确归属）


def validate_manifest(gs06: str, st02_list: list[str]) -> None:
    """校验发送清单：gs06 非空；st02 1～100 个、非空且唯一。

    长度上下限由 HTTP 模型（min/max_length）先行保证，此处只查非空与唯一；
    清单非法属于整次 422，错误无法定位到报文段，seg 为 None。
    """
    if gs06 == "":
        raise X12Error("ACK_MANIFEST_GS06_EMPTY", "发送清单 gs06 不能为空")
    seen: set[str] = set()
    for value in st02_list:
        if value == "":
            raise X12Error("ACK_MANIFEST_ST02_EMPTY",
                           "发送清单 st02 不能为空")
        if value in seen:
            raise X12Error("ACK_MANIFEST_ST02_DUPLICATE",
                           f"发送清单 st02 重复: {value!r}")
        seen.add(value)


def parse_ack_status(edi_text: str, gs06: str,
                     st02_list: list[str]) -> list[ItemStatus]:
    """解析 997 回执并按清单顺序返回各项 A/R/U 状态；非法抛 X12Error（422）。"""
    validate_manifest(gs06, st02_list)

    # ---- 与 verifier 相同的预检与 ISA 解析 ------------------------------
    raw_bytes = edi_text.encode("utf-8")
    if not raw_bytes:
        raise X12Error("EMPTY_INPUT", "edi_text 为空")
    try:
        raw_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise X12Error("NOT_ASCII", "edi_text 必须为纯 ASCII")
    if len(raw_bytes) > MAX_BYTES:
        raise X12Error(
            "PAYLOAD_TOO_LARGE",
            f"edi_text 大小 {len(raw_bytes)} 字节超过 {MAX_BYTES} 字节上限")
    raw = raw_bytes

    if not raw.startswith(b"ISA"):
        raise X12Error("ISA_NOT_FIRST", "报文必须以 ISA 段开头")
    if len(raw) < 106:
        raise X12Error("ISA_TOO_SHORT",
                       f"ISA 段必须为固定 106 字节，实际 {len(raw)} 字节")

    element_sep = chr(raw[3])
    component_sep = chr(raw[104])
    segment_term = chr(raw[105])

    if element_sep.isalnum() or element_sep in (" ", "\r", "\n", "\t"):
        raise X12Error("INVALID_ELEMENT_SEPARATOR",
                       f"元素分隔符 {element_sep!r} 不能为字母数字或空白")
    if segment_term.isalnum() or segment_term in (" ", "\t"):
        raise X12Error("INVALID_SEGMENT_TERMINATOR",
                       f"段终止符 {segment_term!r} 不合法")
    if segment_term == element_sep:
        raise X12Error("SEPARATOR_CONFLICT", "元素分隔符与段终止符不能相同")
    if component_sep.isalnum() or component_sep in (" ", "\r", "\n", "\t"):
        raise X12Error("INVALID_COMPONENT_SEPARATOR",
                       f"ISA16 组件分隔符 {component_sep!r} 不合法")
    if component_sep in (element_sep, segment_term):
        raise X12Error("SEPARATOR_CONFLICT",
                       "组件分隔符不能与元素分隔符或段终止符相同")

    isa_fragment = raw[:105].decode("ascii")
    isa_fields = isa_fragment.split(element_sep)
    if len(isa_fields) != 17:
        raise X12Error("ISA_MALFORMED",
                       f"ISA 段必须包含 16 个元素分隔符，实际 "
                       f"{len(isa_fields) - 1} 个")
    if any(f == "" for f in isa_fields):
        raise X12Error("ISA_MALFORMED", "ISA 段不允许空元素")
    if isa_fields[12] != "00401":  # ISA12
        raise X12Error("ISA12_BAD_VERSION",
                       f'ISA12 必须为 "00401"，实际为 {isa_fields[12]!r}',
                       1, "ISA")
    isa_ctrl = isa_fields[13]       # ISA13

    # ---- 切段（与 verifier 相同的尾随/空段处理）-------------------------
    fragments = raw[106:].decode("ascii").split(segment_term)
    if segment_term not in ("\r", "\n"):
        fragments = [f.strip("\r\n") for f in fragments]

    trailing_data: Optional[str] = None
    if fragments:
        if fragments[-1] == "":
            fragments.pop()
        else:
            trailing_data = fragments.pop()
            if fragments and fragments[-1] == "":
                fragments.pop()

    segments: list[tuple[int, str, list[str]]] = []  # (段序号, 段标识, 元素)
    for i, fragment in enumerate(fragments, start=2):
        if fragment == "":
            raise X12Error("EMPTY_SEGMENT", "出现空段", i)
        fields = fragment.split(element_sep)
        if fields[0] == "":
            raise X12Error("EMPTY_SEGMENT", "缺少段标识", i)
        segments.append((i, fields[0], fields))

    def require(pos: int, tag: str) -> tuple[int, list[str]]:
        """取出第 pos 个后随段（segments 从下标 0 开始对应全局段号 2），
        要求存在且段标识恰为 tag。"""
        if pos >= len(segments):
            raise X12Error("MISSING_SEGMENT",
                           f"报文缺少 {tag} 段或其后继段，未正常闭合")
        idx, actual, fields = segments[pos]
        if actual != tag:
            raise X12Error("UNEXPECTED_SEGMENT",
                           f"此处必须为 {tag} 段，实际为 {actual}", idx, actual)
        return idx, fields

    def need_elements(tag: str, fields: list[str], n: int, idx: int) -> None:
        if len(fields) < n:
            raise X12Error(
                "SEGMENT_TOO_FEW_ELEMENTS",
                f"{tag} 段至少需要 {n} 个元素，实际 {len(fields)} 个",
                idx, tag)

    # ---- 信封：GS / ST ---------------------------------------------------
    gs_idx, gs_fields = require(0, "GS")
    need_elements("GS", gs_fields, 9, gs_idx)
    if gs_fields[1] != "FA":  # GS01
        raise X12Error("GS01_NOT_FA",
                       f"GS01 必须为 FA，实际为 {gs_fields[1]!r}",
                       gs_idx, "GS")
    if gs_fields[8] != "004010":  # GS08
        raise X12Error("GS08_BAD_VERSION",
                       f'GS08 必须为 "004010"，实际为 {gs_fields[8]!r}',
                       gs_idx, "GS")
    gs_ctrl = gs_fields[6]  # GS06

    st_idx, st_fields = require(1, "ST")
    need_elements("ST", st_fields, 3, st_idx)
    if st_fields[1] != "997":  # ST01
        raise X12Error("ST01_NOT_997",
                       f"ST01 必须为 997，实际为 {st_fields[1]!r}",
                       st_idx, "ST")
    st_ctrl = st_fields[2]  # ST02

    # ---- 正文：AK1 / (AK2, AK5)* / AK9 -----------------------------------
    ak1_idx, ak1_fields = require(2, "AK1")
    need_elements("AK1", ak1_fields, 3, ak1_idx)
    if len(ak1_fields) != 3:  # AK101/AK102 两个元素，多余元素同样非法
        raise X12Error("AK1_ELEMENT_COUNT",
                       f"AK1 必须包含 2 个元素，实际 {len(ak1_fields) - 1} 个",
                       ak1_idx, "AK1")
    if ak1_fields[1] == "":
        raise X12Error("AK101_EMPTY", "AK101 功能标识符不能为空",
                       ak1_idx, "AK1")
    if ak1_fields[2] != gs06:  # AK102 必须引用清单 gs06
        raise X12Error("AK1_GS06_MISMATCH",
                       f"AK102 {ak1_fields[2]!r} 未引用清单 gs06 {gs06!r}",
                       ak1_idx, "AK1")

    expected = set(st02_list)
    detail: dict[str, str] = {}   # 已列出 st02 -> A/R
    pos = 3
    while pos < len(segments) and segments[pos][1] == "AK2":
        idx2, _, ak2_fields = segments[pos]
        need_elements("AK2", ak2_fields, 3, idx2)
        if len(ak2_fields) != 3:  # AK201/AK202 两个元素，多余元素同样非法
            raise X12Error("AK2_ELEMENT_COUNT",
                           f"AK2 必须包含 2 个元素，实际 "
                           f"{len(ak2_fields) - 1} 个",
                           idx2, "AK2")
        if ak2_fields[1] != "810":  # AK201
            raise X12Error("AK201_NOT_810",
                           f"AK201 必须为 810，实际为 {ak2_fields[1]!r}",
                           idx2, "AK2")
        ctrl = ak2_fields[2]  # AK202
        if ctrl not in expected:
            raise X12Error("AK2_ST02_UNKNOWN",
                           f"AK202 {ctrl!r} 不在发送清单 st02 中",
                           idx2, "AK2")
        if ctrl in detail:
            raise X12Error("AK2_ST02_DUPLICATE",
                           f"AK202 引用的 st02 重复: {ctrl!r}",
                           idx2, "AK2")

        idx5, ak5_fields = require(pos + 1, "AK5")
        need_elements("AK5", ak5_fields, 2, idx5)
        if len(ak5_fields) != 2:  # AK501 一个元素，多余元素同样非法
            raise X12Error("AK5_ELEMENT_COUNT",
                           f"AK5 必须包含 1 个元素，实际 "
                           f"{len(ak5_fields) - 1} 个",
                           idx5, "AK5")
        ak5 = ak5_fields[1]
        if ak5 not in ("A", "R"):
            raise X12Error("AK501_INVALID",
                           f"AK501 必须为 A 或 R，实际为 {ak5!r}",
                           idx5, "AK5")
        detail[ctrl] = ak5
        pos += 2

    # AK2 之后必须恰好是 AK9（AK2 未配对 AK5 的情况由 require 拦截）
    if pos >= len(segments):
        raise X12Error("MISSING_SEGMENT", "报文缺少 AK9 段，未正常闭合")
    ak9_idx, ak9_tag, ak9_fields = segments[pos]
    if ak9_tag != "AK9":
        raise X12Error("UNEXPECTED_SEGMENT",
                       f"AK2/AK5 之后必须为 AK9，实际为 {ak9_tag}",
                       ak9_idx, ak9_tag)
    if len(ak9_fields) != 5:  # AK901..AK904 四个元素，缺一/多一均非法
        raise X12Error("AK9_ELEMENT_COUNT",
                       f"AK9 必须包含 4 个元素（状态*总数*收到数*接受数），"
                       f"实际 {len(ak9_fields) - 1} 个",
                       ak9_idx, "AK9")

    def count_of(element: str, name: str) -> int:
        if not _DIGITS_RE.fullmatch(element):
            raise X12Error("AK9_COUNT_INVALID",
                           f"AK9 {name} 必须为非负整数: {element!r}",
                           ak9_idx, "AK9")
        return int(element)

    ak9_status = ak9_fields[1]
    if ak9_status not in ("A", "R", "P"):
        raise X12Error("AK901_INVALID",
                       f"AK901 状态必须为 A/R/P，实际为 {ak9_status!r}",
                       ak9_idx, "AK9")
    total = count_of(ak9_fields[2], "总数")
    received = count_of(ak9_fields[3], "收到数")
    accepted = count_of(ak9_fields[4], "接受数")

    manifest_len = len(st02_list)
    if total != manifest_len or received != manifest_len:
        raise X12Error(
            "AK9_COUNT_MISMATCH",
            f"AK9 总数 {total}、收到数 {received} 均必须等于清单长度 "
            f"{manifest_len}",
            ak9_idx, "AK9")
    if not 0 <= accepted <= manifest_len:
        raise X12Error("AK9_ACCEPTED_OUT_OF_RANGE",
                       f"AK9 接受数 {accepted} 超出 [0, {manifest_len}] 范围",
                       ak9_idx, "AK9")
    if ak9_status == "A" and accepted != manifest_len:
        raise X12Error("AK9_STATUS_CONTRADICTION",
                       f"AK9 状态为 A 时接受数必须为 {manifest_len}（全接受），"
                       f"实际为 {accepted}",
                       ak9_idx, "AK9")
    if ak9_status == "R" and accepted != 0:
        raise X12Error("AK9_STATUS_CONTRADICTION",
                       f"AK9 状态为 R 时接受数必须为 0（全拒绝），"
                       f"实际为 {accepted}",
                       ak9_idx, "AK9")
    if ak9_status == "P" and not (0 < accepted < manifest_len):
        raise X12Error("AK9_STATUS_CONTRADICTION",
                       f"AK9 状态为 P 时接受数必须在 1～{manifest_len - 1} "
                       f"之间（部分接受），实际为 {accepted}",
                       ak9_idx, "AK9")

    # ---- 明细接受数与 AK9 接受数对账，推断未列项状态 ----------------------
    detail_accepted = sum(1 for s in detail.values() if s == "A")
    unlisted = manifest_len - len(detail)
    residual = accepted - detail_accepted  # 归属到未列项的接受数
    if residual < 0 or residual > unlisted:
        raise X12Error(
            "AK9_ACCEPTED_CONTRADICTION",
            f"AK9 接受数 {accepted} 与明细接受数 {detail_accepted}、"
            f"未列项数 {unlisted} 矛盾",
            ak9_idx, "AK9")
    if unlisted > 0:
        if residual == unlisted:
            unlisted_status = "A"
        elif residual == 0:
            unlisted_status = "R"
        else:
            unlisted_status = "U"
    else:
        unlisted_status = None  # 无未列项，不使用

    # ---- SE / GE / IEA：配对、计数与段数 ---------------------------------
    se_idx, se_fields = require(pos + 1, "SE")
    need_elements("SE", se_fields, 3, se_idx)
    if not _DIGITS_RE.fullmatch(se_fields[1]):
        raise X12Error("SE01_INVALID",
                       f"SE01 必须为非负整数: {se_fields[1]!r}", se_idx, "SE")
    se_count = int(se_fields[1])
    if se_fields[2] != st_ctrl:
        raise X12Error("ST_SE_CONTROL_MISMATCH",
                       f"ST02 {st_ctrl!r} 与 SE02 {se_fields[2]!r} 不一致",
                       se_idx, "SE")
    # ST 在全局段号 3；SE01 = ST..SE（含两端）
    expected_se_count = se_idx - 3 + 1
    if se_count != expected_se_count:
        raise X12Error("SE01_WRONG_COUNT",
                       f"SE01 声明 {se_count} 段，ST..SE 实际为 "
                       f"{expected_se_count} 段",
                       se_idx, "SE")

    ge_idx, ge_fields = require(pos + 2, "GE")
    need_elements("GE", ge_fields, 3, ge_idx)
    if not _DIGITS_RE.fullmatch(ge_fields[1]):
        raise X12Error("GE01_INVALID",
                       f"GE01 必须为非负整数: {ge_fields[1]!r}", ge_idx, "GE")
    if int(ge_fields[1]) != 1:
        raise X12Error("GE01_NOT_ONE",
                       f"GE01 必须为 1，实际为 {ge_fields[1]}", ge_idx, "GE")
    if ge_fields[2] != gs_ctrl:
        raise X12Error("GS_GE_CONTROL_MISMATCH",
                       f"GS06 {gs_ctrl!r} 与 GE02 {ge_fields[2]!r} 不一致",
                       ge_idx, "GE")

    iea_idx, iea_fields = require(pos + 3, "IEA")
    need_elements("IEA", iea_fields, 3, iea_idx)
    if not _DIGITS_RE.fullmatch(iea_fields[1]):
        raise X12Error("IEA01_INVALID",
                       f"IEA01 必须为非负整数: {iea_fields[1]!r}",
                       iea_idx, "IEA")
    if int(iea_fields[1]) != 1:
        raise X12Error("IEA01_NOT_ONE",
                       f"IEA01 必须为 1，实际为 {iea_fields[1]}",
                       iea_idx, "IEA")
    if iea_fields[2] != isa_ctrl:
        raise X12Error("ISA_IEA_CONTROL_MISMATCH",
                       f"ISA13 {isa_ctrl!r} 与 IEA02 {iea_fields[2]!r} 不一致",
                       iea_idx, "IEA")

    if pos + 4 < len(segments):
        extra_idx, extra_tag, _ = segments[pos + 4]
        raise X12Error("UNEXPECTED_SEGMENT",
                       f"IEA 之后不允许再出现段: {extra_tag}",
                       extra_idx, extra_tag)

    if trailing_data is not None:
        raise X12Error("TRAILING_DATA",
                       f"IEA 段终止符之后存在多余数据: {trailing_data[:20]!r}",
                       len(segments) + 2)

    # ---- 按清单顺序输出：明细以 AK5 为准，未列项取推断状态 --------------
    results: list[ItemStatus] = []
    for ctrl in st02_list:
        status = detail.get(ctrl)
        if status is None:
            assert unlisted_status is not None  # 存在未列项时必已推断
            status = unlisted_status
        results.append(ItemStatus(st02=ctrl, status=status))
    return results
