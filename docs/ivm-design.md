# 增量物化视图维护：组合式差分规范（纠正版）

本文件替代原“current deployment note”。原文件关于“批末新袋可直接双侧相加”“无需匹配计数索引”“DISTINCT 只看增量符号”“分组可从输出行重建累加器”“重开即保留增量语义、旧节点袋无需回填”的声明均不成立，对应事故见 `REVIEW.md` 的 D1-D8 与证据用例 C1-C5、B3、M1-M11（`review.json`）。本规范覆盖该代数**任意合法计划组合**，不止记录在案的 `samples.PLAN`。

证据用例完整 ID（正文使用短名）：C1-normal-recorded-business、C2-shared-subplan-union、C3-null-join-keys、B3-null-key-zero-sum-group、C5-tombstone-revive-receipt、M1-incident-late-dimension、M2-anti-right-only-change、M3-inner-both-sides-same-batch、M4-distinct-support-count、M5-having-group-state-lost、M6-global-group-vanishes-on-empty、M8-left-pad-not-restored-on-right-delete、M9-left-same-batch-left-change-right-removal、M10-anti-combined-batch、M11-shared-self-join-double-count。

## 1. 基础记号与袋版本

- 袋（bag）：`B: tuple -> int`，键为行元组，值为带符号多重度；可见袋只含严格正重量，零重量条目删除。重量是多重度，**任何环节不得展开成事件序列**。
- 对节点 `n` 维护两个完整袋版本：旧版 `B_n`（本批发布前）与新版 `B'_n = B_n + Δ_n`，其中 `Δ_n` 为带符号净差（可含负数、省略零）。
- 一个源记录身份 `(src,id)` 的修订先在事务内合并：高 rev 整体替换、相等 rev 等内容忽略、不等内容 `REVISION_CONFLICT`、低 rev 忽略、null 为墓碑且可被更高 rev 复活。**修订合并必须在任何关系差分传播前完成**（现 `store.py:94-113` 已正确）。合并后源袋的净差 `Δ_s = B'_s − B_s` 才进入引擎；同批对同一身份的多次物理变更只呈现为一个净差。
- 每个节点在一个批次内只处理一次，按计划拓扑序；节点 `n` 的所有消费者共享同一个 `Δ_n`。这就是共享 DAG 的全部版本协议——不需要每边单独版本化（C2、M11 验证：扇出本身正确，错误只来自连接内部的版本选择）。

## 2. 节点不变量与持久化状态

每个节点持久化两类对象：可见输出袋 `B_n`，以及下列**独立于输出袋**的维护状态。把维护状态等价于输出袋是 D5-D7 的共同根因。

| 算子 | 持久化维护状态（除输出袋外） |
|---|---|
| source | 源记录修订表 `(src,rid -> rev,body)`，即事实来源 |
| project/filter/union | 无额外状态（无状态投影） |
| inner/left/anti | 连接键哈希索引：左桶 `L[k]`、右桶 `R[k]`，桶内保存 `(行, 重量)`；右支持计数 `sup_R(k)=Σ_{y∈R[k]} weight(y)`；左外另需左支持 `sup_L(k)`；桶索引随双侧增量算术更新 |
| distinct | 每取值支持计数 `sup_d(row)`（带符号累计，恒 ≥0） |
| group | 每组（含 HAVING 抑制组、含 `keys=[]` 全局组）一条累加器 `acc[key]=(SUM, COUNT(*), COUNT(value))`，与该行当前是否在输出袋无关 |

不变量：

1. 任意时刻 `B_n` 恰为维护状态按本规范导出的可见行（正重量）；不存在“输出袋有、状态推导不出”的行。
2. 所有计数为整数算术：`SUM` 忽略 NULL 值，`COUNT(*)` 计全部出现，`COUNT(value)` 计非 NULL 值出现；支持计数与累加器随带符号增量做加减，永不重扫成员。
3. NULL 键按 SQL 语义在分组中相等（可直接作为字典键）；在连接相等性中 NULL ≠ NULL（见 §6 的 `match`）。
4. 源表、全部节点袋、全部维护状态、receipt 与 generation 同属一个发布世代，在同一事务内原子可见（§10）。

