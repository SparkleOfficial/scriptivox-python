#!/usr/bin/env python3
"""Generate the SDK's types and error classes from the Scriptivox OpenAPI document (stdlib only).

    python scripts/generate.py                 regenerate from the snapshot in openapi/openapi.json
    python scripts/generate.py --fetch [url]   refresh the snapshot first (default: the published document)
    python scripts/generate.py --spec <file>   refresh the snapshot from a local file first
    python scripts/generate.py --check         fail if the generated files differ from the snapshot's output
    python scripts/generate.py --check-live    fail if the snapshot differs from the published document

Outputs (never edit them by hand):
    src/scriptivox/_types.py        a TypedDict per schema, Literal aliases for the enums
    src/scriptivox/_error_codes.py  one exception class per error.code, the async failure codes
"""
from __future__ import annotations

import hashlib
import json
import keyword
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "openapi" / "openapi.json"
PUBLISHED = "https://platform.scriptivox.com/openapi.json"
OUT_TYPES = ROOT / "src" / "scriptivox" / "_types.py"
OUT_ERRORS = ROOT / "src" / "scriptivox" / "_error_codes.py"

#: Names for untitled nested objects whose default name reads badly.
RENAMES = {"ErrorError": "ErrorDetail"}

CATEGORY_BY_STATUS = {
    400: "BadRequestError",
    401: "AuthenticationError",
    402: "PaymentRequiredError",
    403: "PermissionDeniedError",
    404: "NotFoundError",
    405: "MethodNotAllowedError",
    409: "ConflictError",
    410: "GoneError",
    413: "PayloadTooLargeError",
    415: "UnsupportedMediaTypeError",
    422: "UnprocessableEntityError",
    429: "RateLimitError",
    500: "InternalServerError",
    503: "ServiceUnavailableError",
}


def pretty(spec: Any) -> str:
    # Same bytes as the Node SDK's snapshot (JSON.stringify(spec, null, 2) + "\n").
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def fetch(url: str) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "scriptivox-python-generate"})
    with urllib.request.urlopen(req, timeout=60) as res:  # noqa: S310 (fixed https URL or the caller's)
        return json.loads(res.read().decode("utf-8"))


def class_name_for_code(code: str) -> str:
    words = code.lower().split("_")
    if len(words) > 1 and words[-1] == "error":
        words.pop()
    return "".join(w[:1].upper() + w[1:] for w in words) + "Error"


