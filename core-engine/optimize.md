optimize — prompt text compressor

A small CLI tool that reduces prompt size while preserving meaning using simple rule-based text rewriting.

⸻

What it does

* Shortens verbose phrases (e.g. “in order to” → “to”)
* Removes filler words (“please”, “thank you”, “basically”, etc.)
* Cleans up punctuation and spacing
* Avoids changing code, shell commands, or scripts
* Estimates or measures token usage

⸻

Why it exists

To reduce LLM cost and token usage before sending prompts, without using another model.

⸻

Usage

Single text

./optimize.py "your text here"

Pipe input

echo "your text here" | ./optimize.py

Copy result (Mac)

./optimize.py --copy "your text here"

⸻

Batch mode

./optimize.py --batch prompts.txt

Save output:

./optimize.py --batch prompts.txt --out optimized.txt

⸻

Input format (batch)

* One prompt per line
    or
* Separate prompts with:

---

⸻

Output

For each prompt:

* Original tokens
* Optimized tokens
* Tokens saved
* Small preview of optimized text

Example:

120 → 85 tokens (−35, 29%)

⸻

Token counting

* Uses Anthropic API for exact token counts (if key is set)
* Falls back to rough estimate if not available

⸻

Safety rule

Never modifies:

* Code blocks
* CLI commands
* Scripts or shell pipelines

⸻

Goal

Make prompts:

* shorter
* cheaper
* cleaner
    without changing meaning
