"""POST /v1/acknowledge 端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app
from test_verify import build_edi, isa

client = TestClient(app)

ACK = {"isa13": "000000042", "gs06": "77", "st02": "9001"}


def post_ack(edi_text, **ctrl):
    payload = {"edi_text": edi_text, **ACK, **ctrl}
    return client.post("/v1/acknowledge", json=payload)


def split_997(text, elem="*", term="~"):
    """把 997 文本切成 [(tag, [元素...]), ...]。"""
    assert text.endswith(term)
    out = []
    for fragment in text[:-1].split(term):
        fields = fragment.split(elem)
        out.append((fields[0], fields))
    return out


def drop_seg(edi, tag, elem="*", term="~"):
    """按段标识精确删除完整段（不用子串匹配，避免误伤 SENDERGS 等元素）。"""
    isa, rest = edi[:106], edi[106:]
    fragments = rest.split(term)
    assert fragments[-1] == ""
    kept = [f for f in fragments[:-1] if f.split(elem)[0] != tag]
    return isa + term.join(kept) + term


def test_accepted_basic():
    r = post_ack(build_edi([("1", "2", "10.50")]))
    assert r.status_code == 200
    segs = split_997(r.json()["edi_text"])
    tags = [tag for tag, _ in segs]
    assert tags == ["ISA", "GS", "ST", "AK1", "AK2", "AK5", "AK9",
                    "SE", "GE", "IEA"]

    isa_seg = segs[0][1]
    assert isa_seg[6] == "RECEIVER1234567"   # ISA06 = 来报接收方
    assert isa_seg[8] == "SENDER123456789"   # ISA08 = 来报发送方
    assert isa_seg[13] == "000000042"        # ISA13 = 回执控制号

    gs = segs[1][1]
    assert gs[1] == "FA"
    assert gs[2] == "RECVGS" and gs[3] == "SENDERGS"  # 收发方互换
    assert gs[6] == "77" and gs[8] == "004010"

    assert segs[2][1] == ["ST", "997", "9001"]
    assert segs[3][1] == ["AK1", "IN", "42"]          # 来报 GS01/GS06
    assert segs[4][1] == ["AK2", "810", "0001"]       # 来报 ST01/ST02
    assert segs[5][1] == ["AK5", "A"]
    assert segs[6][1] == ["AK9", "A", "1"]            # 接收数 1

    se = segs[7][1]
    assert se[1] == "6" and se[2] == "9001"           # ST..SE 共 6 段
    assert segs[8][1] == ["GE", "1", "77"]
    assert segs[9][1] == ["IEA", "1", "000000042"]


def test_rejected_tds_mismatch_with_ak3():
    edi = build_edi([("1", "2", "10.00")], tds="1999")
    r = post_ack(edi)
    assert r.status_code == 200
    segs = split_997(r.json()["edi_text"])
    tags = [tag for tag, _ in segs]
    assert tags == ["ISA", "GS", "ST", "AK1", "AK2", "AK3", "AK5", "AK9",
                    "SE", "GE", "IEA"]
    # 来报 TDS 段全局序号 6（ISA=1），从 ST 起算位置 4
    assert segs[5][1] == ["AK3", "TDS", "4"]
    assert segs[6][1] == ["AK5", "R"]
    assert segs[7][1] == ["AK9", "R", "0"]            # 接收数 0
    assert segs[8][1][1] == "7"                       # SE01 段数自洽


def test_rejected_ak3_position_from_st():
    # 第 3 个 IT1 行号重复：全局序号 7（ISA=1..IT1=7），从 ST 起算位置 5
    edi = build_edi([("1", "1", "1.00"), ("2", "1", "1.00"),
                     ("1", "1", "2.00")])
    r = post_ack(edi)
    assert r.status_code == 200
    segs = split_997(r.json()["edi_text"])
    assert segs[5][1] == ["AK3", "IT1", "5"]
    assert segs[7][1] == ["AK9", "R", "0"]


# ---------- 可识别来报错误：缺 SE / GS08 非 004010 仍回执 R ----------

def test_rejected_when_se_missing():
    edi = drop_seg(build_edi([("1", "2", "10.00")]), "SE")
    r = post_ack(edi)
    assert r.status_code == 200
    segs = split_997(r.json()["edi_text"])
    tags = [tag for tag, _ in segs]
    # 缺 SE 为组级错误（verifier 在 GE 处报 UNEXPECTED_SEGMENT），无 AK3
    assert tags == ["ISA", "GS", "ST", "AK1", "AK2", "AK5", "AK9",
                    "SE", "GE", "IEA"]
    assert segs[3][1] == ["AK1", "IN", "42"]             # 仍引用原 GS06
    assert segs[4][1] == ["AK2", "810", "0001"]          # 仍引用原 ST02
    assert segs[5][1] == ["AK5", "R"]
    assert segs[6][1] == ["AK9", "R", "0"]
    assert segs[7][1][1] == "6"                          # SE01 段数自洽
    assert segs[8][1] == ["GE", "1", "77"]
    assert segs[9][1] == ["IEA", "1", "000000042"]


def test_rejected_when_gs08_bad_version():
    edi = build_edi([("1", "1", "1.00")]).replace(
        "*004010~", "*005010~")
    r = post_ack(edi)
    assert r.status_code == 200
    segs = split_997(r.json()["edi_text"])
    tags = [tag for tag, _ in segs]
    # GS08 错误落在 GS（组级），不输出 AK3
    assert tags == ["ISA", "GS", "ST", "AK1", "AK2", "AK5", "AK9",
                    "SE", "GE", "IEA"]
    assert segs[3][1] == ["AK1", "IN", "42"]
    assert segs[4][1] == ["AK2", "810", "0001"]
    assert segs[1][1][8] == "004010"                     # 回执自身仍是 004010
    assert segs[5][1] == ["AK5", "R"]
    assert segs[6][1] == ["AK9", "R", "0"]


def test_rejected_ak3_when_se_malformed():
    # SE 段元素不足：SE 是交易集内段（全局 7 -> 从 ST 起算 5），输出 AK3
    edi = build_edi([("1", "1", "1.00")]).replace(
        "SE*5*0001~", "SE~")
    r = post_ack(edi)
    assert r.status_code == 200
    segs = split_997(r.json()["edi_text"])
    assert segs[5][1] == ["AK3", "SE", "5"]
    assert segs[6][1] == ["AK5", "R"]
    assert segs[7][1] == ["AK9", "R", "0"]
    assert segs[8][1][1] == "7"                          # 含 AK3 时段数自洽


def test_rejected_no_se_with_custom_delimiters():
    edi = drop_seg(
        build_edi([("1", "2", "1.00")], elem="|", comp="^", term="#"),
        "SE", elem="|", term="#")
    r = post_ack(edi)
    assert r.status_code == 200
    text = r.json()["edi_text"]
    assert text.endswith("#")
    assert "|" in text and "*" not in text
    segs = split_997(text, elem="|", term="#")
    assert segs[5][1] == ["AK5", "R"]
    assert segs[6][1] == ["AK9", "R", "0"]


def test_rejected_ak3_still_locatable_when_se_missing():
    # 缺 SE 且 TDS 与行合计不一致：先报交易集内的 TDS_MISMATCH，应带 AK3
    edi = drop_seg(build_edi([("1", "2", "10.00")], tds="1999"), "SE")
    r = post_ack(edi)
    assert r.status_code == 200
    segs = split_997(r.json()["edi_text"])
    assert segs[5][1] == ["AK3", "TDS", "4"]
    assert segs[7][1] == ["AK9", "R", "0"]


def test_custom_delimiters_reused():
    edi = build_edi([("1", "2", "1.00")], elem="|", comp="^", term="#")
    r = post_ack(edi)
    assert r.status_code == 200
    text = r.json()["edi_text"]
    assert text.endswith("#")
    assert "|" in text and "*" not in text
    segs = split_997(text, elem="|", term="#")
    assert segs[0][1][0] == "ISA"
    assert segs[5][1] == ["AK5", "A"]
    assert segs[8][1] == ["GE", "1", "77"]


def test_newline_terminator_reused():
    edi = build_edi([("1", "2", "1.00")], term="\n")
    r = post_ack(edi)
    assert r.status_code == 200
    text = r.json()["edi_text"]
    assert text.endswith("\n")
    segs = split_997(text, term="\n")
    assert segs[5][1] == ["AK5", "A"]


def test_control_numbers_self_consistent():
    r = post_ack(build_edi([("1", "1", "1.00")]),
                 isa13="123456789", gs06="G-9", st02="S.7")
    assert r.status_code == 200
    segs = split_997(r.json()["edi_text"])
    assert segs[2][1][2] == segs[7][1][2] == "S.7"    # ST02 == SE02
    assert segs[1][1][6] == segs[8][1][2] == "G-9"    # GS06 == GE02
    assert segs[0][1][13] == segs[9][1][2] == "123456789"  # ISA13 == IEA02


# ---------- 422：外壳不可回执，返回原错误码 ----------

def _expect_422(edi_text, code):
    r = post_ack(edi_text)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == code


def test_422_original_code_when_envelope_unusable():
    _expect_422(isa(), "MISSING_SEGMENT")                       # 无 GE/IEA
    _expect_422(isa()[:50], "ISA_TOO_SHORT")
    _expect_422("XX" + isa()[2:], "ISA_NOT_FIRST")
    _expect_422("café", "NOT_ASCII")
    _expect_422("", "EMPTY_INPUT")
    _expect_422(build_edi([("1", "1", "1.00")]) + "GARBAGE",
                "TRAILING_DATA")
    _expect_422(isa() + "~~", "EMPTY_SEGMENT")


def test_422_original_code_when_ids_not_extractable():
    # GS06 为空：外壳虽完整，但回执无法引用组控制号
    edi = build_edi([("1", "1", "1.00")], gs_ctrl="")
    _expect_422(edi, "ACK_ENVELOPE_UNUSABLE")


def test_422_original_code_when_not_810():
    edi = build_edi([("1", "1", "1.00")]).replace("ST*810*", "ST*820*")
    _expect_422(edi, "ST01_NOT_810")


def test_422_original_code_when_ge_or_iea_missing():
    # 缺 GE：verifier 在 IEA 处报 UNEXPECTED_SEGMENT，保留原错误码
    _expect_422(drop_seg(build_edi([("1", "1", "1.00")]), "GE"),
                "UNEXPECTED_SEGMENT")
    # 缺 IEA：未正常闭合 -> MISSING_SEGMENT
    _expect_422(drop_seg(build_edi([("1", "1", "1.00")]), "IEA"),
                "MISSING_SEGMENT")
    edi = drop_seg(drop_seg(build_edi([("1", "1", "1.00")]), "GE"), "IEA")
    _expect_422(edi, "MISSING_SEGMENT")


# ---------- 422：回执控制号非法 ----------

def test_422_isa13_invalid():
    for bad in ("", "12345678", "1234567890", "abcdefghi", "00000004 "):
        r = post_ack(build_edi([("1", "1", "1.00")]), isa13=bad)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "ACK_ISA13_INVALID"


def test_422_gs06_st02_invalid():
    good = build_edi([("1", "1", "1.00")])
    for field, bad in (("gs06", ""), ("st02", ""),
                       ("gs06", "a*b"), ("st02", "a~b"),
                       ("gs06", "a:b"), ("st02", "a\nb"),
                       ("gs06", "组号")):
        r = post_ack(good, **{field: bad})
        assert r.status_code == 422, (field, bad)
        code = r.json()["error"]["code"]
        assert code in ("ACK_GS06_EMPTY", "ACK_GS06_INVALID",
                        "ACK_ST02_EMPTY", "ACK_ST02_INVALID")


def test_422_missing_request_fields():
    assert client.post("/v1/acknowledge", json={}).status_code == 422
    assert client.post("/v1/acknowledge",
                       json={"edi_text": "x"}).status_code == 422


# ---------- 旧接口保持可用 ----------

def test_existing_endpoints_unaffected():
    edi = build_edi([("1", "2", "10.50")])
    r = client.post("/v1/verify", json={"edi_text": edi})
    assert r.status_code == 200
    assert r.json()["total_amount"] == 2100
    r = client.post("/v1/reconcile", json={
        "order_lines": [{"line_id": "1", "ordered_quantity": "2",
                         "unit_price": "10.50"}],
        "edi_texts": [edi],
    })
    assert r.status_code == 200
    assert r.json()["results"][0]["accepted"] is True
    assert client.get("/health").json() == {"status": "ok"}
