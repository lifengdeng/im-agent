"""Trusted registrations and pinned contracts; events never supply code or paths."""
import copy
import re

from .contracts import Rejected, actor, fingerprint, identifier


def check_schema(schema, depth=0):
    kinds = {"object", "array", "string", "integer", "boolean", "null"}
    if not isinstance(schema, dict) or depth > 8 or schema.get("type") not in kinds:
        raise Rejected("capability_schema_invalid")
    kind = schema["type"]
    allowed = {"type", "enum"} | {"object": {"properties", "required", "additionalProperties"},
        "array": {"items", "minItems", "maxItems"}, "string": {"minLength", "maxLength"},
        "integer": {"minimum", "maximum"}}.get(kind, set())
    if set(schema) - allowed: raise Rejected("unsupported_schema_keyword")
    if kind == "object":
        props = schema.get("properties")
        required = schema.get("required", [])
        if (not isinstance(props, dict) or len(props) > 32 or schema.get("additionalProperties") is not False
                or not isinstance(required, list) or any(not isinstance(v, str) for v in required)
                or len(set(required)) != len(required) or set(required) - set(props)):
            raise Rejected("closed_object_schema_required")
        for key, value in props.items(): identifier(key); check_schema(value, depth + 1)
    elif kind == "array":
        check_schema(schema.get("items"), depth + 1)
        if not 0 <= schema.get("minItems", 0) <= schema.get("maxItems", 32) <= 64:
            raise Rejected("schema_array_budget")
    elif kind == "string":
        if not 0 <= schema.get("minLength", 0) <= schema.get("maxLength", 4000) <= 8192:
            raise Rejected("schema_string_budget")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not 1 <= len(schema["enum"]) <= 64):
        raise Rejected("schema_enum_invalid")


def validate(value, schema, depth=0):
    if depth > 8 or not isinstance(schema, dict):
        raise Rejected("capability_schema_invalid")
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if kind not in types or type(value) is not types[kind]:
        raise Rejected("capability_value_type_invalid")
    if "enum" in schema and value not in schema["enum"]:
        raise Rejected("capability_value_rejected")
    if kind == "object":
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is not False or set(value) - set(properties) or set(schema.get("required", [])) - set(value):
            raise Rejected("capability_object_invalid")
        for key, item in value.items(): validate(item, properties[key], depth + 1)
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 32):
            raise Rejected("capability_array_budget")
        for item in value: validate(item, schema["items"], depth + 1)
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 4000):
            raise Rejected("capability_string_budget")
    elif kind == "integer":
        if not schema.get("minimum", -2**53) <= value <= schema.get("maximum", 2**53):
            raise Rejected("capability_integer_budget")


def echo_descriptor():
    from .contracts import fixture
    return {"project": "example", "capability": "echo", "adapter": "echo-v1", "snapshot": "builtin-v1",
            "actors": [fixture()["actor"]],
            "input_schema": {"type": "object", "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 1000}},
                             "required": ["text"], "additionalProperties": False},
            "result_schema": {"type": "object", "properties": {"verified": {"type": "boolean", "enum": [True]},
                "text": {"type": "string", "minLength": 1, "maxLength": 1000},
                "artifacts": {"type": "array", "maxItems": 0, "items": {"type": "null"}}},
                "required": ["verified", "text", "artifacts"], "additionalProperties": False}}


