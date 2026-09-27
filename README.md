# X12 810 发票核验 API

基于 FastAPI 的 ASC X12 810 Invoice（版本 `ISA12=00401` / `GS08=004010`）报文核验服务。
接收**单份**交换/功能组/交易集，校验信封结构、控制号配对与发票金额合计，
成功返回发票号、各行金额与总额（均为整数分）；任何核验失败返回 `422`，
错误体带**段序号**与**错误码**。

另提供 `POST /v1/issue-810`：由**结构化发票数据**（交换收发方、日期时间、
控制号、发票号与 1～100 行明细）生成 ASC X12 004010 **810 报文**，分隔符
固定为 `*` / `:` / `~`，ISA 段固定 106 字节，控制号两两配对、段数计数自洽，
逐行按 `ROUND_HALF_UP` 取整数分合计写入 `TDS01`；生成的报文不超过 64 KiB
且保证可通过 `/v1/verify`。

另提供 `POST /v1/reconcile`：给定一张采购单，对 1～50 份发票报文按输入顺序
逐张核销（逐张核验、行号/单价比对、数量累计），返回逐张接受状态与各采购单行
剩余数量。

另提供 `POST /v1/split-810`：接收**单份 ISA/IEA、单组 GS/GE、内含 1～50 个
连续 ST…SE 810 交易集**的批量交换报文（ASCII，≤1 MiB），按 ISA 固定位置
读取分隔符，校验批外壳控制号配对、`GE01` 与交易集数一致、`IEA01=1`、各
`ST01=810` 且 `ST02` 不重复；为每个交易集保留原业务段与控制号，重建
`GE01=1` 的单张报文并用与 `/v1/verify` 完全相同的规则逐张核验。**任一条
失败整批 `422`**（指出原报文段序号、交易集序号与错误码，不返回部分结果）；
成功按原顺序返回各张的 `ST02`、发票号与可直接提交 `/v1/verify` 的 `edi_text`
（每份 ≤64 KiB）。

另提供 `POST /v1/acknowledge`：接收单份 810 报文与回执控制号，返回
ASC X12 004010 **997 功能回执**（Functional Acknowledgment）报文；对缺少 `SE`、
`GS08` 非 `004010` 等可识别来报错误生成 `AK5`/`AK9` 为 `R`、接收数为 0 的拒绝回执。

另提供 `POST /v1/ack-status`：接收本次**发送清单**（组控制号 `gs06` 与
1～100 个唯一非空交易集控制号 `st02`）与一份 ASC X12 004010 997 回执报文，
严格核验其信封、控制号配对、段数与 `AK9` 计数，并按清单顺序返回每一项的
接受状态（`A` 接受 / `R` 拒绝 / `U` 未知）；结构非法或计数矛盾返回 `422`，
错误体带**段序号**与**错误码**。

另提供**幂等收件库** `POST /v1/inbox`：接收单份 810 报文，先按 `/v1/verify`
核验（失败原样返回 `422` 且不入库），再以收件键 `(ISA06, GS02, BIG02)` 与
业务指纹（BIG 段 + 按 IT101 排序的 IT1 段 + TDS 整数分，IT102/IT104 分别
规范为千分位/万分位整数，信封控制号与行顺序不参与）原子写入 SQLite。
首次提交返回固定 `receipt_id` 与 `replayed=false`；同键同指纹重发返回原
`receipt_id` 与 `replayed=true`；同键不同指纹返回 `409` 且原记录不变。
`GET /v1/inbox/{receipt_id}` 取回首次保存的原文与核验结果。数据库保存在
Compose 命名卷中，服务重启后仍能识别重发。

## 单条命令启动

需要 Docker（含 Compose 插件）：

```bash
docker compose up --build -d
```

- 服务地址：`http://localhost:8080`
- 核验接口：`POST http://localhost:8080/v1/verify`
- 报文生成：`POST http://localhost:8080/v1/issue-810`
- 核销接口：`POST http://localhost:8080/v1/reconcile`
- 批量拆分：`POST http://localhost:8080/v1/split-810`
- 回执生成：`POST http://localhost:8080/v1/acknowledge`
- 回执状态解析：`POST http://localhost:8080/v1/ack-status`
- 收件入库：`POST http://localhost:8080/v1/inbox`
- 收件查询：`GET http://localhost:8080/v1/inbox/{receipt_id}`
- 健康检查：`GET http://localhost:8080/health`
- 交互文档（Swagger UI）：`http://localhost:8080/docs`

停止：`docker compose down`（收件库数据保留在命名卷 `inbox-data` 中；
连同数据一并删除用 `docker compose down -v`）

## 请求 / 响应（POST /v1/verify）

请求体（JSON）：

```json
{
  "edi_text": "ISA*00*          *00*          *..."
}
```

`edi_text` 必须为纯 ASCII，大小不超过 **64 KiB（65536 字节）**。

成功 `200`：

```json
{
  "invoice_number": "INV-2020-0830",
  "lines": [
    {"line_id": "1", "quantity": "10", "unit_price": "19.99", "line_amount": 19990},
    {"line_id": "2", "quantity": "3",  "unit_price": "0.05",  "line_amount": 15}
  ],
  "total_amount": 20005
}
```

失败 `422`（`seg` 为从 1 开始的段序号；与具体段无关的错误为 `null`）：

```json
{
  "error": {
    "code": "TDS_MISMATCH",
    "message": "TDS01 总额 20000 分与各行金额之和 20005 分不一致",
    "seg": 7,
    "segment": "TDS"
  }
}
```

调用示例：

