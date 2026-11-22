# 时档（HourSpan）

**骨架：** 门店营业属性贴源入档 → 业务键变更检测 → Type2 历史行开闭 → 当前镜投影 → 历史与当前互证

**已落模块 `as_of_query_proof`（时点投影管道，非运营查询台）：**
- 点查：`POST /api/as-of/query`（store_id+as_of_ts → 落在 `valid_from≤t<valid_to` 的 hist 段；无覆盖明确未命中，只读 `dim_store_hours_hist`，禁止当前镜顶替）
- 证明落库**形态甲**：独立 `as_of_proofs` 表（查询键 / 命中 hist_id / 属性 hash / 未命中标记 / 回执快照）
- 命中引用**形态甲（引用齐套为成功条件）**：同事务写 `as_of_hit_cites`（store_id / as_of / hist_id / valid 区间 / row_hash / 未命中标记），并与本次实命中段逐字段对撞；**无引用不得返回成功体**
- 失败闭**路径甲**：proof、引用与查询结果同事务，异常整体回滚；`fault=proof_insert` / `fault=cite_insert` 夹具故障可验「半截写不留假命中、不留无引用成功回执」
- 核证只读：`GET /api/as-of/proofs`、`GET /api/as-of/verify`（对已提交 hist 重推导逐条比对，不落库）、`GET /api/as-of/hit-cites`、`GET /api/as-of/cite-verify`（缺引用显式失败，绝不当通过；分叉 / 邻键串改 / 当前镜冒充均被逮到）；前端「时点投影」页与历史维「段首点查」联动，「核证」页展示命中引用

- 领域：数据工程（去皮=维表 SCD / 历史开闭 / 当前镜 / 核证；不是门店后台 CRUD）
- 端口：前端 **6200**，接口 **11200**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/hourspan-NN-*`
- 栈：Python 3.12 + FastAPI + SQLite；前端 **React + Vite**
- 避让：勿撞筑层分层汇总、价舱改价调度、信汇多源去重、变流 CDC 游标、格湾占用态

本地冒烟：

```powershell
cd claude-workspace/hourspan/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ../frontend
npm install
npm run dev
```

或：`docker compose up -d --build`（6200 / 11200）
