"""Claude Code CLI subprocess client.

Replaces AsyncAnthropic. Every LLM call spawns `claude -p` as a subprocess,
using the machine's own Claude login. No API keys, no gateway.

Structured output is obtained via `--json-schema`, which makes Claude emit a
`StructuredOutput` tool_use block we parse from the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bifrost.cli.config import BifrostSettings, get_settings
from bifrost.cli.config_sections import is_mcp_tool_name
from bifrost.errors import ConfigError, UpstreamError

logger = logging.getLogger(__name__)


# ── Process-wide concurrency limiter for `claude -p` spawns ──────────────────
#
# Every LLM call in Bifrost spawns a `claude -p` subprocess that authenticates
# against the machine's single Claude OAuth session. Under load — the chat
# fan-out (6 research agents in parallel) PLUS the extraction worker
# (BIFROST_MAX_CONCURRENT_EXTRACTIONS, each extraction spawning ~6 `claude -p`
# internally) — 20-40 concurrent spawns hammer the same OAuth session. The API
# overloads at the very first request and `claude -p` exits 1 right after
# emitting its `init` event. In isolation the identical call succeeds, so this
# is OAuth/API oversubscription, not a logic bug.
#
# The fix: a single module-level semaphore acquired around the ACTUAL spawn +
# the subprocess lifetime in every invocation path. Because the API research
# agents and the extraction worker both run in the same backend event loop, one
# process-wide semaphore throttles BOTH — exactly the intent: stop
# oversubscribing the shared OAuth session.
#
# The semaphore is created lazily on first use (so it binds to the running
# event loop) and cached for the process lifetime. ``asyncio.Semaphore`` with
# no explicit loop binds to the loop of the first ``acquire``; recreating it if
# the loop changes (e.g. a fresh loop per test) keeps it correct under pytest.
_CLI_SEMAPHORE: asyncio.Semaphore | None = None
_CLI_SEMAPHORE_LIMIT: int | None = None
_CLI_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None


def _get_cli_semaphore(limit: int) -> asyncio.Semaphore:
    """Return the process-wide spawn semaphore, creating it once.

    Bound to the running event loop. If the loop changes (new loop per test) or
    the configured limit changes, the semaphore is rebuilt so it stays correct.
    """
    global _CLI_SEMAPHORE, _CLI_SEMAPHORE_LIMIT, _CLI_SEMAPHORE_LOOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _CLI_SEMAPHORE is None or limit != _CLI_SEMAPHORE_LIMIT or _CLI_SEMAPHORE_LOOP is not loop:
        _CLI_SEMAPHORE = asyncio.Semaphore(limit)
        _CLI_SEMAPHORE_LIMIT = limit
        _CLI_SEMAPHORE_LOOP = loop
    return _CLI_SEMAPHORE


class ClaudeCliError(UpstreamError):
    """Base exception for CLI client errors."""

    code = "llm.cli"
    retryable = False
    http_status = 502


class ClaudeCliTransientError(ClaudeCliError):
    """Transient error (rate limit, connection, timeout) — worth retrying."""

    code = "llm.cli.transient"
    retryable = True
    http_status = 503


class ClaudeCliAuthError(ClaudeCliError):
    """Auth failure — not retryable. User must run `claude auth login`.

    Maps to 500 (not 401): this is the *server's* own Claude login being
    broken, a server-side config fault, not the API caller's credentials.
    """

    code = "llm.cli.auth"
    retryable = False
    http_status = 500


def _is_max_turns_error(exc: BaseException) -> bool:
    """Detect Claude CLI ``error_max_turns`` failures from the
    rendered error string.

    Claude CLI surfaces this as a ``transient failure (exit 1)`` whose
    JSON tail contains ``"errors":["Reached maximum number of turns
    (N)"]`` and ``"subtype":"error_max_turns"``. The text matching is
    deliberate — the canonical machine-readable form would require
    parsing the full Claude CLI stream-json envelope inside cli_client,
    which adds coupling for a single-purpose detector.
    """
    s = str(exc)
    return (
        "error_max_turns" in s
        or "maximum number of turns" in s.lower()
        or "Reached maximum number of turns" in s
    )


class ClaudeCliParseError(ClaudeCliError):
    """Response couldn't be parsed as structured JSON."""

    code = "llm.cli.parse"
    retryable = False
    http_status = 502


@dataclass(frozen=True)
class Usage:
    """Per-call usage metadata, shaped to match anthropic.types.Usage."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    total_cost_usd: float


@dataclass(frozen=True)
class CliCompletion:
    """Parsed result of a single `claude -p` invocation."""

    parsed: dict  # StructuredOutput tool input — matches the caller's json_schema
    usage: Usage
    text: str  # Final assistant text response (may be empty when max_turns=1)


_TRANSIENT_STDERR_PATTERNS = (
    "rate_limit",
    "rate limit",
    "overloaded",
    "connection error",
    "connection reset",
    "network",
    "timeout",
    "timed out",
    " 503",
    " 529",
    " 502",
    " 504",
)

_AUTH_STDERR_PATTERNS = (
    "not authenticated",
    "authentication required",
    "please log in",
    "unauthorized",
    "claude login",
    "auth failed",
)


def _permission_args(allowed_tools: list[str] | None) -> list[str]:
    """Flags that make ``allowed_tools`` the complete set of tools the CLI may run.

    ``--tools`` only filters built-ins; ``--allowedTools`` is what gates MCP tools,
    and ``dontAsk`` denies everything not listed instead of approving it.
    """
    tools = list(allowed_tools or [])
    for name in tools:
        if not name.strip() or "," in name or "{mcp:" in name:
            raise ConfigError(f"invalid tool name in allowed_tools: {name!r}")
    builtins = ",".join(t for t in tools if not is_mcp_tool_name(t))
    args = ["--tools", builtins]
    if tools:
        args += ["--allowedTools", ",".join(tools)]
    return [*args, "--permission-mode", "dontAsk"]


class ClaudeLlmClient:
    """Async client that invokes Claude Code CLI per call.

    Each call is a fresh `claude -p` process. Session state is not persisted
    across calls (`--no-session-persistence`), MCP servers / skills / plugins
    are stripped so every spawn loads ~200 tokens of baseline instead of 50K.
    """

    def __init__(self, settings: BifrostSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._cli_path = self._settings.claude_cli_path
        self._timeout = self._settings.claude_cli_timeout_seconds
        # Process-wide ceiling on concurrent `claude -p` spawns (shared OAuth
        # session oversubscription guard). See module-level _get_cli_semaphore.
        self._max_concurrent_cli_calls = int(getattr(self._settings, "max_concurrent_cli_calls", 6))
        self._neutral_cwd = self._ensure_neutral_cwd()
        # Content-addressed response cache (cost control). Built once per
        # client; defensive so it can never break a call. See llm_cache.py.
        self._cache = None
        if getattr(self._settings, "llm_cache_enabled", False):
            from bifrost.extraction.llm_cache import LlmResponseCache

            self._cache = LlmResponseCache(self._settings)

    @staticmethod
    def _ensure_neutral_cwd() -> str:
        """Create a persistent empty directory for subprocess CWDs.

        Claude Code auto-discovers CLAUDE.md from CWD; using a neutral dir
        keeps our calls free of project context bleed.
        """
        base = Path(tempfile.gettempdir()) / "bifrost_claude_cli_cwd"
        base.mkdir(parents=True, exist_ok=True)
        claudemd = base / "CLAUDE.md"
        if claudemd.exists():
            with contextlib.suppress(OSError):
                claudemd.unlink()
        return str(base)

    async def stream_text(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model: str,
        max_turns: int = 1,
        allowed_tools: list[str] | None = None,
        add_dirs: list[str] | None = None,
        force_refresh: bool = False,
    ):
        """Stream assistant text token-by-token via `claude --output-format stream-json`.

        Yields dicts:
          {"type": "delta", "text": <str>} — text token(s)
          {"type": "tool_use", "name": str, "input": dict} — when Claude invokes a tool
          {"type": "done", "usage": Usage, "final_text": <str>}

        If `allowed_tools` + `add_dirs` are provided, Claude can explore the
        local repo via Read/Glob/Grep tools.
        """
        # Content-addressed cache (no-tool/no-dir only — see call_with_json_schema).
        # On a hit, replay the cached answer as one delta + a $0 done, so the
        # caller (synthesis) accumulates the full text just like a live stream.
        stream_cache_key: str | None = None
        if self._cache is not None and not force_refresh and not allowed_tools and not add_dirs:
            from bifrost.extraction.llm_cache import make_cache_key

            stream_cache_key = make_cache_key(
                system_prompt=system_prompt,
                user_message=user_message,
                model=model,
                json_schema=None,
                max_turns=max_turns,
            )
            hit = await self._cache.get(stream_cache_key)
            if hit is not None:
                cached_text = hit.get("text", "")
                saved = float(hit.get("cost_usd", 0.0) or 0.0)
                logger.info(
                    "llm_cache HIT stream (%s…) — saved $%.4f",
                    stream_cache_key[-16:],
                    saved,
                )
                await self._cache.record_saving(model=model, saved_usd=saved)
                if cached_text:
                    yield {"type": "delta", "text": cached_text}
                yield {"type": "done", "usage": Usage(0, 0, 0, 0, 0.0), "final_text": cached_text}
                return

        argv = [
            self._cli_path,
            "-p",
            "--system-prompt",
            system_prompt,
            *_permission_args(allowed_tools),
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--input-format",
            "text",
            "--include-partial-messages",
            "--verbose",  # required when stream-json + partial messages
            "--no-session-persistence",
            "--max-turns",
            str(max_turns),
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--setting-sources",
            "",
        ]
        # Grant Claude read access to additional directories (e.g., a cloned repo)
        if add_dirs:
            argv.append("--add-dir")
            argv.extend(add_dirs)

        # Process-wide spawn limiter: hold a semaphore slot for the entire
        # subprocess lifetime (spawn → reaped) so concurrent streams + the
        # extraction worker never oversubscribe the shared OAuth session.
        # Released exactly once via `_release_sem()` on every exit path
        # (normal completion, timeout, cancellation, early generator close).
        sem = _get_cli_semaphore(self._max_concurrent_cli_calls)
        await sem.acquire()
        sem_released = False

        def _release_sem() -> None:
            nonlocal sem_released
            if not sem_released:
                sem_released = True
                sem.release()

        try:
            # stream-json emits large JSON-per-line events (thinking + signatures can be huge).
            # Default asyncio StreamReader limit is 64KB; we bump to 16MB per line.
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._neutral_cwd,
                env=_subprocess_env(),
                limit=16 * 1024 * 1024,
            )
        except BaseException:
            _release_sem()
            raise

        # Send the user prompt and close stdin so the process knows we're done
        if proc.stdin is not None:
            proc.stdin.write(user_message.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        buffer = ""
        final_usage = Usage(0, 0, 0, 0, 0.0)
        final_text_parts: list[str] = []
        try:
            assert proc.stdout is not None
            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=self._timeout,
                )
                if not line:
                    break
                try:
                    evt = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue

                # Each stream-json event — care about text deltas, tool_use, and final result
                etype = evt.get("type")
                if etype == "assistant":
                    msg = evt.get("message", {}) or {}
                    for block in msg.get("content", []) or []:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text = block.get("text") or ""
                            if text and text != buffer:
                                delta = text[len(buffer) :] if text.startswith(buffer) else text
                                buffer = text
                                if delta:
                                    yield {"type": "delta", "text": delta}
                        elif btype == "tool_use":
                            # Claude used a tool (Read/Glob/Grep). Emit event so UI can show it.
                            yield {
                                "type": "tool_use",
                                "name": block.get("name", ""),
                                "input": block.get("input", {}),
                            }
                elif etype == "stream_event":
                    # Anthropic native stream events (content_block_delta)
                    event = evt.get("event", {}) or {}
                    delta = event.get("delta", {}) or {}
                    if delta.get("type") == "text_delta":
                        t = delta.get("text") or ""
                        if t:
                            buffer += t
                            yield {"type": "delta", "text": t}
                elif etype == "result":
                    # Final event — capture usage + final text
                    if evt.get("result"):
                        final_text_parts.append(str(evt["result"]))
                    usage_raw = evt.get("usage") or {}
                    final_usage = Usage(
                        input_tokens=int(usage_raw.get("input_tokens", 0) or 0),
                        output_tokens=int(usage_raw.get("output_tokens", 0) or 0),
                        cache_read_input_tokens=int(
                            usage_raw.get("cache_read_input_tokens", 0) or 0,
                        ),
                        cache_creation_input_tokens=int(
                            usage_raw.get("cache_creation_input_tokens", 0) or 0,
                        ),
                        total_cost_usd=float(evt.get("total_cost_usd", 0.0) or 0.0),
                    )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            _release_sem()
            raise ClaudeCliTransientError(
                f"claude -p stream timed out after {self._timeout}s",
            ) from exc
        except BaseException:
            # Any exit before the subprocess is reaped — cancellation, the
            # caller abandoning the generator (GeneratorExit), or any other
            # error — must kill the subprocess (so it stops consuming CLI
            # session bandwidth) and free the semaphore slot. TimeoutError is
            # handled above (it re-raises as transient); everything else
            # propagates unchanged after cleanup.
            proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            _release_sem()
            raise

        # Drain stderr in case there was an error
        stderr_bytes = b""
        if proc.stderr is not None:
            try:
                stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=3)
            except TimeoutError:
                stderr_bytes = b""

        rc = await proc.wait()
        # Subprocess is reaped — release the spawn slot before any cache work.
        _release_sem()
        if rc != 0:
            _raise_for_stderr(stderr_bytes.decode("utf-8", errors="replace"), returncode=rc)

        final_text = buffer or "".join(final_text_parts)
        # Store the completed answer + its cost for exact-prompt reuse (the cost
        # lets a later hit report $ saved). Defensive put.
        #
        # CACHE-POISONING GUARD: only cache a SUBSTANTIVE answer.
        # A near-empty stream (e.g. synthesis that degraded to one line, a
        # "nothing found"/"couldn't complete" blurb) must NOT be cached — if
        # it were, every identical retry would replay that bad answer for $0
        # and the user could never improve it by re-running. We require a
        # minimum length so a real (long, structured) synthesis is cached but
        # a degraded stub is left uncached and re-attempted live next time.
        if (
            stream_cache_key is not None
            and self._cache is not None
            and _is_cacheable_text(final_text)
        ):
            await self._cache.put(
                stream_cache_key,
                {"text": final_text, "cost_usd": final_usage.total_cost_usd, "model": model},
            )
        yield {"type": "done", "usage": final_usage, "final_text": final_text}

    async def call_with_dynamic_max_turns(
        self,
        *,
        initial_max_turns: int,
        max_attempts: int = 2,
        growth: float = 1.6,
        **kwargs: Any,
    ) -> CliCompletion:
        """Wrapper around ``call_with_json_schema`` that auto-extends
        the turn budget when the model hits ``error_max_turns``.

        Why: agents that paginate through MCP search results (Slack,
        GitHub, Confluence) sometimes need more turns than the static
        budget allows. Hardcoding a single max_turns trades cost (high
        cap = always pay) against reliability (low cap = error_max_turns
        + $0.50 wasted per failure). Dynamic extension pays the higher
        cost ONLY when the first attempt actually demonstrates a need.

        On a max_turns error we retry once with ``int(max_turns * growth)``
        and proportionally extended ``timeout_override``. The retry is
        cache-warm (Anthropic prompt caching pays for the second pass
        in cache-read price, ~$0.10–0.20 incremental, not full $0.85).

        Other error classes (auth, transient network, parse failure)
        are NOT retried here — they're not bigger-budget problems.
        """
        max_turns = initial_max_turns
        timeout_override = kwargs.pop("timeout_override", None)
        last_exc: ClaudeCliError | None = None
        for attempt in range(max_attempts):
            try:
                return await self.call_with_json_schema(
                    max_turns=max_turns,
                    timeout_override=timeout_override,
                    **kwargs,
                )
            except ClaudeCliError as exc:
                last_exc = exc
                if attempt < max_attempts - 1 and _is_max_turns_error(exc):
                    new_turns = max(int(max_turns * growth), max_turns + 4)
                    new_timeout = (
                        int(timeout_override * growth) if timeout_override is not None else None
                    )
                    logger.info(
                        "max_turns hit (attempt %d/%d, model=%s) — "
                        "retrying with max_turns=%d (was %d), timeout=%s",
                        attempt + 1,
                        max_attempts,
                        kwargs.get("model"),
                        new_turns,
                        max_turns,
                        new_timeout,
                    )
                    max_turns = new_turns
                    timeout_override = new_timeout
                    continue
                raise
        # Should be unreachable — the loop always either returns or
        # raises. Re-raise the last seen exception just in case.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("call_with_dynamic_max_turns: empty loop")

    async def call_with_json_schema(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model: str,
        json_schema: dict,
        max_turns: int = 2,
        timeout_override: float | None = None,
        allowed_tools: list[str] | None = None,
        add_dirs: list[str] | None = None,
        inherit_user_mcp: bool = False,
        force_refresh: bool = False,
        cache_key_override: str | None = None,
    ) -> CliCompletion:
        """Run a single Claude call that must produce JSON matching `json_schema`.

        ``cache_key_override``: callers whose prompt contains a per-call random
        token (e.g. the extraction canary) must pass a deterministic key
        computed over the STABLE content, or the response cache would never hit.

        When ``allowed_tools`` + ``add_dirs`` are set, Claude can read local
        code (Read/Glob/Grep) during its reasoning and still produce the
        structured JSON at the end. Used by multi-repo research sub-agents.

        Returns the parsed dict plus usage. Raises ClaudeCliAuthError on auth
        failure, ClaudeCliTransientError on rate/connection/timeout issues,
        ClaudeCliParseError if no StructuredOutput block was emitted.
        """
        # Content-addressed cache: exact re-invocations return $0 instead of
        # re-burning. Defensive — a miss/error just proceeds to a live call.
        # ONLY cache no-tool/no-dir calls: a call that reads the live repo
        # (allowed_tools/add_dirs) isn't deterministic from its prompt, and
        # those (research sub-agents) are already cached sha-aware by the
        # semantic agent_cache. Extraction passes file content inline, so its
        # prompt hash correctly invalidates when content changes.
        cache_key: str | None = None
        if self._cache is not None and not force_refresh and not allowed_tools and not add_dirs:
            from bifrost.extraction.llm_cache import make_cache_key

            cache_key = cache_key_override or make_cache_key(
                system_prompt=system_prompt,
                user_message=user_message,
                model=model,
                json_schema=json_schema,
                max_turns=max_turns,
                allowed_tools=allowed_tools,
                add_dirs=add_dirs,
            )
            hit = await self._cache.get(cache_key)
            if hit is not None:
                saved = float(hit.get("cost_usd", 0.0) or 0.0)
                logger.info("llm_cache HIT (%s…) — saved $%.4f", cache_key[-16:], saved)
                await self._cache.record_saving(model=model, saved_usd=saved)
                # Usage zeroed: a hit costs nothing now (the saving is logged separately).
                return CliCompletion(
                    parsed=hit.get("parsed", {}),
                    usage=Usage(0, 0, 0, 0, 0.0),
                    text=hit.get("text", ""),
                )

        # Pipe user_message via stdin so its contents (which may start with "---"
        # or other flag-looking sequences) can't be parsed as CLI flags.
        argv = [
            self._cli_path,
            "-p",
            "--system-prompt",
            system_prompt,
            *_permission_args(allowed_tools),
            "--model",
            model,
            "--output-format",
            "json",
            "--input-format",
            "text",
            "--no-session-persistence",
            "--max-turns",
            str(max_turns),
        ]
        if not inherit_user_mcp:
            # Extraction phases: strict no-MCP so we don't accidentally
            # leak credentials or hit user-configured servers.
            argv.extend(
                [
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                ]
            )
        # inherit_user_mcp: load the operator's MCP servers. Only --allowedTools
        # (with dontAsk) limits which of their tools run; --tools is built-ins only.
        argv.extend(
            [
                "--disable-slash-commands",
                "--setting-sources",
                "user" if inherit_user_mcp else "",
                "--json-schema",
                json.dumps(json_schema),
            ]
        )
        if add_dirs:
            argv.append("--add-dir")
            argv.extend(add_dirs)

        # Process-wide spawn limiter: hold a semaphore slot from spawn until the
        # subprocess is reaped (communicate returns / killed) so the chat
        # fan-out + extraction worker never oversubscribe the shared OAuth
        # session. ``async with`` guarantees release on every exit path.
        sem = _get_cli_semaphore(self._max_concurrent_cli_calls)
        async with sem:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._neutral_cwd,
                env=_subprocess_env(),
            )

            effective_timeout = timeout_override or self._timeout
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(user_message.encode("utf-8")),
                    timeout=effective_timeout,
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise ClaudeCliTransientError(
                    f"claude -p timed out after {effective_timeout}s"
                ) from exc
            except asyncio.CancelledError:
                # Task was cancelled (user hit DELETE on the task). Kill the
                # subprocess so it doesn't linger holding the semaphore slot.
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except TimeoutError:
                    pass  # subprocess ignored SIGTERM — let the OS reap it
                raise

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # For non-zero exits: the CLI can exit 1 for graceful degradations
        # (error_max_turns, usage caps hit mid-conversation) where stdout
        # still contains a valid event stream. Try parsing first; fall back
        # to stderr classification only if parse fails OR the result event
        # has a subtype we actually care about.
        if proc.returncode != 0:
            try:
                events = json.loads(stdout) if stdout else []
            except json.JSONDecodeError:
                events = []

            # Did we at least get a structured event stream?
            if events and _has_parseable_result(events):
                logger.info(
                    "claude -p exit=%d but stream has result — parsing anyway",
                    proc.returncode,
                )
                return await self._store_completion(cache_key, _extract_completion(events), model)

            logger.warning(
                "claude -p exit=%d stderr=%r stdout_tail=%r",
                proc.returncode,
                stderr[:400],
                stdout[-2000:],
            )
            # An exit-1 with NO parseable result is inherently retryable: the
            # canonical case is the CLI emitting only a `system/init` event and
            # then dying (OAuth-session oversubscription / API overload at the
            # first request). stderr is usually empty and the only signal is the
            # init event in stdout, which matches no transient pattern — so we
            # default these to ClaudeCliTransientError so callers retry.
            _raise_for_stderr(
                stderr or stdout,
                returncode=proc.returncode,
                transient_default=True,
            )

        try:
            events = json.loads(stdout)
        except json.JSONDecodeError as exc:
            logger.warning("claude -p produced non-JSON stdout: %s", stdout[:500])
            raise ClaudeCliParseError(f"Could not parse claude -p JSON output: {exc}") from exc

        return await self._store_completion(cache_key, _extract_completion(events), model)

    async def _store_completion(
        self, cache_key: str | None, completion: CliCompletion, model: str
    ) -> CliCompletion:
        """Persist a fresh completion under its cache key (if caching on),
        then return it unchanged. Stores parsed JSON + final text + the cost it
        incurred, so a later hit can report the $ saved.

        CACHE-POISONING GUARD: a completion is only cached when it
        carries real signal. An empty result (no parsed dict AND no text) or a
        parsed report that self-reports as a failure / "nothing found" is NOT
        stored — otherwise an identical retry would replay the bad result for
        $0 and the user could never recover by re-running. See
        ``_completion_is_cacheable``.
        """
        if (
            cache_key is not None
            and self._cache is not None
            and _completion_is_cacheable(completion)
        ):
            await self._cache.put(
                cache_key,
                {
                    "parsed": completion.parsed,
                    "text": completion.text,
                    "cost_usd": completion.usage.total_cost_usd,
                    "model": model,
                },
            )
        return completion


# Keep subprocess env minimal so an injected agent that runs
# `printenv` or `env` cannot exfiltrate the parent process's secrets
# (JIRA_API_TOKEN, DATABASE_URL, GitHub PAT, etc.). Allowlist-based:
# anything not in this set is stripped.
_SAFE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "SHELL",
        "TMPDIR",
        "VIRTUAL_ENV",
        "CLAUDE_CODE_NO_SESSION_PERSISTENCE",
    }
)


def build_minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal env for any claude subprocess (or shell verify).

    Pass-through allowlist of harmless keys (PATH, HOME, locale, virtualenv).
    Everything else — secrets, tokens, DSNs — is stripped. Also strips
    ANTHROPIC_* / BIFROST_ANTHROPIC_* so Claude Code falls through to OAuth
    rather than picking up a stale gateway key.
    """
    env: dict[str, str] = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    if extra:
        env.update(extra)
    return env


def _subprocess_env() -> dict[str, str]:
    """Backward-compatible alias.

    The historical implementation only stripped ANTHROPIC_* vars; the new
    behaviour is allowlist-based via ``build_minimal_env``. Existing callers
    keep working with a tighter posture.
    """
    return build_minimal_env()


def _raise_for_stderr(stderr: str, *, returncode: int, transient_default: bool = False) -> None:
    """Classify a non-zero exit by scanning stderr.

    ``transient_default``: when True, an exit that matches neither the auth nor
    the explicit-transient patterns is still treated as transient (retryable)
    rather than a hard ClaudeCliError. This is the init-then-exit-1 case — the
    CLI dies right after its `system/init` event under OAuth oversubscription,
    leaving empty stderr and only an init event in stdout. That failure is
    inherently retryable, so callers (the retry wrappers) get a chance to
    succeed once the contention clears. Auth errors still take precedence so a
    genuinely broken login is never silently retried forever.
    """
    lower = stderr.lower()
    if any(p in lower for p in _AUTH_STDERR_PATTERNS):
        raise ClaudeCliAuthError(
            f"Claude CLI is not authenticated. Run `claude auth login` to sign in.\n"
            f"stderr: {stderr.strip()[:500]}"
        )
    if transient_default or any(p in lower for p in _TRANSIENT_STDERR_PATTERNS):
        raise ClaudeCliTransientError(
            f"claude -p transient failure (exit {returncode}): {stderr.strip()[:500]}"
        )
    raise ClaudeCliError(f"claude -p failed (exit {returncode}): {stderr.strip()[:500]}")


def _has_parseable_result(events: list) -> bool:
    """Did claude -p emit at least one assistant StructuredOutput tool_use
    we can salvage, even if returncode was non-zero?

    Used to distinguish "graceful exit-1" (max_turns / usage cap hit AFTER
    producing output) from real failures.
    """
    if not isinstance(events, list):
        return False
    for obj in events:
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "assistant":
            msg = obj.get("message", {}) or {}
            for block in msg.get("content", []) or []:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "StructuredOutput"
                    and isinstance(block.get("input"), dict)
                ):
                    return True
    return False


