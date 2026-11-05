"""Read-only, same-method before/after inventory; no imports of the application.

Run from the repository root with .venv/bin/python and redirect stdout as wanted.
Counts are literal splitlines (comments/blanks included), not complexity metrics.
The baseline is main immediately before the first epic bugfix merge (#431/#408).
"""
import ast
import json
import subprocess

BASE = "aaebe1a817b1f9c57fc2f423f5681806027b9c8f"
FINAL = "92a06736e1eab6536b448da8b47e8375f1bee827"
HTTP = {"get", "post", "put", "patch", "delete", "head", "options"}


def git(*args):
    return subprocess.check_output(["git", *args])


def inventory(ref):
    paths = git("ls-tree", "-r", "--name-only", ref).decode().splitlines()
    groups = {name: 0 for name in ("app_root_python", "app_services_python", "frontend_src", "tests_python")}
    operations = set()
    routes = set()
    for path in paths:
        group = None
        if path.startswith("app/") and path.endswith(".py"):
            group = "app_services_python" if path.startswith("app/services/") else "app_root_python"
        elif path.startswith("frontend/src/") and path.rsplit(".", 1)[-1] in {"ts", "tsx", "css", "json"} and path not in {"frontend/src/api/openapi.generated.ts", "frontend/src/api/types.ts"}:
            group = "frontend_src"
        elif path.startswith("tests/") and path.endswith(".py"):
            group = "tests_python"
        if group is None:
            continue
        content = git("show", f"{ref}:{path}").decode()
        groups[group] += len(content.splitlines())
        if path.startswith("app/") and path.endswith(".py"):
            for node in ast.walk(ast.parse(content)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                            and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app"
                            and dec.func.attr in HTTP and dec.args and isinstance(dec.args[0], ast.Constant)):
                        route = str(dec.args[0].value)
                        routes.add(route)
                        operations.add((dec.func.attr.upper(), route))
    return {"commit": ref, "tree": git("rev-parse", f"{ref}^{{tree}}").decode().strip(),
            "literal_lines": groups, "decorated_http_operations": len(operations),
            "distinct_http_paths": len(routes)}


if __name__ == "__main__":
    print(json.dumps({"method": __doc__.strip(), "baseline": inventory(BASE), "final": inventory(FINAL)}, indent=2))