```bash
curl -s http://localhost:8080/v1/verify \
  -H 'Content-Type: application/json' \
  -d '{"edi_text": "ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*RECEIVER1234567*000830*1200*U*00401*000000001*0*P*:~GS*IN*S*R*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV1~IT1*1*10*EA*19.99~TDS*19990~SE*5*0001~GE*1*42~IEA*1*000000001~"}'
```

## 请求 / 响应（POST /v1/issue-810）

由**结构化发票数据**生成 ASC X12 004010 **810 报文**。分隔符固定为元素 `*`、
组件 `:`、段终止符 `~`；ISA 段固定 106 字节（`ISA06`/`ISA08` 右侧补空格至
15 字符）。控制号两两配对（`ISA13=IEA02`、`GS06=GE02`、`ST02=SE02`），
`SE01` 为 ST..SE（含两端）段数，`GE01`/`IEA01` 均为 1；`TDS01` 为逐行
`ROUND_HALF_UP` 取整后的整数分合计（N2 隐含两位小数）。生成的报文不超过
**64 KiB**，且返回前经与 `/v1/verify` 完全相同的核验自检，保证可再提交
`/v1/verify`、`/v1/inbox`、`/v1/acknowledge` 等接口。

请求体（JSON）：

```json
{
  "isa05": "ZZ", "isa06": "SENDER123456789",
  "isa07": "ZZ", "isa08": "RECEIVER1234567",
  "isa09": "000830", "isa10": "1200", "isa13": "000000001",
  "gs02": "SENDERGS", "gs03": "RECVGS",
  "gs04": "20200830", "gs05": "1200", "gs06": "42",
  "st02": "0001",
  "big01": "20200830", "big02": "INV-2020-0830",
  "lines": [
    {"it101": "1", "it102": "10", "it104": "19.99"},
    {"it101": "2", "it102": "3",  "it104": "0.05"}
  ]
}
```

字段约束（所有字段均须为**纯 ASCII**，且不得包含分隔符 `*` `:` `~` 或控制字符）：

| 字段 | 约束 |
| --- | --- |
| `isa05` / `isa07` | 恰好 2 字符 |
| `isa06` / `isa08` | 1～15 字符（生成时右侧补空格至 15） |
| `isa09` | 6 位数字 `YYMMDD`，合法日历日期 |
| `isa10` | 4 位数字 `HHMM`，合法时间 |
| `isa13` | 恰好 9 位数字 |
| `gs02` / `gs03` | 1～15 字符 |
| `gs04` / `big01` | 8 位数字 `CCYYMMDD`，合法日历日期 |
| `gs05` | 4/6/8 位数字 `HHMM[SS[DD]]`，合法时间 |
| `gs06` / `st02` | 1～9 字符 |
| `big02` | 1～22 字符 |
| `lines` | 1～100 行 |
| `lines[].it101` | 1～20 字符，全局唯一 |
| `lines[].it102` | 十进制正数，至多 3 位小数，≤15 字符 |
| `lines[].it104` | 十进制非负，至多 4 位小数，≤17 字符 |

成功 `200`（`total_amount` 为 TDS01 整数分）：

```json
{
  "edi_text": "ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*RECEIVER1234567*000830*1200*U*00401*000000001*0*P*:~GS*IN*SENDERGS*RECVGS*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV-2020-0830~IT1*1*10*EA*19.99~IT1*2*3*EA*0.05~TDS*20005~SE*6*0001~GE*1*42~IEA*1*000000001~",
  "total_amount": 20005
}
```

失败 `422`（`field` 为请求字段名；行内字段形如 `lines[1].it101`，下标从 0 开始）：

```json
{
  "error": {
    "code": "IT101_DUPLICATE",
    "message": "IT101 行号重复: '1'",
    "field": "lines[1].it101"
  }
}
```

### 生成错误码（422）

| code | 含义 |
| --- | --- |
| `NOT_ASCII` | 字段含非 ASCII 字符 |
| `FIELD_EMPTY` / `FIELD_TOO_SHORT` / `FIELD_TOO_LONG` | 字段为空 / 短于下限 / 超出长度上限 |
| `SEPARATOR_INJECTION` | 字段含分隔符（`*` `:` `~`）或控制字符 |
| `ISA13_INVALID` | ISA13 不是恰好 9 位数字 |
| `DATE_INVALID` / `TIME_INVALID` | 日期（ISA09/GS04/BIG01）或时间（ISA10/GS05）非法 |
| `LINES_EMPTY` / `LINES_TOO_MANY` | 发票行数为 0 / 超过 100 |
| `IT101_DUPLICATE` | IT101 行号重复 |
| `IT102_INVALID` / `IT104_INVALID` | 数量非正或小数超 3 位 / 单价为负或小数超 4 位（或格式非法） |
| `PAYLOAD_TOO_LARGE` | 生成的报文超过 64 KiB（正常字段上限下不会触发） |

调用示例：

```bash
curl -s http://localhost:8080/v1/issue-810 \
  -H 'Content-Type: application/json' \
  -d '{
    "isa05": "ZZ", "isa06": "SENDER123456789",
    "isa07": "ZZ", "isa08": "RECEIVER1234567",
    "isa09": "000830", "isa10": "1200", "isa13": "000000001",
    "gs02": "SENDERGS", "gs03": "RECVGS",
    "gs04": "20200830", "gs05": "1200", "gs06": "42",
    "st02": "0001",
    "big01": "20200830", "big02": "INV-2020-0830",
    "lines": [
      {"it101": "1", "it102": "10", "it104": "19.99"},
      {"it101": "2", "it102": "3",  "it104": "0.05"}
    ]
  }'
```

## 请求 / 响应（POST /v1/reconcile）

按一张采购单对 **1～50 份**发票报文（`edi_texts`，按输入顺序）逐张核销。

