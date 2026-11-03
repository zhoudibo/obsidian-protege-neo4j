---
name: obsidian-protege-neo4j
description: Build and operate a three-layer local knowledge graph from an Obsidian vault — Obsidian as the source of truth, Protege/OWL for the ontology, Neo4j for the queryable graph. Use when turning a vault into a graph, designing an OWL ontology, generating Neo4j import CSVs, planning incremental syncs without accidental deletions, or verifying graph backups. Triggers on "Obsidian 知识图谱", "Protege", "OWL 本体", "Neo4j 灌库", "知识图谱同步", "vault to graph", "ontology to Neo4j".
---

# Obsidian → Protégé → Neo4j 三层知识库架构

把 Obsidian 里的 Markdown 笔记变成**可查询、可增量更新、可恢复**的知识图谱。

## 三层职责（不可混淆）

| 层 | 组件 | 存什么 | 不该做什么 |
|---|---|---|---|
| L1 事实源 | Obsidian Vault | 原始 Markdown、frontmatter、`[[wikilink]]` | 不被任何脚本改写 |
| L2 语义层 | Protégé / OWL | 类、对象属性、domain/range、SHACL 约束 | 不存实例数据；不手工拖入几万个文件 |
| L3 查询层 | Neo4j | 节点、边、索引、约束 | 不是事实源；丢了可以从 L1 重建 |

**关键边界**：OWL 本体是业务语义标准，Neo4j 是运行查询投影。两者通过稳定的类/属性/关系标识映射，**不要求把全部 OWL 公理搬进 Neo4j**。

## 施工顺序（照做，不要跳步）

### 第 0 步：先跑通三张假笔记

绝不用整库冒险。先在 Obsidian 建三张虚构笔记（一个索引卡 + 两张普通笔记），互相 `[[链接]]` 一个，跑完整条链路，确认数字对得上，再上真库。

- 唯一 ID 用 **vault 内相对路径**，不用标题。标题会撞（每个目录都有 `00_索引.md`），路径不会。
- 改名或移动文件 = 旧 ID 删除 + 新 ID 建立。同步时必须按这个处理，不能假装没影响。

### 第 1 步：Protégé 定义"名词和关系"

新建 OWL ontology → Classes 页建类树 → Object properties 页建关系并写 domain/range → 保存 `.owl`/`.ttl`。

起步最小集（够用且好验证）：

```
类    : Document, IndexCard, Directory, Tag
关系  : CONTAINS      (Directory → Document)
        LINKS_TO      (IndexCard → Document)
        TAGGED_WITH   (Document → Tag)
```

**方向必须写清楚**。`CONTAINS` 是目录指向文档，反了查询就全反。

验收不能只看"文件能打开"。至少抽一张索引卡、一张普通笔记、一张附件，问三个问题：
1. 类别对吗？ 2. 关系方向对吗？ 3. 同名文件 ID 还唯一吗？
不对就先改模型再灌库——否则错误会被大规模复制。

模型定稿后，用脚本核验而不是靠 GUI 截图：

```bash
python3 scripts/owl_check.py ./ontology.ttl
python3 scripts/owl_check.py ./ontology.ttl --json
```

### 第 2 步：ETL 生成 CSV

```bash
python3 scripts/kb_etl.py --vault ./vault --out ./out --dry-run
python3 scripts/kb_etl.py --vault ./vault --out ./out --save-baseline ./state.json
```

输出目录**必须在 vault 外面**，否则下次扫描会读到自己的产物，图谱开始自我吞噬。

节点数 ≠ 笔记篇数：目录、标签、源系统都会成为节点。

### 第 3 步：准备 Neo4j 离线导入（最容易踩的坑）

```bash
python3 scripts/prepare_import.py ./out ./out_ready
```

这一步做三件不显眼但决定成败的事：

1. 保留 `:ID` 列（离线导入靠它匹配关系端点）
2. **额外写入普通 `id` 属性**——`:ID` 只是导入期产物，不会存成属性
3. 所有节点加 `KBNode` 标签

> **为什么关键**：日更脚本用 `MERGE (n:KBNode {id: $id})` 找节点。如果只有 `:ID` 没有 `id` 属性，MERGE 永远匹配不到，每次同步都会新建节点，图谱越滚越大；或者错误回收旧节点。
> 首灌漏了这两项，增量必炸。修好后必须测：**同一输入跑两次，节点数不增加**。

然后只在**新建、空的、可丢弃**的 Neo4j 库上执行：

```bash
neo4j-admin database import full \
  --nodes-dir=out_ready --relationships-dir=out_ready \
  --nodes=nodes.csv --relationships=relationships.csv \
  --overwrite-destination=true
```

命令参数随版本变化，**以本机 `neo4j-admin database import full --help` 为准**。

启动后在 Browser 立刻核两句：

```cypher
MATCH (n:KBNode) RETURN count(n) AS nodes;
MATCH ()-[r]->() RETURN count(r) AS relationships;
MATCH (a:IndexCard)-[:LINKS_TO]->(b) RETURN a.title, b.title LIMIT 10;
```

第一句核规模，第二句核"是否真能按关系查"。只会返回一个漂亮节点云，还谈不上可用。

### 第 4 步：增量同步——守住"不能误删"

```bash
python3 scripts/kb_sync_plan.py --vault ./vault --graph-state ./graph_nodes.json
```

