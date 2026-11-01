"""Fake harness binaries for bin/bot tests.

install(bindir, scenario_dir) writes claude, codex, copilot and opencode scripts into
bindir. Each run reads <scenario_dir>/<harness>.json, one object per invocation in
order (the last one repeats):

    {"stdout": "...", "stderr": "...", "exit": 0, "sleep": 0}

Each script also appends the argv of every invocation, one per line, to
<scenario_dir>/<harness>.args, the prompt it was given to <harness>.prompts, and the
*_TOKEN/*_KEY/*_SECRET variables it saw to <harness>.secrets, so a test can check flags,
session handling, secrets and what the launcher said. `claude auth status`
and `codex login status` answer from <scenario_dir>/login (present: logged in).

Helpers build the output a real harness produces for an outcome, so the launcher's
classifier is tested against realistic text, not against the regex it uses.
"""
import json
import os
import stat
import uuid

HARNESSES = ("claude", "codex", "copilot", "opencode")

SCRIPT = r'''#!/usr/bin/env python3
import json, os, re, sys, time
name = os.path.basename(sys.argv[0])
sdir = os.environ["FAKE_SCENARIO_DIR"]
args = sys.argv[1:]
with open(os.path.join(sdir, name + ".args"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
with open(os.path.join(sdir, name + ".secrets"), "a") as fh:   # the secrets each run was given
    fh.write(json.dumps({k: v for k, v in os.environ.items() if re.fullmatch(r"[A-Z][A-Z0-9_]*_(TOKEN|KEY|SECRET)", k)}) + "\n")
with open(os.path.join(sdir, name + ".env"), "a") as fh:     # the login variables each run saw
    fh.write(json.dumps({k: os.environ.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "GH_CONFIG_DIR")}) + "\n")
with open(os.path.join(sdir, name + ".config"), "a") as fh:  # the inline OpenCode configuration each run saw
    fh.write(json.dumps(os.environ.get("OPENCODE_CONFIG_CONTENT")) + "\n")
if name == "claude" and args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": os.path.exists(os.path.join(sdir, "login"))})); sys.exit(0)
if name == "codex" and args[:2] == ["login", "status"]:
    if os.path.exists(os.path.join(sdir, "login")): print("Logged in using ChatGPT"); sys.exit(0)
    print("Not logged in", file=sys.stderr); sys.exit(1)
prompt = ""
if name == "copilot":
    if "-p" in args: prompt = args[args.index("-p") + 1]
elif args:
    prompt = args[-1]
with open(os.path.join(sdir, name + ".prompts"), "a") as fh:
    fh.write(prompt.replace("\n", "\\n") + "\n")
path = os.path.join(sdir, name + ".json")
try:
    plan = json.load(open(path))
except (OSError, ValueError):
    plan = []
count_path = os.path.join(sdir, name + ".count")
try:
    n = int(open(count_path).read())
except (OSError, ValueError):
    n = 0
open(count_path, "w").write(str(n + 1))
step = plan[min(n, len(plan) - 1)] if plan else {"stdout": "", "stderr": "", "exit": 0}
time.sleep(float(step.get("sleep", 0)))
out = step.get("stdout", "")
if "{SESSION}" in out:
    sid = ""
    for flag in ("--session-id", "--resume", "--session"):
        if flag in args: sid = args[args.index(flag) + 1]
    if name == "codex" and "resume" in args: sid = args[args.index("resume") + 1]
    out = out.replace("{SESSION}", sid or step.get("new_session", "fake-session-1"))
sys.stdout.write(out); sys.stdout.flush()
sys.stderr.write(step.get("stderr", "")); sys.stderr.flush()
sys.exit(int(step.get("exit", 0)))
'''


