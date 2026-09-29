from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from enum import Enum
from typing import Any, ClassVar, Generic, Protocol, TypeVar
from urllib.parse import unquote, urldefrag, urljoin

from attrs import evolve, field
from rpds import HashTrieMap, HashTrieSet, List

from referencing import exceptions
from referencing._attrs import frozen
from referencing.typing import URI, Anchor as AnchorType, D, Mapping, Retrieve

EMPTY_UNCRAWLED: HashTrieSet[URI] = HashTrieSet()
EMPTY_PREVIOUS_RESOLVERS: List[URI] = List()


class _Unset(Enum):
    """
    What sillyness...
    """

    SENTINEL = 1


_UNSET = _Unset.SENTINEL


class _MaybeInSubresource(Protocol[D]):
    def __call__(
        self,
        segments: Sequence[int | str],
        resolver: Resolver[D],
        subresource: Resource[D],
    ) -> Resolver[D]: ...


def _detect_or_error(contents: D) -> Specification[D]:
    if not isinstance(contents, Mapping):
        raise exceptions.CannotDetermineSpecification(contents)

    jsonschema_dialect_id = contents.get("$schema")  # type: ignore[reportUnknownMemberType]
    if not isinstance(jsonschema_dialect_id, str):
        raise exceptions.CannotDetermineSpecification(contents)

    from referencing.jsonschema import specification_with

    return specification_with(jsonschema_dialect_id)


def _detect_or_default(
    default: Specification[D],
) -> Callable[[D], Specification[D]]:
    def _detect(contents: D) -> Specification[D]:
        if not isinstance(contents, Mapping):
            return default

        jsonschema_dialect_id = contents.get("$schema")  # type: ignore[reportUnknownMemberType]
        if jsonschema_dialect_id is None:
            return default

        from referencing.jsonschema import specification_with

        return specification_with(
            jsonschema_dialect_id,  # type: ignore[reportUnknownArgumentType]
            default=default,
        )

    return _detect


class _SpecificationDetector:
    def __get__(
        self,
        instance: Specification[D] | None,
        cls: type[Specification[D]],
    ) -> Callable[[D], Specification[D]]:
        if instance is None:
            return _detect_or_error
        else:
            return _detect_or_default(instance)


@frozen
class Specification(Generic[D]):
    """
    A specification which defines referencing behavior.

    The various methods of a `Specification` allow for varying referencing
    behavior across JSON Schema specification versions, etc.
    """

    #: A short human-readable name for the specification, used for debugging.
    name: str

    #: Find the ID of a given document.
    id_of: Callable[[D], URI | None]

    #: Retrieve the subresources of the given document (without traversing into
    #: the subresources themselves).
    subresources_of: Callable[[D], Iterable[D]]

    #: While resolving a JSON pointer, conditionally enter a subresource
    #: (if e.g. we have just entered a keyword whose value is a subresource)
    maybe_in_subresource: _MaybeInSubresource[D]

    #: Retrieve the anchors contained in the given document.
    _anchors_in: Callable[
        [Specification[D], D],
        Iterable[AnchorType[D]],
    ] = field(alias="anchors_in")

    #: An opaque specification where resources have no subresources
    #: nor internal identifiers.
    OPAQUE: ClassVar[Specification[Any]]

    #: Attempt to discern which specification applies to the given contents.
    #:
    #: May be called either as an instance method or as a class method, with
    #: slightly different behavior in the following case:
    #:
    #: Recall that not all contents contains enough internal information about
    #: which specification it is written for -- the JSON Schema ``{}``,
    #: for instance, is valid under many different dialects and may be
    #: interpreted as any one of them.
    #:
    #: When this method is used as an instance method (i.e. called on a
    #: specific specification), that specification is used as the default
    #: if the given contents are unidentifiable.
    #:
    #: On the other hand when called as a class method, an error is raised.
    #:
    #: To reiterate, ``DRAFT202012.detect({})`` will return ``DRAFT202012``
    #: whereas the class method ``Specification.detect({})`` will raise an
    #: error.
    #:
    #: (Note that of course ``DRAFT202012.detect(...)`` may return some other
    #: specification when given a schema which *does* identify as being for
    #: another version).
    #:
    #: Raises:
    #:
    #:     `CannotDetermineSpecification`
    #:
    #:         if the given contents don't have any discernible
    #:         information which could be used to guess which
    #:         specification they identify as
    detect = _SpecificationDetector()

    def __repr__(self) -> str:
        return f"<Specification name={self.name!r}>"

    def anchors_in(self, contents: D):
        """
        Retrieve the anchors contained in the given document.
        """
        return self._anchors_in(self, contents)

    def create_resource(self, contents: D) -> Resource[D]:
        """
        Create a resource which is interpreted using this specification.
        """
        return Resource(contents=contents, specification=self)


