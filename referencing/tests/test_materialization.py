"""
Behavioral guards for the Registry materialization work queue.

These tests pin down the shared behavior of the five materialization
entry points (``__rmatmul__``/``with_resource(s)``, ``crawl``,
``combine``, ``get_or_retrieve`` and resolver-driven lookups) before and
after the incremental work queue refactor.
"""

from __future__ import annotations

import pytest

from referencing import Anchor, Registry, Resource, Specification, exceptions


def counted_specification(name="counted"):
    """
    Build a specification plus a counter of resource expansions.

    A resource is expanded exactly once when its ``anchors_in`` and
    ``subresources_of`` are consulted together a single time.
    """

    counts = {}

    def id_of(contents):
        return contents.get("ID") if isinstance(contents, dict) else None

    def subresources_of(contents):
        counts[id(contents)] = counts.get(id(contents), 0) + 1
        if not isinstance(contents, dict):
            return []
        return contents.get("children", [])

    def anchors_in(specification, contents):
        if not isinstance(contents, dict):
            return []
        return [
            Anchor(
                name=anchor_name,
                resource=specification.create_resource(each),
            )
            for anchor_name, each in contents.get("anchors", {}).items()
        ]

    specification = Specification(
        name=name,
        id_of=id_of,
        subresources_of=subresources_of,
        anchors_in=anchors_in,
        maybe_in_subresource=lambda segments, resolver, subresource: resolver,
    )
    return specification, counts


