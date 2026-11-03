# 节点 ID 与标签命名规范

## 节点 ID

ID 一旦定下就是**长期契约**——关系边引用它，增量同步靠它匹配。改 ID 规则 = 重建整图。

| 节点 | ID 格式 | 示例 |
|---|---|---|
| 文件 | vault 相对路径（POSIX） | `经营笔记/杯子保养FAQ.md` |
| 目录 | 相对路径 + 结尾 `/` | `经营笔记/` |
| 标签 | `tag:` + 标签名 | `tag:客服` |
| 源系统 | `source:` + 名称 | `source:教学样本` |

### 硬性规则

1. **不用标题做 ID。** 标题会撞（每个目录都有 `00_索引.md`）。
2. **不用 mtime、文件大小、随机 UUID 做 ID。** 前两者会变，后者无法复现。
3. **ID 一旦发布就不能改。** 需要改映射规则时，加 `id_alias` 表做兼容。
4. **路径分隔符统一用 `/`。** Windows 上产生的 `\` 必须归一化。

### 文件 ID 与图节点 id 的对应

| 层 | 字段 | 关系 |
|---|---|---|
| ETL 节点表 | `:ID` | 导入期匹配关系端点 |
| ETL 节点表 | `id` | **与 `:ID` 同值**，持久化属性 |
| Neo4j 节点 | `n.id` | 增量 `MERGE (n:KBNode {id: $id})` 的匹配键 |

`:ID` 和 `id` 必须同值。不同值会导致增量同步匹配失败。

## 标签命名

Neo4j 标签语法限制：不能以数字开头，不能含空格、连字符以外的标点。

```
目录结构      →  Directory + Domain
入口索引页    →  IndexCard
普通笔记      →  Document
标签          →  Tag
源系统        →  SourceSystem
```

多标签用 `;` 分隔（CSV 友好）：

```
Directory;Domain
Document
```

### 通用标签

所有节点统一带 `KBNode`，这样一条查询覆盖全库：

```cypher
MATCH (n:KBNode) RETURN count(n);
```

同时保留细粒度标签，两者并存：

```cypher
MATCH (c:IndexCard)-[:LINKS_TO]->(d:Document) RETURN c.title, d.title;
```

## 关系类型

Neo4j 关系类型用**全大写**，全库统一：

```
CONTAINS      HAS_INDEX      LINKS_TO
TAGGED_WITH   SOURCED_FROM   RELATED_TO
```

### OWL ↔ Cypher 映射

本体里用小驼峰，数据库里用全大写，**两边要有显式映射表**，不要靠猜：

| OWL 属性 | Cypher 关系 | domain → range |
|---|---|---|
| `kb:contains` | `CONTAINS` | Directory → KnowledgeArtifact |
| `kb:linksTo` | `LINKS_TO` | IndexCard → Document |
| `kb:taggedWith` | `TAGGED_WITH` | Document → Tag |
| `kb:hasIndex` | `HAS_INDEX` | Directory → IndexCard |
| `kb:sourcedFrom` | `SOURCED_FROM` | KnowledgeArtifact → SourceSystem |
| `kb:relatedTo` | `RELATED_TO` | KnowledgeArtifact → KnowledgeArtifact |

映射表本身也进版本控制。

## 属性命名

图数据库里属性用小驼峰，与本体一致：

```cypher
n.businessId
n.contentHash
n.securityLevel
n.sourceSystem
n.sourceRecordId
```

**必须有的属性**：

| 属性 | 谁有 | 用途 |
|---|---|---|
| `id` | 所有节点 | 增量匹配键 |
| `businessId` | 业务实体 | 唯一约束 + 跨系统对齐 |
| `sourceSystem` | 业务实体 | 溯源 |
| `sourceRecordId` | 业务实体 | 溯源 + 复合索引 |
| `contentHash` | 文档/版本 | 去重、增量判断、追溯 |
| `securityLevel` | 文档/切片 | 权限过滤 |

## 命名检查清单

```
□ 文件 ID 是相对路径，不是标题
□ 目录 ID 以 / 结尾
□ 标签前缀 tag:，源系统前缀 source:
□ 所有节点有 KBNode 标签
□ 所有节点有 id 属性，且等于 :ID
□ 关系类型全大写，全库一致
□ OWL↔Cypher 映射表存在且进版本控制
□ 没有硬编码本机绝对路径
□ 没有把真实项目名/客户名写进 ID
```
