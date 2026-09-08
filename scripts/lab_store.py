"""Lab workers coexist under platform locks; they never recover other workers."""
from note_bridge.storage import Store


class LabStore(Store):
    def recover_interrupted(self) -> None:
        # Recovery belongs to an explicit stopped-lab audit. The desktop Store
        # retains its normal startup recovery; no global class is monkeypatched.
        pass