Specification.OPAQUE = Specification(
    name="opaque",
    id_of=lambda contents: None,
    subresources_of=lambda contents: [],
    anchors_in=lambda specification, contents: [],
    maybe_in_subresource=lambda segments, resolver, subresource: resolver,
)


@frozen
class Resource(Generic[D]):
    r"""
    A document (deserialized JSON) with a concrete interpretation under a spec.

    In other words, a Python object, along with an instance of `Specification`
    which describes how the document interacts with referencing -- both
    internally (how it refers to other `Resource`\ s) and externally (how it
    should be identified such that it is referenceable by other documents).
    """

    contents: D
    _specification: Specification[D] = field(alias="specification")

    @classmethod
    def from_contents(
        cls,
        contents: D,
        default_specification: (
            type[Specification[D]] | Specification[D]
        ) = Specification,
    ) -> Resource[D]:
        """
        Create a resource guessing which specification applies to the contents.

        Raises:

            `CannotDetermineSpecification`

                if the given contents don't have any discernible
                information which could be used to guess which
                specification they identify as

        """
        specification = default_specification.detect(contents)
        return specification.create_resource(contents=contents)

    @classmethod
    def opaque(cls, contents: D) -> Resource[D]:
        """
        Create an opaque `Resource` -- i.e. one with opaque specification.

        See `Specification.OPAQUE` for details.
        """
        return Specification.OPAQUE.create_resource(contents=contents)

    def id(self) -> URI | None:
        """
        Retrieve this resource's (specification-specific) identifier.
        """
        id = self._specification.id_of(self.contents)
        if id is None:
            return
        return id.rstrip("#")

    def subresources(self) -> Iterable[Resource[D]]:
        """
        Retrieve this resource's subresources.
        """
        return (
            Resource.from_contents(
                each,
                default_specification=self._specification,
            )
            for each in self._specification.subresources_of(self.contents)
        )

    def anchors(self) -> Iterable[AnchorType[D]]:
        """
        Retrieve this resource's (specification-specific) anchors.
        """
        return self._specification.anchors_in(self.contents)

    def pointer(self, pointer: str, resolver: Resolver[D]) -> Resolved[D]:
        """
        Resolve the given JSON pointer.

        Raises:

            `exceptions.PointerToNowhere`

                if the pointer points to a location not present in the document

        """
        if not pointer:
            return Resolved(contents=self.contents, resolver=resolver)

        contents = self.contents
        segments: list[int | str] = []
        for segment in unquote(pointer[1:]).split("/"):
            if isinstance(contents, Sequence):
                segment = int(segment)
            else:
                segment = segment.replace("~1", "/").replace("~0", "~")
            try:
                contents = contents[segment]  # type: ignore[reportUnknownArgumentType]
            except LookupError as lookup_error:
                error = exceptions.PointerToNowhere(ref=pointer, resource=self)
                raise error from lookup_error

            segments.append(segment)
            last = resolver
            resolver = self._specification.maybe_in_subresource(
                segments=segments,
                resolver=resolver,
                subresource=self._specification.create_resource(contents),
            )
            if resolver is not last:
                segments = []
        return Resolved(contents=contents, resolver=resolver)  # type: ignore[reportUnknownArgumentType]


