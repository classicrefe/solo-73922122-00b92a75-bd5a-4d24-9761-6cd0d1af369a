# 正射图幅配准核查（平移量 · 两条独立测量路径）

Flask + SQLite 的网页：每个项目记录**一个地物**（同名地物）在若干正射图幅上的
**至多 120 条测量**。每条测量填写唯一测量号、两张图幅，以及该地物在两张图幅上
分别量得的**整数米坐标**。网页选择**基准片**（平移量为零）与**待配准片**，
按“两条不共用测量的路径”独立复核待配准片的平移量。

## 运行（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- 端口：`ADJCHECK_PORT=8080 docker compose up --build`。
- 数据：命名卷 `adjdata`（容器内 `/data/adjcheck.sqlite3`），`docker compose down`
  不丢数据，`docker compose down -v` 清空。
- 容器以非 root 运行，带 `/healthz` 健康检查；gunicorn 单 worker + 4 线程。

首次启动自动建立三个示例项目：

| 项目 | 地物 | 图幅 | 演示 |
| --- | --- | --- | --- |
| `DEMO-REG-001` | 独立树 | A、B、C（均 `0 0 100 100`） | A→C 已核实：C 平移 `(3, -2)`，两条路径 `M1→M2`（A-B-C）与 `M3`（A-C） |
| `DEMO-REG-002` | 涵洞 | P、Q、R | P→R **失联**（P-Q 有测量，R 无有效连接） |
| `DEMO-REG-003` | 水塔 | X、Y | X→Y **测量矛盾**（K1、K2 推算的平移量不同） |

### 演示更正前后

`DEMO-REG-001` 的测量（各自图幅上的量测坐标）：

| 测量号 | 图幅 A | 图幅 B | 图幅 C |
| --- | --- | --- | --- |
| M1 | (10, 10) | (12, 9) | — |
| M2 | — | (40, 20) | (35, 23) |
| M3 | (60, 60) | — | (57, 62) |
| M4 | (30, 70) | (32, 69) | — |

正确平移量 A=(0,0)、B=(-2,1)、C=(3,-2)。页面/演示脚本按以下两步展示更正前后：

1. **更正前**（目录 `A 0 0 100 100 / B 0 0 100 100 / C 0 0 100 100`）：
   A→C 判为**已核实**，平移 `(3, -2)`，路径 `M1→M2` 与 `M3`。
2. **更正图幅**为 `C 0 0 57 100`（在页面“图幅更正”表单提交，或用脚本）：
   测量原坐标不变；因坐标范围**含下界、不含上界**，M3 在 C 上的点 `x=57`
   越界而失效，只剩一条路径，A→C 改判**证据不足（1 条路径）**，
   平移量不再给出，页面同时列出失效原因与 M3 的原坐标。

```bash
# 服务启动后，另开终端：
./scripts/demo.sh
# 或指定地址 / 项目 / 更正内容：
BASE=http://localhost:8080 SHEETS="C 0 0 58 100" ./scripts/demo.sh
```

`SHEETS="C 0 0 58 100"` 时 M3 仍有效，更正前后都判**已核实**，用于对照。

## 规则实现

**测量有效性**（`service.measurement_validity`）：测量点落在对应**现行**图幅的
半开矩形内（`x0 ≤ x < x1`、`y0 ≤ y < y1`）才参与核查；图幅被更正后重新判定，
测量表中保存的**原始坐标永不改写**，页面始终保留供复核。

**几何相容**：基准片平移为零。若测量 `i` 在片 A、B 上的点为 `pA`、`pB`，
则要求 `pA + T_A == pB + T_B`。从基准片按有效测量做 BFS 传播，得到组内各片
的唯一候选平移；传播后再逐条回代，任何一条测量与组内传播值不一致即判
**测量矛盾**。

**连通与路径**：只有待配准片在基准片所在的“有效测量连通组”内才可能给出平移量，
否则提示**失联**。组内相容后，用“测量容量为 1”的最大流（每条测量切成容量 1 的
中间节点，图幅节点不限容量）求两条**不共用测量**的路径——两条路径可以共用图幅。
只有恰好找到两条才判**已核实**，返回平移量与两条路径各自的测量号和图幅序列；
只有 0/1 条时判**证据不足**。判定优先级：失联 → 测量矛盾 → 证据不足 → 已核实。