请求体（JSON）：

```json
{
  "order_lines": [
    {"line_id": "1", "ordered_quantity": "10", "unit_price": "19.99"},
    {"line_id": "2", "ordered_quantity": "5",  "unit_price": "4.25"}
  ],
  "edi_texts": [
    "ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*RECEIVER1234567*000830*1200*U*00401*000000001*0*P*:~GS*IN*SENDERGS*RECVGS*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV-1~IT1*1*2*EA*19.99~IT1*2*1*EA*4.25~TDS*4423~SE*6*0001~GE*1*42~IEA*1*000000001~",
    "ISA*00*          ...第二份报文..."
  ]
}
```

- `order_lines`：采购单行。`line_id` 须唯一；`ordered_quantity` 为十进制字符串，
  必须为正、至多 3 位小数；`unit_price` 为十进制字符串，非负、至多 4 位小数。
  **采购单非法时整个请求返回 `422`**（错误体为 `{"error": {"code", "message"}}`）。
- `edi_texts`：1～50 份报文，每份要求同 `/v1/verify`（纯 ASCII、≤64 KiB）。

成功 `200`（逐张结果 + 各采购单行剩余数量）：

```json
{
  "results": [
    {"index": 0, "accepted": true,  "invoice_number": "INV-1", "error": null},
    {"index": 1, "accepted": false, "invoice_number": null,
     "error": {"code": "TDS_MISMATCH",
               "message": "TDS01 总额 1 分与各行金额之和 1999 分不一致",
               "seg": 6, "segment": "TDS", "line_id": null}},
    {"index": 2, "accepted": false, "invoice_number": "INV-3",
     "error": {"code": "QUANTITY_EXCEEDED",
               "message": "行 '1' 累计数量 11 超出订购数量 10",
               "seg": null, "segment": null, "line_id": "1"}}
  ],
  "remaining": [
    {"line_id": "1", "remaining_quantity": "0"},
    {"line_id": "2", "remaining_quantity": "4"}
  ]
}
```

### 核销规则

1. 每份报文先做与 `/v1/verify` 完全相同的核验（ASCII、64 KiB、X12 结构与金额）。
   **核验失败只拒绝该张**，`error` 中原样带回核验错误码、`seg`、`segment`，
   后续报文继续处理。
2. 通过核验的发票按 `IT101` 匹配采购单行号，`IT104` 与采购单单价按**十进制数值**
   精确比较（`"1.5"` 与 `"1.50"` 视为同一价格），`IT102` 逐行累计到对应采购单行。
3. 出现以下任一情况时**拒绝整张发票，且不占用任何数量**（后续发票继续按输入
   顺序核销）：
   - 发票行号不在采购单中（`PO_LINE_NOT_FOUND`）；
   - 单价与采购单不符（`UNIT_PRICE_MISMATCH`）；
   - 发票号（`BIG02`）与**已接受**的发票重复（`INVOICE_DUPLICATE`；
     被拒绝的发票不占用发票号，同号重发可被接受）；
   - 任一采购单行累计数量超出订购数量（`QUANTITY_EXCEEDED`；恰好等于订购
     数量时接受）。
4. 数量一律以 10⁻³ 整数（毫单位）累计、单价以 10⁻⁴ 整数比较，**全程不使用
   浮点数**；`remaining_quantity` 以最简十进制字符串返回（如 `"7.5"`、`"0"`）。

### 核销错误码（采购单 422）

| code | 含义 |
| --- | --- |
| `ORDER_LINE_ID_EMPTY` | 采购单行号为空 |
| `ORDER_LINE_DUPLICATE` | 采购单行号重复 |
| `ORDER_QUANTITY_INVALID` | 订购数量非正、格式非法或小数超过 3 位 |
| `ORDER_PRICE_INVALID` | 单价为负、格式非法或小数超过 4 位 |

### 核销错误码（逐张拒绝，见 `results[].error.code`）

| code | 含义 |
| --- | --- |
| `PO_LINE_NOT_FOUND` | `IT101` 行号不在采购单中 |
| `UNIT_PRICE_MISMATCH` | `IT104` 单价与采购单单价不符 |
| `INVOICE_DUPLICATE` | 发票号与已接受的发票重复 |
| `QUANTITY_EXCEEDED` | 累计数量超出订购数量 |
| （其余） | 原 `/v1/verify` 核验错误码，如 `NOT_ASCII`、`TDS_MISMATCH` 等 |

调用示例：

```bash
curl -s http://localhost:8080/v1/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "order_lines": [{"line_id": "1", "ordered_quantity": "10", "unit_price": "19.99"}],
    "edi_texts": ["ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*RECEIVER1234567*000830*1200*U*00401*000000001*0*P*:~GS*IN*S*R*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV1~IT1*1*10*EA*19.99~TDS*19990~SE*5*0001~GE*1*42~IEA*1*000000001~"]
  }'
```

## 请求 / 响应（POST /v1/split-810）

把**单份交换/单功能组内的 1～50 张 810**批量交换报文拆成单张报文，并逐张用
与 `/v1/verify` 完全相同的规则核验。请求体 `edi_text` 必须为纯 ASCII，
大小不超过 **1 MiB（1048576 字节）**。

请求体（JSON）：

```json
{
  "edi_text": "ISA*00*          *00*          *ZZ*...*:~GS*IN*S*R*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV1~IT1*1*10*EA*19.99~TDS*19990~SE*5*0001~ST*810*0002~BIG*20200830*INV2~IT1*1*2*EA*4.25~TDS*850~SE*5*0002~GE*2*42~IEA*1*000000001~"
}
```