def _fail_to_retrieve(uri: URI):
    raise exceptions.NoSuchResource(ref=uri)


@frozen
class Registry(Mapping[URI, Resource[D]]):
    r"""
    A registry of `Resource`\ s, each identified by their canonical URIs.

    Registries store a collection of in-memory resources, and optionally
    enable additional resources which may be stored elsewhere (e.g. in a
    database, a separate set of files, over the network, etc.).

    They also lazily walk their known resources, looking for subresources
    within them. In other words, subresources contained within any added
    resources will be retrievable via their own IDs (though this discovery of
    subresources will be delayed until necessary).

    All of that discovery is driven by one incremental *work queue*. Each
    work item is a ``(base_uri, resource)`` pair. The item is "keyed" when
    its resource has (or will have) a canonical URI, which is tracked as
    unexpanded in ``_uncrawled``; unkeyed subresources (those without an
    internal id) never become work items -- they are expanded the moment
    they are discovered, since their only effect is to register anchors at
    their parent's base URI.

    Work items are processed in a stable LIFO order, and a single resource
    object is expanded at most once: discovering a resource whose canonical
    URI has already been expanded or scheduled never enqueues it again,
    which keeps cyclic graphs and shared subresources from growing the
    queue.

    Registries are immutable, and their methods return new instances of the
    registry with the additional resources added to them.

    The ``retrieve`` argument can be used to configure retrieval of resources
    dynamically, either over the network, from a database, or the like.
    Pass it a callable which will be called if any URI not present in the
    registry is accessed. It must either return a `Resource` or else raise a
    `NoSuchResource` exception indicating that the resource does not exist
    even according to the retrieval logic.
    """

    _resources: HashTrieMap[URI, Resource[D]] = field(
        default=HashTrieMap(),
        converter=HashTrieMap.convert,  # type: ignore[reportGeneralTypeIssues]
        alias="resources",
    )
    _anchors: HashTrieMap[tuple[URI, str], AnchorType[D]] = HashTrieMap()
    _uncrawled: HashTrieSet[URI] = EMPTY_UNCRAWLED
    _expanded: HashTrieSet[URI] = HashTrieSet()
    _to_crawl: List[tuple[URI, Resource[D]]] = field(
        default=List(),
        eq=False,
        repr=False,
    )
    _retrieve: Retrieve[D] = field(default=_fail_to_retrieve, alias="retrieve")

    def __getitem__(self, uri: URI) -> Resource[D]:
        """
        Return the (already crawled) `Resource` identified by the given URI.
        """
        try:
            return self._resources[uri.rstrip("#")]
        except KeyError:
            raise exceptions.NoSuchResource(ref=uri) from None

    def __iter__(self) -> Iterator[URI]:
        """
        Iterate over all crawled URIs in the registry.
        """
        return iter(self._resources)

    def __len__(self) -> int:
        """
        Count the total number of fully crawled resources in this registry.
        """
        return len(self._resources)

    def __rmatmul__(
        self,
        new: Resource[D] | Iterable[Resource[D]],
    ) -> Registry[D]:
        """
        Create a new registry with resource(s) added using their internal IDs.

        Resources must have a internal IDs (e.g. the :kw:`$id` keyword in
        modern JSON Schema versions), otherwise an error will be raised.

        Both a single resource as well as an iterable of resources works, i.e.:

            * ``resource @ registry`` or

            * ``[iterable, of, multiple, resources] @ registry``

        which -- again, assuming the resources have internal IDs -- is
        equivalent to calling `Registry.with_resources` as such:

        .. code:: python

            registry.with_resources(
                (resource.id(), resource) for resource in new_resources
            )

        Raises:

            `NoInternalID`

                if the resource(s) in fact do not have IDs

        """
        if isinstance(new, Resource):
            new = (new,)

        pairs = []
        for resource in new:
            id = resource.id()
            if id is None:
                raise exceptions.NoInternalID(resource=resource)
            pairs.append((id, resource))
        return self.with_resources(pairs)

    def __repr__(self) -> str:
        size = len(self)
        pluralized = "resource" if size == 1 else "resources"
        if self._uncrawled:
            uncrawled = len(self._uncrawled)
            if uncrawled == size:
                summary = f"uncrawled {pluralized}"
            else:
                summary = f"{pluralized}, {uncrawled} uncrawled"
        else:
            summary = f"{pluralized}"
        return f"<Registry ({size} {summary})>"

    def get_or_retrieve(self, uri: URI) -> Retrieved[D, Resource[D]]:
        """
        Get a resource from the registry, crawling or retrieving if necessary.

        Crawling is incremental: only the work strictly necessary to find the
        given URI is materialized. If crawling the known resources doesn't
        find it, the retrieval callable is invoked once, and its result is
        added to the returned registry (without being crawled itself).
        """
        if uri in self._expanded:
            return Retrieved(registry=self, value=self._resources[uri])

        registry = self._advance(target=uri)
        if uri in registry._expanded:
            return Retrieved(registry=registry, value=registry._resources[uri])
        resource = registry._resources.get(uri)
        if resource is not None:
            return Retrieved(registry=registry, value=resource)

        try:
            resource = registry._retrieve(uri)
        except (
            exceptions.CannotDetermineSpecification,
            exceptions.NoSuchResource,
        ):
            raise
        except Exception as error:
            raise exceptions.Unretrievable(ref=uri) from error
        else:
            registry = registry.with_resource(uri, resource)
            return Retrieved(registry=registry, value=resource)

    def remove(self, uri: URI):
        """
        Return a registry with the resource identified by a given URI removed.
        """
        if uri not in self._resources:
            raise exceptions.NoSuchResource(ref=uri)

        remaining = List()
        for work in self._to_crawl:
            if work[0] != uri:
                remaining = remaining.push_front(work)
        return evolve(
            self,
            resources=self._resources.remove(uri),
            uncrawled=self._uncrawled.discard(uri),
            expanded=self._expanded.discard(uri),
            to_crawl=remaining,
            anchors=HashTrieMap(
                (k, v) for k, v in self._anchors.items() if k[0] != uri
            ),
        )

    def anchor(self, uri: URI, name: str):
        """
        Retrieve a given anchor from a resource which must already be crawled.

        Anchors may live in an as-yet undiscovered subresource (including
        one without an internal id, resolved against ``uri``), so the full
        work queue is drained before deciding the anchor is absent.
        """
        value = self._anchors.get((uri, name))
        if value is not None:
            return Retrieved(value=value, registry=self)

        registry = self._advance(target=(uri, name))
        value = registry._anchors.get((uri, name))
        if value is not None:
            return Retrieved(value=value, registry=registry)

        resource = registry[uri]
        canonical_uri = resource.id()
        if canonical_uri is not None:
            value = registry._anchors.get((canonical_uri, name))
            if value is not None:
                return Retrieved(value=value, registry=registry)

        if "/" in name:
            raise exceptions.InvalidAnchor(
                ref=uri,
                resource=resource,
                anchor=name,
            )
        raise exceptions.NoSuchAnchor(ref=uri, resource=resource, anchor=name)

    def contents(self, uri: URI) -> D:
        """
        Retrieve the (already crawled) contents identified by the given URI.
        """
        return self[uri].contents

    def crawl(self) -> Registry[D]:
        """
        Crawl all added resources, discovering subresources.
        """
        return self._advance(target=None)

    def with_resource(self, uri: URI, resource: Resource[D]):
        """
        Add the given `Resource` to the registry, without crawling it.
        """
        return self.with_resources([(uri, resource)])

    def with_resources(
        self,
        pairs: Iterable[tuple[URI, Resource[D]]],
    ) -> Registry[D]:
        r"""
        Add the given `Resource`\ s to the registry, without crawling them.
        """
        resources = self._resources
        uncrawled = self._uncrawled
        to_crawl = self._to_crawl
        for uri, resource in pairs:
            # Empty fragment URIs are equivalent to URIs without the fragment.
            # TODO: Is this true for non JSON Schema resources? Probably not.
            uri = uri.rstrip("#")
            resources = resources.insert(uri, resource)
            if uri not in uncrawled:
                uncrawled = uncrawled.insert(uri)
                to_crawl = to_crawl.push_front((uri, resource))
        return evolve(
            self,
            resources=resources,
            uncrawled=uncrawled,
            to_crawl=to_crawl,
        )

    def with_contents(
        self,
        pairs: Iterable[tuple[URI, D]],
        **kwargs: Any,
    ) -> Registry[D]:
        r"""
        Add the given contents to the registry, autodetecting when necessary.
        """
        return self.with_resources(
            (uri, Resource.from_contents(each, **kwargs))
            for uri, each in pairs
        )

    def combine(self, *registries: Registry[D]) -> Registry[D]:
        """
        Combine together one or more other registries, producing a unified one.
        """
        if registries == (self,):
            return self
        resources = self._resources
        anchors = self._anchors
        uncrawled = self._uncrawled
        expanded = self._expanded
        retrieve = self._retrieve
        for registry in registries:
            resources = resources.update(registry._resources)
            anchors = anchors.update(registry._anchors)
            uncrawled = uncrawled.update(registry._uncrawled)
            expanded = expanded.update(registry._expanded)

            if registry._retrieve is not _fail_to_retrieve:
                if registry._retrieve is not retrieve is not _fail_to_retrieve:
                    raise ValueError(  # noqa: TRY003
                        "Cannot combine registries with conflicting retrieval "
                        "functions.",
                    )
                retrieve = registry._retrieve

        # Rebuild a single work queue for the combined ``uncrawled`` set.
        # Each still-to-expand canonical URI is enqueued once. Input work
        # items may remember a discovered resource under its (relative)
        # parent base, so the canonical URI is recomputed from the item's
        # own resource rather than assumed to equal its queued base.
        to_crawl = List()
        enqueued = set()
        for registry in (self, *registries):
            for base_uri, resource in registry._to_crawl:
                id = resource.id()
                canonical = urljoin(base_uri, id) if id is not None else base_uri
                if canonical in uncrawled and canonical not in enqueued:
                    enqueued.add(canonical)
                    to_crawl = to_crawl.push_front((canonical, resource))
        return evolve(
            self,
            anchors=anchors,
            resources=resources,
            uncrawled=uncrawled,
            expanded=expanded,
            to_crawl=to_crawl,
            retrieve=retrieve,
        )

    def resolver(self, base_uri: URI = "") -> Resolver[D]:
        """
        Return a `Resolver` which resolves references against this registry.
        """
        return Resolver(base_uri=base_uri, registry=self)

    def resolver_with_root(self, resource: Resource[D]) -> Resolver[D]:
        """
        Return a `Resolver` with a specific root resource.
        """
        uri = resource.id() or ""
        return Resolver(
            base_uri=uri,
            registry=self.with_resource(uri, resource),
        )

    def _advance(self, target):
        """
        Process the work queue incrementally until ``target`` is found.

        ``target`` is a resource URI (lookup succeeds once it has been
        expanded, leaving unrelated work queued). Any other non-`None`
        value (used for anchor lookups), like `None` (an explicit crawl),
        drains the whole queue.

        Every entry point shares the same expansion procedure: find the
        resource's canonical URI, register its anchors at that URI, and
        schedule each subresource once (relative ids resolved against the
        parent's canonical URI).
        """
        resources = self._resources
        anchors = self._anchors
        uncrawled = self._uncrawled
        expanded = self._expanded
        to_crawl = self._to_crawl

        while to_crawl:
            if isinstance(target, str) and target in expanded:
                # On-demand resource lookup: stop the moment the requested
                # resource has been expanded, leaving unrelated work queued.
                break

            base_uri, resource = to_crawl.first
            to_crawl = to_crawl.rest
            # A discovered item may remember its parent base even though
            # it is scheduled under its own resolved URI; clear the pending
            # mark for the canonical URI before expanding.
            queued_id = resource.id()
            pending = (
                urljoin(base_uri, queued_id)
                if queued_id is not None
                else base_uri
            )
            uncrawled = uncrawled.discard(pending)

            (
                resources,
                anchors,
                uncrawled,
                expanded,
                to_crawl,
            ) = self._expand(
                base_uri=base_uri,
                resource=resource,
                resources=resources,
                anchors=anchors,
                uncrawled=uncrawled,
                expanded=expanded,
                to_crawl=to_crawl,
            )
        else:
            uncrawled = EMPTY_UNCRAWLED

        return evolve(
            self,
            resources=resources,
            anchors=anchors,
            uncrawled=uncrawled,
            expanded=expanded,
            to_crawl=to_crawl,
        )

    @classmethod
    def _expand(
        cls,
        base_uri,
        resource,
        resources,
        anchors,
        uncrawled,
        expanded,
        to_crawl,
    ):
        """
        Expand one resource once.

        Register its canonical URI (if any) and its anchors, then discover
        its direct subresources. Unkeyed subresources are expanded right
        away (they only contribute anchors at the parent's base URI); keyed
        subresources are scheduled for later in stable LIFO order.
        """
        canonical = base_uri
        id = resource.id()
        if id is not None:
            canonical = urljoin(base_uri, id)
            resources = resources.insert(canonical, resource)
            uncrawled = uncrawled.discard(canonical)
            expanded = expanded.insert(canonical)
        for each in resource.anchors():
            anchors = anchors.insert((canonical, each.name), each)
        for child in resource.subresources():
            (
                resources,
                anchors,
                uncrawled,
                expanded,
                to_crawl,
            ) = cls._schedule(
                base_uri=canonical,
                resource=child,
                resources=resources,
                anchors=anchors,
                uncrawled=uncrawled,
                expanded=expanded,
                to_crawl=to_crawl,
            )
        return resources, anchors, uncrawled, expanded, to_crawl

    @classmethod
    def _schedule(
        cls,
        base_uri,
        resource,
        resources,
        anchors,
        uncrawled,
        expanded,
        to_crawl,
    ):
        """
        Discover one subresource and either expand it or enqueue it once.
        """
        id = resource.id()
        if id is None:
            # No canonical URI: only useful for anchors at the parent's
            # base URI, so expand it immediately without touching the map.
            return cls._expand(
                base_uri=base_uri,
                resource=resource,
                resources=resources,
                anchors=anchors,
                uncrawled=uncrawled,
                expanded=expanded,
                to_crawl=to_crawl,
            )
        uri = urljoin(base_uri, id)
        # Already expanded, or already scheduled-but-unexpanded: cycles and
        # shared subresources never grow the queue a second time.
        if uri in expanded or uri in uncrawled:
            return resources, anchors, uncrawled, expanded, to_crawl
        # Remember the child under its resolved URI so it is not scheduled
        # twice, but queue it with the *parent* base it must be resolved
        # against (its own id may be relative). It is keyed into the
        # resource map only when it is later expanded.
        uncrawled = uncrawled.insert(uri)
        to_crawl = to_crawl.push_front((base_uri, resource))
        return resources, anchors, uncrawled, expanded, to_crawl



