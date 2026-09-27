"""POST /v1/reconcile 端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app
from app.reconciler import MAX_EDI_TEXTS, milli_to_str
from test_verify import build_edi

client = TestClient(app)


def edi(number, lines, **kwargs):
    """构造一份发票报文，BIG02 发票号为 number。"""
    return build_edi(lines, **kwargs).replace(
        "BIG*20200830*9001~", f"BIG*20200830*{number}~")


def post_reconcile(order_lines, edi_texts):
    return client.post("/v1/reconcile", json={
        "order_lines": order_lines,
        "edi_texts": edi_texts,
    })


def po(line_id, qty, price):
    return {"line_id": line_id, "ordered_quantity": qty, "unit_price": price}


# ---------- 成功路径 ----------

def test_happy_path_partial_consumption():
    r = post_reconcile(
        [po("1", "10", "19.99"), po("2", "5", "4.25")],
        [
            edi("INV-1", [("1", "2", "19.99"), ("2", "1", "4.25")]),
            edi("INV-2", [("1", "3", "19.99")]),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert [(x["index"], x["accepted"]) for x in body["results"]] == [
        (0, True), (1, True)]
    assert [x["invoice_number"] for x in body["results"]] == ["INV-1", "INV-2"]
    assert all(x["error"] is None for x in body["results"])
    assert body["remaining"] == [
        {"line_id": "1", "remaining_quantity": "5"},
        {"line_id": "2", "remaining_quantity": "4"},
    ]


def test_exact_quantity_boundary_accepted():
    # 累计数量恰好等于订购数量：接受
    r = post_reconcile(
        [po("1", "10", "1.00")],
        [edi("A", [("1", "6", "1.00")]), edi("B", [("1", "4", "1.00")])],
    )
    assert r.status_code == 200
    assert [x["accepted"] for x in r.json()["results"]] == [True, True]
    assert r.json()["remaining"] == [
        {"line_id": "1", "remaining_quantity": "0"}]


def test_price_compared_as_exact_decimal():
    # 单价按十进制数值精确比较："1.50" 与 "1.5"、"1.5000" 视为同一价格
    r = post_reconcile(
        [po("1", "10", "1.50")],
        [edi("A", [("1", "1", "1.5")]), edi("B", [("1", "1", "1.5000")])],
    )
    assert [x["accepted"] for x in r.json()["results"]] == [True, True]


def test_remaining_quantity_formatting():
    r = post_reconcile(
        [po("1", "10.500", "1.00"), po("2", "2", "1.00"),
         po("3", "0.001", "1.00")],
        [edi("A", [("1", "3", "1.00"), ("2", "0.5", "1.00"),
                   ("3", "0.001", "1.00")])],
    )
    assert r.json()["remaining"] == [
        {"line_id": "1", "remaining_quantity": "7.5"},
        {"line_id": "2", "remaining_quantity": "1.5"},
        {"line_id": "3", "remaining_quantity": "0"},
    ]


def test_empty_order_lines_rejects_every_invoice():
    r = post_reconcile([], [edi("A", [("1", "1", "1.00")])])
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["accepted"] is False
    assert body["results"][0]["error"]["code"] == "PO_LINE_NOT_FOUND"
    assert body["remaining"] == []


# ---------- 核验失败：只拒绝该张 ----------

def test_verify_failure_rejects_only_that_invoice():
    good1 = edi("INV-A", [("1", "2", "1.00")])
    bad = edi("INV-BAD", [("1", "1", "1.00")], tds="1")  # TDS_MISMATCH
    good2 = edi("INV-B", [("1", "3", "1.00")])
    r = post_reconcile([po("1", "10", "1.00")], [good1, bad, good2])
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["accepted"] for x in results] == [True, False, True]
    err = results[1]["error"]
    assert err["code"] == "TDS_MISMATCH"      # 原核验错误码
    assert isinstance(err["seg"], int) and err["segment"] == "TDS"
    assert results[1]["invoice_number"] is None
    assert r.json()["remaining"] == [
        {"line_id": "1", "remaining_quantity": "5"}]


def test_verify_failure_not_ascii_and_too_large():
    r = post_reconcile(
        [po("1", "10", "1.00")],
        ["café", "A" * (64 * 1024 + 1), edi("INV-OK", [("1", "1", "1.00")])],
    )
    results = r.json()["results"]
    assert [x["accepted"] for x in results] == [False, False, True]
    assert results[0]["error"]["code"] == "NOT_ASCII"
    assert results[1]["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert results[0]["invoice_number"] is None


# ---------- 核销规则 ----------

def test_po_line_not_found():
    r = post_reconcile(
        [po("1", "10", "1.00")],
        [edi("A", [("9", "1", "1.00")]), edi("B", [("1", "2", "1.00")])],
    )
    results = r.json()["results"]
    assert [x["accepted"] for x in results] == [False, True]
    assert results[0]["error"]["code"] == "PO_LINE_NOT_FOUND"
    assert results[0]["error"]["line_id"] == "9"
    assert results[0]["invoice_number"] == "A"
    # 被拒绝的发票不占用数量
    assert r.json()["remaining"] == [
        {"line_id": "1", "remaining_quantity": "8"}]


def test_unit_price_mismatch():
    r = post_reconcile(
        [po("1", "10", "19.99")],
        [edi("A", [("1", "1", "19.98")])],
    )
    err = r.json()["results"][0]["error"]
    assert err["code"] == "UNIT_PRICE_MISMATCH"
    assert err["line_id"] == "1"


def test_rejected_invoice_does_not_consume_quantity():
    # 第一张价格不符被拒；第二张用满全部数量仍应接受
    r = post_reconcile(
        [po("1", "10", "1.00")],
        [edi("A", [("1", "6", "9.99")]), edi("B", [("1", "10", "1.00")])],
    )
    assert [x["accepted"] for x in r.json()["results"]] == [False, True]
    assert r.json()["remaining"] == [
        {"line_id": "1", "remaining_quantity": "0"}]


def test_duplicate_invoice_number_rejected():
    r = post_reconcile(
        [po("1", "10", "1.00")],
        [edi("DUP", [("1", "1", "1.00")]), edi("DUP", [("1", "2", "1.00")])],
    )
    results = r.json()["results"]
    assert [x["accepted"] for x in results] == [True, False]
    assert results[1]["error"]["code"] == "INVOICE_DUPLICATE"
    assert results[1]["invoice_number"] == "DUP"
    assert r.json()["remaining"] == [
        {"line_id": "1", "remaining_quantity": "9"}]


def test_duplicate_of_rejected_invoice_is_allowed():
    # 只与“已接受”的发票查重：第一张被拒，同号第二张可接受
    r = post_reconcile(
        [po("1", "10", "1.00")],
        [edi("RETRY", [("1", "1", "9.99")]), edi("RETRY", [("1", "1", "1.00")])],
    )
    results = r.json()["results"]
    assert [x["accepted"] for x in results] == [False, True]
    assert results[0]["error"]["code"] == "UNIT_PRICE_MISMATCH"


def test_quantity_exceeded_rolls_back_whole_invoice():
    # 多行发票中任一行超量 -> 整张拒绝，其他行也不占用数量
    r = post_reconcile(
        [po("1", "10", "1.00"), po("2", "1", "2.00")],
        [edi("A", [("1", "5", "1.00"), ("2", "2", "2.00")]),
         edi("B", [("1", "5", "1.00"), ("2", "1", "2.00")])],
    )
    results = r.json()["results"]
    assert [x["accepted"] for x in results] == [False, True]
    err = results[0]["error"]
    assert err["code"] == "QUANTITY_EXCEEDED"
    assert err["line_id"] == "2"
    assert r.json()["remaining"] == [
        {"line_id": "1", "remaining_quantity": "5"},
        {"line_id": "2", "remaining_quantity": "0"},
    ]


def test_quantity_accumulates_across_invoices():
    r = post_reconcile(
        [po("1", "10", "1.00")],
        [edi("A", [("1", "6", "1.00")]),
         edi("B", [("1", "5", "1.00")]),   # 6+5=11 > 10 -> 拒绝
         edi("C", [("1", "4", "1.00")])],  # 6+4=10 -> 接受
    )
    results = r.json()["results"]
    assert [x["accepted"] for x in results] == [True, False, True]
    assert results[1]["error"]["code"] == "QUANTITY_EXCEEDED"
    assert r.json()["remaining"] == [
        {"line_id": "1", "remaining_quantity": "0"}]


# ---------- 数量计算不用浮点数 ----------

def test_no_float_quantity_math():
    # 浮点数下 0.1 + 0.2 = 0.30000000000000004 > 0.3，整数定点下恰好相等
    r = post_reconcile(
        [po("1", "0.3", "0.1")],
        [edi("A", [("1", "0.1", "0.1")]), edi("B", [("1", "0.2", "0.1")])],
    )
    assert [x["accepted"] for x in r.json()["results"]] == [True, True]
    assert r.json()["remaining"] == [
        {"line_id": "1", "remaining_quantity": "0"}]


def test_no_float_big_quantities():
    big = "999999999999999999999.999"
    r = post_reconcile(
        [po("1", big, "1")],
        [edi("A", [("1", big, "1")])],
    )
    assert r.json()["results"][0]["accepted"] is True
    assert r.json()["remaining"] == [
        {"line_id": "1", "remaining_quantity": "0"}]


def test_milli_to_str():
    assert milli_to_str(0) == "0"
    assert milli_to_str(10000) == "10"
    assert milli_to_str(10500) == "10.5"
    assert milli_to_str(7500) == "7.5"
    assert milli_to_str(1) == "0.001"
    assert milli_to_str(10) == "0.01"


# ---------- 非法采购单：整单 422 ----------

def expect_order_422(order_lines, code):
    r = post_reconcile(order_lines, [edi("A", [("1", "1", "1.00")])])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code


def test_order_duplicate_line_id_422():
    expect_order_422([po("1", "1", "1.00"), po("1", "2", "2.00")],
                     "ORDER_LINE_DUPLICATE")


def test_order_quantity_invalid_422():
    expect_order_422([po("1", "1.0001", "1.00")], "ORDER_QUANTITY_INVALID")
    expect_order_422([po("1", "0", "1.00")], "ORDER_QUANTITY_INVALID")
    expect_order_422([po("1", "-1", "1.00")], "ORDER_QUANTITY_INVALID")
    expect_order_422([po("1", "abc", "1.00")], "ORDER_QUANTITY_INVALID")


def test_order_price_invalid_422():
    expect_order_422([po("1", "1", "1.00001")], "ORDER_PRICE_INVALID")
    expect_order_422([po("1", "1", "-0.5")], "ORDER_PRICE_INVALID")
    expect_order_422([po("1", "1", "x")], "ORDER_PRICE_INVALID")


def test_order_empty_line_id_422():
    expect_order_422([po("", "1", "1.00")], "ORDER_LINE_ID_EMPTY")


def test_order_422_envelope_shape():
    r = post_reconcile([po("1", "0", "1.00")], [edi("A", [("1", "1", "1.00")])])
    assert r.status_code == 422
    err = r.json()["error"]
    assert set(err) == {"code", "message"}


# ---------- 请求形状 ----------

def test_edi_texts_count_bounds():
    good = edi("A", [("1", "1", "1.00")])
    assert post_reconcile([po("1", "1", "1.00")], []).status_code == 422
    too_many = [good] * (MAX_EDI_TEXTS + 1)
    assert post_reconcile([po("1", "1", "1.00")], too_many).status_code == 422
    exactly = [edi(f"N{i}", [("1", "1", "1.00")]) for i in range(MAX_EDI_TEXTS)]
    r = post_reconcile([po("1", str(MAX_EDI_TEXTS), "1.00")], exactly)
    assert r.status_code == 200
    assert len(r.json()["results"]) == MAX_EDI_TEXTS


def test_schema_validation_422():
    assert client.post("/v1/reconcile", json={}).status_code == 422
    # 数量/单价必须是字符串，不接受 JSON 数字
    r = client.post("/v1/reconcile", json={
        "order_lines": [{"line_id": "1", "ordered_quantity": 1,
                         "unit_price": "1.00"}],
        "edi_texts": ["x"],
    })
    assert r.status_code == 422


def test_results_follow_input_order():
    r = post_reconcile(
        [po("1", "10", "1.00")],
        [edi(f"INV-{i}", [("1", "1", "1.00")]) for i in range(5)],
    )
    assert [x["index"] for x in r.json()["results"]] == [0, 1, 2, 3, 4]
    assert [x["invoice_number"] for x in r.json()["results"]] == [
        f"INV-{i}" for i in range(5)]
