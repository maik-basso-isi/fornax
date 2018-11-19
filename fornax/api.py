"""
Fornax API documentation
========================

Introduction
------------

Fornax performs fuzzy subgraph matching between graphs with labelled nodes.
Given a small graph (the query graph) and a large graph (the target graph)
fornax will approximate the top `n` subgraphs in the target graph that are most
similar to the query graph even if the node labels and graph relationships are
not exactly the same.

Use this query API to specify query and target graphs and to seach for fuzzy
subgraph matches of the query graph to the target graph.

fornax is designed to handle very large graphs of millions of nodes.
As such graphs are persisted in a database.
Rather than interacting directly with a graph, the API implements GraphHandles.
These are similar to file handles or file pointers for a file system.
They allow the user to Create, Read, Update and Delete graphs but much like a
file the graphs will still persist even if the handle goes out of scope.

Similarly query objects, which define a search operation, can be created using
a QueryHandle.
"""
import fornax.select
import fornax.opt
import sqlalchemy
import contextlib
import itertools
import collections
import json
import os
import sys
import hashlib

import typing
from sqlalchemy import event
from sqlalchemy.engine import Engine
import fornax.model as model

# TODO: sqlalchemy database integrity exceptions are not caught by the API

"""URL for a supported SQL database backend"""
DB_URL = os.environ.get('FORNAX_DB_URL')
if DB_URL is None:
    DB_URL = 'sqlite://'

MAX_SIZE = sys.maxsize
SQLITE_MAX_SIZE = 2147483647
if DB_URL == 'sqlite://':
    MAX_SIZE = min(MAX_SIZE, SQLITE_MAX_SIZE)

ECHO = False
ENGINE = sqlalchemy.create_engine(DB_URL, echo=ECHO)
CONNECTION = ENGINE.connect()
Session = sqlalchemy.orm.sessionmaker(bind=ENGINE)
fornax.model.Base.metadata.create_all(CONNECTION)


def _hash(item: str) -> int:
    """An unsalted hash function with a range between 0 and MAX_SIZE

    :param item: string or string like object that is accepted by builtin
    function `str`
    :type item: str
    :return: hash between 0 and MAX_SIZE
    :rtype: int
    """

    if isinstance(item, int):
        return item % MAX_SIZE
    else:
        return int(
            hashlib.sha256(str(item).encode('utf-8')).hexdigest(), 16
        ) % MAX_SIZE


# enforce foreign key constrains in SQLite
@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@contextlib.contextmanager
def session_scope():
    """
    Provide a transactional scope around a series of db operations.
    Transactions will be rolled back in the case of an exception.
    """

    session = Session()
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()


class InvalidNodeError(Exception):

    def __init__(self, message: str):
        """This exception will be raised if invalid Nodes are found to be inserted
        into the database

        :param message: Description of the failed criteria
        :type message: str
        """
        super().__init__(message)


def check_nodes(
    nodes: typing.Iterable[model.Node]
) -> typing.Generator[model.Node, None, None]:
    """Guard against invalid nodes by raising an InvalidNodeError for
    forbidden node parameters

    :param nodes: An iterable of Nodes
    :type nodes: typing.Iterable[model.Node]
    :raises InvalidNodeError: Raised when Node.node_id is not an integer
    :raises InvalidNodeError: Raised when Node.node_id is larger than MAX_INT
    :return: Yield each node if there are no uncaught exceptions
    :rtype: typing.Generator[model.Node, None, None]
    """

    for node in nodes:
        try:
            node_id = int(node.node_id)
        except ValueError:
            raise InvalidNodeError(
                '{}, node_id must be an integer'.format(node)
            )
        if node_id > SQLITE_MAX_SIZE and DB_URL == 'sqlite://':
            raise InvalidNodeError('node id {} is too large'.format(node))
        yield node