## 3. 必要索引

- 连接节点：在 `on` 中每个左列/右列投影出的连接键 `k` 上建哈希桶索引，左右各一。桶条目按整行存重量，使键查找为 O(1) 期望，桶内聚合为 O(桶大小)。
- DISTINCT：取值 → 支持计数的哈希表（即输出行的隐藏重量）。
- 分组：分组键 → 累加器的哈希表；`keys=[]` 恒有键 `()`。
- 源：`(src,rid)` 主键修订表（现有 `records` 表已满足）。
- 所有索引随增量做 O(受影响键) 的算术更新；索引与节点袋在同一事务中维护，禁止“只改袋不改索引”的中间可见点。

## 4. 无状态一元与多集算子（project / filter / union）

设输入增量为 `Δ`（带符号袋）。

```text
project(n):  Δ_n = Counter()
             for (row, w) in Δ: Δ_n[proj_cols(row)] += w        # 重复列位置合法，同投影行累加
filter(n):   Δ_n = Counter()
             for (row, w) in Δ:
                 if three_valued(where, row) is TRUE: Δ_n[row] += w   # FALSE/UNKNOWN 都不保留
union(n):    Δ_n = Σ_{child in inputs} Δ_child                  # UNION ALL，同一子计划可在多条边上各计一次
B'_n = B_n + Δ_n；零重量条目删除。
```

三值逻辑按 `rel.scalar` 的真值表（AND/OR/NOT/IS NULL，NULL 比较得 UNKNOWN）。这些算子无隐藏状态，组合性由“输出净差 = 输入净差的同态像”保证；C2 验证共享 union 边的重量精确性。

## 5. 内连接差分（修正 D3）

记旧袋 `L,R`、新袋 `L'=L+ΔL`、`R'=R+ΔR`，`match(x,y)` 为 `on` 上全部等式成立（任一侧为 NULL 即假）。精确恒等式：

```text
(L' ⋈ R') − (L ⋈ R) = ΔL ⋈ R  +  L ⋈ ΔR  +  ΔL ⋈ ΔR
```

可执行伪代码（只触碰受影响键桶，不展开重量）：

```text
Δ_inner(n):
  out = Counter()
  # 左增量 × 旧右桶
  for (x, w) in ΔL:
      for (y, wy) in R_old.bucket(key_of(x)):      # 仅所需关联桶
          out[x+y] += w * wy
  # 旧左桶 × 右增量
  for (y, w) in ΔR:
      for (x, wx) in L_old.bucket(key_of(y)):
          out[x+y] += wx * w
  # 跨项：左增量桶 × 右增量桶，恰好一次
  for k in keys(ΔL) ∩ keys(ΔR):
      for (x, wx) in ΔL.bucket(k):
          for (y, wy) in ΔR.bucket(k):
              if match(x,y): out[x+y] += wx * wy
  return out
```

证明：把 `L'=L+ΔL`、`R'=R+ΔR` 代入双线性 `⋈` 展开即得四项 `L⋈R + ΔL⋈R + L⋈ΔR + ΔL⋈ΔR`，减去旧项恰为上式；实现把跨项独立计算且只算一次，消除现网 `ivm.py:50-51` 用新袋导致的跨项双计（M3、M11）。工作量随受影响键的实际配对数增长，不读取无关键桶。

## 6. 左外连接与反关联：支持计数与零匹配翻转（修正 D1、D2、D4）

定义键级右支持 `sup_R(k)` 与左支持 `sup_L(k)`。左外输出 = 匹配配对（同内连接）加上**零匹配补位**：左行 `x` 在 `sup_R(key(x))=0` 时，按其重量贡献 `x+NULL^wR`。反关联输出 = `sup_R(key(x))=0` 的左行原样输出。关键在于补位/存在性是**键级**性质，翻转只发生在支持计数跨零的键，且对该键上**所有左出现**（无论该行本批是否变动）生效。

### 6.1 维护支持计数与桶

```text
for (y, w) in ΔR: R.add(y, w); sup_R[key(y)] += w; 若归 0 删除计数条目
for (x, w) in ΔL: L.add(x, w); sup_L[key(x)] += w
```

