"""
Type inference base on CPA.
The algorithm guarantees monotonic growth of type-sets for each variable.

Steps:
    1. seed initial types
    2. build constraints
    3. propagate constraints
    4. unify types

Constraint propagation is precise and does not regret (no backtracing).
Constraints push types forward following the dataflow.
"""

from __future__ import print_function, division, absolute_import

import contextlib
import itertools
from pprint import pprint
import traceback

from numba import ir, types, utils, config, six, typing
from .errors import TypingError, UntypedAttributeError, new_error_context


class TypeVar(object):
    def __init__(self, context, var):
        self.context = context
        self.var = var
        self.type = None
        self.locked = False
        # Stores source location of first definition
        self.define_loc = None

    def add_type(self, tp, loc):
        assert isinstance(tp, types.Type), type(tp)

        if self.locked:
            if tp != self.type:
                if self.context.can_convert(tp, self.type) is None:
                    msg = ("No conversion from %s to %s for '%s', "
                           "defined at %s")
                    raise TypingError(msg % (tp, self.type, self.var,
                                             self.define_loc),
                                      loc=loc)
        else:
            if self.type is not None:
                unified = self.context.unify_pairs(self.type, tp)
                if unified is None:
                    msg = "cannot unify %s and %s for '%s', defined at %s"
                    raise TypingError(msg % (self.type, tp, self.var,
                                             self.define_loc),
                                      loc=loc)
            else:
                # First time definition
                unified = tp
                self.define_loc = loc

            self.type = unified

        return self.type

    def lock(self, tp, loc):
        assert isinstance(tp, types.Type), type(tp)
        assert not self.locked

        # If there is already a type, ensure we can convert it to the
        # locked type.
        if (self.type is not None and
            self.context.can_convert(self.type, tp) is None):
            raise TypingError("No conversion from %s to %s for "
                              "'%s'" % (tp, self.type, self.var))

        self.type = tp
        self.locked = True
        if self.define_loc is None:
            self.define_loc = loc

    def union(self, other, loc):
        if other.type is not None:
            self.add_type(other.type, loc=loc)

        return self.type

    def __repr__(self):
        return '%s := %s' % (self.var, self.type)

    @property
    def defined(self):
        return self.type is not None

    def get(self):
        return (self.type,) if self.type is not None else ()

    def getone(self):
        assert self.type is not None
        return self.type

    def __len__(self):
        return 1 if self.type is not None else 0


class ConstraintNetwork(object):
    """
    TODO: It is possible to optimize constraint propagation to consider only
          dirty type variables.
    """

    def __init__(self):
        self.constraints = []

    def append(self, constraint):
        self.constraints.append(constraint)

    def propagate(self, typeinfer):
        """
        Execute all constraints.  Errors are caught and returned as a list.
        This allows progressing even though some constraints may fail
        due to lack of information (e.g. imprecise types such as List(undefined)).
        """
        errors = []
        for constraint in self.constraints:
            loc = constraint.loc
            with typeinfer.warnings.catch_warnings(filename=loc.filename,
                                                   lineno=loc.line):
                try:
                    constraint(typeinfer)
                except TypingError as e:
                    errors.append(e)
                except Exception:
                    msg = "Internal error at {con}:\n{sep}\n{err}{sep}\n"
                    e = TypingError(msg.format(con=constraint,
                                               err=traceback.format_exc(),
                                               sep='--%<' +'-' * 65),
                                    loc=constraint.loc)
                    errors.append(e)
        return errors


class Propagate(object):
    """
    A simple constraint for direct propagation of types for assignments.
    """

    def __init__(self, dst, src, loc):
        self.dst = dst
        self.src = src
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of assignment at {0}", self.loc):
            typeinfer.copy_type(self.src, self.dst, loc=self.loc)
            # If `dst` is refined, notify us
            typeinfer.refine_map[self.dst] = self

    def refine(self, typeinfer, target_type):
        # Do not back-propagate to locked variables (e.g. constants)
        typeinfer.add_type(self.src, target_type, unless_locked=True,
                           loc=self.loc)


class ArgConstraint(object):

    def __init__(self, dst, src, loc):
        self.dst = dst
        self.src = src
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of argument at {0}", self.loc):
            typevars = typeinfer.typevars
            src = typevars[self.src]
            if not src.defined:
                return
            ty = src.getone()
            if isinstance(ty, types.Omitted):
                ty = typeinfer.context.resolve_value_type(ty.value)
            typeinfer.add_type(self.dst, ty, loc=self.loc)


