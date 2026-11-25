"""`odata-extractor` command line (also `python -m odata_extractor`).

Exit codes for `extract`: 0 COMPLETE, 3 INDETERMINATE (warning), 4 INCOMPLETE, 5 FAILED.
`validate`: 0 consistent, 4 problems found. Any command: 1 unexpected error, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

EXIT = {"COMPLETE": 0, "INDETERMINATE": 3, "INCOMPLETE": 4, "FAILED": 5}


def cmd_seed(args: argparse.Namespace) -> int:
    from .dataset import COMPANY, digest, generate

    data = generate(args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    sizes = ", ".join(f"{k} {len(v)}" for k, v in data.items())
    print(f"{COMPANY}: wrote {args.out} ({sizes}); digest {digest(data)}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .faults import SCENARIOS
    from .mock_server import MockServer

    token = os.environ.get(args.require_token_env) if args.require_token_env else None
    if args.require_token_env and not token:
        print(f"environment variable {args.require_token_env} is empty", file=sys.stderr)
        return 2
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("the mock server only binds to a loopback address", file=sys.stderr)
        return 2
    server = MockServer(args.scenario, args.port, args.page_size, token, args.host)
    scenario = SCENARIOS[args.scenario]
    print(f"mock OData service at {server.url} (scenario {scenario.name}: {scenario.description})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _config(args: argparse.Namespace) -> Any:
    from .client import ClientConfig, RetryPolicy

    token = os.environ.get(args.token_env) if args.token_env else None
    if args.token_env and not token:
        raise SystemExit(f"environment variable {args.token_env} is empty")
    return ClientConfig(service_root=args.base_url, timeout=args.timeout, page_size=args.page_size, max_pages=args.max_pages,
                        allowed_hosts=tuple(args.allow_host), token=token, reconcile=not args.no_reconcile,
                        retry=RetryPolicy(max_attempts=args.max_attempts, base_delay=args.backoff_base))


def cmd_extract(args: argparse.Namespace) -> int:
    from .pipeline import run
    from .privacy import Pseudonymizer

    outcome = run(_config(args), args.entity, args.output, key=args.key, rules_path=args.privacy,
                  pseudonymizer=Pseudonymizer.from_environment(demo=args.demo_pseudonym_key))
    integrity, metrics = outcome.integrity, outcome.run.metrics.to_dict()
    print(f"{args.entity}: {integrity.status}  requests {metrics['requests_total']}, retries {metrics['retries']}, "
          f"pages {integrity.counts.get('pages')}, unique records {integrity.counts.get('unique_records')}")
    for item in integrity.reasons:
        print(f"  {item['code']}: {item['message']}")
    for item in integrity.warnings:
        print(f"  warning {item['code']}: {item['message']}")
    print(f"  output: {outcome.output}")
    return EXIT[integrity.status]


def cmd_inspect(args: argparse.Namespace) -> int:
    from .client import ClientConfig, ODataClient
    from .pipeline import fetch_metadata
    from .schema import infer_from_samples, render_report

    client = ODataClient(ClientConfig(service_root=args.base_url, timeout=args.timeout, reconcile=False))
    metadata, error = (None, None) if args.no_metadata else fetch_metadata(client)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 5
    entities = [args.entity] if args.entity else sorted(metadata or {})
    if not entities:
        print("no $metadata available; pass --entity to infer from samples", file=sys.stderr)
        return 2
    for entity in entities:
        inferred = None
        if metadata is None or args.sample:
            url = client.entity_url(entity, {"$top": str(args.sample or 50)})
            _, payload = client.request(url, None, "inspect")
            known = {name: info["key"][0] for name, info in (metadata or {}).items() if info["key"]}
            inferred = infer_from_samples(payload["value"], known)
        declared = (metadata or {}).get(entity)
        if args.json:
            print(json.dumps({"entity": entity, "metadata": declared, "inferred": inferred}, indent=2))
        else:
            print(render_report(entity, declared, inferred))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .export import validate_output

    problems = validate_output(args.directory)
    print("output is consistent" if not problems else "problems found:")
    for problem in problems:
        print(f"  {problem}")
    return 0 if not problems else 4


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import render, run_all

    traces = run_all(args.scenario)
    for trace in traces:
        mark = "ok " if trace["as_expected"] else "!! "
        m = trace["metrics"]
        print(f"{mark}{trace['name']:<20} {trace['status']:<14} requests {m['requests_total']:>3}  pages {m['pages']:>2}  "
              f"received {m['records_received']:>3}  unique {m['unique_records']:>3}  retries {m['retries']}")
    unexpected = [t["name"] for t in traces if not t["as_expected"]]
    if args.out:
        content = render(traces)
        if args.check:
            if not args.out.exists() or args.out.read_text(encoding="utf-8") != content:
                print(f"{args.out} is out of date; run `python -m odata_extractor demo --out {args.out}`", file=sys.stderr)
                return 1
            print(f"{args.out} is up to date")
        else:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(content.encode("utf-8"))
            print(f"wrote {args.out}")
    print(f"{len(traces) - len(unexpected)}/{len(traces)} scenarios produced their expected integrity status")
    return 1 if unexpected else 0


def cmd_scenarios(_: argparse.Namespace) -> int:
    from .faults import DEMO_ORDER, SCENARIOS

    for name in DEMO_ORDER:
        s = SCENARIOS[name]
        print(f"{name:<20} {'conformant' if s.conformant else 'provider defect':<16} expect {s.expected_status:<14} {s.description}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    from .faults import SCENARIOS

    parser = argparse.ArgumentParser(prog="odata-extractor", description="Resilient OData extraction with integrity checks.")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="write the synthetic dataset to a JSON file")
    seed.add_argument("--out", type=Path, default=Path("data/dataset.json"))
    seed.add_argument("--seed", type=int, default=20260115)
    seed.set_defaults(func=cmd_seed)

    serve = sub.add_parser("serve", help="run the local mock OData service")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--scenario", choices=sorted(SCENARIOS), default="healthy")
    serve.add_argument("--page-size", type=int, default=50)
    serve.add_argument("--require-token-env", help="require a bearer token read from this environment variable")
    serve.set_defaults(func=cmd_serve)

    extract = sub.add_parser("extract", help="extract one entity set, assess integrity, write outputs")
    extract.add_argument("--base-url", required=True, help="service root, e.g. http://127.0.0.1:8765/odata/")
    extract.add_argument("--entity", required=True)
    extract.add_argument("--output", type=Path, default=Path("output"))
    extract.add_argument("--key", help="key property (default: read from $metadata)")
    extract.add_argument("--page-size", type=int, default=50, help="requested via Prefer: odata.maxpagesize")
    extract.add_argument("--timeout", type=float, default=10.0, help="seconds per request")
    extract.add_argument("--max-attempts", type=int, default=5, help="attempts per request, including the first")
    extract.add_argument("--backoff-base", type=float, default=0.5, help="first backoff ceiling in seconds")
    extract.add_argument("--max-pages", type=int, default=1000)
    extract.add_argument("--allow-host", action="append", default=[], help="extra host[:port] nextLinks may point to")
    extract.add_argument("--token-env", help="read a bearer token from this environment variable (never written out)")
    extract.add_argument("--no-reconcile", action="store_true", help="skip the independent key reconciliation pass (the run can then be at best INDETERMINATE)")
    extract.add_argument("--privacy", type=Path, help="privacy rules JSON (default: bundled rules)")
    extract.add_argument("--demo-pseudonym-key", action="store_true", help="use the public demo key for reproducible pseudonyms")
    extract.set_defaults(func=cmd_extract)

    inspect = sub.add_parser("inspect-schema", help="report the schema from $metadata or from sample records")
    inspect.add_argument("--base-url", required=True)
    inspect.add_argument("--entity")
    inspect.add_argument("--sample", type=int, default=0, help="also infer from this many sample records")
    inspect.add_argument("--no-metadata", action="store_true", help="ignore $metadata and infer only")
    inspect.add_argument("--timeout", type=float, default=10.0)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    validate = sub.add_parser("validate", help="check an output folder against its manifest and report")
    validate.add_argument("directory", type=Path)
    validate.set_defaults(func=cmd_validate)

    demo = sub.add_parser("demo", help="run every failure scenario against the mock server")
    demo.add_argument("--scenario", choices=sorted(SCENARIOS))
    demo.add_argument("--out", type=Path, help="also write demo data, e.g. demo/data.js")
    demo.add_argument("--check", action="store_true", help="with --out: fail if the file differs from a fresh run")
    demo.set_defaults(func=cmd_demo)

    scenarios = sub.add_parser("scenarios", help="list the failure scenarios")
    scenarios.set_defaults(func=cmd_scenarios)
    return parser


def main(argv: list[str] | None = None) -> int:
    from .client import ExtractionError

    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, ExtractionError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
