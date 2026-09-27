"""POST /v1/split-810 端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app
from app.splitter import MAX_BATCH_BYTES, MAX_TRANSACTION_SETS
from app.verifier import MAX_BYTES, verify_text
from test_verify import _expected_cents, isa

client = TestClient(app)


# ---------- 批量报文构造工具 ----------


def build_set(number, lines, *, st_ctrl="0001", tds=None, elem="*", term="~",
              st01="810", se_ctrl=None, se01=None, big=True, se=True,
              extra=()):
    """组装一个 ST…SE 交易集文本（不含 ISA/GS/GE/IEA）。"""
    def seg(*fields):
        return elem.join(fields) + term

    if tds is None:
        tds = str(sum(_expected_cents(qty, price)
                      for _lid, qty, price in lines))
    se_ctrl = st_ctrl if se_ctrl is None else se_ctrl

    parts = [seg("ST", st01, st_ctrl)]
    if big:
        parts.append(seg("BIG", "20200830", number))
    for et in extra:
        parts.append(seg(*et))
    for lid, qty, price in lines:
        parts.append(seg("IT1", lid, qty, "EA", price))
    parts.append(seg("TDS", tds))
    if se:
        count = se01 if se01 is not None else str(len(parts) + 1)
        parts.append(seg("SE", count, se_ctrl))
    return "".join(parts)


def build_batch(set_texts, *, ctrl="000000001", gs_ctrl="42", elem="*",
                comp=":", term="~", ge01=None, iea01="1", ge_ctrl=None,
                iea_ctrl=None, gs08="004010", include_gs=True,
                include_ge=True, include_iea=True):
    """把若干交易集包进 ISA/GS…GE/IEA 批量交换报文。"""
    def seg(*fields):
        return elem.join(fields) + term

    if ge01 is None:
        ge01 = str(len(set_texts))
    ge_ctrl = gs_ctrl if ge_ctrl is None else ge_ctrl
    iea_ctrl = ctrl if iea_ctrl is None else iea_ctrl

    out = isa(ctrl, elem, comp, term)
    if include_gs:
        out += seg("GS", "IN", "SENDERGS", "RECVGS", "20200830", "1200",
                   gs_ctrl, "X", gs08)
    out += "".join(set_texts)
    if include_ge:
        out += seg("GE", ge01, ge_ctrl)
    if include_iea:
        out += seg("IEA", iea01, iea_ctrl)
    return out


def set_one(number, st_ctrl, **kw):
    """单行明细的标准交易集：ST,BIG,IT1,TDS,SE 共 5 段。"""
    return build_set(number, [(number[0] if number[0].isdigit() else "1",
                               "2", "10.00")], st_ctrl=st_ctrl, **kw)


def split(edi_text):
    return client.post("/v1/split-810", json={"edi_text": edi_text})


def assert_error(r, code, *, seg=..., segment=None, transaction_set=None):
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == code, err
    if seg is not ...:
        assert err["seg"] == seg, err
    assert err["segment"] == segment, err
    assert err["transaction_set"] == transaction_set, err
    # 失败不返回部分结果
    assert "invoices" not in r.json()


# ---------- 成功路径 ----------


def test_happy_path_two_sets_preserves_order():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "19.99")], st_ctrl="0001"),
        build_set("INV-2", [("1", "3", "4.25"), ("2", "1", "4.25")],
                  st_ctrl="0002"),
    ])
    r = split(edi)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert [x["index"] for x in body["invoices"]] == [0, 1]
    assert [x["st02"] for x in body["invoices"]] == ["0001", "0002"]
    assert [x["invoice_number"] for x in body["invoices"]] == [
        "INV-1", "INV-2"]


def test_single_set_round_trips_byte_identical():
    edi = build_batch([build_set("INV-1", [("1", "2", "19.99")],
                                 st_ctrl="0001")])
    r = split(edi)
    assert r.status_code == 200, r.text
    out = r.json()["invoices"][0]["edi_text"]
    # 原报文本身就是 GE01=1 的单张报文：重建结果应逐字节一致
    assert out == edi
    verify_text(out)  # 可由 /v1/verify 接收


def test_each_rebuilt_text_passes_verify_http():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "19.99")], st_ctrl="0001"),
        build_set("INV-2", [("1", "3", "4.25")], st_ctrl="0002"),
    ])
    items = split(edi).json()["invoices"]
    for item in items:
        text = item["edi_text"]
        assert len(text.encode("ascii")) <= MAX_BYTES
        r = client.post("/v1/verify", json={"edi_text": text})
        assert r.status_code == 200, r.text
        assert r.json()["invoice_number"] == item["invoice_number"]
    # 每张重建报文 GE01=1、IEA01=1
    assert all("GE*1*42~IEA*1*000000001~" in x["edi_text"] for x in items)


def test_rebuilt_ge01_rewritten_to_one():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002"),
    ])
    items = split(edi).json()["invoices"]
    for item in items:
        assert item["edi_text"].count("GE*1*42~") == 1
        assert "GE*2*42~" not in item["edi_text"]


def test_preserves_business_segments_with_custom_delimiters():
    elem, comp, term = "|", "^", "#"
    s1 = build_set("INV-1", [("L1", "2", "19.99"), ("L2", "3", "0.50")],
                   st_ctrl="AAA", elem=elem, term=term)
    s2 = build_set("INV-2", [("9", "1", "4.25")], st_ctrl="BBB",
                   elem=elem, term=term)
    edi = build_batch([s1, s2], elem=elem, comp=comp, term=term)
    r = split(edi)
    assert r.status_code == 200, r.text
    items = r.json()["invoices"]
    assert [x["st02"] for x in items] == ["AAA", "BBB"]
    first = items[0]["edi_text"]
    # ISA 固定 106 字节，自定义分隔符原样沿用
    assert first[:3] == "ISA"
    assert first[3] == "|" and chr(ord(first[104:105])) == "^"
    assert first[105] == "#"
    assert len(first[:first.index(term) + 1]) == 106
    # 业务段原文保留（ST…SE 片段逐字节来自原报文）
    assert ("ST|810|AAA#BIG|20200830|INV-1#IT1|L1|2|EA|19.99#"
            "IT1|L2|3|EA|0.50#TDS|4148#SE|6|AAA#") in first
    verify_text(first)
    verify_text(items[1]["edi_text"])


def test_crlf_typesetting_whitespace_accepted():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "19.99")], st_ctrl="0001"),
        build_set("INV-2", [("1", "3", "4.25")], st_ctrl="0002"),
    ]).replace("~", "~\r\n") + "\r\n"
    r = split(edi)
    assert r.status_code == 200, r.text
    for item in r.json()["invoices"]:
        assert "\r" not in item["edi_text"]
        assert "\n" not in item["edi_text"]
        verify_text(item["edi_text"])


def test_fifty_sets_accepted():
    sets = [build_set(f"INV-{i:02d}", [(str(i), "1", "1.00")],
                      st_ctrl=f"{i:04d}") for i in range(1, 51)]
    edi = build_batch(sets, ge01="50")
    r = split(edi)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 50
    assert [x["st02"] for x in body["invoices"]] == [
        f"{i:04d}" for i in range(1, 51)]
    assert [x["invoice_number"] for x in body["invoices"]] == [
        f"INV-{i:02d}" for i in range(1, 51)]


# ---------- 批外壳：交易集数量 ----------


def test_fifty_one_sets_rejected():
    sets = [build_set(f"INV-{i:02d}", [("1", "1", "1.00")],
                      st_ctrl=f"{i:04d}") for i in range(1, 52)]
    edi = build_batch(sets, ge01="51")
    r = split(edi)
    # 第 51 个 ST：ISA=1, GS=2，每个单行集 5 段 -> 3 + 50*5 = 253
    assert_error(r, "SPLIT_TOO_MANY_TRANSACTION_SETS",
                 seg=253, segment="ST", transaction_set=51)


def test_zero_sets_rejected():
    edi = build_batch([], ge01="0")
    r = split(edi)
    assert_error(r, "SPLIT_NO_TRANSACTION_SETS",
                 seg=3, segment="GE", transaction_set=None)


def test_ge01_count_mismatch():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002"),
    ], ge01="3")
    r = split(edi)
    # ISA1 GS2 集1:3-7 集2:8-12 GE=13
    assert_error(r, "GE01_COUNT_MISMATCH",
                 seg=13, segment="GE", transaction_set=None)


# ---------- 批外壳：控制号配对 / 版本 / 收尾 ----------


def test_isa_iea_control_mismatch():
    edi = build_batch([set_one("A", "0001")], iea_ctrl="000000999")
    # 单集：ISA1 GS2 ST3 BIG4 IT1_5 TDS6 SE7 GE8 IEA9
    assert_error(split(edi), "ISA_IEA_CONTROL_MISMATCH",
                 seg=9, segment="IEA", transaction_set=None)


def test_gs_ge_control_mismatch():
    edi = build_batch([set_one("A", "0001")], ge_ctrl="99")
    assert_error(split(edi), "GS_GE_CONTROL_MISMATCH",
                 seg=8, segment="GE", transaction_set=None)


def test_st_se_control_mismatch_second_set():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002",
                  se_ctrl="9999"),
    ])
    # 集2 SE 全局段号：3+5+4 = 12
    assert_error(split(edi), "ST_SE_CONTROL_MISMATCH",
                 seg=12, segment="SE", transaction_set=2)


def test_iea01_not_one():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002"),
    ], iea01="2")
    assert_error(split(edi), "IEA01_NOT_ONE",
                 seg=14, segment="IEA", transaction_set=None)


def test_ge01_invalid():
    edi = build_batch([set_one("A", "0001")], ge01="X")
    assert_error(split(edi), "GE01_INVALID",
                 seg=8, segment="GE", transaction_set=None)


def test_iea01_invalid():
    edi = build_batch([set_one("A", "0001")], iea01="X")
    assert_error(split(edi), "IEA01_INVALID",
                 seg=9, segment="IEA", transaction_set=None)


def test_isa12_bad_version():
    edi = build_batch([set_one("A", "0001")]).replace("*00401*", "*00501*", 1)
    assert_error(split(edi), "ISA12_BAD_VERSION",
                 seg=1, segment="ISA", transaction_set=None)


def test_gs08_bad_version():
    edi = build_batch([set_one("A", "0001")], gs08="005010")
    assert_error(split(edi), "GS08_BAD_VERSION",
                 seg=2, segment="GS", transaction_set=None)


def test_st01_not_810_second_set():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002",
                  st01="850"),
    ])
    assert_error(split(edi), "ST01_NOT_810",
                 seg=8, segment="ST", transaction_set=2)


def test_st02_duplicate():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0001"),
    ])
    assert_error(split(edi), "ST02_DUPLICATE",
                 seg=8, segment="ST", transaction_set=2)


def test_st02_empty():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl=""),
    ])
    assert_error(split(edi), "ST02_EMPTY",
                 seg=8, segment="ST", transaction_set=2)


def test_stray_envelope_segment_inside_set():
    # 集1 的 BIG 之后、SE 之前又出现一个 ST
    bad = build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001",
                    extra=[("ST", "810", "7777")])
    edi = build_batch([
        bad,
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002"),
    ])
    # ISA1 GS2 ST3 BIG4 stray-ST5
    assert_error(split(edi), "UNEXPECTED_SEGMENT",
                 seg=5, segment="ST", transaction_set=1)


def test_missing_se_in_second_set():
    no_se = build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002", se=False)
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        no_se,
    ])
    # 集2：ST8 BIG9 IT1_10 TDS11，随后 GE=12
    assert_error(split(edi), "MISSING_SEGMENT",
                 seg=12, segment="GE", transaction_set=2)


def test_missing_ge():
    edi = build_batch([set_one("A", "0001")], include_ge=False)
    assert_error(split(edi), "UNEXPECTED_SEGMENT",
                 seg=8, segment="IEA", transaction_set=None)


def test_missing_iea():
    edi = build_batch([set_one("A", "0001")], include_iea=False)
    assert_error(split(edi), "MISSING_SEGMENT",
                 seg=..., segment=None, transaction_set=None)


def test_extra_segment_after_iea():
    edi = build_batch([set_one("A", "0001")]) + "ZZ*X~"
    # ISA1 GS2 ST3 BIG4 IT1_5 TDS6 SE7 GE8 IEA9 ZZ10
    assert_error(split(edi), "UNEXPECTED_SEGMENT",
                 seg=10, segment="ZZ", transaction_set=None)


def test_trailing_data_after_terminator():
    edi = build_batch([set_one("A", "0001")]) + "GARBAGE"
    assert_error(split(edi), "TRAILING_DATA",
                 seg=..., segment=None, transaction_set=None)


def test_empty_segment():
    edi = build_batch([set_one("A", "0001")]).replace(
        "ST*810*0001~", "ST*810*0001~~", 1)
    assert_error(split(edi), "EMPTY_SEGMENT",
                 seg=..., segment=None, transaction_set=None)


def test_missing_gs():
    edi = build_batch([set_one("A", "0001")], include_gs=False)
    assert_error(split(edi), "UNEXPECTED_SEGMENT",
                 seg=2, segment="ST", transaction_set=None)


# ---------- 输入预检 ----------


def test_empty_input():
    assert_error(split(""), "EMPTY_INPUT",
                 seg=None, segment=None, transaction_set=None)


def test_not_ascii():
    edi = build_batch([set_one("A", "0001")]) + "é"
    assert_error(split(edi), "NOT_ASCII",
                 seg=None, segment=None, transaction_set=None)


def test_batch_over_one_mib_rejected():
    edi = "ISA" + "A" * (MAX_BATCH_BYTES + 1)
    assert_error(split(edi), "PAYLOAD_TOO_LARGE",
                 seg=None, segment=None, transaction_set=None)


def test_one_mib_boundary_accepted_by_precheck():
    # 恰好 1 MiB 的输入不会在批大小预检处失败；逐张核验阶段再按 64 KiB 拒绝
    sets = [build_set(f"INV-{i:02d}", [("1", "1", "1.00")],
                      st_ctrl=f"{i:04d}") for i in range(1, 51)]
    edi = build_batch(sets, ge01="50")
    # 用一段长数字替换集 1 的 IT102 数量（"1" -> 共 pad+1 个字符），使整批
    # 恰好达到 1 MiB；单价 0.0000 使 TDS 仍为 0，金额计算不受位数影响
    pad = MAX_BATCH_BYTES - len(edi.encode("ascii"))
    assert pad > 0
    padded_qty = "1" * (pad + 1)
    sets[0] = build_set("INV-01", [("1", padded_qty, "0.0000")],
                        st_ctrl="0001")
    edi = build_batch(sets, ge01="50")
    assert len(edi.encode("ascii")) == MAX_BATCH_BYTES
    # 批大小预检通过；集 1 重建后超过 64 KiB -> 逐张核验失败，整批 422
    assert_error(split(edi), "PAYLOAD_TOO_LARGE",
                 seg=None, segment=None, transaction_set=1)


# ---------- 逐张核验失败（段序号映射回原报文） ----------


def test_per_set_tds_mismatch_maps_original_seg():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002", tds="1"),
    ])
    # 集2 TDS 全局段号 11（重建报文中为 6）
    assert_error(split(edi), "TDS_MISMATCH",
                 seg=11, segment="TDS", transaction_set=2)


def test_per_set_se01_wrong_count_maps_original_seg():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002", se01="9"),
    ])
    # 集2 SE 全局段号 12
    assert_error(split(edi), "SE01_WRONG_COUNT",
                 seg=12, segment="SE", transaction_set=2)


def test_per_set_missing_big_maps_original_seg():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001", big=False),
    ])
    # 重建报文 ST=3 后即 IT1（重建 seg=4），原报文同样为段 4
    assert_error(split(edi), "UNEXPECTED_SEGMENT",
                 seg=4, segment="IT1", transaction_set=1)


def test_per_set_duplicate_it101_maps_original_seg():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001"),
        build_set("INV-2",
                  [("DUP", "1", "1.00"), ("DUP", "1", "1.00")],
                  st_ctrl="0002"),
    ])
    # 集2：ST8 BIG9 IT1_10 IT1_11(重复)
    assert_error(split(edi), "IT101_DUPLICATE",
                 seg=11, segment="IT1", transaction_set=2)


def test_per_set_over_64kib_rejects_whole_batch():
    big_qty = "1" * (MAX_BYTES + 10_000)
    edi = build_batch([
        build_set("INV-1", [("1", big_qty, "0.0000")], st_ctrl="0001"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002"),
    ])
    # 批 ≤1 MiB 但集 1 重建后 >64 KiB：即使集 2 合法也整批 422
    assert len(edi.encode("ascii")) <= MAX_BATCH_BYTES
    assert_error(split(edi), "PAYLOAD_TOO_LARGE",
                 seg=None, segment=None, transaction_set=1)


def test_first_failure_reported_no_partial_results():
    # 批外壳先于逐张核验：GE01 错误优先于集内 TDS 错误
    edi = build_batch([
        build_set("INV-1", [("1", "2", "10.00")], st_ctrl="0001", tds="1"),
        build_set("INV-2", [("1", "2", "10.00")], st_ctrl="0002", tds="2"),
    ], ge01="9")
    assert_error(split(edi), "GE01_COUNT_MISMATCH",
                 seg=13, segment="GE", transaction_set=None)


def test_rebuilt_envelope_uses_original_isa_and_gs():
    edi = build_batch([
        build_set("INV-1", [("1", "2", "19.99")], st_ctrl="0001"),
        build_set("INV-2", [("1", "3", "4.25")], st_ctrl="0002"),
    ])
    items = split(edi).json()["invoices"]
    # ISA+GS 头在每张重建报文中原样沿用
    isa_gs_head = edi[:edi.index("ST*810*0001")]
    assert items[0]["edi_text"].startswith(
        isa_gs_head + "ST*810*0001~")
    assert items[1]["edi_text"].startswith(
        isa_gs_head + "ST*810*0002~")
    assert items[1]["edi_text"].endswith("GE*1*42~IEA*1*000000001~")