class InvalidEdgeError(Exception):

    def __init__(self, message: str):
        """This exception will be raised if invalid Edges are found to be inserted
        into the database

        :param message: Description of the failed criteria
        :type message: str
        """
        super().__init__(message)


def check_edges(
    edges: typing.Iterable[model.Edge]
) -> typing.Generator[model.Edge, None, None]:
    """Guard against invalid edges by raising an InvalidEdgeError for
    forbidden edge parameters

    :param edges: An iterable of Edges
    :type edges: typing.List[model.Edge]
    :raises InvalidEdgeError: Raised if edge start or edge end is not an int
    :raises InvalidEdgeError: Raised if edge start and edge end are the same
    :return: Yield each edge if there are no uncaught exceptions
    :rtype: typing.Generator[model.Edge, None, None]
    """

    for edge in edges:
        try:
            start, end = int(edge.start), int(edge.end)
        except ValueError:
            raise InvalidEdgeError(
                '{}, edge start and end must be integers'.format(edge)
            )
        if start == end:
            raise InvalidEdgeError(
                '{}, edges must start and end on different nodes'.format(edge)
            )
        yield edge


class InvalidMatchError(Exception):

    def __init__(self, message: str):
        """This exception will be raised if invalid Matches are found to be inserted
        into the database

        :param message: Description of the failed criteria
        :type message: str
        """
        super().__init__(message)


def check_matches(
    matches: typing.Iterable[model.Match]
) -> typing.Generator[model.Match, None, None]:
    """Guard against invalid matches by raising an InvalidMatchError
    for forbidden Match parameters

    :param matches: Iterable of Match objects
    :type matches: typing.Iterable[model.Match]
    :raises ValueError: Raised if match start cannot be coorced to an integer
    :raises ValueError: Raised if match end cannot be coorced to an integer
    :raises ValueError: Raised if match weight cannot be coorced to a float
    :raises ValueError: Raised if match weight is not in the range 0 to 1
    :return: yield each match
    :rtype: typing.Generator[model.Match, None, None]
    """

    for match in matches:
        try:
            start = int(match.start)
        except ValueError:
            raise ValueError(
                '<Match(start={}, end={}, weight={})>, match start must be int'
            )
        try:
            end = int(match.end)
        except ValueError:
            raise ValueError(
                '<Match(start={}, end={}, weight={})>, match end must be int'
            )
        try:
            weight = float(match.weight)
        except ValueError:
            raise ValueError(
                '<Match(start={}, end={}, weight={})>,\
                 match weight must be number'
            )
        if not 0 < weight <= 1:
            raise ValueError(
                '<Match(start={}, end={}, weight={})>,\
                 bounds error: 0 < weight <= 1'
                )
        yield match


class NullValue:
    """
    A dummy nul value that will cause an exception when serialised to json
    """

    def __init__(self):
        pass


class Node:
    """Representation of a Node use internally by QueryHandle

    :param node_id: unique id of a node
    :type node_id: int
    :param node_type: either `source` or `target`
    :type node_type: str
    :param meta: meta data to attach to a node to be json serialised
    :type meta: dict
    :raises ValueError: Raised is type is not either `source` or `target`
    """

    __slots__ = ['id', 'type', 'meta']

    def __init__(self, node_id: int, node_type: str,  meta: dict):
        if node_type not in ('query', 'target'):
            raise ValueError('Nodes must be of type "query", "target"')
        self.id = node_id
        self.type = node_type
        self.meta = meta

    def __eq__(self, other):
        return (self.id, self.type, self.meta) == (
            other.id, other.type, other.meta)

    def __repr__(self):
        return '<Node(id={}, type={}, meta={})>'.format(
            self.id, self.type, self.meta)

    def __lt__(self, other):
        return (self.type, self.id) < (other.type, other.id)

    def to_dict(self) -> dict:
        """Return self as a json serialisable dictionary

        :return: dictionary with keys `id`, `type` and `meta`
        :rtype: dict
        """

        return {
            # hash id with type so that the node id is unique to a given
            # submatch result
            **{
                'id': _hash((self.id, self.type)),
                'type': self.type
            },
            **self.meta
        }


