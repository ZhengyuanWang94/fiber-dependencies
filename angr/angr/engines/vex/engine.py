import sys
from cachetools import LRUCache

import pyvex
import claripy
from archinfo import ArchARM

from ... import sim_options as o
from ...state_plugins.inspect import BP_AFTER, BP_BEFORE
from ...state_plugins.sim_action import SimActionExit, SimActionObject
from ...errors import (SimError, SimIRSBError, SimSolverError, SimMemoryAddressError, SimReliftException,
                       UnsupportedDirtyError, SimTranslationError, SimEngineError, SimSegfaultError,
                       SimMemoryError)
from ..engine import SimEngine
from .statements import translate_stmt
from .expressions import translate_expr

#HZ: newly imported
from ...state_plugins.sim_action_object import _raw_ast

import logging
l = logging.getLogger("angr.engines.vex.engine")

#pylint: disable=arguments-differ

VEX_IRSB_MAX_SIZE = 400
VEX_IRSB_MAX_INST = 99

class SimEngineVEX(SimEngine):
    """
    Execution engine based on VEX, Valgrind's IR.
    """

    def __init__(self, stop_points=None,
            use_cache=True,
            cache_size=10000,
            default_opt_level=1,
            support_selfmodifying_code=False,
            single_step=False):

        super(SimEngineVEX, self).__init__()

        self._stop_points = stop_points
        self._use_cache = use_cache
        self._default_opt_level = default_opt_level
        self._support_selfmodifying_code = support_selfmodifying_code
        self._single_step = single_step
        self._cache_size = cache_size

        self._block_cache = None
        self._cache_hit_count = 0
        self._cache_miss_count = 0

        self._initialize_block_cache()


    def _initialize_block_cache(self):
        self._block_cache = LRUCache(maxsize=self._cache_size)
        self._cache_hit_count = 0
        self._cache_miss_count = 0

    def process(self, state,
            irsb=None,
            skip_stmts=0,
            last_stmt=99999999,
            whitelist=None,
            inline=False,
            force_addr=None,
            insn_bytes=None,
            size=None,
            num_inst=None,
            traceflags=0,
            thumb=False,
            opt_level=None,
            **kwargs):
        """
        :param state:       The state with which to execute
        :param irsb:        The PyVEX IRSB object to use for execution. If not provided one will be lifted.
        :param skip_stmts:  The number of statements to skip in processing
        :param last_stmt:   Do not execute any statements after this statement
        :param whitelist:   Only execute statements in this set
        :param inline:      This is an inline execution. Do not bother copying the state.
        :param force_addr:  Force execution to pretend that we're working at this concrete address

        :param thumb:           Whether the block should be lifted in ARM's THUMB mode.
        :param opt_level:       The VEX optimization level to use.
        :param insn_bytes:      A string of bytes to use for the block instead of the project.
        :param size:            The maximum size of the block, in bytes.
        :param num_inst:        The maximum number of instructions.
        :param traceflags:      traceflags to be passed to VEX. (default: 0)
        :returns:           A SimSuccessors object categorizing the block's successors
        """
        return super(SimEngineVEX, self).process(state, irsb,
                skip_stmts=skip_stmts,
                last_stmt=last_stmt,
                whitelist=whitelist,
                inline=inline,
                force_addr=force_addr,
                insn_bytes=insn_bytes,
                size=size,
                num_inst=num_inst,
                traceflags=traceflags,
                thumb=thumb,
                opt_level=opt_level)

    def _check(self, state, *args, **kwargs):
        return True

    def _process(self, state, successors, irsb=None, skip_stmts=0, last_stmt=99999999, whitelist=None, insn_bytes=None, size=None, num_inst=None, traceflags=0, thumb=False, opt_level=None):
        successors.sort = 'IRSB'
        successors.description = 'IRSB'
        state.history.recent_block_count = 1
        state.scratch.guard = claripy.true
        state.scratch.sim_procedure = None
        addr = successors.addr

        state._inspect('irsb', BP_BEFORE, address=addr)
        while True:
            if irsb is None:
                irsb = self.lift(
                    addr=addr,
                    state=state,
                    insn_bytes=insn_bytes,
                    size=size,
                    num_inst=num_inst,
                    traceflags=traceflags,
                    thumb=thumb,
                    opt_level=opt_level)

            if irsb.size == 0:
                raise SimIRSBError("Empty IRSB passed to SimIRSB.")

            # check permissions, are we allowed to execute here? Do we care?
            if o.STRICT_PAGE_ACCESS in state.options:
                try:
                    perms = state.memory.permissions(addr)
                except (KeyError, SimMemoryError):  # TODO: can this still raise KeyError?
                    raise SimSegfaultError(addr, 'exec-miss')
                else:
                    if not perms.symbolic:
                        perms = state.se.any_int(perms)
                        if not perms & 4 and o.ENABLE_NX in state.options:
                            raise SimSegfaultError(addr, 'non-executable')

            state.scratch.tyenv = irsb.tyenv
            state.scratch.irsb = irsb

            try:
                self._handle_irsb(state, successors, irsb, skip_stmts, last_stmt, whitelist)
            except SimReliftException as e:
                state = e.state
                if insn_bytes is not None:
                    raise SimEngineError("You cannot pass self-modifying code as insn_bytes!!!")
                new_ip = state.scratch.ins_addr
                if size is not None:
                    size -= new_ip - addr
                if num_inst is not None:
                    num_inst -= state.scratch.num_insns
                addr = new_ip

                # clear the stage before creating the new IRSB
                state.scratch.dirty_addrs.clear()
                irsb = None

            except SimError as ex:
                ex.record_state(state)
                raise
            else:
                break
        state._inspect('irsb', BP_AFTER)

        successors.processed = True

    def _handle_irsb(self, state, successors, irsb, skip_stmts, last_stmt, whitelist):
        # shortcut. we'll be typing this a lot
        ss = irsb.statements
        num_stmts = len(ss)

        # fill in artifacts
        successors.artifacts['irsb'] = irsb
        successors.artifacts['irsb_size'] = irsb.size
        successors.artifacts['irsb_direct_next'] = irsb.direct_next
        successors.artifacts['irsb_default_jumpkind'] = irsb.jumpkind

        insn_addrs = [ ]

        # if we've told the block to truncate before it ends, it will definitely have a default
        # exit barring errors
        has_default_exit = num_stmts <= last_stmt

        # This option makes us only execute the last four instructions
        if o.SUPER_FASTPATH in state.options:
            imark_counter = 0
            for i in xrange(len(ss) - 1, -1, -1):
                if type(ss[i]) is pyvex.IRStmt.IMark:
                    imark_counter += 1
                if imark_counter >= 4:
                    skip_stmts = max(skip_stmts, i)
                    break

        #HZ: Current BP_AFTER instruction breakpoint is not well implemented, since it relies on the beginning of next instruction.
        #This will cause it fails to capture the instruction at the very end that has no successors. I will implement the true BP_AFTER
        #breakpoint that relies on the end statement of *this* instruction, not the initial IMark of *next* instruction.
        for stmt_idx, stmt in enumerate(ss):
            if isinstance(stmt, pyvex.IRStmt.IMark):
                #HZ: Part 1 of our 'real instruction BP_AFTER breakpoint'
                if insn_addrs:
                    state._inspect('instruction', BP_AFTER, instruction=insn_addrs[-1])
                insn_addrs.append(stmt.addr + stmt.delta)

            if stmt_idx < skip_stmts:
                l.debug("Skipping statement %d", stmt_idx)
                continue
            if last_stmt is not None and stmt_idx > last_stmt:
                l.debug("Truncating statement %d", stmt_idx)
                continue
            if whitelist is not None and stmt_idx not in whitelist:
                l.debug("Blacklisting statement %d", stmt_idx)
                continue

            try:
                state.scratch.stmt_idx = stmt_idx
                state._inspect('statement', BP_BEFORE, statement=stmt_idx)
                self._handle_statement(state, successors, stmt)
                state._inspect('statement', BP_AFTER)
            except UnsupportedDirtyError:
                if o.BYPASS_UNSUPPORTED_IRDIRTY not in state.options:
                    raise
                if stmt.tmp not in (0xffffffff, -1):
                    retval_size = state.scratch.tyenv.sizeof(stmt.tmp)
                    retval = state.se.Unconstrained("unsupported_dirty_%s" % stmt.cee.name, retval_size)
                    state.scratch.store_tmp(stmt.tmp, retval, None, None)
                state.history.add_event('resilience', resilience_type='dirty', dirty=stmt.cee.name,
                                    message='unsupported Dirty call')
            except (SimSolverError, SimMemoryAddressError):
                l.warning("%#x hit an error while analyzing statement %d", successors.addr, stmt_idx, exc_info=True)
                has_default_exit = False
                break

            #HZ: Part 2 of our 'real instruction BP_AFTER breakpoint'.
            if stmt_idx >= len(ss)-1 and insn_addrs:
                #HZ: Deal with the instruction at the end.
                state._inspect('instruction', BP_AFTER, instruction=insn_addrs[-1])

        state.scratch.stmt_idx = num_stmts

        successors.artifacts['insn_addrs'] = insn_addrs

        # If there was an error, and not all the statements were processed,
        # then this block does not have a default exit. This can happen if
        # the block has an unavoidable "conditional" exit or if there's a legitimate
        # error in the simulation
        if has_default_exit:
            l.debug("%s adding default exit.", self)

            try:
                next_expr = translate_expr(irsb.next, state)
                state.history.extend_actions(next_expr.actions)

                if o.TRACK_JMP_ACTIONS in state.options:
                    target_ao = SimActionObject(
                        next_expr.expr,
                        reg_deps=next_expr.reg_deps(), tmp_deps=next_expr.tmp_deps()
                    )
                    state.history.add_action(SimActionExit(state, target_ao, exit_type=SimActionExit.DEFAULT))

                successors.add_successor(state, next_expr.expr, state.scratch.guard, irsb.jumpkind,
                                         exit_stmt_idx='default', exit_ins_addr=state.scratch.ins_addr)

            except KeyError:
                # For some reason, the temporary variable that the successor relies on does not exist.
                # It can be intentional (e.g. when executing a program slice)
                # We save the current state anyways
                successors.unsat_successors.append(state)
                l.debug("The temporary variable for default exit of %s is missing.", self)
        else:
            l.debug("%s has no default exit", self)

        # do return emulation and calless stuff
        for exit_state in list(successors.all_successors):
            exit_jumpkind = exit_state.history.jumpkind
            if exit_jumpkind is None: exit_jumpkind = ""

            if o.CALLLESS in state.options and exit_jumpkind == "Ijk_Call":
                exit_state.registers.store(
                    exit_state.arch.ret_offset,
                    exit_state.se.Unconstrained('fake_ret_value', exit_state.arch.bits)
                )
                exit_state.scratch.target = exit_state.se.BVV(
                    successors.addr + irsb.size, exit_state.arch.bits
                )
                exit_state.history.jumpkind = "Ijk_Ret"
                exit_state.regs.ip = exit_state.scratch.target
                #HZ: We fix another Angr bug here, when we: (1) specify the CALLLESS option and (2) the call addr is unconstrained (e.g. blr x3)
                #HZ: then the successors (pointing to the call target) generated in 'successors.py' will be 'unconstrained'. And finally at here
                #HZ: we basically want to skip this call and return to next instruction directly, we modified the ip, but the problem is the
                #HZ: modified successor is still in 'unconstrained' queue (regardless that the 'ip' hasn't been the unconstrained call target any more).
                #HZ: Remove the successor from 'unconstrained' queue and add it to 'flat' queue.
                if successors.unconstrained_successors.count(exit_state) > 0:
                    successors.unconstrained_successors.remove(exit_state)
                    successors.flat_successors.append(exit_state)
                    successors.successors.append(exit_state)

            elif o.DO_RET_EMULATION in exit_state.options and \
                (exit_jumpkind == "Ijk_Call" or exit_jumpkind.startswith('Ijk_Sys')):
                l.debug("%s adding postcall exit.", self)

                ret_state = exit_state.copy()
                guard = ret_state.se.true if o.TRUE_RET_EMULATION_GUARD in state.options else ret_state.se.false
                target = ret_state.se.BVV(successors.addr + irsb.size, ret_state.arch.bits)
                if ret_state.arch.call_pushes_ret and not exit_jumpkind.startswith('Ijk_Sys'):
                    ret_state.regs.sp = ret_state.regs.sp + ret_state.arch.bytes
                successors.add_successor(
                    ret_state, target, guard, 'Ijk_FakeRet', exit_stmt_idx='default',
                    exit_ins_addr=state.scratch.ins_addr
                )

        if whitelist and successors.is_empty:
            # If statements of this block are white-listed and none of the exit statement (not even the default exit) is
            # in the white-list, successors will be empty, and there is no way for us to get the final state.
            # To this end, a final state is manually created
            l.debug('Add an incomplete successor state as the result of an incomplete execution due to the white-list.')
            successors.flat_successors.append(state)

    def _handle_statement(self, state, successors, stmt):
        """
        This function receives an initial state and imark and processes a list of pyvex.IRStmts
        It annotates the request with a final state, last imark, and a list of SimIRStmts
        """
        if type(stmt) == pyvex.IRStmt.IMark:
            ins_addr = stmt.addr + stmt.delta
            state.scratch.ins_addr = ins_addr

            # Raise an exception if we're suddenly in self-modifying code
            for subaddr in xrange(stmt.len):
                if subaddr + stmt.addr in state.scratch.dirty_addrs:
                    raise SimReliftException(state)
            #HZ: I have made a better BP_AFTER in _handle_irsb, so no need for this.
            #state._inspect('instruction', BP_AFTER)

            l.debug("IMark: %#x", stmt.addr)
            state.scratch.num_insns += 1
            state._inspect('instruction', BP_BEFORE, instruction=ins_addr)
            #HZ: Try to record ins_addr here in state's 'history' plugin, currently the 'ins_addr' field in 'history' plugin is never used.
            state.history.recent_ins_addrs.append(ins_addr)

        # process it!
        s_stmt = translate_stmt(stmt, state)
        if s_stmt is not None:
            state.history.extend_actions(s_stmt.actions)

        # for the exits, put *not* taking the exit on the list of constraints so
        # that we can continue on. Otherwise, add the constraints
        if type(stmt) == pyvex.IRStmt.Exit:
            l.debug("%s adding conditional exit", self)

            # Produce our successor state!
            # Let SimSuccessors.add_successor handle the nitty gritty details
            exit_state = state.copy()
            #-------------------------------------------------------------------------------------------------------
            #HZ: this is the place to honor the state option 'IGNORE_EXIT_GUARDS', which will not take any effect in current implementation IMO.
            #My plan is that if this option is present, then I'll replace original 'guard' with a special one, we cannot:
            #(1) make it 'None' because later code 'add_successor()' will assume a non-None guard.
            #(2) just use 'add_guard=False' parameter in 'add_successor()' because this only makes sense for symbolic guard.
            #NOTE: the replacement should be done for all kinds of exit and all branches.
            #-------------------------------------------------------------------------------------------------------
            #HZ: Here we use a simple heuristic, if the guard is symbolic, we assume that it has the potential to go all branches.
            #TODO: this may not be precise, we may need to replace it with 'not is_false() and not is_true()'. By using 'symbolic',
            #we have the performance gain, but may have some path loss.
            dbg = False
            if dbg:
                print '+++++++++++++++++++++++++++++++++++++++++++++++++'
                print 'I am at %x' % state.history.recent_ins_addrs[-1]
            if o.IGNORE_EXIT_GUARDS not in state.options:
                #Here is the original logic
                if dbg:
                    print 'Do nothing, guard %s, target: %s' % (str(s_stmt.guard),str(s_stmt.target))
                successors.add_successor(exit_state, s_stmt.target, s_stmt.guard, s_stmt.jumpkind,
                                         exit_stmt_idx=state.scratch.stmt_idx, exit_ins_addr=state.scratch.ins_addr)

                # Do our bookkeeping on the continuing state
                cont_condition = claripy.Not(s_stmt.guard)
                state.add_constraints(cont_condition)
                state.scratch.guard = claripy.And(state.scratch.guard, cont_condition)
            else:
                #HZ: I wanna be free.
                #We have some code inside 'add_successor' function to deal with 'IGNORE_EXIT_GUARDS', so no special things to do here.
                successors.add_successor(exit_state, s_stmt.target, s_stmt.guard, s_stmt.jumpkind,
                                         exit_stmt_idx=state.scratch.stmt_idx, exit_ins_addr=state.scratch.ins_addr)
                #HZ: what about the continuing state?
                #(1) We don't add the constraints to the state.
                #(2) But we still need to provide real constraint in the 'scratch', because later it will be used in 'add_successor' to add
                #this continuing successor, and we need to record this real constraint in the 'exit' breakpoint.
                state.scratch.guard = claripy.And(state.scratch.guard, claripy.Not(s_stmt.guard))
            if dbg:
                print '----------------------------------------------------'

    def lift(self,
            state=None,
            clemory=None,
            insn_bytes=None,
            arch=None,
            addr=None,
            size=None,
            num_inst=None,
            traceflags=0,
            thumb=False,
            opt_level=None):

        """
        Lift an IRSB.

        There are many possible valid sets of parameters. You at the very least must pass some
        source of data, some source of an architecture, and some source of an address.

        Sources of data in order of priority: insn_bytes, clemory, state

        Sources of an address, in order of priority: addr, state

        Sources of an architecture, in order of priority: arch, clemory, state

        :param state:           A state to use as a data source.
        :param clemory:         A cle.memory.Clemory object to use as a data source.
        :param addr:            The address at which to start the block.
        :param thumb:           Whether the block should be lifted in ARM's THUMB mode.
        :param opt_level:       The VEX optimization level to use. The final IR optimization level is determined by
                                (ordered by priority):
                                - Argument opt_level
                                - opt_level is set to 1 if OPTIMIZE_IR exists in state options
                                - self._default_opt_level
        :param insn_bytes:      A string of bytes to use as a data source.
        :param size:            The maximum size of the block, in bytes.
        :param num_inst:        The maximum number of instructions.
        :param traceflags:      traceflags to be passed to VEX. (default: 0)
        """
        # phase 0: sanity check
        if not state and not clemory and not insn_bytes:
            raise ValueError("Must provide state or clemory or insn_bytes!")
        if not state and not clemory and not arch:
            raise ValueError("Must provide state or clemory or arch!")
        if addr is None and not state:
            raise ValueError("Must provide state or addr!")
        if arch is None:
            arch = clemory._arch if clemory else state.arch
        if arch.name.startswith("MIPS") and self._single_step:
            l.error("Cannot specify single-stepping on MIPS.")
            self._single_step = False

        # phase 1: parameter defaults
        if addr is None:
            addr = state.se.any_int(state._ip)
        if size is not None:
            size = min(size, VEX_IRSB_MAX_SIZE)
        if size is None:
            size = VEX_IRSB_MAX_SIZE
        if num_inst is not None:
            num_inst = min(num_inst, VEX_IRSB_MAX_INST)
        if num_inst is None and self._single_step:
            num_inst = 1
        if opt_level is None:
            if state and o.OPTIMIZE_IR in state.options:
                opt_level = 1
            else:
                opt_level = self._default_opt_level
        if self._support_selfmodifying_code:
            if opt_level > 0:
                l.warning("Self-modifying code is not always correctly optimized by PyVEX. To guarantee correctness, VEX optimizations have been disabled.")
                opt_level = 0
                if state and o.OPTIMIZE_IR in state.options:
                    state.options.remove(o.OPTIMIZE_IR)

        # phase 2: thumb normalization
        thumb = int(thumb)
        if isinstance(arch, ArchARM):
            if addr % 2 == 1:
                thumb = 1
            if thumb:
                addr &= ~1
        elif thumb:
            l.error("thumb=True passed on non-arm architecture!")
            thumb = 0

        # phase 3: check cache
        cache_key = (addr, insn_bytes, size, num_inst, thumb, opt_level)
        if self._use_cache and cache_key in self._block_cache:
            self._cache_hit_count += 1
            irsb = self._block_cache[cache_key]
            stop_point = self._first_stoppoint(irsb)
            if stop_point is None:
                return irsb
            else:
                size = stop_point - addr
                # check the cache again
                cache_key = (addr, insn_bytes, size, num_inst, thumb, opt_level)
                if cache_key in self._block_cache:
                    self._cache_hit_count += 1
                    return self._block_cache[cache_key]
                else:
                    self._cache_miss_count += 1
        else:
            self._cache_miss_count += 1

        # phase 4: get bytes
        if insn_bytes is not None:
            buff, size = insn_bytes, len(insn_bytes)
        else:
            buff, size = self._load_bytes(addr, size, state, clemory)

        if not buff or size == 0:
            raise SimEngineError("No bytes in memory for block starting at %#x." % addr)

        # phase 5: call into pyvex
        l.debug("Creating pyvex.IRSB of arch %s at %#x", arch.name, addr)
        try:
            for subphase in xrange(2):
                irsb = pyvex.IRSB(buff, addr + thumb, arch,
                                  num_bytes=size,
                                  num_inst=num_inst,
                                  bytes_offset=thumb,
                                  traceflags=traceflags,
                                  opt_level=opt_level)

                if subphase == 0:
                    # check for possible stop points
                    stop_point = self._first_stoppoint(irsb)
                    if stop_point is not None:
                        size = stop_point - addr
                        continue

                if self._use_cache:
                    self._block_cache[cache_key] = irsb
                return irsb

        # phase x: error handling
        except pyvex.PyVEXError:
            l.debug("VEX translation error at %#x", addr)
            if isinstance(buff, str):
                l.debug('Using bytes: ' + buff)
            else:
                l.debug("Using bytes: " + str(pyvex.ffi.buffer(buff, size)).encode('hex'))
            e_type, value, traceback = sys.exc_info()
            raise SimTranslationError, ("Translation error", e_type, value), traceback

    def _load_bytes(self, addr, max_size, state=None, clemory=None):
        if not clemory:
            if state is None:
                raise SimEngineError('state and clemory cannot both be None in _load_bytes().')
            if o.ABSTRACT_MEMORY in state.options:
                # abstract memory
                clemory = state.memory.regions['global'].memory.mem._memory_backer
            else:
                # symbolic memory
                clemory = state.memory.mem._memory_backer

        buff, size = "", 0

        # Load from the clemory if we can
        smc = self._support_selfmodifying_code
        if state:
            try:
                p = state.memory.permissions(addr)
                if p.symbolic:
                    smc = True
                else:
                    smc = claripy.is_true(p & 2 != 0)
            except: # pylint: disable=bare-except
                smc = True # I don't know why this would ever happen, we checked this right?

        if not smc or not state:
            try:
                buff, size = clemory.read_bytes_c(addr)
            except KeyError:
                pass

        # If that didn't work, try to load from the state
        if size == 0 and state:
            if addr in state.memory and addr + max_size - 1 in state.memory:
                buff = state.se.any_str(state.memory.load(addr, max_size, inspect=False))
                size = max_size
            else:
                good_addrs = []
                for i in xrange(max_size):
                    if addr + i in state.memory:
                        good_addrs.append(addr + i)
                    else:
                        break

                buff = ''.join(chr(state.se.any_int(state.memory.load(i, 1, inspect=False))) for i in good_addrs)
                size = len(buff)

        size = min(max_size, size)
        return buff, size

    def _first_stoppoint(self, irsb):
        """
        Enumerate the imarks in the block. If any of them (after the first one) are at a stop point, returns the address
        of the stop point. None is returned otherwise.
        """
        if self._stop_points is None:
            return None

        first_imark = True
        for stmt in irsb.statements:
            if isinstance(stmt, pyvex.stmt.IMark):
                addr = stmt.addr + stmt.delta
                if not first_imark and addr in self._stop_points:
                    # could this part be moved by pyvex?
                    return addr

                first_imark = False
        return None

    def clear_cache(self):
        self._block_cache = LRUCache(maxsize=self._cache_size)

        self._cache_hit_count = 0
        self._cache_miss_count = 0

    #
    # Pickling
    #

    def __setstate__(self, state):
        super(SimEngineVEX, self).__setstate__(state)

        self._stop_points = state['_stop_points']
        self._use_cache = state['_use_cache']
        self._default_opt_level = state['_default_opt_level']
        self._support_selfmodifying_code = state['_support_selfmodifying_code']
        self._single_step = state['_single_step']
        self._cache_size = state['_cache_size']

        # rebuild block cache
        self._initialize_block_cache()

    def __getstate__(self):
        s = super(SimEngineVEX, self).__getstate__()

        s['_stop_points'] = self._stop_points
        s['_use_cache'] = self._use_cache
        s['_default_opt_level'] = self._default_opt_level
        s['_support_selfmodifying_code'] = self._support_selfmodifying_code
        s['_single_step'] = self._single_step
        s['_cache_size'] = self._cache_size

        return s