成功 `200`（`invoices` 严格按原报文顺序；每张 `edi_text` 为重建的单张
004010 810 报文，可直接再提交 `/v1/verify`、`/v1/inbox`、`/v1/acknowledge`）：

```json
{
  "count": 2,
  "invoices": [
    {
      "index": 0,
      "st02": "0001",
      "invoice_number": "INV1",
      "edi_text": "ISA*00*          *...*ST*810*0001~BIG*20200830*INV1~IT1*1*10*EA*19.99~TDS*19990~SE*5*0001~GE*1*42~IEA*1*000000001~"
    },
    {
      "index": 1,
      "st02": "0002",
      "invoice_number": "INV2",
      "edi_text": "ISA*00*          *...*ST*810*0002~BIG*20200830*INV2~IT1*1*2*EA*4.25~TDS*850~SE*5*0002~GE*1*42~IEA*1*000000001~"
    }
  ]
}
```

### 拆分规则

1. **分隔符**：从 106 字节 `ISA` 段的固定位置读取（偏移 3 元素分隔符、
   104 组件分隔符、105 段终止符），合法性与互不冲突规则同 `/v1/verify`；
   拆出的每张报文沿用原分隔符（支持非 `*` `:` `~` 的自定义分隔符与
   CR/LF 段终止符）。
2. **批外壳**：报文结构固定为
   `ISA → GS → (ST → … → SE){1..50} → GE → IEA`；
   `ISA12=00401`、`GS08=004010`；恰为一份交换、一组。
3. **控制号配对**：`ISA13=IEA02`、`GS06=GE02`、每个交易集 `ST02=SE02`；
   `GE01` 必须等于实际交易集数量、`IEA01=1`；各交易集 `ST01=810`、
   `ST02` 非空且在本批内**不重复**；ST 与 SE 之间不允许再出现
   `ISA/GS/ST/GE/IEA` 信封段。
4. **逐张重建**：每个交易集的 ST…SE 业务段与原 `ST02` **原文保留**，
   重新生成单张信封 `ISA → GS → 原 ST…SE → GE(GE01=1, GE02=GS06) →
   IEA(IEA01=1, IEA02=ISA13)`，再用与 `/v1/verify` 完全相同的规则核验
   （外壳、`SE01` 段数、`IT1`/`TDS` 金额等），每份结果必须为纯 ASCII 且
   **≤64 KiB**。
5. **整批失败语义**：批外壳非法或任一交易集核验失败时，整个请求返回
   `422`，**不返回任何部分结果**。错误体在 `/v1/verify` 错误体基础上增加
   `transaction_set`（批内交易集序号，**从 1 开始**；ISA/GS/GE/IEA 等
   信封级错误为 `null`）；`seg` 始终指向**原批量报文**中从 1 开始的段
   序号（ISA = 1；与具体段无关的错误为 `null`），`segment` 为段标识：

```json
{
  "error": {
    "code": "TDS_MISMATCH",
    "message": "TDS01 总额 1 分与各行金额之和 2000 分不一致",
    "seg": 11,
    "segment": "TDS",
    "transaction_set": 2
  }
}
```

### 批量拆分错误码（422）

| code | 含义 |
| --- | --- |
| `EMPTY_INPUT` / `NOT_ASCII` / `PAYLOAD_TOO_LARGE` | 报文为空 / 非 ASCII / 超过 1 MiB（逐张核验阶段超过 64 KiB 时复用同一 code） |
| `ISA_*`、`GS08_BAD_VERSION`、`INVALID_*SEPARATOR*`、`SEPARATOR_CONFLICT` | ISA/分隔符/版本错误（同 `/v1/verify`） |
| `ST01_NOT_810` / `ST02_EMPTY` / `ST02_DUPLICATE` | 交易集类型非 810 / ST02 为空 / 批内 ST02 重复 |
| `ST_SE_CONTROL_MISMATCH` / `GS_GE_CONTROL_MISMATCH` / `ISA_IEA_CONTROL_MISMATCH` | 控制号配对不一致 |
| `GE01_INVALID` / `GE01_COUNT_MISMATCH` / `IEA01_INVALID` / `IEA01_NOT_ONE` | GE01/IEA01 非数字、GE01 与交易集数不符、IEA01 不为 1 |
| `SPLIT_NO_TRANSACTION_SETS` / `SPLIT_TOO_MANY_TRANSACTION_SETS` | 交易集数量为 0 / 超过 50 |
| `UNEXPECTED_SEGMENT` / `EMPTY_SEGMENT` / `MISSING_SEGMENT` / `TRAILING_DATA` / `SEGMENT_TOO_FEW_ELEMENTS` | 段顺序错误、空段、未正常闭合、IEA 后多余数据、段元素不足 |
| （其余） | 逐张核验返回的原 `/v1/verify` 错误码（如 `TDS_MISMATCH`、`SE01_WRONG_COUNT`、`IT101_DUPLICATE` 等），`seg` 已映射回原报文段序号 |

调用示例：

```bash
curl -s http://localhost:8080/v1/split-810 \
  -H 'Content-Type: application/json' \
  -d '{"edi_text": "ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*RECEIVER1234567*000830*1200*U*00401*000000001*0*P*:~GS*IN*S*R*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV1~IT1*1*10*EA*19.99~TDS*19990~SE*5*0001~ST*810*0002~BIG*20200830*INV2~IT1*1*2*EA*4.25~TDS*850~SE*5*0002~GE*2*42~IEA*1*000000001~"}'
```

返回的任一 `invoices[].edi_text` 都可原样作为 `/v1/verify` 的 `edi_text`
再次提交。

## 请求 / 响应（POST /v1/acknowledge）

对**单份** 810 报文生成 ASC X12 004010 **997 功能回执**。

请求体（JSON）：