class BuildTupleConstraint(object):
    def __init__(self, target, items, loc):
        self.target = target
        self.items = items
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of tuple at {0}", self.loc):
            typevars = typeinfer.typevars
            tsets = [typevars[i.name].get() for i in self.items]
            oset = typevars[self.target]
            for vals in itertools.product(*tsets):
                if vals and all(vals[0] == v for v in vals):
                    tup = types.UniTuple(dtype=vals[0], count=len(vals))
                else:
                    # empty tuples fall here as well
                    tup = types.Tuple(vals)
                typeinfer.add_type(self.target, tup, loc=self.loc)


class _BuildContainerConstraint(object):

    def __init__(self, target, items, loc):
        self.target = target
        self.items = items
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of list at {0}", self.loc):
            typevars = typeinfer.typevars
            oset = typevars[self.target]
            tsets = [typevars[i.name].get() for i in self.items]
            if not tsets:
                typeinfer.add_type(self.target,
                                   self.container_type(types.undefined),
                                   loc=self.loc)
            else:
                for typs in itertools.product(*tsets):
                    unified = typeinfer.context.unify_types(*typs)
                    if unified is not None:
                        typeinfer.add_type(self.target,
                                           self.container_type(unified),
                                           loc=self.loc)


class BuildListConstraint(_BuildContainerConstraint):
    container_type = types.List


class BuildSetConstraint(_BuildContainerConstraint):
    container_type = types.Set



class ExhaustIterConstraint(object):
    def __init__(self, target, count, iterator, loc):
        self.target = target
        self.count = count
        self.iterator = iterator
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of exhaust iter at {0}", self.loc):
            typevars = typeinfer.typevars
            oset = typevars[self.target]
            for tp in typevars[self.iterator.name].get():
                if isinstance(tp, types.BaseTuple):
                    if len(tp) == self.count:
                        typeinfer.add_type(self.target, tp, loc=self.loc)
                    else:
                        raise ValueError("wrong tuple length for %r: "
                                         "expected %d, got %d"
                                         % (self.iterator.name, self.count, len(tp)))
                elif isinstance(tp, types.IterableType):
                    tup = types.UniTuple(dtype=tp.iterator_type.yield_type,
                                         count=self.count)
                    typeinfer.add_type(self.target, tup, loc=self.loc)


class PairFirstConstraint(object):
    def __init__(self, target, pair, loc):
        self.target = target
        self.pair = pair
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of pair-first at {0}", self.loc):
            typevars = typeinfer.typevars
            oset = typevars[self.target]
            for tp in typevars[self.pair.name].get():
                if not isinstance(tp, types.Pair):
                    # XXX is this an error?
                    continue
                typeinfer.add_type(self.target, tp.first_type, loc=self.loc)


class PairSecondConstraint(object):
    def __init__(self, target, pair, loc):
        self.target = target
        self.pair = pair
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of pair-second at {0}", self.loc):
            typevars = typeinfer.typevars
            oset = typevars[self.target]
            for tp in typevars[self.pair.name].get():
                if not isinstance(tp, types.Pair):
                    # XXX is this an error?
                    continue
                typeinfer.add_type(self.target, tp.second_type, loc=self.loc)


class StaticGetItemConstraint(object):
    def __init__(self, target, value, index, index_var, loc):
        self.target = target
        self.value = value
        self.index = index
        if index_var is not None:
            self.fallback = IntrinsicCallConstraint(target, 'getitem',
                                                    (value, index_var), {},
                                                    None, loc)
        else:
            self.fallback = None
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of static-get-item at {0}", self.loc):
            typevars = typeinfer.typevars
            oset = typevars[self.target]
            for ty in typevars[self.value.name].get():
                itemty = typeinfer.context.resolve_static_getitem(value=ty,
                                                                  index=self.index)
                if itemty is not None:
                    typeinfer.add_type(self.target, itemty, loc=self.loc)
                elif self.fallback is not None:
                    self.fallback(typeinfer)

    def get_call_signature(self):
        # The signature is only needed for the fallback case in lowering
        return self.fallback and self.fallback.get_call_signature()


