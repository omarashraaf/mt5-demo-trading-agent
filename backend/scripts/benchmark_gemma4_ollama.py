import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


SERVER_URL = "http://10.0.0.10:11434"
BENCHMARK_MODEL = "gemma4:e4b"
REQUIRED_MODEL_CHECK = "gemma4:31b"
REQUEST_TIMEOUT_SECONDS = 600
REPORT_PATH = Path(__file__).resolve().parents[2] / "gemma4_benchmark_report.md"


SCENARIOS = [
    (
        "A. Simple prompt",
        "Explain RSI in simple words in 3 short bullet points.",
    ),
    (
        "B. Warm simple prompt again",
        "Explain RSI in simple words in 3 short bullet points.",
    ),
    (
        "C. Realistic LinkTrade decision prompt",
        "\n".join(
            [
                "System intent:",
                "You are LinkTrade's primary trade decision model.",
                "Use SmartAgent baseline, recent chart structure, Benzinga/news relevance, meta-model preview,",
                "strategy context, risk mode, and recent trade outcomes.",
                "Return JSON only with:",
                "action, confidence, stop_loss_price, take_profit_price, max_hold_minutes,",
                "strategy, summary_reason, market_regime, risk_note.",
                "If evidence is mixed or weak, prefer HOLD.",
                "",
                "Input context:",
                "Symbol: EURUSD",
                "Policy mode: balanced",
                "SmartAgent baseline: mild SELL bias",
                "5m trend: lower highs, weak bearish momentum",
                "15m trend: range-bound, resistance holding twice",
                "1h trend: neutral to slightly bearish",
                "Recent bars summary: price rejected from intraday resistance and failed breakout",
                "Benzinga/news relevance: no major relevant event in the last hour",
                "Meta-model preview: 0.58 confidence, low edge",
                "Recent trade outcomes: last 3 breakout longs underperformed, 2 hit stop loss",
                "Position status: flat",
                "Need concise structured JSON only",
            ]
        ),
    ),
    (
        "D. Realistic LinkTrade prompt repeated a second time",
        "\n".join(
            [
                "System intent:",
                "You are LinkTrade's primary trade decision model.",
                "Use SmartAgent baseline, recent chart structure, Benzinga/news relevance, meta-model preview,",
                "strategy context, risk mode, and recent trade outcomes.",
                "Return JSON only with:",
                "action, confidence, stop_loss_price, take_profit_price, max_hold_minutes,",
                "strategy, summary_reason, market_regime, risk_note.",
                "If evidence is mixed or weak, prefer HOLD.",
                "",
                "Input context:",
                "Symbol: EURUSD",
                "Policy mode: balanced",
                "SmartAgent baseline: mild SELL bias",
                "5m trend: lower highs, weak bearish momentum",
                "15m trend: range-bound, resistance holding twice",
                "1h trend: neutral to slightly bearish",
                "Recent bars summary: price rejected from intraday resistance and failed breakout",
                "Benzinga/news relevance: no major relevant event in the last hour",
                "Meta-model preview: 0.58 confidence, low edge",
                "Recent trade outcomes: last 3 breakout longs underperformed, 2 hit stop loss",
                "Position status: flat",
                "Need concise structured JSON only",
            ]
        ),
    ),
]


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url=url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(raw.decode(charset))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection error calling {url}: {exc.reason}") from exc


def ns_to_seconds(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 1_000_000_000.0
    except (TypeError, ValueError):
        return None


def short_preview(text: str, limit: int = 220) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def classify_result(warm_realistic_wall_time: float | None) -> str:
    if warm_realistic_wall_time is None:
        return "Too slow to use in current setup"
    if warm_realistic_wall_time <= 5:
        return "Good for live trading loop"
    if warm_realistic_wall_time <= 15:
        return "Acceptable only for slower/manual decisions"
    if warm_realistic_wall_time <= 30:
        return "Better only for post-trade analysis / deep review"
    return "Too slow to use in current setup"


def benchmark_generate(model_name: str, scenario_name: str, prompt: str) -> dict:
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
    }
    started = time.perf_counter()
    response = http_json("POST", f"{SERVER_URL}/api/generate", payload)
    wall_time = time.perf_counter() - started

    if response.get("error"):
        raise RuntimeError(f"{scenario_name} failed: {response['error']}")

    return {
        "scenario": scenario_name,
        "wall_time_seconds": wall_time,
        "total_duration_seconds": ns_to_seconds(response.get("total_duration")),
        "load_duration_seconds": ns_to_seconds(response.get("load_duration")),
        "prompt_eval_duration_seconds": ns_to_seconds(response.get("prompt_eval_duration")),
        "eval_duration_seconds": ns_to_seconds(response.get("eval_duration")),
        "prompt_eval_count": response.get("prompt_eval_count"),
        "eval_count": response.get("eval_count"),
        "done": response.get("done"),
        "done_reason": response.get("done_reason"),
        "response_preview": short_preview(response.get("response", "")),
        "raw_response": response.get("response", ""),
    }


def print_line(label: str, value: str = "") -> None:
    if value:
        print(f"{label}: {value}")
    else:
        print(label)


def print_result(result: dict) -> None:
    print("=" * 72)
    print(result["scenario"])
    print("-" * 72)
    print_line("Wall time", f"{result['wall_time_seconds']:.3f}s")
    print_line("Ollama total_duration", format_seconds(result["total_duration_seconds"]))
    print_line("Ollama load_duration", format_seconds(result["load_duration_seconds"]))
    print_line("Ollama prompt_eval_duration", format_seconds(result["prompt_eval_duration_seconds"]))
    print_line("Ollama eval_duration", format_seconds(result["eval_duration_seconds"]))
    print_line("Prompt tokens", str(result["prompt_eval_count"]))
    print_line("Eval tokens", str(result["eval_count"]))
    print_line("Done", str(result["done"]))
    print_line("Done reason", str(result["done_reason"]))
    print_line("Response preview", result["response_preview"] or "<empty>")
    print()