```json
{
  "edi_text": "ISA*00*          *00*          *...810 报文...",
  "isa13": "000000002",
  "gs06": "43",
  "st02": "0002"
}
```

- `isa13`：回执 ISA13 交换控制号，**恰好 9 位数字**；
- `gs06` / `st02`：回执 GS06 组控制号 / ST02 交易集控制号，**非空**，且不得
  包含来报分隔符或控制字符。

成功 `200`，`edi_text` 为完整 997 报文：

```json
{
  "edi_text": "ISA*00*          *00*          *ZZ*RECEIVER1234567*ZZ*SENDER123456789*000830*1200*U*00401*000000002*0*P*:~GS*FA*RECVGS*SENDERGS*20200830*1200*43*X*004010~ST*997*0002~AK1*IN*42~AK2*810*0001~AK5*A~AK9*A*1~SE*6*0002~GE*1*43~IEA*1*000000002~"
}
```

当来报存在可识别错误（例如缺少 `SE`，或 `GS08` 不为 `004010`）时同样返回
`200`，但回执为拒绝：仍沿用来报分隔符、互换收发方并引用原 `GS06`/`ST02`，
`AK5`/`AK9` 为 `R`、接收数为 `0`（组级错误无 `AK3`；交易集内可定位错误
带 `AK3`）：

```json
{
  "edi_text": "ISA*00*          *00*          *ZZ*RECEIVER1234567*ZZ*SENDER123456789*000830*1200*U*00401*000000002*0*P*:~GS*FA*RECVGS*SENDERGS*20200830*1200*43*X*004010~ST*997*0002~AK1*IN*42~AK2*810*0001~AK5*R~AK9*R*0~SE*6*0002~GE*1*43~IEA*1*000000002~"
}
```

### 回执生成规则

1. **仅当来报外壳可回执时才生成 997**：ISA 可解析（106 字节、分隔符合法、
   `ISA12=00401`）、恰为一份交换/功能组且 `GS`/`ST` 在前、`GE`/`IEA` 正常收尾
   （**缺 `GE`/`IEA` 不可回执**）、收发方（ISA05/06、ISA07/08）及 `GS06`/`ST02`
   可提取（`GS01..GS07` 齐全、`ST01=810`）。满足上述前提但存在以下**可识别来报
   错误**时，不返回 422，而是生成 **R 拒绝回执**（沿用来报分隔符、引用原
   `GS06`/`ST02`、`AK5*R`/`AK9*R*0`）：
   - 缺少 `SE`（或 `SE` 畸形、未在 `GE` 之前正常收尾）；
   - `GS08` 不为 `004010`（回执自身的 `GS08` 仍为 `004010`）。

   其余不可回执情形返回 `422`，错误体**原样带回来报核验的错误码**
   （如 `MISSING_SEGMENT`、`TRAILING_DATA`、`NOT_ASCII` 等）。
2. **A/R 复用现有发票核验**（与 `/v1/verify` 完全相同的规则）：
   - 核验通过：`AK5*A`、`AK9*A*1`（接收数为 1），不输出 AK3；
   - 核验失败：`AK5*R`、`AK9*R*0`（接收数为 0）；错误定位在**交易集内**
     （`ST` 与 `GE` 之间，含 `SE`）时输出 `AK3*<段标识>*<位置>`，位置
     **从 ST 起算**（ST = 1，如 TDS 段为 4、正常位置的 SE 为 5）；`GS`/`GE`/
     `IEA` 等组级/交换级段错误不输出 AK3（因此缺 `SE` 时错误落在 `GE`、
     `GS08` 错误落在 `GS`，均无 AK3；若交易集内另有可定位错误则仍输出 AK3）。
3. **回执外壳**：沿用来报的元素/组件/段终止符；ISA 与 GS 的收发方**互换**
   （ISA05/06 ↔ ISA07/08、GS02 ↔ GS03），`AK1` 引用来报 `GS01`/`GS06`，
   `AK2` 引用 `810` 与来报 `ST02`；回执自身的 `ST02=SE02`、`GS06=GE02`、
   `ISA13=IEA02` 两两配对，`SE01` 段数与 `GE01`/`IEA01`（均为 1）计数自洽。

### 回执错误码（422）

| code | 含义 |
| --- | --- |
| `ACK_ISA13_INVALID` | 回执 ISA13 不是恰好 9 位数字 |
| `ACK_GS06_EMPTY` / `ACK_ST02_EMPTY` | 回执 GS06 / ST02 为空 |
| `ACK_GS06_INVALID` / `ACK_ST02_INVALID` | 回执 GS06 / ST02 含非 ASCII、分隔符或控制字符 |
| `ACK_ENVELOPE_UNUSABLE` | 外壳可解析但收发方或 GS06/ST02 无法提取（缺 SE、GS08 非 004010 等可识别来报错误不返回 422，而是生成 R 拒绝回执） |
| （其余） | 来报核验的原始错误码，如 `MISSING_SEGMENT`、`TRAILING_DATA` 等 |

调用示例：

```bash
curl -s http://localhost:8080/v1/acknowledge \
  -H 'Content-Type: application/json' \
  -d '{
    "edi_text": "ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*RECEIVER1234567*000830*1200*U*00401*000000001*0*P*:~GS*IN*SENDERGS*RECVGS*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV1~IT1*1*10*EA*19.99~TDS*19990~SE*5*0001~GE*1*42~IEA*1*000000001~",
    "isa13": "000000002",
    "gs06": "43",
    "st02": "0002"
  }'
```

## 请求 / 响应（POST /v1/ack-status）

