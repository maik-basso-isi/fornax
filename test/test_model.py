import unittest
import fornax.model as model
from test_base import TestCaseDB


class TestNode(TestCaseDB):

    def setUp(self):
        super().setUp()
        new_node_type = model.NodeType(id=0, description="A node type for testing")
        self.session.add(new_node_type)
        self.session.commit()

    def test_node_round_trip(self):
        """ node round trip """
        new_node = model.Node(label='Ed Jones', type=0)
        self.session.add(new_node)
        self.session.commit()

        row = self.session.query(model.Node).first()
        self.assertIsNotNone(row)
        self.assertEqual(new_node.label, row.label)
        self.assertEqual(new_node.type, 0)

    def test_trgm(self):
        """ Assert that pg_trgm is enabled """
        # Insert some labelled nodes
        labels = ['Matt', 'Dom', 'Callum', 'David', 'Anthony']
        for label in labels:
            new_node = model.Node(label=label, type=0)
            self.session.add(new_node)
            self.session.commit()

        # Find a node using a label with an edit distance of at least one
        # from any node label
        query = self.session.query(model.Node)
        query = query.order_by(model.Node.label.op('<->')('Calum'))
        row = query.first()

        self.assertIsNotNone(row)
        self.assertEqual(row.label, 'Callum')


class TestEdge(TestCaseDB):

    def setUp(self):
        super().setUp()
        new_edge_type = model.EdgeType(id=0, description="An edge type for testing")
        new_node_type = model.NodeType(id=0, description="A node type for testing")
        self.session.add(new_edge_type)
        self.session.add(new_node_type)
        self.session.commit()

        new_nodes = [
            model.Node(id=id_, label=label, type=type_) 
            for id_, label, type_ in [(0, "Greg", 0), (1, "Sue", 0)]
        ]

        for new_node in new_nodes:
            self.session.add(new_node)
        self.session.commit()

        new_edge = model.Edge(start=0, end=1, type=0, weight=1.)
        self.session.add(new_edge)
        self.session.commit()

    def test_edge_round_trip(self):
        """ edge round trip """
        row = self.session.query(model.Edge).first()
        self.assertIsNotNone(row)
        self.assertEqual(0, row.start)

    def test_edge_join_start(self):
        """ find a node by joining on the start of an edge """
        query = self.session.query(model.Node)
        query = query.join(model.Edge, model.Node.id==model.Edge.start)
        row = query.first()
        self.assertIsNotNone(row)
        self.assertEqual(row.id, 0)

    def test_edge_join_end(self):
        """ find a node by joining on the end of an edge """
        query = self.session.query(model.Node)
        query = query.join(model.Edge, model.Node.id==model.Edge.end)
        row = query.first()
        self.assertIsNotNone(row)
        self.assertEqual(row.id, 1)


if __name__ == '__main__':
    unittest.main()