def format_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}s"


def write_report(
    timestamp_utc: str,
    tags_payload: dict,
    benchmark_results: list[dict],
    conclusion: str,
) -> None:
    available_models = sorted(
        str(model.get("name", ""))
        for model in tags_payload.get("models", [])
        if isinstance(model, dict) and model.get("name")
    )

    lines = [
        "# Gemma 4 Ollama Benchmark Report",
        "",
        f"- Timestamp (UTC): {timestamp_utc}",
        f"- Server URL: {SERVER_URL}",
        f"- Benchmark model: {BENCHMARK_MODEL}",
        f"- Required model check: {REQUIRED_MODEL_CHECK}",
        f"- Required model present: {'yes' if REQUIRED_MODEL_CHECK in available_models else 'no'}",
        f"- Benchmark model present: {'yes' if BENCHMARK_MODEL in available_models else 'no'}",
        "",
        "## Available Models",
        "",
    ]

    if available_models:
        lines.extend(f"- {name}" for name in available_models)
    else:
        lines.append("- <none returned>")

    for result in benchmark_results:
        lines.extend(
            [
                "",
                f"## {result['scenario']}",
                "",
                f"- Wall time: {result['wall_time_seconds']:.3f}s",
                f"- Ollama total_duration: {format_seconds(result['total_duration_seconds'])}",
                f"- Ollama load_duration: {format_seconds(result['load_duration_seconds'])}",
                f"- Ollama prompt_eval_duration: {format_seconds(result['prompt_eval_duration_seconds'])}",
                f"- Ollama eval_duration: {format_seconds(result['eval_duration_seconds'])}",
                f"- Prompt tokens: {result['prompt_eval_count']}",
                f"- Eval tokens: {result['eval_count']}",
                f"- Done: {result['done']}",
                f"- Done reason: {result['done_reason']}",
                f"- Response preview: {result['response_preview'] or '<empty>'}",
                "",
                "```text",
                result["raw_response"] or "",
                "```",
            ]
        )

    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            f"- Recommendation: {conclusion}",
        ]
    )

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_failure_report(timestamp_utc: str, title: str, detail: str) -> None:
    lines = [
        "# Gemma 4 Ollama Benchmark Report",
        "",
        f"- Timestamp (UTC): {timestamp_utc}",
        f"- Server URL: {SERVER_URL}",
        f"- Benchmark model: {BENCHMARK_MODEL}",
        f"- Required model check: {REQUIRED_MODEL_CHECK}",
        "",
        "## Result",
        "",
        f"- Status: {title}",
        f"- Detail: {detail}",
        "",
        "## Conclusion",
        "",
        "- Recommendation: Benchmark could not be completed because the Ollama server was unreachable or the model was unavailable.",
    ]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    print(f"Gemma 4 benchmark started at {timestamp_utc}")
    print_line("Server URL", SERVER_URL)
    print_line("Benchmark model", BENCHMARK_MODEL)
    print_line("Required model check", REQUIRED_MODEL_CHECK)
    print()

    try:
        tags_payload = http_json("GET", f"{SERVER_URL}/api/tags")
    except Exception as exc:
        print(f"Connectivity check failed: {exc}")
        write_failure_report(timestamp_utc, "Connectivity check failed", str(exc))
        print_line("Report written to", str(REPORT_PATH))
        return 1

    model_names = sorted(
        str(model.get("name", ""))
        for model in tags_payload.get("models", [])
        if isinstance(model, dict) and model.get("name")
    )

    print("Connectivity check: OK")
    print_line("Available models returned", str(len(model_names)))
    print_line("Required model present", "yes" if REQUIRED_MODEL_CHECK in model_names else "no")
    print_line("Benchmark model present", "yes" if BENCHMARK_MODEL in model_names else "no")
    print()

    if BENCHMARK_MODEL not in model_names:
        print(f"Benchmark model '{BENCHMARK_MODEL}' was not found on the Ollama server.")
        if REQUIRED_MODEL_CHECK not in model_names:
            print(f"Required model check '{REQUIRED_MODEL_CHECK}' was also not found.")
        write_failure_report(
            timestamp_utc,
            "Benchmark model missing",
            f"Available models did not include benchmark model '{BENCHMARK_MODEL}'.",
        )
        print_line("Report written to", str(REPORT_PATH))
        return 2

    results = []
    try:
        for scenario_name, prompt in SCENARIOS:
            result = benchmark_generate(BENCHMARK_MODEL, scenario_name, prompt)
            results.append(result)
            print_result(result)
    except Exception as exc:
        print(f"Benchmark failed: {exc}")
        write_failure_report(timestamp_utc, "Benchmark request failed", str(exc))
        print_line("Report written to", str(REPORT_PATH))
        return 3

    warm_realistic = next((item for item in results if item["scenario"].startswith("D.")), None)
    conclusion = classify_result(warm_realistic["wall_time_seconds"] if warm_realistic else None)

    print("=" * 72)
    print_line("Final recommendation", conclusion)
    print_line("Report written to", str(REPORT_PATH))

    write_report(timestamp_utc, tags_payload, results, conclusion)
    return 0


if __name__ == "__main__":
    sys.exit(main())
