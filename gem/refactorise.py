"""Data structures and algorithms for generic expansion and
refactorisation."""

from __future__ import absolute_import, print_function, division
from six import iteritems, itervalues
from six.moves import intern, map

from collections import Counter, OrderedDict, defaultdict, namedtuple
from itertools import product, count

import numpy

from gem.node import Memoizer, traversal
from gem.gem import (Node, Zero, Product, Sum, Indexed, ListTensor, one,
                     IndexSum)
from gem.optimise import (remove_componenttensors, sum_factorise,
                          traverse_product, traverse_sum, unroll_indexsum,
                          fast_sum_factorise, associate_product, associate_sum)


# Refactorisation labels

ATOMIC = intern('atomic')
"""Label: the expression need not be broken up into smaller parts"""

COMPOUND = intern('compound')
"""Label: the expression must be broken up into smaller parts"""

OTHER = intern('other')
"""Label: the expression is irrelevant with regards to refactorisation"""


Monomial = namedtuple('Monomial', ['sum_indices', 'atomics', 'rest'])
"""Monomial type, representation of a tensor product with some
distinguished factors (called atomics).

- sum_indices: indices to sum over
- atomics: tuple of expressions classified as ATOMIC
- rest: a single expression classified as OTHER

A :py:class:`Monomial` is a structured description of the expression:

.. code-block:: python

    IndexSum(reduce(Product, atomics, rest), sum_indices)

"""


