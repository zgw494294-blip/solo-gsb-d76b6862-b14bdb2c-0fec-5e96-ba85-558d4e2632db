"""POST /v1/issue-810 端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app
from test_verify import build_edi

client = TestClient(app)


def payload(**over):
    """合法请求；用关键字覆盖单个字段。"""
    req = {
        "isa05": "ZZ", "isa06": "SENDER123456789",
        "isa07": "ZZ", "isa08": "RECEIVER1234567",
        "isa09": "000830", "isa10": "1200", "isa13": "000000001",
        "gs02": "SENDERGS", "gs03": "RECVGS",
        "gs04": "20200830", "gs05": "1200", "gs06": "42",
        "st02": "0001",
        "big01": "20200830", "big02": "INV-2020-0830",
        "lines": [{"it101": "1", "it102": "10", "it104": "19.99"},
                  {"it101": "2", "it102": "3", "it104": "0.05"}],
    }
    req.update(over)
    return req


def post_issue(**over):
    return client.post("/v1/issue-810", json=payload(**over))


def split_edi(text, elem="*", term="~"):
    """把报文切成 [[字段...], ...]（ISA 为首段）。"""
    assert text.endswith(term)
    return [fragment.split(elem) for fragment in text[:-1].split(term)]


# ---------- 成功路径 ----------

def test_issue_basic_structure():
    r = post_issue()
    assert r.status_code == 200
    body = r.json()
    assert body["total_amount"] == 20005
    text = body["edi_text"]

    # ISA 固定 106 字节；分隔符固定 * : ~
    assert len(text[:106]) == 106
    assert text[3] == "*" and text[104] == ":" and text[105] == "~"

    segs = split_edi(text)
    assert [s[0] for s in segs] == ["ISA", "GS", "ST", "BIG", "IT1", "IT1",
                                    "TDS", "SE", "GE", "IEA"]
    isa = segs[0]
    assert isa[5] == "ZZ" and isa[6] == "SENDER123456789"
    assert isa[7] == "ZZ" and isa[8] == "RECEIVER1234567"
    assert isa[9] == "000830" and isa[10] == "1200"
    assert isa[11] == "U" and isa[12] == "00401"
    assert isa[13] == "000000001"
    assert isa[14] == "0" and isa[15] == "P" and isa[16] == ":"

    assert segs[1] == ["GS", "IN", "SENDERGS", "RECVGS", "20200830", "1200",
                       "42", "X", "004010"]
    assert segs[2] == ["ST", "810", "0001"]
    assert segs[3] == ["BIG", "20200830", "INV-2020-0830"]
    assert segs[4] == ["IT1", "1", "10", "EA", "19.99"]
    assert segs[5] == ["IT1", "2", "3", "EA", "0.05"]
    assert segs[6] == ["TDS", "20005"]
    assert segs[7] == ["SE", "6", "0001"]      # ST..SE 共 6 段
    assert segs[8] == ["GE", "1", "42"]
    assert segs[9] == ["IEA", "1", "000000001"]


def test_control_numbers_paired():
    segs = split_edi(post_issue().json()["edi_text"])
    isa, gs, st = segs[0], segs[1], segs[2]
    se, ge, iea = segs[-3], segs[-2], segs[-1]
    assert isa[13] == iea[2]                   # ISA13 == IEA02
    assert gs[6] == ge[2]                      # GS06 == GE02
    assert st[2] == se[2]                      # ST02 == SE02
    assert ge[1] == "1" and iea[1] == "1"


def test_generated_message_passes_verify():
    body = post_issue().json()
    r = client.post("/v1/verify", json={"edi_text": body["edi_text"]})
    assert r.status_code == 200
    got = r.json()
    assert got["invoice_number"] == "INV-2020-0830"
    assert got["total_amount"] == body["total_amount"] == 20005
    assert got["lines"] == [
        {"line_id": "1", "quantity": "10", "unit_price": "19.99",
         "line_amount": 19990},
        {"line_id": "2", "quantity": "3", "unit_price": "0.05",
         "line_amount": 15},
    ]


def test_round_half_up_per_line():
    # 2 * 1.005 = 2.010 -> 201；2 * 1.0025 = 2.005 -> 201（逐行取整分再求和）
    r = post_issue(lines=[{"it101": "1", "it102": "2", "it104": "1.005"},
                          {"it101": "2", "it102": "2", "it104": "1.0025"}])
    assert r.status_code == 200
    body = r.json()
    assert body["total_amount"] == 402
    segs = split_edi(body["edi_text"])
    assert segs[6] == ["TDS", "402"]
    got = client.post("/v1/verify", json={"edi_text": body["edi_text"]}).json()
    assert [line["line_amount"] for line in got["lines"]] == [201, 201]


def test_isa06_isa08_padded_to_15():
    r = post_issue(isa06="ABC", isa08="XY")
    assert r.status_code == 200
    text = r.json()["edi_text"]
    assert len(text[:106]) == 106              # 补齐后 ISA 仍为 106 字节
    isa = split_edi(text)[0]
    assert isa[6] == "ABC" + " " * 12
    assert isa[8] == "XY" + " " * 13
    assert client.post("/v1/verify",
                       json={"edi_text": text}).status_code == 200


def test_max_lines_100_ok():
    lines = [{"it101": str(i), "it102": "1", "it104": "0.01"}
             for i in range(1, 101)]
    r = post_issue(lines=lines)
    assert r.status_code == 200
    body = r.json()
    assert body["total_amount"] == 100
    assert len(body["edi_text"].encode("ascii")) <= 64 * 1024
    segs = split_edi(body["edi_text"])
    assert segs[-3] == ["SE", "104", "0001"]   # ST..SE = 100 行 + 4
    assert client.post("/v1/verify",
                       json={"edi_text": body["edi_text"]}).status_code == 200


def test_generated_message_acknowledgeable():
    text = post_issue().json()["edi_text"]
    r = client.post("/v1/acknowledge",
                    json={"edi_text": text, "isa13": "000000002",
                          "gs06": "43", "st02": "0002"})
    assert r.status_code == 200
    assert "~AK5*A~" in r.json()["edi_text"]


def test_amount_text_preserved():
    # IT102/IT104 原文写入报文；金额按十进制数值计算
    r = post_issue(lines=[{"it101": "1", "it102": "1.50",
                           "it104": "19.9900"}])
    assert r.status_code == 200
    assert "~IT1*1*1.50*EA*19.9900~" in r.json()["edi_text"]
    # 1.5 * 19.99 = 29.985 -> 2999 分（ROUND_HALF_UP）
    assert r.json()["total_amount"] == 2999


def test_zero_unit_price_ok():
    r = post_issue(lines=[{"it101": "1", "it102": "999", "it104": "0.0000"}])
    assert r.status_code == 200
    assert r.json()["total_amount"] == 0
    assert "~TDS*0~" in r.json()["edi_text"]


def test_tiny_amounts_round_to_zero():
    # 0.001 * 0.0001 = 0.0000001 -> 0 分
    r = post_issue(lines=[{"it101": "1", "it102": "0.001",
                           "it104": "0.0001"}])
    assert r.status_code == 200
    assert r.json()["total_amount"] == 0


# ---------- 422：通用字段校验 ----------

def _expect_422(resp, code, field):
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == code
    assert err["field"] == field
    assert err["message"]


def test_422_not_ascii():
    for field in ("isa06", "gs02", "big02", "st02"):
        _expect_422(post_issue(**{field: "中文"}), "NOT_ASCII", field)
    _expect_422(
        post_issue(lines=[{"it101": "行1", "it102": "1", "it104": "1"}]),
        "NOT_ASCII", "lines[0].it101")


def test_422_separator_injection():
    for bad in ("a*b", "a:b", "a~b", "a\nb", "a\rb", "a\x01b", "a\x7fb"):
        _expect_422(post_issue(big02=bad), "SEPARATOR_INJECTION", "big02")
    _expect_422(post_issue(gs06="4*2"), "SEPARATOR_INJECTION", "gs06")
    _expect_422(
        post_issue(lines=[{"it101": "1~", "it102": "1", "it104": "1"}]),
        "SEPARATOR_INJECTION", "lines[0].it101")


def test_422_field_too_long():
    _expect_422(post_issue(isa05="ZZZ"), "FIELD_TOO_LONG", "isa05")
    _expect_422(post_issue(isa06="A" * 16), "FIELD_TOO_LONG", "isa06")
    _expect_422(post_issue(isa08="B" * 16), "FIELD_TOO_LONG", "isa08")
    _expect_422(post_issue(gs02="G" * 16), "FIELD_TOO_LONG", "gs02")
    _expect_422(post_issue(gs06="1" * 10), "FIELD_TOO_LONG", "gs06")
    _expect_422(post_issue(st02="1" * 10), "FIELD_TOO_LONG", "st02")
    _expect_422(post_issue(big02="I" * 23), "FIELD_TOO_LONG", "big02")
    _expect_422(
        post_issue(lines=[{"it101": "L" * 21, "it102": "1", "it104": "1"}]),
        "FIELD_TOO_LONG", "lines[0].it101")
    _expect_422(
        post_issue(lines=[{"it101": "1", "it102": "1" * 16, "it104": "1"}]),
        "FIELD_TOO_LONG", "lines[0].it102")
    _expect_422(
        post_issue(lines=[{"it101": "1", "it102": "1", "it104": "1" * 18}]),
        "FIELD_TOO_LONG", "lines[0].it104")


def test_422_field_empty_or_short():
    _expect_422(post_issue(isa05=""), "FIELD_EMPTY", "isa05")
    _expect_422(post_issue(isa05="Z"), "FIELD_TOO_SHORT", "isa05")
    _expect_422(post_issue(isa06=""), "FIELD_EMPTY", "isa06")
    _expect_422(post_issue(gs06=""), "FIELD_EMPTY", "gs06")
    _expect_422(post_issue(st02=""), "FIELD_EMPTY", "st02")
    _expect_422(post_issue(big02=""), "FIELD_EMPTY", "big02")
    _expect_422(
        post_issue(lines=[{"it101": "", "it102": "1", "it104": "1"}]),
        "FIELD_EMPTY", "lines[0].it101")


def test_422_isa13_invalid():
    for bad in ("", "12345678", "1234567890", "abcdefghi", "00000001 ",
                "00000001*"):
        _expect_422(post_issue(isa13=bad), "ISA13_INVALID", "isa13")


def test_422_date_invalid():
    for bad in ("000230", "001332", "991332", "abcdef", "0008 0"):
        _expect_422(post_issue(isa09=bad), "DATE_INVALID", "isa09")
    for bad in ("20201301", "20200230", "00000830"):
        _expect_422(post_issue(gs04=bad), "DATE_INVALID", "gs04")
    _expect_422(post_issue(big01="20230229"), "DATE_INVALID", "big01")
    _expect_422(post_issue(isa09="00083"), "FIELD_TOO_SHORT", "isa09")
    _expect_422(post_issue(gs04="202008300"), "FIELD_TOO_LONG", "gs04")
    # 合法闰日
    assert post_issue(big01="20200229", isa09="000229").status_code == 200


def test_422_time_invalid():
    for bad in ("2460", "1260", "abcd", "12 0"):
        _expect_422(post_issue(isa10=bad), "TIME_INVALID", "isa10")
    _expect_422(post_issue(gs05="12345"), "TIME_INVALID", "gs05")   # 5 位
    _expect_422(post_issue(gs05="126000"), "TIME_INVALID", "gs05")  # SS=60
    _expect_422(post_issue(gs05="240000"), "TIME_INVALID", "gs05")
    _expect_422(post_issue(isa10="120"), "FIELD_TOO_SHORT", "isa10")
    _expect_422(post_issue(isa10="120000"), "FIELD_TOO_LONG", "isa10")
    # HHMM / HHMMSS / HHMMSSDD 均合法
    for good in ("1200", "235959", "00000099"):
        assert post_issue(gs05=good).status_code == 200


def test_422_lines_count():
    _expect_422(post_issue(lines=[]), "LINES_EMPTY", "lines")
    lines = [{"it101": str(i), "it102": "1", "it104": "1"}
             for i in range(1, 102)]
    _expect_422(post_issue(lines=lines), "LINES_TOO_MANY", "lines")


def test_422_it101_duplicate():
    lines = [{"it101": "1", "it102": "1", "it104": "1"},
             {"it101": "2", "it102": "1", "it104": "1"},
             {"it101": "1", "it102": "2", "it104": "2"}]
    _expect_422(post_issue(lines=lines), "IT101_DUPLICATE", "lines[2].it101")


def test_422_it102_invalid():
    for bad in ("0", "0.0", "0.000", "-1", "1.0001", "abc", "1.", ".5",
                "1,5", "1 5"):
        _expect_422(
            post_issue(lines=[{"it101": "1", "it102": bad, "it104": "1"}]),
            "IT102_INVALID", "lines[0].it102")


def test_422_it104_invalid():
    for bad in ("-1", "-0.5", "1.00001", "abc", "1.", ".5"):
        _expect_422(
            post_issue(lines=[{"it101": "1", "it102": "1", "it104": bad}]),
            "IT104_INVALID", "lines[0].it104")


def test_422_missing_request_fields():
    assert client.post("/v1/issue-810", json={}).status_code == 422
    assert client.post("/v1/issue-810",
                       json={"isa05": "ZZ"}).status_code == 422


# ---------- 旧接口保持可用 ----------

def test_existing_endpoints_unaffected():
    edi = build_edi([("1", "2", "10.50")])
    r = client.post("/v1/verify", json={"edi_text": edi})
    assert r.status_code == 200
    assert r.json()["total_amount"] == 2100
    assert client.get("/health").json() == {"status": "ok"}
