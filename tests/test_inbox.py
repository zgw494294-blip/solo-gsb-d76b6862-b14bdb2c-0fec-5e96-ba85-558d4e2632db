"""/v1/inbox 幂等收件库端到端测试。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.inbox import InboxStore
from app.main import app, get_inbox_store


# ---------- 报文构造工具 ----------

def _expected_cents(qty: str, price: str) -> int:
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
              st_ctrl="0001", big01="20200830", big02="9001",
              elem="*", comp=":", term="~"):
    """组装一份 810；未显式给出的 tds 取各行整数分之和。"""
    if tds is None:
        tds = str(sum(_expected_cents(qty, price)
                      for _lid, qty, price in lines))

    def seg(*fields):
        return elem.join(fields) + term

    body = []
    body.append(seg("GS", "IN", "SENDERGS", "RECVGS", "20200830", "1200",
                    gs_ctrl, "X", "004010"))
    body.append(seg("ST", "810", st_ctrl))
    body.append(seg("BIG", big01, big02))
    for lid, qty, price in lines:
        body.append(seg("IT1", lid, qty, "EA", price))
    body.append(seg("TDS", tds))
    se01 = len(body) - 2 + 2  # ISA=1, GS=2, ST=3；含 SE 自身
    body.append(seg("SE", str(se01), st_ctrl))
    body.append(seg("GE", "1", gs_ctrl))
    body.append(seg("IEA", "1", ctrl))
    return isa(ctrl, elem, comp, term) + "".join(body)


EDI_A = build_edi([("1", "2", "10.50"), ("2", "3", "4.25")])


# ---------- 收件库 fixture：每个用例独立的临时数据库 ----------

@pytest.fixture()
def store(tmp_path):
    s = InboxStore(str(tmp_path / "inbox.db"))
    app.dependency_overrides[get_inbox_store] = lambda: s
    try:
        yield s
    finally:
        app.dependency_overrides.clear()
        s.close()


@pytest.fixture()
def client(store):
    return TestClient(app)


def _post(client, edi_text):
    return client.post("/v1/inbox", json={"edi_text": edi_text})


# ---------- 首次提交与重发 ----------

def test_first_submit_saved_and_replayed_false(client):
    r = _post(client, EDI_A)
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is False
    assert isinstance(body["receipt_id"], str) and len(body["receipt_id"]) == 64
    assert body["invoice_number"] == "9001"
    assert body["lines"] == [
        {"line_id": "1", "quantity": "2", "unit_price": "10.50",
         "line_amount": 2100},
        {"line_id": "2", "quantity": "3", "unit_price": "4.25",
         "line_amount": 1275},
    ]
    assert body["total_amount"] == 3375


def test_identical_resend_replayed_true_same_receipt(client):
    first = _post(client, EDI_A).json()
    second = _post(client, EDI_A)
    assert second.status_code == 200
    body = second.json()
    assert body["replayed"] is True
    assert body["receipt_id"] == first["receipt_id"]
    assert body["total_amount"] == first["total_amount"]


def test_receipt_id_is_deterministic(client):
    a = _post(client, EDI_A).json()["receipt_id"]
    b = _post(client, EDI_A).json()["receipt_id"]
    assert a == b


def test_envelope_control_numbers_do_not_participate(client):
    first = _post(client, EDI_A).json()
    # 仅改 ISA13/GS06/ST02（及配对的 SE02/GE02/IEA02），业务内容不变
    variant = build_edi([("1", "2", "10.50"), ("2", "3", "4.25")],
                        ctrl="000000777", gs_ctrl="99", st_ctrl="0451")
    r = _post(client, variant)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert r.json()["receipt_id"] == first["receipt_id"]


def test_line_order_does_not_participate(client):
    first = _post(client, EDI_A).json()
    swapped = build_edi([("2", "3", "4.25"), ("1", "2", "10.50")])
    r = _post(client, swapped)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert r.json()["receipt_id"] == first["receipt_id"]


def test_quantity_and_price_normalized_in_fingerprint(client):
    first = _post(client, build_edi([("1", "1.5", "10.00")])).json()
    # 1.5 == 1.50（千分位 1500）、10.00 == 10.0（万分位 100000），TDS 不变
    variant = build_edi([("1", "1.50", "10.0")])
    r = _post(client, variant)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert r.json()["receipt_id"] == first["receipt_id"]


# ---------- 同键不同指纹 -> 409 ----------

def test_same_key_different_fingerprint_conflict(client):
    first = _post(client, EDI_A).json()
    # 同 ISA06/GS02/BIG02，但数量变化（TDS 同步变化）
    changed = build_edi([("1", "5", "10.50"), ("2", "3", "4.25")])
    r = _post(client, changed)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INBOX_CONFLICT"
    # 原记录未改动：重发原文仍 replayed=true，GET 仍返回首次内容
    again = _post(client, EDI_A).json()
    assert again["replayed"] is True
    assert again["receipt_id"] == first["receipt_id"]
    got = client.get(f"/v1/inbox/{first['receipt_id']}").json()
    assert got["edi_text"] == EDI_A
    assert got["total_amount"] == 3375


def test_big_segment_participates_in_fingerprint(client):
    _post(client, EDI_A)
    # 仅 BIG01 日期变化（BIG 段属于业务指纹）
    changed = build_edi([("1", "2", "10.50"), ("2", "3", "4.25")],
                        big01="20200901")
    r = _post(client, changed)
    assert r.status_code == 409


def test_inconsistent_tds_rejected_before_inbox(client):
    _post(client, EDI_A)
    changed = build_edi([("1", "2", "10.50"), ("2", "3", "4.25")],
                        tds="3376")
    # TDS 与行合计不一致会先被核验拒绝（422），不会到达收件库
    r = _post(client, changed)
    assert r.status_code == 422


def test_different_key_new_receipt(client):
    first = _post(client, EDI_A).json()
    other = build_edi([("1", "2", "10.50"), ("2", "3", "4.25")],
                      big02="9002")
    r = _post(client, other)
    assert r.status_code == 200
    assert r.json()["replayed"] is False
    assert r.json()["receipt_id"] != first["receipt_id"]


# ---------- 核验失败：原样 422 且不写库 ----------

def test_verify_failure_422_and_nothing_saved(client, store):
    bad = build_edi([("1", "2", "10.50")], tds="9999")
    r = _post(client, bad)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "TDS_MISMATCH"
    assert r.json()["error"]["seg"] is not None
    # 未写入收件库：同键的合法报文随后仍按首次提交处理
    good = build_edi([("1", "2", "10.50")])
    r2 = _post(client, good)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is False


def test_non_ascii_422(client):
    r = _post(client, "ISAé")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NOT_ASCII"


# ---------- GET /v1/inbox/{receipt_id} ----------

def test_get_returns_first_saved_text_and_result(client):
    first = _post(client, EDI_A).json()
    rid = first["receipt_id"]
    # 重发一个信封控制号不同的等价报文后，GET 仍返回首次保存的原文
    _post(client, build_edi([("1", "2", "10.50"), ("2", "3", "4.25")],
                            ctrl="000000888", gs_ctrl="7", st_ctrl="9"))
    r = client.get(f"/v1/inbox/{rid}")
    assert r.status_code == 200
    body = r.json()
    assert body["receipt_id"] == rid
    assert body["edi_text"] == EDI_A
    assert body["invoice_number"] == "9001"
    assert body["lines"] == first["lines"]
    assert body["total_amount"] == 3375


def test_get_unknown_receipt_404(client):
    r = client.get("/v1/inbox/" + "0" * 64)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RECEIPT_NOT_FOUND"


# ---------- 并发 ----------

def test_concurrent_identical_submits(client):
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda _: TestClient(app).post("/v1/inbox",
                                           json={"edi_text": EDI_A}),
            range(16)))
    assert all(r.status_code == 200 for r in responses)
    bodies = [r.json() for r in responses]
    assert len({b["receipt_id"] for b in bodies}) == 1
    assert sum(1 for b in bodies if b["replayed"] is False) == 1
    assert sum(1 for b in bodies if b["replayed"] is True) == 15


def test_concurrent_conflicting_submits(client):
    edi_b = build_edi([("1", "9", "10.50"), ("2", "3", "4.25")])  # 同键不同指纹
    payloads = [EDI_A] * 8 + [edi_b] * 8
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda text: TestClient(app).post("/v1/inbox",
                                              json={"edi_text": text}),
            payloads))
    ok = [r.json() for r in responses if r.status_code == 200]
    conflict = [r for r in responses if r.status_code == 409]
    # 同一收件键只有一组指纹能入库：先到的那组全部 200（1 次首次 + 7 次重发）
    assert len(ok) == 8
    assert len(conflict) == 8
    assert len({b["receipt_id"] for b in ok}) == 1
    assert sum(1 for b in ok if b["replayed"] is False) == 1


# ---------- 持久化：重启（新 store 实例，同一数据库文件）后仍识别重发 ----------

def test_persistence_across_restart(store, client, tmp_path):
    first = _post(client, EDI_A).json()
    assert first["replayed"] is False
    # 模拟重启：同一数据库文件上新建 InboxStore
    store.close()
    reopened = InboxStore(str(tmp_path / "inbox.db"))
    app.dependency_overrides[get_inbox_store] = lambda: reopened
    try:
        r = _post(client, EDI_A)
        assert r.status_code == 200
        assert r.json()["replayed"] is True
        assert r.json()["receipt_id"] == first["receipt_id"]
        got = client.get(f"/v1/inbox/{first['receipt_id']}").json()
        assert got["edi_text"] == EDI_A
    finally:
        reopened.close()
