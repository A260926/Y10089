"""
Behavior guards for the incremental materialization work queue.

These tests pin down how resources are discovered, canonicalized, how their
anchors are registered, and how subresources are scheduled -- in particular
the fact that on-demand resolution only materializes the work strictly
necessary to find a target, while an explicit `Registry.crawl` finishes the
remaining queue.
"""


import pytest

from referencing import Anchor, Registry, Resource, Specification, exceptions


def counting_specification():
    """
    Build a spec which records how often each resource is expanded.

    Expansion corresponds to a single work-queue step: reading the
    resource's id, registering its anchors and scheduling its subresources.
    """

    expansions: list[str] = []

    def id_of(contents):
        return contents.get("ID")

    def subresources_of(contents):
        expansions.append(contents.get("ID"))
        return contents.get("children", [])

    def anchors_in(specification, contents):
        return [
            Anchor(
                name=name,
                resource=specification.create_resource(contents=each),
            )
            for name, each in contents.get("anchors", {}).items()
        ]

    def maybe_in_subresource(segments, resolver, subresource):
        return resolver

    specification = Specification(
        name="counting",
        id_of=id_of,
        subresources_of=subresources_of,
        anchors_in=anchors_in,
        maybe_in_subresource=maybe_in_subresource,
    )
    return specification, expansions


def chain(specification):
    """
    A main chain plus an unrelated side branch.

    The chain (main -> mid -> target) shares no resource with the side
    branch (side -> sidechild), so an on-demand lookup of ``urn:target``
    must never expand the side branch.
    """

    main = specification.create_resource(
        {
            "ID": "urn:main",
            "children": [
                {
                    "ID": "urn:mid",
                    "children": [{"ID": "urn:target"}],
                },
            ],
        },
    )
    side = specification.create_resource(
        {
            "ID": "urn:side",
            "children": [{"ID": "urn:sidechild"}],
        },
    )
    return main, side


def uncrawled_uris(registry):
    return set(registry._uncrawled)