def install(bindir, scenario_dir):
    os.makedirs(bindir, exist_ok=True)
    os.makedirs(scenario_dir, exist_ok=True)
    for name in HARNESSES:
        path = os.path.join(bindir, name)
        with open(path, "w") as fh:
            fh.write(SCRIPT)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def plan(scenario_dir, harness, steps):
    """Write the steps a harness answers with, in order; the last repeats."""
    with open(os.path.join(scenario_dir, harness + ".json"), "w") as fh:
        json.dump(steps, fh)
    for suffix in (".count",):
        try:
            os.remove(os.path.join(scenario_dir, harness + suffix))
        except FileNotFoundError:
            pass


def secrets(scenario_dir, harness):
    """The *_TOKEN/*_KEY/*_SECRET variables each invocation ran with, one dict per run."""
    try:
        return [json.loads(l) for l in open(os.path.join(scenario_dir, harness + ".secrets")) if l.strip()]
    except OSError:
        return []


def runs(scenario_dir, harness):
    """argv of each invocation so far (auth checks included)."""
    try:
        with open(os.path.join(scenario_dir, harness + ".args")) as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        return []


def prompts(scenario_dir, harness):
    try:
        with open(os.path.join(scenario_dir, harness + ".prompts")) as fh:
            return [l.rstrip("\n").replace("\\n", "\n") for l in fh]
    except FileNotFoundError:
        return []


# ---- what each real harness prints, per outcome ------------------------------------------
# Captured from claude 2.1.304, codex-cli 0.163.0 and opencode 1.18.11 on 2026-10-20 where
# a probe was possible (lost sessions, the result object); the rest follows their formats.

def claude_result(text="ok", **fields):
    """The one result object `claude -p --output-format json` prints."""
    base = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 3500,
            "duration_api_ms": 3400, "num_turns": 1, "result": text, "session_id": "{SESSION}",
            "total_cost_usd": 0.41, "api_error_status": None, "permission_denials": [],
            "stop_reason": "end_turn", "terminal_reason": "completed", "uuid": str(uuid.uuid4())}
    base.update(fields)
    return json.dumps(base) + "\n"


def claude(outcome, text="ok"):
    """(stdout, stderr, exit) for an outcome, as claude -p --output-format json behaves."""
    if outcome == "ok":
        return claude_result(text), "", 0
    if outcome == "warned_overflow":      # completed, with a warning in the words of an overflow
        return claude_result(text), "warn: prompt is too long, compacting\n", 0
    if outcome == "session_lost":
        return "", "No conversation found with session ID: {SESSION}\n", 1
    if outcome == "context_overflow":
        return claude_result("API Error: 400 {\"type\":\"error\",\"error\":{\"type\":\"invalid_request_error\","
                             "\"message\":\"prompt is too long\"}}", is_error=True,
                             subtype="error_during_execution", api_error_status=400), "", 1
    if outcome == "bad_invocation":
        return "", "error: unknown option '--no-such-flag'\n", 1
    if outcome == "logged_out":
        return claude_result("Not logged in · Please run /login", is_error=True, subtype="error_during_execution",
                             api_error_status=401), "", 1
    if outcome == "logged_out_refresh":   # the stored login's refresh token has expired: no result object (#295)
        return "", "Failed to authenticate: OAuth session expired and could not be refreshed\n", 1
    if outcome == "limited":
        return claude_result("You've hit your weekly limit · resets Oct 26 at 6am (America/Los_Angeles)",
                             is_error=True, subtype="error_during_execution", api_error_status=429), "", 1
    if outcome == "limited_zero_exit":   # an older claude, text mode: the refusal is the whole output, exit 0 (#81)
        return "You've hit your weekly limit · resets Oct 26 at 6am (America/Los_Angeles)\n", "", 0
    if outcome == "capacity":
        return claude_result("API Error: 529 {\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\","
                             "\"message\":\"Overloaded\"}}", is_error=True, subtype="error_during_execution",
                             api_error_status=529), "", 1
    if outcome == "retry":
        return claude_result("API Error: 500 Internal server error", is_error=True,
                             subtype="error_during_execution", api_error_status=500), "", 1
    if outcome == "retry_network":
        return "", "fetch failed: getaddrinfo ENOTFOUND api.anthropic.com\n", 1
    if outcome == "exhausted":
        return claude_result("", is_error=True, subtype="error_max_turns", num_turns=50), "", 1
    if outcome == "exhausted_budget":
        return claude_result("", is_error=True, subtype="error_max_budget_usd", total_cost_usd=5.0), "", 1
    if outcome == "unknown":
        return claude_result("API Error: 400 {\"type\":\"error\",\"error\":{\"type\":\"invalid_request_error\","
                             "\"message\":\"messages: tool_use ids must be unique\"}}", is_error=True,
                             subtype="error_during_execution", api_error_status=400), "", 1
    if outcome == "unknown_no_result":    # died before printing a result object
        return "", "TypeError: Cannot read properties of undefined (reading 'content')\n", 1
    if outcome == "text_ok":              # an older claude or a text harness: no result object
        return text + "\n", "", 0
    raise ValueError(outcome)


