"""X12 810 发票核验、报文生成、采购单核销、997 功能回执生成与 997 状态解析 HTTP API。"""

from __future__ import annotations

import os
import threading
from typing import Literal, Optional

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .ack_status import parse_ack_status
from .acknowledger import acknowledge
from .inbox import (
    InboxConflict,
    InboxStore,
    ReceiptNotFound,
    business_fingerprint,
    inbox_key,
    receipt_id_for,
)
from .issuer import IssueError, issue
from .reconciler import (
    MAX_EDI_TEXTS,
    OrderError,
    milli_to_str,
    reconcile,
    validate_order_lines,
)
from .splitter import SplitError, split_text
from .verifier import Invoice, X12Error, verify_text

app = FastAPI(
    title="X12 810 Invoice Verification API",
    version="1.6.0",
    description="核验 ASC X12 004010 单份 810 发票报文的外壳结构与发票金额，"
                "由结构化发票数据生成 004010 X12 810 报文，"
                "将单交换/单组内含 1～50 个 810 交易集的交换报文拆分为多张单张报文，"
                "按采购单对多份发票逐张核销，对单份 810 生成 997 功能回执，"
                "按发送清单解析 997 回执中各交易集的接受状态，"
                "并提供基于 SQLite 的幂等收件库（重发识别与冲突检测）。",
)


class VerifyRequest(BaseModel):
    edi_text: str = Field(..., description="完整 X12 810 报文（ASCII，≤64 KiB）")


class LineOut(BaseModel):
    line_id: str = Field(..., description="IT101 行号")
    quantity: str = Field(..., description="IT102 数量（原文）")
    unit_price: str = Field(..., description="IT104 单价（原文）")
    line_amount: int = Field(..., description="行金额，整数分（ROUND_HALF_UP）")


class VerifyResponse(BaseModel):
    invoice_number: str = Field(..., description="BIG02 发票号")
    lines: list[LineOut]
    total_amount: int = Field(..., description="TDS01 总额，整数分")


@app.exception_handler(X12Error)
async def x12_error_handler(_request, exc: X12Error) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "seg": exc.seg,
                "segment": exc.segment,
            }
        },
    )


@app.exception_handler(OrderError)
async def order_error_handler(_request, exc: OrderError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(SplitError)
async def split_error_handler(_request, exc: SplitError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "seg": exc.seg,
                "segment": exc.segment,
                "set_index": exc.set_index,
            }
        },
    )


@app.exception_handler(IssueError)
async def issue_error_handler(_request, exc: IssueError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "field": exc.field,
            }
        },
    )


