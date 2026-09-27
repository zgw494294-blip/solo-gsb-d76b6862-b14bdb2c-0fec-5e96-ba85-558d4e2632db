"""采购单与多份 X12 810 发票的逐张核销。

每张发票先经过与 /v1/verify 完全相同的核验（verify_text），核验失败仅拒绝
该张并继续；通过核验的发票按 IT101 匹配采购单行、按十进制数值精确比较
IT104 与采购单单价，并逐行累计 IT102 数量。出现以下任一情况时拒绝整张
发票且不占用任何数量（后续发票继续按输入顺序核销）：

  * 发票行号不在采购单中（PO_LINE_NOT_FOUND）
  * 发票单价与采购单单价不符（UNIT_PRICE_MISMATCH）
  * 发票号与已接受的发票重复（INVOICE_DUPLICATE）
  * 任一采购单行累计数量超出订购数量（QUANTITY_EXCEEDED）

数量一律换算为 10^-3 的整数（毫单位）累计，单价换算为 10^-4 的整数比较，
全程不使用浮点数，任意位数的数量均可精确核销。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol

from .verifier import Invoice, X12Error, verify_text

MAX_EDI_TEXTS = 50

# 与 verifier 中 IT102 / IT104 相同的十进制格式约束
_QTY_RE = re.compile(r"^(\d+)(?:\.(\d{1,3}))?$")   # 数量：正，至多 3 位小数
_UNIT_RE = re.compile(r"^(\d+)(?:\.(\d{1,4}))?$")  # 单价：非负，至多 4 位小数


class OrderError(Exception):
    """采购单非法；整个请求返回 422。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OrderLineIn(Protocol):
    """采购单行输入（与 HTTP 层解耦，仅要求这三个属性）。"""

    line_id: str
    ordered_quantity: str
    unit_price: str


@dataclass(frozen=True)
class OrderLine:
    line_id: str
    ordered_milli: int   # ordered_quantity * 1000（整数）
    unit_4dp: int        # unit_price * 10000（整数）


@dataclass(frozen=True)
class RejectReason:
    code: str
    message: str
    seg: Optional[int] = None      # 原核验错误的段序号（仅核验失败时）
    segment: Optional[str] = None  # 原核验错误的段标识（仅核验失败时）
    line_id: Optional[str] = None  # 触发拒绝的采购单行号（核销失败时）


@dataclass(frozen=True)
class InvoiceResult:
    index: int                     # edi_texts 中的输入序号（从 0 开始）
    accepted: bool
    invoice_number: Optional[str]  # 核验失败时为 None
    error: Optional[RejectReason]


@dataclass(frozen=True)
class ReconcileOutcome:
    results: list[InvoiceResult]
    remaining: list[tuple[str, int]]  # (line_id, 剩余数量 * 1000)，按采购单顺序


def _parse_scaled(raw: str, pattern: re.Pattern[str], scale: int) -> Optional[int]:
    """把十进制字符串换算为 10**-scale 的整数；格式不符返回 None。"""
    m = pattern.fullmatch(raw)
    if not m:
        return None
    whole, frac = m.group(1), m.group(2) or ""
    return int(whole + frac.ljust(scale, "0"))


def milli_to_str(milli: int) -> str:
    """毫单位整数 -> 最简十进制字符串（去掉多余的尾随 0）。"""
    whole, frac = divmod(milli, 1000)
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:03d}".rstrip("0")


def validate_order_lines(lines: list[OrderLineIn]) -> list[OrderLine]:
    """校验采购单；任一非法抛 OrderError（整单 422）。"""
    seen: set[str] = set()
    out: list[OrderLine] = []
    for pos, line in enumerate(lines, start=1):
        if line.line_id == "":
            raise OrderError("ORDER_LINE_ID_EMPTY",
                             f"第 {pos} 行 line_id 不能为空")
        if line.line_id in seen:
            raise OrderError("ORDER_LINE_DUPLICATE",
                             f"采购单行号重复: {line.line_id!r}")
        seen.add(line.line_id)

        qty = _parse_scaled(line.ordered_quantity, _QTY_RE, 3)
        if qty is None:
            raise OrderError(
                "ORDER_QUANTITY_INVALID",
                f"行 {line.line_id!r} 的 ordered_quantity 不是合法十进制数"
                f"（至多 3 位小数）: {line.ordered_quantity!r}")
        if qty <= 0:
            raise OrderError(
                "ORDER_QUANTITY_INVALID",
                f"行 {line.line_id!r} 的 ordered_quantity 必须为正数: "
                f"{line.ordered_quantity!r}")

        price = _parse_scaled(line.unit_price, _UNIT_RE, 4)
        if price is None:
            raise OrderError(
                "ORDER_PRICE_INVALID",
                f"行 {line.line_id!r} 的 unit_price 不是合法十进制数"
                f"（非负，至多 4 位小数）: {line.unit_price!r}")

        out.append(OrderLine(line.line_id, qty, price))
    return out


