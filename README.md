# 许可缩减合规分析系统（License-Retraction Compliance Analytics）

当数据来源的许可范围缩减后，系统基于**实际贡献度**重新判断受影响的样本、
缓存和导出，并在「从安全子集重建」与「阻断整份报告」之间作出可解释的裁决。
三个服务独立运行、各自持有数据库；控制措施位于服务端而非网页按钮。

零第三方依赖，仅使用 Python 3.11 标准库（`http.server` / `sqlite3` / `urllib`）。

## 架构

| 服务 | 端口 | 独立存储 | 职责 |
|---|---|---|---|
| 授权快照服务 `snapshot_service` | 8071 | `snapshot.db` | 数据源目录、授权、**不可变快照**、撤回事件出站箱、丢失模拟、拉取对账 |
| 样本构建器 `builder_service` | 8072 | `builder.db` | 服务端授权闸门、逐行血缘、可中断/可恢复构建、用途变更、样本隔离 |
| 报告服务 `report_service` | 8073 | `report.db` | 真实指标、贡献度分析、重建/阻断裁决、缓存隔离、导出吊销、封存审计、脱敏解释包 |
| Web 网关 + 控制台 `web` | 8080 | 静态文件 | 反向代理与操作界面；`/internal/*` 原始值端点在网关层被 403 拦截 |

```
浏览器 ──8080── 网关(屏蔽 /internal/*)
                   ├──8071 授权快照（outbox：可丢失 / 可拉取补偿）
                   ├──8072 样本构建（inbox：独立收件箱，可重放）
                   └──8073 报告分析（inbox：独立收件箱，可重放）
```

## 快速开始

```bash
# 1. 生成全部虚构（合成）原始数据 —— 确定性种子，血缘 ID 可复现
python3 -m app.bootstrap.generate_fixtures

# 2. 启动三个独立服务
python3 -m app.run_all &

# 3. 启动 Web 网关与控制台
python3 -m app.web.gateway &

# 4. 注册数据源并生成基线授权快照
python3 -m app.bootstrap.seed

# 5. 一条命令走完整业务流（含两类故障模拟）
python3 -m app.bootstrap.demo
```

打开 http://127.0.0.1:8080 使用控制台。

测试：`python3 -m app.tests.e2e_test`（14 个端到端用例，动态端口，自带临时数据）。

## 核心合规设计

### 1. 不可变授权快照
每次授权变更都生成新版本快照，物化当时全部授权状态并计算指纹。旧快照永不修改，
封存审计按快照时点的授权保留证据；授权校验始终针对明确的快照版本，不信任缓存标志。

### 2. 服务端授权闸门（不是隐藏按钮）
样本构建与用途变更都由构建器调用授权服务的 `/authorization/check` 在服务端裁决。
无授权的构建直接 `403`，与界面是否展示入口无关。

### 3. 逐行血缘，可追到每个输出
样本每物化一行就记录确定性血缘 ID（`LN_<source>_<行号>_<行内容哈希>`）。
报告的每个指标都保存完整 `lineage_ids` 与按数据源的子合计；
`/lineage/trace/{id}` 提供 指标 → 报告 → 样本 → 原始行号 的端到端追踪。
原始敏感值只存在于构建器库内，外部接口永不返回。

### 4. 基于实际贡献度的重建/阻断裁决
撤回后逐指标计算受影响行数与贡献百分比（`common/policy.py`）：

- 贡献度 > 25%，或安全行 < 5 → **阻断（block）**，报告不可重建，
  只能基于新授权样本生成替代报告；
- 否则 → 可从安全子集**重建（rebuild_safe）**，生成新版本报告，旧版本标记 superseded。

裁决依据逐指标写入 `impact_findings`，解释包中可查。

### 5. 样本 / 缓存 / 导出的差异化处理
- 受影响样本版本 → `quarantined`；
- 受影响缓存条目 → `quarantined`；
- 普通下载导出 → `revoked`，下载返回 410；
- **已合法封存的审计** → 工件在磁盘保留、状态 `sealed`、legal hold 锁定（下载返回 423），
  仅作证据不可再用于分析；其普通下载仍被吊销。

### 6. 解释包结构上不含敏感明细
解释包只由聚合指标、子合计、血缘标识和裁决结论组装，根本不加载原始行；
写出前再经 `scrubber.safe_copy` 脱敏 + `assert_no_sensitive_detail` 扫描，
发现任何敏感键即失败关闭（fail-closed）。测试断言原始行键与合成身份值均不出现。

## 故障模拟与对账

- **通知丢失**：`POST /sim/loss` 令授权服务对指定消费者丢弃下一次推送；
  事件保留在出站箱，消费者通过 `/events/pull` 拉取补偿，事件被重新投递到独立收件箱，
  再 `POST /inbox/consume` 重放生效。
- **构建中断**：`POST /sim/fail` 在指定阶段注入失败；物化中断时已处理行作为检查点提交，
  `POST /builds/{id}/run` 从检查点恢复，最终行数与无中断的确定性构建一致。
- 撤回的授权吊销与快照物化包在单个 `BEGIN IMMEDIATE` 事务内，
  杜绝并发撤回导致快照捕获中间状态。

## 目录

```
app/common/          数据库、HTTP 框架/客户端、脱敏器、合规策略
app/snapshot_service 授权快照服务（schema.sql + app.py）
app/builder_service  样本构建器
app/report_service   报告服务（metrics.py 为真实指标计算）
app/web/             网关与静态控制台（深色主题，配色经 dataviz 校验）
app/bootstrap/       虚构数据生成、seed、演示编排
app/tests/e2e_test.py 端到端测试
data/raw/            合成原始数据（.gitignore 忽略 db/运行时）
```