@app.exception_handler(InboxConflict)
async def inbox_conflict_handler(_request, exc: InboxConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(ReceiptNotFound)
async def receipt_not_found_handler(_request, exc: ReceiptNotFound) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.post(
    "/v1/verify",
    response_model=VerifyResponse,
    summary="核验一份 X12 810 发票报文",
)
async def verify_invoice(req: VerifyRequest) -> VerifyResponse:
    invoice = verify_text(req.edi_text)
    return VerifyResponse(
        invoice_number=invoice.invoice_number,
        lines=[
            LineOut(
                line_id=line.line_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                line_amount=line.line_amount,
            )
            for line in invoice.lines
        ],
        total_amount=invoice.total_amount,
    )


# ---------- POST /v1/issue-810 ----------


class IssueLineInModel(BaseModel):
    it101: str = Field(..., description="IT101 行号（1～20 字符，全局唯一）")
    it102: str = Field(
        ..., description="IT102 数量（十进制字符串，正，至多 3 位小数，≤15 字符）")
    it104: str = Field(
        ..., description="IT104 单价（十进制字符串，非负，至多 4 位小数，≤17 字符）")


class IssueRequest(BaseModel):
    isa05: str = Field(..., description="ISA05 交换发送方限定符（恰好 2 字符）")
    isa06: str = Field(
        ..., description="ISA06 交换发送方标识（1～15 字符，右侧补空格至 15）")
    isa07: str = Field(..., description="ISA07 交换接收方限定符（恰好 2 字符）")
    isa08: str = Field(
        ..., description="ISA08 交换接收方标识（1～15 字符，右侧补空格至 15）")
    isa09: str = Field(..., description="ISA09 交换日期（YYMMDD，6 位数字）")
    isa10: str = Field(..., description="ISA10 交换时间（HHMM，4 位数字）")
    isa13: str = Field(..., description="ISA13 交换控制号（恰好 9 位数字）")
    gs02: str = Field(..., description="GS02 应用发送方代码（1～15 字符）")
    gs03: str = Field(..., description="GS03 应用接收方代码（1～15 字符）")
    gs04: str = Field(..., description="GS04 功能组日期（CCYYMMDD，8 位数字）")
    gs05: str = Field(
        ..., description="GS05 功能组时间（HHMM[SS[DD]]，4/6/8 位数字）")
    gs06: str = Field(..., description="GS06 组控制号（1～9 字符）")
    st02: str = Field(..., description="ST02 交易集控制号（1～9 字符）")
    big01: str = Field(..., description="BIG01 发票日期（CCYYMMDD，8 位数字）")
    big02: str = Field(..., description="BIG02 发票号（1～22 字符）")
    lines: list[IssueLineInModel] = Field(
        ..., description="1～100 行发票行（IT101/IT102/IT104）")


class IssueResponse(BaseModel):
    edi_text: str = Field(
        ..., description="ASC X12 004010 810 报文（分隔符 * : ~，ISA 106 字节，"
                         "可通过 /v1/verify）")
    total_amount: int = Field(..., description="TDS01 总额，整数分")


@app.post(
    "/v1/issue-810",
    response_model=IssueResponse,
    summary="由结构化发票数据生成 ASC X12 004010 810 报文",
)
async def issue_810(req: IssueRequest) -> IssueResponse:
    edi_text, total_amount = issue(
        isa05=req.isa05, isa06=req.isa06, isa07=req.isa07, isa08=req.isa08,
        isa09=req.isa09, isa10=req.isa10, isa13=req.isa13,
        gs02=req.gs02, gs03=req.gs03, gs04=req.gs04, gs05=req.gs05,
        gs06=req.gs06, st02=req.st02, big01=req.big01, big02=req.big02,
        lines=req.lines,
    )
    return IssueResponse(edi_text=edi_text, total_amount=total_amount)


# ---------- POST /v1/split-810 ----------


class SplitRequest(BaseModel):
    edi_text: str = Field(
        ..., description="含 1～50 个连续 ST…SE 交易集的完整 X12 交换报文"
                         "（单 ISA/IEA、单 GS/GE，ASCII，≤1 MiB）")


class SplitItemOut(BaseModel):
    set_index: int = Field(..., description="交易集在原报文中的序号（从 0 开始）")
    st02: str = Field(..., description="ST02 交易集控制号（原文）")
    invoice_number: str = Field(..., description="BIG02 发票号")
    edi_text: str = Field(
        ..., description="重建的单张 810 报文（GE01=1，可直接提交 /v1/verify，≤64 KiB）")


class SplitResponse(BaseModel):
    invoices: list[SplitItemOut] = Field(
        ..., description="按原报文顺序排列的各交易集拆分结果")


@app.post(
    "/v1/split-810",
    response_model=SplitResponse,
    summary="将含多个 810 交易集的交换报文拆分为多张单张报文并逐张核验",
)
async def split_810(req: SplitRequest) -> SplitResponse:
    # 任一外壳或业务核验失败均由 SplitError 处理器返回 422，不返回部分结果
    items = split_text(req.edi_text)
    return SplitResponse(invoices=[
        SplitItemOut(
            set_index=item.set_index,
            st02=item.st02,
            invoice_number=item.invoice_number,
            edi_text=item.edi_text,
        )
        for item in items
    ])


# ---------- POST /v1/inbox、GET /v1/inbox/{receipt_id} ----------

_STORE: Optional[InboxStore] = None
_STORE_LOCK = threading.Lock()


def get_inbox_store() -> InboxStore:
    """收件库单例（惰性创建，路径取环境变量 INBOX_DB_PATH，默认 ./inbox.db）。"""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = InboxStore(os.environ.get("INBOX_DB_PATH", "inbox.db"))
    return _STORE


def _verify_result(invoice: Invoice) -> dict:
    """与 /v1/verify 响应一致的核验结果（用于保存与返回）。"""
    return {
        "invoice_number": invoice.invoice_number,
        "lines": [
            {
                "line_id": line.line_id,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "line_amount": line.line_amount,
            }
            for line in invoice.lines
        ],
        "total_amount": invoice.total_amount,
    }


class InboxSubmitResponse(BaseModel):
    receipt_id: str = Field(
        ..., description="收件回执 ID（由收件键与业务指纹确定，固定不变）")
    replayed: bool = Field(
        ..., description="false=首次保存；true=同收件键同业务指纹的重发")
    invoice_number: str = Field(..., description="BIG02 发票号")
    lines: list[LineOut]
    total_amount: int = Field(..., description="TDS01 总额，整数分")


class InboxRecordResponse(BaseModel):
    receipt_id: str = Field(..., description="收件回执 ID")
    edi_text: str = Field(..., description="首次保存的 810 报文原文")
    invoice_number: str = Field(..., description="BIG02 发票号")
    lines: list[LineOut]
    total_amount: int = Field(..., description="TDS01 总额，整数分")


@app.post(
    "/v1/inbox",
    response_model=InboxSubmitResponse,
    summary="幂等接收一份 X12 810 发票报文（核验后入收件库）",
)
async def inbox_submit(
    req: VerifyRequest,
    store: InboxStore = Depends(get_inbox_store),
) -> InboxSubmitResponse:
    # 先沿用 /v1/verify 核验；失败抛 X12Error -> 原样 422，且不写收件库
    invoice = verify_text(req.edi_text)
    key = inbox_key(invoice)
    fingerprint = business_fingerprint(invoice)
    record = store.submit(
        key=key,
        fingerprint=fingerprint,
        receipt_id=receipt_id_for(key, fingerprint),
        edi_text=req.edi_text,
        result=_verify_result(invoice),
    )
    return InboxSubmitResponse(
        receipt_id=record.receipt_id,
        replayed=record.replayed,
        invoice_number=record.result["invoice_number"],
        lines=[LineOut(**line) for line in record.result["lines"]],
        total_amount=record.result["total_amount"],
    )


@app.get(
    "/v1/inbox/{receipt_id}",
    response_model=InboxRecordResponse,
    summary="按 receipt_id 取回收件记录（首次保存的原文与核验结果）",
)
async def inbox_get(
    receipt_id: str,
    store: InboxStore = Depends(get_inbox_store),
) -> InboxRecordResponse:
    record = store.get(receipt_id)
    return InboxRecordResponse(
        receipt_id=record.receipt_id,
        edi_text=record.edi_text,
        invoice_number=record.result["invoice_number"],
        lines=[LineOut(**line) for line in record.result["lines"]],
        total_amount=record.result["total_amount"],
    )


# ---------- POST /v1/reconcile ----------


class OrderLineInModel(BaseModel):
    line_id: str = Field(..., description="采购单行号，须唯一")
    ordered_quantity: str = Field(
        ..., description="订购数量（十进制字符串，正，至多 3 位小数）")
    unit_price: str = Field(
        ..., description="单价（十进制字符串，非负，至多 4 位小数）")


class ReconcileRequest(BaseModel):
    order_lines: list[OrderLineInModel] = Field(..., description="采购单行")
    edi_texts: list[str] = Field(
        ..., min_length=1, max_length=MAX_EDI_TEXTS,
        description=f"按输入顺序核销的 1～{MAX_EDI_TEXTS} 份 X12 810 报文")


class RejectOut(BaseModel):
    code: str = Field(..., description="拒绝原因错误码（核验失败时为原核验错误码）")
    message: str
    seg: Optional[int] = Field(None, description="原核验错误的段序号（仅核验失败时）")
    segment: Optional[str] = Field(None, description="原核验错误的段标识（仅核验失败时）")
    line_id: Optional[str] = Field(None, description="触发拒绝的采购单行号（核销失败时）")


class InvoiceResultOut(BaseModel):
    index: int = Field(..., description="edi_texts 中的输入序号（从 0 开始）")
    accepted: bool
    invoice_number: Optional[str] = Field(
        None, description="BIG02 发票号；核验失败时为 null")
    error: Optional[RejectOut] = None


class RemainingOut(BaseModel):
    line_id: str = Field(..., description="采购单行号")
    remaining_quantity: str = Field(..., description="剩余数量（十进制字符串）")


class ReconcileResponse(BaseModel):
    results: list[InvoiceResultOut] = Field(..., description="逐张核销结果")
    remaining: list[RemainingOut] = Field(
        ..., description="各采购单行剩余数量（按采购单顺序）")


@app.post(
    "/v1/reconcile",
    response_model=ReconcileResponse,
    summary="按采购单对多份 X12 810 发票逐张核销",
)
async def reconcile_invoices(req: ReconcileRequest) -> ReconcileResponse:
    # 采购单非法 -> 整单 422
    order_lines = validate_order_lines(req.order_lines)
    outcome = reconcile(order_lines, req.edi_texts)
    return ReconcileResponse(
        results=[
            InvoiceResultOut(
                index=r.index,
                accepted=r.accepted,
                invoice_number=r.invoice_number,
                error=RejectOut(
                    code=r.error.code,
                    message=r.error.message,
                    seg=r.error.seg,
                    segment=r.error.segment,
                    line_id=r.error.line_id,
                ) if r.error is not None else None,
            )
            for r in outcome.results
        ],
        remaining=[
            RemainingOut(line_id=line_id,
                         remaining_quantity=milli_to_str(milli))
            for line_id, milli in outcome.remaining
        ],
    )


# ---------- POST /v1/acknowledge ----------


class AcknowledgeRequest(BaseModel):
    edi_text: str = Field(..., description="完整 X12 810 报文（ASCII，≤64 KiB）")
    isa13: str = Field(..., description="回执 ISA13 交换控制号（恰好 9 位数字）")
    gs06: str = Field(..., description="回执 GS06 组控制号（非空，不含分隔符）")
    st02: str = Field(..., description="回执 ST02 交易集控制号（非空，不含分隔符）")


class AcknowledgeResponse(BaseModel):
    edi_text: str = Field(..., description="ASC X12 004010 997 功能回执报文")


@app.post(
    "/v1/acknowledge",
    response_model=AcknowledgeResponse,
    summary="对单份 X12 810 报文生成 997 功能回执",
)
async def acknowledge_invoice(req: AcknowledgeRequest) -> AcknowledgeResponse:
    return AcknowledgeResponse(
        edi_text=acknowledge(req.edi_text, req.isa13, req.gs06, req.st02))


# ---------- POST /v1/ack-status ----------


class AckStatusRequest(BaseModel):
    gs06: str = Field(..., description="发送清单的组控制号（非空）")
    st02: list[str] = Field(
        ..., min_length=1, max_length=100,
        description="发送清单的 1～100 个唯一非空交易集控制号")
    edi_text: str = Field(
        ..., description="完整 ASC X12 004010 997 功能回执报文（ASCII，≤64 KiB）")


class AckStatusItemOut(BaseModel):
    st02: str = Field(..., description="发送清单中的交易集控制号")
    status: Literal["A", "R", "U"] = Field(
        ..., description="A=接受（AK5=A 或未列项接受数恰好覆盖全部未列项）；"
                         "R=拒绝（AK5=R 或未列项无接受数）；"
                         "U=未知（未列项，剩余接受数介于两者之间）")


class AckStatusResponse(BaseModel):
    items: list[AckStatusItemOut] = Field(
        ..., description="按发送清单顺序排列的各项状态")


@app.post(
    "/v1/ack-status",
    response_model=AckStatusResponse,
    summary="按发送清单解析 997 功能回执中各 810 交易集的接受状态",
)
async def acknowledge_status(req: AckStatusRequest) -> AckStatusResponse:
    items = parse_ack_status(req.edi_text, req.gs06, req.st02)
    return AckStatusResponse(
        items=[AckStatusItemOut(st02=item.st02, status=item.status)
               for item in items])


@app.get("/health", summary="存活探针")
async def health() -> dict:
    return {"status": "ok"}