给定本次**发送清单**与一份 ASC X12 004010 **997 功能回执**报文，解析回执中
各 810 交易集的接受状态。回执报文必须为纯 ASCII、不超过 **64 KiB**，分隔符
从其 106 字节 ISA 段读取（规则同 `/v1/verify`）。

请求体（JSON）：

```json
{
  "gs06": "42",
  "st02": ["0001", "0002", "0003"],
  "edi_text": "ISA*00*          *00*          *ZZ*RECEIVER1234567*ZZ*SENDER123456789*000830*1200*U*00401*000000002*0*P*:~GS*FA*RECVGS*SENDERGS*20200830*1200*42*X*004010~ST*997*9001~AK1*IN*42~AK2*810*0001~AK5*A~AK2*810*0002~AK5*R~AK9*P*3*3*2~SE*8*9001~GE*1*42~IEA*1*000000002~"
}
```

- `gs06`：发送清单的组控制号，**非空**；回执 `AK102` 必须引用它。
- `st02`：**1～100 个**交易集控制号，**非空且唯一**（重复或为空整次 `422`）。
- `edi_text`：完整 997 报文，纯 ASCII、≤64 KiB。

成功 `200`，`items` **严格按清单顺序**返回每一项的状态：

```json
{
  "items": [
    {"st02": "0001", "status": "A"},
    {"st02": "0002", "status": "R"},
    {"st02": "0003", "status": "A"}
  ]
}
```

### 回执结构与计数规则

报文限**单交换 / 单功能组 / 单交易集**，信封顺序固定：

```
ISA → GS → ST → AK1 → (AK2 → AK5) × 0..n → AK9 → SE → GE → IEA
```

1. **信封**：`ISA12=00401`、`GS01=FA`、`GS08=004010`、`ST01=997`；
   控制号配对 `ISA13=IEA02`、`GS06=GE02`、`ST02=SE02`；`GE01=IEA01=1`；
   `SE01` 等于 ST 到 SE（含两端）的段数。
2. **AK1**：恰好 `AK101/AK102` 两个元素，`AK102` 必须等于清单 `gs06`。
3. **AK2/AK5 对**：零至多个，每个 `AK2` 必须紧跟一个 `AK5`；`AK201=810`，
   `AK202` 必须引用清单中**已知且未重复**的 `st02`；`AK501` 仅允许 `A`/`R`。
4. **AK9**：恰好四个元素 `状态*总数*收到数*接受数`：
   - 状态仅 `A`/`R`/`P`；
   - **总数、收到数均必须等于清单长度**（否则 `AK9_COUNT_MISMATCH`）；
   - `A` 要求接受数等于清单长度（全接受）；`R` 要求接受数为 0（全拒绝）；
     `P` 要求 0 < 接受数 < 清单长度（部分接受），不符为
     `AK9_STATUS_CONTRADICTION`。
5. **未列项推断**：用 AK9 接受数扣除明细（各 `AK5=A`）的接受数得到剩余
   接受数 `residual`，设未列项数为 `unlisted`：
   - `residual == unlisted`：未列项**全部接受**（`A`）；
   - `residual == 0`：未列项**全部拒绝**（`R`）；
   - 其余：未列项状态**未知**（`U`）；
   - `residual` 不在 `[0, unlisted]`（明细接受数已超过 AK9 接受数等）为
     计数矛盾，返回 `422`（`AK9_ACCEPTED_CONTRADICTION`）。
6. 任何结构非法（段缺失/顺序错误、段元素过多或过少、出现 AK1/AK2/AK5/AK9
   之外的正文段、IEA 后多余数据等）或计数矛盾，整次返回 `422`，错误体带
   **从 1 开始的段序号** `seg` 与段标识 `segment`（与段无关的清单错误
   `seg` 为 `null`）。

> 注：`POST /v1/acknowledge` 生成的回执 `AK9` 只带 `状态*接收数` 两个元素
> （用于单交易集回执）；本接口按 004010 完整 997 语法要求四个元素，因此
> 那份报文提交到本接口会返回 `AK9_ELEMENT_COUNT`（422）。

### ack-status 错误码（422）

| code | 含义 |
| --- | --- |
| `EMPTY_INPUT` / `NOT_ASCII` / `PAYLOAD_TOO_LARGE` | edi_text 为空 / 非 ASCII / 超过 64 KiB |
| `ACK_MANIFEST_GS06_EMPTY` / `ACK_MANIFEST_ST02_EMPTY` / `ACK_MANIFEST_ST02_DUPLICATE` | 清单 gs06 为空 / st02 为空 / st02 重复 |
| `ISA_*`、`INVALID_*SEPARATOR*`、`SEPARATOR_CONFLICT` | ISA 与分隔符错误（同 `/v1/verify`） |
| `GS01_NOT_FA` / `GS08_BAD_VERSION` / `ST01_NOT_997` | 功能标识、版本或交易集类型不符 |
| `UNEXPECTED_SEGMENT` / `EMPTY_SEGMENT` / `MISSING_SEGMENT` / `TRAILING_DATA` | 段顺序错误、空段、未正常闭合、IEA 后多余数据 |
| `SEGMENT_TOO_FEW_ELEMENTS` | 段元素个数不足 |
| `AK1_ELEMENT_COUNT` / `AK2_ELEMENT_COUNT` / `AK5_ELEMENT_COUNT` | AK1/AK2/AK5 元素数多于规定 |
| `AK101_EMPTY` | AK101 功能标识符为空 |
| `AK1_GS06_MISMATCH` | AK102 未引用清单 gs06 |
| `AK201_NOT_810` / `AK2_ST02_UNKNOWN` / `AK2_ST02_DUPLICATE` | AK201 非 810 / 引用未知 st02 / st02 重复 |
| `AK501_INVALID` / `AK901_INVALID` | AK501 非 A/R、AK901 非 A/R/P |
| `AK9_ELEMENT_COUNT` / `AK9_COUNT_INVALID` | AK9 元素数不为 4 / 计数不是非负整数 |
| `AK9_COUNT_MISMATCH` | AK9 总数或收到数不等于清单长度 |
| `AK9_ACCEPTED_OUT_OF_RANGE` | AK9 接受数超出 `[0, 清单长度]` |
| `AK9_STATUS_CONTRADICTION` | A/R/P 状态与接受数语义矛盾 |
| `AK9_ACCEPTED_CONTRADICTION` | 接受数与明细接受数、未列项数矛盾 |
| `SE01_INVALID` / `SE01_WRONG_COUNT`、`GE01_*`、`IEA01_*`、`*_CONTROL_MISMATCH` | 段数、计数与控制号配对错误（同 `/v1/verify`） |