class TestSharedProcess:
    def test_combine_with_common_resources(self):
        """
        Two registries sharing a subresource produce one canonical entry
        and the shared resource is expanded a single time when crawling.
        """

        specification, counts = counted_specification()

        shared = {"ID": "urn:shared", "anchors": {}}
        left_contents = {"ID": "urn:left", "children": [shared]}
        right_contents = {"ID": "urn:right", "children": [shared]}

        left = specification.create_resource(left_contents) @ Registry()
        right = specification.create_resource(right_contents) @ Registry()

        combined = left.combine(right)
        crawled = combined.crawl()

        assert set(crawled) == {"urn:left", "urn:right", "urn:shared"}
        assert counts[id(shared)] == 1
        # The shared object was a child under both parents but discovered
        # only once.
        assert counts[id(left_contents)] == 1
        assert counts[id(right_contents)] == 1

        # Both pre-combine registries are untouched.
        assert set(left) == {"urn:left"}
        assert set(right) == {"urn:right"}
        assert "urn:shared" not in left
        assert "urn:shared" not in right

    def test_combine_conflicting_retrieve_callables(self):
        """
        Combining registries with two different retrieve callables raises
        ValueError and never invokes either callable.
        """

        calls = []

        def first_retrieve(uri):
            calls.append(("first", uri))

        def second_retrieve(uri):
            calls.append(("second", uri))

        resource = Resource.opaque({})
        first = Registry(retrieve=first_retrieve).with_resource(
            "urn:first",
            resource,
        )
        second = Registry(retrieve=second_retrieve).with_resource(
            "urn:second",
            resource,
        )

        with pytest.raises(ValueError, match="conflict.*retriev"):
            first.combine(second)
        assert calls == []

        # Neither registry is mutated.
        assert first._retrieve is first_retrieve
        assert second._retrieve is second_retrieve

    def test_subresource_without_id_is_enqueued_once_per_parent(self):
        """
        Anonymous subresources (no internal ID) are keyed by their base URI.

        The same in-memory anonymous child reached twice under the same base
        is a single work item and expands once. Reached under two different
        parents it is a legitimate distinct anchor in each parent and expands
        once per parent. Cycles through an anonymous child never grow.
        """

        specification, counts = counted_specification()

        shared_under_one_base = {"anchors": {"k": 1}, "children": []}
        # Two different parents which resolve to the *same* canonical base
        # URI (one absolute, one relative that joins to the same URI) share
        # the one anonymous child, which is scheduled a single time.
        parent_a = {
            "ID": "http://example.com/x",
            "children": [shared_under_one_base],
        }
        parent_b = {
            "ID": "http://example.com/",
            "children": [
                {"ID": "x", "children": [shared_under_one_base]},
            ],
        }
        # A genuinely different base keeps the child as its own anchor.
        other_base_child = {"anchors": {"k": 2}, "children": []}
        parent_c = {"ID": "urn:c", "children": [other_base_child]}

        registry = specification.create_resource(parent_a) @ Registry()
        registry = specification.create_resource(parent_b) @ registry
        registry = specification.create_resource(parent_c) @ registry
        crawled = registry.crawl()

        assert counts[id(shared_under_one_base)] == 1
        assert counts[id(other_base_child)] == 1
        assert "http://example.com/x" in crawled
        assert "urn:c" in crawled
        assert crawled.anchor("http://example.com/x", "k") is not None
        assert crawled.anchor("urn:c", "k") is not None

    def test_anonymous_cycle_does_not_grow(self):
        specification, counts = counted_specification()
        root = {"ID": "urn:root", "children": []}
        anonymous = {"anchors": {}, "children": [root]}
        # The anonymous child points back at its own base, which must not
        # re-schedule the already-expanded root.
        root["children"].append(anonymous)

        registry = specification.create_resource(root) @ Registry()
        crawled = registry.crawl()
        assert set(crawled) == {"urn:root"}
        assert counts[id(root)] == 1
        assert counts[id(anonymous)] == 1

    def test_explicit_re_add_reopens_one_uri_only(self):
        """
        Re-adding an already crawled URI schedules just that URI again
        without revisiting the rest of the graph.
        """

        specification, counts = counted_specification()
        root = {
            "ID": "urn:root",
            "children": [{"ID": "urn:child"}],
        }
        registry = specification.create_resource(root) @ Registry()
        crawled = registry.crawl()
        assert counts[id(root)] == 1

        replacement = specification.create_resource(
            {"ID": "urn:root", "children": []},
        )
        reopened = crawled.with_resource("urn:root", replacement).crawl()
        assert set(reopened) == {"urn:root", "urn:child"}
        # The child registered earlier survives; only urn:root re-expands.
        assert counts[id(root)] == 1
        assert counts[id(replacement.contents)] == 1

    def test_relative_subresource_id(self):
        """
        A relative subresource ID is resolved against its parent's base
        URI, exactly as in the previous crawler.
        """

        specification, _ = counted_specification()
        root = {
            "ID": "http://example.com/root/",
            "children": [
                {"ID": "child", "children": [{"ID": "grand"}]},
            ],
        }
        registry = specification.create_resource(root) @ Registry()
        crawled = registry.crawl()
        assert "http://example.com/root/child" in crawled
        assert "http://example.com/root/grand" in crawled
        assert len(crawled) == 3

    def test_same_named_anchors_in_different_resources(self):
        """
        Anchors are keyed by (canonical URI, name), so same-named anchors
        in different resources are both visible; within a resource later
        registration wins.
        """

        specification, _ = counted_specification()
        root = {
            "ID": "urn:root",
            "children": [
                {"ID": "urn:a", "anchors": {"foo": {"via": "a"}}},
                {"ID": "urn:b", "anchors": {"foo": {"via": "b"}}},
            ],
        }
        registry = specification.create_resource(root) @ Registry()
        crawled = registry.crawl()
        assert crawled.anchor("urn:a", "foo").value.resolve(
            crawled.resolver(),
        ).contents == {"via": "a"}
        assert crawled.anchor("urn:b", "foo").value.resolve(
            crawled.resolver(),
        ).contents == {"via": "b"}

    def test_cyclic_graph_does_not_loop_and_dedupes(self):
        """
        Cycles between identified resources terminate; each resource object
        is expanded once.
        """

        specification, counts = counted_specification()
        root = {"ID": "urn:root", "children": []}
        child = {"ID": "urn:child", "children": [root, root]}
        root["children"].append(child)

        registry = specification.create_resource(root) @ Registry()
        crawled = registry.crawl()
        assert set(crawled) == {"urn:root", "urn:child"}
        assert counts[id(root)] == 1
        assert counts[id(child)] == 1

    def test_remove_uncrawled_resource(self):
        """
        Removing an uncrawled resource also drops its pending queue entry,
        so crawling never rediscovers it or its anchors.
        """

        specification, _ = counted_specification()
        root = {
            "ID": "urn:root",
            "children": [{"ID": "urn:child", "anchors": {"foo": 1}}],
        }
        registry = specification.create_resource(root) @ Registry()
        assert repr(registry) == "<Registry (1 uncrawled resource)>"

        removed = registry.remove("urn:root")
        assert "urn:root" not in removed
        assert repr(removed) == "<Registry (0 resources)>"
        crawled = removed.crawl()
        assert "urn:child" not in crawled
        with pytest.raises(exceptions.NoSuchResource):
            crawled.anchor("urn:child", "foo")
        with pytest.raises(exceptions.NoSuchResource):
            removed.remove("urn:root")

    def test_retrieve_cannot_determine_specification_passes_through(self):
        def retrieve(uri):
            return Resource.from_contents({})

        registry = Registry(retrieve=retrieve)
        with pytest.raises(exceptions.CannotDetermineSpecification):
            registry.get_or_retrieve("urn:mystery")

    def test_retrieve_arbitrary_exception_cause_is_preserved(self):
        original = RuntimeError("boom")

        def retrieve(uri):
            raise original

        registry = Registry(retrieve=retrieve)
        with pytest.raises(exceptions.Unretrievable) as info:
            registry.get_or_retrieve("urn:boom")
        assert info.value.__cause__ is original

    def test_retrieve_no_such_resource_passes_through(self):
        def retrieve(uri):
            raise exceptions.NoSuchResource(ref=uri)

        registry = Registry(retrieve=retrieve)
        with pytest.raises(exceptions.NoSuchResource) as info:
            registry.get_or_retrieve("urn:nope")
        assert info.value.__cause__ is None

    def test_lookup_only_materializes_what_is_needed(self):
        """
        An on-demand lookup advances just far enough to find the target;
        explicit crawl() afterwards does the remaining work, and retrieve
        is called only after the queue is drained.
        """

        specification, counts = counted_specification()
        target = {"ID": "urn:target", "children": []}
        decoy_chain = {
            "ID": "urn:decoy",
            "children": [
                {"ID": "urn:decoy-2", "children": [{"ID": "urn:decoy-3"}]},
            ],
        }
        root = {
            "ID": "urn:root",
            "children": [target, decoy_chain],
        }

        retrieve_calls = []

        def retrieve(uri):
            retrieve_calls.append(uri)
            raise exceptions.NoSuchResource(ref=uri)

        registry = specification.create_resource(root) @ Registry(
            retrieve=retrieve,
        )
        resolver = registry.resolver()
        resolved = resolver.lookup("urn:target")
        assert resolved.contents is target

        partial = resolved.resolver._registry
        assert "urn:target" in partial
        assert len(partial) < 5
        # The decoy subtree was not fully walked just to find the target.
        assert counts[id(target)] == 1

        # Nothing to retrieve: the queue was checked before the retriever.
        assert retrieve_calls == []

        full = partial.crawl()
        assert set(full) == {
            "urn:root",
            "urn:target",
            "urn:decoy",
            "urn:decoy-2",
            "urn:decoy-3",
        }
        assert counts[id(target)] == 1

    def test_retrieve_called_once_and_then_cached(self):
        resource = Resource.opaque({"foo": "bar"})
        calls = []

        def retrieve(uri):
            calls.append(uri)
            return resource

        registry = Registry(retrieve=retrieve)
        first = registry.get_or_retrieve("urn:remote")
        second = first.registry.get_or_retrieve("urn:remote")
        assert first.value is resource
        assert second.value is resource
        assert calls == ["urn:remote"]

    def test_old_registry_not_side_effected(self):
        """
        Materialization returns new registries; the original keeps its
        uncrawled state and repr.
        """

        specification, _ = counted_specification()
        root = {
            "ID": "urn:root",
            "children": [{"ID": "urn:child"}],
        }
        registry = specification.create_resource(root) @ Registry()
        assert repr(registry) == "<Registry (1 uncrawled resource)>"

        crawled = registry.crawl()
        assert set(crawled) == {"urn:root", "urn:child"}
        assert "urn:child" not in registry
        assert repr(registry) == "<Registry (1 uncrawled resource)>"
        assert repr(crawled) == "<Registry (2 resources)>"

    def test_repr_counts_after_partial_advance(self):
        specification, _ = counted_specification()
        root = {
            "ID": "urn:root",
            "children": [
                {"ID": "urn:a"},
                {"ID": "urn:b"},
            ],
        }
        registry = specification.create_resource(root) @ Registry()
        partial = registry.resolver().lookup("urn:a").resolver._registry
        # Root has been crawled, one entry still remains pending.
        assert repr(partial) == "<Registry (2 resources, 1 uncrawled)>"
        assert repr(registry) == "<Registry (1 uncrawled resource)>"


