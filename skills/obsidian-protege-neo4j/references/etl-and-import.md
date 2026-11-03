# ETL 与 Neo4j 离线导入

## 数据流

```
Obsidian Vault (只读)
   │  kb_etl.py
   ▼
nodes.csv + relationships.csv        ← 原始 ETL 产物
   │  prepare_import.py
   ▼
nodes.csv + relationships.csv        ← 导入就绪（+id 属性、+KBNode 标签）
   │  neo4j-admin database import full
   ▼
Neo4j (新建空库)
```

## ETLng

### 节点 ID 规则

| 节点 | ID | 示例 |
|---|---|---|
| 文件 | vault 相对路径 | `经营笔记/杯子保养FAQ.md` |
| 目录 | 相对路径 + `/` | `经营笔记/` |
| 标签 | `tag:` + 标签名 | `tag:客服` |
| 源系统 | `source:` + 名称 | `source:教学样本` |

**为什么用相对路径不用标题**：每个目录都有 `00_索引.md`，标题必然撞。路径天然唯一。

**路径的代价**：改名/移动会改变 ID。所以同步要按"旧 ID 删除 + 新 ID 建立"处理，而不是"改名"。改名频繁的项目，可以额外维护一张 `id_alias` 映射表，让历史边仍能解析。

### 关系类型

```
Directory  -CONTAINS->   Document / IndexCard
Directory  -HAS_INDEX->  IndexCard
IndexCard  -LINKS_TO->   Document
Document   -TAGGED_WITH-> Tag
Directory  -SOURCED_FROM-> SourceSystem
```

### 两遍扫描

第一遍收集文件、目录、标签；第二遍解析 wikilink。因为链接目标可能在后面才出现。

**歧义链接必须报错，不许猜**：

```
WARN ambiguous wikilinks: 1 (first: {'from': 'a.md', 'target': '索引', 'candidates': ['x/索引.md', 'y/索引.md']})
```

同名文件有多个候选时，正确做法是**在源笔记里改用路径式链接** `[[x/索引]]`，而不是让脚本随机选一个。

### 零候选 = 断链，忽略

Obsidian 允许指向不存在的笔记。ETL 不应该因此失败。断链是数据问题，由笔记维护者修。

### 输出目录必须在 vault 外

```python
if out == vault or vault in out.parents:
    raise ValueError('output directory must be OUTSIDE the vault')
```

否则下次扫描把上次的 `nodes.csv` 当笔记读进去，图谱开始自我吞噬。这个检查必须硬编码，不能靠自觉。

### 增量基线

`--save-baseline state.json` 记录每个文件的 SHA-256。下次运行：

```
delta added=3 changed=1 removed=0 safety=ok
```

用**内容哈希**而不是 mtime 或文件数——mtime 会被 `git checkout`、解压缩、同步工具改掉。

### 安全停手线

```
--max-delete-ratio 0.5
```

扫描量骤降超过 50% 或预计删除超过 50% → 退出码 1，不执行删除。

典型触发场景：vault 路径写错、磁盘没挂载、`.gitignore` 规则把内容全排除了、过滤规则写太宽。

## Neo4j 离线导入

### 为什么用离线全量导入

在线逐条写入 4 万节点 + 8 万关系要几十分钟。`neo4j-admin database import full` 是分钟级。

### 前提条件

1. **目标数据库必须不存在或是空的**——绝不能指向有数据的库
2. **实例必须处于停止状态**（导入前停服务，导入后再启动）
3. **参数随版本变化**，先跑 `neo4j-admin database import full --help`

### 三步准备

```bash
python3 scripts/kb_etl.py --vault ./vault --out ./out
python3 scripts/prepare_import.py ./out ./out_ready
# PASS nodes=8 relationships=11; id property and KBNode label present
```

### `prepare_import.py` 到底改了什么

| 改动 | 原因 |
|---|---|
| 保留 `:ID` 列 | 离线导入靠它匹配关系两端 |
| 新增 `id` 属性 | `:ID` 是导入期产物，不会存成属性；日更 `MERGE` 需要它 |
| 加 `KBNode` 标签 | 让 `MATCH (n:KBNode)` 能覆盖所有节点 |
| 校验 ID 非空唯一 | 重复 ID 会导致节点被覆盖 |
| 校验关系端点存在 | 悬空边会在导入时报错，且难定位 |

**这三点缺一个，增量同步都会出问题。** 这是整个链路里最常被跳过、后果最严重的一步。

### 导入后的第一轮核验

```cypher
MATCH (n:KBNode) RETURN count(n) AS nodes;
MATCH ()-[r]->() RETURN count(r) AS relationships;
MATCH (a:IndexCard)-[:LINKS_TO]->(b) RETURN a.title, b.title LIMIT 10;
```

三条都要跑。第三条是"是否真能按关系查"的验证——只跑前两条，图里全是孤岛也算"成功"。

**再跑一次完整 ETL + 导入验证幂等**：同一输入跑两次，节点数不应增加。

## Neo4j 约束与索引基线

```cypher
CREATE CONSTRAINT product_business_id IF NOT EXISTS
  FOR (n:Product) REQUIRE n.businessId IS UNIQUE;

CREATE RANGE INDEX source_record IF NOT EXISTS
  FOR (n:BusinessEntity) ON (n.sourceSystem, n.sourceRecordId);

CREATE FULLTEXT INDEX entity_name_fulltext IF NOT EXISTS
FOR (n:Product|ProductModel|Material|Customer|Supplier|ServiceProvider|Document)
ON EACH [n.name, n.title, n.code, n.aliases];
```

要点：
- 全部 `IF NOT EXISTS`，支持重复部署
- 唯一约束加在 `businessId` 这类业务键上，不是内部 id
- 全文索引的标签列表要显式列全，别指望全库
- 向量索引的维度必须和嵌入模型固定维度一致，**先定模型再写索引定义**
- 权限类索引用 `(securityLevel, tenantId, orgUnitId)` 组合，支撑权限过滤下推

## 常见报错对照

| 报错/现象 | 根因 | 处置 |
|---|---|---|
| 导入报 ID 不存在 | 关系端点缺节点 | `prepare_import.py` 会提前拦截 |
| 增量后节点暴涨 | 缺 `id` 属性 | 重跑 prepare_import，补 `id` + `KBNode` |
| 关系方向全反 | 本体 domain/range 写反 | 改本体后重灌 |
| 同名文件合并成一个 | 用标题做 ID | 改用相对路径 |
| 图里出现 `nodes.csv` 节点 | 输出目录在 vault 内 | 移到 vault 外并清理 |
| 导入很慢 | 目标库非空 / 实例未停 | 停服务、空库 |
| 关系类型大小写混乱 | OWL 与 Cypher 未做映射 | 建显式映射表 |