调用示例：

```bash
curl -s http://localhost:8080/v1/ack-status \
  -H 'Content-Type: application/json' \
  -d '{
    "gs06": "42",
    "st02": ["0001", "0002", "0003"],
    "edi_text": "ISA*00*          *00*          *ZZ*RECEIVER1234567*ZZ*SENDER123456789*000830*1200*U*00401*000000002*0*P*:~GS*FA*RECVGS*SENDERGS*20200830*1200*42*X*004010~ST*997*9001~AK1*IN*42~AK2*810*0001~AK5*A~AK2*810*0002~AK5*R~AK9*P*3*3*2~SE*8*9001~GE*1*42~IEA*1*000000002~"
  }'
```

## 请求 / 响应（POST /v1/inbox 与 GET /v1/inbox/{receipt_id}）

**幂等收件库**：接收单份 810 报文，先按与 `/v1/verify` 完全相同的规则核验，
核验通过后在 SQLite 中**原子**保存原文与核验结果，并返回固定 `receipt_id`。

请求体（JSON，同 `/v1/verify`）：

```json
{
  "edi_text": "ISA*00*          *00*          *..."
}
```

首次提交 `200`（`replayed=false`）：

```json
{
  "receipt_id": "a872fb5d6bc63cbc4be99f73f473e69aac5ba3a8958912fc004cdddafb95588a",
  "replayed": false,
  "invoice_number": "INV1",
  "lines": [
    {"line_id": "1", "quantity": "10", "unit_price": "19.99", "line_amount": 19990}
  ],
  "total_amount": 19990
}
```

- **核验失败**：原样返回 `/v1/verify` 的 `422` 错误体，**不写入收件库**。
- **收件键**：`(ISA06, GS02, BIG02)` —— 发送方标识 + 应用发送方代码 + 发票号。
- **业务指纹**（SHA-256）：BIG 段全部元素（原文）、按 `IT101` 排序的各 IT1 段
  （其中 `IT102` 数量规范为 **10⁻³ 千分位整数**、`IT104` 单价规范为
  **10⁻⁴ 万分位整数**，故 `"1.5"` 与 `"1.50"`、`"19.99"` 与 `"19.9900"`
  指纹相同）、`TDS` 整数分。**信封控制号**（ISA13/GS06/ST02/SE02/GE02/IEA02）
  **与 IT1 行顺序不参与**指纹。
- **`receipt_id` 固定**：由收件键与业务指纹共同哈希确定，同一收件内容无论
  何时提交、无论服务是否重启，`receipt_id` 都不变。
- **同键同指纹重发**：返回原 `receipt_id` 与 `replayed=true`（响应中的核验
  结果为首次保存的记录）。
- **同键不同指纹**：返回 `409`，**原记录不改动**：

```json
{
  "error": {
    "code": "INBOX_CONFLICT",
    "message": "收件键 (ISA06='SENDER123456789', GS02='S', BIG02='INV1') 已保存过业务内容不同的发票，本次提交被拒绝，原记录未改动"
  }
}
```

- **并发**：同一收件键的并发提交遵守同一组规则——同一指纹的并发提交中
  恰好一次为 `replayed=false`，其余为 `replayed=true`；不同指纹的并发提交
  中只有一组能入库，其余全部 `409`。

`GET /v1/inbox/{receipt_id}` 返回**首次保存**的原文与核验结果：

```json
{
  "receipt_id": "a872fb5d6bc63cbc4be99f73f473e69aac5ba3a8958912fc004cdddafb95588a",
  "edi_text": "ISA*00*          *00*          *...",
  "invoice_number": "INV1",
  "lines": [
    {"line_id": "1", "quantity": "10", "unit_price": "19.99", "line_amount": 19990}
  ],
  "total_amount": 19990
}
```

`receipt_id` 不存在时返回 `404`（`{"error": {"code": "RECEIPT_NOT_FOUND", ...}}`）。

### 持久化

数据库文件为 SQLite，路径取环境变量 `INBOX_DB_PATH`（镜像内默认
`/data/inbox.db`，本地开发默认 `./inbox.db`）。Compose 将命名卷
`inbox-data` 挂载到 `/data`，因此 `docker compose restart` 或重建容器后
仍能识别重发；`docker compose down -v` 会连同数据一起删除。

调用示例：

```bash
# 首次提交 -> replayed=false；重复提交同一报文 -> replayed=true，receipt_id 不变
curl -s http://localhost:8080/v1/inbox \
  -H 'Content-Type: application/json' \
  -d '{"edi_text": "ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*RECEIVER1234567*000830*1200*U*00401*000000001*0*P*:~GS*IN*S*R*20200830*1200*42*X*004010~ST*810*0001~BIG*20200830*INV1~IT1*1*10*EA*19.99~TDS*19990~SE*5*0001~GE*1*42~IEA*1*000000001~"}'

# 取回收件记录（替换为实际 receipt_id）
curl -s http://localhost:8080/v1/inbox/a872fb5d6bc63cbc4be99f73f473e69aac5ba3a8958912fc004cdddafb95588a
```

