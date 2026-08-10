"""CLI entrypoints for the local Gemma 3 1B runtime.

Examples:
  python -m local_llm download
  python -m local_llm chat
  python -m local_llm serve --host 0.0.0.0 --port 8088
  python -m local_llm complete "Write a haiku about staging."
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import DEFAULT_HOST, DEFAULT_PORT, MODEL_FILENAME, MODELS_DIR, QUANT, default_model_path
from .download import ensure_model
from .engine import LocalGemma, detect_runtime


def cmd_download(args: argparse.Namespace) -> int:
    path = ensure_model(force=args.force)
    print(f"ready: {path} ({path.stat().st_size / (1024 * 1024):.1f} MB)")
    print(f"quant: {QUANT}  file: {MODEL_FILENAME}")
    return 0


def cmd_info(_: argparse.Namespace) -> int:
    path = default_model_path()
    info = {
        "model_path": str(path),
        "exists": path.exists(),
        "size_mb": round(path.stat().st_size / (1024 * 1024), 1) if path.exists() else None,
        "models_dir": str(MODELS_DIR),
        "quant": QUANT,
        "runtime": detect_runtime(),
    }
    print(json.dumps(info, indent=2))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    ensure_model()
    with LocalGemma(verbose=args.verbose) as llm:
        result = llm.complete(
            args.prompt,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
        )
        assert hasattr(result, "text")
        print(result.text)
        if args.show_usage:
            print(
                f"\n[{result.prompt_tokens} prompt + {result.completion_tokens} completion tokens]",
                file=sys.stderr,
            )
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    ensure_model()
    messages: list[dict[str, str]] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})

    with LocalGemma(verbose=args.verbose) as llm:
        print(f"model={llm.model_id} backend={llm.runtime['backend']} threads={llm.runtime['n_threads']}")
        print("chat ready. empty line or Ctrl-D to exit.\n")
        while True:
            try:
                user = input(">>> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user:
                break
            messages.append({"role": "user", "content": user})
            if args.stream:
                print("...", end="", flush=True)
                chunks: list[str] = []
                for piece in llm.chat(
                    messages,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    stream=True,
                ):
                    if not chunks:
                        print("\r   ", end="\r", flush=True)
                    print(piece, end="", flush=True)
                    chunks.append(piece)
                print()
                text = "".join(chunks)
            else:
                result = llm.chat(
                    messages,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                )
                assert hasattr(result, "text")
                text = result.text
                print(text)
            messages.append({"role": "assistant", "content": text})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    ensure_model()
    import uvicorn

    from .openai_api import create_app

    engine = LocalGemma(verbose=args.verbose)
    app = create_app(engine)
    print(
        f"serving {engine.model_id} on http://{args.host}:{args.port}/v1 "
        f"(backend={engine.runtime['backend']})"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m local_llm", description="Local Gemma 3 1B (GGUF)")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("download", help="download the QAT GGUF into local_llm/models/")
    d.add_argument("--force", action="store_true")
    d.set_defaults(func=cmd_download)

    i = sub.add_parser("info", help="show model path and detected runtime knobs")
    i.set_defaults(func=cmd_info)

    c = sub.add_parser("complete", help="one-shot text completion")
    c.add_argument("prompt")
    c.add_argument("--max-tokens", type=int, default=256)
    c.add_argument("--temperature", type=float, default=0.0)
    c.add_argument("--verbose", action="store_true")
    c.add_argument("--show-usage", action="store_true")
    c.set_defaults(func=cmd_complete)

    ch = sub.add_parser("chat", help="interactive chat (OpenAI message format)")
    ch.add_argument("--system", default="")
    ch.add_argument("--max-tokens", type=int, default=256)
    ch.add_argument("--temperature", type=float, default=0.0)
    ch.add_argument("--stream", action="store_true")
    ch.add_argument("--verbose", action="store_true")
    ch.set_defaults(func=cmd_chat)

    s = sub.add_parser("serve", help="OpenAI-compatible HTTP server")
    s.add_argument("--host", default=DEFAULT_HOST)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