class MonomialSum(object):
    """Represents a sum of :py:class:`Monomial`s.

    The set of :py:class:`Monomial` summands are represented as a
    mapping from a pair of unordered ``sum_indices`` and unordered
    ``atomics`` to a ``rest`` GEM expression.  This representation
    makes it easier to merge similar monomials.
    """
    def __init__(self):
        # (unordered sum_indices, unordered atomics) -> rest
        self.monomials = defaultdict(Zero)

        # We shall retain ordering for deterministic code generation:
        #
        # (unordered sum_indices, unordered atomics) ->
        #     (ordered sum_indices, ordered atomics)
        self.ordering = OrderedDict()

    def add(self, sum_indices, atomics, rest):
        """Updates the :py:class:`MonomialSum` adding a new monomial."""
        sum_indices = tuple(sum_indices)
        sum_indices_set = frozenset(sum_indices)
        # Sum indices cannot have duplicates
        assert len(sum_indices) == len(sum_indices_set)

        atomics = tuple(atomics)
        atomics_set = frozenset(iteritems(Counter(atomics)))

        assert isinstance(rest, Node)

        key = (sum_indices_set, atomics_set)
        self.monomials[key] = Sum(self.monomials[key], rest)
        self.ordering.setdefault(key, (sum_indices, atomics))

    def __iter__(self):
        """Iteration yields :py:class:`Monomial` objects"""
        for key, (sum_indices, atomics) in iteritems(self.ordering):
            rest = self.monomials[key]
            yield Monomial(sum_indices, atomics, rest)

    @staticmethod
    def sum(*args):
        """Sum of multiple :py:class:`MonomialSum`s"""
        result = MonomialSum()
        for arg in args:
            assert isinstance(arg, MonomialSum)
            # Optimised implementation: no need to decompose and
            # reconstruct key.
            for key, rest in iteritems(arg.monomials):
                result.monomials[key] = Sum(result.monomials[key], rest)
            for key, value in iteritems(arg.ordering):
                result.ordering.setdefault(key, value)
        return result

    @staticmethod
    def product(*args):
        """Product of multiple :py:class:`MonomialSum`s"""
        result = MonomialSum()
        for monomials in product(*args):
            sum_indices = []
            atomics = []
            rest = one
            for s, a, r in monomials:
                sum_indices.extend(s)
                atomics.extend(a)
                rest = Product(r, rest)
            result.add(sum_indices, atomics, rest)
        return result

    def argument_indices_extent(self, factor):
        """
        Returns the product of extents of argument indices of :param: factor
        """
        if self.argument_indices is None:
            raise AssertionError("argument_indices property not initialised.")
        return numpy.product([i.extent for i in set(factor.free_indices).intersection(self.argument_indices)])

    def unique_sum_indices(self):
        """generator of unique sum indices, together with their original
        ordering of this MonomialSum
        """
        seen = set()
        for (sum_indices_set, _), (sum_indices, _) in iteritems(self.ordering):
            if sum_indices_set not in seen:
                seen.add(sum_indices_set)
                yield (sum_indices_set, sum_indices)

    def to_expression(self):
        """
        Convert MonomialSum object to gem node. Use associate_product() and
        associate_sum() to promote hoisting in subsequent code generation.
        ordering ensures deterministic code generation.
        :return: gem node represented by this MonomialSum object.
        """
        indexsums = []  # The result is summation of indexsums
        monomial_group = OrderedDict()  # (sum_indices_set, sum_indices) -> [(atomics, rest)]
        # Group monomials according to their sum indices
        for key, (sum_indices, atomics) in iteritems(self.ordering):
            sum_indices_set, _ = key
            rest = self.monomials[key]
            if not sum_indices_set:
                indexsums.append(fast_sum_factorise(sum_indices, atomics + (rest,)))
            else:
                monomial_group.setdefault((sum_indices_set, sum_indices), []).append((atomics, rest))

        # Form IndexSum's from each monomial group
        for (_, sum_indices), monomials in iteritems(monomial_group):
            all_atomics = [m[0] for m in monomials]
            all_rest = [m[1] for m in monomials]
            if len(all_atomics) == 1:
                # Just one term, add to indexsums directly
                indexsums.append(fast_sum_factorise(sum_indices, all_atomics[0] + (all_rest[0],)))
            else:
                # Form products for each monomial
                products = [associate_product(atomics + (_rest,))[0] for atomics, _rest in zip(all_atomics, all_rest)]
                indexsums.append(IndexSum(associate_sum(products)[0], sum_indices))

        return associate_sum(indexsums)[0]

    def find_optimal_atomics(self, sum_indices):
        sum_indices_set, _ = sum_indices
        index = count()
        atomic_index = OrderedDict()  # Atomic gem node -> int
        connections = []
        # add connections (list of lists)
        for (_sum_indices, _), (_, atomics) in iteritems(self.ordering):
            if _sum_indices == sum_indices_set:
                connection = []
                for atomic in atomics:
                    if atomic not in atomic_index:
                        atomic_index[atomic] = next(index)
                    connection.append(atomic_index[atomic])
                connections.append(tuple(connection))

        if len(atomic_index) == 0:
            return ((), ())
        if len(atomic_index) == 1:
            return ((list(atomic_index.keys())[0], ), ())

        # set up the ILP
        import pulp as ilp
        ilp_prob = ilp.LpProblem('gem factorise', ilp.LpMinimize)
        ilp_var = ilp.LpVariable.dicts('node', range(len(atomic_index)), 0, 1, ilp.LpBinary)

        # Objective function
        # Minimise number of factors to pull. If same number, favour factor with larger extent
        big = 10000000  # some arbitrary big number
        ilp_prob += ilp.lpSum(ilp_var[index] * (big - self.argument_indices_extent(atomic)) for atomic, index in iteritems(atomic_index))

        # constraints
        for connection in connections:
            ilp_prob += ilp.lpSum(ilp_var[index] for index in connection) >= 1

        ilp_prob.solve()
        if ilp_prob.status != 1:
            raise AssertionError("Something bad happened during ILP")

        optimal_atomics = [atomic for atomic, _index in iteritems(atomic_index) if ilp_var[_index].value() == 1]
        other_atomics = [atomic for atomic, _index in iteritems(atomic_index) if ilp_var[_index].value() == 0]
        optimal_atomics = sorted(optimal_atomics, key=lambda x: self.argument_indices_extent(x), reverse=True)
        other_atomics = sorted(other_atomics, key=lambda x: self.argument_indices_extent(x), reverse=True)
        return (tuple(optimal_atomics), tuple(other_atomics))

    def factorise_atomics(self, optimal_atomics):
        """
        Group and factorise monomials based on a list of atomics. Create new monomials for
        each group and optimise them recursively.
        :param optimal_atomics: list of tuples of optimal atomics and their sum indices
        :return: new MonomialSum object with atomics factorised.
        """
        if not optimal_atomics:
            return self
        if len(self.ordering) < 2:
            return self
        new_monomial_sum = MonomialSum()
        new_monomial_sum.argument_indices = self.argument_indices
        # Group monomials according to each optimal atomic
        factor_group = OrderedDict()
        for key, (_sum_indices, _atomics) in iteritems(self.ordering):
            for (sum_indices_set, sum_indices), oa in optimal_atomics:
                if key[0] == sum_indices_set and oa in _atomics:
                    # Add monomial key to the list of corresponding optimal atomic
                    factor_group.setdefault(((sum_indices_set, sum_indices), oa), []).append(key)
                    break
            else:
                # Add monomials that do no have argument factors to new MonomialSum
                new_monomial_sum.add(_sum_indices, _atomics, self.monomials[key])
        # We should not drop monomials
        assert sum([len(x) for x in itervalues(factor_group)]) + len(new_monomial_sum.ordering) == len(self.ordering)

        for ((sum_indices_set, sum_indices), oa), keys in iteritems(factor_group):
            if len(keys) == 1:
                # Just one monomials with this atomic, add to new MonomialSum straightaway
                _, _atomics = self.ordering[keys[0]]
                _rest = self.monomials[keys[0]]
                new_monomial_sum.add(sum_indices, _atomics, _rest)
                continue
            all_atomics = []  # collect all atomics from monomials
            all_rest = []  # collect all rest from monomials
            for key in keys:
                _, _atomics = self.ordering[key]
                _atomics = list(_atomics)
                _atomics.remove(oa)  # remove common factor
                all_atomics.append(_atomics)
                all_rest.append(self.monomials[key])
            # Create new MonomialSum for the factorised out terms
            sub_monomial_sum = MonomialSum()
            sub_monomial_sum.argument_indices = self.argument_indices
            for _atomics, _rest in zip(all_atomics, all_rest):
                sub_monomial_sum.add((), _atomics, _rest)
            sub_monomial_sum = sub_monomial_sum.optimise()
            assert len(sub_monomial_sum.ordering) != 0
            if len(sub_monomial_sum.ordering) == 1:
                # result is a product, add to new MonomialSum directly
                (_, new_atomics), = list(itervalues(sub_monomial_sum.ordering))
                new_atomics += (oa,)
                new_rest, = list(itervalues(sub_monomial_sum.monomials))
            else:
                # result is a sum, need to form new node
                new_node = sub_monomial_sum.to_expression()
                new_atomics = [oa]
                new_rest = one
                if set(self.argument_indices) & set(new_node.free_indices):
                    new_atomics.append(new_node)
                else:
                    new_rest = new_node
            new_monomial_sum.add(sum_indices, new_atomics, new_rest)
        return new_monomial_sum

    def optimise(self):
        optimal_atomics = []  # [(sum_indices, optimal_atomics))]
        for sum_indices in self.unique_sum_indices():
            atomics = self.find_optimal_atomics(sum_indices)
            optimal_atomics.extend([(sum_indices, _atomic) for _atomic in atomics[0]])
        # This algorithm is O(N!), where N = len(optimal_atomics)
        # we could truncate the optimal_atomics list at say 10
        return self.factorise_atomics(optimal_atomics)


