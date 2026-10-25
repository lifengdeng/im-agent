"""Dispatch offline acceptance to the fixed private instance's trusted verifier."""
from pathlib import PurePosixPath
import importlib.util
import json

from im_agent.contracts import Rejected


def verify(args):
    from resident_pc import paths, selected, check_release
    _, state = paths(args)
    release, _ = check_release(state, selected(state))
    instance = release / "instance"
    declaration = json.loads((instance / "instance-release.json").read_text(encoding="utf-8"))
    name = declaration.get("offline_resident_verifier", "")
    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or ".." in relative.parts or "\\" in name
            or ":" in name or relative.suffix != ".py"):
        raise Rejected("trusted_instance_verifier_required")
    spec = importlib.util.spec_from_file_location("im_agent_instance_offline_verification", instance.joinpath(*relative.parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify(args)