class Edge:
    """Representation of an Edge used internally be QueryHandle

    :param start: id of start node
    :type start: int
    :param end: id of end node
    :type end: int
    :param edge_type: either query target or match
    :type edge_type: str
    :param meta: dictionary of edge metadata to be json serialised
    :type meta: dict
    :param weight: weight between 0 and 1, defaults to 1.
    :raises ValueError: Raised if type is not `query`, `target` or `match`
    """
    __slots__ = ['start', 'end', 'type', 'meta', 'weight']

    def __init__(
        self, start: int, end: int,
        edge_type: str, meta: dict, weight=1.
    ):
        if edge_type not in ('query', 'target', 'match'):
            raise ValueError(
                'Edges must be of type "query", "target", "match"'
            )
        self.start = start
        self.end = end
        self.type = edge_type
        self.meta = meta
        self.weight = weight

    def __eq__(self, other):
        return (self.type, self.start, self.end, self.meta) == (
            other.type, other.start, other.end, other.meta)

    def __lt__(self, other):
        return (self.type, self.start, self.end) < (
            other.type, other.start, other.end)

    def __repr__(self):
        return '<Edge(start={}, end={}, type={}, meta={})>'.format(
            self.start, self.end, self.type, self.meta
        )

    def to_dict(self):
        """Return self as a json serialisable dictionary

        Returns:
            dict -- dictionart with keys start, end, type, metadata and weight
        """
        if self.type == 'query' or self.type == 'target':
            # hash start and end with the edge type
            # to make id unique within a subgraph match
            start = _hash((self.start, self.type))
            end = _hash((self.end, self.type))

        elif self.type == 'match':
            # hash start and end with the edge type
            # to make id unique within a subgraph match
            start = _hash((self.start, 'query'))
            end = _hash((self.end, 'target'))
        return {
            **{
                'source': start,
                'target': end,
                'type': self.type,
                'weight': self.weight
            },
            **self.meta
        }


