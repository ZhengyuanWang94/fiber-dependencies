#!/usr/bin/env python

import functools
import itertools
import contextlib
import weakref

import logging
l = logging.getLogger("angr.sim_state")

import claripy
import ana
from archinfo import arch_from_id
from .misc.ux import deprecated

def arch_overrideable(f):
    @functools.wraps(f)
    def wrapped_f(self, *args, **kwargs):
        if hasattr(self.arch, f.__name__):
            arch_f = getattr(self.arch, f.__name__)
            return arch_f(self, *args, **kwargs)
        else:
            return f(self, *args, **kwargs)
    return wrapped_f

from .state_plugins import default_plugins

# This is a counter for the state-merging symbolic variables
merge_counter = itertools.count()

class SimState(ana.Storable): # pylint: disable=R0904
    """
    The SimState represents the state of a program, including its memory, registers, and so forth.

    :ivar regs:         A convenient view of the state's registers, where each register is a property
    :ivar mem:          A convenient view of the state's memory, a :class:`angr.state_plugins.view.SimMemView`
    :ivar registers:    The state's register file as a flat memory region
    :ivar memory:       The state's memory as a flat memory region
    :ivar se:           The solver engine for this state
    :ivar inspect:      The breakpoint manager, a :class:`angr.state_plugins.inspect.SimInspector`
    :ivar log:          Information about the state's history
    :ivar scratch:      Information about the current execution step
    :ivar posix:        MISNOMER: information about the operating system or environment model
    :ivar libc:         Information about the standard library we are emulating
    :ivar cgc:          Information about the cgc environment
    :ivar uc_manager:   Control of under-constrained symbolic execution
    :ivar unicorn:      Control of the Unicorn Engine
    """

    def __init__(self, project=None, arch=None, plugins=None, memory_backer=None, permissions_backer=None, mode=None, options=None,
                 add_options=None, remove_options=None, special_memory_filler=None, os_name=None):
        self.project = project
        self.arch = arch if arch is not None else project.arch.copy() if project is not None else None

        if type(self.arch) is str:
            self.arch = arch_from_id(self.arch)

        # the options
        if options is None:
            if mode is None:
                l.warning("SimState defaulting to symbolic mode.")
                mode = "symbolic"
            options = o.modes[mode]

        options = set(options)
        if add_options is not None:
            options |= add_options
        if remove_options is not None:
            options -= remove_options
        self.options = options
        self.mode = mode

        # plugins
        self.plugins = { }
        if plugins is not None:
            for n,p in plugins.iteritems():
                self.register_plugin(n, p)

        if not self.has_plugin('memory'):
            # we don't set the memory endness because, unlike registers, it's hard to understand
            # which endness the data should be read

            if o.ABSTRACT_MEMORY in self.options:
                # We use SimAbstractMemory in static mode
                # Convert memory_backer into 'global' region
                if memory_backer is not None:
                    memory_backer = {'global': memory_backer}

                # TODO: support permissions backer in SimAbstractMemory
                self.register_plugin('memory', SimAbstractMemory(memory_backer=memory_backer, memory_id="mem"))
            elif o.FAST_MEMORY in self.options:
                self.register_plugin('memory', SimFastMemory(memory_backer=memory_backer, memory_id="mem"))
            else:
                self.register_plugin('memory', SimSymbolicMemory(memory_backer=memory_backer, permissions_backer=permissions_backer, memory_id="mem"))
        if not self.has_plugin('registers'):
            if o.FAST_REGISTERS in self.options:
                self.register_plugin('registers', SimFastMemory(memory_id="reg", endness=self.arch.register_endness))
            else:
                self.register_plugin('registers', SimSymbolicMemory(memory_id="reg", endness=self.arch.register_endness))

        # OS name
        self.os_name = os_name

        # This is used in static mode as we don't have any constraints there
        self._satisfiable = True

        # states are big, so let's give them UUIDs for ANA right away to avoid
        # extra pickling
        self.make_uuid()

        self.uninitialized_access_handler = None
        self._special_memory_filler = special_memory_filler

        # this is a global condition, applied to all added constraints, memory reads, etc
        self._global_condition = None
        self.ip_constraints = []

    def _ana_getstate(self):
        s = dict(ana.Storable._ana_getstate(self))
        s['plugins'] = { k:v for k,v in s['plugins'].iteritems() if k not in ('inspector', 'regs', 'mem') }
        return s

    def _ana_setstate(self, s):
        ana.Storable._ana_setstate(self, s)
        for p in self.plugins.values():
            p.set_state(self._get_weakref() if not isinstance(p, SimAbstractMemory) else self)
            if p.STRONGREF_STATE:
                p.set_strongref_state(self)

    def _get_weakref(self):
        return weakref.proxy(self)

    def _get_strongref(self):
        return self

    def __repr__(self):
        try:
            ip_str = "%#x" % self.addr
        except (SimValueError, SimSolverModeError):
            ip_str = repr(self.regs.ip)

        return "<SimState @ %s>" % ip_str

    #
    # Easier access to some properties
    #

    @property
    def ip(self):
        """
        Get the instruction pointer expression, trigger SimInspect breakpoints, and generate SimActions.
        Use ``_ip`` to not trigger breakpoints or generate actions.

        :return: an expression
        """
        return self.regs.ip

    @ip.setter
    def ip(self, val):
        self.regs.ip = val

    @property
    def _ip(self):
        """
        Get the instruction pointer expression without triggering SimInspect breakpoints or generating SimActions.

        :return: an expression
        """
        return self.regs._ip

    @_ip.setter
    def _ip(self, val):
        """
        Set the instruction pointer without triggering SimInspect breakpoints or generating SimActions.

        :param val: The new instruction pointer.
        :return:    None
        """

        self.regs._ip = val

    @property
    def addr(self):
        """
        Get the concrete address of the instruction pointer, without triggering SimInspect breakpoints or generating
        SimActions. An integer is returned, or an exception is raised if the instruction pointer is symbolic.

        :return: an int
        """

        return self.se.exactly_int(self.regs._ip)

    #
    # Plugin accessors
    #

    def __getattr__(self, v):
        try:
            return self.get_plugin(v)
        except KeyError:
            raise AttributeError(v)

    @property
    def memory(self):
        return self.get_plugin('memory')

    @property
    def registers(self):
        return self.get_plugin('registers')

    @property
    def se(self):
        return self.get_plugin('solver_engine')

    @property
    def solver(self):
        return self.get_plugin('solver_engine')

    @property
    def inspect(self):
        return self.get_plugin('inspector')

    @property
    def log(self):
        return self.get_plugin('log')

    @property
    def scratch(self):
        return self.get_plugin('scratch')

    @property
    def history(self):
        return self.get_plugin('history')

    @property
    def posix(self):
        return self.get_plugin('posix')

    @property
    def libc(self):
        return self.get_plugin('libc')

    @property
    def cgc(self):
        return self.get_plugin('cgc')

    @property
    def regs(self):
        return self.get_plugin('regs')

    @property
    def mem(self):
        return self.get_plugin('mem')

    @property
    def gdb(self):
        return self.get_plugin('gdb')

    @property
    def globals(self):
        return self.get_plugin('globals')

    @property
    def uc_manager(self):
        return self.get_plugin('uc_manager')

    @property
    def unicorn(self):
        return self.get_plugin('unicorn')

    def _inspect(self, *args, **kwargs):
        if self.has_plugin('inspector'):
            self.inspect.action(*args, **kwargs)

    def _inspect_getattr(self, attr, default_value):
        if self.has_plugin('inspector'):
            if hasattr(self.inspect, attr):
                return getattr(self.inspect, attr)

        return default_value

    #
    # Plugins
    #

    def has_plugin(self, name):
        return name in self.plugins

    def get_plugin(self, name):
        if name not in self.plugins:
            p = default_plugins[name]()
            self.register_plugin(name, p)
            return p
        return self.plugins[name]

    def register_plugin(self, name, plugin):
        #l.debug("Adding plugin %s of type %s", name, plugin.__class__.__name__)
        plugin.set_state(self._get_weakref() if not isinstance(plugin, SimAbstractMemory) else self)
        if plugin.STRONGREF_STATE:
            plugin.set_strongref_state(self)
        self.plugins[name] = plugin
        plugin.init_state()
        return plugin

    def release_plugin(self, name):
        if name in self.plugins:
            del self.plugins[name]

    #
    # Constraint pass-throughs
    #

    def simplify(self, *args):
        """
        Simplify this state's constraints.
        """
        return self.se.simplify(*args)

    def add_constraints(self, *args, **kwargs):
        """
        Add some constraints to the state.

        You may pass in any number of symbolic booleans as variadic positional arguments.
        """
        if len(args) > 0 and isinstance(args[0], (list, tuple)):
            raise Exception("Tuple or list passed to add_constraints!")

        if o.TRACK_CONSTRAINTS in self.options and len(args) > 0:
            if o.SIMPLIFY_CONSTRAINTS in self.options:
                constraints = [ self.simplify(a) for a in args ]
            else:
                constraints = args

            self._inspect('constraints', BP_BEFORE, added_constraints=constraints)
            constraints = self._inspect_getattr("added_constraints", constraints)
            added = self.se.add(*constraints)
            self._inspect('constraints', BP_AFTER)

            # add actions for the added constraints
            if o.TRACK_CONSTRAINT_ACTIONS in self.options:
                for c in added:
                    sac = SimActionConstraint(self, c)
                    self.history.add_action(sac)
        else:
            # preserve the old action logic for when we don't track constraints (why?)
            if (
                'action' in kwargs and kwargs['action'] and
                o.TRACK_CONSTRAINT_ACTIONS in self.options and len(args) > 0
            ):
                for arg in args:
                    if self.se.symbolic(arg):
                        sac = SimActionConstraint(self, arg)
                        self.history.add_action(sac)

        if o.ABSTRACT_SOLVER in self.options and len(args) > 0:
            for arg in args:
                if self.se.is_false(arg):
                    self._satisfiable = False
                    return

                if self.se.is_true(arg):
                    continue

                # `is_true` and `is_false` does not use VSABackend currently (see commits 97a75366 and 2dfba73e in
                # claripy). There is a chance that VSA backend can in fact handle it.
                # Therefore we try to resolve it with VSABackend again
                if claripy.backends.vsa.is_false(arg):
                    self._satisfiable = False
                    return

                if claripy.backends.vsa.is_true(arg):
                    continue

                # It's neither True or False. Let's try to apply the condition

                # We take the argument, extract a list of constrained SIs out of it (if we could, of course), and
                # then replace each original SI the intersection of original SI and the constrained one.

                _, converted = self.se.constraint_to_si(arg)

                for original_expr, constrained_si in converted:
                    if not original_expr.variables:
                        l.error('Incorrect original_expression to replace in add_constraints(). ' +
                                'This is due to defects in VSA logics inside claripy. Please report ' +
                                'to Fish and he will fix it if he\'s free.')
                        continue

                    new_expr = constrained_si
                    self.registers.replace_all(original_expr, new_expr)
                    for _, region in self.memory.regions.items():
                        region.memory.replace_all(original_expr, new_expr)

                    l.debug("SimState.add_constraints: Applied to final state.")
        elif o.SYMBOLIC not in self.options and len(args) > 0:
            for arg in args:
                if self.se.is_false(arg):
                    self._satisfiable = False
                    return

    def satisfiable(self, **kwargs):
        """
        Whether the state's constraints are satisfiable
        """
        if o.ABSTRACT_SOLVER in self.options or o.SYMBOLIC not in self.options:
            extra_constraints = kwargs.pop('extra_constraints', ())
            for e in extra_constraints:
                if self.se.is_false(e):
                    return False

            return self._satisfiable
        else:
            return self.se.satisfiable(**kwargs)

    def downsize(self):
        """
        Clean up after the solver engine. Calling this when a state no longer needs to be solved on will reduce memory
        usage.
        """
        if 'solver_engine' in self.plugins:
            self.se.downsize()

    #
    # State branching operations
    #

    # Returns a dict that is a copy of all the state's plugins
    def _copy_plugins(self):
        return { n: p.copy() for n,p in self.plugins.iteritems() }

    def copy(self):
        """
        Returns a copy of the state.
        """

        if self._global_condition is not None:
            raise SimStateError("global condition was not cleared before state.copy().")

        c_plugins = self._copy_plugins()
        state = SimState(project=self.project, arch=self.arch, plugins=c_plugins, options=self.options, mode=self.mode, os_name=self.os_name)

        state.uninitialized_access_handler = self.uninitialized_access_handler
        state._special_memory_filler = self._special_memory_filler
        state.ip_constraints = self.ip_constraints

        return state

    def merge(self, *others, **kwargs):
        """
        Merges this state with the other states. Returns the merging result, merged state, and the merge flag.

        :param states: the states to merge
        :param merge_conditions: a tuple of the conditions under which each state holds
        :param common_ancestor: a state that represents the common history between the states being merged
        :param plugin_whitelist: a list of plugin names that will be merged. If this option is given and is not None,
                                 any plugin that is not inside this list will not be merged, and will be created as a
                                 fresh instance in the new state.
        :return: (merged state, merge flag, a bool indicating if any merging occured)
        """

        merge_conditions = kwargs.pop('merge_conditions', None)
        common_ancestor = kwargs.pop('common_ancestor', None)
        plugin_whitelist = kwargs.pop('plugin_whitelist', None)

        if len(kwargs) != 0:
            raise ValueError("invalid arguments: %s" % kwargs.keys())

        #HZ: Here we implement a customized state option: IGNORE_MERGE_CONDITIONS
        if merge_conditions is None or o.IGNORE_MERGE_CONDITIONS in self.options:
            # TODO: maybe make the length of this smaller? Maybe: math.ceil(math.log(len(others)+1, 2))
            merge_flag = self.se.BVS("state_merge_%d" % merge_counter.next(), 16)
            merge_values = range(len(others)+1)
            merge_conditions = [ merge_flag == b for b in merge_values ]
        else:
            merge_conditions = [
                (self.se.true if len(mc) == 0 else self.se.And(*mc)) for mc in merge_conditions
            ]

        if len(set(o.arch.name for o in others)) != 1:
            import ipdb; ipdb.set_trace()
            raise SimMergeError("Unable to merge due to different architectures.")

        all_plugins = set(self.plugins.keys()) | set.union(*(set(o.plugins.keys()) for o in others))

        if plugin_whitelist is not None:
            all_plugins = all_plugins.intersection(set(plugin_whitelist))

        merged = self.copy()
        merging_occurred = False

        # fix parent
        merged.history.parent = self.history

        # plugins
        for p in all_plugins:
            our_plugin = merged.plugins[p] if p in merged.plugins else None
            their_plugins = [ (pl.plugins[p] if p in pl.plugins else None) for pl in others ]

            plugin_classes = (
                set([our_plugin.__class__]) | set(pl.__class__ for pl in their_plugins)
            ) - set([None.__class__])
            if len(plugin_classes) != 1:
                raise SimMergeError(
                    "There are differing plugin classes (%s) for plugin %s" % (plugin_classes, p)
                )
            plugin_class = plugin_classes.pop()

            our_filled_plugin = our_plugin if our_plugin is not None else merged.register_plugin(
                p, plugin_class()
            )
            their_filled_plugins = [
                (tp if tp is not None else t.register_plugin(p, plugin_class()))
                for t,tp in zip(others, their_plugins)
            ]

            plugin_state_merged = our_filled_plugin.merge(
                their_filled_plugins, merge_conditions, common_ancestor=(
                    common_ancestor.plugins[p] if
                    (common_ancestor is not None and p in common_ancestor.plugins) else
                    None
                )
            )
            if plugin_state_merged:
                l.debug('Merging occured in %s', p)
                merging_occurred = True

        merged.add_constraints(merged.se.Or(*merge_conditions))
        return merged, merge_conditions, merging_occurred

    def widen(self, *others):
        """
        Perform a widening between self and other states
        :param others:
        :return:
        """

        if len(set(frozenset(o.plugins.keys()) for o in others)) != 1:
            raise SimMergeError("Unable to widen due to different sets of plugins.")
        if len(set(o.arch.name for o in others)) != 1:
            raise SimMergeError("Unable to widen due to different architectures.")

        widened = self.copy()
        widening_occurred = False

        # plugins
        for p in self.plugins:
            if p in ('solver_engine', 'unicorn'):
                continue
            plugin_state_widened = widened.plugins[p].widen([_.plugins[p] for _ in others])
            if plugin_state_widened:
                l.debug('Widening occured in %s', p)
                widening_occurred = True

        return widened, widening_occurred

    #############################################
    ### Accessors for tmps, registers, memory ###
    #############################################

    def reg_concrete(self, *args, **kwargs):
        """
        Returns the contents of a register but, if that register is symbolic,
        raises a SimValueError.
        """
        e = self.registers.load(*args, **kwargs)
        if self.se.symbolic(e):
            raise SimValueError("target of reg_concrete is symbolic!")
        return self.se.any_int(e)

    def mem_concrete(self, *args, **kwargs):
        """
        Returns the contents of a memory but, if the contents are symbolic,
        raises a SimValueError.
        """
        e = self.memory.load(*args, **kwargs)
        if self.se.symbolic(e):
            raise SimValueError("target of mem_concrete is symbolic!")
        return self.se.any_int(e)

    ###############################
    ### Stack operation helpers ###
    ###############################

    @arch_overrideable
    def stack_push(self, thing):
        """
        Push 'thing' to the stack, writing the thing to memory and adjusting the stack pointer.
        """
        # increment sp
        sp = self.regs.sp + self.arch.stack_change
        self.regs.sp = sp
        return self.memory.store(sp, thing, endness=self.arch.memory_endness)

    @arch_overrideable
    def stack_pop(self):
        """
        Pops from the stack and returns the popped thing. The length will be the architecture word size.
        """
        sp = self.regs.sp
        self.regs.sp = sp - self.arch.stack_change
        return self.memory.load(sp, self.arch.bits / 8, endness=self.arch.memory_endness)

    @arch_overrideable
    def stack_read(self, offset, length, bp=False):
        """
        Reads length bytes, at an offset into the stack.

        :param offset:  The offset from the stack pointer.
        :param length:  The number of bytes to read.
        :param bp:      If True, offset from the BP instead of the SP. Default: False.
        """
        sp = self.regs.bp if bp else self.regs.sp
        return self.memory.load(sp+offset, length, endness=self.arch.memory_endness)

    ###############################
    ### Other helpful functions ###
    ###############################

    def make_concrete_int(self, expr):
        if isinstance(expr, (int, long)):
            return expr

        if not self.se.symbolic(expr):
            return self.se.any_int(expr)

        v = self.se.any_int(expr)
        self.add_constraints(expr == v)
        return v

    # This handles the preparation of concrete function launches from abstract functions.
    @arch_overrideable
    def prepare_callsite(self, retval, args, cc='wtf'):
        #TODO
        pass

    def _stack_values_to_string(self, stack_values):
        """
        Convert each stack value to a string

        :param stack_values: A list of values
        :return: The converted string
        """

        strings = [ ]
        for stack_value in stack_values:
            if self.se.symbolic(stack_value):
                concretized_value = "SYMBOLIC - %s" % repr(stack_value)
            else:
                if len(self.se.any_n_int(stack_value, 2)) == 2:
                    concretized_value = repr(stack_value)
                else:
                    concretized_value = repr(stack_value)
            strings.append(concretized_value)

        return " .. ".join(strings)

    def dbg_print_stack(self, depth=None, sp=None):
        """
        Only used for debugging purposes.
        Return the current stack info in formatted string. If depth is None, the
        current stack frame (from sp to bp) will be printed out.
        """

        var_size = self.arch.bits / 8
        sp_sim = self.regs._sp
        bp_sim = self.regs._bp
        if self.se.symbolic(sp_sim) and sp is None:
            result = "SP is SYMBOLIC"
        elif self.se.symbolic(bp_sim) and depth is None:
            result = "BP is SYMBOLIC"
        else:
            sp_value = sp if sp is not None else self.se.any_int(sp_sim)
            if self.se.symbolic(bp_sim):
                result = "SP = 0x%08x, BP is symbolic\n" % (sp_value)
                bp_value = None
            else:
                bp_value = self.se.any_int(bp_sim)
                result = "SP = 0x%08x, BP = 0x%08x\n" % (sp_value, bp_value)
            if depth is None:
                # bp_value cannot be None here
                depth = (bp_value - sp_value) / var_size + 1 # Print one more value
            pointer_value = sp_value
            for i in xrange(depth):
                # For AbstractMemory, we wanna utilize more information from VSA
                stack_values = [ ]

                if o.ABSTRACT_MEMORY in self.options:
                    sp = self.regs._sp
                    segment_sizes = self.memory.get_segments(sp + i * var_size, var_size)

                    pos = i * var_size
                    for segment_size in segment_sizes:
                        stack_values.append(self.stack_read(pos, segment_size, bp=False))
                        pos += segment_size
                else:
                    stack_values.append(self.stack_read(i * var_size, var_size, bp=False))

                # Convert it into a big string!
                val = self._stack_values_to_string(stack_values)

                if pointer_value == sp_value:
                    line = "(sp)% 16x | %s" % (pointer_value, val)
                elif pointer_value == bp_value:
                    line = "(bp)% 16x | %s" % (pointer_value, val)
                else:
                    line = "% 20x | %s" % (pointer_value, val)

                pointer_value += var_size
                result += line + "\n"
        return result

    #
    # Other helper methods
    #

    def set_mode(self, mode):
        self.mode = mode
        self.options = set(o.modes[mode])

    @property
    def thumb(self):
        if not self.arch.name.startswith('ARM'):
            return False

        if self.regs.ip.symbolic:
            # return True when IP can *only* be odd
            new_state = self.copy()
            new_state.add_constraints(new_state.regs.ip % 2 == 1, new_state.regs.ip % 2 != 0)
            return new_state.satisfiable()

        else:
            concrete_ip = self.se.any_int(self.regs.ip)
            return concrete_ip % 2 == 1

    #
    # Some pretty fancy global condition stuff!
    #

    @property
    def with_condition(self):
        @contextlib.contextmanager
        def ctx(c):
            old_condition = self._global_condition
            try:
                new_condition = c if old_condition is None else self.se.And(old_condition, c)
                self._global_condition = new_condition
                yield
            finally:
                self._global_condition = old_condition

        return ctx

    def _adjust_condition(self, c):
        if self._global_condition is None:
            return c
        elif c is None:
            return self._global_condition
        else:
            return self.se.And(self._global_condition, c)

    def _adjust_condition_list(self, conditions):
        if self._global_condition is None:
            return conditions
        elif len(conditions) == 0:
            return conditions.__class__((self._global_condition,))
        else:
            return conditions.__class__((self._adjust_condition(self.se.And(*conditions)),))

    #
    # Compatibility layer
    #

    @property
    @deprecated
    def state(self):
        return self

    @property
    @deprecated
    def length(self):
        return self.history.block_count

    @property
    @deprecated
    def jumpkind(self):
        return self.scratch.jumpkind

    @property
    @deprecated
    def last_actions(self):
        return self.history.recent_actions

    @property
    @deprecated
    def history_iterator(self):
        return self.history.lineage

    @property
    @deprecated
    def addr_trace(self):
        return self.history.addr_trace

    @property
    @deprecated
    def trace(self):
        return self.history.trace

    @property
    @deprecated
    def targets(self):
        return self.history.jump_targets

    @property
    @deprecated
    def guards(self):
        return self.history.jump_guards

    @property
    @deprecated
    def jumpkinds(self):
        return self.history.jumpkinds

    @property
    @deprecated
    def events(self):
        return self.history.events

    @property
    @deprecated
    def actions(self):
        return self.history.actions

    @deprecated
    def trim_history(self):
        self.history.trim()

    @property
    @deprecated
    def reachable(self):
        return self.history.reachable()

    #
    #HZ: I have to add several more member functions here for sim_state.
    #

    #Previously we can use 'loop limiter' as an exploration_technique to prevent angr from dealing with back-edges.
    #The loop limiter assumes that 'path_group' constitutes 'path' and it relies on 'detect_loops()' method of the 'path'
    #to decide whether this path has loop and how many. BUT, current angr has 'SimulationManager' replace 'path_group' and
    #'state' replace 'path', while 'state' doesn't have 'detect_loops()' method...
    import itertools
    def detect_loops(self, n = 3):
        '''
        #A simple method: count the number of basic blocks appearing repeatedly in the 'addr_trace' and return the largest one. 
        addrs = [x for x in self.history.bbl_addrs]
        return max([len(list(g)) for k,g in itertools.groupby(addrs)]) - 1
        '''
        addr_strs = [ "%x"%x for x in self.history.bbl_addrs ]
        if n > len(addr_strs):
            return 0
        bigstr = "".join(addr_strs)
        c = "".join(addr_strs[-n-0:])
        return bigstr.count(c) - 1

from .state_plugins.symbolic_memory import SimSymbolicMemory
from .state_plugins.fast_memory import SimFastMemory
from .state_plugins.abstract_memory import SimAbstractMemory
from .errors import SimMergeError, SimValueError, SimStateError, SimSolverModeError
from .state_plugins.inspect import BP_AFTER, BP_BEFORE
from .state_plugins.sim_action import SimActionConstraint
from . import sim_options as o