### 6.2 左外连接差分

```text
Δ_left(n):
  out = Δ_inner_matches(n)          # §5 的匹配配对净差（含一次跨项）
  # 零匹配补位翻转：只看支持跨零的键
  for k in support_boundary_keys(ΔR, ΔL):
      old_zero = (old_sup_R(k) == 0); new_zero = (new_sup_R(k) == 0)
      if old_zero and not new_zero:                # 未匹配 -> 已匹配：撤回补位
          for (x, wx) in L'.bucket(k): out[x+NULL^wR] -= wx
      elif not old_zero and new_zero:              # 已匹配 -> 未匹配：重补
          for (x, wx) in L'.bucket(k): out[x+NULL^wR] += wx
      # old_zero=new_zero=True：补位重量仅随左增量变化
      if old_zero and new_zero:
          for (x, wx) in ΔL.bucket(k): out[x+NULL^wR] += wx
  return out
```

其中 `support_boundary_keys` 为 `keys(ΔR) ∪ keys(ΔL)` 中 `old_sup_R` 与 `new_sup_R` 零状态不同、或两侧同零且有左增量的键。变动左行的补位必须同时用旧、新两套支持判定（对照 M9：现网只用新右袋，丢失旧出现的翻转）；不变左行由键级翻转统一覆盖（对照 M1、M8：现网完全不覆盖）。NULL 键的 `sup_R` 恒按“无匹配”处理，因为 `match` 对 NULL 为假（C3）。

### 6.3 反关联差分

```text
Δ_anti(n):
  out = Counter()
  # 左增量在新右袋上的存在性（新版本判定，这一路现网 ivm.py:47-48 做对了）
  for (x, w) in ΔL:
      if new_sup_R(key(x)) == 0: out[x] += w
  # 右增量驱动的、不变左行翻转（现网缺失，D2）
  for k in keys(ΔR):
      if old_sup_R(k) == 0 and new_sup_R(k) > 0:     # 0 -> 1：撤回该键全部旧左行
          for (x, wx) in L.bucket(k): out[x] -= wx
      elif old_sup_R(k) > 0 and new_sup_R(k) == 0:  # 1 -> 0：重新发出新袋中的左行
          for (x, wx) in L'.bucket(k): out[x] += wx
  return out
```

M2 验证两个右向翻转；M10 验证左增量存在性路径与右驱动翻转是两个独立病因，修右驱动时不能破坏左增量的新袋判定。

### 6.4 工作量边界

两条规则只读取：变动行、其键对应的对侧桶（形成实际变化配对）、以及支持跨零键上的左桶（零匹配翻转所必需）。右支持保持为正且左侧无变化的键，不重读任何左桶——满足契约的存在性测试约束。

## 7. DISTINCT：支持计数跨零（修正 D5）

```text
Δ_distinct(n):
  out = Counter()
  for (row, w) in Δ:                       # Δ 为输入带符号净差
      old = sup_d.get(row, 0); new = old + w
      assert new >= 0                      # 不变量：支持计数不可为负
      if old == 0 and new > 0: out[row] += 1
      elif old > 0 and new == 0: out[row] -= 1
      sup_d[row] = new                     # 归 0 可删条目
  return out
```

输出重量恒为 1；支持 1→2、2→1 不产生任何输出变化（M4 的两个错误批次由此消除）。判断依据是维护计数的跨零，而非增量符号；同值多记录按计数算术合并，不展开。

## 8. 分组累加器：HAVING 与无键全局组（修正 D6、D7）

```text
Δ_group(n):
  if n.keys == []: ensure acc[()] exists                    # 全局组恒存在（D7）
  touched = set()
  for (row, w) in Δ:
      k = tuple(row[i] for i in n.keys); touched.add(k)
      a = acc.setdefault(k, (0,0,0))
      a.COUNT_STAR += w
      if row[n.value] is not None:
          a.SUM += w * row[n.value]; a.COUNT_VALUE += w
  out = Counter()
  for k in touched:                                         # 只重算受影响组，不遍历全表
      a = acc[k]
      if n.keys == [] or a.COUNT_STAR > 0:                 # 键组计数归 0 即消失；全局组永存
          visible_sum = a.SUM if a.COUNT_VALUE > 0 else NULL
          row_out = k + (visible_sum, a.COUNT_STAR, a.COUNT_VALUE)
          old_visible = (k in old_output) and passes(old_having_pred(...))
          new_visible = passes_having(n.having, visible_sum, a)
          if new_visible and not old_visible: out[row_out] += 1
          elif old_visible and not new_visible: out[old_row(k)] -= 1
          elif old_visible and new_visible:
              out[old_row(k)] -= 1; out[row_out] += 1      # 行内容变化也算净差
  return out
```

