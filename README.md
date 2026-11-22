# 湿地轨迹分段交接 API

把一条已采样的湿地巡护轨迹按时间顺序切成若干段，交给 GIS。已签发的段一经落库即不可变（原样保留），后续申请只能从最新进度继续。

## 运行

```bash
docker compose up --build
```

- API / Swagger UI: <http://localhost:8000/docs>
- OpenAPI JSON: <http://localhost:8000/openapi.json>
- SQLite 数据文件持久化在 compose 卷 `track_data` 的 `/data/handover.db`。

本机没有 Docker 时可直接跑（需先安装 `requirements.txt`）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/trails` | 登记轨迹：2..200 个采样点（严格递增整数秒 + 平面整米坐标）、0..200 个轴对齐矩形管护区、位置误差限值、全程点数上限 |
| `GET` | `/trails` | 列出已登记轨迹及签发摘要 |
| `GET` | `/trails/{trail_id}` | 轨迹详情（含原始点、各区、约束、已签发段） |
| `POST` | `/trails/{trail_id}/segments` | 申请下一段：填写所见进度 `observed_progress`、终点 `end_index`、必留复核点 `required_pins` |
| `GET` | `/trails/{trail_id}/segments` | 查已签发段（重启后续交用） |
| `GET` | `/trails/{trail_id}/segments/{seq}` | 查单段 |
| `GET` | `/healthz` | 健康检查 |

## 在 Swagger 里试跑

1. 打开 <http://localhost:8000/docs>。
2. `POST /trails` → **Try it out**，粘贴一条轨迹（`samples` 为 `{"t","x","y"}` 列表，`error_limit` 用字符串或数字，`point_limit` 为整数）。
3. 记下返回的 `id`。`POST /trails/{trail_id}/segments` 填写
   `{"observed_progress": 0, "end_index": <终点下标>, "required_pins": [<必留点>]}`。
4. 返回的 `points` 即为本段要交给 GIS 的原采样点；`total_points_used` 为含接缝的累计点数。
5. 用上一段的 `end_index` 作为下一次的 `observed_progress` 继续申请；`GET .../segments` 可随时核对已签发段。

### 核心语义

- **只选原采样点**：每段的起点/终点及段内保留点均为原轨迹采样点下标，坐标与时刻原样照抄。
- **段首尾必留**：每段的首、尾采样点必然入选。
- **接缝只计一次**：上一段的终点即本段的起点；`total_points_used` 中接缝点只算一个。
- **必留复核点**：`required_pins` 给出的采样点下标必须落在 `[observed_progress, end_index]` 内，否则 422。
- **简化误差**：原始轨迹与简化轨迹都按时间线性插值；在任一原始采样时刻，两者位置偏差（平面欧氏距离，米）不得超过登记的位置误差限值。全程使用 `Fraction` 精确有理数比较，临界值不因浮点舍入而翻转。
- **驻留时段一致**：每个管护区按开矩形处理（严格在内部才算区内，**边界不算区内**）；简化前后落在每个区内的驻留时段集合必须完全相同。判定同样是精确有理数运算。
- **点数最少**：在满足上述约束（且余段仍能装进剩余点数）的前提下，签发保留点数最少的本段；并列最优时取下标序列字典序最小者，签发确定可复现。
- **超额**：若按本次选择无法在剩余点数内完成整条轨迹，返回 409 `point_limit_exceeded`，并在 `minimum_total_points` 给出按本次选择完成整条轨迹所需的最少总点数（已签发点 + 本段最少点 + 余段最少新增点）；进度保持不变。
- **过期进度**：`observed_progress` 必须等于当前最新签发进度（未签发时为 0），否则 409 `stale_progress` 并回传 `current_progress`，需重新拉取后再提交。
- **已签发段不可变**：API 未暴露任何修改/删除入口（405），数据库层另有 `BEFORE UPDATE` / `BEFORE DELETE` 触发器兜底。
- **并发**：每次段申请在一个 `BEGIN IMMEDIATE` 写事务内完成"读进度 → 校验 → 落库"，重复/并发提交同一进度只会签发一次。
- **重启续交**：轨迹与已签发段都在 SQLite 中，进程重启后可直接查询并从未端继续。

## 测试

```bash
python -m pytest                 # 单元 + API + 性质测试
python scripts/smoke_http.py     # 对运行中的服务做端到端冒烟（含 Swagger 可达性）
```

测试内容：

- `tests/test_geometry.py`：精确误差判定、时间插值、开矩形边界、驻留合并、复核点。
- `tests/test_properties.py`：随机轨迹与独立的暴力枚举结果比对（最少点数）、独立验证器复核误差与驻留时段。
- `tests/test_handover.py`：多段全流程、接缝计一次、复核点、预算守卫、200 点轨迹。
- `tests/test_api.py`：参数校验、不可变性、过期进度、超额回报、并发、重启持久化、OpenAPI。

`Dockerfile`、`docker-compose.yml` 仅用于容器启动；本机缺 Docker 时上述命令可直接在 venv 中执行。