def fold_arg_vars(typevars, args, vararg, kws):
    """
    Fold and resolve the argument variables of a function call.
    """
    # Fetch all argument types, bail if any is unknown
    n_pos_args = len(args)
    kwds = [kw for (kw, var) in kws]
    argtypes = [typevars[a.name] for a in args]
    argtypes += [typevars[var.name] for (kw, var) in kws]
    if vararg is not None:
        argtypes.append(typevars[vararg.name])

    if not all(a.defined for a in argtypes):
        return

    args = tuple(a.getone() for a in argtypes)
    pos_args = args[:n_pos_args]
    if vararg is not None:
        if not isinstance(args[-1], types.BaseTuple):
            # Unsuitable for *args
            # (Python is more lenient and accepts all iterables)
            raise TypeError("*args in function call should be a tuple, got %s"
                            % (args[-1],))
        pos_args += args[-1].types
        args = args[:-1]
    kw_args = dict(zip(kwds, args[n_pos_args:]))
    return pos_args, kw_args


class CallConstraint(object):
    """Constraint for calling functions.
    Perform case analysis foreach combinations of argument types.
    """
    signature = None

    def __init__(self, target, func, args, kws, vararg, loc):
        self.target = target
        self.func = func
        self.args = args
        self.kws = kws or {}
        self.vararg = vararg
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of call at {0}", self.loc):
            typevars = typeinfer.typevars
            fnty = typevars[self.func].getone()
            with new_error_context("resolving callee type: {0}", fnty):
                self.resolve(typeinfer, typevars, fnty)

    def resolve(self, typeinfer, typevars, fnty):
        assert fnty
        context = typeinfer.context

        r = fold_arg_vars(typevars, self.args, self.vararg, self.kws)
        if r is None:
            # Cannot resolve call type until all argument types are known
            return
        pos_args, kw_args = r

        # Resolve call type
        sig = typeinfer.resolve_call(fnty, pos_args, kw_args)
        if sig is None:
            # Arguments are invalid => explain why
            headtemp = "Invalid usage of {0} with parameters ({1})"
            args = [str(a) for a in pos_args]
            args += ["%s=%s" % (k, v) for k, v in sorted(kw_args.items())]
            head = headtemp.format(fnty, ', '.join(map(str, args)))
            desc = context.explain_function_type(fnty)
            msg = '\n'.join([head, desc])
            raise TypingError(msg, loc=self.loc)

        typeinfer.add_type(self.target, sig.return_type, loc=self.loc)

        # If the function is a bound function and its receiver type
        # was refined, propagate it.
        if (isinstance(fnty, types.BoundFunction)
            and sig.recvr is not None
            and sig.recvr != fnty.this):
            refined_this = context.unify_pairs(sig.recvr, fnty.this)
            if refined_this is not None and refined_this.is_precise():
                refined_fnty = fnty.copy(this=refined_this)
                typeinfer.propagate_refined_type(self.func, refined_fnty)

        # If the return type is imprecise but can be unified with the
        # target variable's inferred type, use the latter.
        # Useful for code such as::
        #    s = set()
        #    s.add(1)
        # (the set() call must be typed as int64(), not undefined())
        if not sig.return_type.is_precise():
            target = typevars[self.target]
            if target.defined:
                targetty = target.getone()
                if context.unify_pairs(targetty, sig.return_type) == targetty:
                    sig.return_type = targetty

        self.signature = sig

    def get_call_signature(self):
        return self.signature


class IntrinsicCallConstraint(CallConstraint):
    def __call__(self, typeinfer):
        with new_error_context("typing of intrinsic-call at {0}", self.loc):
            self.resolve(typeinfer, typeinfer.typevars, fnty=self.func)


class GetAttrConstraint(object):
    def __init__(self, target, attr, value, loc, inst):
        self.target = target
        self.attr = attr
        self.value = value
        self.loc = loc
        self.inst = inst

    def __call__(self, typeinfer):
        with new_error_context("typing of get attribute at {0}", self.loc):
            typevars = typeinfer.typevars
            valtys = typevars[self.value.name].get()
            for ty in valtys:
                attrty = typeinfer.context.resolve_getattr(ty, self.attr)
                if attrty is None:
                    raise UntypedAttributeError(ty, self.attr, loc=self.inst.loc)
                else:
                    typeinfer.add_type(self.target, attrty, loc=self.loc)
            typeinfer.refine_map[self.target] = self

    def refine(self, typeinfer, target_type):
        if isinstance(target_type, types.BoundFunction):
            recvr = target_type.this
            typeinfer.add_type(self.value.name, recvr, loc=self.loc)
            source_constraint = typeinfer.refine_map.get(self.value.name)
            if source_constraint is not None:
                source_constraint.refine(typeinfer, recvr)

    def __repr__(self):
        return 'resolving type of attribute "{attr}" of "{value}"'.format(
            value=self.value, attr=self.attr)