def _extract_completion(events: list[dict]) -> CliCompletion:
    """Walk the stream of events from `--output-format json`."""
    parsed: dict | None = None
    text_parts: list[str] = []
    usage_obj: Usage | None = None
    is_error = False
    error_subtype = ""
    result_text = ""

    for obj in events:
        if not isinstance(obj, dict):
            continue
        otype = obj.get("type")
        if otype == "assistant":
            message = obj.get("message", {}) or {}
            for block in message.get("content", []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use" and block.get("name") == "StructuredOutput":
                    input_data = block.get("input")
                    if isinstance(input_data, dict):
                        parsed = input_data
                elif btype == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        text_parts.append(t)
        elif otype == "result":
            is_error = bool(obj.get("is_error", False))
            error_subtype = str(obj.get("subtype") or "")
            result_text = str(obj.get("result") or "")
            usage_raw = obj.get("usage") or {}
            usage_obj = Usage(
                input_tokens=int(usage_raw.get("input_tokens", 0) or 0),
                output_tokens=int(usage_raw.get("output_tokens", 0) or 0),
                cache_read_input_tokens=int(usage_raw.get("cache_read_input_tokens", 0) or 0),
                cache_creation_input_tokens=int(
                    usage_raw.get("cache_creation_input_tokens", 0) or 0
                ),
                total_cost_usd=float(obj.get("total_cost_usd", 0.0) or 0.0),
            )

    if parsed is None:
        if is_error and error_subtype not in {"success", "error_max_turns"}:
            raise ClaudeCliTransientError(
                f"claude -p errored ({error_subtype}): {result_text[:500]}"
            )
        raise ClaudeCliParseError(
            f"No StructuredOutput block in claude -p response "
            f"(is_error={is_error} subtype={error_subtype})"
        )

    if usage_obj is None:
        usage_obj = Usage(0, 0, 0, 0, 0.0)

    return CliCompletion(
        parsed=parsed,
        usage=usage_obj,
        text=("".join(text_parts) or result_text),
    )


# Minimum substantive length for a streamed answer to be worth caching.
# A real synthesis answer is hundreds–thousands of chars; a degraded stub
# ("Nothing useful was found.", "I couldn't complete this research.") is
# tens of chars. 40 is comfortably below any genuine answer and above any
# failure blurb.
_MIN_CACHEABLE_TEXT_LEN = 40

# Substrings that mark a self-reported failure/degraded result. If a parsed
# report or a short text answer contains one of these, we refuse to cache it
# so an identical retry runs live and can succeed.
_NON_CACHEABLE_MARKERS: tuple[str, ...] = (
    "nothing useful",
    "nothing was found",
    "nothing found",
    "no relevant",
    "couldn't complete",
    "could not complete",
    "didn't complete",
    "did not complete",
    "research failed",
    "no results found",
    "unable to find",
)


def _looks_like_failure_text(text: str) -> bool:
    low = (text or "").strip().lower()
    return any(m in low for m in _NON_CACHEABLE_MARKERS)


def _is_cacheable_text(text: str) -> bool:
    """A streamed text answer is cacheable only if it's substantive — long
    enough to be a real answer and not a self-reported failure blurb."""
    if not text or len(text.strip()) < _MIN_CACHEABLE_TEXT_LEN:
        return False
    return not _looks_like_failure_text(text)


def _completion_is_cacheable(completion: CliCompletion) -> bool:
    """Decide whether a structured completion carries enough signal to cache.

    Refuses to cache when:
      * the completion is empty (no parsed dict AND no text), or
      * the parsed report self-reports as a failure: an ``error`` field is
        set, or it's an all-``none``/empty relevance report with no findings.

    This is the structured-output analogue of ``_is_cacheable_text`` and is
    the core fix for cache poisoning: a router/agent/planner result that
    found nothing must not be the value an identical retry replays for $0.
    """
    parsed = completion.parsed or {}
    text = completion.text or ""

    # Wholly empty — never cache.
    if not parsed and not text.strip():
        return False

    if isinstance(parsed, dict):
        # Explicit error field set by an agent report → don't cache.
        err = parsed.get("error")
        if isinstance(err, str) and err.strip():
            return False
        # A report whose only signal is relevance="none" (and which carries
        # no findings/components/answer) is a "looked, found nothing" result.
        # Caching it would block a real answer on retry. We treat it as
        # cacheable ONLY if it also carries some positive payload.
        rel = parsed.get("relevance")
        if isinstance(rel, str) and rel.strip().lower() == "none":
            has_payload = any(
                bool(parsed.get(k))
                for k in (
                    "key_components",
                    "key_files",
                    "findings",
                    "answer",
                    "summary",
                    "rankings",
                )
            )
            if not has_payload:
                return False

    return True
