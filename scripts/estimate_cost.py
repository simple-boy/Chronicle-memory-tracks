"""Planning estimate only. No API calls; counts are NOT official Full workload sizes."""
import argparse
import json
import math


def image_tokens(width, height, detail):
    if detail == "low":
        return 2833
    scale = min(1.0, 2048 / max(width, height))
    width, height = width * scale, height * scale
    scale = min(1.0, 768 / min(width, height))
    return 2833 + 5667 * math.ceil(width * scale / 512) * math.ceil(height * scale / 512)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--code-chunks", type=int, default=0)
    p.add_argument("--images", type=int, default=0)
    p.add_argument("--width", type=int, default=1024)
    p.add_argument("--height", type=int, default=1024)
    p.add_argument("--detail", choices=["low", "high"], default="high")
    p.add_argument("--text-input-tokens-per-call", type=int, default=2500)
    p.add_argument("--output-tokens-per-call", type=int, default=384)
    args = p.parse_args()
    if min(args.code_chunks, args.images, args.text_input_tokens_per_call, args.output_tokens_per_call) < 0 or min(args.width,args.height) <= 0:
        p.error("counts must be nonnegative and dimensions positive")
    calls = args.code_chunks + args.images
    inputs = calls * args.text_input_tokens_per_call + args.images * image_tokens(args.width, args.height, args.detail)
    outputs = calls * args.output_tokens_per_call
    cost = (inputs * 0.15 + outputs * 0.60) / 1_000_000
    print(json.dumps({"kind": "planning_estimate_not_measured", "pricing_date": "2026-10-03",
        "model": "gpt-4o-mini", "calls": calls, "input_tokens": inputs, "output_token_budget": outputs,
        "estimated_usd_no_retries": round(cost,6), "estimated_usd_two_attempts": round(cost*2,6),
        "excluded": "storage, compute, bandwidth, taxes, repeated failed Add, actual workload uncertainty",
        "source": "https://developers.openai.com/api/docs/models/gpt-4o-mini"}, indent=2))


if __name__ == "__main__":
    main()