class TestIncrementalMaterialization:
    def test_combine_keeps_shared_resource_single_work_item(self):
        """
        Two registries which both know the same uncrawled resource combine
        it into one work item; its graph is not expanded twice.
        """

        specification, expansions = counting_specification()
        contents = {
            "ID": "urn:root",
            "anchors": {"rootAnchor": 1},
            "children": [
                {
                    "ID": "urn:child",
                    "anchors": {"childAnchor": 2},
                },
            ],
        }

        left = Registry().with_resource(
            "urn:root",
            specification.create_resource(contents),
        )
        right = Registry().with_resource(
            "urn:root",
            specification.create_resource(contents),
        )

        combined = left.combine(right)
        assert uncrawled_uris(combined) == {"urn:root"}

        crawled = combined.crawl()
        assert expansions.count("urn:root") == 1
        assert expansions.count("urn:child") == 1
        assert set(crawled) == {"urn:root", "urn:child"}
        assert (
            crawled.anchor("urn:root", "rootAnchor").value
            == Anchor(
                name="rootAnchor",
                resource=specification.create_resource(1),
            )
        )
        assert (
            crawled.anchor("urn:child", "childAnchor").value
            == Anchor(
                name="childAnchor",
                resource=specification.create_resource(2),
            )
        )
        print(f"shared resource crawl expansions: {expansions}")

    def test_combine_rejects_conflicting_retrieve_callables(self):
        """
        Combining registries with different non-default retrievers raises,
        and leaves the inputs untouched.
        """

        left = Registry(retrieve=lambda uri: Resource.opaque({"from": "left"}))
        right = Registry(retrieve=lambda uri: Resource.opaque({"from": "right"}))

        with pytest.raises(ValueError, match="conflict.*retriev"):
            left.combine(right)

        assert left.get_or_retrieve("urn:x").value.contents == {
            "from": "left",
        }
        assert right.get_or_retrieve("urn:x").value.contents == {
            "from": "right",
        }

    def test_no_id_subresource_is_transient(self):
        """
        A subresource without an id is not keyed in the registry but its
        anchors still become visible at the parent's URI once the parent is
        expanded.
        """

        specification, expansions = counting_specification()
        root = specification.create_resource(
            {
                "ID": "urn:root",
                "children": [{"anchors": {"inner": {"depth": 1}}}],
            },
        )
        registry = root @ Registry()

        retrieved = registry.get_or_retrieve("urn:root")
        # Expanding the root schedules (and immediately processes, to
        # register the anchor) the unkeyed child, but adds no URI key.
        assert set(retrieved.registry) == {"urn:root"}
        assert (
            retrieved.registry.anchor("urn:root", "inner").value
            == Anchor(
                name="inner",
                resource=specification.create_resource({"depth": 1}),
            )
        )
        assert expansions == ["urn:root", None]
        print(f"no-id subresource expansions: {expansions}")

    def test_relative_subresource_id_is_resolved_against_parent(self):
        root_contents = {
            "ID": "http://example.com/schemas/",
            "children": [
                {
                    "ID": "child.json",
                    "children": [{"ID": "deep.json"}],
                },
            ],
        }
        specification, expansions = counting_specification()
        root = specification.create_resource(root_contents)
        registry = root @ Registry()

        retrieved = registry.get_or_retrieve(
            "http://example.com/schemas/child.json",
        )
        assert retrieved.registry is not registry
        assert set(retrieved.registry) == {
            "http://example.com/schemas/",
            "http://example.com/schemas/child.json",
        }
        assert uncrawled_uris(retrieved.registry) == {
            "http://example.com/schemas/deep.json",
        }
        full = retrieved.registry.crawl()
        assert "http://example.com/schemas/deep.json" in full
        assert expansions == [
            "http://example.com/schemas/",
            "child.json",
            "deep.json",
        ]
        print(f"relative id expansions: {expansions}")

    def test_same_named_anchors_are_registered_per_resource_uri(self):
        specification, expansions = counting_specification()
        one = specification.create_resource(
            {"ID": "urn:one", "anchors": {"name": 1}},
        )
        two = specification.create_resource(
            {"ID": "urn:two", "anchors": {"name": 2}},
        )
        registry = [one, two] @ Registry()

        crawled = registry.crawl()
        assert expansions == ["urn:two", "urn:one"]
        assert crawled.anchor("urn:one", "name").value == Anchor(
            name="name",
            resource=specification.create_resource(1),
        )
        assert crawled.anchor("urn:two", "name").value == Anchor(
            name="name",
            resource=specification.create_resource(2),
        )
        print(f"same-named anchor expansions: {expansions}")

    def test_cyclic_graph_expands_each_resource_once(self):
        specification, expansions = counting_specification()
        a = specification.create_resource(
            {"ID": "urn:a", "children": [{"ID": "urn:b"}]},
        )
        b = specification.create_resource(
            {"ID": "urn:b", "children": [{"ID": "urn:a"}]},
        )
        registry = [a, b] @ Registry()

        crawled = registry.crawl()
        assert expansions == ["urn:b", "urn:a"]
        assert not uncrawled_uris(crawled)
        assert set(crawled) == {"urn:a", "urn:b"}
        # A second crawl is a no-op, a cycle never re-grows the queue.
        assert crawled.crawl() == crawled
        assert expansions == ["urn:b", "urn:a"]
        print(f"cyclic graph expansions: {expansions}")

    def test_shared_subresource_expands_once(self):
        specification, expansions = counting_specification()
        shared = {"ID": "urn:shared"}
        left = specification.create_resource(
            {"ID": "urn:left", "children": [shared]},
        )
        right = specification.create_resource(
            {"ID": "urn:right", "children": [shared]},
        )
        registry = [left, right] @ Registry()

        crawled = registry.crawl()
        assert expansions == ["urn:right", "urn:shared", "urn:left"]
        assert expansions.count("urn:shared") == 1
        assert set(crawled) == {"urn:left", "urn:right", "urn:shared"}
        print(f"shared subresource expansions: {expansions}")

    def test_remove_uncrawled_resource_drops_its_work(self):
        specification, expansions = counting_specification()
        keep = Resource.opaque(contents={})
        root = specification.create_resource(
            {
                "ID": "urn:root",
                "children": [{"ID": "urn:child"}],
            },
        )
        registry = Registry().with_resources(
            [("urn:keep", keep), ("urn:root", root)],
        )

        trimmed = registry.remove("urn:root")
        assert uncrawled_uris(trimmed) == {"urn:keep"}
        assert trimmed.crawl() == Registry({"urn:keep": keep})
        assert expansions == []
        print(f"remove-uncrawled expansions: {expansions}")

    def test_retrieved_resource_with_undeterminable_specification_keeps_cause(self):
        marker = object()

        def retrieve(uri):
            raise exceptions.CannotDetermineSpecification(contents=marker)

        registry = Registry(retrieve=retrieve)
        with pytest.raises(
            exceptions.CannotDetermineSpecification,
        ) as exc_info:
            registry.get_or_retrieve("urn:uhoh")
        assert exc_info.value.contents is marker
        # The registry is unchanged after the failure.
        assert not registry

        # A CannotDetermineSpecification error propagates unchanged (it is
        # not wrapped), through both the registry and a resolver.
        resolver = registry.resolver()
        with pytest.raises(
            exceptions.CannotDetermineSpecification,
        ) as resolved_info:
            resolver.lookup("urn:uhoh")
        assert resolved_info.value.contents is marker

    def test_arbitrary_retrieve_error_keeps_original_cause(self):
        original = RuntimeError("boom")

        def retrieve(uri):
            raise original

        registry = Registry(retrieve=retrieve)
        with pytest.raises(exceptions.Unretrievable) as info:
            registry.get_or_retrieve("urn:uhoh")
        assert info.value.__cause__ is original

        with pytest.raises(exceptions.Unresolvable) as resolved_info:
            registry.resolver().lookup("urn:uhoh")
        # The wrapping is preserved end to end but the original cause
        # remains reachable.
        assert resolved_info.value.__cause__.__cause__ is original

    def test_on_demand_lookup_is_not_a_full_crawl(self):
        """
        The central guarantee: looking up a deep target only expands the
        work needed to reach it. The side branch stays scheduled but
        unexpanded, until an explicit crawl drains it.
        """

        specification, expansions = counting_specification()
        main, side = chain(specification)
        registry = Registry().with_resources(
            [("urn:side", side), ("urn:main", main)],
        )

        retrieved = registry.get_or_retrieve("urn:target")
        assert expansions == ["urn:main", "urn:mid", "urn:target"]
        assert set(retrieved.registry) == {
            "urn:main",
            "urn:mid",
            "urn:target",
            "urn:side",
        }
        assert uncrawled_uris(retrieved.registry) == {"urn:side"}
        assert repr(registry) == "<Registry (2 uncrawled resources)>"
        assert (
            repr(retrieved.registry)
            == "<Registry (4 resources, 1 uncrawled)>"
        )

        full = retrieved.registry.crawl()
        assert expansions == [
            "urn:main",
            "urn:mid",
            "urn:target",
            "urn:side",
            "urn:sidechild",
        ]
        assert repr(full) == "<Registry (5 resources)>"
        print(f"on-demand then crawl expansions: {expansions}")

    def test_on_demand_lookup_calls_retrieve_only_after_known_work(self):
        """
        Looking up an unknown URI drains the known work queue exactly once
        and then invokes the retriever; the result is reused afterwards
        without a second retrieval, and does not force a re-crawl.
        """

        specification, expansions = counting_specification()
        main, side = chain(specification)
        calls = []
        remote = Resource.opaque(contents={"remote": True})

        def retrieve(uri):
            calls.append(uri)
            return remote

        registry = Registry(retrieve=retrieve).with_resources(
            [("urn:side", side), ("urn:main", main)],
        )

        first = registry.resolver().lookup("urn:remote")
        # The target is unknown, so every *known* resource is expanded once;
        # only then is the retriever consulted.
        assert expansions == [
            "urn:main",
            "urn:mid",
            "urn:target",
            "urn:side",
            "urn:sidechild",
        ]
        assert calls == ["urn:remote"]
        assert first.contents == {"remote": True}

        again = first.resolver.lookup("urn:remote")
        assert again.contents == {"remote": True}
        assert calls == ["urn:remote"]

        full = first.resolver._registry.crawl()
        assert repr(full) == "<Registry (6 resources)>"
        assert calls == ["urn:remote"]
        print(f"unknown-uri retrieve expansions: {expansions}")

    def test_retrieve_is_not_called_when_target_is_discovered_by_crawl(self):
        """
        A target reachable within a known resource is found without ever
        invoking the retriever.
        """

        specification, expansions = counting_specification()
        main, side = chain(specification)
        registry = Registry(retrieve=lambda uri: pytest.fail("retrieved!")).with_resources(
            [("urn:side", side), ("urn:main", main)],
        )
        resolved = registry.resolver().lookup("urn:target")
        assert resolved.contents == {"ID": "urn:target"}
        assert expansions == ["urn:main", "urn:mid", "urn:target"]
        assert uncrawled_uris(resolved.resolver._registry) == {"urn:side"}

    def test_retrieved_resource_is_added_uncrawled(self):
        """
        A retrieved resource is registered but is not itself crawled: its
        subresources are not discovered until an explicit crawl.
        """

        specification, expansions = counting_specification()
        remote = specification.create_resource(
            {"ID": "urn:remote", "children": [{"ID": "urn:rchild"}]},
        )
        registry = Registry(retrieve=lambda uri: remote)

        retrieved = registry.get_or_retrieve("urn:remote")
        assert retrieved.value == remote
        assert set(retrieved.registry) == {"urn:remote"}
        assert uncrawled_uris(retrieved.registry) == {"urn:remote"}
        assert "urn:rchild" not in retrieved.registry
        assert expansions == []

        full = retrieved.registry.crawl()
        assert "urn:rchild" in full
        assert expansions == ["urn:remote", "urn:rchild"]

    def test_remove_expanded_resource_clears_its_state(self):
        specification, expansions = counting_specification()
        root = specification.create_resource(
            {"ID": "urn:root", "children": [{"ID": "urn:child"}]},
        )
        registry = (root @ Registry()).crawl()
        assert set(registry._expanded) == {"urn:root", "urn:child"}

        trimmed = registry.remove("urn:child")
        assert "urn:child" not in trimmed
        assert "urn:child" not in trimmed._expanded
        assert "urn:child" not in uncrawled_uris(trimmed)

        # The root remains fully crawled and removing does not reschedule.
        assert trimmed.crawl() == trimmed
        assert expansions == ["urn:root", "urn:child"]

    def test_old_registry_is_not_mutated_by_materialization(self):
        specification, expansions = counting_specification()
        main, side = chain(specification)
        registry = Registry().with_resources(
            [("urn:side", side), ("urn:main", main)],
        )
        snapshot = repr(registry)
        snapshot_uncrawled = uncrawled_uris(registry)

        retrieved = registry.get_or_retrieve("urn:target")
        retrieved.registry.crawl()

        assert repr(registry) == snapshot
        assert uncrawled_uris(registry) == snapshot_uncrawled
        assert set(registry) == {"urn:main", "urn:side"}
        assert "urn:target" not in registry

    def test_work_queue_order_is_stable_lifo(self):
        specification, expansions = counting_specification()
        one = specification.create_resource(
            {"ID": "urn:a", "children": [{"ID": "urn:a-child"}]},
        )
        two = specification.create_resource(
            {"ID": "urn:b", "children": [{"ID": "urn:b-child"}]},
        )
        registry = [two, one] @ Registry()
        crawled = registry.crawl()
        assert expansions == ["urn:a", "urn:a-child", "urn:b", "urn:b-child"]
        assert set(crawled) == {
            "urn:a",
            "urn:b",
            "urn:a-child",
            "urn:b-child",
        }