**铁律：不能用"这次没扫描到"判断删除。** 早期做法差点把未变更但仍被引用的标签一起清掉。

正确的归属模型：

| 节点类型 | 归属 | 删除策略 |
|---|---|---|
| 文件 / 目录 | vault（内容哈希跟踪） | vault 中消失才可删 |
| 标签 `tag:*` | **不属于任何文件** | 仅回收无入边的孤儿标签 |
| 源系统 `source:*` | 流水线 | 永不自动删 |

停手线：扫描量骤降或预计删除数超阈值 → **停止删除并报警**。

四步验收（必做，记前后计数）：
1. `--dry-run` 看预计写入/删除，删除数异常就停
2. 新增一张临时卡（内含 `[[已有卡]]`），同步后查临时节点和 LINKS_TO
3. 移走临时卡再同步，确认临时节点被清理、**已有卡仍在**
4. 同一状态再跑一次，总数不应继续涨，重复边不应出现

### 第 5 步：备份分两份，但别把"存在"当"能恢复"

- **Git bundle** → 留住已被 Git 跟踪的文本与提交历史。**不含未跟踪附件**。
- **Neo4j JSON 逻辑导出** → 节点和关系。Community 版 `neo4j-admin database dump` 要求数据库离线，不能拿在线 dump 当已跑通。

```bash
python3 scripts/graph_verify.py --export ./neo4j.export.json --bundle ./vault.git.bundle
```

**两级验证必须分开写结论**：

| 层级 | 动作 | 能得出什么 |
|---|---|---|
| A 级：导出完整性 | 读 JSON 核节点 ID/关系端点/计数；核 bundle 可打开 | 备份文件结构可信 |
| B 级：隔离恢复 | 空库或另一台机器导入备份，跑固定查询 + 抽查原卡 | 灾难恢复成功 |

只做了 A 级，报告里**不能写"恢复成功"**。

### 第 6 步：月度安全体检

检查项：Git 工作区、凭据权限、私密内容跟踪状态、备份新鲜度、Neo4j 连通性、待审队列、逻辑导出结构。

⚠️ **体检脚本本身会腐烂**：某个旧版月度脚本仍在读已迁走的 `password` 配置项，当前配置已改为 `credentials_file`，实际执行直接报错。安全治理也要检查脚本是否随凭据迁移更新。

审计报告**不应把命中的密码原文写进报告或终端**，只报告"命中位置与条数"，秘密留在受保护凭据文件。

## 安全边界（不可协商）

- **Obsidian 原文件是事实源**，任何导入脚本不得顺手改写源文件
- 公开/共享图谱：先用副本试跑，列出私密、客户、家庭信息排除规则
- 不把身份证、银行卡、完整联系方式、生产密钥、未脱敏原文写入图数据库
- 原始合同、图纸、照片留在对象存储，图库只存关系、必要属性和**来源指针**
- 权限过滤必须**先于**召回和生成；只有已发布、当前有效、用户有权访问的内容进入默认检索
- Neo4j 是派生副本——丢了能从 Obsidian 重建，这是敢做图谱的前提

## 文件

```
scripts/
  kb_etl.py           Obsidian → nodes.csv / relationships.csv
  prepare_import.py   CSV → Neo4j 离线导入就绪（加 id 属性 + KBNode 标签）
  owl_check.py        OWL/Turtle 本体检索（类、对象属性、domain/range、告警）
  kb_sync_plan.py     增量同步规划（ADD/UPDATE/DELETE + 误删停手线）
  graph_verify.py     备份只读核验（A 级完整性；B 级需真实库）
references/
  ontology-design.md     本体设计：类树、关系方向、SHACL 约束
  etl-and-import.md      ETL 与离线导入细节、常见报错
  sync-and-backup.md     增量同步、备份、恢复演练
  security-boundaries.md  脱敏、权限、审计红线
templates/
  ontology-minimal.ttl   可直接在 Protégé 打开的最小本体
  node-labeling.md       节点 ID 与标签命名规范
```

## 全流程自检

```bash
python3 scripts/kb_etl.py --vault ./vault --out ./out --dry-run
python3 scripts/prepare_import.py ./out ./out_ready
python3 scripts/owl_check.py ./ontology.ttl
python3 scripts/kb_sync_plan.py --vault ./vault --graph-state ./state.json
python3 scripts/graph_verify.py --export ./export.json
```

全部零第三方依赖，只用 Python 标准库。所有脚本对 vault **只读**。

## 常见失败模式

| 症状 | 根因 | 对策 |
|---|---|---|
| 增量同步后节点暴涨 | 只有 `:ID` 无 `id` 属性，MERGE 匹配不到 | `prepare_import.py` 补 `id` + `KBNode` |
| 标签被误删 | 用"没扫描到"当删除依据 | 只回收无入边孤儿标签 |
| 同名文件互相覆盖 | 用标题做 ID | 用 vault 相对路径 |
| 下次扫描吃到自己 | 输出目录在 vault 内 | 输出必须在 vault 外 |
| 关系方向全反 | 本体没写 domain/range | Protégé 里补齐并核验方向 |
| 恢复演练做不出来 | 只做了 A 级完整性 | 隔离库导入 + 固定查询才算 B 级 |
| 体检脚本报 `'password'` | 脚本未跟随凭据迁移 | 同步改脚本配置读取方式 |
