import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from dataclasses_json import DataClassJsonMixin


@dataclass
class DeviceProps(DataClassJsonMixin):
    ip_address: Optional[str] = None


@dataclass
class Persistence(DataClassJsonMixin):
    devices: Dict[str, DeviceProps] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path):
        if path.exists():
            with open(path) as f:
                j = json.load(f)
                if "devices" in j and not isinstance(j["devices"], dict):
                    j["devices"] = {i: DeviceProps() for i in j["devices"]}
                return cls.from_dict(j)
        return cls()

    def persist(self, path: Path):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