#: An anchor or resource.
AnchorOrResource = TypeVar(
    "AnchorOrResource",
    AnchorType[Any],
    Resource[Any],
    default=Resource[Any],
)


@frozen
class Retrieved(Generic[D, AnchorOrResource]):
    """
    A value retrieved from a `Registry`.
    """

    value: AnchorOrResource
    registry: Registry[D]


@frozen
class Resolved(Generic[D]):
    """
    A reference resolved to its contents by a `Resolver`.
    """

    contents: D
    resolver: Resolver[D]


@frozen
class Resolver(Generic[D]):
    """
    A reference resolver.

    Resolvers help resolve references (including relative ones) by
    pairing a fixed base URI with a `Registry`.

    This object, under normal circumstances, is expected to be used by
    *implementers of libraries* built on top of `referencing` (e.g. JSON Schema
    implementations or other libraries resolving JSON references),
    not directly by end-users populating registries or while writing
    schemas or other resources.

    References are resolved against the base URI, and the combined URI
    is then looked up within the registry.

    The process of resolving a reference may itself involve calculating
    a *new* base URI for future reference resolution (e.g. if an
    intermediate resource sets a new base URI), or may involve encountering
    additional subresources and adding them to a new registry.
    """

    _base_uri: URI = field(alias="base_uri")
    _registry: Registry[D] = field(alias="registry")
    _previous: List[URI] = field(default=List(), repr=False, alias="previous")

    def lookup(self, ref: URI) -> Resolved[D]:
        """
        Resolve the given reference to the resource it points to.

        Raises:

            `exceptions.Unresolvable`

                or a subclass thereof (see below) if the reference isn't
                resolvable

            `exceptions.NoSuchAnchor`

                if the reference is to a URI where a resource exists but
                contains a plain name fragment which does not exist within
                the resource

            `exceptions.PointerToNowhere`

                if the reference is to a URI where a resource exists but
                contains a JSON pointer to a location within the resource
                that does not exist

        """
        if ref.startswith("#"):
            uri, fragment = self._base_uri, ref[1:]
        else:
            uri, fragment = urldefrag(urljoin(self._base_uri, ref))
        try:
            retrieved = self._registry.get_or_retrieve(uri)
        except exceptions.NoSuchResource:
            raise exceptions.Unresolvable(ref=ref) from None
        except exceptions.Unretrievable as error:
            raise exceptions.Unresolvable(ref=ref) from error

        if fragment.startswith("/"):
            resolver = self._evolve(registry=retrieved.registry, base_uri=uri)
            return retrieved.value.pointer(pointer=fragment, resolver=resolver)

        if fragment:
            retrieved = retrieved.registry.anchor(uri, fragment)
            resolver = self._evolve(registry=retrieved.registry, base_uri=uri)
            return retrieved.value.resolve(resolver=resolver)

        resolver = self._evolve(registry=retrieved.registry, base_uri=uri)
        return Resolved(contents=retrieved.value.contents, resolver=resolver)

    def in_subresource(self, subresource: Resource[D]) -> Resolver[D]:
        """
        Create a resolver for a subresource (which may have a new base URI).
        """
        id = subresource.id()
        if id is None:
            return self
        return evolve(self, base_uri=urljoin(self._base_uri, id))

    def dynamic_scope(self) -> Iterable[tuple[URI, Registry[D]]]:
        """
        In specs with such a notion, return the URIs in the dynamic scope.
        """
        for uri in self._previous:
            yield uri, self._registry

    def _evolve(self, base_uri: URI, **kwargs: Any):
        """
        Evolve, appending to the dynamic scope.
        """
        previous = self._previous
        if self._base_uri and (not previous or base_uri != self._base_uri):
            previous = previous.push_front(self._base_uri)
        return evolve(self, base_uri=base_uri, previous=previous, **kwargs)


@frozen
class Anchor(Generic[D]):
    """
    A simple anchor in a `Resource`.
    """

    name: str
    resource: Resource[D]

    def resolve(self, resolver: Resolver[D]):
        """
        Return the resource for this anchor.
        """
        return Resolved(contents=self.resource.contents, resolver=resolver)
