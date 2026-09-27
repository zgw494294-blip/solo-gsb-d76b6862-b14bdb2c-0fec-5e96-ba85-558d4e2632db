"""POST /v1/split-810 端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app
from app.splitter import MAX_SPLIT_BYTES, MAX_TRANSACTIONS
from app.verifier import verify_text
from test_verify import build_edi, isa

client = TestClient(app)


# ---------- 批量报文构造工具 ----------


def seg(tag, *values, elem="*", term="~"):
    return elem.join((tag, *values)) + term


def build_batch(items, *, ge01=None, iea01="1", gs_ctrl="42",
                ctrl="000000001", ge_ctrl=None, iea_ctrl=None,
                elem="*", comp=":", term="~"):
    """组装含多个 810 交易集的交换报文。

    items: [(st02, invoice_number, lines, tds_override?)]，lines 同 build_edi。
    GE01 默认按实际交易集数填写。
    """
    from decimal import Decimal, ROUND_HALF_UP, localcontext

    def expected_cents(qty, price):
        with localcontext() as ctx:
            ctx.prec = 200
            return int((Decimal(qty) * Decimal(price)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)

    ge_ctrl = gs_ctrl if ge_ctrl is None else ge_ctrl
    iea_ctrl = ctrl if iea_ctrl is None else iea_ctrl

    parts = [
        isa(ctrl, elem, comp, term),
        seg("GS", "IN", "SENDERGS", "RECVGS", "20200830", "1200",
            gs_ctrl, "X", "004010", elem=elem, term=term),
    ]
    n = 0
    for entry in items:
        st02, number, lines = entry[0], entry[1], entry[2]
        tds = entry[3] if len(entry) > 3 else str(
            sum(expected_cents(qty, price) for _lid, qty, price in lines))
        parts.append(seg("ST", "810", st02, elem=elem, term=term))
        parts.append(seg("BIG", "20200830", number, elem=elem, term=term))
        for lid, qty, price in lines:
            parts.append(seg("IT1", lid, qty, "EA", price,
                             elem=elem, term=term))
        parts.append(seg("TDS", tds, elem=elem, term=term))
        parts.append(seg("SE", str(4 + len(lines)), st02,
                         elem=elem, term=term))
        n += 1
    parts.append(seg("GE", str(n if ge01 is None else ge01), ge_ctrl,
                     elem=elem, term=term))
    parts.append(seg("IEA", iea01, iea_ctrl, elem=elem, term=term))
    return "".join(parts)


def single_edi(st02, number, lines, *, gs_ctrl="42", ctrl="000000001",
               elem="*", comp=":", term="~"):
    """与 build_batch 单张输出完全一致的单张报文（用于比对重建结果）。"""
    return build_edi(
        lines, ctrl=ctrl, gs_ctrl=gs_ctrl, st_ctrl=st02,
        elem=elem, comp=comp, term=term,
    ).replace("BIG*20200830*9001~", f"BIG*20200830*{number}~")


def post_split(edi_text):
    return client.post("/v1/split-810", json={"edi_text": edi_text})


def expect_422(edi_text, code, **fields):
    r = post_split(edi_text)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == code, (code, err["code"])
    for key, value in fields.items():
        assert err[key] == value, (key, err[key], value)
    return err


# ---------- 成功路径 ----------


def test_happy_path_three_sets():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "3", "4.25"), ("2", "1", "1.00")]),
        ("0003", "INV-3", [("1", "1", "19.99")]),
    ])
    r = post_split(edi)
    assert r.status_code == 200, r.text
    invoices = r.json()["invoices"]
    assert [x["set_index"] for x in invoices] == [0, 1, 2]
    assert [x["st02"] for x in invoices] == ["0001", "0002", "0003"]
    assert [x["invoice_number"] for x in invoices] == ["INV-1", "INV-2", "INV-3"]
    # 顺序与内容
    expected = [single_edi("0001", "INV-1", [("1", "2", "10.50")]),
                single_edi("0002", "INV-2", [("1", "3", "4.25"),
                                             ("2", "1", "1.00")]),
                single_edi("0003", "INV-3", [("1", "1", "19.99")])]
    assert [x["edi_text"] for x in invoices] == expected
    # 每张都能再通过 /v1/verify
    for x in invoices:
        assert len(x["edi_text"].encode("ascii")) <= 64 * 1024
        inv = verify_text(x["edi_text"])
        assert inv.invoice_number == x["invoice_number"]
        vr = client.post("/v1/verify", json={"edi_text": x["edi_text"]})
        assert vr.status_code == 200
        assert vr.json()["invoice_number"] == x["invoice_number"]


def test_single_set_rebuilt_identical():
    edi = build_batch([("0001", "INV-1", [("1", "2", "10.50")])])
    r = post_split(edi)
    assert r.status_code == 200
    assert r.json()["invoices"][0]["edi_text"] == edi


def test_50_sets_ok():
    edi = build_batch([
        (f"{i:04d}", f"INV-{i}", [("1", "1", "1.00")]) for i in range(1, 51)
    ])
    r = post_split(edi)
    assert r.status_code == 200, r.text
    invoices = r.json()["invoices"]
    assert len(invoices) == MAX_TRANSACTIONS
    assert [x["st02"] for x in invoices] == [f"{i:04d}" for i in range(1, 51)]
    assert [x["set_index"] for x in invoices] == list(range(50))
    assert all(len(x["edi_text"].encode()) <= 64 * 1024 for x in invoices)


def test_custom_delimiters_preserved():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ], elem="|", comp="^", term="#")
    r = post_split(edi)
    assert r.status_code == 200, r.text
    invoices = r.json()["invoices"]
    assert all(x["edi_text"].startswith("ISA|") for x in invoices)
    assert all(x["edi_text"].endswith("#") for x in invoices)
    for x in invoices:
        verify_text(x["edi_text"])


def test_newline_segment_terminator():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ], term="\n")
    r = post_split(edi)
    assert r.status_code == 200
    assert len(r.json()["invoices"]) == 2


def test_crlf_after_terminators_stripped_on_rebuild():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ]).replace("~", "~\r\n") + "\r\n"
    r = post_split(edi)
    assert r.status_code == 200
    for x in r.json()["invoices"]:
        assert "\r" not in x["edi_text"]
        assert verify_text(x["edi_text"])


def test_wrong_original_se01_is_rewritten_not_rejected():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ]).replace("SE*5*0001~", "SE*9*0001~")
    r = post_split(edi)
    assert r.status_code == 200, r.text
    assert "SE*5*0001~" in r.json()["invoices"][0]["edi_text"]


def test_ge01_in_batch_equals_set_count_is_required():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ], ge01="1")
    expect_422(edi, "GE01_COUNT_MISMATCH", seg=13, segment="GE", set_index=None)


def test_each_rebuilt_has_ge01_one_and_iea01_one():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ])
    for x in post_split(edi).json()["invoices"]:
        assert "GE*1*42~" in x["edi_text"]
        assert "IEA*1*000000001~" in x["edi_text"]


# ---------- 批量外壳错误 ----------


def test_empty_and_ascii_and_size():
    expect_422("", "EMPTY_INPUT", seg=None, set_index=None)
    expect_422("café", "NOT_ASCII")
    expect_422("A" * (MAX_SPLIT_BYTES + 1), "PAYLOAD_TOO_LARGE")
    expect_422("XX" + isa()[2:], "ISA_NOT_FIRST")
    expect_422(isa()[:50], "ISA_TOO_SHORT")


def test_bad_versions():
    edi = build_batch([("0001", "INV-1", [("1", "1", "1.00")])])
    expect_422(edi.replace("*00401*", "*00501*"), "ISA12_BAD_VERSION",
               seg=1, segment="ISA", set_index=None)
    expect_422(edi.replace("*004010~", "*005010~"), "GS08_BAD_VERSION",
               seg=2, segment="GS", set_index=None)


def test_control_number_pairing():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ])
    expect_422(edi.replace("IEA*1*000000001~", "IEA*1*000000999~"),
               "ISA_IEA_CONTROL_MISMATCH", segment="IEA", set_index=None)
    expect_422(edi.replace("GE*2*42~", "GE*2*99~"),
               "GS_GE_CONTROL_MISMATCH", segment="GE", set_index=None)
    expect_422(edi.replace("SE*5*0002~", "SE*5*9999~"),
               "ST_SE_CONTROL_MISMATCH", set_index=1, segment="SE")


def test_iea01_must_be_one():
    edi = build_batch([("0001", "INV-1", [("1", "1", "1.00")])], iea01="2")
    expect_422(edi, "IEA01_NOT_ONE", segment="IEA", set_index=None)


def test_ge01_non_numeric():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ]).replace("GE*2*42~", "GE*X*42~")
    expect_422(edi, "GE01_INVALID", segment="GE")


def test_st01_must_be_810():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ]).replace("ST*810*0002~", "ST*820*0002~")
    expect_422(edi, "ST01_NOT_810", set_index=1, segment="ST")


def test_st02_empty_or_duplicate():
    good = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ])
    expect_422(good.replace("ST*810*0002~", "ST*810*~"),
               "ST02_EMPTY", set_index=1, segment="ST")
    dup = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0001", "INV-2", [("1", "1", "1.00")]),
    ])
    expect_422(dup, "ST02_DUPLICATE", set_index=1, segment="ST")


def test_too_many_transactions():
    edi = build_batch([
        (f"{i:04d}", f"INV-{i}", [("1", "1", "1.00")]) for i in range(1, 52)
    ])
    expect_422(edi, "TS_TOO_MANY", set_index=50, segment="ST")


def test_zero_transactions_empty_group():
    edi = (isa()
           + "GS*IN*S*R*20200830*1200*42*X*004010~"
           + "GE*0*42~IEA*1*000000001~")
    expect_422(edi, "TS_MISSING", segment="GE", set_index=None)


def test_missing_segments():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ])
    expect_422(isa(), "MISSING_SEGMENT")
    expect_422(
        isa() + "GS*IN*S*R*20200830*1200*42*X*004010~",
        "MISSING_SEGMENT")
    expect_422(edi.rsplit("GE*2*42~", 1)[0], "MISSING_SEGMENT")
    expect_422(edi.rsplit("IEA*1*000000001~", 1)[0], "MISSING_SEGMENT")


def test_missing_se_inside_set():
    edi = (
        isa()
        + "GS*IN*S*R*20200830*1200*42*X*004010~"
        + "ST*810*0001~BIG*20200830*INV1~IT1*1*2*EA*10.50~TDS*2100~"
        + "GE*1*42~IEA*1*000000001~"
    )
    expect_422(edi, "UNEXPECTED_SEGMENT", set_index=0, segment="GE")


def test_envelope_tag_inside_transaction_body():
    edi = (
        isa()
        + "GS*IN*S*R*20200830*1200*42*X*004010~"
        + "ST*810*0001~BIG*20200830*INV1~ST*810*0002~"
        + "TDS*2100~SE*5*0001~GE*1*42~IEA*1*000000001~"
    )
    expect_422(edi, "UNEXPECTED_SEGMENT", set_index=0, segment="ST")


def test_trailing_data_and_empty_segment():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ])
    expect_422(edi + "GARBAGE", "TRAILING_DATA")
    expect_422(isa() + "~~", "EMPTY_SEGMENT")


def test_separator_errors():
    edi_tail = build_batch([("0001", "INV-1", [("1", "1", "1.00")])])[106:]
    expect_422(isa(elem="~", term="~"), "SEPARATOR_CONFLICT")
    expect_422(isa(elem="A") + edi_tail, "INVALID_ELEMENT_SEPARATOR")


def test_se01_non_numeric_is_batch_error():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ]).replace("SE*5*0001~", "SE*X*0001~")
    expect_422(edi, "SE01_INVALID", set_index=0, segment="SE", seg=7)


# ---------- 逐张 /v1/verify 业务核验（整批 422 + 原报文段序号） ----------


def test_tds_mismatch_in_second_set_maps_original_seg():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "3", "4.25")], "9999"),
        ("0003", "INV-3", [("1", "1", "19.99")]),
    ])
    # ISA=1 GS=2；set1: ST3 BIG4 IT5 TDS6 SE7；set2: ST8 BIG9 IT10 TDS11
    err = expect_422(edi, "TDS_MISMATCH",
                     set_index=1, segment="TDS", seg=11)
    assert "9999" in err["message"]


def test_it101_duplicate_inside_set_maps_original_seg():
    edi = (
        isa()
        + "GS*IN*S*R*20200830*1200*42*X*004010~"
        + "ST*810*0001~BIG*20200830*INV1~IT1*1*2*EA*10.50~TDS*2100~SE*5*0001~"
        + "ST*810*0002~BIG*20200830*INV2~"
        + "IT1*1*2*EA*10.50~IT1*1*1*EA*1.00~TDS*3100~SE*6*0002~"
        + "GE*2*42~IEA*1*000000001~"
    )
    # set2: ST=8 BIG=9 IT1=10 IT1(dup)=11
    expect_422(edi, "IT101_DUPLICATE", set_index=1, segment="IT1", seg=11)


def test_big02_empty_maps_original_seg():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "1", "1.00")]),
    ]).replace("BIG*20200830*INV-2~", "BIG*20200830*~")
    # set2: ST=8 BIG=9
    expect_422(edi, "BIG02_EMPTY", set_index=1, segment="BIG", seg=9)


def test_business_failure_returns_no_partial_results():
    edi = build_batch([
        ("0001", "INV-1", [("1", "2", "10.50")]),
        ("0002", "INV-2", [("1", "3", "4.25")], "1"),  # TDS 错误
    ])
    r = post_split(edi)
    assert r.status_code == 422
    body = r.json()
    assert "invoices" not in body
    assert set(body["error"].keys()) == {"code", "message", "seg",
                                         "segment", "set_index"}


def test_it1_too_many_maps_original_seg():
    lines1 = [("1", "1", "1.00")]
    set1 = ("ST*810*0001~BIG*20200830*INV1~"
            + "".join(f"IT1*{i}*1*EA*1.00~" for i in range(1, 102))
            + "TDS*10100~SE*105*0001~")
    edi = (
        isa()
        + "GS*IN*S*R*20200830*1200*42*X*004010~"
        + set1
        + "ST*810*0002~BIG*20200830*INV2~IT1*1*1*EA*1.00~TDS*100~SE*5*0002~"
        + "GE*2*42~IEA*1*000000001~"
    )
    # ISA=1 GS=2 ST=3 BIG=4，第 101 个 IT1 位于段 5+100 = 105
    expect_422(edi, "IT1_TOO_MANY", set_index=0, segment="IT1", seg=105)


def test_rebuilt_single_set_over_64kib_fails_whole_batch():
    big_line = "IT1*" + "1" * 20 + "*1*EA*1.00~"
    body = big_line * 4000  # 约 136 KiB 业务段；批量整体仍 ≤ 1 MiB
    edi = (
        isa()
        + "GS*IN*S*R*20200830*1200*42*X*004010~"
        + "ST*810*0001~BIG*20200830*INV1~" + body
        + f"TDS*0~SE*{body.count('~') + 3}*0001~"
        + "GE*1*42~IEA*1*000000001~"
    )
    assert len(edi.encode("ascii")) <= MAX_SPLIT_BYTES
    expect_422(edi, "PAYLOAD_TOO_LARGE", set_index=0, seg=None, segment=None)


# ---------- 请求模型 ----------


def test_missing_field_is_422():
    r = client.post("/v1/split-810", json={})
    assert r.status_code == 422


def test_existing_endpoints_unchanged():
    # /v1/verify 单张行为不变
    edi = single_edi("0001", "INV-1", [("1", "2", "10.50")])
    r = client.post("/v1/verify", json={"edi_text": edi})
    assert r.status_code == 200
    assert r.json()["invoice_number"] == "INV-1"
    assert client.get("/health").json() == {"status": "ok"}


def test_batch_with_50_full_sets_each_under_64kib():
    # 50 张、每张 100 个 IT1：批量报文约 150 KiB（≤1 MiB），每张均 ≤64 KiB
    edi = build_batch([
        (f"{i:04d}", f"INV-{i}", [(str(j), "1", "1.00") for j in range(1, 101)])
        for i in range(1, 51)
    ])
    assert len(edi.encode("ascii")) <= MAX_SPLIT_BYTES
    r = post_split(edi)
    assert r.status_code == 200, r.text
    invoices = r.json()["invoices"]
    assert len(invoices) == 50
    assert all(len(x["edi_text"].encode("ascii")) <= 64 * 1024
               for x in invoices)
    assert all(len(client.post("/v1/verify", json={"edi_text": x["edi_text"]}).json()["lines"]) == 100
               for x in invoices)