要点：

- 累加器独立持久化，被 HAVING 抑制的组仍保留完整 `(SUM,COUNT(*),COUNT(value))`，重新可见时恢复真实累计（M5）。`passes_having`：`having=null` 恒过；否则仅当 `SUM IS NOT NULL AND SUM > threshold`（严格大于；SUM 为 NULL 的组被抑制）。
- `keys=[]` 时 `acc[()]` 永不删除，空输入导出 `[NULL,0,0]`（HAVING 前）；HAVING 可合法抑制该行，但累加器仍在（M6）。
- 零和非空组是真实组：`COUNT(*)>0` 且 `SUM=0` 在无 HAVING 时正常输出（B3 的 `[null,0,2,2]`）；全 NULL 值使 `COUNT(value)=0`、SUM 导出 NULL 但 `COUNT(*)` 为正（B3 的 `[1,null,1,0]`）。
- 只遍历 `touched` 键，禁止重扫组成员，全局组也不例外。

## 9. 组合性、共享节点传播与符号净差保持

**组合定理**：若每个节点规则满足 `B'_n = F_n(新版本子袋)` 且 `Δ_n = B'_n − B_n`（F 为该算子的全量语义），则按拓扑序单次传播后，根节点 `B'_root` 等于对本批最终源袋做全量求值的结果。

归纳证明：源节点经修订合并后 `B'_s` 即最终源袋，基成立。设节点 `n` 的全部子节点已满足 `B'_c=F_c(...)`。

- project/filter/union 是袋同态（§4），`Δ_n` 是 `Δ_child` 的同态像，`B_n+Δ_n=F_n(B'_children)`。
- 连接由双线性展开（§5）与键级支持翻转（§6）逐配对、逐键精确构造新旧袋之差，加减项与 `F_n` 的成员判定一一对应，故 `B_n+Δ_n=F_n(...)`。
- distinct/group 的输出是维护状态的纯函数，跨零/可见性翻转枚举了 `F_n` 输出集合的全部成员变化（§7、§8）。

共享子计划：节点只处理一次、只产出一个 `Δ_n`，多消费者读取同一对象；袋同态与双线性保证同一 `Δ_n` 在每条边上贡献正确次数（union 自边在 C2 计两次、连接自边在 M11 应只计一次跨项），无需额外版本协议。

**符号净差**：所有 `Δ` 都是两个完整袋的净差而非瞬变序列；输入物理顺序改变只改变 `Counter` 合并顺序，整数加法可交换，结果不变。负重量在中间表示中允许存在，发布袋在 `B+Δ` 后删除零/负条目；现网 `store.py:121` 用 `rel.diff(new_root, old_root)` 计算收据净差的做法正确，应保留。

## 10. 原子持久化与发布边界

沿用现网单事务模型（`store.py:82-127` 的结构正确，保留）：

1. `BEGIN IMMEDIATE` 取得写锁；先查 receipts 实现幂等重放（原始收据在任何其他检查前返回）。
2. 在事务内完成修订合并、源袋净差、按拓扑序的全部节点 `Δ_n`、桶索引/支持计数/累加器更新。
3. 源记录、全部节点袋与维护状态、generation、receipt 在同一事务写入；`after_sources`/`after_views` 故障点抛出即整体回滚，数据库不留半态。语义错误（本规范要消除的 D1-D7）与事务原子性无关：当前事故都是**整笔提交的错误袋**，不能用“SQLite 只提交了一半”解释。
4. 维护状态须落库（新表或节点 body 的版本化结构），使重开连接后索引与计数可重建/直接加载；仅持久化输出袋不足以恢复（M2、M5、M6 重开后仍错）。
5. 世代内全部对象同生共死；receipt 记录该原始世代的根净差，后续重放返回原 receipt，不随当前 read 改变。

