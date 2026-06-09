calibrate — same-prompt LLM brevity benchmark

This tool measures how much token savings you get when forcing a model to be more concise, using real Anthropic API usage data.

What it does

* Takes a small set of benchmark prompts (e.g. explanations like hash maps, HTTPS, TCP vs UDP)
* Sends each prompt twice to the same model:
    * Baseline run: normal response
    * Concise run: same prompt + a “be brief” instruction
* Reads actual output tokens from the API response
* Computes token savings per prompt and overall percentage reduction

Key idea

It performs a true same-prompt A/B test:

* Only difference = presence of a brevity instruction
* Measures real output_tokens, not estimated length

Output

For each prompt:

* baseline output tokens vs concise output tokens
* token savings per request

At the end:

* total tokens saved
* percentage reduction
* model used

Dashboard reporting

* Sends results to a central dashboard (/api/record)
* Tracks:
    * output tokens
    * input tokens
    * concise vs non-concise runs
    * model, host, user metadata

Sampling & execution

* Randomly samples prompts from a small gauge set
* Optionally repeats periodically (--every N minutes)
* Can be tuned via environment variables

Configuration

* CALIBRATE_MODEL → model to test
* CALIBRATE_MAX_TOKENS → max output length
* SAMPLE → number of prompts per run
* CONCISE_NOTE → custom brevity instruction
* INFERENCEIQ_DASHBOARD → metrics endpoint

Purpose

* Quantifies the real impact of “be concise” prompting
* Helps estimate cost savings (output tokens are the main cost driver)
* Provides a baseline for prompt optimization, routing, and caching strategies
