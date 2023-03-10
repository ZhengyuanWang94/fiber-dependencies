
import logging

from .plugin import SimStatePlugin
import copy

l = logging.getLogger('angr.state_plugins.globals')


class SimStateGlobals(SimStatePlugin):
    def __init__(self, backer=None):
        super(SimStateGlobals, self).__init__()
        self._backer = backer if backer is not None else {}

    def set_state(self, state):
        pass

    def merge(self, others, merge_conditions, common_ancestor=None):

        for other in others:
            for k in other.keys():
                if k not in self:
                    self[k] = other[k]

        return True

    def widen(self, others):
        l.warning("Widening is unimplemented for globals")
        return False

    def __getitem__(self, k):
        return self._backer[k]

    def __setitem__(self, k, v):
        self._backer[k] = v

    def __delitem__(self, k):
        del self._backer[k]

    def __contains__(self, k):
        return k in self._backer

    def keys(self):
        return self._backer.keys()

    def values(self):
        return self._backer.values()

    def items(self):
        return self._backer.items()

    def get(self, k, alt=None):
        return self._backer.get(k, alt)

    #HZ: We must use deepcopy here since 'globals' can hold another dict or other complex objects,
    #one example is the 'loop_ctrs' dict used in veritesting.
    def copy(self):
        return SimStateGlobals(copy.deepcopy(self._backer))

SimStatePlugin.register_default('globals', SimStateGlobals)