## 11. 旧库复用、在线回填与切换条件

**可直接复用**：`records` 修订表（可重建任意时刻全部源袋，是可重建的事实来源）、`meta`（plan、generation）、`receipts`（请求审计与幂等）。**不可信任**：现有 `nodes` 袋——它可能已携带 D1-D7 的错误内容，不能作为新维护状态的种子（原设计文档“无需 backfill 即可复用”的声明作废）。

在线回填（仓库持续接受序列化的公开调用，允许一次初始全源扫描，禁止逐批全刷新）：

1. **冻结模式版本号**：在 `meta` 引入状态格式版本；新代码双读、旧代码只读旧格式。部署计划本身在库初始化后不可变。
2. **影子构建**：新维护状态（桶索引、支持计数、distinct 支持表、分组累加器）写入带新前缀/新表的影子区，从 `records` 重放源袋做**一次**全量计算；构建期间公开事务照常提交，每笔已提交批次的源净差同时记入一份短回填日志（或基于 generation 水位拉取增量）。
3. **追赶**：构建器按 generation 顺序把水位之后的已提交净差算术应用到影子状态；因为公开调用串行化，追赶只需重放源修订净差，不与在线写冲突。
4. **校验门**：影子根袋与对当前源袋的一次独立全量求值比对（`rel.evaluate` 仅作迁移期校验，不作为运行期策略），并抽样核对支持计数/累加器守恒；不一致则放弃影子区、保留旧库继续服务、重试构建。
5. **切换条件（全部满足）**：(a) 影子水位等于最新已提交 generation；(b) 校验门通过；(c) 切换在一个事务内完成 `meta` 版本翻转与节点表原子替换；(d) 切换后第一笔普通批次按新规则处理并再次核对根净差。切换前任何失败都可回退到旧只读路径，不丢已接受修订。
6. 重建缺失状态的最小充分条件是可取得完整源修订历史（`records` 完备且与 receipts 的 generation 连续）；若历史存在缺口，只能从受影响世代起拒绝切换并告警，不得用可能已损坏的旧节点袋凑补。

## 12. 受影响键边界：正确性与工作量的统一

正确性规则与工作约束由同一原则驱动：**一个节点的输出成员关系只可能在其输入增量触及的键上改变**。

- 连接：变动键 = `keys(ΔL) ∪ keys(ΔR)`；仅这些键的桶、支持计数与补位/存在性需要处理；无关键桶不读。
- distinct：仅 `keys(Δ)` 的支持计数需要检查跨零。
- 分组：仅 `touched` 组的累加器与可见性需要重算，不遍历组全集。
- 下游后果可能必然很大：当一个键的翻转影响该键上的全部出现（如右支持 1→0 使左桶所有出现补位/反关联发出），或一个高重量源行变动经连接扇出到大量配对时，输出净差本身就与受影响配对数同阶——这是结果规模决定的必要工作，不是违规扫描。违规是指读取与本批变化键无关的成员（现网 `rel.py:51-56`、`ivm.py:16,25,47-48` 的全扫），规范中的桶索引与 touched 集合将其消除。
- 全程以重量算术（`wx*wy`、计数加减）表达，不展开多重度，不拒绝复杂计划：任意 DAG（多消费者、同子计划多边、嵌套分组/外连接/反关联/distinct）都按 §9 归纳组合。

## 13. 证据对照

本规范每条规则对应 `review.json` 用例：§4↔C2/C5；§5↔M3/M11；§6↔M1/M8/M9/M2/M10/C3；§7↔M4/M11；§8↔M5/M6/B3；§9 共享 DAG↔C2/M11；§10 持久化与重开↔全部带 `reopen_after` 的用例（C1、C3、B3、C5、M1、M2、M4、M5、M6、M8、M11）；§11 回填↔M2/M5/M6 的重开持久化错误。运行清单：`python3 review_evidence.py` 生成证据，`python3 checks.py verify` 重算并复核，`python3 auditcheck.py demo` 展示实际与期望行。