class GraphHandle:
    """Accessor for a graph

    Because fornax is designed to operate on very large graphs node and edges
    are not stored in memory.
    Rather, they are persisted using a database back end.
    Currently sqlite and postgres are supported.

    GraphHandle is an interface to this persistent layer.
    One can access an existing graph by
    specifying it using the `graph_id` itentifier.

    :param graph_id: unique id for an existing graph
    :type graph_id: int
    """

    def __init__(self, graph_id: int):
        self._graph_id = graph_id
        self._check_exists()

    def __len__(self):
        """Return the number of nodes in the graph

        :return: node count
        :rtype: int
        """
        with session_scope() as session:
            count = session.query(model.Node).filter(
                model.Node.graph_id == self._graph_id
            ).count()
        return count

    def __repr__(self):
        return '<GraphHandle(graph_id={})>'.format(self._graph_id)

    def __eq__(self, other):
        return self.graph_id == other.graph_id

    @property
    def graph_id(self):
        """Unique identifier for a graph"""
        return self._graph_id

    @classmethod
    def create(cls):
        """Create a new empy graph and return a GraphHandle to it

        :return: GraphHandle to a new graph
        :rtype: GraphHandle
        """

        with session_scope() as session:

            query = session.query(
                sqlalchemy.func.max(model.Graph.graph_id)
            ).first()
            graph_id = query[0]

            if graph_id is None:
                graph_id = 0
            else:
                graph_id += 1
            session.add(model.Graph(graph_id=graph_id))
            session.commit()
        return GraphHandle(graph_id)

    @classmethod
    def read(cls, graph_id: int):
        """Create a new GraphHandle to an existing graph
        with unique identifier `graph_id`

        :param graph_id: unique identifier for an existing graph
        :type graph_id: int
        :return: A new graph handle to an existing graph
        :rtype: GraphHandle
        """

        return GraphHandle(graph_id)

    def delete(self):
        """Delete a graph.

        Delete the graph accessed through graph handle and
        all of the associated nodes and edges.

        """

        self._check_exists()
        with session_scope() as session:
            session.query(
                model.Graph
            ).filter(model.Graph.graph_id == self._graph_id).delete()
            session.query(
                model.Edge
            ).filter(model.Edge.graph_id == self._graph_id).delete()
            session.query(
                model.Node
            ).filter(model.Node.graph_id == self._graph_id).delete()

    def _check_exists(self):
        with session_scope() as session:
            exists = session.query(sqlalchemy.exists().where(
                model.Graph.graph_id == self._graph_id
            )).scalar()
        if not exists:
            raise ValueError(
                'cannot read graph with graph id: {}'.format(self._graph_id)
            )

    def add_nodes(self, **kwargs):
        """Append nodes to a graph

        :param id_src: An iterable if Unique hashable identifiers
        for each node, defaults to None
        :raises ValueError: Raised if `id` is used as a keyword argument
        :raises ValueError: Raised if no keyword arguments are provided

        If `id_src` is not provided,
        each node will be indentifed by order of insertion
        using a continuous range index starting at zero.

        Metadata can be attached to each node
        by specifying extra keyword arguments
        (not that id is reserved).
        For example, to attach a name to each node:

        :Example:

        graph_handle.add_node(id_src=[1,2,3], name=['a', 'b', 'c'])

        """

        keys = kwargs.keys()

        if not len(keys):
            raise ValueError(
                'add_nodes requires at least one keyword argument'
            )

        if 'id' in keys:
            raise(ValueError('id is a reserved node attribute \
            which cannot be assigned'))
        if kwargs.get('id_src') is not None:
            id_src = kwargs['id_src']
            zipped = itertools.zip_longest(
                *kwargs.values(), fillvalue=NullValue()
            )
            zipped = itertools.zip_longest(
                id_src, zipped, fillvalue=NullValue()
            )
        else:
            zipped = enumerate(
                itertools.zip_longest(*kwargs.values(), fillvalue=NullValue())
            )

        nodes = (
            model.Node(
                node_id=_hash(node_id),
                graph_id=self.graph_id,
                meta=json.dumps({key: val for key, val in zip(keys, values)})
            )
            for node_id, values in zipped
        )
        nodes = check_nodes(nodes)
        with session_scope() as session:
            session.add_all(nodes)
            session.commit()

    def add_edges(
        self, sources: typing.Iterable, targets: typing.Iterable, **kwargs
    ):
        """Append edges to a graph representing relationships between nodes

        :param sources: node `id_src`
        :type sources: typing.Iterable
        :param targets: node `id_src`
        :type targets: typing.Iterable

        keyword arguments can be used to attach metadata to the edges.

        :Example:

        graph_handle.add_edges([0, 0], [1, 1], relation=['friend', 'foe'])

        """

        keys = kwargs.keys()
        if 'start' in keys:
            raise(
                ValueError('start is a reserved node attribute \
                which cannot be assigned using kwargs'))
        if 'end' in keys:
            raise(ValueError('end is a reserved node attribute \
            which cannot be assigned using kwargs'))
        if 'type' in keys:
            raise(ValueError('type is a reserved node attribute \
            which cannot be assigned using kwargs'))
        if 'weight' in keys:
            raise(ValueError('weight is a reserved node attribute \
            which cannot be assigned using kwargs'))
        hashed_sources = map(_hash, sources)
        hashed_targets = map(_hash, targets)
        zipped = itertools.zip_longest(
            hashed_sources, hashed_targets,
            *kwargs.values(), fillvalue=NullValue()
        )
        edges = itertools.chain.from_iterable(
            (
                model.Edge(
                    start=start, end=end, graph_id=self._graph_id,
                    meta=json.dumps(
                        {key: val for key, val in zip(keys, values)}
                    )),
                model.Edge(
                    start=end, end=start, graph_id=self._graph_id,
                    meta=json.dumps(
                        {key: val for key, val in zip(keys, values)}
                    ))
            )
            for start, end, *values in zipped
        )
        edges = check_edges(edges)
        with session_scope() as session:
            session.add_all(edges)
            session.commit()