class TestExpansionScenarioOutput:
    def test_print_expansion_counts(self):
        """
        Print the per-scenario expansion counts (visible with ``-s``).
        """

        scenarios = []

        def make(name, contents):
            specification, counts = counted_specification(name)
            registry = specification.create_resource(contents) @ Registry()
            registry.crawl()
            total = sum(counts.values())
            scenarios.append((name, total))
            return total

        chain = {
            "ID": "urn:a",
            "children": [
                {"ID": "urn:b", "children": [{"ID": "urn:c"}]},
            ],
        }
        make("linear-chain", chain)

        shared = {"ID": "urn:shared"}
        diamond = {
            "ID": "urn:root",
            "children": [
                {"ID": "urn:left", "children": [shared]},
                {"ID": "urn:right", "children": [shared]},
            ],
        }
        assert make("diamond-shared-subresource", diamond) == 4

        cycle_a = {"ID": "urn:a", "children": []}
        cycle_b = {"ID": "urn:b", "children": [cycle_a]}
        cycle_a["children"].append(cycle_b)
        assert make("id-cycle", cycle_a) == 2

        self_anchor = {"ID": "urn:self", "children": []}
        self_anchor["children"].append(self_anchor)
        assert make("self-cycle", self_anchor) == 1

        print()  # noqa: T201
        for name, total in scenarios:
            print(f"  {name}: {total} expansion(s)")  # noqa: T201