def reconcile(order_lines: list[OrderLine],
              edi_texts: list[str]) -> ReconcileOutcome:
    """按输入顺序逐张核销发票；被拒绝的发票不占用数量、不登记发票号。"""
    by_id = {ol.line_id: ol for ol in order_lines}
    consumed = {ol.line_id: 0 for ol in order_lines}  # 已接受数量，毫单位
    accepted_numbers: set[str] = set()
    results: list[InvoiceResult] = []

    for index, edi_text in enumerate(edi_texts):
        # 1) 与 /v1/verify 相同的 ASCII/大小/X12 核验；失败只拒绝该张
        try:
            invoice = verify_text(edi_text)
        except X12Error as exc:
            results.append(InvoiceResult(
                index, False, None,
                RejectReason(exc.code, exc.message, exc.seg, exc.segment)))
            continue

        reason = _check_invoice(invoice, by_id, consumed, accepted_numbers)
        if reason is not None:
            results.append(
                InvoiceResult(index, False, invoice.invoice_number, reason))
            continue

        # 接受：占用数量、登记发票号
        for line in invoice.lines:
            add = _parse_scaled(line.quantity, _QTY_RE, 3)
            assert add is not None  # 已经过 verify 核验
            consumed[line.line_id] += add
        accepted_numbers.add(invoice.invoice_number)
        results.append(InvoiceResult(index, True, invoice.invoice_number, None))

    remaining = [(ol.line_id, ol.ordered_milli - consumed[ol.line_id])
                 for ol in order_lines]
    return ReconcileOutcome(results, remaining)


def _check_invoice(invoice: Invoice,
                   by_id: dict[str, OrderLine],
                   consumed: dict[str, int],
                   accepted_numbers: set[str]) -> Optional[RejectReason]:
    """核销规则检查；返回 None 表示可接受。"""
    # 行号必须都在采购单中，且单价按十进制数值与采购单精确相等
    for line in invoice.lines:
        order_line = by_id.get(line.line_id)
        if order_line is None:
            return RejectReason(
                "PO_LINE_NOT_FOUND",
                f"IT101 行号 {line.line_id!r} 不在采购单中",
                line_id=line.line_id)
        inv_price = _parse_scaled(line.unit_price, _UNIT_RE, 4)
        assert inv_price is not None  # 已经过 verify 核验
        if inv_price != order_line.unit_4dp:
            return RejectReason(
                "UNIT_PRICE_MISMATCH",
                f"行 {line.line_id!r} 的 IT104 单价 {line.unit_price!r} "
                f"与采购单单价不符",
                line_id=line.line_id)

    # 发票号不得与已接受的发票重复
    if invoice.invoice_number in accepted_numbers:
        return RejectReason(
            "INVOICE_DUPLICATE",
            f"发票号 {invoice.invoice_number!r} 与已接受的发票重复")

    # 逐行累计数量，任一行超出订购数量即拒绝整张
    for line in invoice.lines:
        order_line = by_id[line.line_id]
        add = _parse_scaled(line.quantity, _QTY_RE, 3)
        assert add is not None
        new_total = consumed[line.line_id] + add
        if new_total > order_line.ordered_milli:
            return RejectReason(
                "QUANTITY_EXCEEDED",
                f"行 {line.line_id!r} 累计数量 {milli_to_str(new_total)} "
                f"超出订购数量 {milli_to_str(order_line.ordered_milli)}",
                line_id=line.line_id)
    return None