class FactorisationError(Exception):
    """Raised when factorisation fails to achieve some desired form."""
    pass


def _collect_monomials(expression, self):
    """Refactorises an expression into a sum-of-products form, using
    distributivity rules (i.e. a*(b + c) -> a*b + a*c).  Expansion
    proceeds until all "compound" expressions are broken up.

    :arg expression: a GEM expression to refactorise
    :arg self: function for recursive calls

    :returns: :py:class:`MonomialSum`

    :raises FactorisationError: Failed to break up some "compound"
                                expressions with expansion.
    """
    # Phase 1: Collect and categorise product terms
    def stop_at(expr):
        # Break up compounds only
        return self.classifier(expr) != COMPOUND
    common_indices, terms = traverse_product(expression, stop_at=stop_at)
    common_indices = tuple(common_indices)

    common_atomics = []
    common_others = []
    compounds = []
    for term in terms:
        label = self.classifier(term)
        if label == ATOMIC:
            common_atomics.append(term)
        elif label == COMPOUND:
            compounds.append(term)
        elif label == OTHER:
            common_others.append(term)
        else:
            raise ValueError("Classifier returned illegal value.")
    common_atomics = tuple(common_atomics)

    # Phase 2: Attempt to break up compound terms into summands
    sums = []
    for expr in compounds:
        summands = traverse_sum(expr, stop_at=stop_at)
        if len(summands) <= 1:
            # Compound term is not an addition, avoid infinite
            # recursion and fail gracefully raising an exception.
            raise FactorisationError(expr)
        # Recurse into each summand, concatenate their results
        sums.append(MonomialSum.sum(*map(self, summands)))

    # Phase 3: Expansion
    #
    # Each element of ``sums`` is a MonomialSum.  Expansion produces a
    # series (representing a sum) of products of monomials.
    result = MonomialSum()
    for s, a, r in MonomialSum.product(*sums):
        all_indices = common_indices + s
        atomics = common_atomics + a

        # All free indices that appear in atomic terms
        atomic_indices = set().union(*[atomic.free_indices
                                       for atomic in atomics])

        # Sum indices that appear in atomic terms
        # (will go to the result :py:class:`Monomial`)
        sum_indices = tuple(index for index in all_indices
                            if index in atomic_indices)

        # Sum indices that do not appear in atomic terms
        # (can factorise them over atomic terms immediately)
        rest_indices = tuple(index for index in all_indices
                             if index not in atomic_indices)

        # Not really sum factorisation, but rather just an optimised
        # way of building a product.
        rest = sum_factorise(rest_indices, common_others + [r])

        result.add(sum_indices, atomics, rest)
    return result


