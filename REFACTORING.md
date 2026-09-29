# Registry materialization: one incremental work queue

This note describes the refactor that unified how a `Registry` discovers
resources, canonicalizes their URIs, registers anchors and schedules
subresources. All public behavior and raised exceptions are unchanged; the
behavioral guard suite is in `referencing/tests/test_materialization.py`.

## The old five entry points

Before the refactor each way of getting material into (or out of) a registry
implemented discovery separately:

- `Registry.with_resource` / `Registry.with_resources` inserted a resource
  into the resource map and added its raw URI to an `_uncrawled` set, with no
  ordered representation of what work remained
  (`referencing/_core.py:523`, `referencing/_core.py:529`).
- `Registry.__rmatmul__` (`resource @ registry`) did the same insertion loop
  inline rather than reusing `with_resources` (`referencing/_core.py:367`).
- `Registry.crawl` owned the only actual traversal: it built an ad-hoc Python
  `list` of `(uri, resource)` pairs out of the `_uncrawled` set and popped it
  to completion (`referencing/_core.py:517`).
- `Registry.combine` merged the maps and the `_uncrawled` sets but had no
  notion of pending traversal, so a resource common to two registries could be
  traversed twice (`referencing/_core.py:567`).
- `Registry.get_or_retrieve` — used by every on-demand `Resolver.lookup`
  (`referencing/_core.py:834`) — simply called the *full* `crawl()` before
  consulting the retrieval callable, so a single lookup expanded resources
  unrelated to the target (`referencing/_core.py:423`).

`Registry.anchor` likewise forced a full crawl (`referencing/_core.py:479`),
and that behavior is deliberately retained because an anchor can live in a
subresource (including one without an internal id) that is only resolvable
against the queried URI.

## The new shared work queue

All five entry points now converge on one representation and one procedure.

State (`referencing/_core.py:337`):

- `_resources` — the public, canonical-URI keyed resource map. Resources
  explicitly added are present immediately; resources *discovered* while
  crawling are keyed in only once they are expanded.
- `_uncrawled` — canonical URIs scheduled but not yet expanded. This drives
  the `repr` crawled/uncrawled counts exactly as before.
- `_expanded` — canonical URIs whose id, anchors and direct subresources have
  already been processed. Membership here is what an on-demand lookup waits
  for (`referencing/_core.py:338` and the check at `referencing/_core.py:432`).
- `_to_crawl` — the pending items in stable LIFO order. Each item is a
  `(base_uri, resource)` pair. The base is the URI the (possibly relative)
  internal id must be resolved against; for an explicitly added resource it
  is the URI given to `with_resource` (`referencing/_core.py:339`).

The shared pieces are:

- `_advance(target)` — pops `_to_crawl` in LIFO order until the queue drains
  (explicit `crawl`, or an anchor lookup) or until a *resource* `target` has
  been expanded (on-demand lookup), then returns a new immutable registry
  (`referencing/_core.py:632`).
- `_expand(base_uri, resource)` — the single "expand one resource once" step:
  canonicalize the id with `urljoin(base_uri, id)`, record the resource under
  that canonical URI, register every anchor at that URI, and discover each
  direct subresource (`referencing/_core.py:691`).
- `_schedule(base_uri, resource)` — the single "discover one subresource"
  step (`referencing/_core.py:737`).

`with_resources` and `__rmatmul__` now both just enqueue; `combine` rebuilds
one queue for the merged `_uncrawled` set, recomputing each item's canonical
URI from its own resource so a shared resource is enqueued once
(`referencing/_core.py:597`–`referencing/_core.py:612`); `get_or_retrieve`, `crawl` and `Resolver.lookup`
all call `_advance`.

## Queue invariants

1. **Stable LIFO order.** Items are processed newest-first from the immutable
   list; the order depends only on insertion order, never on set iteration
   (`referencing/_core.py:652`). The guards assert exact expansion sequences,
   e.g. a cyclic graph `a ↔ b` expands as `['urn:b', 'urn:a']` and a shared
   subresource expands exactly once.
2. **One expansion per resource.** `_schedule` enqueues a keyed subresource
   only if its canonical URI is in neither `_expanded` nor `_uncrawled`
   (`referencing/_core.py:764`). Cycles and diamond/shared subgraphs therefore
   cannot grow the queue; a second `crawl()` on an already-crawled registry is
   a no-op.
3. **Unkeyed subresources never get keyed.** A subresource without an
   internal id cannot be addressed by URI; its only effect is to contribute
   anchors at the parent's base URI, so `_schedule` expands it immediately
   without touching the resource map (`referencing/_core.py:750`).
4. **Relative ids resolve against the parent.** A discovered child is queued
   with the *parent's* canonical URI as its base, and only keyed under
   `urljoin(parent, child_id)` when it is later expanded
   (`referencing/_core.py:772`). This preserves the existing behavior relied
   on by nested relative ids such as `http://example.com/` → `child/` →
   `grandchild`.
5. **Immutability.** Every step produces new rpds-py structures via
   `attrs.evolve`; the registry a caller holds is never mutated by materializing
   a later registry. The guard `test_old_registry_is_not_mutated_by_...` pins
   this down.

## Why on-demand lookup is not a full crawl

Previously `get_or_retrieve` called `crawl()` unconditionally, so
`Resolver.lookup("urn:target")` expanded every known root, including branches
with no path to the target. Now it calls `_advance(target_uri)`, which stops
the instant the requested URI lands in `_expanded` (`referencing/_core.py:650`,
`referencing/_core.py:431`). In the guard's chain/side-branch graph, looking
up `urn:target` expands only

```
['urn:main', 'urn:mid', 'urn:target']
```

and leaves `urn:side` in `_uncrawled`; an explicit `crawl()` afterwards drains
the remaining `['urn:side', 'urn:sidechild']` (see
`test_on_demand_lookup_is_not_a_full_crawl`).

This is still correct, not merely lazy: the LIFO walk is depth-first, so by
the time the target is expanded everything on every path that could reach it
has already been processed; the only items left are provably unrelated. When
the target is *not* reachable from known resources, the queue is drained
exactly once and the retrieval callable is invoked a single time
(`referencing/_core.py:442`); the retrieved resource is registered uncrawled
but is not itself crawled. In other words the minimum work needed to answer
the lookup is done eagerly, and only an explicit `crawl()` performs the rest —
on-demand lookup never degrades into a full scan.

## Exceptions

The exception surface is unchanged. The retrieval callable's
`CannotDetermineSpecification` and `NoSuchResource` propagate untouched, any
other error is wrapped in `Unretrievable` with the original retained as
`__cause__` (`referencing/_core.py:445`), and `Resolver.lookup` wraps
`NoSuchResource`/`Unretrievable` in `Unresolvable`. The guard
`test_arbitrary_retrieve_error_keeps_original_cause` checks the chained cause
end to end.
