# Registry 物化重构说明

`referencing.Registry` 是不可变映射：资源先进入“未爬取（uncrawled）”集合，之后才由不同入口按需或全量物化为规范 URI、anchor 与子资源。本次重构把原先分散在五条入口里的物化逻辑收敛为**单一增量工作队列**，同时保持全部公开行为与异常。

行为守卫位于 `referencing/tests/test_materialization.py`，实现位于 `referencing/_core.py`。

## 旧的五条入口（重构前）

重构前每条路径各自维护 resource id、子资源和 anchor，语义分散且存在重复展开：

1. **`__rmatmul__` / `with_resource(s)`**：把资源直接塞进 `_resources` 与 `_uncrawled` 集合，不做展开（`referencing/_core.py:417`、`referencing/_core.py:579`）。
2. **`crawl()`**：旧实现用一个临时 Python list 做 LIFO，`pop()` 后就地规范 URI、登记 anchor，再 `extend` 子资源；它**没有任何去重记忆**，因此已标识资源构成的循环（如 A↔B）会无限循环，共享子资源也会被重复“展开”（只靠 map insert 幂等掩盖了重复注册）。
3. **`combine()`**：直接 `update` resources / anchors / uncrawled 三个结构（`referencing/_core.py:712`），只能合并集合层面的信息。
4. **`get_or_retrieve()`**：找不到 URI 时直接调用**全量** `self.crawl()`，之后才调用 retrieve callable（`referencing/_core.py:473`）。
5. **`Resolver.lookup()` / `anchor()`**：`anchor()` 同样先全量 `crawl()`，`lookup()` 经由 `get_or_retrieve()` 间接触发全量爬取（`referencing/_core.py:818`、`referencing/_core.py:528`）。

也就是说，旧代码里一次“按需 lookup”实际上退化成了全量扫描，且循环图会死循环。

## 新的工作队列不变量

队列状态全部放在 `Registry` 的一个 `eq=False` 字段里，因此不影响 attrs 全字段相等性：

- `_pending`：有序 FIFO 队列，元素是 `(resolve_base, resource)`（`referencing/_core.py:389`）。
- `_uncrawled`：仍保留为公开 repr/相等语义使用的集合，但**始终由 pending 队列派生**（`_uncrawled_for`，`referencing/_core.py:309`），不再被各入口手工修改，杜绝两者漂移。

核心循环是 `_advance(found=...)`（`referencing/_core.py:612`），五条入口都经过它。其不变量：

1. **稳定顺序**：使用 `collections.deque` 的 FIFO（`referencing/_core.py:628`）；合并队列时保持“首次出现位置、后者覆盖值”的顺序（`_merge_pending`，`referencing/_core.py:330`）。
2. **发现 → 规范 URI → anchor → 子资源** 是唯一一处展开逻辑：弹出后先 `urljoin(base, resource.id())` 规范 URI 并登记资源，再登记 `(uri, anchor.name)` anchor，最后安排直接子资源（`referencing/_core.py:643`–`referencing/_core.py:656`）。
3. **每个资源对象一次推进只展开一次**：`expanded` 集合按对象身份去重，同一匿名资源即使在同一次推进里挂在多个 base 下也只展开一次（`referencing/_core.py:634`、`referencing/_core.py:639`）。
4. **循环 / 共享子资源不会重复增长**：每个待处理项有 `_pending_key` 身份（`referencing/_core.py:294`）——已标识资源用其规范 URI（`("u", urljoin(base, id))`），匿名资源用 `(base, id(resource))`。`scheduled` 集合保证已在队列或本次已展开的项不会再次入队（`referencing/_core.py:631`、`referencing/_core.py:652`–`referencing/_core.py:656`）。这正是 A↔B、自环与菱形共享图现在都终止且只展开一次的原因。
5. **规范 URI 延迟到展开时计算**：`_schedule_subresources` 只把父规范 URI 作为 `resolve_base` 传出，子资源的相对 id 仍在子资源自身被展开时 `urljoin`（`referencing/_core.py:684`），与旧 crawler 的相对 URI 解析逐字一致。
6. **显式添加可重开单个 URI**：`_enqueue` 合并队列时按 key 替换该项，使已爬取的 URI 能被重新添加，而不会重访整图（`referencing/_core.py:593`）。
7. **不可变**：`_advance` / `_enqueue` / `combine` / `remove` 全部用 `attrs.evolve` 返回新 `Registry`；旧 registry 的 `_pending`、`_uncrawled` 与 repr 在新 registry 物化后保持不变（守卫 `test_old_registry_not_side_effected`）。