def collect_monomials(expressions, classifier):
    """Refactorises expressions into a sum-of-products form, using
    distributivity rules (i.e. a*(b + c) -> a*b + a*c).  Expansion
    proceeds until all "compound" expressions are broken up.

    :arg expressions: GEM expressions to refactorise
    :arg classifier: a function that can classify any GEM expression
                     as ``ATOMIC``, ``COMPOUND``, or ``OTHER``.  This
                     classification drives the factorisation.

    :returns: list of :py:class:`MonomialSum`s

    :raises FactorisationError: Failed to break up some "compound"
                                expressions with expansion.
    """
    # Get ComponentTensors out of the way
    expressions = remove_componenttensors(expressions)

    # Get ListTensors out of the way
    must_unroll = []  # indices to unroll
    for node in traversal(expressions):
        if isinstance(node, Indexed):
            child, = node.children
            if isinstance(child, ListTensor) and classifier(node) == COMPOUND:
                must_unroll.extend(node.multiindex)
    if must_unroll:
        must_unroll = set(must_unroll)
        expressions = unroll_indexsum(expressions,
                                      predicate=lambda i: i in must_unroll)
        expressions = remove_componenttensors(expressions)

    # Finally, refactorise expressions
    mapper = Memoizer(_collect_monomials)
    mapper.classifier = classifier
    return list(map(mapper, expressions))
