"""POST /v1/ack-status 端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

GS06 = "42"


def build_997(pairs=(), *, status=None, total=None, received=None,
              accepted=None, gs06=GS06, st_ctrl="9001", isa_ctrl="000000009",
              elem="*", comp=":", term="~", gs01="FA", gs08="004010",
              st01="997", ak101="IN", ak102=None,
              ge01="1", iea01="1", ge_ctrl=None, iea_ctrl=None,
              se_ctrl=None, se01=None, ak9=None,
              trailing="", omit=()):
    """组装一份符合本接口语法的 997；未显式给出的计数取自洽默认值。

    pairs: [(st02, "A"/"R"), ...]  AK2/AK5 明细
    ak9:   直接给出完整 AK9 段文本（不含终止符），用于构造非法 AK9
    """
    ak102 = gs06 if ak102 is None else ak102
    ge_ctrl = gs06 if ge_ctrl is None else ge_ctrl
    iea_ctrl = isa_ctrl if iea_ctrl is None else iea_ctrl
    se_ctrl = st_ctrl if se_ctrl is None else se_ctrl

    isa_fields = [
        "00", "          ",
        "00", "          ",
        "ZZ", "RECEIVER1234567",
        "ZZ", "SENDER123456789",
        "000830", "1200",
        "U", "00401",
        isa_ctrl,
        "0",
        "P",
        comp,
    ]
    isa = "ISA" + elem + elem.join(isa_fields) + term
    assert len(isa) == 106

    segs = [
        elem.join(["GS", gs01, "RECVGS", "SENDERGS", "20200830", "1200",
                   gs06, "X", gs08]) + term,
        elem.join(["ST", st01, st_ctrl]) + term,
        elem.join(["AK1", ak101, ak102]) + term,
    ]
    for ctrl, code in pairs:
        segs.append(elem.join(["AK2", "810", ctrl]) + term)
        segs.append(elem.join(["AK5", code]) + term)

    if total is None:
        total = len(pairs)
    if received is None:
        received = total
    if accepted is None:
        accepted = sum(1 for _c, s in pairs if s == "A") if status != "A" \
            else total
    if status is None:
        if accepted == 0:
            status = "R"
        elif accepted == total:
            status = "A"
        else:
            status = "P"
    if ak9 is None:
        ak9 = elem.join(["AK9", status, str(total), str(received),
                         str(accepted)])
    segs.append(ak9 + term)

    se_idx = len(segs) + 2  # segs 为 GS..AK9（全局段号 2 起），SE 为下一段
    count = (se01 if se01 is not None
             else se_idx - 3 + 1)  # ST..SE 含两端 = len(segs)
    segs.append(elem.join(["SE", str(count), se_ctrl]) + term)
    segs.append(elem.join(["GE", ge01, ge_ctrl]) + term)
    segs.append(elem.join(["IEA", iea01, iea_ctrl]) + term)

    kept = [s for s in segs
            if not any(s.startswith(tag + elem) for tag in omit)]
    return isa + "".join(kept) + trailing


def post_status(edi_text, st02=("0001",), gs06=GS06):
    return client.post("/v1/ack-status",
                       json={"gs06": gs06, "st02": list(st02),
                             "edi_text": edi_text})


def statuses(r):
    return [item["status"] for item in r.json()["items"]]


def keys_of(r):
    return [item["st02"] for item in r.json()["items"]]


# ---------- 200：基本路径与计数推断 ----------

def test_all_accepted_single():
    r = post_status(build_997([("0001", "A")]), st02=("0001",))
    assert r.status_code == 200
    assert r.json() == {"items": [{"st02": "0001", "status": "A"}]}


def test_all_rejected_single():
    r = post_status(build_997([("0001", "R")]))
    assert r.status_code == 200
    assert statuses(r) == ["R"]


def test_zero_ak2_all_accepted():
    # 无 AK2：AK9=A*3*3*3，三个未列项全部接受
    edi = build_997((), total=3, accepted=3)
    r = post_status(edi, st02=("a", "b", "c"))
    assert r.status_code == 200
    assert keys_of(r) == ["a", "b", "c"]
    assert statuses(r) == ["A", "A", "A"]


def test_zero_ak2_all_rejected():
    edi = build_997((), total=2, accepted=0)
    r = post_status(edi, st02=("a", "b"))
    assert r.status_code == 200
    assert statuses(r) == ["R", "R"]


def test_mixed_detail_and_unlisted_unknown():
    # 清单 4 项：列出 s1=A、s2=R；AK9 接受数 2 -> 明细占 1，剩 1/2 未知
    edi = build_997([("s1", "A"), ("s2", "R")], total=4, accepted=2)
    r = post_status(edi, st02=("s1", "s2", "s3", "s4"))
    assert r.status_code == 200
    assert statuses(r) == ["A", "R", "U", "U"]
    assert keys_of(r) == ["s1", "s2", "s3", "s4"]


def test_unlisted_all_accepted_when_residual_equals_unlisted():
    edi = build_997([("s1", "R")], total=3, accepted=2)
    r = post_status(edi, st02=("s1", "s2", "s3"))
    assert r.status_code == 200
    assert statuses(r) == ["R", "A", "A"]


def test_unlisted_all_rejected_when_residual_zero():
    edi = build_997([("s1", "A")], total=3, accepted=1)
    r = post_status(edi, st02=("s1", "s2", "s3"))
    assert r.status_code == 200
    assert statuses(r) == ["A", "R", "R"]


def test_all_listed_partial():
    edi = build_997([("a", "A"), ("b", "R"), ("c", "A")],
                   total=3, accepted=2)
    r = post_status(edi, st02=("a", "b", "c"))
    assert r.status_code == 200
    assert statuses(r) == ["A", "R", "A"]


def test_response_order_follows_manifest_not_ak2():
    edi = build_997([("z", "A"), ("a", "R"), ("m", "A")],
                   total=3, accepted=2)
    r = post_status(edi, st02=("m", "z", "a"))
    assert r.status_code == 200
    assert r.json()["items"] == [
        {"st02": "m", "status": "A"},
        {"st02": "z", "status": "A"},
        {"st02": "a", "status": "R"},
    ]


def test_100_items_accepted():
    ids = [f"{i:04d}" for i in range(1, 101)]
    pairs = [(v, "A") for v in ids]
    edi = build_997(pairs, total=100, accepted=100)
    r = post_status(edi, st02=ids)
    assert r.status_code == 200
    assert statuses(r) == ["A"] * 100


def test_custom_delimiters():
    edi = build_997([("0001", "A")], elem="|", comp="^", term="#")
    r = post_status(edi)
    assert r.status_code == 200
    assert statuses(r) == ["A"]


def test_newline_terminator():
    edi = build_997([("0001", "A")], term="\n")
    r = post_status(edi)
    assert r.status_code == 200
    assert statuses(r) == ["A"]


# ---------- 422：输入与清单 ----------

def _expect_422(edi_text, code, st02=("0001",), gs06=GS06):
    r = post_status(edi_text, st02=st02, gs06=gs06)
    assert r.status_code == 422, (r.status_code, r.text)
    assert r.json()["error"]["code"] == code
    return r


def test_422_not_ascii():
    _expect_422("café", "NOT_ASCII")


def test_422_empty():
    _expect_422("", "EMPTY_INPUT")


def test_422_too_large():
    big = build_997([("0001", "A")]) + " " * 65536
    _expect_422(big, "PAYLOAD_TOO_LARGE")


def test_422_manifest_gs06_empty():
    _expect_422(build_997([("0001", "A")], gs06=""),
                "ACK_MANIFEST_GS06_EMPTY", gs06="")


def test_422_manifest_st02_empty():
    _expect_422(build_997([("0001", "A")], total=2, accepted=2),
                "ACK_MANIFEST_ST02_EMPTY", st02=("0001", ""))


def test_422_manifest_st02_duplicate():
    _expect_422(build_997([("0001", "A")], total=2, accepted=2),
                "ACK_MANIFEST_ST02_DUPLICATE", st02=("0001", "0001"))


def test_422_st02_list_length_bounds():
    assert client.post("/v1/ack-status",
                       json={"gs06": "42", "st02": [],
                             "edi_text": "x"}).status_code == 422
    assert client.post("/v1/ack-status",
                       json={"gs06": "42",
                             "st02": [str(i) for i in range(101)],
                             "edi_text": "x"}).status_code == 422


def test_422_missing_request_fields():
    assert client.post("/v1/ack-status", json={}).status_code == 422


# ---------- 422：ISA / 信封 ----------

def test_422_isa_errors():
    from test_verify import isa
    _expect_422("XX" + isa()[2:], "ISA_NOT_FIRST")
    _expect_422(isa()[:50], "ISA_TOO_SHORT")


def test_422_gs01_not_fa():
    _expect_422(build_997([("0001", "A")], gs01="IN"), "GS01_NOT_FA")


def test_422_gs08_bad_version():
    _expect_422(build_997([("0001", "A")], gs08="005010"),
                "GS08_BAD_VERSION")


def test_422_st01_not_997():
    _expect_422(build_997([("0001", "A")], st01="999"), "ST01_NOT_997")


def test_422_missing_ak1():
    _expect_422(build_997([("0001", "A")], omit=("AK1",)),
                "UNEXPECTED_SEGMENT")


def test_422_ak1_wrong_gs06():
    # GS06=77（与 GE02 配对），但 AK102 仍为清单中的 42 -> 不引用本回执组
    edi = build_997([("0001", "A")], gs06="77")
    _expect_422(edi, "AK1_GS06_MISMATCH", gs06="42")


def test_422_missing_segments():
    _expect_422(build_997([("0001", "A")], omit=("GE",)),
                "UNEXPECTED_SEGMENT")
    _expect_422(build_997([("0001", "A")], omit=("IEA",)),
                "MISSING_SEGMENT")


def test_422_control_pairing():
    _expect_422(build_997([("0001", "A")], ge_ctrl="99"),
                "GS_GE_CONTROL_MISMATCH")
    _expect_422(build_997([("0001", "A")], iea_ctrl="000000008"),
                "ISA_IEA_CONTROL_MISMATCH")
    _expect_422(build_997([("0001", "A")], se_ctrl="8888"),
                "ST_SE_CONTROL_MISMATCH")
    _expect_422(build_997([("0001", "A")], ge01="2"),
                "GE01_NOT_ONE")
    _expect_422(build_997([("0001", "A")], iea01="2"),
                "IEA01_NOT_ONE")


def test_422_se01_wrong_count():
    _expect_422(build_997([("0001", "A")], se01="99"),
                "SE01_WRONG_COUNT")


def test_422_se01_changes_with_ak2_pairs():
    # 两个 AK2/AK5 对时 SE01 应为 8；故意保留单对时的 6
    edi = build_997([("a", "A"), ("b", "R")], total=2, accepted=1, se01=6)
    _expect_422(edi, "SE01_WRONG_COUNT", st02=("a", "b"))


def test_422_trailing_data_and_garbage():
    _expect_422(build_997([("0001", "A")], trailing="GARBAGE"),
                "TRAILING_DATA")
    _expect_422(build_997([("0001", "A")]) + "XX*1~",
                "UNEXPECTED_SEGMENT")


# ---------- 422：正文结构 ----------

def test_422_ak1_element_count():
    edi = build_997([("0001", "A")], ak9=None)
    # 直接改写 AK1 段
    edi = edi.replace("AK1*IN*42~", "AK1*IN~")
    _expect_422(edi, "SEGMENT_TOO_FEW_ELEMENTS")
    edi = build_997([("0001", "A")]).replace("AK1*IN*42~", "AK1*IN*42*X~")
    _expect_422(edi, "AK1_ELEMENT_COUNT")


def test_422_ak201_not_810():
    edi = build_997([("0001", "A")]).replace("AK2*810*0001~", "AK2*820*0001~")
    _expect_422(edi, "AK201_NOT_810")


def test_422_ak2_unknown_st02():
    edi = build_997([("0001", "A")])
    _expect_422(edi, "AK2_ST02_UNKNOWN", st02=("9999",))


def test_422_ak2_duplicate_st02():
    edi = _dup_ak2()
    _expect_422(edi, "AK2_ST02_DUPLICATE", st02=("a", "b"))


def _dup_ak2():
    base = build_997([("a", "A"), ("b", "R")], total=2, accepted=0)
    return base.replace("AK2*810*b~", "AK2*810*a~", 1)


def test_422_ak2_without_ak5():
    # 手工构造：AK2 之后直接 AK9
    edi = build_997([("0001", "A")]).replace("AK5*A~", "")
    _expect_422(edi, "UNEXPECTED_SEGMENT")


def test_422_ak5_invalid():
    edi = build_997([("0001", "A")]).replace("AK5*A~", "AK5*P~")
    _expect_422(edi, "AK501_INVALID")


def test_422_ak5_element_count():
    edi = build_997([("0001", "A")]).replace("AK5*A~", "AK5~")
    _expect_422(edi, "SEGMENT_TOO_FEW_ELEMENTS")
    edi = build_997([("0001", "A")]).replace("AK5*A~", "AK5*A*X~")
    _expect_422(edi, "AK5_ELEMENT_COUNT")


def test_422_unexpected_body_segment():
    # 在 AK5 与 AK9 之间插入 AK3（不属于 997 允许的正文段）
    edi = build_997([("0001", "A")]).replace(
        "AK9*A*1*1*1~", "AK3*TDS*4~AK9*A*1*1*1~", 1)
    _expect_422(edi, "UNEXPECTED_SEGMENT")


def test_422_ak9_element_count():
    edi = build_997([("0001", "A")], ak9="AK9*A*1*1")
    _expect_422(edi, "AK9_ELEMENT_COUNT")
    edi = build_997([("0001", "A")], ak9="AK9*A*1*1*1*0")
    _expect_422(edi, "AK9_ELEMENT_COUNT")


def test_422_ak901_invalid():
    edi = build_997([("0001", "A")],
                   ak9="AK9*X*1*1*1")
    _expect_422(edi, "AK901_INVALID")


def test_422_ak9_count_not_number():
    edi = build_997([("0001", "A")], ak9="AK9*A*1*1*X")
    _expect_422(edi, "AK9_COUNT_INVALID")


def test_422_ak9_counts_must_equal_manifest_length():
    edi = build_997((), total=3, received=3, accepted=3)
    _expect_422(edi, "AK9_COUNT_MISMATCH", st02=("a", "b"))
    edi = build_997((), total=2, received=1, accepted=1)
    _expect_422(edi, "AK9_COUNT_MISMATCH", st02=("a", "b"))


def test_422_ak9_status_contradictions():
    # A 但接受数不足
    edi = build_997((), total=2, received=2, accepted=1, status="A")
    _expect_422(edi, "AK9_STATUS_CONTRADICTION", st02=("a", "b"))
    # R 但接受数非 0
    edi = build_997((), total=2, received=2, accepted=1, status="R")
    _expect_422(edi, "AK9_STATUS_CONTRADICTION", st02=("a", "b"))
    # P 但全接受
    edi = build_997((), total=2, received=2, accepted=2, status="P")
    _expect_422(edi, "AK9_STATUS_CONTRADICTION", st02=("a", "b"))
    # P 但零接受
    edi = build_997((), total=2, received=2, accepted=0, status="P")
    _expect_422(edi, "AK9_STATUS_CONTRADICTION", st02=("a", "b"))


def test_422_detail_accepted_exceeds_ak9():
    # 明细两个 A（接受数 2），AK9 只接受 1
    edi = build_997([("a", "A"), ("b", "A")], total=3, accepted=1)
    _expect_422(edi, "AK9_ACCEPTED_CONTRADICTION", st02=("a", "b", "c"))


def test_422_accepted_out_of_range():
    edi = build_997((), total=2, received=2, accepted=3)
    _expect_422(edi, "AK9_ACCEPTED_OUT_OF_RANGE", st02=("a", "b"))


def test_422_error_has_seg_and_segment():
    r = post_status(build_997([("0001", "A")], gs01="IN"))
    body = r.json()["error"]
    assert body["seg"] == 2
    assert body["segment"] == "GS"


# ---------- 旧接口保持可用 ----------

def test_existing_endpoints_unaffected():
    assert client.get("/health").json() == {"status": "ok"}
    from test_verify import build_edi
    edi = build_edi([("1", "2", "10.50")])
    r = client.post("/v1/verify", json={"edi_text": edi})
    assert r.status_code == 200
    r = client.post("/v1/acknowledge", json={
        "edi_text": edi, "isa13": "000000042",
        "gs06": "77", "st02": "9001"})
    assert r.status_code == 200