各入口如何共享该过程：

- `__rmatmul__` 与 `with_resource(s)` → `_enqueue`（`referencing/_core.py:417`、`referencing/_core.py:579`）。
- `crawl()` → 无 `found` 谓词，排空队列；队列空时直接返回自身（`referencing/_core.py:565`）。
- `combine()` → 用 `_merge_pending` 合并有序队列、`_uncrawled_for` 重算集合，并保留原有的 retrieve callable 冲突检查与“later wins”语义（`referencing/_core.py:712`）。
- `get_or_retrieve()` 与 `anchor()` → 带 `found` 谓词的 `_advance`（`referencing/_core.py:473`、`referencing/_core.py:528`）。
- `Resolver.lookup()` 不经改动，继续通过 `get_or_retrieve` / `anchor` 自动获得按需推进（`referencing/_core.py:818`）。

## 为什么按需 lookup 没有退化成全量 crawl

旧代码在 `get_or_retrieve` / `anchor` 中无条件 `self.crawl()`，即一次解析会走完整张图。新代码让 `_advance` 接收一个 `found` 谓词：

- `get_or_retrieve(uri)` 的谓词是“目标 URI 已出现在 resources 中”（`referencing/_core.py:487`）；
- `anchor(uri, name)` 的谓词是“目标 `(uri, name)` 已出现在 anchors 中”（`referencing/_core.py:538`）。

循环每展开一个资源（并安排其子资源）后才检查谓词；一旦命中，就带着**剩余队列**提前返回（`referencing/_core.py:658`–`referencing/_core.py:671`），未触及的子树仍留在 `_pending`，由后续显式 `crawl()` 完成。因此：

- 解析一条深层引用只展开路径上必要的资源前缀，旁边的 decoy 子树不会被遍历（守卫 `test_lookup_only_materializes_what_is_needed` 实测查找 `urn:target` 时 `urn:decoy*` 未展开；深层链查找展开序列恰为 `root → child → leaf`）。
- retrieve callable 仍然只在队列**排空后**目标仍不存在时才调用，所以本地可发现的资源绝不会触发外部检索，且成功检索只发生一次并被缓存（`referencing/_core.py:489`–`referencing/_core.py:503`；守卫 `test_retrieve_called_once_and_then_cached`）。
- 异常行为不变：retrieve 抛 `NoSuchResource` / `CannotDetermineSpecification` 原样透传，其它异常包成 `Unretrievable` 且保留 `__cause__`（`referencing/_core.py:494`–`referencing/_core.py:501`；守卫 `test_retrieve_*`）。

## 行为守卫与验证

`referencing/tests/test_materialization.py` 覆盖：两个 registry 合并的共有资源、不同 retrieve callable 冲突、无 id 子资源、相对 id、同名 anchor、循环图（含自环与匿名回边）、删除未 crawl 资源、retrieve 返回无法判定 specification，并核对 Registry 内容、anchor 可见性、retrieve 调用次数、repr 的 crawled/uncrawled 数量与异常 cause。

运行（仓库测试子模块 `suite/` 未检出时，外部一致性套件文件 `test_referencing_suite.py` 无法收集，可忽略）：

```sh
python3 -m pip install -e . pytest
python3 -m pytest -q referencing/tests/test_materialization.py -s
python3 -m pytest -q
```

`-s` 会打印各场景展开次数：线性链 3、菱形共享 4、id 循环 2、自环 1，均无重复增长。