## 核验规则

报文结构（顺序固定，各 1 份，`IT1` 为 1～100 个）：

```
ISA → GS → ST → BIG → IT1(1..100) → TDS → SE → GE → IEA
```

- **分隔符**：ISA 段为固定 106 字节。字节偏移 3 取元素分隔符，偏移 104 取组件分隔符，
  偏移 105 取段终止符；三者必须互不相同且非字母数字。段终止符之后允许 CR/LF 排版空白。
- **版本**：`ISA12 == "00401"`、`GS08 == "004010"`、`ST01 == "810"`。
- **控制号配对**：`ISA13 == IEA02`、`GS06 == GE02`、`ST02 == SE02`。
- **计数**：`GE01 == 1`、`IEA01 == 1`；`SE01` 等于从 ST 到 SE（含两端）的段数。
- **业务字段**：`BIG02` 发票号非空；各 `IT101` 行号非空且全局唯一。
- **金额**：
  - `IT102` 数量为正，小数位至多 3 位；
  - `IT104` 单价非负，小数位至多 4 位；
  - 每行以整数定点数精确相乘（不受 Decimal 默认精度限制），按 `ROUND_HALF_UP` 取整到 0.01（整数分）；
  - 各行整数分之和必须等于 `TDS01`。`TDS01` 按 X12 `N2` 隐含两位小数解析
    （`"19990"` = 199.90），也接受显式小数（`"199.90"`，至多两位）。

### 错误码

| code | 含义 |
| --- | --- |
| `EMPTY_INPUT` | edi_text 为空 |
| `NOT_ASCII` | 含非 ASCII 字节 |
| `PAYLOAD_TOO_LARGE` | 超过 64 KiB |
| `ISA_NOT_FIRST` / `ISA_TOO_SHORT` / `ISA_MALFORMED` | ISA 不在开头 / 不足 106 字节 / 元素结构错误 |
| `INVALID_ELEMENT_SEPARATOR` / `INVALID_SEGMENT_TERMINATOR` / `INVALID_COMPONENT_SEPARATOR` / `SEPARATOR_CONFLICT` | 分隔符不合法或互相冲突 |
| `ISA12_BAD_VERSION` / `GS08_BAD_VERSION` / `ST01_NOT_810` | 版本或交易集类型不符 |
| `UNEXPECTED_SEGMENT` / `EMPTY_SEGMENT` / `MISSING_SEGMENT` / `TRAILING_DATA` | 段顺序错误、空段、未正常闭合、IEA 后多余数据 |
| `SEGMENT_TOO_FEW_ELEMENTS` | 段元素个数不足 |
| `BIG02_EMPTY` | 发票号为空 |
| `IT1_MISSING` / `IT1_TOO_MANY` / `IT101_EMPTY` / `IT101_DUPLICATE` | IT1 数量/行号问题 |
| `IT102_INVALID` / `IT104_INVALID` | 数量或单价格式/取值错误 |
| `TDS01_INVALID` / `TDS_MISMATCH` | TDS01 格式错误 / 与行合计不一致 |
| `SE01_INVALID` / `SE01_WRONG_COUNT` | SE01 格式错误 / 段数不符 |
| `GE01_INVALID` / `GE01_NOT_ONE` / `IEA01_INVALID` / `IEA01_NOT_ONE` | 计数字段错误 |
| `ISA_IEA_CONTROL_MISMATCH` / `GS_GE_CONTROL_MISMATCH` / `ST_SE_CONTROL_MISMATCH` | 控制号配对不一致 |

## 本地开发（不用 Docker）

Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

收件库默认写到当前目录的 `inbox.db`；用环境变量 `INBOX_DB_PATH` 指定其他路径：

```bash
INBOX_DB_PATH=/var/tmp/inbox.db uvicorn app.main:app --host 0.0.0.0 --port 8080
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest -q
```

## 仓库结构

```
app/main.py        FastAPI 路由、请求/响应模型、422/409/404 异常处理
app/verifier.py    ISA 分隔符解析、外壳状态机、整数定点金额核验
app/issuer.py      结构化发票校验与 004010 X12 810 报文生成（固定 * : ~ 分隔符）
app/reconciler.py  采购单校验与多发票逐张核销（整数毫单位，无浮点）
app/splitter.py    批量交换报文（单组 1～50 个 810 交易集）拆分与逐张核验
app/acknowledger.py 外壳可回执性提取与 004010 X12 997 回执生成
app/ack_status.py  发送清单校验与 004010 X12 997 回执状态解析（A/R/U 推断）
app/inbox.py       收件键/业务指纹计算与 SQLite 幂等收件库
tests/test_verify.py      /v1/verify 端到端/单元测试（39 个）
tests/test_issue_810.py   /v1/issue-810 端到端测试（23 个）
tests/test_reconcile.py   /v1/reconcile 端到端测试（25 个）
tests/test_split_810.py   /v1/split-810 端到端测试（40 个）
tests/test_acknowledge.py /v1/acknowledge 端到端测试（19 个）
tests/test_ack_status.py  /v1/ack-status 端到端测试（48 个）
tests/test_inbox.py       /v1/inbox 幂等收件端到端测试（17 个）
Dockerfile         python:3.12-slim 镜像，以 nobody 运行，INBOX_DB_PATH=/data/inbox.db
docker-compose.yml 映射 8080 端口，命名卷 inbox-data 持久化收件库，含 /health 健康检查
```