class QueryHandle:
    """Accessor for a fuzzy subgraph matching query

    :param query_id: unique id for an existing query
    :type query_id: int
    """

    def __init__(self, query_id: int):
        self.query_id = query_id
        self._check_exists()

    def __eq__(self, other):
        return self.query_id == other.query_id

    def __len__(self):
        """Return the number of matches in the query

        Returns:
            {int} -- Count of matching edges
        """

        self._check_exists()
        with session_scope() as session:
            count = session.query(model.Match).filter(
                model.Match.query_id == self.query_id).count()
        return count

    def _check_exists(self):
        """Raise a value error is the query had been deleted

        Raises:
            ValueError -- Raised if the query had been deleted
        """

        with session_scope() as session:
            exists = session.query(model.Query).filter(
                model.Query.query_id == self.query_id
            ).scalar()
        if not exists:
            raise ValueError(
                'cannot read query with query id {}'.format(self.query_id)
            )

    @classmethod
    def create(cls, query_graph: GraphHandle, target_graph: GraphHandle):
        """Create a new query and return a QueryHandle for it

        :param query_graph: Subgraph to be search for in the target graph
        :type query_graph: GraphHandle
        :param target_graph: Graph to be searched
        :type target_graph: GraphHandle
        :return: new QueryHandle
        :rtype: QueryHandle
        """

        with session_scope() as session:
            query_id = session.query(
                sqlalchemy.func.max(model.Query.query_id)
            ).first()[0]
            if query_id is None:
                query_id = 0
            else:
                query_id += 1
            new_query = model.Query(
                query_id=query_id,
                start_graph_id=query_graph.graph_id,
                end_graph_id=target_graph.graph_id
            )
            session.add(new_query)
        return QueryHandle(query_id)

    @classmethod
    def read(cls, query_id: int):
        """Create a new QueryHandle to an existing query with unique id `query_id`

        :param query_id: unique identifier for a query
        :type query_id: int
        :return: new QueryHandle
        :rtype: QueryHandle
        """

        return QueryHandle(query_id)

    def delete(self):
        """Delete this query and any associated matches
        """

        self._check_exists()
        with session_scope() as session:
            session.query(model.Query).filter(
                model.Query.query_id == self.query_id
            ).delete()
            session.query(model.Match).filter(
                model.Match.query_id == self.query_id
            ).delete()

    def query_graph(self) -> GraphHandle:
        """Get a QueryHandle for the query graph

        :return: query graph
        :rtype: GraphHandle
        """

        self._check_exists()
        with session_scope() as session:
            start_graph = session.query(
                model.Graph
            ).join(
                model.Query, model.Graph.graph_id == model.Query.start_graph_id
            ).filter(model.Query.query_id == self.query_id).first()
            graph_id = start_graph.graph_id
        return GraphHandle(graph_id)

    def target_graph(self) -> GraphHandle:
        """Get a QueryHandle for the target graph

        :return: target graph
        :rtype: GraphHandle
        """

        self._check_exists()
        with session_scope() as session:
            end_graph = session.query(
                model.Graph
            ).join(
                model.Query, model.Graph.graph_id == model.Query.end_graph_id
            ).filter(model.Query.query_id == self.query_id).first()
            graph_id = end_graph.graph_id
        return GraphHandle(graph_id)

    def add_matches(
        self,
        sources: typing.Iterable[int],
        targets: typing.Iterable[int],
        weights: typing.Iterable[float],
        **kwargs
    ):
        """Add candidate matches between the query graph and the target graph

        Matches represent a pairwise node similarity
        between all nodes in the query graph
        and all nodes in the target graph.
        Only similarities with non zero score need to be stated explicitly.
        Matches with zero score are implicit.

        :param sources: Iterable of `src_id` in the query graph
        :type sources: typing.Iterable[int]
        :param targets: Iterable of `src_id` in the target graph
        :type targets: typing.Iterable[int]
        :param weights: Iterable of weights between 0 and 1
        :type weights: typing.Iterable[float]

        """

        self._check_exists()
        keys = kwargs.keys()
        if 'start' in keys:
            raise(ValueError('start is a reserved node attribute \
            which cannot be assigned using kwargs'))
        if 'end' in keys:
            raise(ValueError('end is a reserved node attribute \
            which cannot be assigned using kwargs'))
        if 'type' in keys:
            raise(ValueError('type is a reserved node attribute \
            which cannot be assigned using kwargs'))
        if 'weight' in keys:
            raise(ValueError('weight is a reserved node attribute \
            which cannot be assigned using kwargs'))
        hashed_sources = map(_hash, sources)
        hashed_targetes = map(_hash, targets)
        zipped = itertools.zip_longest(
            hashed_sources, hashed_targetes, weights,
            *kwargs.values(), fillvalue=NullValue()
        )
        query_graph = self.query_graph()
        target_graph = self.target_graph()
        matches = (
            model.Match(
                start=start,
                end=end,
                start_graph_id=query_graph.graph_id,
                end_graph_id=target_graph.graph_id,
                query_id=self.query_id,
                weight=weight,
                meta=json.dumps({key: val for key, val in zip(keys, values)})
            )
            for start, end, weight, *values in zipped
        )
        matches = check_matches(matches)
        with session_scope() as session:
            session.add_all(matches)
            session.commit()

    def _query_nodes(self):
        with session_scope() as session:
            nodes = session.query(model.Node).join(
                model.Query, model.Node.graph_id == model.Query.start_graph_id
            ).filter(model.Query.query_id == self.query_id).all()
            nodes = [
                Node(n.node_id, 'query', json.loads(n.meta)) for n in nodes
            ]
        return nodes

    def _query_edges(self):
        with session_scope() as session:
            edges = session.query(model.Edge).join(
                model.Query, model.Edge.graph_id == model.Query.start_graph_id
            ).filter(
                model.Query.query_id == self.query_id
            ).filter(
                model.Edge.start < model.Edge.end
            )
            edges = [
                Edge(e.start, e.end, 'query', json.loads(e.meta))
                for e in edges
            ]
        return edges

    def _target_nodes(self):
        with session_scope() as session:
            nodes = session.query(model.Node).join(
                model.Query, model.Node.graph_id == model.Query.end_graph_id
            ).filter(model.Query.query_id == self.query_id).all()
            nodes = [
                Node(n.node_id, 'target', json.loads(n.meta)) for n in nodes
            ]
        return nodes

    @staticmethod
    def is_between(target_ids, edge):
        return edge.start in target_ids and edge.end in target_ids

    def _target_edges(self, target_nodes, target_edges_arr):
        # only include target edges that are between the target nodes above
        target_ids = [n.id for n in target_nodes]
        edges = (
            Edge(int(start), int(end), 'target', None)
            for start, end, d
            in target_edges_arr[['u', 'uu', 'dist_u']]
            if d < 2
        )
        edges = [edge for edge in edges if self.is_between(target_ids, edge)]
        starts, ends = [], []
        for edge in edges:
            start, end = sorted((edge.start, edge.end))
            starts.append(start)
            ends.append(end)

        with session_scope() as session:
            edges = session.query(model.Edge).join(
                model.Query, model.Query.end_graph_id == model.Edge.graph_id
            ).filter(
                model.Query.query_id == self.query_id
            ).filter(
                model.Edge.start.in_(starts)
            ).filter(
                model.Edge.end.in_(ends)
            ).filter(
                model.Edge.start < model.Edge.end
            ).distinct().all()
            edges = [
                Edge(e.start, e.end, 'target', json.loads(e.meta))
                for e in edges
            ]
        return edges

    def _optimise(self, hopping_distance, max_iters, offsets):
        with session_scope() as session:
            sql_query = fornax.select.join(
                self.query_id, h=hopping_distance, offsets=offsets
            )
            records = sql_query.with_session(session).all()

        packed = fornax.opt.solve(
            records,
            hopping_distance=hopping_distance,
            max_iters=max_iters
        )
        inference_costs, subgraphs, iters, sz, target_edges_arr = packed
        return inference_costs, subgraphs, iters, sz, target_edges_arr

    @classmethod
    def _get_scores(cls, inference_costs, query_nodes, subgraphs, sz):
        scores = []
        for subgraph in subgraphs:
            score = sum(inference_costs[k] for k in subgraph)
            score += sz - len(subgraph)
            score /= len(query_nodes)
            scores.append(score)
        return scores

    def execute(self, n=5, hopping_distance=2, max_iters=10):
        """Execute a fuzzy subgraph matching query

        :param n: number of subgraph matches to return, defaults to 5
        :param n: int, optional
        :param hopping_distance: lengthscale hyperparameter, defaults to 2
        :param hopping_distance: int, optional
        :param max_iters: maximum number of optimisation iterations,
        defaults to 10
        :param max_iters: int, optional
        :raises ValueError: Raised if there are no matches
        between the query and target graph
        :return: query result
        :rtype: dict
        """

        offsets = None  # TODO: implement batching
        self._check_exists()
        if not len(self):
            raise ValueError('Cannot execute query with no matches')

        graphs = []
        query_nodes = sorted(self._query_nodes())
        target_nodes = sorted(self._target_nodes())
        # we will with get target edges from the optimiser
        # since the optimiser knows this anyway
        target_edges = None
        query_edges = sorted(self._query_edges())

        packed = self._optimise(hopping_distance, max_iters, offsets)
        inference_costs, subgraphs, iters, sz, target_edges_arr = packed
        target_edges = self._target_edges(target_nodes, target_edges_arr)
        target_edges = sorted(target_edges)

        scores = self._get_scores(inference_costs, query_nodes, subgraphs, sz)
        # sort graphs by score then deturministicly by hashing
        idxs = sorted(
            enumerate(scores),
            key=lambda x: (x[1], _hash(tuple(subgraphs[x[0]])))
        )

        query_nodes_payload = [node.to_dict() for node in query_nodes]
        query_edges_payload = [edge.to_dict() for edge in query_edges]
        target_nodes_payload = [node.to_dict() for node in target_nodes]
        target_edges_payload = [edge.to_dict() for edge in target_edges]

        for i, score in idxs[:min(n, len(idxs))]:
            _, match_ends = zip(*subgraphs[i])
            matches = [
                Edge(s, e, 'match', {}, 1. - inference_costs[s, e]).to_dict()
                for s, e in sorted(subgraphs[i])
            ]
            match_ends = set(_hash((i, 'target')) for i in match_ends)
            nxt_graph = {
                'is_multigraph': False,
                'cost': score,
                'nodes': list(query_nodes_payload),  # make a copy
                'links': matches + list(query_edges_payload)  # make a copy
            }
            nxt_graph['nodes'].extend([
                n for n in target_nodes_payload
                if n['id'] in match_ends
            ])
            nxt_graph['links'].extend(
                [
                    e for e in target_edges_payload
                    if e['source'] in match_ends and e['target'] in match_ends
                ]
            )
            graphs.append(nxt_graph)

        return {
            'graphs': graphs,
            'iters': iters,
            'hopping_distance': hopping_distance,
            'max_iters': max_iters
        }
