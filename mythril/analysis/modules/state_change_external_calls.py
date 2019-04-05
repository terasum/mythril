from mythril.analysis.swc_data import REENTRANCY
from mythril.analysis.modules.base import DetectionModule
from mythril.analysis.report import Issue
from mythril.laser.smt import symbol_factory, UGT, BitVec
from mythril.laser.ethereum.state.global_state import GlobalState
from mythril.laser.ethereum.state.annotation import StateAnnotation
from mythril.analysis import solver
from mythril.exceptions import UnsatError
from typing import List, cast, Optional
from copy import copy

import logging

log = logging.getLogger(__name__)

DESCRIPTION = """

Check whether there is a state change of the contract after the execution of an external call
"""


class StateChangeCallsAnnotation(StateAnnotation):
    def __init__(self, call_state: GlobalState, user_defined_address: bool) -> None:
        self.call_state = call_state
        self.state_change_states = []  # type: List[GlobalState]
        self.user_defined_address = user_defined_address

    def __copy__(self):
        new_annotation = StateChangeCallsAnnotation(
            self.call_state, self.user_defined_address
        )
        new_annotation.state_change_states = self.state_change_states[:]
        return new_annotation

    def get_issue(self) -> Optional[Issue]:
        if not self.state_change_states:
            return None

        severity = "Medium" if self.user_defined_address else "Low"
        address = self.call_state.get_current_instruction()["address"]
        logging.debug(
            "[EXTERNAL_CALLS] Detected state changes at addresses: {}".format(address)
        )
        description_head = (
            "The contract account state is changed after an external call. "
        )
        description_tail = (
            "Consider that the called contract could re-enter the function before this "
            "state change takes place. This can lead to business logic vulnerabilities."
        )

        return Issue(
            contract=self.call_state.environment.active_account.contract_name,
            function_name=self.call_state.environment.active_function_name,
            address=address,
            title="State change after external call",
            severity=severity,
            description_head=description_head,
            description_tail=description_tail,
            swc_id=REENTRANCY,
            bytecode=self.call_state.environment.code.bytecode,
        )


class StateChange(DetectionModule):
    """This module searches for state change after low level calls (e.g. call.value()) that
    forward gas to the callee."""

    def __init__(self):
        """"""
        super().__init__(
            name="State Change After External calls",
            swc_id=REENTRANCY,
            description=DESCRIPTION,
            entrypoint="callback",
            pre_hooks=[
                "CALL",
                "SSTORE",
                "DELEGATECALL",
                "STATICCALL",
                "CREATE",
                "CALLCODE",
            ],
        )

    def execute(self, state: GlobalState):
        self._issues.extend(self._analyze_state(state))
        return self.issues

    @staticmethod
    def _add_external_call(global_state: GlobalState) -> None:
        gas = global_state.mstate.stack[-1]
        to = global_state.mstate.stack[-2]
        try:
            constraints = copy(global_state.mstate.constraints)
            solver.get_model(
                constraints + [UGT(gas, symbol_factory.BitVecVal(2300, 256))]
            )

            # Check whether we can also set the callee address
            try:
                constraints += [to == 0xDEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEF]
                solver.get_model(constraints)

                global_state.annotate(StateChangeCallsAnnotation(global_state, True))
            except UnsatError:
                global_state.annotate(StateChangeCallsAnnotation(global_state, False))
        except UnsatError:
            pass

    @staticmethod
    def _analyze_state(global_state: GlobalState) -> List[Issue]:

        annotations = cast(
            List[StateChangeCallsAnnotation],
            list(global_state.get_annotations(StateChangeCallsAnnotation)),
        )
        op_code = global_state.get_current_instruction()["opcode"]

        if len(annotations) == 0:
            if op_code in ("SSTORE", "CREATE", "CREATE2"):
                return []
        if op_code in ("SSTORE", "CREATE", "CREATE2"):
            for annotation in annotations:
                annotation.state_change_states.append(global_state)

        # Record state changes following from a transfer of ether
        if op_code in ("CALL", "DELEGATECALL", "CALLCODE"):
            value: BitVec = global_state.mstate.stack[-3]
            if StateChange._balance_change(value, global_state):
                for annotation in annotations:
                    annotation.state_change_states.append(global_state)

        # Record external calls
        if op_code in ("CALL", "DELEGATECALL", "CALLCODE"):
            StateChange._add_external_call(global_state)

        # Check for vulnerabilities
        vulnerabilities = []
        for annotation in annotations:
            if not annotation.state_change_states:
                continue
            vulnerabilities.append(annotation.get_issue())
            global_state.annotations.remove(annotation)
        return vulnerabilities

    @staticmethod
    def _balance_change(value: BitVec, global_state: GlobalState) -> bool:
        if not value.symbolic:
            assert value.value is not None
            return value.value > 0

        else:
            constraints = copy(global_state.mstate.constraints)

            try:
                solver.get_model(
                    constraints + [value > symbol_factory.BitVecVal(0, 256)]
                )
                return True
            except UnsatError:
                return False


detector = StateChange()
