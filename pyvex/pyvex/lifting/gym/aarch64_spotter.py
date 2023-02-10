
from ..util.lifter_helper import GymratLifter
from ..util.instr_helper import Instruction, ParseError
from ..util import JumpKind, Type
from .. import register
from ...expr import *

l = logging.getLogger(__name__)

class AARCH64Instruction(Instruction):
    #TODO: add some aarch64-specific handling logics here.
    pass

class Instruction_MSRI(AARCH64Instruction):
    name = "MSR"
    bin_format = "1101010100000ppp0100mmmmPPP11111"

    def compute_result(self):
        l.debug("Ignoring MSR instruction at %#x. VEX cannot support this instruction. See pyvex/lifting/gym/aarch64_spotter.py", self.addr)

class Instruction_MSRR(AARCH64Instruction):
    name = "MSR"
    bin_format = "110101010001opppnnnnmmmmPPPttttt"

    def compute_result(self):
        l.debug("Ignoring MSR instruction at %#x. VEX cannot support this instruction. See pyvex/lifting/gym/aarch64_spotter.py", self.addr)

class Instruction_MRS(AARCH64Instruction):
    name = "MRS"
    bin_format = "110101010011opppnnnnmmmmPPPttttt"

    def compute_result(self):
        l.debug("Ignoring MRS instruction at %#x. VEX cannot support this instruction. See pyvex/lifting/gym/aarch64_spotter.py", self.addr)

class AARCH64Spotter(GymratLifter):
    aarch64_instrs = [
        Instruction_MSRI,
        Instruction_MSRR,
        Instruction_MRS,
    ]
    instrs = None

    def lift(self, disassemble=False, dump_irsb=False):
        self.instrs = self.aarch64_instrs
        super(AARCH64Spotter, self).lift(disassemble, dump_irsb)

register(AARCH64Spotter, "AARCH64")