**一次核查使用同一快照**：`Database.get_snapshot` 在一个读事务中同时取出项目、
图幅目录和测量表；核查只读这份快照。计算期间别人更正图幅不会改变本次核查的
输入，下一次核查读取新快照并按新图幅重判有效性。更正写入用 `BEGIN IMMEDIATE`
并在同一事务内校验页面版本（乐观锁），过期页面得到 409，目录与测量表保持原状。

**整数口径**：坐标、平移量都是整数米，比较是精确相等，无浮点容差。

## 页面与接口

页面 `/projects/<name>`：图幅目录、基准片/待配准片选择、核查结论（状态、平移量、
两条路径、矛盾明细、未参与核查的测量及原因）、测量表与录入表单、图幅更正、更正记录。

| 方法/路径 | 说明 |
| --- | --- |
| `GET /` | 项目目录 |
| `POST /projects` | 新建项目（项目名、地物名、图幅清单） |
| `POST /projects/<name>/delete` | 删除项目（级联图幅、测量、更正记录） |
| `GET /projects/<name>?reference=A&target=C` | 项目页并执行一次核查 |
| `POST /projects/<name>/measurements` | 表单录入测量（无 JS 也可用） |
| `POST /projects/<name>/correct` | 表单提交图幅更正（带 `version` 隐藏域） |
| `POST /api/projects/<name>/verify` | JSON 核查：`{"reference":"A","target":"C"}` |
| `POST /api/projects/<name>/measurements` | JSON 录入：`{"no","sheet_a","ax","ay","sheet_b","bx","by"}` |
| `GET /api/projects/<name>/state` | 快照：项目、图幅、测量、各测量有效性 |
| `POST /api/projects/<name>/correct` | JSON 更正：`{"version":0,"sheets":[["C",0,0,57,100]]}` |
| `GET /healthz` | 健康检查 |

上限：每项目最多 500 张图幅、120 条测量（服务层与 SQLite 触发器双重约束）；
一次更正最多 500 张图幅。

## 测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

- `tests/test_service.py`：有效性边界（含/不含上界）、失联、矛盾、证据不足、
  两条路径不共用测量、路径可共用图幅，以及 300 组随机小图与暴力枚举的
  “两条边不相交路径”结果对照。
- `tests/test_db.py`：测量号唯一、120 条上限（含触发器）、同片测量被拒、
  更正保留原坐标并 bump 版本、过期版本不写、级联删除、持有快照期间更正不影响本次核查。
- `tests/test_web.py`：三个示例页面的结论渲染、API 核查/录入/更正、陈旧版本 409、
  表单回退、重启持久化，以及“计算期间更正图幅”不影响本次核查、下一次核查重新判定。
- `tests/test_geometry.py`：半开区间与整数校验。

## 目录

```
adjcheck/geometry.py   Rect/Point，半开区间判定
adjcheck/models.py     Project/Sheet/Measurement/Snapshot/Verification
adjcheck/service.py    解析、有效性、传播与矛盾、两条不共用测量路径、更正编排、示例
adjcheck/db.py         SQLite schema、快照读取、更正事务与版本校验
adjcheck/app.py        Flask 路由（页面 + JSON API）
adjcheck/templates/    页面
adjcheck/static/       样式
scripts/demo.sh        对运行中的服务演示“更正前 → 更正图幅 → 更正后”
Dockerfile, docker-compose.yml, .dockerignore
tests/
```

## 已知边界

- 只支持整数米坐标与平移量；小数或其它单位需先换算。
- 无登录/权限，适合内网或单机演示；`ADJCHECK_SECRET` 请自行替换。
- 测量号唯一但测量不可编辑/删除，只能通过重建项目更正错录；页面未做分页。
- 按需求只核对“基准片所在组”的相容性；与基准片失联的其它组的矛盾不会报告。