class Registry:
    def __init__(self, declarations=None):
        self.versions, self.active, self.handlers = {}, {}, {}
        self.contextual = set()
        if declarations is None:
            self.add(echo_descriptor(), lambda args: {"verified": True, "text": args["text"], "artifacts": []})
        else:
            if not isinstance(declarations, dict) or set(declarations) != {"schema", "entries"} or declarations["schema"] != 1:
                raise Rejected("registry_declaration_invalid")
            if not isinstance(declarations["entries"], list) or not 1 <= len(declarations["entries"]) <= 64:
                raise Rejected("registry_entry_budget")
            for entry in declarations["entries"]: self.add(entry)

    def add(self, descriptor, handler=None, *, contextual=False):
        required = {"project", "capability", "adapter", "snapshot", "actors", "input_schema", "result_schema"}
        if not isinstance(descriptor, dict) or not required <= set(descriptor) or set(descriptor) - required - {"expected_result","group_scopes"}:
            raise Rejected("adapter_descriptor_invalid")
        descriptor = copy.deepcopy(descriptor)
        for key in ["project", "capability", "adapter", "snapshot"]: identifier(descriptor[key])
        scopes = descriptor.get('group_scopes', [])
        if not isinstance(scopes,list) or len(scopes)>32:raise Rejected('adapter_group_scope_invalid')
        for scope in scopes:
            if not isinstance(scope,dict) or set(scope)!={'instance','agent','tenant','chat','kind'} or scope['kind']!='group':
                raise Rejected('adapter_group_scope_invalid')
            actor(dict(scope,user='scope-validation'))
        if not isinstance(descriptor["actors"], list) or not (1 if not scopes else 0) <= len(descriptor["actors"]) <= 128:
            raise Rejected("adapter_actor_policy_required")
        for who in descriptor["actors"]: actor(who)
        check_schema(descriptor["input_schema"]); check_schema(descriptor["result_schema"])
        if "expected_result" in descriptor: validate(descriptor["expected_result"], descriptor["result_schema"])
        contract = fingerprint(descriptor)
        self.versions[contract] = descriptor
        self.active[(descriptor["project"], descriptor["capability"])] = contract
        if handler is not None:
            self.handlers[contract] = handler
            if contextual:self.contextual.add(contract)
            else:self.contextual.discard(contract)
        return contract

    @staticmethod
    def permits(descriptor, who):
        # Only transport-verified identity reaches this check. A group scope
        # fixes all boundaries except the individual sender, who is still
        # retained in every event, execution and memory key.
        return who in descriptor['actors'] or any(
            all(who[k]==scope[k] for k in scope) for scope in descriptor.get('group_scopes',[]))

    def declarations(self, *, active_only=True):
        keys = sorted(set(self.active.values()) if active_only else self.versions)
        return {"schema": 1, "entries": [copy.deepcopy(self.versions[k]) for k in keys]}

    @staticmethod
    def policy(descriptor):
        """A host may publish a version, never expand a registered authority."""
        return {k: v for k, v in descriptor.items() if k not in {"snapshot", "expected_result"}}

    def descriptor(self, value, binding=None):
        key = (value["project"], value["capability"])
        contract = self.active.get(key)
        current = self.versions.get(contract)
        if current is None or not self.permits(current,value['actor']):
            raise Rejected("registered_actor_or_capability_rejected")
        if binding is not None:
            contract = binding.get("contract", contract)
        selected = self.versions.get(contract)
        if selected is None or (selected["project"], selected["capability"]) != key or not self.permits(selected,value['actor']):
            raise Rejected("pinned_adapter_contract_missing")
        validate(value["input"], selected["input_schema"])
        return contract, selected

    @staticmethod
    def _binding(contract, selected):
        # Preserve the built-in wire contract for existing offline fixtures.
        if selected == echo_descriptor():
            return {"protocol": 1, "project": "example", "adapter": "echo-v1", "snapshot": "builtin-v1"}
        return {"protocol": 1, **{k: selected[k] for k in ["project", "capability", "adapter", "snapshot"]}, "contract": contract}

    def binding(self, value):
        contract, selected = self.descriptor(value)
        return self._binding(contract, selected)

    def verify_binding(self, value, binding):
        contract, selected = self.descriptor(value, binding)
        if self._binding(contract, selected) != binding:
            raise Rejected("host_adapter_binding_rejected")
        return contract, selected

    def result(self, value, binding, result):
        _, selected = self.verify_binding(value, binding)
        validate(result, selected["result_schema"])
        if selected == echo_descriptor():
            if result != {"verified": True, "text": value["input"]["text"], "artifacts": []}:
                raise Rejected("adapter_verification_failed")
        elif "expected_result" in selected and result != selected["expected_result"]:
            raise Rejected("adapter_verification_failed")
        return result

    def execute(self, value, binding, fallback=None):
        contract, selected = self.verify_binding(value, binding)
        handler = self.handlers.get(contract)
        if fallback is not None:
            if selected != echo_descriptor(): raise Rejected("unregistered_callback_rejected")
            handler = fallback
        if handler is None: raise Rejected("local_adapter_handler_missing")
        return self.result(value, binding, handler(copy.deepcopy(value if contract in self.contextual else value['input'])))