def codex_events(*events):
    return "".join(json.dumps(e) + "\n" for e in events)


def codex(outcome, text="ok"):
    started = {"type": "thread.started", "thread_id": "{SESSION}"}
    done = {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
    msg = {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    if outcome == "ok":
        return codex_events(started, msg, done), "", 0
    if outcome == "session_lost":
        return "", "Error: thread/resume: thread/resume failed: no rollout found for thread id {SESSION} (code -32600)\n", 1
    if outcome == "context_overflow":
        return codex_events(started, {"type": "turn.failed", "error": {"message": "Your input exceeds the context window of this model",
                                                                        "code": "context_length_exceeded"}}), "", 1
    if outcome == "warned_overflow":
        return codex_events(started, msg, done), "context_length_exceeded: trimming history\n", 0
    if outcome == "context_overflow_wording":
        return codex_events(started, {"type": "turn.failed", "error": {"message": "Codex ran out of room in the model's context window"}}), "", 1
    if outcome == "recovered_overflow":   # reported, retried and finished: exit 0 (#329)
        return codex_events(started, {"type": "error", "message": "Your input exceeds the context window of this model"}, msg, done), "", 0
    if outcome == "bad_invocation":
        return "", "error: unexpected argument '--no-such-flag' found\n\nUsage: codex exec [OPTIONS] [PROMPT]\n", 2
    if outcome == "logged_out":
        return codex_events(started, {"type": "error", "message": "Not logged in. Run codex login."}), "", 1
    if outcome == "limited":
        return codex_events(started, {"type": "turn.failed", "error": {"message": "You have exceeded your quota",
                                                                        "code": "insufficient_quota"}}), "", 0
    if outcome == "capacity":
        return codex_events(started, {"type": "turn.failed", "error": {
            "message": "Selected model is at capacity. Please try a different model."}}), "", 1
    if outcome == "retry":
        return codex_events(started, {"type": "error", "message": "stream disconnected before completion: "
                                                                   "error sending request: connection reset by peer"}), "", 1
    if outcome == "exhausted":
        return codex_events(started, {"type": "turn.failed", "error": {"message": "max turns reached"}}), "", 1
    if outcome == "unknown":
        return codex_events(started, {"type": "turn.failed", "error": {"message": "sandbox failed"}}), "", 1
    if outcome == "quoted_limit":   # the model talks about limits; nothing is wrong
        return codex_events(started, {"type": "item.completed", "item": {
            "type": "agent_message", "text": "Confirmed: matches session limit|usage limit|rate limit in the output"}},
            {"type": "item.completed", "item": {"type": "command_execution",
                                                "aggregated_output": "ERROR: usage limit reached"}}, done), "", 0
    raise ValueError(outcome)


def opencode_events(*events):
    return "".join(json.dumps(e) + "\n" for e in events)


def opencode(outcome, text="ok"):
    ev = {"type": "text", "sessionID": "{SESSION}", "part": {"text": text}}
    if outcome == "ok":
        return opencode_events(ev), "", 0
    if outcome == "session_lost":
        return "", "\x1b[91m\x1b[1mError: \x1b[0mSession not found\n", 1
    if outcome == "recovered_overflow":   # reported, compacted and finished: exit 0 (#329)
        return opencode_events({"type": "error", "sessionID": "{SESSION}", "error": {"name": "UnknownError", "data": {
            "message": "Context size has been exceeded."}}}, ev), "", 0
    if outcome == "warned_overflow":
        return opencode_events(ev), "Context size has been exceeded, compacting\n", 0
    if outcome == "context_overflow_wording":
        return opencode_events({"type": "error", "sessionID": "{SESSION}", "error": {"name": "APIError", "data": {
            "message": "the request exceeds the available context size", "type": "exceed_context_size_error"}}}), "", 1
    if outcome == "context_overflow":     # a llama.cpp server with --no-context-shift, as opencode reports it (#323)
        return opencode_events({"type": "error", "sessionID": "{SESSION}", "error": {"name": "UnknownError", "data": {
            "message": json.dumps({"message": "Context size has been exceeded.", "type": "server_error", "code": 500})}}}), "", 1
    if outcome == "bad_invocation":
        return "", "Unknown argument: no-such-flag\n", 1
    if outcome == "limited":
        return opencode_events({"type": "error", "sessionID": "{SESSION}",
                                "error": {"name": "ProviderError", "message": "429 rate limit reached"}}), "", 1
    if outcome == "retry":
        return opencode_events({"type": "error", "sessionID": "{SESSION}",
                                "error": {"name": "APIError", "message": "503 Service Unavailable"}}), "", 1
    if outcome == "unknown":
        return opencode_events({"type": "error", "sessionID": "{SESSION}",
                                "error": {"name": "UnknownError", "message": "tool exploded"}}), "", 1
    raise ValueError(outcome)


def copilot(outcome, text="ok"):
    """Copilot CLI prints plain text; errors go to stderr."""
    if outcome == "ok":
        return text + "\n", "", 0
    if outcome == "session_lost":
        return "", "Error: session {SESSION} not found\n", 1
    if outcome == "context_overflow":
        return "", "Error: prompt is too long: 210000 tokens > 200000 maximum\n", 1
    if outcome == "quoted_overflow":      # a short reply that only talks about it, exit 0
        return "The log says: prompt is too long.\n", "", 0
    if outcome == "bad_invocation":
        return "", "error: unknown option '--no-such-flag'\n", 1
    if outcome == "logged_out":
        return "", ("Error: No authentication information found.\n\nCopilot can be authenticated with GitHub using an OAuth "
                    "Token or a Fine-Grained Personal Access Token.\n\nTo authenticate, you can use any of the following "
                    "methods:\n  • Start 'copilot' and run the '/login' command\n"), 1      # copilot 1.0.105, no login (#285)
    if outcome == "logged_out_token":     # a token GitHub refuses
        return "", ("Error: Authentication token found but could not be validated.\n\n  Failed to fetch PAT user login "
                    "(401): GitHub returned: Bad credentials\n\nYour token may still be valid. Check your network "
                    "connection and try again.\n"), 1
    if outcome == "limited":
        return "", "Error: rate limit exceeded, too many requests\n", 1
    if outcome == "retry":
        return "", "Error: request to https://api.githubcopilot.com failed, reason: connect ETIMEDOUT\n", 1
    if outcome == "unknown":
        return "", "Error: something else went wrong\n", 1
    raise ValueError(outcome)


BUILDERS = {"claude": claude, "codex": codex, "opencode": opencode, "copilot": copilot}


def step(harness, outcome, text="ok", **extra):
    out, err, code = BUILDERS[harness](outcome, text)
    return {"stdout": out, "stderr": err, "exit": code, **extra}