def one_line(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().replace('"""', "'''")


def pascal(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", name) if p)


class TypeGen:
    def __init__(self, spec: Dict[str, Any]) -> None:
        self.schemas: Dict[str, Any] = spec["components"]["schemas"]
        self.blocks: List[str] = []
        self.done: set = set()

    def ref_name(self, ref: str) -> str:
        return ref.rsplit("/", 1)[-1]

    def literal(self, values: List[Any]) -> str:
        return "Literal[" + ", ".join(json.dumps(v) if isinstance(v, str) else repr(v) for v in values) + "]"

    def py_type(self, schema: Dict[str, Any], hint: str) -> str:
        """The annotation for a schema; nested objects become their own TypedDict named `hint`."""
        if "$ref" in schema:
            name = self.ref_name(schema["$ref"])
            self.emit(name, self.schemas[name])
            return name
        for key in ("oneOf", "anyOf"):
            if key in schema:
                parts = [self.py_type(s, f"{hint}{i}") for i, s in enumerate(schema[key])]
                return self.union(parts)
        if "allOf" in schema and len(schema["allOf"]) == 1:
            return self.py_type(schema["allOf"][0], hint)
        if "const" in schema:
            return self.literal([schema["const"]])
        if "enum" in schema:
            return self.literal([v for v in schema["enum"] if v is not None]) if any(v is not None for v in schema["enum"]) else "None"
        types = schema.get("type")
        if isinstance(types, list):
            parts = [self.py_type({**schema, "type": t}, hint) for t in types]
            return self.union(parts)
        if types == "null":
            return "None"
        if types == "string":
            return "str"
        if types == "integer":
            return "int"
        if types == "number":
            return "float"
        if types == "boolean":
            return "bool"
        if types == "array":
            return f"List[{self.py_type(schema.get('items', {}), hint + 'Item')}]"
        if types == "object" or "properties" in schema:
            if schema.get("properties"):
                name = pascal(schema.get("title") or hint)
                name = RENAMES.get(name, name)
                self.emit(name, schema)
                return name
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict):
                return f"Dict[str, {self.py_type(extra, hint + 'Value')}]"
            return "Dict[str, Any]"
        return "Any"

    @staticmethod
    def union(parts: List[str]) -> str:
        uniq: List[str] = []
        for p in parts:
            if p not in uniq:
                uniq.append(p)
        non_null = [p for p in uniq if p != "None"]
        if len(non_null) == len(uniq):
            return uniq[0] if len(uniq) == 1 else f"Union[{', '.join(uniq)}]"
        inner = non_null[0] if len(non_null) == 1 else f"Union[{', '.join(non_null)}]"
        return f"Optional[{inner}]"

    def emit(self, name: str, schema: Dict[str, Any]) -> None:
        if name in self.done:
            return
        self.done.add(name)
        doc = one_line(schema.get("description"))
        if "allOf" in schema and len(schema["allOf"]) > 1:
            bases: List[str] = []
            props: Dict[str, Any] = {}
            required: List[str] = []
            for part in schema["allOf"]:
                if "$ref" in part:
                    base = self.ref_name(part["$ref"])
                    self.emit(base, self.schemas[base])
                    bases.append(base)
                else:
                    props.update(part.get("properties", {}))
                    required += part.get("required", [])
            self.object_block(name, props, required, doc, bases)
            return
        if (schema.get("type") == "object" or "properties" in schema) and not any(k in schema for k in ("oneOf", "anyOf")):
            self.object_block(name, schema.get("properties", {}), schema.get("required", []), doc, [])
            return
        annotation = self.py_type(schema, name + "Value")
        self.blocks.append((f"# {doc}\n" if doc else "") + f"{name} = {annotation}\n")

    def object_block(self, name: str, props: Dict[str, Any], required: List[str], doc: str, bases: List[str]) -> None:
        for key in props:
            if not key.isidentifier() or keyword.iskeyword(key):
                raise SystemExit(f"{name}.{key} is not a valid Python field name; extend the generator")
        fields: List[Tuple[str, str, str, bool]] = []
        for key, sub in props.items():
            annotation = self.py_type(sub, name + pascal(key))
            fields.append((key, annotation, one_line(sub.get("description")), key in required))
        req = [f for f in fields if f[3]]
        opt = [f for f in fields if not f[3]]

        def body(fs: List[Tuple[str, str, str, bool]]) -> str:
            lines = []
            for key, annotation, d, _ in fs:
                if d:
                    lines.append(f"    #: {d}")
                lines.append(f"    {key}: {annotation}")
            return "\n".join(lines) if lines else "    pass"

        docstring = f'    """{doc}"""\n' if doc else ""
        if req and opt:
            base_req = f"_{name}Required"
            parent = ", ".join(bases + ["TypedDict"]) if not bases else ", ".join(bases)
            self.blocks.append(f"class {base_req}({parent}):\n{body(req)}\n")
            self.blocks.append(f"class {name}({base_req}, total=False):\n{docstring}{body(opt)}\n")
        else:
            parent = ", ".join(bases) if bases else "TypedDict"
            total = "" if req or not fields else ", total=False"
            if bases and not req:
                total = ", total=False"
            self.blocks.append(f"class {name}({parent}{total}):\n{docstring}{body(req or opt)}\n")


def build_types(spec: Dict[str, Any], spec_hash: str) -> str:
    gen = TypeGen(spec)
    for name, schema in spec["components"]["schemas"].items():
        gen.emit(name, schema)
    header = (
        "# GENERATED by scripts/generate.py from openapi/openapi.json — do not edit by hand.\n"
        f"# OpenAPI {spec['info']['version']} · sha256 {spec_hash}\n"
        '"""Response and request shapes of the Scriptivox API, as TypedDicts (plain dicts at runtime)."""\n'
        "from __future__ import annotations\n\n"
        "from typing import Any, Dict, List, Literal, Optional, TypedDict, Union\n\n"
    )
    names = sorted(n for n in gen.done)
    return header + "\n\n".join(gen.blocks) + "\n\n__all__ = [\n" + "".join(f'    "{n}",\n' for n in names) + "]\n"


def build_errors(spec: Dict[str, Any], spec_hash: str) -> str:
    prop = spec["components"]["schemas"]["Error"]["properties"]["error"]["properties"]["code"]
    codes: List[str] = prop["enum"]
    status_by_code: Dict[str, int] = prop["x-httpStatusByCode"]
    descriptions: Dict[str, str] = prop.get("x-enumDescriptions", {})
    async_prop = spec["components"]["schemas"]["TranscriptionError"]["properties"]["code"]
    async_codes: List[str] = async_prop["enum"]
    async_desc: Dict[str, str] = async_prop.get("x-enumDescriptions", {})
    categories = set(CATEGORY_BY_STATUS.values())
    used = set()
    classes: List[str] = []
    mapping: List[str] = []
    for code in codes:
        status = status_by_code[code]
        parent = CATEGORY_BY_STATUS.get(status) or ("InternalServerError" if status >= 500 else "APIError")
        used.add(parent)
        name = class_name_for_code(code)
        if name in categories or name == "APIError":
            used.add(name)
            mapping.append(f'    "{code}": {name},')
            continue
        classes.append(f'class {name}({parent}):\n    """``{code}`` (HTTP {status}). {one_line(descriptions.get(code))}"""\n')
        mapping.append(f'    "{code}": {name},')
    imports = sorted(used | {"APIError"})
    async_doc = "\n".join(f"#   {c}: {one_line(async_desc.get(c))}" for c in async_codes)
    exported = [class_name_for_code(c) for c in codes if class_name_for_code(c) not in categories]
    return (
        "# GENERATED by scripts/generate.py from openapi/openapi.json — do not edit by hand.\n"
        f"# OpenAPI {spec['info']['version']} · sha256 {spec_hash}\n"
        '"""One exception class per API ``error.code``, and the codes a job can end with."""\n'
        "from __future__ import annotations\n\n"
        "from typing import Dict, Tuple, Type\n\n"
        "from ._exceptions import (\n" + "".join(f"    {n},\n" for n in imports) + ")\n\n"
        "#: Every ``error.code`` a request can answer with (the API may add more; unknown codes map by HTTP status).\n"
        f"API_ERROR_CODES: Tuple[str, ...] = {tuple(codes)!r}\n\n"
        "#: The HTTP status each code is sent with.\n"
        f"HTTP_STATUS_BY_CODE: Dict[str, int] = {json.dumps(status_by_code)}\n\n"
        "# Codes a job can end with (status \"failed\", error.code). They never come back on the request itself:\n"
        "# wait() raises them as TranscriptionFailedError.\n" + async_doc + "\n"
        f"TRANSCRIPTION_ERROR_CODES: Tuple[str, ...] = {tuple(async_codes)!r}\n\n\n"
        + "\n\n".join(classes)
        + "\n\n#: The class raised for each code.\nERROR_CLASS_BY_CODE: Dict[str, Type[APIError]] = {\n"
        + "\n".join(mapping)
        + "\n}\n\n__all__ = [\n"
        + "".join(f'    "{n}",\n' for n in ["API_ERROR_CODES", "HTTP_STATUS_BY_CODE", "TRANSCRIPTION_ERROR_CODES", "ERROR_CLASS_BY_CODE", *exported])
        + "]\n"
    )


def generate(spec: Dict[str, Any]) -> Dict[Path, str]:
    spec_hash = hashlib.sha256(pretty(spec).encode("utf-8")).hexdigest()[:16]
    return {OUT_TYPES: build_types(spec, spec_hash), OUT_ERRORS: build_errors(spec, spec_hash)}


def arg_after(args: List[str], flag: str) -> Optional[str]:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            return args[i + 1]
    return None


def main(args: List[str]) -> int:
    if "--check-live" in args:
        url = arg_after(args, "--check-live") or PUBLISHED
        if pretty(fetch(url)) != SNAPSHOT.read_text(encoding="utf-8"):
            print(f"openapi/openapi.json differs from {url}. Run: python scripts/generate.py --fetch", file=sys.stderr)
            return 1
        print("snapshot matches the published OpenAPI document")
        return 0
    if "--fetch" in args:
        url = arg_after(args, "--fetch") or PUBLISHED
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(pretty(fetch(url)), encoding="utf-8")
        print(f"snapshot refreshed from {url}")
    elif arg_after(args, "--spec"):
        src = Path(arg_after(args, "--spec") or "")
        SNAPSHOT.write_text(pretty(json.loads(src.read_text(encoding="utf-8"))), encoding="utf-8")
        print(f"snapshot refreshed from {src}")
    spec = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    outputs = generate(spec)
    if "--check" in args:
        stale = [p for p, text in outputs.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
        if stale:
            print("stale generated files: " + ", ".join(str(p.relative_to(ROOT)) for p in stale) + ". Run: python scripts/generate.py", file=sys.stderr)
            return 1
        print("generated files are up to date")
        return 0
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
    print("generated " + ", ".join(str(p.relative_to(ROOT)) for p in outputs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