class SetItemConstraint(object):
    def __init__(self, target, index, value, loc):
        self.target = target
        self.index = index
        self.value = value
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of setitem at {0}", self.loc):
            typevars = typeinfer.typevars
            if not all(typevars[var.name].defined
                       for var in (self.target, self.index, self.value)):
                return
            targetty = typevars[self.target.name].getone()
            idxty = typevars[self.index.name].getone()
            valty = typevars[self.value.name].getone()

            sig = typeinfer.context.resolve_setitem(targetty, idxty, valty)
            if sig is None:
                raise TypingError("Cannot resolve setitem: %s[%s] = %s" %
                                  (targetty, idxty, valty), loc=self.loc)
            self.signature = sig

    def get_call_signature(self):
        return self.signature


class StaticSetItemConstraint(object):
    def __init__(self, target, index, index_var, value, loc):
        self.target = target
        self.index = index
        self.index_var = index_var
        self.value = value
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of staticsetitem at {0}", self.loc):
            typevars = typeinfer.typevars
            if not all(typevars[var.name].defined
                       for var in (self.target, self.index_var, self.value)):
                return
            targetty = typevars[self.target.name].getone()
            idxty = typevars[self.index_var.name].getone()
            valty = typevars[self.value.name].getone()

            sig = typeinfer.context.resolve_static_setitem(targetty, self.index, valty)
            if sig is None:
                sig = typeinfer.context.resolve_setitem(targetty, idxty, valty)
            if sig is None:
                raise TypingError("Cannot resolve setitem: %s[%r] = %s" %
                                  (targetty, self.index, valty), loc=self.loc)
            self.signature = sig

    def get_call_signature(self):
        return self.signature


class DelItemConstraint(object):
    def __init__(self, target, index, loc):
        self.target = target
        self.index = index
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of delitem at {0}", self.loc):
            typevars = typeinfer.typevars
            if not all(typevars[var.name].defined
                       for var in (self.target, self.index)):
                return
            targetty = typevars[self.target.name].getone()
            idxty = typevars[self.index.name].getone()

            sig = typeinfer.context.resolve_delitem(targetty, idxty)
            if sig is None:
                raise TypingError("Cannot resolve delitem: %s[%s]" %
                                  (targetty, idxty), loc=self.loc)
            self.signature = sig

    def get_call_signature(self):
        return self.signature


class SetAttrConstraint(object):
    def __init__(self, target, attr, value, loc):
        self.target = target
        self.attr = attr
        self.value = value
        self.loc = loc

    def __call__(self, typeinfer):
        with new_error_context("typing of set attribute {0!r} at {1}",
                               self.attr, self.loc):
            typevars = typeinfer.typevars
            if not all(typevars[var.name].defined
                       for var in (self.target, self.value)):
                return
            targetty = typevars[self.target.name].getone()
            valty = typevars[self.value.name].getone()
            sig = typeinfer.context.resolve_setattr(targetty, self.attr,
                                                    valty)
            if sig is None:
                raise TypingError("Cannot resolve setattr: (%s).%s = %s" %
                                  (targetty, self.attr, valty),
                                  loc=self.loc)
            self.signature = sig

    def get_call_signature(self):
        return self.signature


class PrintConstraint(object):
    def __init__(self, args, vararg, loc):
        self.args = args
        self.vararg = vararg
        self.loc = loc

    def __call__(self, typeinfer):
        typevars = typeinfer.typevars

        r = fold_arg_vars(typevars, self.args, self.vararg, {})
        if r is None:
            # Cannot resolve call type until all argument types are known
            return
        pos_args, kw_args = r

        fnty = typeinfer.context.resolve_value_type(print)
        assert fnty is not None
        sig = typeinfer.resolve_call(fnty, pos_args, kw_args)
        self.signature = sig

    def get_call_signature(self):
        return self.signature


