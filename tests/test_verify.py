"""端到端核验测试。"""

import pytest

from fastapi.testclient import TestClient

from app.main import app
from app.verifier import MAX_BYTES, X12Error, verify

client = TestClient(app)


# ---------- 报文构造工具 ----------

def _expected_cents(qty: str, price: str) -> int:
    """独立参考实现：用 200 位精度的 Decimal 逐行 ROUND_HALF_UP 到整数分。"""
    from decimal import Decimal, ROUND_HALF_UP, localcontext
    with localcontext() as ctx:
        ctx.prec = 200
        return int((Decimal(qty) * Decimal(price)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def isa(ctrl="000000001", elem="*", comp=":", term="~"):
    fields = [
        "00", "          ",  # ISA01/02
        "00", "          ",  # ISA03/04
        "ZZ", "SENDER123456789",  # ISA05/06 (15 字符)
        "ZZ", "RECEIVER1234567",  # ISA07/08 (15 字符)
        "000830", "1200",    # ISA09/10
        "U", "00401",         # ISA11/12
        ctrl,                 # ISA13 (9 字符)
        "0",                   # ISA14
        "P",                    # ISA15
        comp,                  # ISA16 组件分隔符
    ]
    out = "ISA" + elem + elem.join(fields)
    assert len(out) == 105, len(out)
    return out + term


def build_edi(lines, *, tds=None, ctrl="000000001", gs_ctrl="42",
              st_ctrl="0001", elem="*", comp=":", term="~",
              ge01="1", iea01="1", ge_ctrl=None, iea_ctrl=None,
              st_ctrl_se=None, omit=()):
    """组装一份 810；未显式给出的 tds 取各行整数分之和。"""
    if tds is None:
        tds = str(sum(_expected_cents(qty, price)
                      for _lid, qty, price in lines))
    ge_ctrl = ge_ctrl if ge_ctrl is not None else gs_ctrl
    iea_ctrl = iea_ctrl if iea_ctrl is not None else ctrl
    st_ctrl_se = st_ctrl_se if st_ctrl_se is not None else st_ctrl

    def seg(*fields):
        return elem.join(fields) + term

    body = []
    body.append(seg("GS", "IN", "SENDERGS", "RECVGS", "20200830", "1200",
                    gs_ctrl, "X", "004010"))
    body.append(seg("ST", "810", st_ctrl))
    body.append(seg("BIG", "20200830", "9001"))
    for lid, qty, price in lines:
        body.append(seg("IT1", lid, qty, "EA", price))
    body.append(seg("TDS", tds))
    st_pos = 2  # ISA=1, GS=2, ST=3
    se01 = len(body) - st_pos + 2  # SE itself included
    body.append(seg("SE", str(se01), st_ctrl_se))
    body.append(seg("GE", ge01, ge_ctrl))
    body.append(seg("IEA", iea01, iea_ctrl))

    kept = [seg for seg in body if not any(tag in seg for tag in omit)]
    return isa(ctrl, elem, comp, term) + "".join(kept)


# ---------- 核心解析：成功路径 ----------

def test_valid_basic():
    inv = verify(build_edi([("1", "2", "10.50"), ("2", "3", "4.25")]))
    assert inv.invoice_number == "9001"
    assert [l.line_amount for l in inv.lines] == [2100, 1275]
    assert inv.total_amount == 3375


def test_round_half_up_per_line():
    # 2 * 1.005 = 2.010 -> 2.01；2 * 1.0025 = 2.005 -> 2.01（先逐行取整分再求和）
    inv = verify(build_edi([("1", "2", "1.005"), ("2", "2", "1.0025")]))
    assert [l.line_amount for l in inv.lines] == [201, 201]
    assert inv.total_amount == 402


def test_three_four_decimal_places_accepted():
    inv = verify(build_edi([("1", "0.001", "0.0001")]))
    # 0.001 * 0.0001 = 0.0000001 -> 0 分
    assert inv.lines[0].line_amount == 0
    assert inv.total_amount == 0


def test_zero_unit_price_ok():
    inv = verify(build_edi([("1", "999", "0.0000")]))
    assert inv.total_amount == 0


def test_tds_implied_decimal_n2():
    # TDS01 为 N2：3375 表示 33.75
    inv = verify(build_edi([("1", "1", "33.75")], tds="3375"))
    assert inv.total_amount == 3375


def test_tds_explicit_decimal():
    inv = verify(build_edi([("1", "1", "33.75")], tds="33.75"))
    assert inv.total_amount == 3375


def test_crlf_after_terminator_stripped():
    inv = verify(build_edi(
        [("1", "1", "1.00")]).replace("~", "~\r\n") + "\r\n")
    assert inv.total_amount == 100


def test_custom_delimiters():
    inv = verify(build_edi([("1", "2", "1.00")], elem="|", comp="^", term="#"))
    assert inv.total_amount == 200


def test_newline_segment_terminator():
    inv = verify(build_edi([("1", "2", "1.00")], term="\n"))
    assert inv.total_amount == 200


def test_100_it1_lines_ok():
    lines = [(str(i), "1", "1.00") for i in range(1, 101)]
    inv = verify(build_edi(lines))
    assert len(inv.lines) == 100
    assert inv.total_amount == 10000


def test_29_digit_quantity_unit_price_1():
    # 回归：默认 Decimal 精度 28 位，29 位整数相乘 + quantize 会抛 InvalidOperation
    qty = "12345678901234567890123456789"  # 恰好 29 位正整数
    assert len(qty) == 29
    edi = build_edi([("1", qty, "1")])
    inv = verify(edi)
    assert inv.lines[0].quantity == qty      # 原文回显
    assert inv.lines[0].unit_price == "1"
    assert inv.lines[0].line_amount == int(qty) * 100
    assert inv.total_amount == int(qty) * 100


def test_big_quantity_with_decimals_round_half_up():
    # 29 位数量、4 位小数单价：乘积远超 28 位，逐行 ROUND_HALF_UP 仍须精确
    qty = "99999999999999999999999999999"  # 29 位
    price = "1.0001"
    expected = _expected_cents(qty, price)
    inv = verify(build_edi([("1", qty, price)]))
    assert inv.lines[0].line_amount == expected
    assert inv.total_amount == expected


def test_big_amount_three_decimal_quantity_tie_rounds_up():
    # 大数边界上的半数进位：...001 * 0.0001 等组合验证 ROUND_HALF_UP 方向
    qty = "1000000000000000000000000000.005"   # 28 位整数 + 3 位小数
    price = "1"
    expected = _expected_cents(qty, price)
    inv = verify(build_edi([("1", qty, price)]))
    assert inv.lines[0].line_amount == expected
    assert inv.total_amount == expected


def test_big_amount_100_lines_sum():
    # 100 行大数求和不受精度影响，报文仍小于 64 KiB
    qty = "1000000000000000000000000001"  # 28 位
    price = "2.5"
    lines = [(str(i), qty, price) for i in range(1, 101)]
    edi = build_edi(lines)
    assert len(edi.encode("ascii")) < MAX_BYTES
    inv = verify(edi)
    per_line = _expected_cents(qty, price)
    assert len(inv.lines) == 100
    assert all(l.line_amount == per_line for l in inv.lines)
    assert inv.total_amount == per_line * 100


def test_big_amount_tds_explicit_decimal():
    qty = "12345678901234567890123456789"
    total = int(qty) * 100
    inv = verify(build_edi([("1", qty, "1")], tds=f"{total // 100}.00"))
    assert inv.total_amount == total


def test_big_amount_tds_mismatch_returns_422():
    qty = "12345678901234567890123456789"
    edi = build_edi([("1", qty, "1")], tds=str(int(qty)))  # 差 100 倍
    e = expect_code(edi, "TDS_MISMATCH")
    assert e.seg is not None and e.segment == "TDS"
    r = post(edi)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "TDS_MISMATCH"
    assert isinstance(err["seg"], int) and err["segment"] == "TDS"


def test_http_big_amount_ok():
    qty = "12345678901234567890123456789"
    r = post(build_edi([("1", qty, "1")]))
    assert r.status_code == 200
    body = r.json()
    assert body["lines"][0]["quantity"] == qty
    assert body["lines"][0]["unit_price"] == "1"
    assert body["lines"][0]["line_amount"] == int(qty) * 100
    assert body["total_amount"] == int(qty) * 100


# ---------- 核心解析：失败路径 ----------

def expect_code(edi, code):
    with pytest.raises(X12Error) as exc:
        verify(edi)
    assert exc.value.code == code
    return exc.value


def test_tds_mismatch():
    e = expect_code(build_edi([("1", "2", "10.00")], tds="1999"), "TDS_MISMATCH")
    assert e.seg is not None and e.segment == "TDS"


def test_isa_iea_control_mismatch():
    edi = build_edi([("1", "1", "1.00")], iea_ctrl="000000999")
    e = expect_code(edi, "ISA_IEA_CONTROL_MISMATCH")
    assert e.segment == "IEA"


def test_gs_ge_control_mismatch():
    edi = build_edi([("1", "1", "1.00")], ge_ctrl="99")
    expect_code(edi, "GS_GE_CONTROL_MISMATCH")


def test_st_se_control_mismatch():
    edi = build_edi([("1", "1", "1.00")], st_ctrl_se="9999")
    expect_code(edi, "ST_SE_CONTROL_MISMATCH")


def test_ge01_not_one():
    edi = build_edi([("1", "1", "1.00")], ge01="2")
    expect_code(edi, "GE01_NOT_ONE")


def test_iea01_not_one():
    edi = build_edi([("1", "1", "1.00")], iea01="2")
    expect_code(edi, "IEA01_NOT_ONE")


def test_se01_wrong_count():
    good = build_edi([("1", "1", "1.00")])
    # SE01 实际应为 5（ST,BIG,IT1,TDS,SE），改成 9
    assert "SE*5*" in good
    bad = good.replace("SE*5*", "SE*9*")
    expect_code(bad, "SE01_WRONG_COUNT")


def test_duplicate_it1():
    e = expect_code(
        build_edi([("1", "1", "1.00"), ("1", "1", "2.00")]),
        "IT101_DUPLICATE",
    )
    assert e.segment == "IT1"


def test_big02_empty():
    edi = isa() + (
        "GS*IN*A*B*20200830*1200*42*X*004010~"
        "ST*810*0001~"
        "BIG*20200830*~"
        "IT1*1*1*EA*1.00~TDS*100~SE*5*0001~GE*1*42~IEA*1*000000001~"
    )
    expect_code(edi, "BIG02_EMPTY")


def test_it102_zero_or_bad_decimals():
    expect_code(build_edi([("1", "0", "1.00")]), "IT102_INVALID")
    expect_code(build_edi([("1", "1.0001", "1.00")]), "IT102_INVALID")


def test_it104_negative_or_bad_decimals():
    expect_code(build_edi([("1", "1", "-1.00")]), "IT104_INVALID")
    expect_code(build_edi([("1", "1", "1.00001")]), "IT104_INVALID")


def test_zero_it1_missing():
    edi = isa() + (
        "GS*IN*A*B*20200830*1200*42*X*004010~"
        "ST*810*0001~BIG*20200830*9001~TDS*0~SE*4*0001~GE*1*42~IEA*1*000000001~"
    )
    expect_code(edi, "IT1_MISSING")


def test_101_it1_too_many():
    lines = [(str(i), "1", "1.00") for i in range(1, 102)]
    expect_code(build_edi(lines), "IT1_TOO_MANY")


def test_bad_versions():
    edi = build_edi([("1", "1", "1.00")])
    expect_code(edi.replace("*00401*", "*00501*"), "ISA12_BAD_VERSION")
    expect_code(edi.replace("*004010~", "*005010X222A1~"),
                "GS08_BAD_VERSION")


def test_wrong_order_and_truncation():
    edi = build_edi([("1", "1", "1.00")])
    expect_code(edi.replace("ST*810*", "ST*820*"), "ST01_NOT_810")
    expect_code(isa() + "XX*1~", "UNEXPECTED_SEGMENT")
    expect_code(isa(), "MISSING_SEGMENT")
    expect_code(isa()[:50], "ISA_TOO_SHORT")
    expect_code("XX" + isa()[2:], "ISA_NOT_FIRST")


def test_trailing_data_and_empty_segment():
    # IEA 终止符之后还有无终止符收尾的数据 -> TRAILING_DATA
    expect_code(build_edi([("1", "1", "1.00")]) + "GARBAGE", "TRAILING_DATA")
    expect_code(isa() + "~~", "EMPTY_SEGMENT")


def test_separator_conflict_and_bad_separator():
    expect_code(isa(elem="~", term="~"), "SEPARATOR_CONFLICT")
    expect_code(isa(elem="A"), "INVALID_ELEMENT_SEPARATOR")


# ---------- HTTP 层 ----------

def post(edi_text):
    return client.post("/v1/verify", json={"edi_text": edi_text})


def test_http_ok():
    r = post(build_edi([("1", "2", "10.50")]))
    assert r.status_code == 200
    body = r.json()
    assert body["invoice_number"] == "9001"
    assert body["lines"][0]["line_amount"] == 2100
    assert body["total_amount"] == 2100


def test_http_422_envelope():
    r = post(build_edi([("1", "1", "1.00")], tds="1"))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "TDS_MISMATCH"
    assert isinstance(err["seg"], int)
    assert err["segment"] == "TDS"


def test_http_ascii_and_size():
    assert post("café").status_code == 422
    assert post(build_edi([("1", "1", "1.00")])[:0]).status_code == 422
    big = "A" * (MAX_BYTES + 1)
    r = post(big)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_http_missing_field_is_422():
    r = client.post("/v1/verify", json={})
    assert r.status_code == 422


def test_health():
    assert client.get("/health").json() == {"status": "ok"}
