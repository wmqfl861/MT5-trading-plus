# 台账链头锚定（T37/M6/D27）

由 `scripts/ledger_anchor.py` 生成；管理员每日提交本文件（计划任务）。
链头 = 各链式表按 rowid 最后一行的 `row_hash`；行数为该表当前行数。
与本文件记录不符的台账即发生过改写/删行（即便整链被重算）——
比对用 `bt ledger verify` + 本表。

| 锚定 UTC | 台账 | 表 | 行数 | 链头 row_hash |
| --- | --- | --- | --- | --- |
| 2026-10-25T17:23:56.020981+00:00 | data/research/ledger.sqlite | runs | 186 | 162199b5ee7abfcbd8d89f075d2f041e2700f874caf648efa342a6552a18cec0 |
| 2026-10-25T17:23:56.020981+00:00 | data/research/ledger.sqlite | gates | 40 | 1ecb76981af61e3392cc90426a1441ac3381a5dc86369bc555d16cfe8a46bcca |
| 2026-10-25T17:23:56.020981+00:00 | data/research/ledger.sqlite | lockbox_uses | 0 | （空表） |
