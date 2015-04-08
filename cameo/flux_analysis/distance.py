# Copyright 2014 Novo Nordisk Foundation Center for Biosustainability, DTU.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import, print_function

"""Methods for manipulating a model to compute a set of fluxes that minimize (or maximize)
different notions of distance (L1, number of binary changes etc.) to a given reference flux distribution."""

import six
from six import types

from sympy import Add
from sympy import Mul
from sympy import RealNumber

from cameo.core.result import FluxDistributionResult
from cameo.core.solution import SolutionBase

add = Add._from_args
mul = Mul._from_args

import logging
logger = logging.getLogger(__name__)


class Distance(object):

    """Abstract distance base class."""

    @staticmethod
    def __check_valid_reference(reference):
        if not isinstance(reference, types.DictType) and not isinstance(reference, SolutionBase):
            raise ValueError('%s is not a valid reference flux distribution. Needs to be either a dict or Solution object.')

    def __init__(self, model, reference=None, *args, **kwargs):
        super(Distance, self).__init__(*args, **kwargs)
        self.__check_valid_reference(reference)
        self._reference = reference
        self._model = model.copy()

    @property  # read-only
    def model(self):
        return self._model

    @property
    def reference(self):
        return self._reference

    @reference.setter
    def reference(self, value):
        self.__check_valid_reference(value)
        self._set_new_reference(value)

    def _set_new_reference(self, reference):
        raise NotImplementedError

    def _prep_model(self):
        raise NotImplementedError

    def minimize(self, *args, **kwargs):
        raise NotImplementedError

    def maximize(self, *args, **kwargs):
        raise NotImplementedError


class ManhattanDistance(Distance):

    """Compute steady-state fluxes that minimizes the Manhattan distance (L1 norm)
    to a reference flux distribution.

    Parameters
    ----------
    model : Model
    reference : dict or Solution
        A reference flux distribution.

    Attributes
    ----------
    model : Model
    reference : dict
    """

    def __init__(self, model, reference=None, *args, **kwargs):
        super(ManhattanDistance, self).__init__(model, reference=reference, *args, **kwargs)
        self._aux_variables = dict()
        self._deviation_constraints = dict()
        self._prep_model()

    def _prep_model(self):
        for rid, flux_value in self.reference.iteritems():
            self._add_deviavtion_constraint(rid, flux_value)
        objective = self.model.solver.interface.Objective(add(self._aux_variables.values()), name='deviations')
        self.model.objective = objective

    def _set_new_reference(self, reference):
        # remove unnecessary constraints
        constraints_to_remove = list()
        aux_vars_to_remove = list()
        for key in self._deviation_constraints.keys():
            if key not in reference:
                constraints_to_remove.extend(self._deviation_constraints.pop(key))
                aux_vars_to_remove.append(self._aux_variables[key])
        self.model.solver._remove_constraints(constraints_to_remove)
        self.model.solver._remove_variables(aux_vars_to_remove)
        # Add new or adapt existing constraints
        for key, value in six.iteritems(reference):
            try:
                (lb_constraint, ub_constraint) = self._deviation_constraints[key]
                lb_constraint.lb = value
                ub_constraint.ub = value
            except KeyError:
                self._add_deviavtion_constraint(key, value)

    def _add_deviavtion_constraint(self, reaction_id, flux_value):
        reaction = self.model.reactions.get_by_id(reaction_id)
        aux_var = self.model.solver.interface.Variable('aux_'+reaction_id, lb=0)
        self._aux_variables[reaction_id] = aux_var
        self.model.solver._add_variable(aux_var)
        if reaction.reverse_variable is None:
            expression = reaction.variable - aux_var
        else:
            expression = reaction.variable - reaction.reverse_variable - aux_var
        constraint_lb = self.model.solver.interface.Constraint(expression, ub=flux_value, name='deviation_lb_'+reaction_id)
        self.model.solver._add_constraint(constraint_lb, sloppy=True)
        if reaction.reverse_variable is None:
            expression = reaction.variable + aux_var
        else:
            expression = reaction.variable - reaction.reverse_variable + aux_var
        constraint_ub = self.model.solver.interface.Constraint(expression, lb=flux_value, name='deviation_ub_'+reaction_id)
        self.model.solver._add_constraint(constraint_ub, sloppy=True)
        self._deviation_constraints[reaction_id] = (constraint_lb, constraint_ub)

    def minimize(self, *args, **kwargs):
        self.model.objective.direction = 'min'
        solution = self.model.solve()
        result = FluxDistributionResult(solution)
        return result


class RegulatoryOnOffDistance(Distance):
    """Minimize the number of reactions that need to be activated in order for a model
    to compute fluxes that are close to a provided reference flux distribution (none need to be activated
    if, for example, the model itself had been used to produce the reference flux distribution).

    Parameters
    ----------
    model : Model
    reference : dict or Solution
        A reference flux distribution.

    Attributes
    ----------
    model : Model
    reference : dict
    """

    def __init__(self, model, reference=None, *args, **kwargs):
        super(RegulatoryOnOffDistance, self).__init__(model, reference=reference, *args, **kwargs)
        self._aux_variables = dict()
        self._switch_constraints = dict()
        self._prep_model()

    def _prep_model(self):
        for rid, flux_value in self.reference.iteritems():
            self._add_switch_constraint(rid, flux_value)
        objective = self.model.solver.interface.Objective(add(self._aux_variables.values()), name='switches')
        self.model.objective = objective

    def _add_switch_constraint(self, reaction_id, flux_value, delta=0.03, epsilon=0.001):
        reaction = self.model.reactions.get_by_id(reaction_id)
        var_id = "y_%s" % reaction_id
        var = self.model.solver.interface.Variable(var_id, type="binary")
        self.model.solver._add_variable(var)
        self._aux_variables[var.name] = var

        constraint_a_id = "c_%s_lb" % reaction_id
        w_u = flux_value + delta * abs(flux_value) + epsilon
        # vi - yi(vmaxi + w_ui) >= w_ui
        expression = add([
            reaction.variable,
            mul([RealNumber(-reaction.upper_bound + w_u), var])])
        constraint_a = self.model.solver.interface.Constraint(expression, ub=w_u, name=constraint_a_id)

        self._switch_constraints[constraint_a.name] = constraint_a
        w_l = flux_value - delta * abs(flux_value) - epsilon
        constraint_b_id = "c_%s_ub" % reaction_id
        # vi - yi(vmini - w_li) <= w_li
        expression = add([
            reaction.variable,
            mul([RealNumber(-reaction.lower_bound + w_l), var])])
        constraint_b = self.model.solver.interface.Constraint(expression, lb=w_l, name=constraint_b_id)

        self._switch_constraints[constraint_b.name] = constraint_b
        self.model.solver._add_constraint(constraint_a)
        self.model.solver._add_constraint(constraint_b)

    def minimize(self, *args, **kwargs):
        self.model.objective.direction = 'min'
        solution = self.model.solve()
        result = FluxDistributionResult(solution)
        return result