class TypeVarMap(dict):
    def set_context(self, context):
        self.context = context

    def __getitem__(self, name):
        if name not in self:
            self[name] = TypeVar(self.context, name)
        return super(TypeVarMap, self).__getitem__(name)

    def __setitem__(self, name, value):
        assert isinstance(name, str)
        if name in self:
            raise KeyError("Cannot redefine typevar %s" % name)
        else:
            super(TypeVarMap, self).__setitem__(name, value)


# A temporary mapping of {function name: dispatcher object}
_temporary_dispatcher_map = {}

@contextlib.contextmanager
def register_dispatcher(disp):
    """
    Register a Dispatcher for inference while it is not yet stored
    as global or closure variable (e.g. during execution of the @jit()
    call).  This allows resolution of recursive calls with eager
    compilation.
    """
    assert callable(disp)
    assert callable(disp.py_func)
    name = disp.py_func.__name__
    _temporary_dispatcher_map[name] = disp
    try:
        yield
    finally:
        del _temporary_dispatcher_map[name]


class TypeInferer(object):
    """
    Operates on block that shares the same ir.Scope.
    """

    def __init__(self, context, func_ir, warnings):
        self.context = context
        self.blocks = func_ir.blocks
        self.generator_info = func_ir.generator_info
        self.func_id = func_ir.func_id

        self.typevars = TypeVarMap()
        self.typevars.set_context(context)
        self.constraints = ConstraintNetwork()
        self.warnings = warnings

        # { index: mangled name }
        self.arg_names = {}
        #self.return_type = None
        # Set of assumed immutable globals
        self.assumed_immutables = set()
        # Track all calls and associated constraints
        self.calls = []
        # The inference result of the above calls
        self.calltypes = utils.UniqueDict()
        # Target var -> constraint with refine hook
        self.refine_map = {}

        if config.DEBUG or config.DEBUG_TYPEINFER:
            self.debug = TypeInferDebug(self)
        else:
            self.debug = NullDebug()

    def _mangle_arg_name(self, name):
        # Disambiguise argument name
        return "arg.%s" % (name,)

    def _get_return_vars(self):
        rets = []
        for blk in utils.itervalues(self.blocks):
            inst = blk.terminator
            if isinstance(inst, ir.Return):
                rets.append(inst.value)
        return rets

    def seed_argument(self, name, index, typ):
        name = self._mangle_arg_name(name)
        self.seed_type(name, typ)
        self.arg_names[index] = name

    def seed_type(self, name, typ):
        """All arguments should be seeded.
        """
        self.lock_type(name, typ, loc=None)

    def seed_return(self, typ):
        """Seeding of return value is optional.
        """
        for var in self._get_return_vars():
            self.lock_type(var.name, typ, loc=None)

    def build_constraint(self):
        for blk in utils.itervalues(self.blocks):
            for inst in blk.body:
                self.constrain_statement(inst)

    def propagate(self):
        newtoken = self.get_state_token()
        oldtoken = None
        # Since the number of types are finite, the typesets will eventually
        # stop growing.
        while newtoken != oldtoken:
            self.debug.propagate_started()
            oldtoken = newtoken
            # Errors can appear when the type set is incomplete; only
            # raise them when there is no progress anymore.
            errors = self.constraints.propagate(self)
            newtoken = self.get_state_token()
            self.debug.propagate_finished()
        if errors:
            raise errors[0]

    def add_type(self, var, tp, loc, unless_locked=False):
        assert isinstance(var, str), type(var)
        tv = self.typevars[var]
        if unless_locked and tv.locked:
            return
        oldty = tv.type
        unified = tv.add_type(tp, loc=loc)
        if unified != oldty:
            self.propagate_refined_type(var, unified)

    def add_calltype(self, inst, signature):
        self.calltypes[inst] = signature

    def copy_type(self, src_var, dest_var, loc):
        unified = self.typevars[dest_var].union(self.typevars[src_var], loc=loc)

    def lock_type(self, var, tp, loc):
        tv = self.typevars[var]
        tv.lock(tp, loc=loc)

    def propagate_refined_type(self, updated_var, updated_type):
        source_constraint = self.refine_map.get(updated_var)
        if source_constraint is not None:
            source_constraint.refine(self, updated_type)

    def unify(self):
        """
        Run the final unification pass over all inferred types, and
        catch imprecise types.
        """
        typdict = utils.UniqueDict()

        def check_var(name):
            tv = self.typevars[name]
            if not tv.defined:
                raise TypingError("Undefined variable '%s'" % (var,))
            tp = tv.getone()
            if not tp.is_precise():
                raise TypingError("Can't infer type of variable '%s': %s" % (var, tp))
            typdict[var] = tp

        # For better error display, check first user-visible vars, then
        # temporaries
        temps = set(k for k in self.typevars if not k[0].isalpha())
        others = set(self.typevars) - temps
        for var in sorted(others):
            check_var(var)
        for var in sorted(temps):
            check_var(var)

        retty = self.get_return_type(typdict)
        fntys = self.get_function_types(typdict)
        if self.generator_info:
            retty = self.get_generator_type(typdict, retty)

        self.debug.unify_finished(typdict, retty, fntys)

        return typdict, retty, fntys

    def get_generator_type(self, typdict, retty):
        gi = self.generator_info
        arg_types = [None] * len(self.arg_names)
        for index, name in self.arg_names.items():
            arg_types[index] = typdict[name]
        state_types = [typdict[var_name] for var_name in gi.state_vars]
        yield_types = [typdict[y.inst.value.name] for y in gi.get_yield_points()]
        if not yield_types:
            raise TypingError("Cannot type generator: it does not yield any value")
        yield_type = self.context.unify_types(*yield_types)
        if yield_type is None:
            raise TypingError("Cannot type generator: cannot unify yielded types "
                              "%s" % (yield_types,))
        return types.Generator(self.func_id.func, yield_type, arg_types,
                               state_types, has_finalizer=True)

    def get_function_types(self, typemap):
        """
        Fill and return the calltypes map.
        """
        # XXX why can't this be done on the fly?
        calltypes = self.calltypes
        for call, constraint in self.calls:
            calltypes[call] = constraint.get_call_signature()
        return calltypes

    def _unify_return_types(self, rettypes):
        if rettypes:
            unified = self.context.unify_types(*rettypes)
            if unified is None or not unified.is_precise():
                raise TypingError("Can't unify return type from the "
                                  "following types: %s"
                                  % ", ".join(sorted(map(str, rettypes))))
            return unified
        else:
            # Function without a successful return path
            return types.none

    def get_return_type(self, typemap):
        rettypes = set()
        for var in self._get_return_vars():
            rettypes.add(typemap[var.name])
        return self._unify_return_types(rettypes)

    def get_state_token(self):
        """The algorithm is monotonic.  It can only grow or "refine" the
        typevar map.
        """
        return [tv.type for name, tv in sorted(self.typevars.items())]

    def constrain_statement(self, inst):
        if isinstance(inst, ir.Assign):
            self.typeof_assign(inst)
        elif isinstance(inst, ir.SetItem):
            self.typeof_setitem(inst)
        elif isinstance(inst, ir.StaticSetItem):
            self.typeof_static_setitem(inst)
        elif isinstance(inst, ir.DelItem):
            self.typeof_delitem(inst)
        elif isinstance(inst, ir.SetAttr):
            self.typeof_setattr(inst)
        elif isinstance(inst, ir.Print):
            self.typeof_print(inst)
        elif isinstance(inst, (ir.Jump, ir.Branch, ir.Return, ir.Del)):
            pass
        elif isinstance(inst, ir.StaticRaise):
            pass
        else:
            raise NotImplementedError(inst)

    def typeof_setitem(self, inst):
        constraint = SetItemConstraint(target=inst.target, index=inst.index,
                                       value=inst.value, loc=inst.loc)
        self.constraints.append(constraint)
        self.calls.append((inst, constraint))

    def typeof_static_setitem(self, inst):
        constraint = StaticSetItemConstraint(target=inst.target,
                                             index=inst.index,
                                             index_var=inst.index_var,
                                             value=inst.value, loc=inst.loc)
        self.constraints.append(constraint)
        self.calls.append((inst, constraint))

    def typeof_delitem(self, inst):
        constraint = DelItemConstraint(target=inst.target, index=inst.index,
                                       loc=inst.loc)
        self.constraints.append(constraint)
        self.calls.append((inst, constraint))

    def typeof_setattr(self, inst):
        constraint = SetAttrConstraint(target=inst.target, attr=inst.attr,
                                       value=inst.value, loc=inst.loc)
        self.constraints.append(constraint)
        self.calls.append((inst, constraint))

    def typeof_print(self, inst):
        constraint = PrintConstraint(args=inst.args, vararg=inst.vararg,
                                     loc=inst.loc)
        self.constraints.append(constraint)
        self.calls.append((inst, constraint))

    def typeof_assign(self, inst):
        value = inst.value
        if isinstance(value, ir.Const):
            self.typeof_const(inst, inst.target, value.value)
        elif isinstance(value, ir.Var):
            self.constraints.append(Propagate(dst=inst.target.name,
                                              src=value.name, loc=inst.loc))
        elif isinstance(value, (ir.Global, ir.FreeVar)):
            self.typeof_global(inst, inst.target, value)
        elif isinstance(value, ir.Arg):
            self.typeof_arg(inst, inst.target, value)
        elif isinstance(value, ir.Expr):
            self.typeof_expr(inst, inst.target, value)
        elif isinstance(value, ir.Yield):
            self.typeof_yield(inst, inst.target, value)
        else:
            raise NotImplementedError(type(value), str(value))

    def resolve_value_type(self, inst, val):
        """
        Resolve the type of a simple Python value, such as can be
        represented by literals.
        """
        try:
            return self.context.resolve_value_type(val)
        except ValueError as e:
            msg = str(e)
        raise TypingError(msg, loc=inst.loc)

    def typeof_arg(self, inst, target, arg):
        src_name = self._mangle_arg_name(arg.name)
        self.constraints.append(ArgConstraint(dst=target.name,
                                              src=src_name,
                                              loc=inst.loc))

    def typeof_const(self, inst, target, const):
        self.lock_type(target.name, self.resolve_value_type(inst, const),
                       loc=inst.loc)

    def typeof_yield(self, inst, target, yield_):
        # Sending values into generators isn't supported.
        self.add_type(target.name, types.none, loc=inst.loc)

    def sentry_modified_builtin(self, inst, gvar):
        """
        Ensure that builtins are not modified.
        """
        if (gvar.name in ('range', 'xrange') and
            gvar.value not in utils.RANGE_ITER_OBJECTS):
            bad = True
        elif gvar.name == 'slice' and gvar.value is not slice:
            bad = True
        elif gvar.name == 'len' and gvar.value is not len:
            bad = True
        else:
            bad = False

        if bad:
            raise TypingError("Modified builtin '%s'" % gvar.name,
                              loc=inst.loc)

    def resolve_call(self, fnty, pos_args, kw_args):
        """
        Resolve a call to a given function type.  A signature is returned.
        """
        if isinstance(fnty, types.RecursiveCall):
            # Recursive call
            disp = fnty.dispatcher_type.dispatcher
            pysig, args = disp.fold_argument_types(pos_args, kw_args)

            # Fetch the return type as given by the user
            rettypes = set()
            frame = self.context.callstack.match(disp.py_func, args)

            # Unify known return types
            for retvar in frame.typeinfer._get_return_vars():
                typevar = frame.typeinfer.typevars[retvar.name]
                if typevar.defined:
                    rettypes.add(typevar.getone())
            # No known return type
            if not rettypes:
                raise TypingError("cannot type infer runaway recursion")

            return_type = self._unify_return_types(rettypes)

            sig = typing.signature(return_type, *args)
            sig.pysig = pysig
            return sig
        else:
            # Normal non-recursive call
            return self.context.resolve_function_type(fnty, pos_args, kw_args)

    def typeof_global(self, inst, target, gvar):
        try:
            typ = self.resolve_value_type(inst, gvar.value)
        except TypingError as e:
            if (gvar.name == self.func_id.func_name
                and gvar.name in _temporary_dispatcher_map):
                # Self-recursion case where the dispatcher is not (yet?) known
                # as a global variable
                typ = types.Dispatcher(_temporary_dispatcher_map[gvar.name])
            else:
                e.patch_message("Untyped global name '%s': %s"
                                % (gvar.name, e))
                raise

        if isinstance(typ, types.Dispatcher) and typ.dispatcher.is_compiling:
            # Recursive call
            callframe = self.context.callstack.findfirst(typ.dispatcher.py_func)
            if callframe is not None:
                typ = types.RecursiveCall(typ, callframe.func_id)
            else:
                raise NotImplementedError(
                    "call to %s: unsupported recursion"
                    % typ.dispatcher)

        if isinstance(typ, types.Array):
            # Global array in nopython mode is constant
            # XXX why layout='C'?
            typ = typ.copy(layout='C', readonly=True)

        self.sentry_modified_builtin(inst, gvar)
        self.lock_type(target.name, typ, loc=inst.loc)
        self.assumed_immutables.add(inst)

    def typeof_expr(self, inst, target, expr):
        if expr.op == 'call':
            if isinstance(expr.func, ir.Intrinsic):
                sig = expr.func.type
                self.add_type(target.name, sig.return_type, loc=inst.loc)
                self.add_calltype(expr, sig)
            else:
                self.typeof_call(inst, target, expr)
        elif expr.op in ('getiter', 'iternext'):
            self.typeof_intrinsic_call(inst, target, expr.op, expr.value)
        elif expr.op == 'exhaust_iter':
            constraint = ExhaustIterConstraint(target.name, count=expr.count,
                                               iterator=expr.value,
                                               loc=expr.loc)
            self.constraints.append(constraint)
        elif expr.op == 'pair_first':
            constraint = PairFirstConstraint(target.name, pair=expr.value,
                                             loc=expr.loc)
            self.constraints.append(constraint)
        elif expr.op == 'pair_second':
            constraint = PairSecondConstraint(target.name, pair=expr.value,
                                              loc=expr.loc)
            self.constraints.append(constraint)
        elif expr.op == 'binop':
            self.typeof_intrinsic_call(inst, target, expr.fn, expr.lhs, expr.rhs)
        elif expr.op == 'inplace_binop':
            self.typeof_intrinsic_call(inst, target, expr.fn,
                                       expr.lhs, expr.rhs)
        elif expr.op == 'unary':
            self.typeof_intrinsic_call(inst, target, expr.fn, expr.value)
        elif expr.op == 'static_getitem':
            constraint = StaticGetItemConstraint(target.name, value=expr.value,
                                                 index=expr.index,
                                                 index_var=expr.index_var,
                                                 loc=expr.loc)
            self.constraints.append(constraint)
            self.calls.append((inst.value, constraint))
        elif expr.op == 'getitem':
            self.typeof_intrinsic_call(inst, target, 'getitem', expr.value, expr.index)
        elif expr.op == 'getattr':
            constraint = GetAttrConstraint(target.name, attr=expr.attr,
                                           value=expr.value, loc=inst.loc,
                                           inst=inst)
            self.constraints.append(constraint)
        elif expr.op == 'build_tuple':
            constraint = BuildTupleConstraint(target.name, items=expr.items,
                                              loc=inst.loc)
            self.constraints.append(constraint)
        elif expr.op == 'build_list':
            constraint = BuildListConstraint(target.name, items=expr.items,
                                             loc=inst.loc)
            self.constraints.append(constraint)
        elif expr.op == 'build_set':
            constraint = BuildSetConstraint(target.name, items=expr.items,
                                            loc=inst.loc)
            self.constraints.append(constraint)
        elif expr.op == 'cast':
            self.constraints.append(Propagate(dst=target.name,
                                              src=expr.value.name,
                                              loc=inst.loc))
        else:
            raise NotImplementedError(type(expr), expr)

    def typeof_call(self, inst, target, call):
        constraint = CallConstraint(target.name, call.func.name, call.args,
                                    call.kws, call.vararg, loc=inst.loc)
        self.constraints.append(constraint)
        self.calls.append((inst.value, constraint))

    def typeof_intrinsic_call(self, inst, target, func, *args):
        constraint = IntrinsicCallConstraint(target.name, func, args,
                                             kws=(), vararg=None, loc=inst.loc)
        self.constraints.append(constraint)
        self.calls.append((inst.value, constraint))


class NullDebug(object):

    def propagate_started(self):
        pass

    def propagate_finished(self):
        pass

    def unify_finished(self, typdict, retty, fntys):
        pass


class TypeInferDebug(object):

    def __init__(self, typeinfer):
        self.typeinfer = typeinfer

    def _dump_state(self):
        print('---- type variables ----')
        pprint([v for k, v in sorted(self.typeinfer.typevars.items())])

    def propagate_started(self):
        print("propagate".center(80, '-'))

    def propagate_finished(self):
        self._dump_state()

    def unify_finished(self, typdict, retty, fntys):
        print("Variable types".center(80, "-"))
        pprint(typdict)
        print("Return type".center(80, "-"))
        pprint(retty)
        print("Call types".center(80, "-"))
        pprint(fntys)
