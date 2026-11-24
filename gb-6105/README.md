# 榆梯检点·证书哈希目录

复核工位用证书编号检出归档记录，追查桶分裂及缺失、到期、撤销的证书。

## 业务流程

1. 检证接班
2. 资质卷宗
3. 目录重放
4. 检证与桶证据

Python 依据哈希低位寻址，按局部深度拆桶，必要时倍增目录。

榆梯维保复核组收维保档案员交来的证书卷宗，按点检计划复核编号、失效及撤销状态，再交给维保调度。旧表反复全表找号，导入量上升时桶深不可见，退件与过期无法在同一交接表解释。

08:30 收 YT-1124-东梯检的 9 条证书，08:35 核查编号，08:40 保存 6 条点查项，08:45 设单桶 2 件，08:46 后台插入并重放目录，08:50 核对缺失 1、到期 2、撤销 1，08:55 导出回执和桶证据。

以 SHA-256 前 64 位的低位索引目录，桶局部深度等于全局深度时先扩容目录，再按新增区分位迁移槽位和记录。深度上限为 12，封顶桶允许溢出且显式报告。点查显式返回有效、到期、撤销与未登记，失效日零点起即过期。索引为可重复的 Python 存储实验，PostgreSQL 保存输入、任务及结果。本实验未模拟磁盘页、并发写入与删除后缩容。

## 在线版本化目录与缩容重建

「在线检证」模块把内存哈希目录提升为带版本的在线目录，覆盖巡检中途调小桶容量的完整闭环：

- **变更先行**：补录、更正、撤销先写权威证书登记与有序变更流（自增 `seq`），再同步当前在用（serving）目录。切换前点查走旧目录，也能查到已入库的更正。
- **影子重建**：申请调小容量后新建一版 `rebuilding` 目录，从空目录按变更流序号重放到新容量；旧版始终 `serving`，重建期间补录、撤销、点查照常进行。
- **收齐才切换**：worker 每轮追平新增变更并落盘 `applied_seq` 与快照；只有新目录收齐全部变更、且与权威登记逐证核对（不多证、不漏证、撤销不复活、内容一致）通过，才在同一事务内把新版置为 `serving`、旧版置为 `archived` 只读。核对不过则置 `failed`，绝不发布。
- **中断可恢复**：重建进度与快照持久在库，worker 重启后从 `applied_seq` 续建；发布用状态条件更新保证幂等，重试不会重复发布目录。
- **同批同版**：点查批次创建时固定 `directory_version` 与目录摘要，整批复用同一版目录，中途切换不影响本批；批次结果与 SHA-256 摘要固化，旧批次下载的桶证据和结论保持原样，检证页面明确标出所用目录版本。
- **显式溢出**：新容量下若触及深度上限，溢出桶在切换说明与点查桶证据中显式报告。

## 启动

```sh
cp -n .env.example .env
docker compose config --quiet
docker compose up -d --build --wait
```

打开 <http://localhost:18254>。OpenAPI 在 <http://localhost:19254/docs>，数据库映射到本机 17754。所有暴露端口只绑定回环地址。Compose 启动 frontend、backend、worker、db、redis，各自通过健康探针检查。数据库、Redis 和共享产物目录各有命名卷；任务状态及导出内容以数据库中的证据为准。

首次启动时幂等载入演示数据。手动载入可运行 `docker compose exec -T backend python -m scripts.seed_demo`。常规停止可用 `docker compose down`。验收环境清理才加 `-v`，它会删除本项目的数据卷。

## 角色与交接

演示账号共用口令 `Demo-DbWorkbench-2026`。

| 登录名 | 角色 | 可执行操作 |
|---|---|---|
| operator | 资料操作员 | 登记业务资料、设置「目标单桶容量」、提交、查看和下载结果；在线目录补录/更正/撤销、申请缩容重建、提交点查 |
| reviewer | 业务复核员 | 查看执行证据、签署单次复核、下载交接单；在线目录只读点查与证据下载 |
| viewer | 只读观察员 | 查看资料、执行结果及审计，下载证据；在线目录只读点查与证据下载 |

选用「YT-1124-东梯检」对应的演示资料，保存一份复演资料，再以默认参数 2 提交。worker 保存结果摘要、业务明细和 SHA-256。复核员登录后在「检证与桶证据」签署。

## 开发说明

| 部分 | 实现 |
|---|---|
| 前端 | 原生 HTML、CSS、JavaScript ES Modules、Nginx |
| 后端 | Python 3.11、FastAPI、SQLAlchemy、JWT |
| 数据库 | PostgreSQL 16 |
| 后台执行 | Redis 通知、数据库持久任务队列和行锁、Python worker |

从 `backend/src/domain/scenario.py` 修改业务入口和演示口径，运行 `docker compose up -d --build --wait` 查看变更。数据库表结构在 `backend/src/models` 和 `backend/migrations`，输入模型在 `backend/src/schemas`。业务页和请求接口在 `frontend/src/pages`、`frontend/src/api`。资料、执行、复核三个工作流实体各有独立表和 API；场景输入及结果保留在所属资料和批次中。

角色枚举在 `backend/src/constants/roles.py`、`frontend/src/constants/roles.js`，用于服务端访问控制、账号初始化、前端导航与签署。批次状态在 `backend/src/constants/job_state.py`、`frontend/src/constants/states.js`，用于队列、状态 API、状态标记与轮询。登记、提交、完成和签署事件写入审计表。

`COMPOSE_PROJECT_NAME` 设置栈名，`FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT` 设置端口。通过 `DB_PASSWORD`、`JWT_SECRET`、`DEMO_PASSWORD` 配置本地凭据。设置 `SEED_DEMO=false` 可关闭演示数据载入，此时数据库初始没有演示账号。数据库密码调整后，已有数据卷需在数据库内同步修改用户密码。

## 验收范围

在内存计算目录，容量与最大深度可复核。卷宗和证据由 PostgreSQL 持久化。 当前界面用 JSON 编辑业务资料，以 JSON 下载证据。过程和截图保存在批次的 [验收记录](../../test-artifacts/gb-6105/TESTING.md)。

[MIT License](LICENSE)
