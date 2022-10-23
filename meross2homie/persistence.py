import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Set

from dataclasses_json import DataClassJsonMixin


@dataclass
class Persistence(DataClassJsonMixin):
    devices: Set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path):
        if path.exists():
            with open(path) as f:
                return cls.from_dict(json.load(f))
        return cls()

    def persist(self, path: Path):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
