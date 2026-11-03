# 本体设计：Protégé / OWL 层经验

> 这一层回答三个问题：**这个库里有哪几种"东西"？它们之间有什么关系？哪些数据算合法？**

## 为什么要有这一层

没有本体时，每个 ETL 脚本各写各的字段名，图里出现 `contains` / `CONTAINS` / `HasChild` 三种写法，半年后没人知道哪个对。

本体把"业务语言"固定下来：类名、关系名、方向、约束。它是**词表与约束**，不是数据本身——不要手工把几万个文件拖进 Protégé。

## 建模顺序

```
1. 新建 ontology，设本体 IRI + 版本 IRI
2. Classes 页：先建顶层类，再挂子类
3. Object properties 页：建关系，逐个写 domain / range
4. 需要时补 Datatype properties
5. 反向关系写 owl:inverseOf
6. 保存 .owl 或 .ttl，提交版本控制
7. 用 owl_check.py 核验，再考虑一致性检查（HermiT / ELK）
```

## 类层级设计

从真实工程里抽象出的稳定骨架：

```
BusinessEntity
├── OrganizationEntity
│   ├── Group
│   ├── LegalEntity
│   ├── BusinessUnit
│   ├── Department
│   ├── Position
│   └── Warehouse
├── Party
│   ├── Employee
│   ├── Customer
│   ├── Supplier
│   └── ServiceProvider
├── ProductEntity
│   ├── Product
│   ├── ProductModel
│   ├── Material
│   ├── BOM
│   ├── BOMItem
│   └── EquipmentUnit
├── TransactionEntity
│   ├── Inquiry / Quotation / Contract / SalesOrder
│   ├── Shipment / PurchaseOrder / ProductionOrder / StockLot
│   ├── ServiceWorkOrder / Inspection / Settlement
├── KnowledgeAsset
│   ├── Evidence
│   │   ├── TestReport
│   │   └── ServiceEvidence
│   ├── ControlledVocabulary
│   │   ├── FaultPhenomenon
│   │   └── FaultCause
│   ├── Resolution
│   ├── Drawing
│   ├── QualityStandard
│   ├── KnowledgeSpace
│   ├── Document
│   ├── DocumentVersion
│   ├── KnowledgeChunk
│   └── Rule
└── SourceSystem
```

**Obsidian 场景的最小子集**（先够用，再扩）：

```
Document      普通笔记
IndexCard     目录入口页（00_索引.md）
Directory     目录
Tag           标签
Domain        主题域
KnowledgeMap  知识地图
SubDomain     子域
Project / Person / Resource
```

## 关系设计：方向必须显式

| 关系 | domain | range | 说明 |
|---|---|---|---|
| `contains` | Directory | Document | 目录包含文档 |
| `linksTo` | IndexCard | Document | 索引卡指向笔记 |
| `taggedWith` | Document | Tag | 文档带标签 |
| `hasIndex` | Directory | IndexCard | 目录有入口索引卡 |
| `sourcedFrom` | Directory | SourceSystem | 目录来自哪个源系统 |
| `hasSubDomain` | Domain | SubDomain | 域含子域 |
| `relatedTo` | KnowledgeArtifact | KnowledgeArtifact | 知识产物互相关联 |
| `authoredBy` | KnowledgeArtifact | Person | 作者 |

⚠️ **方向搞反的代价**：整库灌完才发现，所有查询都要反过来写，返工成本是重跑 ETL + 重灌 Neo4j。

## 命名规范

- 类名：大驼峰（`ServiceWorkOrder`）
- 关系名：小驼峰或全大写，**在一个库内保持一致**
- 图数据库里关系类型全大写 `CONTAINS`，OWL 里属性名小驼峰 `contains` —— 两边要有**显式映射表**，不要靠猜

映射表建议长这样（CSV，一行一个字段）：

```
source_system,source_object,source_key,graph_label,graph_key,properties,relationships,load_mode,data_owner,notes
ERP,rd_product,id,Product,businessId,"code|name|status","HAS_MODEL->ProductModel",incremental,研发,"以ERP产品主数据为权威源"
```

列的含义：
- `source_system` / `source_object`：来源系统与表
- `source_key` / `graph_key`：两边的唯一键
- `graph_label` / `relationships`：目标标签与关系
- `load_mode`：`incremental`（增量）/ `event`（事件驱动）/ `full`
- `data_owner`：谁是数据责任人
- `notes`：排除规则（比如"银行账号不进入图数据库"）

## SHACL：给实例数据加质量约束

OWL 管本体，SHACL 管数据。两者用同一套命名空间。

```turtle
biz:CommonBusinessEntityShape
    a sh:NodeShape ;
    sh:targetClass biz:BusinessEntity ;
    sh:property [
        sh:path biz:businessId ;
        sh:minCount 1 ; sh:maxCount 1 ;
        sh:datatype xsd:string ; sh:minLength 1 ;
        sh:message "业务实体必须有且仅有一个非空businessId。"@zh-CN
    ] ;
    sh:property [
        sh:path biz:sourceSystem ;
        sh:minCount 1 ; sh:maxCount 1 ;
        sh:datatype xsd:string ;
        sh:message "业务实体必须记录来源系统。"@zh-CN
    ] .
```

常用约束组合：

| 约束 | 作用 | 典型场景 |
|---|---|---|
| `sh:minCount 1` | 必填 | businessId、sourceSystem |
| `sh:maxCount 1` | 唯一单值 | businessId |
| `sh:class` | 目标类型约束 | BOM明细必须指向 Material |
| `sh:minExclusive 0` | 数值下界 | 用量必须 > 0 |
| `sh:minInclusive / maxInclusive` | 闭区间 | 损耗系数 0–1 |
| `sh:in (...)` | 枚举 | securityLevel ∈ {public, internal, confidential, restricted} |
| `sh:minLength 32` | 格式长度 | contentHash 便于去重追溯 |

**每个约束都写 `sh:message`**，用业务语言写。人看不懂的校验规则等于没有。

## 验收：三个问题

模型定稿、灌库之前，抽三类样本问：

1. **类别对吗？** —— 这张卡该是 `IndexCard` 还是 `Document`？
2. **关系方向对吗？** —— `CONTAINS` 是目录→文档，还是文档→目录？
3. **同名文件 ID 还唯一吗？** —— 两个目录都有 `00_索引.md` 时，ID 是否区分开了？

三条都过才灌库。**不对就先改模型**——本体改一次是一小时，灌完再改是重来。

## 脚本核验

```bash
python3 scripts/owl_check.py ./ontology.ttl
```

输出：类数、类树、对象属性数、每个属性的 domain → range、inverseOf、以及告警（缺 domain/range、subClassOf 指向未知类）。

为什么需要脚本：Protégé 是 GUI，**没法把它的窗口截图当证据**，更没法从截图断言"有 13 个类"。模型在 GUI 里做，验证在文件上做。

## 版本治理

- 本体文件进 Git，每次语义变更有独立 commit
- 版本 IRI 跟着版本走：`.../ontology/1.0.0`
- OWL 本体与属性图映射**解耦并各自受版本控制**
- 破坏性变更（删类、改关系方向）走新版本 IRI，不原地改
